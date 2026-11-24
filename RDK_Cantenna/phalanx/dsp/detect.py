"""Stage 4 — detection: power map → clutter map → CA-CFAR → list of detections.

Shared by both phases. Everything here works on *linear* power (float32);
dB is only for display.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage


# ---------------------------------------------------------------------------
# Clutter map
# ---------------------------------------------------------------------------

class ClutterMap:
    """Exponential moving average of the zero-Doppler row (static returns).

    Used two ways: (1) as a display baseline, (2) subtracted from the
    zero-Doppler row before CFAR when MTI is off, so that the range cells
    holding walls/ground don't raise the CFAR threshold for their neighbours.
    alpha = weight of the newest frame (0.1 ≈ 10-frame memory).
    """

    def __init__(self, alpha: float = 0.1):
        self.alpha = alpha
        self.map: np.ndarray | None = None

    def update(self, rd_power: np.ndarray, zero_row_index: int | None = None) -> np.ndarray:
        if zero_row_index is None:
            zero_row_index = rd_power.shape[0] // 2
        zero_row = rd_power[zero_row_index]
        if self.map is None or self.map.shape != zero_row.shape:
            self.map = zero_row.astype(np.float32).copy()
        else:
            self.map = (1 - self.alpha) * self.map + self.alpha * zero_row
        return self.map

    def reset(self) -> None:
        self.map = None


# ---------------------------------------------------------------------------
# CA-CFAR
# ---------------------------------------------------------------------------

@dataclass
class CfarConfig:
    guard: tuple[int, int] = (1, 2)     # (doppler, range) guard half-widths
    train: tuple[int, int] = (3, 8)     # (doppler, range) training half-widths
    pfa: float = 1e-4
    """Design Pfa. For CA-CFAR on square-law (exponential) noise with N training
    cells the threshold multiplier is  alpha = N * (Pfa^(-1/N) − 1)."""
    range_mode: str = "ca"
    """How the noise is estimated along range.

    "ca"   : cell averaging over both sides (Part 1 default, unchanged).
    "soca" : smallest-of. The nearer and the farther training windows are averaged
             separately and the SMALLER is used; a window that runs off the map is ignored
             instead of being filled by repeating the edge cell. Use it when targets sit in
             the first few range cells next to a strong leakage cell (B210 with coarse range
             cells): with "ca" the leakage cell is counted many times in the threshold of
             its neighbours and hides them. Cost: somewhat more false alarms than the design
             Pfa at clutter edges."""


def cfar_alpha(n_train: int, pfa: float) -> float:
    return n_train * (pfa ** (-1.0 / n_train) - 1.0)


def ca_cfar(p: np.ndarray, cfg: CfarConfig = CfarConfig()) -> tuple[np.ndarray, np.ndarray, float]:
    """2-D cell-averaging CFAR.

    p      : (N_d, N_r) linear power (1-D input is treated as (1, N))
    returns: (mask bool, noise estimate float32 same shape, alpha)

    Implementation: the sum over the full (train+guard) box minus the sum over
    the guard box, via two uniform filters — O(1) per cell regardless of
    window size. Edges use 'nearest' replication (so edge cells get a slightly
    optimistic noise estimate; the range gate in the pipeline hides most of it).
    """
    p = np.asarray(p, dtype=np.float32)
    squeeze = p.ndim == 1
    if squeeze:
        p = p[None, :]
    gd, gr = cfg.guard
    td, tr = cfg.train
    # 1-D maps (single pulse / CW) must not average along the empty axis
    if p.shape[0] == 1:
        gd, td = 0, 0
    outer = (2 * (gd + td) + 1, 2 * (gr + tr) + 1)
    inner = (2 * gd + 1, 2 * gr + 1)
    n_outer = outer[0] * outer[1]
    n_inner = inner[0] * inner[1]
    n_train = n_outer - n_inner
    sum_outer = ndimage.uniform_filter(p, size=outer, mode="nearest") * n_outer
    sum_inner = ndimage.uniform_filter(p, size=inner, mode="nearest") * n_inner
    noise = ((sum_outer - sum_inner) / n_train).astype(np.float32)
    alpha = cfar_alpha(n_train, cfg.pfa)
    if cfg.range_mode == "soca" and tr > 0:
        rows = 2 * (gd + td) + 1
        col = ndimage.uniform_filter1d(p.astype(np.float64), size=rows, axis=0, mode="nearest") * rows
        c = np.concatenate([np.zeros((p.shape[0], 1)), np.cumsum(col, axis=1)], axis=1)
        n = p.shape[1]
        r = np.arange(n)
        near_a, near_b = r - gr - tr, r - gr - 1            # inclusive cell indices
        far_a, far_b = r + gr + 1, r + gr + tr
        clip = lambda k: np.clip(k, 0, n - 1)               # noqa: E731
        near = np.where(near_a >= 0, c[:, clip(near_b) + 1] - c[:, clip(near_a)], np.inf)
        far = np.where(far_b <= n - 1, c[:, clip(far_b) + 1] - c[:, clip(far_a)], np.inf)
        side = np.minimum(near, far) / (rows * tr)
        usable = np.isfinite(side)                          # maps too small for either window: keep CA
        noise = np.where(usable, side, noise).astype(np.float32)
        alpha = cfar_alpha(rows * tr, cfg.pfa)
    elif cfg.range_mode not in ("ca", "soca"):
        raise ValueError(f"unknown CFAR range_mode {cfg.range_mode!r}")
    mask = p > alpha * noise
    if squeeze:
        return mask[0], noise[0], alpha
    return mask, noise, alpha


# ---------------------------------------------------------------------------
# Detections
# ---------------------------------------------------------------------------

@dataclass
class Detection:
    range_m: float
    velocity_mps: float
    snr_db: float
    power_db: float
    range_bin: int
    doppler_bin: int
    n_cells: int = 1            # size of the CFAR cluster it was the peak of
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {"range_m": round(self.range_m, 2), "velocity_mps": round(self.velocity_mps, 2),
             "snr_db": round(self.snr_db, 1), "power_db": round(self.power_db, 1),
             "range_bin": self.range_bin, "doppler_bin": self.doppler_bin,
             "n_cells": self.n_cells}
        d.update(self.extra)
        return d


def _parabolic_peak(y_m1: float, y_0: float, y_p1: float) -> float:
    """Sub-bin peak offset from three log-power samples (−0.5 … +0.5)."""
    den = y_m1 - 2 * y_0 + y_p1
    if den >= 0:
        return 0.0
    return float(np.clip(0.5 * (y_m1 - y_p1) / den, -0.5, 0.5))


def extract_detections(p: np.ndarray, mask: np.ndarray, noise: np.ndarray,
                       range_axis: np.ndarray, velocity_axis: np.ndarray,
                       interpolate: bool = True) -> list[Detection]:
    """Group neighbouring CFAR hits and report one detection per group (its peak)."""
    p = np.atleast_2d(p)
    mask = np.atleast_2d(mask)
    noise = np.atleast_2d(noise)
    labels, n = ndimage.label(mask, structure=np.ones((3, 3)))
    dets: list[Detection] = []
    if n == 0:
        return dets
    p_db = 10 * np.log10(np.maximum(p, 1e-20))
    for lab in range(1, n + 1):
        cells = np.argwhere(labels == lab)
        k = cells[np.argmax(p[cells[:, 0], cells[:, 1]])]
        d, r = int(k[0]), int(k[1])
        # sub-bin refinement in range (and Doppler when there is more than one row)
        r_frac = d_frac = 0.0
        if interpolate and 0 < r < p.shape[1] - 1:
            r_frac = _parabolic_peak(p_db[d, r - 1], p_db[d, r], p_db[d, r + 1])
        if interpolate and 0 < d < p.shape[0] - 1:
            d_frac = _parabolic_peak(p_db[d - 1, r], p_db[d, r], p_db[d + 1, r])
        rng = np.interp(r + r_frac, np.arange(len(range_axis)), range_axis)
        vel = (np.interp(d + d_frac, np.arange(len(velocity_axis)), velocity_axis)
               if len(velocity_axis) > 1 else float(velocity_axis[0]))
        snr = 10 * np.log10(p[d, r] / max(noise[d, r], 1e-20))
        dets.append(Detection(float(rng), float(vel), float(snr), float(p_db[d, r]),
                              r, d, int(len(cells))))
    dets.sort(key=lambda x: -x.snr_db)
    return dets
