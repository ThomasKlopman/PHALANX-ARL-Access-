"""Part 1 — Pasternack Cantenna kit (real-IF triangle FMCW): ADC decode, sweep
alignment, ramp parity, and the FMCW range-Doppler chain on kit-like data.
Run all Phase 1 tests with `pytest -q` from RDK_Cantenna/."""
import numpy as np
import pytest

from phalanx import (CWProcessor, ProcessorConfig, RangeDopplerProcessor, SimConfig,
                     SimulatedRadar, Target, cantenna_cw, cantenna_fmcw)
from phalanx.dsp.conditioning import estimate_ramp_offset
from phalanx.hardware.codec import decode_hex_words, raw_to_float


# ---------------------------------------------------------------- driver bits

def test_decode_hex_words_ignores_terminators():
    txt = "0000 7FFF\r\n8000FFFF\n"
    np.testing.assert_array_equal(decode_hex_words(txt), [0, 0x7FFF, 0x8000, 0xFFFF])


def test_signedness_autodetect():
    sig = (0.3 * np.sin(np.linspace(0, 50, 1000)) * 32767).astype(np.int32)
    unsigned = (sig + 32768).astype(np.uint16)
    signed = sig.astype(np.int16).view(np.uint16)
    xu, su = raw_to_float(unsigned)
    xs, ss = raw_to_float(signed)
    assert su == "unsigned" and ss == "signed"
    np.testing.assert_allclose(xu, xs, atol=1e-4)


# ---------------------------------------------------------------- params

def test_param_derivations():
    p = cantenna_fmcw(ramp_time_ms=4, bandwidth_mhz=100)
    assert p.n_fast == 80
    assert p.n_pulses == 25                       # 4096 // (2*80)
    assert p.range_resolution_m == pytest.approx(1.499, abs=1e-2)
    assert p.max_range_m == pytest.approx(59.96, abs=0.1)
    assert p.max_velocity_mps == pytest.approx(0.1224 / (4 * 8e-3), rel=1e-2)
    assert p.range_axis()[1] == pytest.approx(p.range_resolution_m, rel=1e-6)
    v = p.velocity_axis()
    assert v[len(v) // 2] == 0.0


# ---------------------------------------------------------------- alignment

def _parity_from_truth(true_offset: int, est_offset: int, n_fast: int) -> int:
    """Which parity makes rows start on up-ramps, given the estimate (±2 samples)."""
    d = (true_offset - est_offset) % (2 * n_fast)
    return 0 if (d <= 2 or d >= 2 * n_fast - 2) else 1


@pytest.mark.parametrize("seed", range(5))
@pytest.mark.parametrize("glitch", [0.0, 0.3])
def test_ramp_offset_estimate(seed, glitch):
    """Symmetry alignment must work with AND without a turnaround transient
    (the real kit, s/n 5035, shows none)."""
    p = cantenna_fmcw()
    sim = SimulatedRadar(p, SimConfig(seed=seed, glitch_amp=glitch))
    f = sim.capture()
    al = estimate_ramp_offset(f.samples, p.n_fast)
    d = (f.meta["true_offset"] - al.offset) % p.n_fast
    assert d <= 2 or d >= p.n_fast - 2, (f.meta["true_offset"], al.offset)
    assert al.confidence > 0.8
    if glitch:   # transient method should agree when there is a transient
        d2 = (al.alt_offset - al.offset) % p.n_fast
        assert d2 <= 2 or d2 >= p.n_fast - 2


def test_ramp_offset_low_confidence_on_noise():
    p = cantenna_fmcw()
    sim = SimulatedRadar(p, SimConfig(targets=[], clutter=[], glitch_amp=0.0, seed=0))
    al = estimate_ramp_offset(sim.capture().samples, p.n_fast)
    assert al.confidence < 0.5


# ---------------------------------------------------------------- end to end

@pytest.mark.parametrize("seed", range(4))
def test_two_targets_detected_with_correct_range_and_velocity(seed):
    p = cantenna_fmcw()
    truth = [Target(30.0, 1.5, 20.0), Target(12.0, -2.5, 18.0)]
    sim = SimulatedRadar(p, SimConfig(targets=truth, seed=seed))
    f = sim.capture()
    proc = RangeDopplerProcessor(p)
    off, _ = proc.align(f.samples - f.samples.mean())
    proc.cfg.ramp_parity = _parity_from_truth(f.meta["true_offset"], off, p.n_fast)
    r = proc.process(f)
    assert len(r.detections) >= 2
    for t in truth:
        best = min(r.detections, key=lambda d: abs(d.range_m - t.range_m))
        assert abs(best.range_m - t.range_m) < p.range_resolution_m, (t, best)
        assert abs(best.velocity_mps - t.velocity_mps) < p.velocity_resolution_mps, (t, best)
        assert best.snr_db > 15


def test_wrong_parity_flips_velocity_sign_only():
    p = cantenna_fmcw()
    sim = SimulatedRadar(p, SimConfig(targets=[Target(25.0, 2.0, 20)], seed=3))
    f = sim.capture()
    proc = RangeDopplerProcessor(p)
    off, _ = proc.align(f.samples - f.samples.mean())
    good = _parity_from_truth(f.meta["true_offset"], off, p.n_fast)
    v = []
    for parity in (good, 1 - good):
        proc.cfg.ramp_parity = parity
        v.append(proc.process(f).detections[0].velocity_mps)
    assert v[0] == pytest.approx(2.0, abs=0.35)
    assert v[1] == pytest.approx(-2.0, abs=0.35)


def test_both_segments_mode_doubles_unambiguous_velocity():
    p = cantenna_fmcw(segment_select="both")
    assert p.n_pulses == 51 and p.pri_s == pytest.approx(4e-3)
    assert p.max_velocity_mps == pytest.approx(2 * cantenna_fmcw().max_velocity_mps)
    sim = SimulatedRadar(p, SimConfig(targets=[Target(20.0, 5.0, 20)], seed=0))
    f = sim.capture()
    proc = RangeDopplerProcessor(p)
    off, _ = proc.align(f.samples - f.samples.mean())
    proc.cfg.ramp_parity = _parity_from_truth(f.meta["true_offset"], off, p.n_fast)
    r = proc.process(f)
    best = max(r.detections, key=lambda d: d.snr_db)
    assert abs(best.range_m - 20.0) < 2 * p.range_resolution_m
    assert abs(best.velocity_mps - 5.0) < 2 * p.velocity_resolution_mps


def test_mti_removes_static_clutter():
    p = cantenna_fmcw()
    sim = SimulatedRadar(p, SimConfig(targets=[], clutter=[Target(10.0, 0.0, 40)], seed=0))
    f = sim.capture()
    r = RangeDopplerProcessor(p, ProcessorConfig(mti_order=1)).process(f)
    assert all(abs(d.velocity_mps) > 0.3 for d in r.detections)  # nothing at zero Doppler
    r0 = RangeDopplerProcessor(p, ProcessorConfig(mti_order=0)).process(f)
    assert any(abs(d.range_m - 10) < 2 and abs(d.velocity_mps) < 0.3 for d in r0.detections)


def test_cw_mode_reports_unsigned_speed():
    p = cantenna_cw()
    sim = SimulatedRadar(p, SimConfig(targets=[Target(10.0, -1.8, 25)], clutter=[], seed=0))
    f = sim.capture()
    r = CWProcessor(p).process(f)
    assert r.detections, "no CW detection"
    assert r.detections[0].velocity_mps == pytest.approx(1.8, abs=0.1)
    assert np.isnan(r.detections[0].range_m)
