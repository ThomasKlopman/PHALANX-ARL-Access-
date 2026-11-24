"""Part 2 operator display: B210 FMCW live and simulator sources, arming, the TX gain limit.
Headless (QT_QPA_PLATFORM=offscreen). Skipped if PyQt6 is absent."""
import json
import os
import time

import numpy as np
import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets  # noqa: E402

from phalanx import save_capture  # noqa: E402
from phalanx.gui import app as gui  # noqa: E402
from phalanx.gui.app import (MainWindow, SRC_B210_FMCW, SRC_B210_FMCW_SIM, SRC_B210_RX,  # noqa: E402
                             SRC_KIT, SRC_REPLAY, SRC_SIM)
from phalanx.hardware import b210_fmcw  # noqa: E402
from phalanx.hardware.b210_fmcw import SimulatedB210Fmcw, SimWalker, b210_fmcw_params  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _wait_for(app, cond, timeout):
    t_end = time.time() + timeout
    while time.time() < t_end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def _window(tmp_path):
    w = MainWindow(repo_root=tmp_path, data_dir=tmp_path)
    w._headless = True
    return w


def test_simulator_source_shows_a_signed_detection_beyond_the_blind_range(app, tmp_path):
    w = _window(tmp_path)
    w.source_box.setCurrentIndex(SRC_B210_FMCW_SIM)
    blind = w.minrange_spin.value()
    assert blind == pytest.approx(1.5 * 149.9 / 6.0, abs=0.2)          # 8 MS/s default → 6 MHz → 37 m
    assert w.maxrange_spin.value() == 300.0 and w.minspeed_spin.value() == pytest.approx(0.3)
    w.start()
    assert _wait_for(app, lambda: w.state == "SIMULATOR" and w.table.rowCount() >= 1, timeout=30)
    assert w.table.item(0, 0).text() == "TARGET" and "SIMULATOR" in w.table.item(0, 4).text()
    rng, vel = float(w.table.item(0, 1).text()), float(w.table.item(0, 2).text())
    assert blind <= rng <= 100 and vel == pytest.approx(+1.4, abs=0.4)   # walker closing from 85 m
    assert "TX ON" in w.detail_label.text() and "latency 62" in w.detail_label.text()
    assert "blind inside" in w.detail_label.text()
    w.stop()
    assert _wait_for(app, lambda: w.state == "STOPPED", timeout=10)
    w.close()


def test_rows_and_gates_follow_the_selected_source(app, tmp_path):
    w = _window(tmp_path)
    assert w.minrange_spin.value() == 2.0 and w.maxrange_spin.value() == 30.0     # kit defaults
    w.source_box.setCurrentIndex(SRC_B210_FMCW)
    for widget in (w.fm_rate_box, w.fm_bw_spin, w.fm_rxgain_spin, w.fm_txgain_spin, w.fm_cal_check,
                   w.arm_check, w.ramp_spin, w.sdr_serial, w.minspeed_spin):
        assert widget.isEnabled()
    assert not w.bw_spin.isEnabled() and not w.parity_box.isEnabled() and not w.sdr_rate.isEnabled()
    w.fm_rate_box.setCurrentIndex(0)                                     # 4 MS/s → 3 MHz → 75 m blind
    assert w.fm_bw_spin.value() == pytest.approx(3.0) and w.fm_bw_spin.maximum() == pytest.approx(3.2)
    assert w.minrange_spin.value() == pytest.approx(75.0, abs=0.2)
    w.source_box.setCurrentIndex(SRC_B210_FMCW_SIM)                      # simulator: nothing to arm
    assert not w.arm_check.isEnabled() and not w.fm_txgain_spin.isEnabled() and w.fm_rate_box.isEnabled()
    w.source_box.setCurrentIndex(SRC_KIT)
    assert w.minrange_spin.value() == 2.0 and w.maxrange_spin.value() == 30.0 and w.minspeed_spin.value() == 0.0
    assert w.bw_spin.isEnabled() and not w.fm_rate_box.isEnabled() and not w.arm_check.isEnabled()
    w.source_box.setCurrentIndex(SRC_B210_RX)
    assert w.sdr_rate.isEnabled() and not w.fm_rate_box.isEnabled()
    w.close()


