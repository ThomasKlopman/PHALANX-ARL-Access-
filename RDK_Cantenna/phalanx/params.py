"""Radar waveform / timing parameters shared by every PHALANX hardware phase.

One dataclass describes both the Phase-1 Cantenna (FMCW, real IF, 20 kS/s) and
the Phase-2 CN0566 + USRP (PLFM, complex I/Q, tens of MS/s). The DSP only ever
reads the *derived* quantities (range axis, velocity axis, samples per pulse),
so swapping hardware is a matter of constructing a different RadarParams.

Conventions
-----------
* ``fast time``  = samples inside one ramp / pulse            → range axis
* ``slow time``  = successive ramps / pulses inside one CPI    → velocity axis
* positive velocity = target approaching the radar (closing)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np

C0 = 299_792_458.0  # m/s

Waveform = Literal["fmcw", "cw", "plfm", "iq"]
Sweep = Literal["ramp", "triangle", "cw"]


@dataclass
class RadarParams:
    # --- RF ---------------------------------------------------------------
    f_start_hz: float = 2.400e9      # Cantenna default SWEEP:FREQSTAR
    f_stop_hz: float = 2.500e9       # Cantenna default SWEEP:FREQSTOP
    # --- timing -----------------------------------------------------------
    waveform: Waveform = "fmcw"
    sweep: Sweep = "triangle"        # Cantenna AUTO mode = free-running triangle
    ramp_time_s: float = 4e-3        # SWEEP:RAMPTIME (1..65536 ms). 4 ms is a
                                     # good range/velocity compromise, see below.
    fs_hz: float = 20_000.0          # Cantenna ADC: 16-bit, fixed 20 kS/s
    n_samples: int = 4096            # CAPT:FRAM max over USB (65536 over BT)
    # --- Phase-2 only (PLFM) ----------------------------------------------
    pulse_width_s: float | None = None
    prf_hz: float | None = None
    # --- processing choices -----------------------------------------------
    segment_select: Literal["up", "both"] = "up"
    """Which triangle segments become slow-time 'pulses'.

    "up"   : up-ramps only. PRI = 2*T. Textbook, unambiguous.
    "both" : up AND down ramps, with the down-ramp range bins conjugated so
             their slow-time phase rotates the same way. PRI = T → doubles the
             unambiguous velocity, but the range bin of a moving target shifts
             by ±f_d between up and down ramps (small when f_d << 1/T).
    """
    doppler_sign: int = -1
    """Maps slow-time FFT frequency to velocity sign.

    The dechirped beat phase is +2*pi*fc*tau = +4*pi*R/lambda, so a *closing*
    target (R decreasing) has a *negative* slow-time frequency. doppler_sign=-1
    therefore makes closing targets positive. If the real hardware mixes with
    the opposite sideband you will see the sign flipped — set +1.
    """
    range_window: str = "hann"
    doppler_window: str = "hann"
    n_fft_range: int | None = None   # None → samples per segment (no zero-pad)
    n_fft_doppler: int | None = None # None → number of pulses

    # ---------------------------------------------------------------------
    # Derived quantities
    # ---------------------------------------------------------------------
    @property
    def fc_hz(self) -> float:
        if self.waveform in ("cw", "iq"):
            return self.f_start_hz
        return 0.5 * (self.f_start_hz + self.f_stop_hz)

    @property
    def bandwidth_hz(self) -> float:
        return 0.0 if self.waveform == "cw" else abs(self.f_stop_hz - self.f_start_hz)

    @property
    def wavelength_m(self) -> float:
        return C0 / self.fc_hz

    @property
    def n_fast(self) -> int:
        """Samples per ramp (FMCW) or per PRI (PLFM) or whole frame (CW)."""
        if self.waveform in ("cw", "iq"):
            return self.n_samples
        if self.waveform == "plfm":
            assert self.prf_hz, "plfm needs prf_hz"
            return int(round(self.fs_hz / self.prf_hz))
        return int(round(self.fs_hz * self.ramp_time_s))

    @property
    def segments_per_pri(self) -> int:
        if self.waveform == "fmcw" and self.sweep == "triangle" and self.segment_select == "up":
            return 2
        return 1

    @property
    def pri_s(self) -> float:
        if self.waveform == "plfm":
            return 1.0 / self.prf_hz
        if self.waveform in ("cw", "iq"):
            return self.n_samples / self.fs_hz
        return self.ramp_time_s * self.segments_per_pri

    @property
    def n_pulses(self) -> int:
        """Complete pulses available in one frame of n_samples."""
        if self.waveform in ("cw", "iq"):
            return 1
        return self.n_samples // (self.n_fast * self.segments_per_pri)

    @property
    def range_resolution_m(self) -> float:
        return np.inf if self.bandwidth_hz == 0 else C0 / (2 * self.bandwidth_hz)

    @property
    def max_range_m(self) -> float:
        """Unambiguous range.

        FMCW with a *real* IF: beat frequencies up to fs/2 →
            R_max = (fs/2) * c * T / (2 B)
        PLFM: one PRI of two-way travel → c / (2 PRF).
        """
        if self.waveform == "fmcw":
            return 0.5 * self.fs_hz * C0 * self.ramp_time_s / (2 * self.bandwidth_hz)
        if self.waveform == "plfm":
            return C0 / (2 * self.prf_hz)
        return np.inf

    @property
    def max_velocity_mps(self) -> float:
        """Unambiguous |velocity| = lambda / (4 PRI)."""
        return self.wavelength_m / (4 * self.pri_s)

    @property
    def velocity_resolution_mps(self) -> float:
        if self.waveform in ("cw", "iq"):
            return self.wavelength_m / 2 * self.fs_hz / self.n_samples
        return self.wavelength_m / (2 * self.n_pulses * self.pri_s)

    # --- axes -------------------------------------------------------------
    def range_axis(self, n_fft: int | None = None) -> np.ndarray:
        """Range of each positive-frequency range bin (FMCW) in metres."""
        n_fft = n_fft or self.n_fft_range or self.n_fast
        if self.waveform == "fmcw":
            f_beat = np.fft.rfftfreq(n_fft, d=1 / self.fs_hz)
            return f_beat * C0 * self.ramp_time_s / (2 * self.bandwidth_hz)
        if self.waveform == "plfm":
            return np.arange(n_fft) * C0 / (2 * self.fs_hz)
        return np.zeros(1)

    def velocity_axis(self, n_fft: int | None = None) -> np.ndarray:
        """Velocity of each (fftshifted) Doppler bin in m/s, closing = positive."""
        n_fft = n_fft or self.n_fft_doppler or self.n_pulses
        f_d = np.fft.fftshift(np.fft.fftfreq(n_fft, d=self.pri_s))
        return self.doppler_sign * f_d * self.wavelength_m / 2

    def cw_velocity_axis(self, n_fft: int | None = None) -> np.ndarray:
        """CW mode: one real capture → unsigned speed per rfft bin."""
        n_fft = n_fft or self.n_samples
        f_d = np.fft.rfftfreq(n_fft, d=1 / self.fs_hz)
        return f_d * self.wavelength_m / 2

    def reference_chirp(self) -> np.ndarray:
        """PLFM transmit pulse s[n] (complex64): linear up-chirp −B/2 … +B/2 over
        pulse_width_s, unit amplitude. The same array is sent to the SDR and used
        as the matched-filter reference, so the two can never disagree."""
        assert self.waveform == "plfm" and self.pulse_width_s, "plfm needs pulse_width_s"
        n_s = int(round(self.pulse_width_s * self.fs_hz))
        t = np.arange(n_s) / self.fs_hz
        k = self.bandwidth_hz / self.pulse_width_s
        return np.exp(1j * np.pi * k * (t - self.pulse_width_s / 2) ** 2).astype(np.complex64)

    @property
    def duty_cycle(self) -> float:
        return (self.pulse_width_s * self.prf_hz) if self.waveform == "plfm" else 1.0

    def summary(self) -> str:
        if self.waveform == "iq":
            return (f"Raw I/Q RX | fc={self.fc_hz/1e9:.6f} GHz\n"
                    f"fs={self.fs_hz/1e6:g} MSPS | {self.n_samples:,} samples/block "
                    f"({1000*self.n_samples/self.fs_hz:g} ms)\n"
                    "Range and velocity are not measured")
        lines = [
            f"waveform={self.waveform} sweep={self.sweep}  fc={self.fc_hz/1e9:.4f} GHz  "
            f"B={self.bandwidth_hz/1e6:.1f} MHz  lambda={self.wavelength_m*100:.2f} cm",
            f"fs={self.fs_hz/1e3:.1f} kS/s  frame={self.n_samples} samples "
            f"({self.n_samples/self.fs_hz*1e3:.1f} ms)",
        ]
        if self.waveform == "fmcw":
            lines += [
                f"ramp T={self.ramp_time_s*1e3:.2f} ms → {self.n_fast} samples/ramp, "
                f"segments/PRI={self.segments_per_pri}, PRI={self.pri_s*1e3:.2f} ms, "
                f"{self.n_pulses} pulses/CPI",
                f"range: res={self.range_resolution_m:.2f} m  max={self.max_range_m:.1f} m",
                f"velocity: res={self.velocity_resolution_mps:.2f} m/s  "
                f"unambiguous=±{self.max_velocity_mps:.2f} m/s",
            ]
        elif self.waveform == "cw":
            lines += [f"velocity res={self.velocity_resolution_mps:.3f} m/s (unsigned)"]
        elif self.waveform == "plfm":
            lines += [
                f"pulse {self.pulse_width_s*1e6:.1f} us, PRF {self.prf_hz/1e3:.2f} kHz "
                f"(duty {100*self.duty_cycle:.0f} %), {self.n_fast} samples/PRI, "
                f"{self.n_pulses} pulses/CPI ({self.n_pulses/self.prf_hz*1e3:.0f} ms), "
                f"TBP={self.bandwidth_hz*self.pulse_width_s:.0f} "
                f"(compression gain {10*np.log10(max(self.bandwidth_hz*self.pulse_width_s,1)):.0f} dB)",
                f"range: res={self.range_resolution_m:.2f} m  unambiguous={self.max_range_m/1e3:.1f} km",
                f"velocity: res={self.velocity_resolution_mps:.2f} m/s  "
                f"unambiguous=±{self.max_velocity_mps:.1f} m/s",
            ]
        return "\n".join(lines)


# Ready-made parameter sets ------------------------------------------------

def cantenna_fmcw(ramp_time_ms: float = 4.0, bandwidth_mhz: float = 100.0,
                  n_samples: int = 4096, **kw) -> RadarParams:
    """Phase-1 Pasternack Cantenna RDK, free-running triangle (SWEEP:TYPE AUTO).

    Because fs is fixed at 20 kS/s, the ramp time T sets the trade-off
        R_max  = c*fs*T/(4B)        (longer ramp → more range)
        v_max  = lambda/(4*2T)      (longer ramp → less velocity)
        N_p    = 4096/(2*fs*T)      (longer ramp → fewer pulses, worse Doppler res)
    T = 4 ms, B = 100 MHz gives ~60 m, ±3.8 m/s, 25 pulses.
    """
    f0 = 2.4e9
    return RadarParams(f_start_hz=f0, f_stop_hz=f0 + bandwidth_mhz * 1e6,
                       waveform="fmcw", sweep="triangle",
                       ramp_time_s=ramp_time_ms * 1e-3, fs_hz=20e3,
                       n_samples=n_samples, **kw)


def cantenna_cw(f_hz: float = 2.4e9, n_samples: int = 4096) -> RadarParams:
    """Phase-1 Cantenna in CW mode (SWEEP:TYPE CW): Doppler only, unsigned."""
    return RadarParams(f_start_hz=f_hz, f_stop_hz=f_hz, waveform="cw", sweep="cw",
                       fs_hz=20e3, n_samples=n_samples)


def b210_plfm(fc_hz: float = 2.45e9, bandwidth_hz: float = 25e6, fs_hz: float = 30.72e6,
              pulse_width_s: float = 0.5e-3, prf_hz: float = 1e3, n_pulses: int = 128) -> RadarParams:
    """RF-01 proposal: USRP B210 driving the two Cantenna cans at 2.45 GHz (PLFM).

    Rationale (to be confirmed by the RF-01 Design card; B200/B210 spec sheet
    and UHD B200 manual):
    * The SDR has no analog dechirp mixer, so range comes from the matched
      filter (`pulse_compress`) — the Phase-2 chain, rehearsed early.
    * fs = 30.72 MS/s: one TX and one RX stream run at the same time (full
      duplex) over USB 3.0, i.e. ~2 x 123 MB/s of sc16 on the bus. The B210's
      1x1 ceiling is 61.44 MS/s / 56 MHz bandwidth, but 2 x 246 MB/s is beyond
      what most USB 3.0 controllers sustain — measure with `benchmark_rate`
      (1 TX + 1 RX) before raising it. 30.72 is also the 2x2 (MIMO) ceiling.
    * B = 25 MHz inside the 30.72 MS/s complex band → 6 m range resolution.
      The CDR's 30 MHz chirp sits at the band edge at this rate; 56 MHz
      (2.7 m) needs fs ≥ 61.44 MS/s and therefore the benchmark above.
    * TX output is only ">10 dBm" (spec sheet) vs the Pasternack kit's 1 W
      (+30 dBm), so energy per CPI has to come from duty cycle: 0.5 ms pulses
      at 1 kHz = 50 % duty, TBP = 12 500 → 41 dB compression gain.
    * The RX ports must NEVER see more than −15 dBm (UHD B200 manual). The
      direct TX→RX coupling between two cans sitting side by side can approach
      that at full TX power: start TX gain low and raise it only while
      watching the received peak (see UsrpRadar and the RF-01 doc).
    * 128 pulses at 1 kHz → 128 ms CPI, 0.48 m/s velocity resolution, ±30 m/s.
    * One CPI = 128 × 30 720 complex64 = 31 MB; fine offline, 1–2 CPI/s live.

    Doppler sign: complex baseband echo phase is −2π·fc·τ, so a closing
    target gives a *positive* slow-time frequency → doppler_sign=+1 here
    (the Cantenna's analog mixer gives the opposite convention).
    """
    n_fast = int(round(fs_hz / prf_hz))
    return RadarParams(f_start_hz=fc_hz - bandwidth_hz / 2, f_stop_hz=fc_hz + bandwidth_hz / 2,
                       waveform="plfm", sweep="ramp", fs_hz=fs_hz,
                       n_samples=n_fast * n_pulses, pulse_width_s=pulse_width_s,
                       prf_hz=prf_hz, segment_select="up", doppler_sign=+1)


def phaser_plfm(fs_hz: float = 30.72e6, bandwidth_hz: float = 30e6,
                pulse_width_s: float = 0.26e-6, prf_hz: float = 7.68e3,
                n_pulses: int = 128) -> RadarParams:
    """Phase-2 CN0566 Phaser + USRP B210 numbers as briefed at the CDR.

    Placeholder so the pipeline can be exercised with Phase-2 shapes; the
    hardware layer for it is a later deliverable.
    """
    n_fast = int(round(fs_hz / prf_hz))
    return RadarParams(f_start_hz=10.25e9 - bandwidth_hz / 2,
                       f_stop_hz=10.25e9 + bandwidth_hz / 2,
                       waveform="plfm", sweep="ramp", fs_hz=fs_hz,
                       n_samples=n_fast * n_pulses, pulse_width_s=pulse_width_s,
                       prf_hz=prf_hz, segment_select="up", doppler_sign=+1)
