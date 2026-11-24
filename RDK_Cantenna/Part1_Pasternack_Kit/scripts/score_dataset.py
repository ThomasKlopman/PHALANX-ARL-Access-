"""Score a labelled dataset with FROZEN detector settings (DSP-01 Test / Audit).

    python Part1_Pasternack_Kit/scripts/score_dataset.py --freeze          # write dataset/detector_settings.json once
    python Part1_Pasternack_Kit/scripts/score_dataset.py                   # score every dataset/*.npz, write results_*.md
    python Part1_Pasternack_Kit/scripts/score_dataset.py --repeats 3       # also prove decisions are identical run-to-run

Decision rule (fixed in the settings file, not tunable at scoring time):
    a recording is "target present" when at least `min_frames_with_detection`
    of its frames contain a detection with snr_db >= `min_snr_db`.

Gates from the DSP-01 card: >= 8 of 10 present recordings detected,
<= 2 of 10 absent recordings flagged, and identical decisions on repeat.
Exit status is non-zero when a gate fails, so this can run in CI.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # RDK_Cantenna/ (the phalanx package)

from phalanx import ProcessorConfig, RangeDopplerProcessor, load_capture   # noqa: E402
from phalanx.dsp.detect import CfarConfig                                 # noqa: E402

DATASET = Path(__file__).resolve().parents[1] / "dataset"   # Part1_Pasternack_Kit/dataset (recordings git-ignored; settings + results committed)
SETTINGS = DATASET / "detector_settings.json"

DEFAULT_SETTINGS = {
    "processor": {
        "mti_order": 1, "min_range_m": 2.0, "max_range_m": 40.0,
        "max_abs_velocity_frac": 0.9, "min_abs_velocity_mps": 0.0,
        "ramp_parity": 1, "notch_hz": [],
    },
    "cfar": {"guard": [1, 2], "train": [3, 8], "pfa": 1e-4},
    "decision": {"min_frames_with_detection": 2, "min_snr_db": 12.0},
    "gates": {"present_min_detected": 8, "absent_max_flagged": 2, "n_each": 10},
}


def settings_hash(settings: dict) -> str:
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:12]


def build_processor(settings: dict, params):
    cfg = ProcessorConfig(cfar=CfarConfig(guard=tuple(settings["cfar"]["guard"]),
                                          train=tuple(settings["cfar"]["train"]),
                                          pfa=settings["cfar"]["pfa"]))
    for k, v in settings["processor"].items():
        setattr(cfg, k, tuple(v) if k == "notch_hz" else v)
    return RangeDopplerProcessor(params, cfg)


def score_file(path: Path, settings: dict) -> dict:
    frames, params, meta = load_capture(path)
    proc = build_processor(settings, params)
    rule = settings["decision"]
    frames_hit, best = 0, None
    for f in frames:
        r = proc.process(f)
        strong = [d for d in r.detections if d.snr_db >= rule["min_snr_db"]]
        if strong:
            frames_hit += 1
            top = max(strong, key=lambda d: d.snr_db)
            if best is None or top.snr_db > best["snr_db"]:
                best = top.as_dict()
    decision = frames_hit >= rule["min_frames_with_detection"]
    label = str(meta.get("label", path.stem.split("_")[0]))
    return {"file": path.name, "label": label, "n_frames": len(frames),
            "frames_with_detection": frames_hit, "decision": "present" if decision else "absent",
            "correct": (label == "present") == decision, "best": best}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--freeze", action="store_true", help="write the settings file (refuses to overwrite)")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--dataset", default=str(DATASET))
    args = ap.parse_args()
    dataset = Path(args.dataset)
    settings_path = dataset / "detector_settings.json"

    if args.freeze:
        if settings_path.exists():
            print(f"refusing to overwrite {settings_path} — delete it deliberately if you must")
            return 2
        dataset.mkdir(exist_ok=True)
        settings_path.write_text(json.dumps(DEFAULT_SETTINGS, indent=2))
        print(f"frozen detector settings → {settings_path}  (hash {settings_hash(DEFAULT_SETTINGS)})")
        return 0

    if not settings_path.exists():
        print("no frozen settings: run `score_dataset.py --freeze` BEFORE scoring")
        return 2
    settings = json.loads(settings_path.read_text())
    files = sorted(dataset.glob("*.npz"))
    if not files:
        print(f"no recordings in {dataset}")
        return 2
    frozen_at = settings_path.stat().st_mtime
    late = [f.name for f in files if f.stat().st_mtime < frozen_at]
    runs = []
    for _ in range(args.repeats):
        runs.append([score_file(f, settings) for f in files])
    rows = runs[0]
    deterministic = all(
        [(r["decision"], r["best"]) for r in run] == [(r["decision"], r["best"]) for r in rows]
        for run in runs[1:])

    present = [r for r in rows if r["label"] == "present"]
    absent = [r for r in rows if r["label"] == "absent"]
    n_det = sum(r["decision"] == "present" for r in present)
    n_fa = sum(r["decision"] == "present" for r in absent)
    g = settings["gates"]
    gate_present = n_det >= g["present_min_detected"] * len(present) / g["n_each"] if present else False
    gate_absent = n_fa <= g["absent_max_flagged"] * max(len(absent), 1) / g["n_each"]

    lines = [f"# DSP-01 scoring — {time.strftime('%Y-%m-%d %H:%M')}",
             f"settings: `{settings_path.name}` hash `{settings_hash(settings)}` "
             f"(frozen {time.strftime('%Y-%m-%d %H:%M', time.localtime(frozen_at))})",
             f"decision rule: ≥{settings['decision']['min_frames_with_detection']} frames with a "
             f"detection ≥{settings['decision']['min_snr_db']} dB", "",
             "| file | label | frames w/ det | decision | ok | best (m, m/s, dB) |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        b = r["best"]
        bs = f"{b['range_m']}, {b['velocity_mps']:+}, {b['snr_db']}" if b else "—"
        lines.append(f"| {r['file']} | {r['label']} | {r['frames_with_detection']}/{r['n_frames']} | "
                     f"{r['decision']} | {'✓' if r['correct'] else '✗'} | {bs} |")
    lines += ["",
              f"present detected: **{n_det}/{len(present)}** (gate ≥{g['present_min_detected']}/{g['n_each']}) "
              f"→ {'PASS' if gate_present else 'FAIL'}",
              f"absent flagged:   **{n_fa}/{len(absent)}** (gate ≤{g['absent_max_flagged']}/{g['n_each']}) "
              f"→ {'PASS' if gate_absent else 'FAIL'}"]
    if args.repeats > 1:
        lines.append(f"repeatability over {args.repeats} runs: {'identical → PASS' if deterministic else 'DIFFER → FAIL'}")
    if late:
        lines.append(f"\n⚠ recorded BEFORE the settings were frozen (fine, as long as settings were "
                     f"not tuned on them): {', '.join(late)}")
    report = "\n".join(lines)
    print(report)
    out = dataset / f"results_{time.strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text(report)
    print(f"\nwritten → {out}")
    ok = gate_present and gate_absent and deterministic
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