def test_tx_gain_cannot_exceed_the_limit_and_calibration_raises_it(app, tmp_path):
    w = _window(tmp_path)
    w.source_box.setCurrentIndex(SRC_B210_FMCW)
    assert not w.arm_check.isChecked()                                   # never armed by default
    assert w.fm_txgain_spin.maximum() == pytest.approx(44.8)
    assert "44.8 dB" in w.fm_limit_label.text() and "no-credit" in w.fm_limit_label.text()
    w.fm_txgain_spin.setValue(70)
    assert w.fm_txgain_spin.value() == pytest.approx(44.8)               # clamped by the widget
    w.fm_cal_check.setChecked(True)                                      # no calibration log yet
    assert w.fm_txgain_spin.maximum() == pytest.approx(44.8) and "no completed" in w.fm_limit_label.text()
    w.fm_cal_check.setChecked(False)
    (tmp_path / "captures").mkdir()
    (tmp_path / "captures" / "txramp_cans_20261110T154855Z.json").write_text(
        json.dumps({"mode": "cans", "recommended_max_tx_gain_db": 70.0}))
    w.fm_cal_check.setChecked(True)
    assert w.fm_txgain_spin.maximum() == pytest.approx(70.0)
    assert "70.0 dB" in w.fm_limit_label.text() and "txramp_cans_20261110T154855Z.json" in w.fm_limit_label.text()
    w.fm_txgain_spin.setValue(70)
    w.fm_cal_check.setChecked(False)                                     # limit drops → value follows
    assert w.fm_txgain_spin.value() == pytest.approx(44.8)
    w.close()


class FakeLiveRadar(SimulatedB210Fmcw):
    """Stands in for B210FmcwRadar: records how the GUI built it, produces simulated frames."""
    built = []

    def __init__(self, params, raw_fs_hz=4e6, serial="", tx_gain_db=40.0, rx_gain_db=40.0,
                 max_tx_gain_db=44.0, transmit=False, **kw):
        super().__init__(params, raw_fs_hz=raw_fs_hz, frame_period_s=0.2, transmit=transmit,
                         walkers=[SimWalker(range_m=80.0, velocity_mps=1.4)])
        type(self).built.append(dict(raw_fs_hz=raw_fs_hz, serial=serial, tx_gain_db=tx_gain_db,
                                     rx_gain_db=rx_gain_db, max_tx_gain_db=max_tx_gain_db,
                                     transmit=transmit))


def test_live_source_is_receive_only_until_armed_and_disarms_after_each_run(app, tmp_path, monkeypatch):
    monkeypatch.setattr(b210_fmcw, "B210FmcwRadar", FakeLiveRadar)
    FakeLiveRadar.built = []
    w = _window(tmp_path)
    w.source_box.setCurrentIndex(SRC_B210_FMCW)
    w.start()                                                            # not armed
    assert _wait_for(app, lambda: w.state == "LIVE" and "TX OFF" in w.detail_label.text(), timeout=30)
    assert FakeLiveRadar.built[-1]["transmit"] is False
    assert "nothing will be transmitted" in w.log.toPlainText()
    w.stop()
    assert _wait_for(app, lambda: w.state == "STOPPED", timeout=10)

    w.arm_check.setChecked(True)
    w.fm_txgain_spin.setValue(42.0)
    w.start()
    assert _wait_for(app, lambda: w.state == "LIVE" and "TX ON" in w.detail_label.text(), timeout=30)
    built = FakeLiveRadar.built[-1]
    assert built["transmit"] is True and built["tx_gain_db"] == pytest.approx(42.0)
    assert built["max_tx_gain_db"] == pytest.approx(44.8) and built["raw_fs_hz"] == 8e6
    assert built["serial"] == "326C061"
    assert "TRANSMITTER ARMED: TX gain 42.0 dB, limit 44.8 dB" in w.log.toPlainText()
    assert not w.arm_check.isEnabled()                                   # locked while running
    w.stop()
    assert _wait_for(app, lambda: w.state == "STOPPED", timeout=10)
    assert not w.arm_check.isChecked()                                   # must be armed again next time
    w.close()


