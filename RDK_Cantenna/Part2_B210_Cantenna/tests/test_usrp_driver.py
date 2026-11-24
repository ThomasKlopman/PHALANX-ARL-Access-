"""UsrpRadar (B210 pulsed-LFM TX+RX) against a mocked UHD: device arguments, analog
bandwidth, TX-off capture, gain cap, async-message timing, zero-range estimate."""
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest

from phalanx.hardware.usrp import (UsrpRadar, build_cpi_waveform, build_device_args,
                                   estimate_zero_range_offset, first_contact_params)

LEAK_DELAY = 37          # samples the fake hardware delays TX→RX (fixed latency + ~0 m path)


class FakeTime:
    def __init__(self, value): self.value = value
    def get_real_secs(self): return self.value


class FakeTX:
    def __init__(self, radio):
        self.radio, self.sent, self.async_polls = radio, [], 0
        self.short_send = False          # report fewer samples accepted than offered
        self.timeouts = []               # timeout passed to every send()
        self.block = None                # threading.Event: send() blocks until it is set
    def get_max_num_samps(self): return 1000
    def send(self, chunk, md, timeout=0.1):
        self.timeouts.append(timeout)
        if self.block is not None:
            self.block.wait()
        self.sent.append((chunk.copy(), md.start_of_burst, md.end_of_burst, md.has_time_spec))
        self.radio.events.append("send")
        return chunk.shape[1] - (1 if self.short_send and chunk.shape[1] else 0)
    def recv_async_msg(self, amd, timeout):
        self.async_polls += 1
        self.radio.events.append("drain")
        return False


class FakeRX:
    """Returns the fake TX waveform delayed by LEAK_DELAY plus a little noise, so the
    receive side sees exactly the leakage the real board would (if TX was sent)."""
    def __init__(self, radio): self.radio, self.commands = radio, []
    def get_max_num_samps(self): return 1000
    def issue_stream_cmd(self, cmd):
        self.commands.append(cmd)
        self.radio.n_samples, self.radio.stream = cmd.num_samps, None
    def recv(self, buf, md, timeout):
        self.radio.events.append("recv")
        n = buf.shape[1]
        if self.radio.stream is None:
            deadline = time.time() + 2.0        # let the driver's TX thread finish its burst
            while self.radio.expect_tx and not any(s[2] for s in self.radio.tx.sent):
                assert time.time() < deadline, "TX burst never completed"
                time.sleep(0.001)
            rng = np.random.default_rng(1)
            total = self.radio.n_samples
            x = 1e-3 * (rng.standard_normal(total) + 1j * rng.standard_normal(total))
            if self.radio.tx.sent:
                wf = np.concatenate([s[0][0] for s in self.radio.tx.sent])
                x[LEAK_DELAY:LEAK_DELAY + len(wf)] += 0.3 * wf[:total - LEAK_DELAY]
            self.radio.stream, self.radio.pos = x.astype(np.complex64), 0
        pos = self.radio.pos
        if self.radio.rx_timeout:
            md.error_code = 1
            md.strerror = lambda: "timeout"
            return 0
        buf[0, :n] = self.radio.stream[pos:pos + n]
        self.radio.pos += n
        md.error_code = 0
        md.strerror = lambda: "none"
        return n


class FakeRadio:
    def __init__(self):
        self.tx, self.rx = FakeTX(self), FakeRX(self)
        self.set = {}
        self.events = []
        self.stream = None
        self.n_samples = 0
        self.expect_tx = True
        self.frozen = set()          # settings the fake device refuses to change (coercion test)
        self.rx_timeout = False
    def get_mboard_name(self): return "B210"
    def __getattr__(self, name):           # set_x_y(value, ch) → stored; get_x_y(ch) → stored
        if name.startswith("set_"):
            def setter(value, *ch):
                if name[4:] not in self.frozen:
                    self.set[name[4:]] = getattr(value, "value", value)
            return setter
        if name.startswith("get_"):
            key = name[4:]
            if key == "time_now": return lambda: FakeTime(10.0)
            if key == "usrp_rx_info": return lambda ch: {"mboard_serial": "fake326"}
            if key == "rx_sensor": return lambda n, ch: SimpleNamespace(to_bool=lambda: True)
            if key == "clock_source" or key == "time_source": return lambda b: "internal"
            if key == "tx_stream": return lambda st: self.tx
            if key == "rx_stream": return lambda st: self.rx
            return lambda *ch: self.set.get(key, 0.0)
        raise AttributeError(name)


