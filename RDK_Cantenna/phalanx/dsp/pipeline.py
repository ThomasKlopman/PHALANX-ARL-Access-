"""The receive chain, stage by stage, as one object the GUI calls once per frame.

    Frame ──► remove_dc ──► ramp alignment ──► reshape_cpi (N_p, N_f)
          ──► range_fft | pulse_compress ──► (N_p, N_r) complex64
          ──► [MTI] ──► doppler_fft ──► (N_d, N_r) ──► power
          ──► clutter map ──► CA-CFAR ──► range gate ──► detections

This is the host-side half of the CN0566 chain in the project notes
(project note rx-signal-chain-cn0566) with one Phase-1 extra — ramp alignment —
and one swap — dechirp range FFT instead of matched-filter pulse compression.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..frame import Frame
from ..params import RadarParams
from . import conditioning as cond
from .detect import CfarConfig, ClutterMap, Detection, ca_cfar, extract_detections
from .doppler import doppler_fft, mti_cancel, power, to_db
from .rangeproc import conjugate_rows, pulse_compress, range_fft


@dataclass
class ProcessorConfig:
    cfar: CfarConfig = field(default_factory=CfarConfig)
    mti_order: int = 1               # 0 = off
    clutter_alpha: float = 0.1
    subtract_clutter: bool = False   # only meaningful with mti_order = 0
    min_range_m: float = 2.0         # gate out the DC / TX-leakage bins
    max_range_m: float | None = None
    min_abs_velocity_mps: float = 0.0  # e.g. 0.2 to ignore the zero-Doppler ridge
    max_abs_velocity_frac: float = 0.9
    """Ignore Doppler rows with |v| > this fraction of v_max.

    Kit s/n 5035 radiates a family of IF spurs at exactly (n + 1/2) x PRF
    (3437.5 Hz and ±125 Hz sidebands). Being half-PRF offset they alias to the
    slow-time Nyquist bin, i.e. they show up as fake targets at ±v_max around
    18–23 m in every frame. Gating the edge rows removes the whole family
    whatever its exact frequency, at the cost of the (already ambiguous) edge
    bins. Set to 1.0 to disable."""
    ramp_offset: int | None = None   # None → estimate per frame (kit 5035: always 79)
    ramp_parity: int = 1             # 0/1, one-time calibration; kit s/n 5035 → 1 (16 Oct 2026)
    notch_hz: tuple[float, ...] = () # fixed IF spurs to remove, e.g. (3440.0,)
    notch_q: float = 30.0
    skip_pulses: int = 0             # drop the first N pulses (capture-start settling)
    reference_chirp: np.ndarray | None = None  # Phase 2 only


@dataclass
class Result:
    rd_power: np.ndarray             # (N_d, N_r) float32 linear
    rd_db: np.ndarray                # (N_d, N_r) float32
    range_axis: np.ndarray           # (N_r,) m
    velocity_axis: np.ndarray        # (N_d,) m/s
    detections: list[Detection]
    noise: np.ndarray                # CFAR noise estimate (N_d, N_r)
    mask: np.ndarray                 # CFAR hits after gating
    pulses: np.ndarray               # (N_p, N_f) aligned pulse matrix (for debugging)
    range_profiles: np.ndarray       # (N_p, N_r) complex64
    clutter: np.ndarray | None
    diag: dict[str, Any]

    def detections_json(self) -> list[dict]:
        return [d.as_dict() for d in self.detections]


class RangeDopplerProcessor:
    """FMCW (Phase 1) and PLFM (Phase 2) range-Doppler processor."""

    def __init__(self, params: RadarParams, cfg: ProcessorConfig | None = None):
        self.params = params
        self.cfg = cfg or ProcessorConfig()
        self.clutter = ClutterMap(self.cfg.clutter_alpha)

    # -- individual stages exposed for notebooks / unit tests -------------
    def align(self, x: np.ndarray) -> tuple[int, cond.RampAlignment | None]:
        if self.params.waveform == "plfm" or self.cfg.ramp_offset is not None:
            return (self.cfg.ramp_offset or 0), None
        al = cond.estimate_ramp_offset(x, self.params.n_fast)
        return al.offset, al

    def pulse_matrix(self, x: np.ndarray, offset: int) -> tuple[np.ndarray, np.ndarray]:
        p = self.params
        pulses = cond.reshape_cpi(x, p.n_fast, offset, p.segments_per_pri, self.cfg.ramp_parity)
        is_down = np.zeros(pulses.shape[0], dtype=bool)
        if p.waveform == "fmcw" and p.sweep == "triangle" and p.segment_select == "both":
            is_down = (np.arange(pulses.shape[0]) % 2) == 1
        return pulses, is_down

    def range_profiles(self, pulses: np.ndarray, is_down: np.ndarray) -> np.ndarray:
        p = self.params
        if p.waveform == "plfm":
            ref = self.cfg.reference_chirp
            if ref is None:
                ref = p.reference_chirp()
            rp = pulse_compress(pulses, ref, win=p.range_window)
        else:
            rp = range_fft(pulses, p.range_window, p.n_fft_range)
            if np.iscomplexobj(pulses):
                # A complex (I/Q) beat gives a two-sided spectrum. With the dechirp convention
                # used for the B210 (b210_fmcw.py) echoes have positive beat frequency, so the
                # first half is the range axis; the other half would be "negative range".
                # Unlike the kit's real beat, the sign of the Doppler is then known in EVERY
                # range cell, including cell 0.
                rp = rp[:, :rp.shape[1] // 2 + 1]
        if is_down.any():
            rp = conjugate_rows(rp, is_down)
        return rp

    def range_doppler(self, rp: np.ndarray) -> np.ndarray:
        if self.cfg.mti_order:
            rp = mti_cancel(rp, self.cfg.mti_order)
        return doppler_fft(rp, self.params.doppler_window, self.params.n_fft_doppler)

    # -- the whole chain -----------------------------------------------------
    def process(self, frame: Frame) -> Result:
        p = self.params
        x = cond.remove_dc(frame.samples)
        if self.cfg.notch_hz:
            x = cond.notch(x, p.fs_hz, self.cfg.notch_hz, self.cfg.notch_q)
        offset, al = self.align(x)
        pulses, is_down = self.pulse_matrix(x, offset)
        if self.cfg.skip_pulses:
            pulses, is_down = pulses[self.cfg.skip_pulses:], is_down[self.cfg.skip_pulses:]
        rp = self.range_profiles(pulses, is_down)
        rd = self.range_doppler(rp)
        pw = power(rd)

        range_axis = p.range_axis(rp.shape[1] if p.waveform == "plfm" else None)[:pw.shape[1]]
        velocity_axis = p.velocity_axis(pw.shape[0])
        # present rows in ascending velocity whatever doppler_sign is
        order = np.argsort(velocity_axis, kind="stable")
        pw, velocity_axis = pw[order], velocity_axis[order]
        zero_row = int(np.argmin(np.abs(velocity_axis)))

        clutter = self.clutter.update(pw, zero_row)
        if self.cfg.subtract_clutter and not self.cfg.mti_order:
            pw = pw.copy()
            pw[zero_row] = np.maximum(pw[zero_row] - clutter, 0)

        mask, noise, alpha = ca_cfar(pw, self.cfg.cfar)
        gate = range_axis >= self.cfg.min_range_m
        if self.cfg.max_range_m is not None:
            gate &= range_axis <= self.cfg.max_range_m
        mask &= gate[None, :]
        if self.cfg.min_abs_velocity_mps > 0:
            mask &= (np.abs(velocity_axis) >= self.cfg.min_abs_velocity_mps)[:, None]
        if self.cfg.max_abs_velocity_frac < 1.0 and len(velocity_axis) > 2:
            v_lim = self.cfg.max_abs_velocity_frac * p.max_velocity_mps
            mask &= (np.abs(velocity_axis) <= v_lim)[:, None]

        dets = extract_detections(pw, mask, noise, range_axis, velocity_axis)
        diag = {"ramp_offset": offset, "cfar_alpha": alpha, "n_pulses": pulses.shape[0],
                "signedness": frame.meta.get("signedness"), "seq": frame.seq}
        if al is not None:
            diag.update(align_confidence=al.confidence, align_score=al.score,
                        segment_means=al.segment_means)
        return Result(pw, to_db(pw), range_axis, velocity_axis, dets, noise, mask,
                      pulses, rp, clutter, diag)


class CWProcessor:
    """CW mode: one real capture → Doppler spectrum → unsigned speed detections.

    The CN0566 analogue is a zero-bandwidth pulse: no range stage, the whole
    frame is one 'pulse' and the FFT runs straight to Doppler. Because the
    Cantenna's IF is real there is no sign — the manual says so too.
    """

    def __init__(self, params: RadarParams, cfg: ProcessorConfig | None = None):
        self.params = params
        self.cfg = cfg or ProcessorConfig(cfar=CfarConfig(guard=(0, 4), train=(0, 32)))

    def process(self, frame: Frame) -> Result:
        p = self.params
        x = cond.remove_dc(frame.samples)[None, :]
        spec = range_fft(x, p.doppler_window, p.n_fft_doppler)       # (1, N/2+1)
        pw = power(spec)
        speed_axis = p.cw_velocity_axis(2 * (pw.shape[1] - 1))
        mask, noise, alpha = ca_cfar(pw, self.cfg.cfar)
        gate = speed_axis >= max(self.cfg.min_abs_velocity_mps, 0.05)  # drop DC bins
        mask &= gate[None, :]
        # reuse the 2-D extractor with speed on the "range" axis
        dets = extract_detections(pw, mask, noise, speed_axis, np.zeros(1))
        for d in dets:
            d.velocity_mps, d.range_m = d.range_m, float("nan")
            d.extra["unsigned"] = True
        return Result(pw, to_db(pw), speed_axis, np.zeros(1), dets, noise, mask, x, spec,
                      None, {"cfar_alpha": alpha, "seq": frame.seq, "mode": "cw"})
