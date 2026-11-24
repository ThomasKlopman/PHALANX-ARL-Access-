"""UI-01 checks, run headless (QT_QPA_PLATFORM=offscreen). Skipped if PyQt6 is absent."""
import os
import time

import numpy as np
import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets  # noqa: E402

from phalanx import SimConfig, SimulatedRadar, cantenna_fmcw, save_capture  # noqa: E402
from phalanx.gui.app import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump(app, seconds):
    t_end = time.time() + seconds
    while time.time() < t_end:
        app.processEvents()
        time.sleep(0.01)


def _wait_for(app, cond, timeout):
    t_end = time.time() + timeout
    while time.time() < t_end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_simulator_start_shows_detections_and_stop_acks_within_1s(app, tmp_path):
    w = MainWindow(repo_root=tmp_path)
    w._headless = True
    w.source_box.setCurrentIndex(0)
    w.start()
    assert _wait_for(app, lambda: w.state == "SIMULATOR" and w.table.rowCount() >= 2, timeout=10)
    assert w.table.item(0, 0).text() == "TARGET"
    assert "SIMULATOR" in w.table.item(0, 4).text()
    assert w.table.horizontalHeaderItem(1).text().endswith("[m]")      # units shown
    t0 = time.perf_counter()
    w.stop()
    assert w.state == "STOPPING"                                       # acknowledged immediately
    assert (time.perf_counter() - t0) < 1.0
    assert _wait_for(app, lambda: w.state == "STOPPED", timeout=5)
    w.close()


def test_replay_source_labels_and_ends_cleanly(app, tmp_path):
    p = cantenna_fmcw()
    frames = [SimulatedRadar(p, SimConfig(seed=0)).capture() for _ in range(2)]
    path = save_capture(tmp_path / "rec.npz", frames, p, label="test")
    w = MainWindow(repo_root=tmp_path)
    w._headless = True
    w.source_box.setCurrentIndex(1)
    w.file_edit.setText(str(path))
    w.start()
    assert _wait_for(app, lambda: w.state == "END OF FILE", timeout=10)
    assert w.table.rowCount() >= 1 and w.table.item(0, 4).text().startswith("REPLAY rec.npz")
    w.close()


def test_missing_recording_is_an_actionable_error_not_a_crash(app, tmp_path):
    w = MainWindow(repo_root=tmp_path)
    w._headless = True
    w.source_box.setCurrentIndex(1)
    w.file_edit.setText(str(tmp_path / "does_not_exist.npz"))
    w.start()
    _pump(app, 0.2)
    assert w.state == "ERROR"
    assert "not found" in w.detail_label.text()
    assert w.table.rowCount() == 0                                     # nothing fake displayed
    assert w.start_btn.isEnabled()                                     # can try again
    w.close()


def test_empty_recording_path_is_rejected(app, tmp_path):
    w = MainWindow(repo_root=tmp_path)
    w._headless = True
    w.source_box.setCurrentIndex(1)
    w.file_edit.setText("")
    w.start()
    _pump(app, 0.2)
    assert w.state == "ERROR" and "Choose a recording" in w.detail_label.text()
    w.close()


def test_hardware_source_failure_reports_error(app, tmp_path):
    """No kit attached here: opening must fail loudly, not hang or crash."""
    w = MainWindow(repo_root=tmp_path)
    w._headless = True
    w.source_box.setCurrentIndex(2)
    w.start()
    assert _wait_for(app, lambda: w.state == "ERROR", timeout=15)
    assert w.table.rowCount() == 0
    w.close()


def test_replay_keeps_detector_settings_editable_but_locks_sweep(app, tmp_path):
    """Replay: the recording fixes ramp/bandwidth, but MTI/Pfa/parity/range gates
    stay editable so old recordings can be re-tested with new detector settings."""
    w = MainWindow(repo_root=tmp_path, data_dir=tmp_path)
    w.source_box.setCurrentIndex(1)
    assert not w.ramp_spin.isEnabled() and not w.bw_spin.isEnabled()
    for widget in (w.parity_box, w.mti_check, w.pfa_box, w.minrange_spin, w.maxrange_spin):
        assert widget.isEnabled()
    w.close()


def test_gui_simulator_is_paced_like_the_kit():
    """Unpaced simulation floods the recorder queue; the GUI paces it at the kit's rate."""
    from phalanx.hardware.simulator import KIT_FRAME_PERIOD_S
    sim = SimulatedRadar(cantenna_fmcw(), SimConfig(seed=0, frame_period_s=0.1))
    t0 = time.perf_counter()
    for _ in range(3):
        sim.capture()
    assert time.perf_counter() - t0 >= 0.19
    assert 0.7 <= KIT_FRAME_PERIOD_S <= 0.9


def test_recordings_default_outside_the_repo(monkeypatch, tmp_path):
    from phalanx.paths import captures_dir
    monkeypatch.setenv("PHALANX_DATA", str(tmp_path / "data"))
    assert captures_dir() == tmp_path / "data" / "captures"
    monkeypatch.delenv("PHALANX_DATA")
    assert captures_dir().parent.name == "PHALANX_data"
