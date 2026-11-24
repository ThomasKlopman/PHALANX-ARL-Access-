"""Continuous, receive-only B210 source. No transmitter is created or enabled.

Each capture owns its samples. A reported RX error or device-time gap stops
the run; a broken stream is never silently stitched into a valid capture.
"""
from dataclasses import replace
import importlib
import time

import numpy as np

from .base import RadarSource
from ..frame import Frame
from ..params import RadarParams


# Highest rate at which a GUI recording has been verified end to end on the team
# laptop (22 Oct 2026: 228 blocks, 22.8 s, verify_session PASS). Raise this only
# after a longer run at the new rate passes verify_session.
TESTED_RECORD_RATE_SPS = 1e6

# USB receive buffering on the host side. UHD's B200 default is 16 transfers of
# 8192 bytes = 128 KB = 32k sc16 samples, i.e. only ~32 ms of slack at 1 MS/s:
# any host stall longer than that (GC, disk flush, power management) is reported
# as ERROR_CODE_OVERFLOW and, by design, ends the run. 512 frames = 4 MB ≈ 1 s of
# slack at 1 MS/s (≈130 ms at 30.72 MS/s). Seen on 5 Nov 2026: a run overflowed
# at ~95k samples into a block after 30+ s of clean streaming on Brandon's laptop.
DEFAULT_NUM_RECV_FRAMES = 512


def b210_rx_params(fs_hz=1e6, fc_hz=2.45e9, n_samples=100_000):
    if not np.isfinite(fs_hz) or fs_hz <= 0 or n_samples < 1:
        raise ValueError("Sample rate and block length must be positive")
    return RadarParams(f_start_hz=fc_hz, f_stop_hz=fc_hz, waveform="iq",
                       sweep="cw", fs_hz=fs_hz, n_samples=int(n_samples))


class B210ReceiveRadar(RadarSource):
    def __init__(self, params=None, serial="", gain_db=10.0,
                 channel=0, antenna="RX2", lead_time_s=0.5,
                 num_recv_frames=DEFAULT_NUM_RECV_FRAMES):
        super().__init__(params or b210_rx_params())
        self.serial, self.gain_db = serial.strip(), gain_db
        self.num_recv_frames = int(num_recv_frames)
        self.channel, self.antenna = channel, antenna
        self.lead_time_s = lead_time_s
        self._uhd = self._radio = self._rx = None
        self._running = False
        self._expected_time = None
        self.info = {}
        self.rx_errors = []

    def open(self):
        self._uhd = importlib.import_module("uhd")
        args = "type=b200" + (f",serial={self.serial}" if self.serial else "")
        if self.num_recv_frames > 0:
            args += f",num_recv_frames={self.num_recv_frames}"
        self._device_args = args
        self._radio = self._uhd.usrp.MultiUSRP(args)
        if "B210" not in self._radio.get_mboard_name().upper():
            self._radio = None
            raise ValueError("B210 receive mode requires a USRP B210")
        return self

    def configure(self, params=None):
        if params is not None:
            self.params = params
        if self.params.waveform != "iq":
            raise ValueError("B210 RX monitor requires raw I/Q parameters")
        u, ch, p = self._radio, self.channel, self.params
        u.set_rx_rate(p.fs_hz, ch)
        u.set_rx_freq(p.fc_hz, ch)
        u.set_rx_gain(self.gain_db, ch)
        u.set_rx_antenna(self.antenna, ch)
        rate, freq = u.get_rx_rate(ch), u.get_rx_freq(ch)
        # Readbacks govern sample timing and displayed axes, including coerced rates.
        self.params = replace(p, fs_hz=rate, f_start_hz=freq, f_stop_hz=freq)
        args = self._uhd.usrp.StreamArgs("fc32", "sc16")
        args.channels = [ch]
        self._rx = u.get_rx_stream(args)
        self.info = {"model": u.get_mboard_name(), "serial": self.serial,
                     "rx_rate_hz": rate, "rx_frequency_hz": freq,
                     "rx_gain_db": u.get_rx_gain(ch),
                     "rx_bandwidth_hz": u.get_rx_bandwidth(ch),
                     "antenna": u.get_rx_antenna(ch), "channel": ch,
                     "clock_source": u.get_clock_source(0),
                     "time_source": u.get_time_source(0),
                     "uhd_version": self._uhd.get_version_string(),
                     "device_args": self._device_args}
        if not self.serial:
            self.info["serial"] = str(u.get_usrp_rx_info(ch).get("mboard_serial", ""))

    def start(self):
        cmd = self._uhd.types.StreamCMD(self._uhd.types.StreamMode.start_cont)
        cmd.stream_now = False
        cmd.time_spec = self._uhd.types.TimeSpec(
            self._radio.get_time_now().get_real_secs() + self.lead_time_s)
        self._expected_time = None
        self.rx_errors = []
        self._rx.issue_stream_cmd(cmd)
        self._running = True

    def stop(self):
        if self._running and self._rx is not None:
            self._rx.issue_stream_cmd(
                self._uhd.types.StreamCMD(self._uhd.types.StreamMode.stop_cont))
        self._running = False

    def close(self):
        try:
            self.stop()
        finally:
            self._rx = self._radio = None

    def _fail(self, detail):
        self.rx_errors.append(detail)
        raise RuntimeError(detail)

    def capture(self):
        if not self._running:
            raise RuntimeError("B210 receive stream is not running")
        p = self.params
        x = np.empty((1, p.n_samples), dtype=np.complex64)
        md = self._uhd.types.RXMetadata()
        got, first_time = 0, None
        host_started = time.time()
        while got < p.n_samples:
            n = self._rx.recv(x[:, got:], md, max(2.0, 2*p.n_samples/p.fs_hz))
            if md.error_code != self._uhd.types.RXMetadataErrorCode.none or md.out_of_sequence:
                self._fail(f"RX discontinuity after {got} samples in block: "
                           f"{md.strerror()}; out_of_sequence={md.out_of_sequence}")
            if n <= 0 or n > p.n_samples - got:
                self._fail(f"Invalid RX count: {n}")
            if not md.has_time_spec:
                self._fail("RX packet has no device timestamp; continuity cannot be verified")
            actual_time = md.time_spec.get_real_secs()
            if not np.isfinite(actual_time):
                self._fail("Nonfinite RX device timestamp")
            if self._expected_time is not None:
                delta_samples = (actual_time - self._expected_time) * p.fs_hz
                if abs(delta_samples) > 0.5:
                    self._fail(f"RX device-time gap/overlap: {delta_samples:.3f} samples")
            if first_time is None:
                first_time = actual_time
            self._expected_time = actual_time + n/p.fs_hz
            got += n
        samples = x[0]  # x is a fresh allocation for every block, never reused.
        if not np.isfinite(samples).all():
            self._fail("Nonfinite RX sample")
        return Frame(samples=samples, params=p, timestamp=host_started,
                     seq=self._next_seq(), meta={
                         "raw": samples, "signal_mode": "raw_iq", "sim": False,
                         "signedness": "complex64", "idn": self.info["model"],
                         "device_time_s": first_time, "timestamp_source": "host_clock",
                         "rx_errors": [], "continuity": "device_time_checked",
                         "device": dict(self.info)})
