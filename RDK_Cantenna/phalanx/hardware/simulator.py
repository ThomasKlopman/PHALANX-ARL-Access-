"""Synthetic Cantenna: lets the DSP and GUI be developed with the kit unplugged.

Models exactly what the real kit hands us — a *real* 16-bit beat signal at
20 kS/s whose capture starts at an unknown point of the free-running
triangle sweep — so every step of the pipeline (signedness detection, ramp
alignment, range/Doppler, CFAR) is exercised the same way as with hardware.

Beat-signal physics (one target, one ramp segment with start frequency f0 and
slope s = ±B/T, local time t_l, round-trip delay tau = 2R/c):

    tx phase  θ(t)   = 2π [ f0 t_l + s t_l² / 2 ]
    beat      φ(t)   = θ(t) − θ(t − τ) = 2π [ f0 τ + s t_l τ − s τ² / 2 ]
    IF        x(t)   = A cos φ(t)

With R(t) = R0 − v t (v > 0 closing) the τ term carries the Doppler.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from ..frame import Frame
from ..params import C0, RadarParams, cantenna_fmcw
from .base import RadarSource
from .codec import raw_to_float


@dataclass
class Target:
    range_m: float
    velocity_mps: float = 0.0       # +ve = closing
    snr_db: float = 20.0            # per-sample SNR of the beat tone vs ADC noise


@dataclass
class SimConfig:
    targets: list[Target] = field(default_factory=lambda: [Target(30.0, 1.5, 20.0)])
    noise_rms: float = 0.01         # ADC noise, full scale = 1.0
    clutter: list[Target] = field(default_factory=lambda: [Target(4.0, 0.0, 30.0)])
    glitch_amp: float = 0.3         # PLL/VCO transient at every segment start
    glitch_tau_s: float = 0.25e-3
    glitch_f_hz: float = 3000.0
    random_start: bool = True       # unknown capture-to-sweep alignment
    signed_words: bool = False      # emulate two's-complement ADC words
    seed: int | None = None
    # Seconds between frames. 0 = as fast as the CPU allows (tests, scoring).
    # The GUI uses KIT_FRAME_PERIOD_S so the simulator behaves like the real kit
    # and a "Record" run does not overflow the recorder queue.
    frame_period_s: float = 0.0


KIT_FRAME_PERIOD_S = 0.8            # measured on s/n 5035: 4096 samples per 801 ms


def _pace(obj, period_s: float) -> None:
    """Sleep so consecutive capture() calls are at least period_s apart."""
    if period_s <= 0:
        return
    now = time.monotonic()
    nxt = getattr(obj, "_next_emit", None)
    if nxt is not None and nxt > now:
        time.sleep(nxt - now)
        now = nxt
    obj._next_emit = now + period_s


class SimulatedRadar(RadarSource):
    def __init__(self, params: RadarParams | None = None, cfg: SimConfig | None = None):
        super().__init__(params or cantenna_fmcw())
        self.cfg = cfg or SimConfig()
        self.rng = np.random.default_rng(self.cfg.seed)
        self._t0 = 0.0
        self._running = False
        self.last_offset: int | None = None  # ground-truth ramp offset (samples)

    def configure(self, params: RadarParams | None = None) -> None:
        if params is not None:
            self.params = params

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    # --- waveform -----------------------------------------------------------
    def _segment_params(self, t: np.ndarray):
        """Per-sample (f0, slope, t_local) of the free-running sweep at times t."""
        p = self.params
        T = p.ramp_time_s
        if p.waveform == "cw":
            return (np.full_like(t, p.f_start_hz), np.zeros_like(t), t)
        slope = p.bandwidth_hz / T
        if p.sweep == "triangle":
            seg = np.floor(t / T).astype(int)
            t_l = t - seg * T
            up = (seg % 2) == 0
            f0 = np.where(up, p.f_start_hz, p.f_stop_hz)
            s = np.where(up, slope, -slope)
        else:                                            # sawtooth ramp
            t_l = np.mod(t, T)
            f0 = np.full_like(t, p.f_start_hz)
            s = np.full_like(t, slope)
        return f0, s, t_l

    def synthesize(self, n: int | None = None, t_start: float | None = None) -> np.ndarray:
        """Continuous-time model sampled at fs → float samples (not yet quantised)."""
        p, c = self.params, self.cfg
        n = n or p.n_samples
        if t_start is None:
            period = 2 * p.ramp_time_s if p.sweep == "triangle" else p.ramp_time_s
            t_start = self.rng.uniform(0, period) if c.random_start and p.waveform != "cw" else 0.0
        t = t_start + np.arange(n) / p.fs_hz
        f0, s, t_l = self._segment_params(t)
        x = np.zeros(n)
        for tg in list(c.targets) + list(c.clutter):
            R = tg.range_m - tg.velocity_mps * (t - t_start)
            tau = 2 * R / C0
            phi = 2 * np.pi * (f0 * tau + s * t_l * tau - 0.5 * s * tau ** 2)
            A = c.noise_rms * 10 ** (tg.snr_db / 20) * np.sqrt(2)
            x += A * np.cos(phi)
        if p.waveform != "cw" and c.glitch_amp > 0:
            x += c.glitch_amp * np.exp(-t_l / c.glitch_tau_s) * np.cos(2 * np.pi * c.glitch_f_hz * t_l)
        x += self.rng.normal(0, c.noise_rms, n)
        # ground truth: first sample index at which an UP segment starts
        seg_start = np.flatnonzero(np.diff(t_l) < 0) + 1
        if p.sweep == "triangle":
            seg_start = seg_start[s[seg_start] > 0]
        self.last_offset = int(seg_start[0]) if seg_start.size else 0
        self.last_t_start = t_start
        return x

    def capture(self) -> Frame:
        _pace(self, self.cfg.frame_period_s)
        x = np.clip(self.synthesize(), -1, 1)
        words = np.round(x * 32767).astype(np.int32)
        raw = (words.astype(np.int16).view(np.uint16) if self.cfg.signed_words
               else (words + 32768).astype(np.uint16))
        samples, signedness = raw_to_float(raw, "auto")   # same path as hardware
        return Frame(samples=samples, params=self.params, seq=self._next_seq(),
                     meta={"raw": raw, "signedness": signedness,
                           "true_offset": self.last_offset, "sim": True})


# ---------------------------------------------------------------------------
# Phase-2 / B210 path: pulsed LFM at complex baseband
# ---------------------------------------------------------------------------

@dataclass
class PLFMSimConfig:
    targets: list[Target] = field(default_factory=lambda: [Target(30.0, 1.5, 20.0)])
    noise_rms: float = 0.01
    leakage_db: float = 40.0        # direct TX→RX path, dB above noise_rms (static, ~0.5 m)
    leakage_range_m: float = 0.5
    seed: int | None = None
    frame_period_s: float = 0.0     # 0 = unpaced; see SimConfig.frame_period_s


class SimulatedPLFM(RadarSource):
    """What an SDR (USRP B210 + cans, later CN0566) hands the host.

    Complex64 baseband, one CPI per frame = n_pulses PRIs of n_fast samples,
    starting exactly at the first pulse (the host issued the timed stream
    command, so there is nothing to align). For each echo:

        r(t) = A · s(t − τ(t)) · exp(−j 2π f_c τ(t)),   τ(t) = 2 R(t) / c

    where s is `params.reference_chirp()` (the array also sent to the TX),
    and R(t) = R0 − v t carries the Doppler as a slow-time phase ramp.
    The matched filter then puts the echo at delay τ with the pulse-compression
    gain of TBP = B·τ_p. Target snr_db is defined AFTER compression, per pulse.
    """

    def __init__(self, params: RadarParams, cfg: PLFMSimConfig | None = None):
        assert params.waveform == "plfm"
        super().__init__(params)
        self.cfg = cfg or PLFMSimConfig()
        self.rng = np.random.default_rng(self.cfg.seed)
        self._t_cpi = 0.0
        self._running = False

    def configure(self, params: RadarParams | None = None) -> None:
        if params is not None:
            self.params = params

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def _echo(self, R0: float, v: float, amp: float, t_abs: np.ndarray, t_local: np.ndarray) -> np.ndarray:
        p = self.params
        tau = 2.0 * (R0 - v * t_abs) / C0
        u = t_local - tau                                   # time into the pulse
        k = p.bandwidth_hz / p.pulse_width_s
        inside = (u >= 0) & (u < p.pulse_width_s)
        s = np.exp(1j * np.pi * k * (u - p.pulse_width_s / 2) ** 2) * inside
        return amp * s * np.exp(-2j * np.pi * p.fc_hz * tau)

    def synthesize(self) -> np.ndarray:
        p, c = self.params, self.cfg
        n_p, n_f = p.n_pulses, p.n_fast
        n = np.arange(n_f)[None, :]
        m = np.arange(n_p)[:, None]
        t_local = n / p.fs_hz                                 # (1, n_f)
        t_abs = self._t_cpi + m * p.pri_s + t_local           # (n_p, n_f)
        x = np.zeros((n_p, n_f), dtype=np.complex128)
        tbp = p.bandwidth_hz * p.pulse_width_s
        for tg in c.targets:
            # per-pulse post-compression SNR = (A^2 * TBP) / sigma^2  → A
            amp = c.noise_rms * 10 ** (tg.snr_db / 20) / np.sqrt(tbp)
            x += self._echo(tg.range_m, tg.velocity_mps, amp, t_abs, t_local)
        if c.leakage_db > -np.inf:
            amp = c.noise_rms * 10 ** (c.leakage_db / 20)
            x += self._echo(c.leakage_range_m, 0.0, amp, t_abs, t_local)
        x += c.noise_rms * (self.rng.normal(size=x.shape) + 1j * self.rng.normal(size=x.shape)) / np.sqrt(2)
        self._t_cpi += n_p * p.pri_s
        return x.reshape(-1).astype(np.complex64)

    def capture(self) -> Frame:
        _pace(self, self.cfg.frame_period_s)
        x = self.synthesize()
        return Frame(samples=x, params=self.params, seq=self._next_seq(),
                     meta={"raw": x, "sim": True, "signedness": "complex64"})
