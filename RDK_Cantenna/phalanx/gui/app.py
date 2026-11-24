"""PHALANX operator GUI (UI-01) — PyQt6 + pyqtgraph.

    python -m phalanx.gui                 # or:  python RDK_Cantenna_GUI.py

Acceptance criteria this is built around (UI-01 card):
  * Start / Stop show the correct state; Stop is acknowledged within one second
    (the UI flips to STOPPING immediately; the worker exits after the frame in flight).
  * Each displayed detection shows decision, measurement values with units,
    and the SOURCE label (LIVE kit / REPLAY file / SIMULATOR) of the record it came from.
  * Invalid configuration or missing data → an actionable error, no crash, and
    never an invalid result shown as a valid detection (the display is cleared).

Acquisition/DSP runs on a worker; a GUI timer consumes one newest preview.
Recording uses a bounded background writer and never selects samples by GUI rate.
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

import numpy as np
from PyQt6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

from .. import __version__
from ..session import SessionRecorder
from .latest import LatestPreview
from ..dsp.detect import CfarConfig
from ..dsp.pipeline import ProcessorConfig, RangeDopplerProcessor, Result
from ..frame import Frame
from ..hardware.base import RadarSource
from ..hardware.recorded import RecordedRadar
from ..hardware.simulator import KIT_FRAME_PERIOD_S, SimConfig, SimulatedRadar, Target
from ..params import RadarParams, cantenna_fmcw
from ..hardware.b210 import B210ReceiveRadar, TESTED_RECORD_RATE_SPS, b210_rx_params
from ..hardware import b210_fmcw
from ..paths import captures_dir
from ..tx_budget import NO_CREDIT_LIMIT_DB, gain_limit

# Source list positions
SRC_SIM, SRC_REPLAY, SRC_KIT, SRC_B210_RX, SRC_B210_FMCW, SRC_B210_FMCW_SIM = range(6)
B210_FMCW_SOURCES = (SRC_B210_FMCW, SRC_B210_FMCW_SIM)

STATE_COLORS = {"STOPPED": "#9e9e9e", "STARTING": "#ffb300", "LIVE": "#43a047",
                "REPLAY": "#1e88e5", "SIMULATOR": "#00897b", "STOPPING": "#ffb300",
                "ERROR": "#e53935", "END OF FILE": "#8e24aa"}
RUNNING_STATES = ("STARTING", "LIVE", "REPLAY", "SIMULATOR")


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

class AcquisitionWorker(QtCore.QThread):
    configured = QtCore.pyqtSignal(object)              # actual RadarParams
    state_changed = QtCore.pyqtSignal(str, str)          # state, detail
    failed = QtCore.pyqtSignal(str)

    def __init__(self, make_source, params: RadarParams, cfg: ProcessorConfig,
                 record_to: Path | None = None, label: str = "", record_queue_blocks=16):
        super().__init__()
        self.make_source = make_source
        self.params, self.cfg = params, cfg
        self.record_to, self.label = record_to, label
        self._stop = False
        self.source_kind = "LIVE"
        self.latest = LatestPreview()
        self.acquired_frames = self.acquired_samples = self.processed_frames = 0
        self.recorder = None
        self.record_queue_blocks = record_queue_blocks

    def request_stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        source = None
        failed, ended, reason = False, False, ""
        try:
            self.state_changed.emit("STARTING", "opening source")
            source = self.make_source()
            source.open()
            # Replay owns its parameters; GUI defaults must not overwrite them.
            source.configure(None if isinstance(source, RecordedRadar) else self.params)
            params = source.params
            self.configured.emit(params)
            monitor = params.waveform == "iq"
            proc = None if monitor else RangeDopplerProcessor(params, self.cfg)
            if self.record_to is not None:
                self.recorder = SessionRecorder(self.record_to, self.label, self.record_queue_blocks)
            source.start()
            self.state_changed.emit(self.source_kind, "RX monitor" if monitor else "")
            while not self._stop:
                if self.recorder is not None and self.recorder.error is not None:
                    raise RuntimeError(self.recorder.error)
                try:
                    frame = source.capture()
                except StopIteration:
                    ended = True
                    break
                self.acquired_frames += 1
                self.acquired_samples += len(frame)
                if (proc is not None and self.acquired_frames == 1 and self.cfg.ramp_offset is None
                        and frame.meta.get("signal_mode") == "fmcw_dechirped"):
                    # A B210 FMCW recording being replayed: its frames start on an up-ramp by
                    # construction, so the kit's ramp search and parity setting do not apply.
                    self.cfg.ramp_offset, self.cfg.ramp_parity = 0, 0
                    proc = RangeDopplerProcessor(params, self.cfg)
                # Recording sees EVERY acquired frame before any display selection.
                if self.recorder is not None:
                    self.recorder.append(frame)
                t0 = time.perf_counter()
                result = None if monitor else proc.process(frame)
                self.processed_frames += 1
                if result is not None:
                    result.diag["dsp_ms"] = (time.perf_counter() - t0) * 1e3
                    result.diag["source_kind"] = self.source_kind
                # No per-frame Qt signals and no sleeps. Only the preview is replaceable.
                self.latest.publish(result, frame)
        except Exception as error:
            failed = True
            reason = f"{type(error).__name__}: {error}"
            self.failed.emit(f"{reason}\n\n{traceback.format_exc(limit=3)}")
        finally:
            if source is not None:
                try:
                    source.stop()
                    source.close()
                except Exception as error:
                    failed, reason = True, f"Source cleanup failed: {error}"
                    self.failed.emit(reason)
            detail = ""
            if self.recorder is not None:
                try:
                    out = self.recorder.finish(valid=not failed, reason=reason,
                                               acquired_frames=self.acquired_frames,
                                               acquired_samples=self.acquired_samples)
                    detail = (f"saved {self.recorder.saved_frames} blocks / "
                              f"{self.recorder.saved_samples} samples → {out}")
                except Exception as error:
                    failed = True
                    self.failed.emit(f"Recording failed: {error}")
            if not failed:
                self.state_changed.emit("END OF FILE" if ended else "STOPPED",
                                        detail or ("recording finished" if ended else ""))



# ---------------------------------------------------------------------------
# Operator scope (PPI-style sector display)
# ---------------------------------------------------------------------------

class ScopeView(pg.PlotWidget):
    """Plan view: the radar is at the bottom centre looking up the page.

    Range rings every `ring_m`, the antenna beam as a shaded wedge. Phase 1 has
    ONE channel and therefore NO bearing information, so a detection is drawn as
    an ARC across the whole beam at its measured range — "something at 6 m,
    somewhere in the beam" — red if approaching, blue if moving away, grey if
    crossing. When a detection carries `azimuth_deg` (Phase 2, CN0566 array)
    it is drawn as a dot at that bearing instead.
    """

    def __init__(self):
        super().__init__()
        self.setAspectLocked(True)
        self.setMenuEnabled(False)
        self.hideAxis("left")
        self.hideAxis("bottom")
        self.setBackground("#0b1a10")
        self._static: list = []
        self._dyn: list = []
        self.max_range = 30.0
        self.beamwidth = 60.0
        self.ring_m = 5.0
        self._draw_static()

    # geometry ----------------------------------------------------------------
    def set_geometry(self, max_range: float, beamwidth_deg: float) -> None:
        if (max_range, beamwidth_deg) != (self.max_range, self.beamwidth):
            self.max_range, self.beamwidth = max_range, beamwidth_deg
            self._draw_static()

    def _arc(self, r: float, a0: float, a1: float, n: int = 60):
        a = np.radians(np.linspace(a0, a1, n))
        return r * np.sin(a), r * np.cos(a)          # 0° = straight ahead (up)

    def _draw_static(self) -> None:
        for it in self._static:
            self.removeItem(it)
        self._static.clear()
        R, hb = self.max_range, self.beamwidth / 2
        self.ring_m = 5.0 if R <= 40 else (10.0 if R <= 120 else 25.0)
        # beam wedge
        xs, ys = self._arc(R, -hb, hb)
        poly = QtGui.QPolygonF([QtCore.QPointF(0, 0)] + [QtCore.QPointF(x, y) for x, y in zip(xs, ys)])
        wedge = QtWidgets.QGraphicsPolygonItem(poly)
        wedge.setPen(pg.mkPen("#3fbf5f", width=1))
        wedge.setBrush(pg.mkBrush(63, 191, 95, 28))
        wedge.setZValue(-10)
        self.addItem(wedge); self._static.append(wedge)
        # range rings + labels
        r = self.ring_m
        while r <= R + 1e-6:
            xs, ys = self._arc(r, -90, 90, 120)
            ring = pg.PlotDataItem(xs, ys, pen=pg.mkPen("#2e7d46", width=1, style=QtCore.Qt.PenStyle.DotLine))
            self.addItem(ring); self._static.append(ring)
            lab = pg.TextItem(f"{r:g} m", color="#8fd3a3", anchor=(0.5, 1.0))
            lab.setPos(0, r); self.addItem(lab); self._static.append(lab)
            r += self.ring_m
        # boresight + radar symbol
        bore = pg.PlotDataItem([0, 0], [0, R], pen=pg.mkPen("#2e7d46", width=1, style=QtCore.Qt.PenStyle.DashLine))
        self.addItem(bore); self._static.append(bore)
        me = pg.ScatterPlotItem([0], [0], size=14, symbol="t1", pen=pg.mkPen("w"), brush=pg.mkBrush("w"))
        self.addItem(me); self._static.append(me)
        self.setXRange(-R * 1.05, R * 1.05, padding=0)
        self.setYRange(-0.12 * R, R * 1.08, padding=0)

    # per frame -----------------------------------------------------------------
    @staticmethod
    def _color(v: float):
        return "#ff5252" if v > 0.15 else ("#448aff" if v < -0.15 else "#bdbdbd")

    def update_detections(self, dets, trail) -> None:
        for it in self._dyn:
            self.removeItem(it)
        self._dyn.clear()
        hb = self.beamwidth / 2
        # faded history first
        n = len(trail)
        for i, (r, v, az) in enumerate(trail):
            if r > self.max_range:
                continue
            alpha = int(30 + 120 * (i + 1) / max(n, 1))
            col = QtGui.QColor(self._color(v)); col.setAlpha(alpha)
            if az is None:
                xs, ys = self._arc(r, -hb, hb)
                it = pg.PlotDataItem(xs, ys, pen=pg.mkPen(col, width=3))
            else:
                it = pg.ScatterPlotItem([r * np.sin(np.radians(az))], [r * np.cos(np.radians(az))],
                                        size=9, pen=None, brush=pg.mkBrush(col))
            self.addItem(it); self._dyn.append(it)
        # current detections
        for d in dets:
            if d.range_m > self.max_range:
                continue
            col = self._color(d.velocity_mps)
            az = d.extra.get("azimuth_deg")
            if az is None:
                xs, ys = self._arc(d.range_m, -hb, hb)
                it = pg.PlotDataItem(xs, ys, pen=pg.mkPen(col, width=6))
                lx, ly = 0.0, d.range_m
            else:
                lx, ly = d.range_m * np.sin(np.radians(az)), d.range_m * np.cos(np.radians(az))
                it = pg.ScatterPlotItem([lx], [ly], size=18, pen=pg.mkPen("w", width=1), brush=pg.mkBrush(col))
            self.addItem(it); self._dyn.append(it)
            word = "closing" if d.velocity_mps > 0.15 else ("opening" if d.velocity_mps < -0.15 else "crossing")
            lab = pg.TextItem(f"{d.range_m:.1f} m  {word} {abs(d.velocity_mps):.1f} m/s", color=col, anchor=(0.5, 1.2))
            lab.setPos(lx, ly); self.addItem(lab); self._dyn.append(lab)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, repo_root: Path | None = None, data_dir: Path | None = None):
        super().__init__()
        self.setWindowTitle(f"PHALANX radar — Phase 1 operator display  (phalanx {__version__})")
        self.resize(1280, 760)
        self.repo_root = repo_root or Path.cwd()
        # Recordings go outside the clone/OneDrive (phalanx/paths.py); tests pass tmp_path.
        self.captures_dir = Path(data_dir) / "captures" if data_dir is not None else captures_dir()
        self.worker: AcquisitionWorker | None = None
        self.state = "STOPPED"
        self._last_result_t = None
        self._stop_requested_at = None
        self._display_failed = False
        self._build_ui()
        self._display_timer = QtCore.QTimer(self)
        self._display_timer.setInterval(100)  # 10 preview refreshes/s; acquisition is independent.
        self._display_timer.timeout.connect(self._display_latest)
        self._display_timer.start()
        self.set_state("STOPPED", "")

    # ---- UI construction ------------------------------------------------
    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QHBoxLayout(central)

        # left: controls
        ctl = QtWidgets.QVBoxLayout()
        root.addLayout(ctl, 0)
        form = QtWidgets.QFormLayout()
        self.source_box = QtWidgets.QComboBox()
        self.source_box.addItems(["Simulator", "Replay recording", "Cantenna kit (USB)", "B210 RX monitor",
                                  "B210 FMCW radar (TX+RX)", "B210 FMCW simulator"])
        self.source_box.currentIndexChanged.connect(self._source_changed)
        form.addRow("Source", self.source_box)
        file_row = QtWidgets.QHBoxLayout()
        self.file_edit = QtWidgets.QLineEdit()
        self.file_edit.setPlaceholderText("capture.npz or session folder/session.json")
        self.file_btn = QtWidgets.QPushButton("…")
        self.file_btn.setFixedWidth(32)
        self.file_btn.clicked.connect(self._pick_file)
        file_row.addWidget(self.file_edit)
        file_row.addWidget(self.file_btn)
        form.addRow("Recording", file_row)
        self.sdr_serial = QtWidgets.QLineEdit("326C061")
        self.sdr_serial.setPlaceholderText("Optional B210 serial")
        form.addRow("B210 serial", self.sdr_serial)
        self.sdr_freq = QtWidgets.QDoubleSpinBox()
        self.sdr_freq.setDecimals(6)
        self.sdr_freq.setRange(0.07, 6.0)
        self.sdr_freq.setValue(2.45)
        self.sdr_freq.setSuffix(" GHz")
        form.addRow("RX center", self.sdr_freq)
        self.sdr_rate = QtWidgets.QDoubleSpinBox()
        self.sdr_rate.setDecimals(3)
        self.sdr_rate.setRange(0.25, 30.72)
        self.sdr_rate.setValue(1.0)
        self.sdr_rate.setSuffix(" MSPS")
        self.sdr_rate.setToolTip(f"Recording verified up to {TESTED_RECORD_RATE_SPS/1e6:g} MSPS "
                                 "(22 Oct 2026). Higher rates may overflow the recorder.")
        form.addRow("RX rate", self.sdr_rate)
        self.sdr_gain = QtWidgets.QDoubleSpinBox()
        self.sdr_gain.setRange(0, 76)
        self.sdr_gain.setValue(10)
        self.sdr_gain.setSuffix(" dB")
        form.addRow("RX gain", self.sdr_gain)
        # --- B210 FMCW radar (Part 2) ---
        self.fm_rate_box = QtWidgets.QComboBox()
        self.fm_rate_box.addItems(["4", "8", "12", "16"])
        self.fm_rate_box.setCurrentIndex(1)
        self.fm_rate_box.setToolTip("B210 sample rate in MS/s. 4 and 8 ran clean on hardware (10 Nov 2026);\n"
                                    "16 overflowed on the laptop's USB link. The sweep cannot be wider than the rate.")
        self.fm_rate_box.currentIndexChanged.connect(self._fm_rate_changed)
        form.addRow("B210 rate [MS/s]", self.fm_rate_box)
        self.fm_bw_spin = QtWidgets.QDoubleSpinBox()
        self.fm_bw_spin.setDecimals(1)
        self.fm_bw_spin.setSuffix(" MHz")
        self.fm_bw_spin.setToolTip("Sweep width. Range cell = 150 m / (sweep in MHz); at most 80 % of the rate.")
        self.fm_bw_spin.valueChanged.connect(self._fm_bw_changed)
        form.addRow("B210 sweep", self.fm_bw_spin)
        self.fm_rxgain_spin = QtWidgets.QDoubleSpinBox()
        self.fm_rxgain_spin.setRange(0, 76)
        self.fm_rxgain_spin.setValue(40)
        self.fm_rxgain_spin.setSuffix(" dB")
        form.addRow("B210 RX gain", self.fm_rxgain_spin)
        self.fm_txgain_spin = QtWidgets.QDoubleSpinBox()
        self.fm_txgain_spin.setDecimals(1)
        self.fm_txgain_spin.setRange(0, NO_CREDIT_LIMIT_DB)
        self.fm_txgain_spin.setValue(40)
        self.fm_txgain_spin.setSuffix(" dB")
        self.fm_txgain_spin.setToolTip("Cannot be set above the limit shown below.")
        form.addRow("B210 TX gain", self.fm_txgain_spin)
        self.fm_cal_check = QtWidgets.QCheckBox("Use the latest ramp calibration")
        self.fm_cal_check.setToolTip("Raises the TX gain limit to the cap reached by the newest completed\n"
                                     "tx_ramp_calibration.py run. Only valid if the cans have NOT moved since.")
        self.fm_cal_check.toggled.connect(self._update_tx_limit)
        form.addRow(self.fm_cal_check)
        self.fm_limit_label = QtWidgets.QLabel("")
        self.fm_limit_label.setWordWrap(True)
        self.fm_limit_label.setStyleSheet("font-size: 11px;")
        form.addRow("TX gain limit", self.fm_limit_label)
        self.arm_check = QtWidgets.QCheckBox("ARM transmitter (off = receive only)")
        self.arm_check.setToolTip("Nothing is transmitted unless this is ticked before Start.\n"
                                  "It unticks itself whenever a run ends.")
        self.arm_check.setStyleSheet("font-weight: bold;")
        form.addRow(self.arm_check)
        self.ramp_spin = QtWidgets.QSpinBox()
        self.ramp_spin.setRange(1, 65536)
        self.ramp_spin.setValue(4)
        self.ramp_spin.setSuffix(" ms")
        form.addRow("Ramp time", self.ramp_spin)
        self.bw_spin = QtWidgets.QDoubleSpinBox()
        self.bw_spin.setRange(1.0, 100.0)
        self.bw_spin.setValue(100.0)
        self.bw_spin.setSuffix(" MHz")
        form.addRow("Sweep BW", self.bw_spin)
        self.parity_box = QtWidgets.QComboBox()
        self.parity_box.addItems(["1 (kit 5035, calibrated)", "0"])
        form.addRow("Ramp parity", self.parity_box)
        self.mti_check = QtWidgets.QCheckBox("Hide stationary objects (MTI)")
        self.mti_check.setToolTip("Off: walls and furniture show as a bright band along velocity = 0.\n"
                                  "On: only things that move are shown.")
        self.mti_check.setChecked(True)
        form.addRow(self.mti_check)
        self.pfa_box = QtWidgets.QComboBox()
        self.pfa_box.addItems(["1e-3", "1e-4", "1e-5", "1e-6"])
        self.pfa_box.setCurrentIndex(1)
        form.addRow("CFAR Pfa", self.pfa_box)
        self.minrange_spin = QtWidgets.QDoubleSpinBox()
        self.minrange_spin.setRange(0.0, 500.0)
        self.minrange_spin.setValue(2.0)
        self.minrange_spin.setSuffix(" m")
        form.addRow("Min range", self.minrange_spin)
        self.maxrange_spin = QtWidgets.QDoubleSpinBox()
        self.maxrange_spin.setRange(5.0, 2000.0)
        self.maxrange_spin.setValue(30.0)
        self.maxrange_spin.setSuffix(" m")
        self.maxrange_spin.setToolTip("Zoom of the display and the farthest range the detector looks at")
        form.addRow("Max range", self.maxrange_spin)
        self.minspeed_spin = QtWidgets.QDoubleSpinBox()
        self.minspeed_spin.setRange(0.0, 3.0)
        self.minspeed_spin.setSingleStep(0.1)
        self.minspeed_spin.setValue(0.0)
        self.minspeed_spin.setSuffix(" m/s")
        self.minspeed_spin.setToolTip("Ignore detections slower than this (0 = off).")
        form.addRow("Min speed", self.minspeed_spin)
        self.beam_spin = QtWidgets.QSpinBox()
        self.beam_spin.setRange(10, 180)
        self.beam_spin.setValue(60)
        self.beam_spin.setSuffix(" °")
        self.beam_spin.setToolTip("Antenna beamwidth drawn on the Scope (cantenna ≈ 60°; measure it)")
        form.addRow("Beam width", self.beam_spin)
        self.trail_check = QtWidgets.QCheckBox("Show trail of recent detections")
        self.trail_check.setChecked(True)
        form.addRow(self.trail_check)
        self.record_check = QtWidgets.QCheckBox("Record all blocks (session in PHALANX_data/captures)")
        self.record_check.setToolTip(str(self.captures_dir))
        form.addRow(self.record_check)
        ctl.addLayout(form)
        self._form = form
        self._family = "kit"
        self._fm_rate_changed()
        self._update_tx_limit()

        btns = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("▶ Start")
        self.stop_btn = QtWidgets.QPushButton("■ Stop")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.stop)
        btns.addWidget(self.start_btn)
        btns.addWidget(self.stop_btn)
        ctl.addLayout(btns)

        self.state_label = QtWidgets.QLabel("STOPPED")
        self.state_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.state_label.setStyleSheet("font-size: 20px; font-weight: bold; padding: 8px; border-radius: 6px;")
        ctl.addWidget(self.state_label)
        self.detail_label = QtWidgets.QLabel("")
        self.detail_label.setWordWrap(True)
        ctl.addWidget(self.detail_label)
        self.readout = QtWidgets.QLabel("—")
        self.readout.setWordWrap(True)
        self.readout.setStyleSheet("font-size: 15px; padding: 6px; border: 1px solid #888; border-radius: 6px;")
        ctl.addWidget(self.readout)
        self.params_label = QtWidgets.QLabel("")
        self.params_label.setWordWrap(True)
        self.params_label.setStyleSheet("font-family: monospace; font-size: 11px;")
        ctl.addWidget(self.params_label)
        ctl.addStretch(1)

        # centre: tabs
        pg.setConfigOptions(antialias=False, imageAxisOrder="row-major")
        self.tabs = QtWidgets.QTabWidget()
        root.addWidget(self.tabs, 1)
        scope_tab = QtWidgets.QWidget()
        scope_layout = QtWidgets.QVBoxLayout(scope_tab)
        scope_layout.setContentsMargins(0, 0, 0, 0)
        self.scope = ScopeView()
        scope_layout.addWidget(self.scope, 1)
        legend = QtWidgets.QLabel(
            "<span style='color:#ff5252'>■</span> approaching &nbsp; "
            "<span style='color:#448aff'>■</span> moving away &nbsp; "
            "<span style='color:#bdbdbd'>■</span> crossing / slow &nbsp;|&nbsp; "
            "you are the ▲ at the bottom, looking up the page &nbsp;|&nbsp; "
            "an <b>arc</b> means the range is known but not the bearing (single-channel kit); "
            "the Phase-2 array will turn arcs into dots")
        legend.setWordWrap(True)
        legend.setStyleSheet("padding: 4px 8px; font-size: 12px;")
        scope_layout.addWidget(legend, 0)
        self.tabs.addTab(scope_tab, "Scope")
        rd_tab = QtWidgets.QWidget()
        plots = QtWidgets.QVBoxLayout(rd_tab)
        self.tabs.addTab(rd_tab, "Range–Doppler")
        raw_tab = QtWidgets.QWidget()
        raw_layout = QtWidgets.QVBoxLayout(raw_tab)
        self.tabs.addTab(raw_tab, "Raw signal")
        self.raw_plot = pg.PlotWidget(title="Received samples (one frame)")
        self.raw_plot.setLabel("bottom", "sample"); self.raw_plot.setLabel("left", "amplitude (full scale = 1)")
        self.raw_curve = self.raw_plot.plot(pen=pg.mkPen("#ffb300", width=1), name="I / real")
        self.raw_q_curve = self.raw_plot.plot(pen=pg.mkPen("#1e88e5", width=1), name="Q")
        self.spec_plot = pg.PlotWidget(title="Spectrum of the frame (dB)")
        self.spec_plot.setLabel("bottom", "frequency", units="Hz"); self.spec_plot.setLabel("left", "dB")
        self.spec_curve = self.spec_plot.plot(pen=pg.mkPen("#1e88e5", width=1))
        raw_layout.addWidget(self.raw_plot, 1); raw_layout.addWidget(self.spec_plot, 1)
        self.rd_plot = pg.PlotWidget(title="Range–Doppler (dB) — red rings = CFAR detections")
        self.rd_plot.setLabel("bottom", "range", units="m")
        self.rd_plot.setLabel("left", "velocity (+ = closing)", units="m/s")
        self.rd_img = pg.ImageItem()
        self.rd_plot.addItem(self.rd_img)
        cmap = pg.colormap.get("viridis")
        self.rd_img.setLookupTable(cmap.getLookupTable(0.0, 1.0, 256))
        self.trail_scatter = pg.ScatterPlotItem(size=8, pen=None, brush=pg.mkBrush(255, 255, 255, 110))
        self.rd_plot.addItem(self.trail_scatter)
        self.det_scatter = pg.ScatterPlotItem(size=18, pen=pg.mkPen("r", width=2), brush=None)
        self.rd_plot.addItem(self.det_scatter)
        self.zero_line = pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen("w", width=1, style=QtCore.Qt.PenStyle.DashLine))
        self.rd_plot.addItem(self.zero_line)
        self.txt_up = pg.TextItem("▲ approaching", color="w", anchor=(0, 0))
        self.txt_dn = pg.TextItem("▼ moving away", color="w", anchor=(0, 1))
        for t in (self.txt_up, self.txt_dn):
            t.setZValue(20)
            self.rd_plot.addItem(t)
        self.det_scatter.setZValue(15)
        self.trail_scatter.setZValue(14)
        self.zero_line.setZValue(12)
        self.rd_plot.showGrid(x=True, y=False, alpha=0.25)
        self.rd_plot.setMenuEnabled(False)
        self._trail: list[tuple[float, float, float | None]] = []
        self._floor_db: float | None = None
        plots.addWidget(self.rd_plot, 3)
        self.profile_plot = pg.PlotWidget(title="Echo strength vs range (blue) and detection threshold (red dashed)")
        self.profile_plot.setLabel("bottom", "range", units="m")
        self.profile_plot.setLabel("left", "dB")
        self.profile_plot.showGrid(x=True, y=True, alpha=0.25)
        self.profile_plot.setMenuEnabled(False)
        self.profile_curve = self.profile_plot.plot(pen=pg.mkPen("#1e88e5", width=1.5))
        self.profile_thresh = self.profile_plot.plot(pen=pg.mkPen("#e53935", width=1, style=QtCore.Qt.PenStyle.DashLine))
        plots.addWidget(self.profile_plot, 1)

        # right: detections table
        right = QtWidgets.QVBoxLayout()
        root.addLayout(right, 0)
        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["decision", "range [m]", "velocity [m/s]", "SNR [dB]", "source"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumWidth(420)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        right.addWidget(QtWidgets.QLabel("Detections (this frame)"))
        right.addWidget(self.table, 1)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(300)
        right.addWidget(QtWidgets.QLabel("Log"))
        right.addWidget(self.log, 1)

        self.status = self.statusBar()
        self.fps_label = QtWidgets.QLabel("")
        self.status.addPermanentWidget(self.fps_label)
        self.counters_label = QtWidgets.QLabel("")
        self.status.addPermanentWidget(self.counters_label)
        self._source_changed()

    # ---- helpers ----------------------------------------------------------
    def _source_changed(self) -> None:
        kind = self.source_box.currentIndex()
        fmcw = kind in B210_FMCW_SOURCES
        active = self.state in RUNNING_STATES or self.state == "STOPPING"
        self.source_box.setEnabled(not active)
        self.record_check.setEnabled(not active)
        self.file_edit.setEnabled(kind == SRC_REPLAY and not active)
        self.file_btn.setEnabled(kind == SRC_REPLAY and not active)
        self.sdr_serial.setEnabled(kind in (SRC_B210_RX, SRC_B210_FMCW) and not active)
        for widget in (self.sdr_freq, self.sdr_rate, self.sdr_gain):
            widget.setEnabled(kind == SRC_B210_RX and not active)
        for widget in (self.fm_rate_box, self.fm_bw_spin):
            widget.setEnabled(fmcw and not active)
        for widget in (self.fm_rxgain_spin, self.fm_txgain_spin, self.fm_cal_check, self.arm_check):
            widget.setEnabled(kind == SRC_B210_FMCW and not active)
        # Sweep settings come from the recording in Replay; detector settings do not,
        # so they stay editable there (re-test old data with new MTI/Pfa/parity/gates).
        self.ramp_spin.setEnabled(kind in (SRC_SIM, SRC_KIT) + B210_FMCW_SOURCES and not active)
        self.bw_spin.setEnabled(kind in (SRC_SIM, SRC_KIT) and not active)
        self.parity_box.setEnabled(kind in (SRC_SIM, SRC_REPLAY, SRC_KIT) and not active)
        for widget in (self.mti_check, self.pfa_box, self.minrange_spin, self.maxrange_spin,
                       self.minspeed_spin):
            widget.setEnabled(kind != SRC_B210_RX and not active)
        # Show only the rows that belong to the selected source (keeps the panel short).
        if self._form is not None and hasattr(self._form, "setRowVisible"):
            for widget in (self.fm_rate_box, self.fm_bw_spin):
                self._form.setRowVisible(widget, fmcw)
            for widget in (self.fm_rxgain_spin, self.fm_txgain_spin, self.fm_cal_check,
                           self.fm_limit_label, self.arm_check):
                self._form.setRowVisible(widget, kind == SRC_B210_FMCW)
            for widget in (self.sdr_freq, self.sdr_rate, self.sdr_gain):
                self._form.setRowVisible(widget, kind == SRC_B210_RX)
            for widget in (self.bw_spin, self.parity_box):
                self._form.setRowVisible(widget, not fmcw and kind != SRC_B210_RX)
        # Detector gates follow the kind of radar: the kit resolves 1.5 m, the B210 tens of metres.
        family = "b210" if fmcw else "kit"
        if family != self._family and not active:
            self._family = family
            if fmcw:
                self.minrange_spin.setValue(self._fm_blind_range())
                self.maxrange_spin.setValue(300.0)
                self.minspeed_spin.setValue(0.3)
            else:
                self.minrange_spin.setValue(2.0)
                self.maxrange_spin.setValue(30.0)
                self.minspeed_spin.setValue(0.0)

    # ---- B210 FMCW controls ----------------------------------------------
    def _fm_rate(self) -> float:
        return float(self.fm_rate_box.currentText()) * 1e6

    def _fm_blind_range(self) -> float:
        return b210_fmcw.BLIND_CELLS * 149.896229 / max(self.fm_bw_spin.value(), 0.1)

    def _fm_rate_changed(self) -> None:
        rate_mhz = float(self.fm_rate_box.currentText())
        self.fm_bw_spin.blockSignals(True)
        self.fm_bw_spin.setRange(1.0, 0.8 * rate_mhz)
        self.fm_bw_spin.setValue(0.75 * rate_mhz)
        self.fm_bw_spin.blockSignals(False)
        self._fm_bw_changed()

    def _fm_bw_changed(self) -> None:
        if getattr(self, "_family", "kit") == "b210":
            self.minrange_spin.setValue(self._fm_blind_range())

    def _tx_limit(self) -> tuple[float, str]:
        """(limit dB, source). Falls back to the no-credit limit if no usable calibration exists."""
        if self.fm_cal_check.isChecked():
            try:
                return gain_limit("latest", self.captures_dir)
            except (OSError, ValueError) as error:
                return NO_CREDIT_LIMIT_DB, f"no-credit budget limit — {error}"
        return NO_CREDIT_LIMIT_DB, "no-credit budget limit"

    def _update_tx_limit(self) -> None:
        limit, source = self._tx_limit()
        self.fm_txgain_spin.setMaximum(limit)               # the spin box cannot exceed the limit
        self.fm_limit_label.setText(f"{limit:.1f} dB ({source})")

    @QtCore.pyqtSlot(object)
    def _on_configured(self, params):
        self.params_label.setText(params.summary())
        monitor = params.waveform == "iq"
        self.tabs.setTabEnabled(0, not monitor)
        self.tabs.setTabEnabled(1, not monitor)
        if monitor:
            self.tabs.setCurrentIndex(2)

    def _display_latest(self):
        worker = self.worker
        if worker is None or self.state not in ("LIVE", "REPLAY", "SIMULATOR", "STOPPING"):
            return
        item = worker.latest.take()
        if item is not None:
            try:
                self.on_result(*item)
            except Exception as error:
                self._display_failed = True
                worker.request_stop()
                worker.latest.clear()
                self.error(f"Display failed: {type(error).__name__}: {error}")
                return
        saved = worker.recorder.saved_frames if worker.recorder is not None else 0
        self.counters_label.setText(
            f"Acquired {worker.acquired_frames} / {worker.acquired_samples:,} samples | "
            f"Handled {worker.processed_frames} | Saved {saved} blocks | "
            f"Drawn {worker.latest.consumed} | Preview replacements {worker.latest.superseded}")


    def _pick_file(self) -> None:
        start = str(self.captures_dir) if self.captures_dir.exists() else str(self.repo_root)
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open recording", start, "Captures (*.npz *.json)")
        if path:
            self.file_edit.setText(path)

    def log_line(self, text: str) -> None:
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")

    def set_state(self, state: str, detail: str = "") -> None:
        if state in ("STOPPED", "END OF FILE"):
            self._display_latest()  # Render the last frame of a short recording.
            if self._display_failed:
                state = "ERROR"
                detail = "Display failed; acquisition stopped"
        self.state = state
        self.state_label.setText(state)
        self.state_label.setStyleSheet(
            f"font-size: 20px; font-weight: bold; padding: 8px; border-radius: 6px; "
            f"background: {STATE_COLORS.get(state, '#9e9e9e')}; color: white;")
        if detail:
            self.detail_label.setText(detail)
            self.log_line(f"[{state}] {detail}")
        else:
            self.log_line(f"[{state}]")
        running = state in RUNNING_STATES
        self.start_btn.setEnabled(not running and state != "STOPPING")
        self.stop_btn.setEnabled(running)
        self._source_changed()
        if state == "STOPPED" and self._stop_requested_at is not None:
            ack = (time.perf_counter() - self._stop_requested_at) * 1e3
            self.log_line(f"stop completed {ack:.0f} ms after request")
            if getattr(self, "_armed_run_stopping", False):
                self.log_line("source closed; TX gain 0 dB was requested (maximum attenuation, "
                              "not a measurement that RF is off)")
        if state in ("STOPPED", "ERROR", "END OF FILE"):
            self._armed_run_stopping = False
            self._stop_requested_at = None
        if state in ("ERROR", "STOPPED", "END OF FILE"):
            self.fps_label.setText("")
            self.arm_check.setChecked(False)                # every transmitting run is armed afresh

    def error(self, msg: str) -> None:
        """Actionable error: state, log, dialog, and the display is cleared so a
        stale result can never be read as a current detection."""
        self.clear_display()
        self.set_state("ERROR", msg.splitlines()[0] if msg else "error")
        self.log_line(msg)
        if QtWidgets.QApplication.instance() and not getattr(self, "_headless", False):
            QtWidgets.QMessageBox.critical(self, "Acquisition error", msg)

    def clear_display(self) -> None:
        self._trail.clear()
        self.scope.update_detections([], [])
        self.raw_curve.setData([], []); self.raw_q_curve.setData([], []); self.spec_curve.setData([], [])
        self._floor_db = None
        self.trail_scatter.setData([], [])
        self.readout.setText("—")
        self.table.setRowCount(0)
        self.det_scatter.setData([], [])
        self.profile_curve.setData([], [])
        self.profile_thresh.setData([], [])
        self.rd_img.clear()

    # ---- configuration → objects ---------------------------------------------
    def build_params(self) -> RadarParams:
        if self.source_box.currentIndex() in B210_FMCW_SOURCES:
            ramp_ms = self.ramp_spin.value()
            if not 1 <= ramp_ms <= 20:
                raise ValueError("Ramp time must be 1–20 ms for the B210 FMCW mode")
            params = b210_fmcw.b210_fmcw_params(ramp_ms, self.fm_bw_spin.value())
            b210_fmcw.raw_params(params, self._fm_rate())    # raises if the sweep does not fit the rate
            if params.n_pulses < 8:
                raise ValueError(f"Ramp time {ramp_ms} ms leaves only {params.n_pulses} pulses per frame; use ≤ 12 ms")
            return params
        if self.source_box.currentIndex() == SRC_B210_RX:
            rate = self.sdr_rate.value() * 1e6
            return b210_rx_params(rate, self.sdr_freq.value()*1e9,
                                  n_samples=int(round(rate*0.1)))
        ramp_ms = self.ramp_spin.value()
        bw = self.bw_spin.value()
        if not 1 <= ramp_ms <= 65536:
            raise ValueError("Ramp time must be 1–65536 ms (kit limit)")
        if not 1.0 <= bw <= 100.0:
            raise ValueError("Sweep bandwidth must be 1–100 MHz (kit covers 2.4–2.5 GHz)")
        return cantenna_fmcw(ramp_ms, bw)

    def build_cfg(self, params: RadarParams | None = None) -> ProcessorConfig:
        if self.source_box.currentIndex() in B210_FMCW_SOURCES and params is not None:
            cfg = b210_fmcw.b210_fmcw_processor_config(
                params, pfa=float(self.pfa_box.currentText()), mti=self.mti_check.isChecked(),
                min_range_m=self.minrange_spin.value(), max_range_m=self.maxrange_spin.value(),
                min_speed_mps=self.minspeed_spin.value())
        else:
            cfg = ProcessorConfig(
                cfar=CfarConfig(pfa=float(self.pfa_box.currentText())),
                mti_order=1 if self.mti_check.isChecked() else 0,
                ramp_parity=1 if self.parity_box.currentIndex() == 0 else 0,
                min_range_m=self.minrange_spin.value(),
                max_range_m=self.maxrange_spin.value(),
                min_abs_velocity_mps=self.minspeed_spin.value())
        if cfg.min_range_m >= cfg.max_range_m:
            raise ValueError("Min range must be smaller than max range")
        return cfg

    def build_source_factory(self, params: RadarParams):
        kind = self.source_box.currentIndex()
        if kind == SRC_SIM:
            return (lambda: SimulatedRadar(params, SimConfig(
                targets=[Target(30.0, 1.5, 20), Target(12.0, -2.5, 15)], seed=0,
                frame_period_s=KIT_FRAME_PERIOD_S))), "SIMULATOR"
        if kind == SRC_REPLAY:
            path = Path(self.file_edit.text().strip())
            if not self.file_edit.text().strip():
                raise ValueError("Choose a recording (.npz) or session.json to replay")
            if not path.exists():
                raise FileNotFoundError(f"Recording not found: {path}")
            return (lambda: RecordedRadar(path, loop=False, realtime=True)), "REPLAY"
        if kind == SRC_KIT:
            def make():
                from ..hardware.cantenna import CantennaRDK
                return CantennaRDK(params)
            return make, "LIVE"
        if kind == SRC_B210_FMCW_SIM:
            rate = self._fm_rate()
            return (lambda: b210_fmcw.SimulatedB210Fmcw(params, raw_fs_hz=rate,
                                                        frame_period_s=0.5)), "SIMULATOR"
        if kind == SRC_B210_FMCW:
            limit, source = self._tx_limit()
            tx_gain, rx_gain = self.fm_txgain_spin.value(), self.fm_rxgain_spin.value()
            if tx_gain > limit + 1e-9:
                raise ValueError(f"TX gain {tx_gain:.1f} dB is above the limit {limit:.1f} dB ({source})")
            armed, rate = self.arm_check.isChecked(), self._fm_rate()
            serial = self.sdr_serial.text().strip()
            if armed:
                self.log_line(f"TRANSMITTER ARMED: TX gain {tx_gain:.1f} dB, limit {limit:.1f} dB ({source})")
            else:
                self.log_line("receive only: transmitter not armed, nothing will be transmitted")

            def make():
                return b210_fmcw.B210FmcwRadar(params, raw_fs_hz=rate, serial=serial,
                                               tx_gain_db=tx_gain, rx_gain_db=rx_gain,
                                               max_tx_gain_db=limit, transmit=armed)
            return make, "LIVE"
        serial, gain = self.sdr_serial.text().strip(), self.sdr_gain.value()
        return (lambda: B210ReceiveRadar(params, serial=serial, gain_db=gain)), "LIVE"

    # ---- start / stop --------------------------------------------------------
    def start(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        try:
            params = self.build_params()
            cfg = self.build_cfg(params)
            factory, kind = self.build_source_factory(params)
        except Exception as e:                            # noqa: BLE001
            self.error(f"Invalid configuration — {e}")
            return
        self.clear_display()
        self._display_failed = False
        record_to = None
        if self.record_check.isChecked():
            record_to = self.captures_dir / f"gui_{time.time_ns()}.session"
            self.log_line(f"recording to {record_to}")
            if params.waveform == "iq" and params.fs_hz > TESTED_RECORD_RATE_SPS:
                self.log_line(f"WARNING: recording at {params.fs_hz/1e6:g} MSPS is above the "
                              f"{TESTED_RECORD_RATE_SPS/1e6:g} MSPS verified rate; if the disk "
                              "cannot keep up the session is marked invalid.")
        self.params_label.setText(params.summary())
        self.worker = AcquisitionWorker(factory, params, cfg, record_to, label="gui")
        self.worker.source_kind = kind
        self.worker.configured.connect(self._on_configured)
        self.worker.state_changed.connect(self.set_state)
        self.worker.failed.connect(self.error)
        self._last_result_t = None
        self.worker.start()

    def _stopping_detail(self, text: str) -> str:
        """Stop only sets a flag: the capture in flight runs to its end (lead time + one frame,
        about 0.4 s) before the source is closed, and it does not call the driver's abort. So
        while an armed run is STOPPING the transmitter may still be on, and the display says so
        (Thomas Klopman's review of 12 Nov 2026, finding 5)."""
        armed = self.source_box.currentIndex() == SRC_B210_FMCW and self.arm_check.isChecked()
        self._armed_run_stopping = armed
        return text + ("; TRANSMITTER MAY STILL BE ON until it ends" if armed else "")

    def stop(self) -> None:
        if self.worker is None or not self.worker.isRunning():
            return
        self._stop_requested_at = time.perf_counter()
        self.set_state("STOPPING", self._stopping_detail("finishing the frame in flight"))
        self.worker.request_stop()

    def closeEvent(self, ev: QtGui.QCloseEvent) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.request_stop()
            self.set_state("STOPPING", self._stopping_detail("waiting for acquisition and recording to finish"))
            ev.ignore()
            QtCore.QTimer.singleShot(100, self.close)
            return
        self._display_timer.stop()
        ev.accept()

    # ---- results -------------------------------------------------------------
    def on_result(self, r: Result | None, frame: Frame) -> None:
        if self.state not in ("LIVE", "REPLAY", "SIMULATOR", "STOPPING"):
            return                                          # never paint a stale result over an error
        if r is None:
            self._draw_raw(frame)
            self.table.setRowCount(0)
            self.readout.setText("<b>Raw I/Q monitor</b><br>Range and velocity are not measured.")
            origin = "REPLAY" if frame.meta.get("replay") else "LIVE B210"
            self.detail_label.setText(f"{origin}: {frame.fs_hz/1e6:g} MSPS, RX2")
            self.fps_label.setText(f"frame {frame.seq} | peak {np.abs(frame.samples).max():.5f}")
            return
        src = r.diag.get("source_kind", "?")
        if frame.meta.get("replay"):
            src = f"REPLAY {Path(frame.meta.get('source_file', '')).name}"
        elif frame.meta.get("sim"):
            src = "SIMULATOR"
        else:
            src = f"LIVE {frame.meta.get('idn', '')[:28]}"
        # heat-map: colour scale pinned to the noise floor (smoothed across frames) so that
        # an empty room looks uniformly dark and anything real stands out; +35 dB = top
        db = r.rd_db
        floor = float(np.median(db))
        self._floor_db = floor if self._floor_db is None else 0.8 * self._floor_db + 0.2 * floor
        self.rd_img.setImage(db, levels=(self._floor_db + 3, self._floor_db + 38), autoLevels=False)
        x0, x1 = float(r.range_axis[0]), float(r.range_axis[-1])
        y0, y1 = float(r.velocity_axis[0]), float(r.velocity_axis[-1])
        self.rd_img.setRect(QtCore.QRectF(x0, y0, x1 - x0, y1 - y0))
        xmax = min(x1, self.maxrange_spin.value())
        self.rd_plot.setXRange(0, xmax, padding=0)
        self.rd_plot.setYRange(y0, y1, padding=0)
        self.profile_plot.setXRange(0, xmax, padding=0)
        self.txt_up.setPos(0.02 * xmax, y1)
        self.txt_dn.setPos(0.02 * xmax, y0)
        self.det_scatter.setData([d.range_m for d in r.detections], [d.velocity_mps for d in r.detections])
        if self.trail_check.isChecked():
            self._trail.extend((d.range_m, d.velocity_mps, d.extra.get("azimuth_deg")) for d in r.detections)
            self._trail = self._trail[-40:]
            self.trail_scatter.setData([t[0] for t in self._trail], [t[1] for t in self._trail])
        else:
            self._trail.clear()
            self.trail_scatter.setData([], [])
        # scope + raw tabs
        self.scope.set_geometry(self.maxrange_spin.value(), float(self.beam_spin.value()))
        self.scope.update_detections(r.detections, self._trail[:-len(r.detections)] if r.detections else self._trail)
        if self.tabs.currentIndex() == 2:
            self._draw_raw(frame)
        # range profile
        prof = db.max(axis=0)
        self.profile_curve.setData(r.range_axis, prof)
        thresh = 10 * np.log10(np.maximum(r.noise.max(axis=0) * r.diag.get("cfar_alpha", 1.0), 1e-20))
        self.profile_thresh.setData(r.range_axis, thresh)
        # plain-language readout
        if r.detections:
            d = max(r.detections, key=lambda k: k.snr_db)
            direction = "approaching" if d.velocity_mps > 0.15 else ("moving away" if d.velocity_mps < -0.15 else "crossing / slow")
            self.readout.setText(f"<b>{len(r.detections)} moving target{'s' if len(r.detections) > 1 else ''}</b><br>"
                                 f"strongest: <b>{d.range_m:.1f} m</b>, {direction} at <b>{abs(d.velocity_mps):.1f} m/s</b> "
                                 f"({d.snr_db:.0f} dB above the noise)")
        else:
            self.readout.setText("<b>No moving targets</b> this frame" if self.mti_check.isChecked()
                                 else "<b>No targets</b> this frame")
        # table
        self.table.setRowCount(len(r.detections))
        for i, d in enumerate(r.detections):
            vals = ["TARGET", f"{d.range_m:.1f}", f"{d.velocity_mps:+.2f}", f"{d.snr_db:.1f}", src]
            for j, v in enumerate(vals):
                item = QtWidgets.QTableWidgetItem(v)
                if j in (1, 2, 3):
                    item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(i, j, item)
        # status
        now = time.perf_counter()
        fps = f"{1 / (now - self._last_result_t):.1f} fps" if self._last_result_t else "—"
        self._last_result_t = now
        self.fps_label.setText(f"frame {frame.seq}  |  {fps}  |  DSP {r.diag.get('dsp_ms', 0):.0f} ms  |  "
                               f"align {r.diag.get('ramp_offset')}  |  {len(r.detections)} det  |  {src}")
        if frame.meta.get("signal_mode") == "fmcw_dechirped":
            tx = "TX ON" if frame.meta.get("tx_enabled") else "TX OFF (receive only)"
            lat, leak = frame.meta.get("latency_samples"), frame.meta.get("leakage_dbfs")
            extra = (f"{tx} | latency {lat if lat is not None else '—'} samples | "
                     f"leakage {leak:.1f} dBFS" if leak is not None else tx)
            self.detail_label.setText(f"{src}\n{extra}\nblind inside {self.minrange_spin.value():.0f} m | "
                                      f"{len(r.detections)} detection(s)")
        else:
            self.detail_label.setText(f"{src}\n{len(r.detections)} detection(s)")

    def _draw_raw(self, frame):
        # All acquisition samples go to recording. Only plotting is limited.
        x = frame.samples
        shown = x[:4096]
        self.raw_curve.setData(shown.real)
        self.raw_q_curve.setData(shown.imag if np.iscomplexobj(x) else [])
        segment = x[:8192]
        win = np.hanning(len(segment))
        scale = max(float(win.sum()), 1.0)
        if np.iscomplexobj(x):
            spectrum = np.fft.fftshift(np.fft.fft(segment*win))/scale
            frequency = np.fft.fftshift(np.fft.fftfreq(len(segment), 1/frame.fs_hz))
            if frame.params.waveform == "iq":
                frequency += frame.params.fc_hz
        else:
            spectrum = np.fft.rfft(segment*win)/scale
            frequency = np.fft.rfftfreq(len(segment), 1/frame.fs_hz)
        self.spec_curve.setData(frequency, 20*np.log10(np.abs(spectrum)+1e-12))
        self.spec_plot.setTitle("Preview spectrum (relative to full scale; not calibrated dBm)")


def main(argv=None) -> int:
    app = QtWidgets.QApplication(argv or sys.argv)
    root = Path(__file__).resolve().parents[2]
    w = MainWindow(repo_root=root)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
