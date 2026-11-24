"""Abstract radar front-end.

Every hardware phase implements this small interface so the DSP and the GUI
never import a device-specific module:

    with SomeRadar(params) as radar:
        radar.start()
        frame = radar.capture()      # blocking, returns one CPI worth of samples
        ...
        radar.stop()

Phase 1: CantennaRDK  (pyvisa / SCPI over USBTMC)
Phase 2: PhaserB210   (pyadi-iio for the CN0566 + UHD for the B210) — later
Any    : SimulatedRadar (synthetic targets, no hardware)
"""
from __future__ import annotations

import abc
from typing import Iterator

from ..frame import Frame
from ..params import RadarParams


class RadarSource(abc.ABC):
    def __init__(self, params: RadarParams):
        self.params = params
        self._seq = 0

    # --- lifecycle --------------------------------------------------------
    def open(self) -> "RadarSource":
        return self

    def close(self) -> None:
        pass

    def __enter__(self) -> "RadarSource":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # --- control ----------------------------------------------------------
    @abc.abstractmethod
    def configure(self, params: RadarParams | None = None) -> None:
        """Push waveform parameters to the hardware."""

    @abc.abstractmethod
    def start(self) -> None:
        """RF on, free-running sweep."""

    @abc.abstractmethod
    def stop(self) -> None:
        """RF off."""

    @abc.abstractmethod
    def capture(self) -> Frame:
        """Block until one frame (params.n_samples) is available and return it."""

    # --- convenience ------------------------------------------------------
    def frames(self, n: int | None = None) -> Iterator[Frame]:
        """Yield frames forever (or n of them)."""
        i = 0
        while n is None or i < n:
            yield self.capture()
            i += 1

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq
