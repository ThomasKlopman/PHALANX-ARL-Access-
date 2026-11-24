"""AQ-01 acceptance: a saved acquisition replays with exactly the stored samples
and count, and its metadata carries everything needed to re-run the DSP."""
import json

import numpy as np
import pytest

from phalanx import (ProcessorConfig, RangeDopplerProcessor, RecordedRadar, SimConfig,
                     SimulatedPLFM, SimulatedRadar, Target, b210_plfm, cantenna_fmcw,
                     load_capture, save_capture)
from phalanx.hardware.simulator import PLFMSimConfig

REQUIRED_META = ["schema_version", "dtype", "timestamp_source", "label", "device", "driver",
                 "fs_hz", "center_frequency_hz", "bandwidth_hz", "waveform", "channels", "gain_db"]


def test_cantenna_capture_roundtrip_is_bit_exact(tmp_path):
    p = cantenna_fmcw()
    sim = SimulatedRadar(p, SimConfig(seed=0))
    frames = [sim.capture() for _ in range(3)]
    path = save_capture(tmp_path / "walk.npz", frames, p, label="walker_toward",
                        device="Pasternack Enterprises,PEM11002,5035,V1.3.1")
    back, p2, meta = load_capture(path)
    assert len(back) == 3 and p2 == p
    for a, b in zip(frames, back):
        np.testing.assert_array_equal(a.meta["raw"], b.meta["raw"])       # ADC words, exact
        np.testing.assert_array_equal(a.samples, b.samples)               # same decode
        assert b.meta["replay"] is True and b.seq == a.seq
    assert meta["label"] == "walker_toward" and meta["dtype"] == "uint16_offset_binary"
    for k in REQUIRED_META:
        assert k in meta, k


def test_sdr_capture_roundtrip_complex64(tmp_path):
    p = b210_plfm(fs_hz=5e6, bandwidth_hz=4e6, pulse_width_s=100e-6, prf_hz=2e3, n_pulses=8)
    frames = [SimulatedPLFM(p, PLFMSimConfig(seed=1)).capture()]
    path = save_capture(tmp_path / "sdr.npz", frames, p, gain_db=30.0, channels=1,
                        timestamp_source="device")
    back, p2, meta = load_capture(path)
    assert back[0].samples.dtype == np.complex64
    np.testing.assert_array_equal(back[0].samples, frames[0].samples)
    assert p2.waveform == "plfm" and p2.prf_hz == 2e3 and meta["gain_db"] == 30.0
    assert json.loads(np.load(path)["params_json"].item())["pulse_width_s"] == 100e-6


def test_recorded_radar_replays_through_same_interface(tmp_path):
    p = cantenna_fmcw()
    sim = SimulatedRadar(p, SimConfig(targets=[Target(25.0, 2.0, 20)], seed=3))
    frames = [sim.capture() for _ in range(4)]
    path = save_capture(tmp_path / "rec.npz", frames, p, label="test")
    with RecordedRadar(path) as src:
        src.configure()
        src.start()
        got = list(src.frames())
    assert len(got) == 4 and len(src) == 4 and src.label == "test"
    # DSP output is identical live vs replay (DSP-01: repeat → identical decisions)
    proc = RangeDopplerProcessor(p, ProcessorConfig(ramp_parity=0))
    live = [d.as_dict() for d in proc.process(frames[1]).detections]
    rep = [d.as_dict() for d in proc.process(got[1]).detections]
    assert live == rep


def test_recorded_radar_rejects_reconfigure_and_loops(tmp_path):
    p = cantenna_fmcw()
    path = save_capture(tmp_path / "r.npz", [SimulatedRadar(p, SimConfig(seed=0)).capture()], p)
    src = RecordedRadar(path, loop=True)
    with pytest.raises(ValueError):
        src.configure(cantenna_fmcw(ramp_time_ms=8))
    src.start()
    assert len(list(src.frames(5))) == 5          # loops instead of stopping


def test_legacy_calibrate_files_still_load(tmp_path):
    p = cantenna_fmcw()
    raw = np.stack([SimulatedRadar(p, SimConfig(seed=0)).capture().meta["raw"] for _ in range(2)])
    legacy = tmp_path / "edge_20261008_112823.npz"
    np.savez(legacy, raw=raw, fs=20000.0, ramp_time_s=0.004, f_start_hz=2.4e9, f_stop_hz=2.5e9,
             sweep="triangle", idn="Pasternack")
    frames, p2, meta = load_capture(legacy)
    assert len(frames) == 2 and p2.n_fast == 80 and meta["schema_version"] == 0
