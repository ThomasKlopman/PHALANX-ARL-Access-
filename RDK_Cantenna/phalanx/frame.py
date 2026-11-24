"""The unit of data that crosses the hardware → DSP boundary.

Phase 1 (Cantenna): ``samples`` is float32, real, shape (N,), already scaled
to volts-ish units with the ADC offset removed.
Phase 2 (CN0566 + B210): ``samples`` is complex64, shape (N,) after the
Rx1/Rx2 calibrated sum (or (2, N) before it).

Keeping one Frame type for both phases is what lets the DSP be shared.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .params import RadarParams


@dataclass
class Frame:
    samples: np.ndarray
    params: RadarParams
    timestamp: float = field(default_factory=time.time)
    seq: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_complex(self) -> bool:
        return np.iscomplexobj(self.samples)

    @property
    def fs_hz(self) -> float:
        return self.params.fs_hz

    def __len__(self) -> int:
        return self.samples.shape[-1]
