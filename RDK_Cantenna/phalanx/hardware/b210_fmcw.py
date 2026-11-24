"""The B210 as a stand-in for the Pasternack board: triangle FMCW with a digital dechirp.

Why this exists: Part 1's kit transmits a continuous triangle chirp and mixes the echo with the
outgoing chirp in an analog mixer, so the laptop only ever sees a slow, real "beat" signal at
20 kS/s. The B210 has no such mixer -- it hands over raw complex I/Q at MS/s rates. This module
does in software what the kit's mixer and ADC do in hardware, and returns frames of exactly the
kit's kind (real, 20 kS/s, 4096 samples, waveform="fmcw"), so the whole Part 1 chain -- range
FFT, MTI, Doppler FFT, CFAR, the GUI's replay -- runs on B210 data unchanged. Pulsed LFM with a
matched filter (usrp.py + pulse_compress) is the Phase-2 path and comes after this.

One frame, stage by stage:
  1. UsrpRadar transmits the triangle chirp (100 % duty) and records raw I/Q for the frame.
  2. Latency: the transmit and receive chains have a fixed delay between them (tens of samples).
     At a chirp rate of B/T that delay alone would look like a target hundreds of metres away,
     so the reference is shifted by it first. It is measured from the direct can-to-can leakage
     (cross-correlation with the transmitted chirp) on the first transmitting frame and then
     held; pass latency_samples to fix it. This is the FMCW mode's zero-range calibration.
  3. Dechirp: beat = reference(delayed) x conj(received). Same product as the analog mixer;
     an echo delayed by tau becomes a tone at (B/T)*tau with phase +2*pi*fc*tau, the kit's
     convention, so RadarParams.doppler_sign stays -1 as in Part 1.
  4. Low-pass FIR, THEN decimate to 20 kS/s (filter before decimation: the anti-alias rule).
  5. The frame keeps the complex (I/Q) beat by default. The kit's IF is real, and beat="real"
     reproduces that exactly (it is what the first hardware runs of 10 Nov 2026 used). But the
     real part cannot tell approaching from receding in range cell 0, and with the B210's
     coarse range cells that is where a nearby person is -- the 10 Nov walking test showed it.
     The complex beat keeps the sign everywhere; the pipeline takes the positive-frequency
     half of its range FFT, so every stage after the range FFT is still Part 1's.

Limits compared with the kit: bandwidth is bounded by the sample rate (3 MHz at 4 MS/s ->
50 m range cells; the kit's 100 MHz gives 1.5 m), and transmit power by the TX gain cap.
Velocity resolution and span are the same as the kit's for the same ramp time.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace

import numpy as np
from scipy import fft as sfft
from scipy.signal import resample_poly

from ..dsp.detect import CfarConfig
from ..dsp.pipeline import ProcessorConfig
from ..frame import Frame
from ..params import C0, RadarParams
from .base import RadarSource
from .usrp import BRINGUP_NUM_SEND_FRAMES, UsrpRadar, build_cpi_waveform

BEAT_FS_HZ = 20_000.0        # the kit's ADC rate; the beat signal is delivered at this rate


def b210_fmcw_params(ramp_time_ms: float = 4.0, bandwidth_mhz: float = 3.0,
                     fc_hz: float = 2.45e9, n_samples: int = 4096, **kw) -> RadarParams:
    """Beat-domain parameters, i.e. what the DSP sees: the same shape as cantenna_fmcw()
    (triangle, 20 kS/s, 4096 samples) centred on fc with the B210's narrower sweep."""
    bw = bandwidth_mhz * 1e6
    return RadarParams(f_start_hz=fc_hz - bw / 2, f_stop_hz=fc_hz + bw / 2,
                       waveform="fmcw", sweep="triangle", ramp_time_s=ramp_time_ms * 1e-3,
                       fs_hz=BEAT_FS_HZ, n_samples=n_samples, **kw)


def decimation(raw_fs_hz: float, beat_fs_hz: float = BEAT_FS_HZ) -> int:
    d = raw_fs_hz / beat_fs_hz
    if d < 1 or abs(d - round(d)) > 1e-9:
        raise ValueError(f"raw rate {raw_fs_hz} S/s must be a whole multiple of {beat_fs_hz} S/s")
    return int(round(d))


