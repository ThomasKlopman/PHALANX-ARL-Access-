"""Bounded background recording: one schema-v1 NPZ per acquired block.

The final session.json is marked complete only after the queue is drained.
Queue exhaustion and I/O failures are explicit errors; samples are never
silently dropped. A terminated process leaves an incomplete session.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from queue import Full, Queue
from threading import Thread

import numpy as np

from .capture import load_capture, save_capture
from .frame import Frame


def _utc():
    return datetime.now(timezone.utc).isoformat()


class SessionRecorder:
    def __init__(self, folder, label="", queue_blocks=16):
        self.folder = Path(folder)
        self.folder.parent.mkdir(parents=True, exist_ok=True)
        self.folder.mkdir(exist_ok=False)
        self.label = label
        self.queue = Queue(maxsize=queue_blocks)
        self.error = None
        self._writer_error = None
        self._acquired_frames = self._acquired_samples = None
        self.submitted_frames = 0
        self.saved_frames = self.saved_samples = 0
        self._closed = False
        self._started = _utc()
        self._manifest("incomplete", "recording in progress")
        self._thread = Thread(target=self._run, name="capture-writer", daemon=True)
        self._thread.start()

    def _manifest(self, state, reason):
        data = {"format": "phalanx-npz-session", "version": 1,
                "state": state, "reason": reason, "label": self.label,
                "started_utc": self._started, "updated_utc": _utc(),
                "submitted_frames": self.submitted_frames,
                "saved_frames": self.saved_frames, "saved_samples": self.saved_samples,
                "acquired_complete_blocks": self._acquired_frames,
                "acquired_samples_in_complete_blocks": self._acquired_samples,
                "block_pattern": "frame_*.npz"}
        temporary = self.folder / "session.json.partial"
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(data, file, indent=2, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, self.folder / "session.json")

    def append(self, frame):
        if self._closed:
            raise RuntimeError("Recording is closed")
        if self.error is not None:
            raise RuntimeError(f"Recording failed: {self.error}")
        # Own samples/metadata before the caller can reuse or modify a buffer.
        meta = deepcopy({k: v for k, v in frame.meta.items() if k != "raw"})
        samples = np.array(frame.samples, copy=True)
        raw = frame.meta.get("raw")
        if raw is not None:
            meta["raw"] = samples if raw is frame.samples else np.array(raw, copy=True)
        snapshot = Frame(samples, deepcopy(frame.params), frame.timestamp, frame.seq, meta)
        try:
            self.queue.put_nowait(snapshot)
        except Full as error:
            self.error = "Recording queue full; acquisition stopped instead of dropping a block"
            raise RuntimeError(self.error) from error
        self.submitted_frames += 1

    def _write_frame(self, frame):
        index = self.saved_frames
        final = self.folder / f"frame_{index:08d}.npz"
        temporary = self.folder / f".frame_{index:08d}.partial.npz"
        device = frame.meta.get("device", {})
        save_capture(temporary, [frame], frame.params, label=self.label,
                     gain_db=float(device.get("rx_gain_db", float("nan"))),
                     extra={"session_block": index})
        # Windows requires a writable handle for the disk flush.
        with temporary.open("r+b") as file:
            file.flush()
            os.fsync(file.fileno())
        # Session directory is exclusively created, so final cannot pre-exist.
        if final.exists():
            raise FileExistsError(final)
        temporary.rename(final)
        self.saved_frames += 1
        self.saved_samples += len(frame)

    def _run(self):
        while True:
            frame = self.queue.get()
            try:
                if frame is None:
                    return
                if self._writer_error is None:
                    try:
                        self._write_frame(frame)
                    except Exception as error:
                        self._writer_error = f"{type(error).__name__}: {error}"
                        self.error = self._writer_error
            finally:
                self.queue.task_done()

    def finish(self, valid=True, reason="", acquired_frames=None, acquired_samples=None):
        if self._closed:
            return self.folder / "session.json"
        self._closed = True
        self.queue.put(None)
        self._thread.join()
        self._acquired_frames = self.submitted_frames if acquired_frames is None else acquired_frames
        self._acquired_samples = self.saved_samples if acquired_samples is None else acquired_samples
        if self.error is not None:
            valid, reason = False, self.error
        if (self.saved_frames != self.submitted_frames or
                self.saved_frames != self._acquired_frames or
                self.saved_samples != self._acquired_samples):
            valid = False
            reason = reason or "Not all submitted blocks were saved"
        self._manifest("complete" if valid else "invalid", reason)
        if self.error is not None:
            raise RuntimeError(f"Recording invalid: {self.error}")
        return self.folder / "session.json"


def session_files(path):
    """Return committed blocks only; reject interrupted or invalid sessions."""
    path = Path(path)
    manifest_path = path / "session.json" if path.is_dir() else path
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if data.get("format") != "phalanx-npz-session" or data.get("version") != 1:
        raise ValueError("Unsupported recording session")
    if data.get("state") != "complete":
        raise ValueError(f"Session is {data.get('state')}: {data.get('reason', '')}")
    files = sorted(manifest_path.parent.glob("frame_*.npz"))
    if not files or len(files) != data.get("saved_frames"):
        raise ValueError("Session block count does not match manifest")
    return files, data


def verify_session(path):
    """Check a completed session: block/sample totals, CRCs (via load), parameter
    consistency, sequence numbers, and device-time continuity when timestamps exist.
    Returns a summary dict; raises ValueError on the first problem found.
    Not an independent reference-sample comparison or an RF calibration."""
    files, info = session_files(path)
    total = 0
    previous_time = previous_seq = params = None
    timed = 0
    for path in files:
        frames, block_params, _ = load_capture(path)
        if len(frames) != 1:
            raise ValueError(f"Expected one frame in {path.name}")
        if params is not None and params != block_params:
            raise ValueError(f"Parameters changed in {path.name}")
        params = block_params
        frame = frames[0]
        if len(frame) != params.n_samples or frame.samples.ndim != 1:
            raise ValueError(f"Sample count/shape mismatch in {path.name}")
        if not np.isfinite(frame.samples).all():
            raise ValueError(f"Nonfinite samples in {path.name}")
        if frame.meta.get("rx_errors"):
            raise ValueError(f"Reported RX errors in {path.name}")
        if previous_seq is not None and frame.seq != previous_seq + 1:
            raise ValueError(f"Frame sequence gap in {path.name}")
        stamp = frame.meta.get("device_time_s")
        if stamp is not None:
            timed += 1
            if previous_time is not None and abs((stamp-previous_time)*params.fs_hz) > 0.5:
                raise ValueError(f"Device-time gap in {path.name}")
            previous_time = stamp + len(frame)/params.fs_hz
        elif timed:
            raise ValueError(f"Missing device timestamp in {path.name}")
        previous_seq = frame.seq
        total += len(frame)
    if total != info["saved_samples"]:
        raise ValueError("Sample total does not match manifest")
    if info["acquired_complete_blocks"] != len(files):
        raise ValueError("Acquired and recorded block counts differ")
    if info["acquired_samples_in_complete_blocks"] != total:
        raise ValueError("Acquired and recorded sample counts differ")
    if params.waveform == "iq" and timed != len(files):
        raise ValueError("Raw I/Q session lacks device timestamps")
    return {"blocks": len(files), "samples": total, "sample_rate_hz": params.fs_hz,
            "sample_duration_s": total/params.fs_hz, "device_timed_blocks": timed}