@pytest.fixture
def fake_uhd(monkeypatch):
    radio = FakeRadio()
    fake = SimpleNamespace(
        usrp=SimpleNamespace(MultiUSRP=lambda args: radio.__dict__.setdefault("args", args) and radio,
                             StreamArgs=lambda cpu, otw: SimpleNamespace(cpu=cpu, otw=otw)),
        types=SimpleNamespace(
            StreamMode=SimpleNamespace(num_done="num_done"),
            StreamCMD=lambda mode: SimpleNamespace(mode=mode),
            TimeSpec=FakeTime, TuneRequest=lambda f: SimpleNamespace(value=f),
            RXMetadata=lambda: SimpleNamespace(), TXMetadata=lambda: SimpleNamespace(),
            TXAsyncMetadata=lambda: SimpleNamespace(event_code="ack"),
            TXMetadataEventCode=SimpleNamespace(burst_ack="ack"),
            RXMetadataErrorCode=SimpleNamespace(none=0, timeout=1)),
        get_version_string=lambda: "fake UHD", find=lambda a: [])
    monkeypatch.setitem(sys.modules, "uhd", fake)
    monkeypatch.setattr(UsrpRadar, "chunk_time_s", 0.0)    # one packet per call: exercises the loops
    return radio


def small_params():
    # 4 MS/s, 4 kHz PRF → 1000-sample PRIs, 0.1 ms (400-sample) pulses, 4 pulses = 4000 samples
    return first_contact_params(fs_hz=4e6, bandwidth_hz=3e6, prf_hz=4e3, n_pulses=4,
                                pulse_width_s=0.1e-3)


def test_pulse_longer_than_pri_is_refused():
    with pytest.raises(ValueError, match="longer than the PRI"):
        build_cpi_waveform(first_contact_params(prf_hz=4e3))     # 0.5 ms pulse, 0.25 ms PRI


def test_device_args_name_the_board_and_usb_buffering():
    assert build_device_args() == "type=b200,num_recv_frames=512,num_send_frames=512"
    assert build_device_args("326C061", 64, 0, "master_clock_rate=32e6") == \
        "type=b200,serial=326C061,num_recv_frames=64,master_clock_rate=32e6"
    r = UsrpRadar(small_params(), serial=" 326C061 ")
    assert r.device_args == "type=b200,serial=326C061,num_recv_frames=512,num_send_frames=512"


def test_gain_cap_at_construction_and_at_runtime(fake_uhd):
    with pytest.raises(ValueError, match="the cap"):
        UsrpRadar(small_params(), tx_gain_db=65)
    with UsrpRadar(small_params(), tx_gain_db=40, max_tx_gain_db=50) as r:
        r.configure()
        assert r.set_tx_gain(45) == 45
        with pytest.raises(ValueError):
            r.set_tx_gain(55)
        assert r.tx_gain_db == 45        # unchanged by the refused request


def test_configure_sets_analog_bandwidth_and_readbacks(fake_uhd):
    p = small_params()
    with UsrpRadar(p) as r:
        r.configure()
        assert fake_uhd.set["tx_bandwidth"] == pytest.approx(1.2 * 3e6)
        assert fake_uhd.set["rx_bandwidth"] == pytest.approx(1.2 * 3e6)
        assert fake_uhd.set["rx_freq"] == pytest.approx(p.fc_hz)
        assert fake_uhd.args.startswith("type=b200,")
        for key in ("model", "serial", "rx_rate_hz", "rx_frequency_hz", "rx_gain_db",
                    "rx_bandwidth_hz", "antenna", "clock_source", "uhd_version", "device_args"):
            assert key in r.info                      # same keys readback_sessions.py prints
        assert r.info["serial"] == "fake326"
    with UsrpRadar(p, analog_bandwidth_hz=2e6) as r:
        r.configure()
        assert fake_uhd.set["rx_bandwidth"] == 2e6


def test_transmit_is_off_unless_asked(fake_uhd):
    fake_uhd.expect_tx = False
    with UsrpRadar(small_params()) as r:              # default transmit=False
        r.configure()
        r.capture()
        assert fake_uhd.tx.sent == []