def raw_params(beat: RadarParams, raw_fs_hz: float = 4e6) -> RadarParams:
    """The same sweep at the B210's sample rate: what is transmitted and received."""
    d = decimation(raw_fs_hz, beat.fs_hz)
    if beat.bandwidth_hz > 0.8 * raw_fs_hz:
        raise ValueError(f"sweep {beat.bandwidth_hz/1e6:g} MHz does not fit in "
                         f"{raw_fs_hz/1e6:g} MS/s (keep it under 80 % of the sample rate)")
    return replace(beat, fs_hz=float(raw_fs_hz), n_samples=beat.n_samples * d)


def estimate_latency(rx: np.ndarray, ref: np.ndarray, max_lag: int = 4096,
                     n_use: int = 1 << 17) -> tuple[int, float]:
    """(delay in samples, peak-to-median dB) of the strongest copy of `ref` in `rx`: the direct
    TX->RX leakage, which physically sits at ~0 m. Cross-correlation over the first n_use samples."""
    n = min(len(rx), len(ref), n_use)
    nfft = sfft.next_fast_len(2 * n)
    c = sfft.ifft(sfft.fft(rx[:n], nfft) * np.conj(sfft.fft(ref[:n], nfft)))
    mag = np.abs(c[:max_lag])
    k = int(np.argmax(mag))
    return k, float(20 * np.log10(mag[k] / (np.median(mag) or 1e-30)))


def dechirp(rx: np.ndarray, ref: np.ndarray, latency: int, decim: int) -> np.ndarray:
    """Digital mixer + anti-alias filter + decimator: raw I/Q -> complex beat at fs/decim."""
    n = len(rx)
    if not 0 <= latency < n:
        raise ValueError(f"latency {latency} outside the frame")
    delayed = np.zeros(n, dtype=np.complex64)
    delayed[latency:] = ref[:n - latency]
    beat = delayed * np.conj(rx)
    return resample_poly(beat, 1, decim).astype(np.complex64)


def beat_samples(beat: np.ndarray, kind: str) -> np.ndarray:
    """The frame's samples from the complex beat: "complex" keeps I/Q, "real" is the kit's IF."""
    if kind == "complex":
        return np.ascontiguousarray(beat, dtype=np.complex64)
    if kind == "real":
        return np.ascontiguousarray(beat.real, dtype=np.float32)
    raise ValueError("beat must be 'complex' or 'real'")


BLIND_CELLS = 1.5       # detections closer than this many range cells are not reported


def blind_range_m(params: RadarParams) -> float:
    """Nearest range at which this mode reports detections: the direct TX->RX leakage fills
    range cell 0 and (through the window) most of cell 1, and nothing in them can be told from
    it reliably. Kit: 1.5 m cells -> 2 m. B210 at 8 MS/s / 6 MHz: 25 m cells -> 37 m."""
    return BLIND_CELLS * params.range_resolution_m


def b210_fmcw_processor_config(params: RadarParams, pfa: float = 1e-4, mti: bool = True,
                               min_range_m: float | None = None,
                               max_range_m: float | None = None, min_speed_mps: float = 0.3,
                               range_mode: str = "ca") -> ProcessorConfig:
    """Detector settings for B210 FMCW frames: Part 1's detector with the gates scaled to the
    B210's range cells.

    * ramp_offset=0, ramp_parity=0: the frame starts on an up-ramp by construction (the kit
      free-runs, so Part 1 has to estimate both).
    * min_range_m defaults to blind_range_m(params) -- the kit's "2 m" gate expressed in cells.
    * range_mode stays "ca". Measured on the three hardware recordings of 10 Nov 2026 (140
      frames, nobody beyond the blind range): "ca" gave 0 false alarms, "soca" gave 110,
      because the real noise is not uniform along range. "soca" sees closer in on simulated
      data and is kept as an option for experiments, not as the default.
    * To look inside the blind range anyway (speed only, no usable range): min_range_m=0 and a
      min_speed_mps of 0.8 or more; expect the leakage's slow residue to show up.
    """
    return ProcessorConfig(cfar=CfarConfig(pfa=pfa, range_mode=range_mode),
                           mti_order=1 if mti else 0, ramp_offset=0, ramp_parity=0,
                           min_range_m=blind_range_m(params) if min_range_m is None else min_range_m,
                           max_range_m=(max(300.0, 12 * params.range_resolution_m)
                                        if max_range_m is None else max_range_m),
                           min_abs_velocity_mps=min_speed_mps)


