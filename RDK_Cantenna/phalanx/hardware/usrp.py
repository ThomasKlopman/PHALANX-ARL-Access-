"""USRP B210 as a pulsed-LFM radar transceiver (RF-01 / Phase-2 rehearsal).

STATUS: receive-only operation of this board is verified (b210.py, 5 Nov 2026);
this transmit+receive driver has NOT yet run on hardware. First-contact
procedure: Part2_B210_Cantenna/README.md and docs/rf01_b210_cantenna_interface.md.

Hardware (Ettus B200/B210 spec sheet; UHD B200 manual): 70 MHz–6 GHz,
2 TX / 2 RX, full duplex, 12-bit ADC/DAC, 56 MHz instantaneous bandwidth
(1x1) / 30.72 MHz (2x2), master clock 5–61.44 MHz, USB 3.0 (USB 2.0 works at
lower rates and then needs the external 6 V supply), TX output > 10 dBm,
RX noise figure < 8 dB, TX gain 0–89.8 dB, RX gain 0–76 dB, timed commands
in the FPGA. Ports per channel: "TX/RX" (transmit or receive) and "RX2"
(receive only).

    !! The RX ports must never see more than -15 dBm (UHD B200 manual). !!
    With TX at full power (>+10 dBm) into one can and the other can a few
    centimetres away, the direct coupling can exceed that. This driver
    therefore starts at a low TX gain, caps it at `max_tx_gain_db` unless
    you raise the cap deliberately, and flags any CPI whose received peak
    is near full scale (meta["rx_peak"], meta["rx_clipping"]). Use
    Part2_B210_Cantenna/scripts/isolation_sweep.py to raise TX gain stepwise.

How one CPI is produced
-----------------------
1. The whole CPI waveform (n_pulses PRIs of [chirp, zeros]) is built on the
   host from params.reference_chirp() — the SAME array the matched filter uses.
2. TX (port TX/RX, channel A) and RX (port RX2, channel A) are both scheduled
   with timed commands at t0 on the device clock. That makes the TX/RX
   relationship *repeatable*, not zero: the DAC and ADC paths have different
   fixed latencies (digital filters, AD9361 paths), so the first RX sample
   is NOT the first TX sample. The direct TX→RX leakage, which physically
   sits at ~0 m, marks true zero range; `estimate_zero_range_offset()` finds
   its bin and the result is used as the pipeline's `ramp_offset`. That
   measurement is the B210's "calibration" (there is no analog mixer to
   calibrate). Measure it once per start; it should repeat across restarts.
3. RX returns complex64 (fc32 on the host, sc16 over the wire). Overflow /
   sequence errors reported by UHD, and TX underflow / late-packet reports,
   are collected in Frame.meta["rx_errors"] and ["tx_errors"] because RF-01's
   acceptance criterion is "zero reported overflow or sequence errors over 60 s".

USB buffering: UHD's B200 default (16 x 8 KB transfers per direction) gives
only ~32 ms of host-side slack at 1 MS/s and ~1 ms at 30.72 MS/s. The device
arguments therefore request `num_recv_frames` / `num_send_frames` (default 512
each, ≈ 4 MB per direction), the same fix that made the receive monitor pass
its 60 s runs (CHANGELOG 2026-11-05).

First contact is deliberately NOT at the 30.72 MS/s preset. `first_contact_params()`
gives 4 MS/s / 3 MHz: 50 m range resolution, useless as a radar, but one CPI is
4 MB instead of 31 MB and the bus runs at ~1/8 of the final load, so timing,
leakage level and gain safety are tested one unknown at a time. Then run
`benchmark_rate --rx_rate 30.72e6 --tx_rate 30.72e6` before the full preset.
"""
from __future__ import annotations

import logging
import threading
import time

import numpy as np

from ..frame import Frame
from ..params import RadarParams, b210_plfm
from .base import RadarSource

log = logging.getLogger(__name__)

DEFAULT_NUM_RECV_FRAMES = 512
DEFAULT_NUM_SEND_FRAMES = 512
CLIP_FRACTION = 0.9          # |x| above this fraction of full scale → meta["rx_clipping"]

# Two-level TX gain limit (B210 TX gain range is 0–89.8 dB):
#   * max_tx_gain_db   — per-instance cap, default 60 dB, raised deliberately after a sweep;
#   * ABSOLUTE_TX_GAIN_CEILING_DB — nobody can pass a cap above this without also passing
#     unlock_full_power=True, so a typo or a GUI spin box cannot reach full output.
# These are RELATIVE gain settings, not calibrated output power. What reaches the RX
# connector is established only by measurement (docs/usrp_damage_review_2026-11-05.md).
ABSOLUTE_TX_GAIN_CEILING_DB = 70.0
TONE_OFFSET_HZ = 100e3       # "tone" test waveform: CW at fc + this, clear of DC / LO leakage
# Bring-up commands REQUEST fewer USB send frames on the TX side, because an abort cannot
# recall samples already handed to the device. This is a requested host-side parameter, not
# a verified bound on device buffering or shutdown latency (FPGA FIFOs and USB are on top of
# it); the real residual is to be MEASURED on the analyser (abort during a tone, time how long
# RF persists) — see docs/usrp_damage_review_2026-11-05.md. TX gain 0 dB after an abort is
# maximum attenuation, not RF off.
BRINGUP_NUM_SEND_FRAMES = 64