def test_stopping_an_armed_run_says_the_transmitter_may_still_be_on(app, tmp_path, monkeypatch):
    """Stop sets a flag and the capture in flight finishes; it is not the driver's abort. The
    STOPPING display must not read as "RF is off" (review of 12 Nov 2026, finding 5)."""
    monkeypatch.setattr(b210_fmcw, "B210FmcwRadar", FakeLiveRadar)
    w = _window(tmp_path)
    w.source_box.setCurrentIndex(SRC_B210_FMCW)
    w.start()                                                            # receive only
    assert _wait_for(app, lambda: w.state == "LIVE", timeout=30)
    w.stop()
    assert w.state == "STOPPING" and "MAY STILL BE ON" not in w.detail_label.text()
    assert _wait_for(app, lambda: w.state == "STOPPED", timeout=10)
    assert "TX gain 0 dB was requested" not in w.log.toPlainText()

    w.arm_check.setChecked(True)
    w.start()
    assert _wait_for(app, lambda: w.state == "LIVE" and "TX ON" in w.detail_label.text(), timeout=30)
    w.stop()
    assert w.state == "STOPPING" and "TRANSMITTER MAY STILL BE ON" in w.detail_label.text()
    assert _wait_for(app, lambda: w.state == "STOPPED", timeout=10)
    log = w.log.toPlainText()
    assert "TRANSMITTER MAY STILL BE ON" in log and "TX gain 0 dB was requested" in log
    assert "not a measurement that RF is off" in log
    w.close()


def test_live_source_without_the_board_is_an_error_not_a_crash(app, tmp_path):
    """No UHD / no B210 here: opening must fail loudly and leave the transmitter disarmed."""
    w = _window(tmp_path)
    w.source_box.setCurrentIndex(SRC_B210_FMCW)
    w.arm_check.setChecked(True)
    w.start()
    assert _wait_for(app, lambda: w.state == "ERROR", timeout=20)
    assert w.table.rowCount() == 0 and not w.arm_check.isChecked() and w.start_btn.isEnabled()
    w.close()


def test_sweep_wider_than_the_rate_allows_is_rejected(app, tmp_path):
    w = _window(tmp_path)
    w.source_box.setCurrentIndex(SRC_B210_FMCW_SIM)
    w.ramp_spin.setValue(40)                                             # too few pulses per frame
    w.start()
    assert _wait_for(app, lambda: w.state == "ERROR", timeout=5)
    assert "Ramp time" in w.detail_label.text()
    w.close()


def test_b210_fmcw_recording_replays_without_touching_the_kit_parity_setting(app, tmp_path):
    p = b210_fmcw_params(bandwidth_mhz=6.0)
    sim = SimulatedB210Fmcw(p, raw_fs_hz=8e6, frame_period_s=0,
                            walkers=[SimWalker(range_m=80.0, velocity_mps=-1.2, far_m=200)])
    sim.start()
    frames = [sim.capture() for _ in range(3)]
    assert np.iscomplexobj(frames[0].samples)
    path = save_capture(tmp_path / "b210.npz", frames, p, label="b210_fmcw")
    w = _window(tmp_path)
    w.source_box.setCurrentIndex(SRC_REPLAY)
    w.file_edit.setText(str(path))
    assert w.parity_box.currentIndex() == 0                              # kit default, left alone
    w.minrange_spin.setValue(37.5); w.maxrange_spin.setValue(300.0)
    w.start()
    assert _wait_for(app, lambda: w.state == "END OF FILE" and w.table.rowCount() >= 1, timeout=20)
    assert w.table.item(0, 4).text().startswith("REPLAY b210.npz")
    assert float(w.table.item(0, 2).text()) == pytest.approx(-1.2, abs=0.4)   # receding stays negative
    w.close()


def test_kit_simulator_still_works_after_the_part2_changes(app, tmp_path):
    w = _window(tmp_path)
    w.source_box.setCurrentIndex(SRC_SIM)
    w.start()
    assert _wait_for(app, lambda: w.state == "SIMULATOR" and w.table.rowCount() >= 2, timeout=15)
    w.stop()
    assert _wait_for(app, lambda: w.state == "STOPPED", timeout=5)
    w.close()
