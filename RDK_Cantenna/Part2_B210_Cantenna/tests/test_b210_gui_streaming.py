"""Regression checks: acquisition/recording are independent of drawing speed."""
import json
import os
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest

from phalanx.frame import Frame
from phalanx.capture import load_capture, save_capture
from phalanx.hardware.base import RadarSource
from phalanx.hardware.b210 import B210ReceiveRadar, b210_rx_params
from phalanx.hardware.recorded import RecordedRadar
from phalanx.session import SessionRecorder, session_files


class FakeTime:
    def __init__(self, value):
        self.value = value
    def get_real_secs(self):
        return self.value


class FakeRX:
    def __init__(self, rate):
        self.rate, self.position = rate, 0
        self.commands = []
        self.overflow = self.gap = False
    def issue_stream_cmd(self, command):
        self.commands.append(command)
    def recv(self, buffer, md, timeout):
        n = min(37, buffer.shape[1])  # Force multiple partial receives per block.
        pos = self.position
        buffer[0, :n] = np.arange(pos, pos+n, dtype=np.float32)*(1-1j)
        md.error_code = 1 if self.overflow else 0
        md.out_of_sequence = False
        md.has_time_spec = True
        md.time_spec = FakeTime(1 + (pos + (3 if self.gap else 0))/self.rate)
        md.strerror = lambda: "overflow" if self.overflow else "none"
        self.position += n
        return n


class FakeRadio:
    # Deliberately has no TX API: receive-only code must never request a TX stream.
    def __init__(self):
        self.rx = FakeRX(1e6)
        self.requested = None
    def get_mboard_name(self): return "B210"
    def set_rx_rate(self, value, ch): self.requested = value
    def set_rx_freq(self, value, ch): self.freq = value
    def set_rx_gain(self, value, ch): self.gain = value
    def set_rx_antenna(self, value, ch): self.antenna = value
    def get_rx_rate(self, ch): return 1e6
    def get_rx_freq(self, ch): return self.freq
    def get_rx_gain(self, ch): return self.gain
    def get_rx_antenna(self, ch): return self.antenna
    def get_rx_bandwidth(self, ch): return 1.2e6
    def get_clock_source(self, board): return "internal"
    def get_time_source(self, board): return "internal"
    def get_time_now(self): return FakeTime(0.5)
    def get_usrp_rx_info(self, ch): return {"mboard_serial": "fake123"}
    def get_rx_stream(self, args):
        assert args.cpu == "fc32" and args.otw == "sc16" and args.channels == [0]
        return self.rx


@pytest.fixture
def fake_uhd(monkeypatch):
    radio = FakeRadio()
    fake = SimpleNamespace(
        usrp=SimpleNamespace(MultiUSRP=lambda args: radio,
            StreamArgs=lambda cpu, otw: SimpleNamespace(cpu=cpu, otw=otw)),
        types=SimpleNamespace(
            StreamMode=SimpleNamespace(start_cont="start", stop_cont="stop"),
            StreamCMD=lambda mode: SimpleNamespace(mode=mode),
            TimeSpec=FakeTime, RXMetadata=lambda: SimpleNamespace(),
            RXMetadataErrorCode=SimpleNamespace(none=0)),
        get_version_string=lambda: "fake UHD")
    monkeypatch.setitem(sys.modules, "uhd", fake)
    return radio


def test_b210_continuous_partial_receives_own_buffers_and_actual_readbacks(fake_uhd):
    src = B210ReceiveRadar(b210_rx_params(fs_hz=1.001e6, n_samples=100))
    with src:
        src.configure()
        assert src.params.fs_hz == 1e6
        src.start()
        first, second = src.capture(), src.capture()
        assert len(fake_uhd.rx.commands) == 1  # Continuous stream, no restart per block.
        assert fake_uhd.rx.commands[0].stream_now is False
        np.testing.assert_array_equal(first.samples, np.arange(100)*(1-1j))
        np.testing.assert_array_equal(second.samples, np.arange(100, 200)*(1-1j))
        assert not np.shares_memory(first.samples, second.samples)
        assert second.meta["device_time_s"] == pytest.approx(1.0001)
        assert first.meta["device"]["serial"] == "fake123"
    assert fake_uhd.rx.commands[-1].mode == "stop"


def test_b210_overflow_is_fatal(fake_uhd):
    with B210ReceiveRadar(b210_rx_params(n_samples=100)) as src:
        src.configure(); src.start()
        fake_uhd.rx.overflow = True
        with pytest.raises(RuntimeError, match="RX discontinuity"):
            src.capture()


def test_b210_timestamp_gap_is_fatal_even_without_error_flag(fake_uhd):
    with B210ReceiveRadar(b210_rx_params(n_samples=100)) as src:
        src.configure(); src.start()
        src.capture()
        fake_uhd.rx.gap = True
        with pytest.raises(RuntimeError, match="gap/overlap"):
            src.capture()