def test_absolute_ceiling_amplitude_and_waveform_guards():
    with pytest.raises(ValueError, match="ceiling"):
        UsrpRadar(small_params(), max_tx_gain_db=80)
    r = UsrpRadar(small_params(), max_tx_gain_db=80, unlock_full_power=True)
    assert r.max_tx_gain_db == 80
    with pytest.raises(ValueError, match="tx_amplitude"):
        UsrpRadar(small_params(), tx_amplitude=1.5)
    with pytest.raises(ValueError, match="tx_waveform"):
        UsrpRadar(small_params(), tx_waveform="sawtooth")


def test_tone_waveform_is_constant_envelope(fake_uhd):
    p = small_params()
    with UsrpRadar(p, transmit=True, tx_waveform="tone") as r:
        r.configure()
        f = r.capture()
    wf = np.concatenate([s[0][0] for s in fake_uhd.tx.sent])
    np.testing.assert_allclose(np.abs(wf), 0.5, atol=1e-6)
    assert len(wf) == p.n_samples and f.meta["tx_waveform"] == "tone"


def test_rx_failure_aborts_the_burst_and_close_zeroes_tx_gain(fake_uhd):
    p = small_params()
    r = UsrpRadar(p, transmit=True).open()
    r.configure()
    original = fake_uhd.rx.recv
    def failing_recv(buf, md, timeout):
        raise RuntimeError("usb went away")
    fake_uhd.rx.recv = failing_recv
    with pytest.raises(RuntimeError, match="usb went away"):
        r.capture()
    assert r._tx_abort.is_set()
    fake_uhd.rx.recv = original
    r.close()
    assert fake_uhd.set["tx_gain"] == 0.0            # RF left at maximum attenuation


def test_capture_refuses_if_device_gain_readback_exceeds_cap(fake_uhd):
    p = small_params()
    with UsrpRadar(p, transmit=True, max_tx_gain_db=50, tx_gain_db=40) as r:
        r.configure()
        fake_uhd.set["tx_gain"] = 80.0                 # device holds 80 dB and refuses our write
        fake_uhd.frozen.add("tx_gain")
        with pytest.raises(ValueError, match="read-back"):
            r.capture()
        assert fake_uhd.tx.sent == []
        assert r._tx_abort.is_set()


def test_tx_gain_is_zero_until_a_burst_is_armed(fake_uhd):
    p = small_params()
    with UsrpRadar(p, transmit=True, tx_gain_db=40) as r:
        assert fake_uhd.set["tx_gain"] == 0.0          # open(): cold start
        r.configure()
        assert fake_uhd.set["tx_gain"] == 0.0          # configure(): still attenuated
        r.set_tx_gain(45)
        assert fake_uhd.set["tx_gain"] == 0.0          # set_tx_gain(): nothing written yet
        r.capture()
        assert fake_uhd.set["tx_gain"] == 45.0         # applied only for the burst
        r.stop()
        assert fake_uhd.set["tx_gain"] == 0.0          # stop(): RF off again


def test_gain_validation_happens_before_any_hardware_write(fake_uhd):
    with pytest.raises(ValueError, match="RX gain range"):
        UsrpRadar(small_params(), rx_gain_db=90)              # rejected at construction
    with pytest.raises(ValueError, match="finite"):
        UsrpRadar(small_params(), tx_gain_db=float("nan"))
    r = UsrpRadar(small_params(), max_tx_gain_db=50).open()
    r.rx_gain_db = 90                                         # corrupted after construction
    with pytest.raises(ValueError, match="RX gain range"):
        r.configure()
    assert "rx_rate" not in fake_uhd.set and "tx_freq" not in fake_uhd.set   # nothing written
    r.close()


def test_rx_timeout_aborts_tx_immediately(fake_uhd):
    p = small_params()
    with UsrpRadar(p, transmit=True) as r:
        r.configure()
        fake_uhd.rx_timeout = True
        with pytest.raises(RuntimeError, match="short CPI"):
            r.capture()
        assert r._tx_abort.is_set()
        assert fake_uhd.set["tx_gain"] == 0.0
        assert r._tx_thread is None                            # joined, not left running