class B210FmcwRadar(RadarSource):
    """RadarSource that yields kit-style FMCW beat frames from the B210 (see module docstring).
    Receive-only unless transmit=True, exactly like UsrpRadar, which does all the hardware work
    and enforces every transmit guard."""

    def __init__(self, params: RadarParams | None = None, raw_fs_hz: float = 4e6,
                 serial: str = "", tx_gain_db: float = 40.0, rx_gain_db: float = 40.0,
                 max_tx_gain_db: float = 44.0, transmit: bool = False,
                 latency_samples: int | None = None, beat: str = "complex", **usrp_kw):
        super().__init__(params or b210_fmcw_params())
        if self.params.waveform != "fmcw":
            raise ValueError("B210FmcwRadar needs FMCW parameters (b210_fmcw_params)")
        if beat not in ("complex", "real"):
            raise ValueError("beat must be 'complex' or 'real'")
        self.beat = beat
        self.raw = raw_params(self.params, raw_fs_hz)
        self.decim = decimation(raw_fs_hz, self.params.fs_hz)
        self.latency_samples = latency_samples
        self.latency_peak_db: float | None = None
        usrp_kw.setdefault("num_send_frames", BRINGUP_NUM_SEND_FRAMES)
        self._usrp = UsrpRadar(self.raw, serial=serial, tx_gain_db=tx_gain_db,
                               rx_gain_db=rx_gain_db, max_tx_gain_db=max_tx_gain_db,
                               transmit=transmit, tx_waveform="fmcw", **usrp_kw)
        self._ref = build_cpi_waveform(self.raw, "fmcw")      # unit amplitude; the mixer's LO
        self.info: dict = {}

    def open(self) -> "B210FmcwRadar":
        self._usrp.open()
        return self

    def close(self) -> None:
        self._usrp.close()

    def configure(self, params: RadarParams | None = None) -> None:
        if params is not None and params != self.params:
            raise ValueError("create a new B210FmcwRadar to change the sweep")
        self._usrp.configure()
        self.info = dict(self._usrp.info, raw_rate_hz=self.raw.fs_hz, decimation=self.decim,
                         beat_rate_hz=self.params.fs_hz)

    def start(self) -> None:
        self._usrp.start()

    def stop(self) -> None:
        self._usrp.stop()

    def set_tx_gain(self, gain_db: float) -> float:
        return self._usrp.set_tx_gain(gain_db)

    def capture(self, transmit: bool | None = None) -> Frame:
        raw = self._usrp.capture(transmit)
        tx_on = bool(raw.meta["tx_enabled"])
        if tx_on and self.latency_samples is None:
            self.latency_samples, self.latency_peak_db = estimate_latency(raw.samples, self._ref)
        beat = dechirp(raw.samples, self._ref, self.latency_samples or 0, self.decim)
        x = beat_samples(beat, self.beat)
        # Leakage = the beat's DC term (zero range). Measured on the complex beat, because the
        # real part alone depends on the leakage's phase.
        leak = float(np.abs(beat.mean()))
        meta = {k: raw.meta[k] for k in ("rx_errors", "tx_errors", "device_time_s", "rx_peak",
                                         "rx_clipping", "tx_enabled", "tx_gain", "rx_gain",
                                         "capture_s")}
        meta.update(raw=x, signedness=str(x.dtype), signal_mode="fmcw_dechirped", sim=False,
                    idn=raw.meta.get("idn", ""), timestamp_source="host_clock",
                    latency_samples=self.latency_samples, latency_peak_db=self.latency_peak_db,
                    leakage_dbfs=float(20 * np.log10(max(leak, 1e-12))),
                    raw_rate_hz=self.raw.fs_hz, decimation=self.decim,
                    device=dict(self.info))
        return Frame(samples=x, params=self.params, timestamp=raw.timestamp,
                     seq=self._next_seq(), meta=meta)


# ---------------------------------------------------------------------------------------------
# Simulator: the same front end fed with synthetic raw I/Q (no hardware, no transmission)
# ---------------------------------------------------------------------------------------------

@dataclass
class SimWalker:
    """A point target that walks back and forth in front of the cans."""
    range_m: float = 85.0
    velocity_mps: float = 1.4        # + = closing
    level_db_at_10m: float = -62.0   # echo level re full scale when 10 m away (falls as 1/R^4)
    near_m: float = 5.0              # turns around here ...
    far_m: float = 90.0              # ... and here