def make_frame(i, n=100):
    p = b210_rx_params(n_samples=n)
    samples = np.arange(n, dtype=np.float32).astype(np.complex64) + i*1j
    return Frame(samples, p, timestamp=100+i*0.1, seq=i,
                 meta={"raw": samples, "signal_mode": "raw_iq", "sim": False,
                       "device_time_s": i*0.0001, "rx_errors": [],
                       "device": {"rx_gain_db": 10.0, "serial": "fake123"}})


def test_session_preserves_every_block_bytes_metadata_and_buffer_ownership(tmp_path):
    folder = tmp_path/"record.session"
    recorder = SessionRecorder(folder, queue_blocks=64)
    expected = []
    for i in range(50):
        f = make_frame(i)
        expected.append(f.samples.tobytes())
        recorder.append(f)
        f.samples[:] = 0  # Producer may reuse/modify its original buffer.
    manifest = recorder.finish()
    files, info = session_files(manifest)
    assert info["saved_samples"] == 5000 and info["saved_frames"] == 50
    with RecordedRadar(manifest) as replay:
        replay.start()
        for i, f in enumerate(replay.frames()):
            assert f.samples.tobytes() == expected[i]
            assert f.meta["device_time_s"] == i*0.0001
            assert f.meta["device"]["serial"] == "fake123"
            assert f.meta["sim"] is False
    assert replay._frames is None  # Session replay does not preload all raw samples.


def test_recording_queue_full_fails_explicitly_and_session_is_invalid(tmp_path):
    entered, release = Event(), Event()
    class SlowRecorder(SessionRecorder):
        def _write_frame(self, frame):
            entered.set()
            assert release.wait(5)
            super()._write_frame(frame)
    recorder = SlowRecorder(tmp_path/"full.session", queue_blocks=1)
    try:
        recorder.append(make_frame(0))
        assert entered.wait(5)
        recorder.append(make_frame(1))
        with pytest.raises(RuntimeError, match="queue full"):
            recorder.append(make_frame(2))
    finally:
        release.set()
    with pytest.raises(RuntimeError, match="Recording invalid"):
        recorder.finish()
    with pytest.raises(ValueError, match="invalid"):
        session_files(recorder.folder)


def test_writer_disk_failure_is_reported_and_replay_rejected(tmp_path):
    class BrokenRecorder(SessionRecorder):
        def _write_frame(self, frame):
            raise OSError("disk full")
    recorder = BrokenRecorder(tmp_path/"broken.session")
    recorder.append(make_frame(0))
    with pytest.raises(RuntimeError, match="disk full"):
        recorder.finish()
    with pytest.raises(ValueError, match="invalid"):
        RecordedRadar(recorder.folder)


def test_incomplete_session_rejected(tmp_path):
    recorder = SessionRecorder(tmp_path/"active.session")
    with pytest.raises(ValueError, match="incomplete"):
        session_files(recorder.folder)
    recorder.finish()


def test_unknown_npz_schema_rejected(tmp_path):
    f=make_frame(0)
    path=save_capture(tmp_path/"future.npz", [f], f.params)
    with np.load(path, allow_pickle=False) as data:
        changed = {k: data[k] for k in data.files}
    changed["schema_version"] = 999
    np.savez(path, **changed)
    with pytest.raises(ValueError, match="Unsupported capture schema"):
        load_capture(path)


# GUI/worker tests are optional when running a pure hardware/core environment.
@pytest.fixture
def qt_app():
    pytest.importorskip("PyQt6")
    pytest.importorskip("pyqtgraph")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class FiniteIQ(RadarSource):
    def __init__(self, count=50, params=None):
        super().__init__(params or b210_rx_params(n_samples=100))
        self.count, self.i = count, 0
    def configure(self, params=None): pass
    def start(self): pass
    def stop(self): pass
    def capture(self):
        if self.i == self.count: raise StopIteration
        frame = make_frame(self.i, self.params.n_samples)
        frame.params = self.params
        self.i += 1
        return frame


def test_stalled_display_does_not_drop_acquisition_or_recording(qt_app, tmp_path):
    from phalanx.gui.app import AcquisitionWorker
    from phalanx.dsp.pipeline import ProcessorConfig
    source = FiniteIQ(50)
    worker = AcquisitionWorker(lambda: source, source.params, ProcessorConfig(),
                               tmp_path/"stalled.session", record_queue_blocks=64)
    worker.start()
    assert worker.wait(10_000)  # Deliberately do not pump the GUI event loop.
    assert worker.acquired_frames == worker.processed_frames == 50
    assert worker.recorder.saved_frames == 50
    assert worker.recorder.saved_samples == 5000
    assert worker.latest.published == 50
    assert worker.latest.consumed == 0
    assert worker.latest.superseded == 49
    result, newest = worker.latest.take()
    assert newest.seq == 49 and result is None
    with RecordedRadar(tmp_path/"stalled.session") as replay:
        replay.start()
        for i, frame in enumerate(replay.frames()):
            assert frame.samples.tobytes() == make_frame(i).samples.tobytes()


