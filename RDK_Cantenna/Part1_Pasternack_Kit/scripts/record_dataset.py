"""Record a labelled dataset for DSP-01 (target-present / target-absent recordings).

    python Part1_Pasternack_Kit/scripts/record_dataset.py --label absent  --count 10          # nobody moving
    python Part1_Pasternack_Kit/scripts/record_dataset.py --label present --count 10          # a walker in the beam
    python Part1_Pasternack_Kit/scripts/record_dataset.py --label present --count 3 --sim     # synthetic, for dry runs

Each recording is `--frames` consecutive frames saved as one schema-v1 capture in
Part1_Pasternack_Kit/dataset/<label>_<NN>.npz with the radar parameters inside it.
There is a countdown before every recording so the walker can reset.

Protocol that makes the result defensible (write it on the Trello card):
  * same kit position, same room, same ramp time for the whole dataset;
  * 'absent'  = nobody within 30 m of the antennas, nothing moving;
  * 'present' = one person walking across/toward the beam at a normal pace,
                starting 4–8 m away, for the whole recording;
  * record 'absent' and 'present' alternately, not all of one then all of the other;
  * freeze the detector settings (score_dataset.py --freeze) BEFORE looking at results.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # RDK_Cantenna/ (the phalanx package)

from phalanx import SimConfig, SimulatedRadar, Target, cantenna_fmcw, save_capture   # noqa: E402

DATASET = Path(__file__).resolve().parents[1] / "dataset"   # Part1_Pasternack_Kit/dataset (recordings git-ignored; settings + results committed)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True, choices=["absent", "present"])
    ap.add_argument("--count", type=int, default=10, help="recordings to make")
    ap.add_argument("--frames", type=int, default=3, help="frames per recording (~1 s each)")
    ap.add_argument("--ramp-ms", type=float, default=4.0)
    ap.add_argument("--start", type=int, default=None, help="first index (default: continue numbering)")
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--countdown", type=int, default=5)
    ap.add_argument("--notes", default="", help="operator notes stored in every file")
    args = ap.parse_args()

    params = cantenna_fmcw(args.ramp_ms)
    DATASET.mkdir(exist_ok=True)
    existing = sorted(DATASET.glob(f"{args.label}_*.npz"))
    start = args.start if args.start is not None else (len(existing) + 1)

    if args.sim:
        targets = [Target(6.0, 1.2, 18.0)] if args.label == "present" else []
        radar = SimulatedRadar(params, SimConfig(targets=targets, seed=start))
    else:
        from phalanx.hardware.cantenna import CantennaRDK
        radar = CantennaRDK(params)

    print(params.summary())
    print(f"\nrecording {args.count} × '{args.label}' ({args.frames} frames each) → {DATASET}")
    with radar:
        radar.configure()
        radar.start()
        for i in range(start, start + args.count):
            name = DATASET / f"{args.label}_{i:02d}.npz"
            for n in range(args.countdown, 0, -1):
                print(f"  {name.name} in {n} ...", end="\r")
                time.sleep(0 if args.sim else 1)
            print(f"  {name.name} RECORDING          ")
            frames = [radar.capture() for _ in range(args.frames)]
            save_capture(name, frames, params, label=args.label,
                         extra={"notes": args.notes, "script": "record_dataset.py",
                                "frames_per_recording": args.frames})
            print(f"  {name.name} saved ({len(frames)} frames, "
                  f"{sum(f.meta.get('capture_s', 0) for f in frames):.1f} s)")
    print("done. Next: python Part1_Pasternack_Kit/scripts/score_dataset.py --freeze   (once), then score.")


if __name__ == "__main__":
    main()