def test_waveform_validation_blocks_bad_samples(fake_uhd):
    p = small_params()
    with UsrpRadar(p, transmit=True) as r:
        r.configure()
        r._waveform[10] = np.nan
        with pytest.raises(ValueError, match="NaN"):
            r.capture()
        assert fake_uhd.tx.sent == []


def test_tx_short_send_stops_the_worker_at_once_and_attenuates(fake_uhd):
    p = small_params()
    fake_uhd.expect_tx = False                   # the burst is cut short; RX must not wait for EOB
    with UsrpRadar(p, transmit=True) as r:
        r.configure()
        fake_uhd.tx.short_send = True
        with pytest.raises(RuntimeError, match="transmit failed.*short send"):
            r.capture()
        assert fake_uhd.set["tx_gain"] == 0.0 and r._tx_abort.is_set()
        nonempty = [s for s in fake_uhd.tx.sent if s[0].shape[1] > 0]
        assert len(nonempty) == 1                # first chunk only; no further waveform chunks
        assert fake_uhd.tx.sent[-1][2] is True   # burst closed with end_of_burst
        # gain was zeroed by the worker itself, before the main thread's cleanup ran
        assert fake_uhd.events.index("send") < len(fake_uhd.events)


def test_stuck_tx_worker_is_kept_blocks_captures_and_close_refuses_to_release(fake_uhd):
    import threading
    p = small_params()
    gate = threading.Event()
    fake_uhd.tx.block = gate                     # the fake device never accepts the send
    fake_uhd.expect_tx = False                   # RX must not wait for a burst that never comes
    r = UsrpRadar(p, transmit=True).open()
    r.configure()
    r.close_timeout_s = r.abort_join_timeout_s = 0.1
    fake_uhd.rx_timeout = True                   # RX times out → abort → worker will not exit
    with pytest.raises(RuntimeError):
        r.capture()
    assert r._tx_thread is not None and r._tx_thread.is_alive()   # reference preserved
    assert r._tx_faulted
    with pytest.raises(RuntimeError, match="faulted"):
        r.capture(transmit=False)                # no further captures, even RX-only
    with pytest.raises(RuntimeError):
        r.close()                                # handles NOT released under a live worker
    assert r._usrp is not None and r._tx is not None
    gate.set()                                   # device recovers: worker exits
    r._tx_thread.join(timeout=2.0)
    assert not r._tx_thread.is_alive()
    r.close_timeout_s = 2.0
    r.close()                                    # worker verified dead → release AND clear fault
    assert r._usrp is None and r._tx_faulted is None
    r.open(); r.configure()                      # documented recovery path works
    fake_uhd.tx.block = None
    fake_uhd.rx_timeout = False
    fake_uhd.expect_tx = True
    r.capture()
    r.close()


@pytest.mark.parametrize("where", ["metadata", "max_num_samps", "zero_spb"])
def test_worker_setup_failure_fails_the_capture_and_attenuates(fake_uhd, where):
    """Thomas, fifth pass: an exception in the worker's setup (before any send) must not
    end in a returned frame with empty tx_errors."""
    p = small_params()
    fake_uhd.expect_tx = False
    with UsrpRadar(p, transmit=True) as r:
        r.configure()
        def boom(*a, **k): raise RuntimeError("streamer setup failed")
        if where == "metadata":
            sys.modules["uhd"].types.TXMetadata = boom
        elif where == "max_num_samps":
            fake_uhd.tx.get_max_num_samps = boom
        else:
            fake_uhd.tx.get_max_num_samps = lambda: 0
        with pytest.raises(RuntimeError, match="transmit failed"):
            r.capture()
        assert not any(s[0].shape[1] for s in fake_uhd.tx.sent)   # no waveform samples went out
        assert fake_uhd.set["tx_gain"] == 0.0 and r._tx_abort.is_set()
        assert not r._tx_done.is_set()


def test_fmcw_waveform_needs_matching_parameters(fake_uhd):
    from phalanx.hardware.b210_fmcw import b210_fmcw_params, raw_params
    fm = raw_params(b210_fmcw_params(n_samples=80), 4e6)          # one 4 ms ramp: 16 000 samples
    with UsrpRadar(fm, transmit=True, tx_waveform="fmcw") as r:
        r.configure()
        f = r.capture()
        wf = np.concatenate([s[0][0] for s in fake_uhd.tx.sent])
        np.testing.assert_allclose(np.abs(wf), 0.5, atol=1e-5)     # continuous, constant envelope
        assert len(wf) == fm.n_samples and f.meta["signal_mode"] == "fmcw"
    with UsrpRadar(small_params(), tx_waveform="fmcw") as r:       # PLFM params, FMCW waveform
        with pytest.raises(ValueError, match="does not match"):
            r.configure()
    with UsrpRadar(fm, tx_waveform="plfm") as r:
        with pytest.raises(ValueError, match="does not match"):
            r.configure()