def first_contact_params(**overrides) -> RadarParams:
    """Low-rate PLFM preset for the first transmit run (see module docstring)."""
    kw = dict(fs_hz=4e6, bandwidth_hz=3e6)
    kw.update(overrides)
    return b210_plfm(**kw)


def build_cpi_waveform(params: RadarParams, kind: str = "plfm",
                       tone_offset_hz: float = TONE_OFFSET_HZ) -> np.ndarray:
    """(n_samples,) complex64 transmit waveform for one CPI.

    kind="plfm": the reference chirp at the start of every PRI, zeros after (the radar).
    kind="tone": an unmodulated carrier at fc + tone_offset_hz for the whole CPI — the
    bring-up waveform (steady leakage reading, LO/TX path check, Doppler by walking).
    """
    if kind == "tone":
        n = params.n_samples
        t = np.arange(n) / params.fs_hz
        return np.exp(2j * np.pi * tone_offset_hz * t).astype(np.complex64)
    if kind == "fmcw":
        # Continuous chirp, 100 % duty: baseband frequency sweeps -B/2 .. +B/2 over each ramp
        # (and back down on odd ramps for a triangle). Phase is the running sum of the
        # instantaneous frequency, so it is continuous across ramps. See b210_fmcw.py.
        if params.waveform != "fmcw":
            raise ValueError("kind='fmcw' needs FMCW parameters (waveform='fmcw')")
        n, n_f, bw = params.n_samples, params.n_fast, params.bandwidth_hz
        k = np.arange(n)
        frac = (k % n_f) / n_f
        f = -bw / 2 + bw * frac
        if params.sweep == "triangle":
            down = (k // n_f) % 2 == 1
            f[down] = bw / 2 - bw * frac[down]
        return np.exp(2j * np.pi * np.cumsum(f) / params.fs_hz).astype(np.complex64)
    if kind != "plfm":
        raise ValueError(f"unknown waveform kind {kind!r} (plfm, fmcw or tone)")
    s = params.reference_chirp()
    if len(s) > params.n_fast:
        raise ValueError(f"pulse ({len(s)} samples) is longer than the PRI ({params.n_fast}): "
                         f"pulse_width_s * prf_hz must be < 1 (duty {params.duty_cycle:.2f})")
    pri = np.zeros(params.n_fast, dtype=np.complex64)
    pri[:len(s)] = s
    return np.tile(pri, params.n_pulses)


def build_device_args(serial: str = "", num_recv_frames: int = DEFAULT_NUM_RECV_FRAMES,
                      num_send_frames: int = DEFAULT_NUM_SEND_FRAMES, extra: str = "") -> str:
    """UHD device-argument string: always a B200-series device, optionally one serial,
    with explicit USB buffering. `extra` is appended verbatim (e.g. "master_clock_rate=32e6")."""
    parts = ["type=b200"]
    if serial.strip():
        parts.append(f"serial={serial.strip()}")
    if num_recv_frames > 0:
        parts.append(f"num_recv_frames={int(num_recv_frames)}")
    if num_send_frames > 0:
        parts.append(f"num_send_frames={int(num_send_frames)}")
    if extra.strip():
        parts.append(extra.strip())
    return ",".join(parts)


def estimate_zero_range_offset(samples: np.ndarray, params: RadarParams,
                               max_pulses: int = 16) -> tuple[int, float]:
    """Find the direct TX→RX leakage in a raw CPI and return (offset_samples, peak_db).

    offset_samples is the fast-time bin (0 ≤ k < n_fast) of the strongest return,
    averaged incoherently over the first `max_pulses` pulses with the PRI grid at
    offset 0. Pass it as ProcessorConfig.ramp_offset so that bin 0 of the range
    axis is the leakage, i.e. true zero range. peak_db is the leakage level
    relative to the median range bin (a proxy for leakage-to-noise ratio).
    The matched filter here is the same `pulse_compress` the pipeline uses.
    """
    from ..dsp.conditioning import reshape_cpi
    from ..dsp.rangeproc import pulse_compress
    pulses = reshape_cpi(samples, params.n_fast, 0, params.segments_per_pri, 0)[:max_pulses]
    rp = pulse_compress(pulses, params.reference_chirp())
    profile = (np.abs(rp) ** 2).mean(axis=0)
    k = int(np.argmax(profile))
    floor = float(np.median(profile)) or 1e-30
    return k, float(10 * np.log10(profile[k] / floor))


class UsrpRadar(RadarSource):
    # Device limits from the B210 probe (uhd_usrp_probe, 5 Nov 2026). The live device
    # range is read back at configure() when the API offers it; these are the fallback.
    B210_TX_GAIN_RANGE_DB = (0.0, 89.8)
    B210_RX_GAIN_RANGE_DB = (0.0, 76.0)
    # Samples are handed to / taken from UHD in chunks of about this much signal time (a whole
    # number of packets). One packet per call is ~2000 Python calls per second at 4 MS/s but
    # ~15 000 at 30.72 MS/s, which the host cannot keep up with; 1 ms chunks keep it near 1000
    # calls per second at any rate. At 4 MS/s a chunk is still one packet, so the runs already
    # made are unchanged. The abort flag is checked between chunks, i.e. about every 1 ms.
    chunk_time_s = 1e-3

    def __init__(self, params: RadarParams | None = None, serial: str = "",
                 tx_gain_db: float = 40.0, rx_gain_db: float = 10.0,
                 max_tx_gain_db: float = 60.0,
                 tx_channel: int = 0, rx_channel: int = 0,
                 tx_antenna: str = "TX/RX", rx_antenna: str = "RX2",
                 tx_amplitude: float = 0.5, lead_time_s: float = 0.2,
                 analog_bandwidth_hz: float | None = None,
                 num_recv_frames: int = DEFAULT_NUM_RECV_FRAMES,
                 num_send_frames: int = DEFAULT_NUM_SEND_FRAMES,
                 extra_device_args: str = "", transmit: bool = False,
                 zero_range_offset: int = 0, tx_waveform: str = "plfm",
                 unlock_full_power: bool = False):
        super().__init__(params or b210_plfm())
        for name, value in (("tx_gain_db", tx_gain_db), ("rx_gain_db", rx_gain_db),
                            ("max_tx_gain_db", max_tx_gain_db), ("tx_amplitude", tx_amplitude),
                            ("lead_time_s", lead_time_s)):
            if not np.isfinite(value):
                raise ValueError(f"{name}={value!r} is not a finite number")
        if max_tx_gain_db > ABSOLUTE_TX_GAIN_CEILING_DB and not unlock_full_power:
            raise ValueError(f"max_tx_gain_db={max_tx_gain_db} is above the "
                             f"{ABSOLUTE_TX_GAIN_CEILING_DB} dB ceiling; pass unlock_full_power=True "
                             "only with a measured isolation log that shows it is safe.")
        if not 0.0 < tx_amplitude <= 1.0:
            raise ValueError(f"tx_amplitude={tx_amplitude} must be in (0, 1] (fraction of DAC full scale)")
        if tx_waveform not in ("plfm", "fmcw", "tone"):
            raise ValueError("tx_waveform must be 'plfm', 'fmcw' or 'tone'")
        if lead_time_s <= 0:
            raise ValueError("lead_time_s must be positive")
        self.max_tx_gain_db = float(max_tx_gain_db)
        self._full_power_unlocked = bool(unlock_full_power)
        self.tx_waveform = tx_waveform
        self.serial = serial.strip()
        self.num_send_frames = int(num_send_frames)
        self.device_args = build_device_args(self.serial, num_recv_frames, num_send_frames,
                                             extra_device_args)
        self.tx_gain_db, self.rx_gain_db = float(tx_gain_db), float(rx_gain_db)
        self.tx_channel, self.rx_channel = tx_channel, rx_channel
        self.tx_antenna, self.rx_antenna = tx_antenna, rx_antenna
        self.tx_amplitude = float(tx_amplitude)   # fraction of full scale (avoid DAC clipping)
        self.lead_time_s = float(lead_time_s)     # how far ahead of 'now' to schedule a CPI
        self.analog_bandwidth_hz = analog_bandwidth_hz   # None → 1.2 × chirp bandwidth
        self.transmit = transmit              # False → RX only (noise floor / safety check)
        self.zero_range_offset = int(zero_range_offset)  # measured leakage bin, see module doc
        self._uhd = self._usrp = self._tx = self._rx = None
        self._waveform: np.ndarray | None = None
        self._tx_abort = threading.Event()
        self._tx_done = threading.Event()      # set by the worker only after a complete, clean burst
        self._tx_thread: threading.Thread | None = None
        self._tx_faulted: str | None = None   # set when a TX worker would not exit; blocks captures
        self.close_timeout_s = 10.0            # how long close() waits for a live TX worker
        self.abort_join_timeout_s = 2.0        # how long an abort waits for the worker to exit
        self.info: dict = {}
        self._validate_gains()                # finite, within cap/ceiling/device range (no device yet)

    # --- lifecycle ----------------------------------------------------------
    def open(self) -> "UsrpRadar":
        import uhd  # noqa: F401  (UHD python bindings; see docs for install)
        self._uhd = uhd
        self._usrp = uhd.usrp.MultiUSRP(self.device_args)
        name = self._usrp.get_mboard_name()
        if "B210" not in name.upper():
            self._usrp = None
            raise ValueError(f"UsrpRadar expects a USRP B210, found {name!r}")
        self._usrp.set_tx_gain(0.0, self.tx_channel)   # cold start: maximum attenuation until a burst
        self.info = {"model": name, "device_args": self.device_args}
        log.info("Opened %s (%s)", name, self.device_args)
        return self

    def close(self) -> None:
        """stop() (abort any burst, TX gain to 0 dB, join the TX worker), then release the
        handles — but only once the worker has exited. A worker that will not exit keeps the
        handles alive (releasing them under a live send() is worse) and close() raises."""
        self.stop()
        t = self._tx_thread
        if t is not None and t.is_alive():
            t.join(timeout=self.close_timeout_s)
        if t is not None and t.is_alive():
            self._tx_faulted = self._tx_faulted or "TX worker did not exit before close()"
            log.error("%s; UHD handles NOT released", self._tx_faulted)
            raise RuntimeError(self._tx_faulted)
        # Worker verified exited (or never existed): release, and clear the fault latch — this
        # is the only recovery path. A new open()/configure() is required before any capture.
        self._tx_thread = None
        self._tx = self._rx = self._usrp = None
        self._waveform = None
        if self._tx_faulted:
            log.info("TX fault cleared on close(): %s", self._tx_faulted)
        self._tx_faulted = None

    # --- validation (called before every hardware write and before every burst) ---------
    def _device_gain_range(self, which: str) -> tuple[float, float]:
        fallback = self.B210_TX_GAIN_RANGE_DB if which == "tx" else self.B210_RX_GAIN_RANGE_DB
        if self._usrp is None:
            return fallback
        try:
            r = getattr(self._usrp, f"get_{which}_gain_range")(getattr(self, f"{which}_channel"))
            lo, hi = float(r.start()), float(r.stop())
            return (lo, hi) if np.isfinite(lo) and np.isfinite(hi) and hi > lo else fallback
        except Exception:
            return fallback

    def _validate_gains(self, tx_gain_db: float | None = None) -> None:
        tx = self.tx_gain_db if tx_gain_db is None else float(tx_gain_db)
        if not (np.isfinite(tx) and np.isfinite(self.rx_gain_db) and np.isfinite(self.max_tx_gain_db)):
            raise ValueError("gains must be finite numbers")
        if self.max_tx_gain_db > ABSOLUTE_TX_GAIN_CEILING_DB and not self._full_power_unlocked:
            raise ValueError("TX gain cap above the absolute ceiling without unlock_full_power")
        if not 0.0 <= tx <= self.max_tx_gain_db:
            raise ValueError(f"tx_gain_db={tx} outside [0, {self.max_tx_gain_db}] (the cap). "
                             "Raise the cap only with a measured isolation log.")
        lo, hi = self._device_gain_range("tx")
        if not lo <= tx <= hi:
            raise ValueError(f"tx_gain_db={tx} outside the device's TX gain range [{lo}, {hi}]")
        lo, hi = self._device_gain_range("rx")
        if not lo <= self.rx_gain_db <= hi:
            raise ValueError(f"rx_gain_db={self.rx_gain_db} outside the device's RX gain range [{lo}, {hi}]")

    @staticmethod
    def _validate_waveform(wf: np.ndarray) -> None:
        """Checked at configure() and again right before every burst."""
        if wf is None or wf.ndim != 1 or len(wf) == 0:
            raise ValueError("transmit waveform is empty")
        if not np.isfinite(wf).all():
            raise ValueError("transmit waveform contains NaN/Inf")
        peak = float(np.abs(wf).max())
        if peak > 1.0:
            raise ValueError(f"transmit waveform peak {peak:.3f} exceeds DAC full scale")

    def _check_device_tx_gain(self) -> float:
        actual = float(self._usrp.get_tx_gain(self.tx_channel))
        if not np.isfinite(actual) or actual > self.max_tx_gain_db + 0.25:
            raise ValueError(f"device TX gain read-back {actual} dB exceeds the cap "
                             f"{self.max_tx_gain_db} dB; refusing to transmit")
        return actual

    # --- RadarSource --------------------------------------------------------
    def _analog_bandwidth(self) -> float:
        if self.analog_bandwidth_hz:
            return float(self.analog_bandwidth_hz)
        return float(min(1.2 * self.params.bandwidth_hz, self.params.fs_hz))

    def configure(self, params: RadarParams | None = None) -> None:
        uhd = self._uhd
        if params is not None:
            self.params = params
        p, u = self.params, self._usrp
        if p.waveform not in ("plfm", "fmcw"):
            raise ValueError("UsrpRadar needs pulsed-LFM (b210_plfm) or FMCW (b210_fmcw) parameters")
        if self.tx_waveform in ("plfm", "fmcw") and self.tx_waveform != p.waveform:
            raise ValueError(f"tx_waveform={self.tx_waveform!r} does not match the parameters' "
                             f"waveform {p.waveform!r}")
        # Everything that can be rejected is rejected BEFORE the first hardware write.
        self._validate_gains()
        waveform = (self.tx_amplitude * build_cpi_waveform(p, self.tx_waveform)).astype(np.complex64)
        self._validate_waveform(waveform)
        u.set_tx_rate(p.fs_hz, self.tx_channel)
        u.set_rx_rate(p.fs_hz, self.rx_channel)
        actual_tx, actual_rx = u.get_tx_rate(self.tx_channel), u.get_rx_rate(self.rx_channel)
        if abs(actual_rx - p.fs_hz) > 1 or abs(actual_tx - p.fs_hz) > 1:
            raise ValueError(f"device chose fs={actual_rx/1e6:.3f} MS/s, wanted "
                             f"{p.fs_hz/1e6:.3f} — pick a rate the master clock divides")
        u.set_tx_freq(uhd.types.TuneRequest(p.fc_hz), self.tx_channel)
        u.set_rx_freq(uhd.types.TuneRequest(p.fc_hz), self.rx_channel)
        bw = self._analog_bandwidth()
        u.set_tx_bandwidth(bw, self.tx_channel)
        u.set_rx_bandwidth(bw, self.rx_channel)
        u.set_tx_gain(0.0, self.tx_channel)   # stays at maximum attenuation until capture(transmit=True)
        u.set_rx_gain(self.rx_gain_db, self.rx_channel)
        u.set_tx_antenna(self.tx_antenna, self.tx_channel)
        u.set_rx_antenna(self.rx_antenna, self.rx_channel)
        u.set_time_now(uhd.types.TimeSpec(0.0))
        st = uhd.usrp.StreamArgs("fc32", "sc16")
        st.channels = [self.tx_channel]
        self._tx = u.get_tx_stream(st)
        st = uhd.usrp.StreamArgs("fc32", "sc16")
        st.channels = [self.rx_channel]
        self._rx = u.get_rx_stream(st)
        self._waveform = waveform
        # Read-backs, using the same keys as B210ReceiveRadar so readback_sessions.py
        # compares TX+RX sessions the same way it compares RX-only ones.
        self.info.update(
            serial=self.serial or str(u.get_usrp_rx_info(self.rx_channel).get("mboard_serial", "")),
            rx_rate_hz=actual_rx, tx_rate_hz=actual_tx,
            rx_frequency_hz=u.get_rx_freq(self.rx_channel),
            tx_frequency_hz=u.get_tx_freq(self.tx_channel),
            rx_gain_db=u.get_rx_gain(self.rx_channel), tx_gain_db=self.tx_gain_db,
            tx_gain_range_db=self._device_gain_range("tx"),
            rx_bandwidth_hz=u.get_rx_bandwidth(self.rx_channel),
            tx_bandwidth_hz=u.get_tx_bandwidth(self.tx_channel),
            antenna=u.get_rx_antenna(self.rx_channel), tx_antenna=u.get_tx_antenna(self.tx_channel),
            channel=self.rx_channel, tx_channel=self.tx_channel,
            clock_source=u.get_clock_source(0), time_source=u.get_time_source(0),
            lo_locked=bool(u.get_rx_sensor("lo_locked", self.rx_channel).to_bool()),
            uhd_version=uhd.get_version_string(),
            tx_amplitude=self.tx_amplitude, transmit=self.transmit,
            tx_waveform=self.tx_waveform, max_tx_gain_db=self.max_tx_gain_db,
            num_send_frames=self.num_send_frames)
        log.info("configured: %s", self.info)

    def set_tx_gain(self, gain_db: float) -> float:
        """Change the TX gain used by the next burst. Validated (cap, ceiling, device range)
        BEFORE anything is written; the hardware is only written when a burst is armed."""
        self._validate_gains(gain_db)
        self.tx_gain_db = float(gain_db)
        return self.tx_gain_db

    def start(self) -> None:
        pass          # nothing free-running: each capture() schedules its own TX+RX burst

    def stop(self) -> None:
        """Best-effort shutdown: abort any burst in flight, request TX gain 0 dB (maximum
        attenuation — not RF off), join the TX worker. Samples already handed to the device
        still play out; how long that lasts is not known until it is measured on the
        analyser (docs/usrp_damage_review_2026-11-05.md, measurement step 4)."""
        self._abort_tx("stop()")

    def _abort_tx(self, reason: str) -> None:
        self._tx_abort.set()
        try:
            if self._usrp is not None:
                self._usrp.set_tx_gain(0.0, self.tx_channel)   # first: attenuate, before anything else
        except Exception as error:               # cleanup must not raise over a cleanup step
            log.warning("could not zero TX gain (%s): %s", reason, error)
        t = self._tx_thread
        if t is not None and t.is_alive():
            t.join(timeout=self.abort_join_timeout_s)
        if t is not None and t.is_alive():
            # Keep the reference: a live worker must stay joinable, further captures are
            # refused, and close() will not release the handles under it.
            self._tx_faulted = f"TX worker still alive after abort ({reason})"
            log.error(self._tx_faulted)
        else:
            self._tx_thread = None

    # --- transmit side ------------------------------------------------------
    def _worker_attenuate(self, reason: str) -> None:
        """Called from the TX worker itself on its own failure: request maximum attenuation
        and raise the abort flag at once, before the main thread notices anything."""
        self._tx_abort.set()
        try:
            self._usrp.set_tx_gain(0.0, self.tx_channel)
        except Exception as error:
            log.warning("worker could not zero TX gain (%s): %s", reason, error)

    def _close_burst(self, md, errors: list) -> None:
        """Best-effort end-of-burst. Tolerates a worker that failed before its metadata
        existed (md is None) or with it half-initialised: builds what it needs, and records
        rather than raises if even that is impossible."""
        try:
            if md is None:
                md = self._uhd.types.TXMetadata()
            md.start_of_burst, md.has_time_spec, md.end_of_burst = False, False, True
            self._tx.send(np.zeros((1, 0), dtype=np.complex64), md, self._send_timeout_s())
        except Exception as error:
            errors.append(f"tx end-of-burst failed {error!r}")

    def _chunk_samples(self, packet_samples: int) -> int:
        packets = int(self.params.fs_hz * self.chunk_time_s // packet_samples)
        return packet_samples * max(1, packets)

    def _send_timeout_s(self) -> float:
        """Timeout for one tx.send() call. UHD's default is 0.1 s, which is shorter than the
        lead time: the burst is scheduled lead_time_s ahead, the device does not start
        consuming samples until then, so once the host-side USB frames are full a send must
        wait that long. With the default, the first run on hardware (10 Nov 2026) ended in
        'tx short send 0/2040' before the burst had even started."""
        return self.lead_time_s + 1.0

    def _tx_worker(self, t0: float, errors: list) -> None:
        """Thread target. Nothing escapes it: ANY failure in setup or sending records an
        error (so capture() fails the CPI), requests attenuation, and attempts end-of-burst.
        `_tx_done` is set only when the whole burst was handed over without error."""
        state = {"md": None}
        try:
            self._send_cpi(t0, errors, state)
        except BaseException as error:                 # setup or send: never leave it silent
            errors.append(f"tx worker exception {error!r}")
            self._worker_attenuate("worker exception")
            self._close_burst(state["md"], errors)
            return
        if not errors:
            self._tx_done.set()

    def _send_cpi(self, t0: float, errors: list, state: dict) -> None:
        """Blocking TX of one CPI starting at device time t0 (called by _tx_worker, which
        owns exception handling). On the first short send: stop sending further chunks,
        request attenuation, close the burst, return with the error recorded."""
        uhd, tx = self._uhd, self._tx
        md = uhd.types.TXMetadata()
        state["md"] = md
        md.start_of_burst, md.end_of_burst = True, False
        md.has_time_spec, md.time_spec = True, uhd.types.TimeSpec(t0)
        spb = int(tx.get_max_num_samps())
        if spb <= 0:
            raise RuntimeError(f"TX streamer reports max_num_samps={spb}")
        wf = self._waveform
        step = self._chunk_samples(spb)                # UHD splits a chunk into packets itself
        for i in range(0, len(wf), step):
            if self._tx_abort.is_set():
                errors.append("tx aborted")
                self._close_burst(md, errors)          # nothing more is sent
                return
            chunk = wf[i:i + step]
            md.end_of_burst = (i + step) >= len(wf)
            sent = tx.send(chunk.reshape(1, -1), md, self._send_timeout_s())
            md.start_of_burst, md.has_time_spec = False, False
            if sent != len(chunk):
                errors.append(f"tx short send {sent}/{len(chunk)}")
                self._worker_attenuate("short send")
                if not md.end_of_burst:
                    self._close_burst(md, errors)
                return                                 # no further chunks

    def _drain_tx_async(self, errors: list, timeout_s: float) -> None:
        """Collect TX async messages (underflow, late packet, ...). Must run AFTER the
        burst has finished playing: the reports arrive while it plays, not when
        send() returns, so draining right after send() misses them."""
        uhd, tx = self._uhd, self._tx
        amd = uhd.types.TXAsyncMetadata()
        deadline = time.time() + timeout_s
        while tx.recv_async_msg(amd, 0.05):
            if amd.event_code != uhd.types.TXMetadataEventCode.burst_ack:
                errors.append(f"tx async {amd.event_code}")
            if time.time() > deadline:
                break

    # --- one CPI --------------------------------------------------------------
    def capture(self, transmit: bool | None = None) -> Frame:
        """One CPI. transmit=False receives with the transmitter silent (noise floor,
        and the safe first step before any RF goes out); default is self.transmit."""
        uhd, u, rx = self._uhd, self._usrp, self._rx
        p = self.params
        n = p.n_samples
        tx_on = self.transmit if transmit is None else bool(transmit)
        rx_errors: list[str] = []
        tx_errors: list[str] = []
        cpi_s = n / p.fs_hz
        # --- everything prepared and checked before any RF can start ---------------
        if self._tx_faulted:
            raise RuntimeError(f"transmitter faulted: {self._tx_faulted}. Recovery: close() once "
                               "the worker has exited (it clears the fault), then open() and "
                               "configure() again, or use a new UsrpRadar instance.")
        if tx_on:
            if self._tx is None or self._waveform is None:
                raise RuntimeError("configure() has not been called")
            if self._tx_thread is not None and self._tx_thread.is_alive():
                raise RuntimeError("a transmit burst is still in flight")
            self._validate_gains()
            self._validate_waveform(self._waveform)
        buf = np.zeros((1, n), dtype=np.complex64)
        md = uhd.types.RXMetadata()
        spb = self._chunk_samples(int(rx.get_max_num_samps()))   # recv fills a multi-packet buffer
        cmd = uhd.types.StreamCMD(uhd.types.StreamMode.num_done)
        cmd.num_samps, cmd.stream_now = n, False
        got = 0
        t_wall = time.time()
        try:
            t0 = u.get_time_now().get_real_secs() + self.lead_time_s
            cmd.time_spec = uhd.types.TimeSpec(t0)
            if tx_on:
                u.set_tx_gain(self.tx_gain_db, self.tx_channel)    # arm: gain applied only now
                actual = self._check_device_tx_gain()               # read back, refuse if over the cap
                self.info["tx_gain_db"] = actual
                self._tx_abort.clear()
                self._tx_done.clear()
                self._tx_thread = threading.Thread(target=self._tx_worker, args=(t0, tx_errors),
                                                   daemon=True)
                self._tx_thread.start()
            rx.issue_stream_cmd(cmd)
            while got < n:
                k = rx.recv(buf[:, got:got + spb], md, self.lead_time_s + 1.0 + cpi_s)
                if md.error_code != uhd.types.RXMetadataErrorCode.none:
                    rx_errors.append(f"rx {md.strerror()}")
                    if md.error_code == uhd.types.RXMetadataErrorCode.timeout:
                        if tx_on:
                            self._abort_tx("rx timeout")
                        break
                if k <= 0:
                    rx_errors.append("rx zero-length receive")
                    if tx_on:
                        self._abort_tx("rx returned no samples")
                    break
                got += k
            if tx_on and self._tx_thread is not None:
                self._tx_thread.join(timeout=self.lead_time_s + cpi_s + 5.0)
                if self._tx_thread.is_alive():
                    tx_errors.append("tx thread did not finish")
                    self._abort_tx("tx thread overran")
                else:
                    self._tx_thread = None
                    if not self._tx_done.is_set() and not tx_errors:
                        # Belt and braces: a worker that ended without reporting completion
                        # is a failure even if it recorded nothing.
                        tx_errors.append("tx worker ended without completing the burst")
                    self._drain_tx_async(tx_errors, timeout_s=1.0)
            if tx_on and tx_errors:
                # Any TX problem (short send, exception, abort, underflow/late reported by the
                # device, overrun) fails the CPI: attenuate, and never return a frame.
                self._abort_tx("tx errors: " + "; ".join(tx_errors))
                raise RuntimeError(f"transmit failed: {tx_errors}; rx_errors={rx_errors}")
        except BaseException:
            if tx_on:
                self._abort_tx("exception during capture")   # stop the burst, then re-raise
            raise
        if got < n:
            raise RuntimeError(f"short CPI: {got}/{n} samples; rx_errors={rx_errors}; "
                               f"tx_errors={tx_errors}")
        x = buf[0].copy()
        peak = float(np.abs(x).max())
        if peak > CLIP_FRACTION:
            log.warning("RX peak %.2f of full scale: reduce rx_gain_db or tx_gain_db "
                        "(direct can-to-can coupling is saturating the receiver)", peak)
        return Frame(samples=x, params=p, timestamp=t_wall, seq=self._next_seq(),
                     meta={"raw": x, "signedness": "complex64", "signal_mode": p.waveform,
                           "idn": self.info.get("model", ""), "sim": False,
                           "rx_errors": rx_errors, "tx_errors": tx_errors,
                           "device_time_s": t0, "timestamp_source": "host_clock",
                           "rx_peak": peak, "rx_clipping": peak > CLIP_FRACTION,
                           "rx_rms": float(np.abs(x).std()),
                           "capture_s": time.time() - t_wall,
                           "tx_enabled": tx_on, "tx_gain": self.tx_gain_db if tx_on else None,
                           "tx_waveform": self.tx_waveform if tx_on else None,
                           "rx_gain": self.rx_gain_db,
                           "zero_range_offset": self.zero_range_offset,
                           "device": dict(self.info)})

def _main(argv=None) -> int:                   # python -m phalanx.hardware.usrp  (first contact)
    import argparse
    parser = argparse.ArgumentParser(
        description="B210 first contact. Receive-only unless --tx is given: one RX-only CPI "
                    "(transmitter silent), then with --tx one TX+RX CPI at the default gain, "
                    "and (plfm) the measured zero-range offset. Read "
                    "docs/usrp_damage_review_2026-11-05.md before using --tx.")
    parser.add_argument("--serial", default="")
    parser.add_argument("--rate", type=float, default=4e6, help="sample rate, S/s (default 4e6)")
    parser.add_argument("--bandwidth", type=float, default=3e6, help="chirp bandwidth, Hz (default 3e6)")
    parser.add_argument("--tx-gain", type=float, default=40.0)
    parser.add_argument("--rx-gain", type=float, default=10.0)
    parser.add_argument("--max-tx-gain", type=float, default=60.0)
    parser.add_argument("--tx", action="store_true",
                        help="ARM THE TRANSMITTER. Without this flag the command is receive-only.")
    parser.add_argument("--waveform", choices=["tone", "plfm"], default="tone",
                        help="tone (default: bring-up) or plfm (the radar chirps)")
    args = parser.parse_args(argv)
    if args.max_tx_gain > ABSOLUTE_TX_GAIN_CEILING_DB:
        parser.error(f"--max-tx-gain above {ABSOLUTE_TX_GAIN_CEILING_DB} dB is not available from "
                     "this command")
    logging.basicConfig(level=logging.INFO)
    try:
        import uhd
        print("UHD", uhd.get_version_string())
        print("devices:", uhd.find("type=b200"))
    except ImportError as e:
        print("UHD python bindings not importable:", e)
        return 1
    params = first_contact_params(fs_hz=args.rate, bandwidth_hz=args.bandwidth)
    print(params.summary())
    with UsrpRadar(params, serial=args.serial, tx_gain_db=args.tx_gain, rx_gain_db=args.rx_gain,
                   max_tx_gain_db=args.max_tx_gain, transmit=args.tx,
                   tx_waveform=args.waveform, num_send_frames=BRINGUP_NUM_SEND_FRAMES) as r:
        r.configure()
        print({k: v for k, v in r.info.items() if k != "pp"})
        quiet = r.capture(transmit=False)
        print(f"TX off : rms={quiet.meta['rx_rms']:.5f}  peak={quiet.meta['rx_peak']:.4f}  "
              f"rx_errors={quiet.meta['rx_errors']}  {quiet.meta['capture_s']*1e3:.0f} ms")
        if not args.tx:
            print("transmitter not armed (no --tx): done.")
            return 0
        f = r.capture(transmit=True)
        print(f"TX on  : rms={f.meta['rx_rms']:.5f}  peak={f.meta['rx_peak']:.4f} (keep < {CLIP_FRACTION})  "
              f"rx_errors={f.meta['rx_errors']}  tx_errors={f.meta['tx_errors']}  "
              f"{f.meta['capture_s']*1e3:.0f} ms")
        if args.waveform == "plfm":
            k, lvl = estimate_zero_range_offset(f.samples, params)
            print(f"leakage: bin {k} of {params.n_fast} ({k/params.fs_hz*1e6:.2f} us after the TX grid), "
                  f"{lvl:.1f} dB above the median range bin → use ramp_offset={k}")
        else:
            spec = np.abs(np.fft.fft(f.samples[:min(len(f.samples), 1 << 16)]))
            fbin = np.fft.fftfreq(len(spec), 1 / params.fs_hz)[int(np.argmax(spec))]
            print(f"tone   : strongest received line at {fbin/1e3:+.1f} kHz from fc "
                  f"(sent {TONE_OFFSET_HZ/1e3:+.0f} kHz)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
