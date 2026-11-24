"""Capture file format (AQ-01) — record frames with complete metadata, replay them exactly.

One `.npz` per recording. Schema v1 keys
--------------------------------------
samples          (n_frames, n_samples)  uint16 (Cantenna ADC words) or complex64 (SDR I/Q)
                 Stored exactly as the hardware delivered them; any scaling is redone on replay.
dtype            str   "uint16_offset_binary" | "complex64"
timestamps       (n_frames,) float64  host time.time() at frame arm
timestamp_source str   "host_clock" (Cantenna: the kit has no clock) | "device" (USRP time_spec)
seq              (n_frames,) int64    frame sequence numbers from the source
schema_version   int   1
label            str   free text ("absent", "walker_toward", ...)
device           str   *IDN? / UHD mboard string
driver           str   "phalanx <version>"
params_json      str   RadarParams as JSON (every field, so RadarParams(**json) reproduces it)
# convenience duplicates of the fields the acceptance criteria name explicitly:
fs_hz, center_frequency_hz, bandwidth_hz, waveform, sweep, ramp_time_s, channels, gain_db
signedness       str   "unsigned" | "signed" (Cantenna word interpretation at record time)
extra_json       str   anything else, JSON (e.g. CFAR config used, operator notes)

Rules: the file carries everything needed to re-run the DSP without the hardware
present, and `load_capture` must return samples that compare equal to what was
saved (tests/test_capture.py checks this bit-exactly).
"""
from __future__ import annotations

import dataclasses
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

import numpy as np

from . import __version__
from .frame import Frame
from .params import RadarParams

SCHEMA_VERSION = 1


def _params_to_json(p: RadarParams) -> str:
    return json.dumps(asdict(p))


def _params_from_json(s: str) -> RadarParams:
    d = json.loads(s)
    fields = {f.name for f in dataclasses.fields(RadarParams)}
    return RadarParams(**{k: v for k, v in d.items() if k in fields})


def frames_to_arrays(frames: list[Frame]) -> tuple[np.ndarray, str]:
    """Stack frames' raw samples. Prefers the hardware words in meta['raw']."""
    if all("raw" in f.meta for f in frames):
        arr = np.stack([np.asarray(f.meta["raw"]) for f in frames])
    else:
        arr = np.stack([np.asarray(f.samples) for f in frames])
    if arr.dtype == np.uint16:
        dtype = "uint16_offset_binary"
    elif np.iscomplexobj(arr):
        arr = arr.astype(np.complex64)
        dtype = "complex64"
    else:
        arr = arr.astype(np.float32)
        dtype = "float32"
    return arr, dtype


def save_capture(path: str | Path, frames: Iterable[Frame], params: RadarParams, *,
                 label: str = "", device: str = "", gain_db: float = float("nan"),
                 channels: int = 1, timestamp_source: str = "host_clock",
                 extra: dict | None = None) -> Path:
    frames = list(frames)
    if not frames:
        raise ValueError("nothing to save")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    samples, dtype = frames_to_arrays(frames)
    signed = frames[0].meta.get("signedness", "")
    device = device or frames[0].meta.get("idn", "")
    per_frame_meta = json.dumps([
        {k: v for k, v in f.meta.items() if k != "raw"} for f in frames
    ], default=lambda value: value.item() if isinstance(value, np.generic) else str(value))
    np.savez(
        path,
        schema_version=SCHEMA_VERSION,
        samples=samples,
        frame_meta_json=per_frame_meta,
        dtype=dtype,
        timestamps=np.array([f.timestamp for f in frames], dtype=np.float64),
        timestamp_source=timestamp_source,
        seq=np.array([f.seq for f in frames], dtype=np.int64),
        label=label,
        device=device,
        driver=f"phalanx {__version__}",
        params_json=_params_to_json(params),
        fs_hz=params.fs_hz,
        center_frequency_hz=params.fc_hz,
        bandwidth_hz=params.bandwidth_hz,
        waveform=params.waveform,
        sweep=params.sweep,
        ramp_time_s=params.ramp_time_s,
        channels=channels,
        gain_db=gain_db,
        signedness=signed,
        extra_json=json.dumps(extra or {}),
        saved_at=time.time(),
    )
    return path if path.suffix == ".npz" else path.with_suffix(path.suffix + ".npz")


def load_capture(path: str | Path) -> tuple[list[Frame], RadarParams, dict]:
    """Returns (frames, params, meta). Frames go through the same word→float
    decode as the live driver, so the DSP sees exactly what it would see live."""
    from .hardware.codec import raw_to_float   # local import: hardware pkg imports capture

    d = np.load(Path(path), allow_pickle=False)
    version = int(d["schema_version"]) if "schema_version" in d.files else 0
    if version not in (0, SCHEMA_VERSION):
        d.close()
        raise ValueError(f"Unsupported capture schema: {version}")
    if version == 0:                                   # pre-schema calibrate.py files
        return _load_legacy(d, path)
    params = _params_from_json(str(d["params_json"]))
    samples, dtype = d["samples"], str(d["dtype"])
    ts, seq = d["timestamps"], d["seq"]
    signed = str(d["signedness"]) or "auto"
    meta = {k: (d[k].item() if d[k].ndim == 0 else d[k]) for k in d.files
            if k not in ("samples", "timestamps", "seq", "params_json")}
    meta["extra"] = json.loads(str(d["extra_json"])) if "extra_json" in d.files else {}
    meta["path"] = str(path)
    frame_meta = json.loads(str(d["frame_meta_json"])) if "frame_meta_json" in d.files else None
    if frame_meta is not None and len(frame_meta) != samples.shape[0]:
        d.close()
        raise ValueError("Frame metadata count does not match sample array")
    frames = []
    for i in range(samples.shape[0]):
        raw = samples[i]
        if dtype == "uint16_offset_binary":
            x, sg = raw_to_float(raw, signed if signed != "auto" else "auto")
            fmeta = {"raw": raw, "signedness": sg}
        else:
            x, fmeta = raw, {"raw": raw}
        if frame_meta is not None:
            fmeta.update(frame_meta[i])
        fmeta.update(idn=str(d["device"]), replay=True, source_file=str(path))
        frames.append(Frame(samples=x, params=params, timestamp=float(ts[i]),
                            seq=int(seq[i]), meta=fmeta))
    d.close()
    return frames, params, meta


def _load_legacy(d, path) -> tuple[list[Frame], RadarParams, dict]:
    """calibrate.py files from 8–16 Oct 2026 (raw, fs, ramp_time_s, f_start_hz, ...)."""
    from .hardware.codec import raw_to_float
    from .params import RadarParams
    p = RadarParams(f_start_hz=float(d["f_start_hz"]), f_stop_hz=float(d["f_stop_hz"]),
                    waveform="fmcw", sweep=str(d["sweep"]) if "sweep" in d.files else "triangle",
                    ramp_time_s=float(d["ramp_time_s"]), fs_hz=float(d["fs"]),
                    n_samples=int(d["raw"].shape[1]))
    frames = []
    for i, raw in enumerate(d["raw"]):
        x, sg = raw_to_float(raw)
        frames.append(Frame(samples=x, params=p, seq=i,
                            meta={"raw": raw, "signedness": sg, "replay": True,
                                  "idn": str(d["idn"]) if "idn" in d.files else "",
                                  "source_file": str(path)}))
    return frames, p, {"schema_version": 0, "label": Path(path).stem.split("_")[0],
                       "path": str(path)}