def test_chunks_are_about_one_millisecond_of_whole_packets(fake_uhd, monkeypatch):
    monkeypatch.setattr(UsrpRadar, "chunk_time_s", 1e-3)
    p = first_contact_params(fs_hz=4e6, bandwidth_hz=3e6, prf_hz=1e3, n_pulses=4)   # 16 000 samples
    with UsrpRadar(p, transmit=True) as r:
        r.configure()
        assert r._chunk_samples(1000) == 4000 and r._chunk_samples(2040) == 2040    # 4 MS/s
        r.capture()
        sizes = [s[0].shape[1] for s in fake_uhd.tx.sent]
        assert sizes == [4000, 4000, 4000, 4000]                  # fake packet size is 1000
        assert fake_uhd.tx.sent[0][1] and fake_uhd.tx.sent[-1][2]
    fast = first_contact_params(fs_hz=30.72e6, bandwidth_hz=24e6)
    assert UsrpRadar(fast)._chunk_samples(2040) == 2040 * 15      # ~1 ms at 30.72 MS/s


def test_send_timeout_outlasts_the_lead_time(fake_uhd):
    """First hardware run, 10 Nov 2026: UHD's default 0.1 s send timeout expired while the
    timed burst was still waiting for its start time → 'tx short send 0/2040'."""
    with UsrpRadar(small_params(), transmit=True, lead_time_s=0.2) as r:
        r.configure()
        r.capture()
    assert fake_uhd.tx.timeouts and min(fake_uhd.tx.timeouts) >= 0.2 + 1.0


def test_tx_done_is_set_only_by_a_clean_burst(fake_uhd):
    with UsrpRadar(small_params(), transmit=True) as r:
        r.configure()
        r.capture()
        assert r._tx_done.is_set()


def test_tx_off_capture_sends_nothing(fake_uhd):
    p = small_params()
    fake_uhd.expect_tx = False
    with UsrpRadar(p, transmit=True) as r:
        r.configure()
        f = r.capture(transmit=False)
        assert fake_uhd.tx.sent == []
        assert f.meta["tx_enabled"] is False and f.meta["tx_errors"] == []
        assert len(f) == p.n_samples and f.meta["rx_peak"] < 0.02
        assert fake_uhd.rx.commands[0].mode == "num_done" and fake_uhd.rx.commands[0].stream_now is False


def test_tx_on_capture_timed_burst_and_async_drain_after_receive(fake_uhd):
    p = small_params()
    with UsrpRadar(p, tx_amplitude=0.5, transmit=True) as r:
        r.configure()
        f = r.capture()
        sent = fake_uhd.tx.sent
        assert sent[0][1] is True and sent[0][3] is True          # start_of_burst with time_spec
        assert all(s[1] is False and s[3] is False for s in sent[1:])
        assert sent[-1][2] is True and not any(s[2] for s in sent[:-1])   # one end_of_burst
        wf = np.concatenate([s[0][0] for s in sent])
        np.testing.assert_allclose(wf, 0.5 * build_cpi_waveform(p), atol=1e-6)
        assert fake_uhd.tx.async_polls >= 1
        ev = fake_uhd.events
        assert ev.index("drain") > max(i for i, e in enumerate(ev) if e == "recv")  # drained after RX
        assert f.meta["tx_enabled"] is True and f.meta["rx_errors"] == [] and f.meta["tx_errors"] == []
        assert f.meta["signal_mode"] == "plfm" and f.meta["device"]["device_args"] == r.device_args


def test_zero_range_offset_finds_the_leakage(fake_uhd):
    p = small_params()
    with UsrpRadar(p, transmit=True) as r:
        r.configure()
        f = r.capture()
    k, level_db = estimate_zero_range_offset(f.samples, p)
    assert k == LEAK_DELAY
    assert level_db > 20