def test_live_gui_uses_iq_monitor_without_fake_detections(qt_app, tmp_path, monkeypatch):
    import time
    import phalanx.gui.app as gui
    monkeypatch.setattr(gui, "B210ReceiveRadar", lambda params, **kw: FiniteIQ(3, params))
    window = gui.MainWindow(repo_root=tmp_path)
    window._headless = True
    window.source_box.setCurrentIndex(3)
    window.start()
    until=time.monotonic()+10
    while window.state != "END OF FILE" and time.monotonic() < until:
        qt_app.processEvents()
        time.sleep(0.01)
    assert window.state == "END OF FILE"
    assert "Raw I/Q monitor" in window.readout.text()
    assert window.tabs.currentIndex() == 2
    assert window.table.rowCount() == 0
    assert window.worker.acquired_frames == 3
    window.close()


def test_replay_nondefault_params_ignores_gui_defaults(qt_app, tmp_path):
    import time
    from phalanx import SimulatedRadar, SimConfig, cantenna_fmcw
    from phalanx.gui.app import MainWindow
    p = cantenna_fmcw(ramp_time_ms=8)
    f = SimulatedRadar(p, SimConfig(seed=0)).capture()
    path = save_capture(tmp_path/"eight.npz", [f], p)
    window = MainWindow(repo_root=tmp_path)
    window._headless = True
    window.source_box.setCurrentIndex(1)
    window.file_edit.setText(str(path))
    window.start()
    until=time.monotonic()+10
    while window.state not in ("END OF FILE", "ERROR") and time.monotonic()<until:
        qt_app.processEvents(); time.sleep(0.01)
    assert window.state == "END OF FILE"
    window.close()


def test_source_failure_marks_session_invalid(qt_app, tmp_path):
    from phalanx.gui.app import AcquisitionWorker
    from phalanx.dsp.pipeline import ProcessorConfig
    class BrokenSource(FiniteIQ):
        def capture(self):
            if self.i == 2:
                raise RuntimeError("RX overflow: discontinuous stream")
            return super().capture()
    source = BrokenSource(10)
    worker = AcquisitionWorker(lambda: source, source.params, ProcessorConfig(),
                               tmp_path/"rx_error.session")
    worker.start()
    assert worker.wait(10_000)
    manifest=json.loads((tmp_path/"rx_error.session"/"session.json").read_text())
    assert manifest["state"] == "invalid"
    assert "overflow" in manifest["reason"]
    assert manifest["saved_frames"] == 2
    with pytest.raises(ValueError, match="invalid"):
        RecordedRadar(tmp_path/"rx_error.session")


def test_session_verifier_checks_samples_and_device_time(tmp_path):
    from phalanx.session import verify_session
    recorder = SessionRecorder(tmp_path/"verified.session")
    for i in range(3):
        recorder.append(make_frame(i))
    manifest = recorder.finish()
    checked = verify_session(manifest)
    assert checked["blocks"] == 3 and checked["samples"] == 300
    frame_path = recorder.folder/"frame_00000002.npz"
    frames, params, meta = load_capture(frame_path)
    frames[0].meta["device_time_s"] += 0.01
    save_capture(frame_path, frames, params)
    with pytest.raises(ValueError, match="Device-time gap"):
        verify_session(manifest)


def test_uploaded_team_b210_plfm_preset_is_preserved():
    from phalanx.params import b210_plfm
    p = b210_plfm()
    assert p.waveform == "plfm"
    assert p.fc_hz == 2.45e9
    assert p.bandwidth_hz == 25e6
    assert p.fs_hz == 30.72e6
    assert p.pulse_width_s == 0.5e-3
    assert p.prf_hz == 1e3
    assert p.n_pulses == 128
    assert p.n_samples == 3_932_160
    assert p.doppler_sign == 1
    # The team uses the B210; the old B310 preset was removed on 27 Oct 2026.
    import phalanx.params
    assert not hasattr(phalanx.params, "b310_plfm")


def test_recording_flush_supports_windows_writable_handle_requirement(tmp_path, monkeypatch):
    import phalanx.session as session
    real_open = Path.open
    real_fsync = session.os.fsync
    opened = {}
    def tracked_open(path, *args, **kwargs):
        file = real_open(path, *args, **kwargs)
        opened[file.fileno()] = file
        return file
    def windows_style_fsync(fd):
        file = opened.get(fd)
        if file is not None and not file.writable():
            raise OSError(9, "Bad file descriptor")
        return real_fsync(fd)
    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(session.os, "fsync", windows_style_fsync)
    recorder = SessionRecorder(tmp_path/"windows.session")
    recorder.append(make_frame(0))
    manifest = recorder.finish()
    assert recorder.saved_frames == 1
    assert session_files(manifest)[1]["state"] == "complete"