class SimulatedB210Fmcw(RadarSource):
    """Synthetic B210 raw I/Q (leakage, fixed TX->RX latency, walkers, noise) pushed through the
    real latency estimate and dechirp, so the GUI and the tests exercise exactly the code the
    hardware uses. Echo delays are applied as a phase slope in the frequency domain, so ranges
    are not quantised to whole samples (one sample at 4 MS/s is 37 m of range)."""

    def __init__(self, params: RadarParams | None = None, raw_fs_hz: float = 4e6,
                 walkers: list[SimWalker] | None = None, leak_db: float = -24.0,
                 noise_rms: float = 3e-4, latency_samples: float = 62.0,
                 frame_period_s: float = 0.4, beat: str = "complex", transmit: bool = True,
                 seed: int | None = 0):
        super().__init__(params or b210_fmcw_params())
        self.raw = raw_params(self.params, raw_fs_hz)
        self.decim = decimation(raw_fs_hz, self.params.fs_hz)
        self.walkers = [replace(w) for w in (walkers if walkers is not None else [SimWalker()])]
        self.leak_db, self.noise_rms = leak_db, noise_rms
        self.true_latency = float(latency_samples)
        self.frame_period_s = frame_period_s
        self.beat, self.transmit = beat, transmit
        self.latency_samples: int | None = None
        self.latency_peak_db: float | None = None
        self._rng = np.random.default_rng(seed)
        self._ref = build_cpi_waveform(self.raw, "fmcw")
        self._ref_f = sfft.fft(self._ref)
        self._f = sfft.fftfreq(len(self._ref))               # cycles per sample
        self._t = np.arange(len(self._ref)) / self.raw.fs_hz
        self._next_t = None
        self.last_raw: np.ndarray | None = None
        self.info = {"model": "simulated B210", "serial": "SIM", "raw_rate_hz": self.raw.fs_hz,
                     "decimation": self.decim, "beat_rate_hz": self.params.fs_hz}

    def configure(self, params: RadarParams | None = None) -> None:
        if params is not None and params != self.params:
            raise ValueError("create a new SimulatedB210Fmcw to change the sweep")

    def start(self) -> None:
        self._next_t = time.perf_counter()

    def stop(self) -> None:
        pass

    def _delayed(self, delay_samples: float) -> np.ndarray:
        return sfft.ifft(self._ref_f * np.exp(-2j * np.pi * self._f * delay_samples))

    def capture(self, transmit: bool | None = None) -> Frame:
        if self.frame_period_s > 0:                           # pace like the hardware
            now = time.perf_counter()
            self._next_t = max(self._next_t or now, now)
            time.sleep(max(0.0, self._next_t - now))
            self._next_t += self.frame_period_s
        tx_on = self.transmit if transmit is None else bool(transmit)
        n = len(self._ref)
        x = self.noise_rms / np.sqrt(2) * (self._rng.standard_normal(n)
                                           + 1j * self._rng.standard_normal(n))
        if tx_on:
            x += 10 ** (self.leak_db / 20) * self._delayed(self.true_latency)
            for w in self.walkers:
                delay = self.true_latency + 2 * w.range_m / C0 * self.raw.fs_hz
                amp = 10 ** ((w.level_db_at_10m + 40 * np.log10(10.0 / max(w.range_m, 1.0))) / 20)
                r_t = w.range_m - w.velocity_mps * self._t
                x += amp * self._delayed(delay) * np.exp(-2j * np.pi * self.raw.fc_hz * 2 * r_t / C0)
        for w in self.walkers:                                # move on, turn at the ends
            w.range_m -= w.velocity_mps * (self.frame_period_s or len(self._ref) / self.raw.fs_hz)
            if w.range_m <= w.near_m or w.range_m >= w.far_m:
                w.velocity_mps = -w.velocity_mps
                w.range_m = float(np.clip(w.range_m, w.near_m, w.far_m))
        x = x.astype(np.complex64)
        self.last_raw = x                                     # for scripts/stage_dump.py
        if tx_on and self.latency_samples is None:
            self.latency_samples, self.latency_peak_db = estimate_latency(x, self._ref)
        beat = dechirp(x, self._ref, self.latency_samples or 0, self.decim)
        samples = beat_samples(beat, self.beat)
        peak = float(np.abs(x).max())
        return Frame(samples=samples, params=self.params, timestamp=time.time(),
                     seq=self._next_seq(),
                     meta={"raw": samples, "signedness": str(samples.dtype),
                           "signal_mode": "fmcw_dechirped", "sim": True, "idn": "simulated B210",
                           "rx_errors": [], "tx_errors": [], "rx_peak": peak,
                           "rx_clipping": peak > 0.9, "tx_enabled": tx_on,
                           "tx_gain": None, "rx_gain": None,
                           "latency_samples": self.latency_samples,
                           "latency_peak_db": self.latency_peak_db,
                           "leakage_dbfs": float(20 * np.log10(max(abs(beat.mean()), 1e-12))),
                           "raw_rate_hz": self.raw.fs_hz, "decimation": self.decim,
                           "device": dict(self.info)})
