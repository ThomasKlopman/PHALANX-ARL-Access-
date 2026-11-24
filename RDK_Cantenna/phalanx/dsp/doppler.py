"""Stage 3 — slow-time processing: range profiles (N_p, N_r) → range-Doppler map (N_d, N_r).

Identical for Phase 1 and Phase 2: by now both hardware paths have produced a
complex range-profile per pulse.

* MTI canceller: y[m] = x[m] − x[m−1] along slow time. A notch at zero
  Doppler that removes stationary clutter *before* the Doppler FFT so its
  sidelobes don't mask slow movers. Costs one pulse per order and is a
  3-dB loss near ±v_max (the notch's mirror). Keep it optional.
* Doppler FFT: window → FFT along slow time → fftshift so bin N_d/2 is zero
  velocity and the axis runs −v_max … +v_max.
"""
from __future__ import annotations

import numpy as np
from scipy import fft as sfft

from .conditioning import window


def mti_cancel(rp: np.ndarray, order: int = 1) -> np.ndarray:
    """order-1: 2-pulse canceller (N_p−1 rows); order-2: 3-pulse (N_p−2 rows)."""
    y = np.asarray(rp, dtype=np.complex64)
    for _ in range(order):
        y = (y[1:] - y[:-1]).astype(np.complex64)
    return y


def doppler_fft(rp: np.ndarray, win: str | None = "hann", n_fft: int | None = None) -> np.ndarray:
    """(N_p, N_r) complex → (N_d, N_r) complex64, fftshifted along slow time."""
    rp = np.asarray(rp, dtype=np.complex64)
    n_p = rp.shape[0]
    n_fft = n_fft or n_p
    w = window(n_p, win)[:, None]
    rd = sfft.fft(rp * w, n=n_fft, axis=0)
    return sfft.fftshift(rd, axes=0).astype(np.complex64)


def power(x: np.ndarray) -> np.ndarray:
    """|x|² as float32 (CFAR works on linear power; dB is for display only)."""
    x = np.asarray(x)
    return (x.real ** 2 + x.imag ** 2).astype(np.float32) if np.iscomplexobj(x) \
        else (x.astype(np.float32) ** 2)


def to_db(p: np.ndarray, floor: float = 1e-20) -> np.ndarray:
    return (10 * np.log10(np.maximum(p, floor))).astype(np.float32)
