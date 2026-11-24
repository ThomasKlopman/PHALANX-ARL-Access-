"""Replay a saved capture through the same RadarSource interface as live hardware.

    with RecordedRadar("captures/walker_toward_01.npz") as src:
        src.start()
        for frame in src.frames():          # same Frame objects the kit would give
            result = proc.process(frame)

Frames come back with the samples the hardware delivered (bit-exact, see
tests/test_capture.py), the recorded RadarParams, and meta['replay']=True so
the GUI can label the source. `realtime=True` paces playback at the original
frame interval; `loop=True` wraps around instead of stopping.
"""
from __future__ import annotations

import time
from pathlib import Path

from ..capture import load_capture
from ..frame import Frame
from ..params import RadarParams
from .base import RadarSource


class RecordedRadar(RadarSource):
    def __init__(self, path: str | Path, loop: bool = False, realtime: bool = False):
        self.path = Path(path)
        self._session_files = None
        self._last_timestamp = None
        if self.path.is_dir() or self.path.suffix == ".json":
            from ..session import session_files
            self._session_files, self.meta = session_files(self.path)
            first, params, _ = load_capture(self._session_files[0])
            if len(first) != 1:
                raise ValueError("Session must contain one frame per block")
            self._frames = None
        else:
            self._frames, params, self.meta = load_capture(self.path)
        super().__init__(params)
        self.loop = loop
        self.realtime = realtime
        self._i = 0
        self._last_emit = 0.0
        self._running = False

    # --- RadarSource ------------------------------------------------------
    def configure(self, params: RadarParams | None = None) -> None:
        # A recording's parameters are fixed; a different request is an error,
        # not something to silently ignore (UI-01: "actionable error").
        if params is not None and params != self.params:
            raise ValueError("Cannot reconfigure a recording; its parameters are fixed "
                             f"(file: {self.path.name})")

    def start(self) -> None:
        self._running = True
        self._i = 0
        self._last_emit = 0.0
        self._last_timestamp = None

    def stop(self) -> None:
        self._running = False

    def capture(self) -> Frame:
        if len(self) == 0:
            raise RuntimeError(f"{self.path.name} holds no frames")
        if self._i >= len(self):
            if not self.loop:
                raise StopIteration("end of recording")
            self._i = 0
        if self._session_files is None:
            f = self._frames[self._i]
        else:
            block, params, _ = load_capture(self._session_files[self._i])
            if len(block) != 1 or params != self.params:
                raise ValueError("Session block shape/parameters changed")
            f = block[0]
        self._i += 1
        if self.realtime:
            interval = self.params.n_samples / self.params.fs_hz
            if self._last_timestamp is not None:
                dt = f.timestamp - self._last_timestamp
                if 0 < dt < 10:
                    interval = dt
            wait = self._last_emit + interval - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last_emit = time.time()
        self._last_timestamp = f.timestamp
        out = Frame(samples=f.samples, params=f.params, timestamp=f.timestamp,
                    seq=f.seq, meta=dict(f.meta))
        return out

    def frames(self, n: int | None = None):
        i = 0
        while n is None or i < n:
            try:
                yield self.capture()
            except StopIteration:
                return
            i += 1

    def __len__(self) -> int:
        return len(self._session_files) if self._session_files is not None else len(self._frames)

    @property
    def label(self) -> str:
        return str(self.meta.get("label", ""))
