"""Part 2 — pulsed LFM at complex baseband (the B210 transmit-and-receive radar,
and the Phase 2 CN0566 chain): matched filter, signed Doppler, leakage removal.
These run on simulated echoes only until the B210 transmits."""
import numpy as np
import pytest

from phalanx import ProcessorConfig, RangeDopplerProcessor, Target
from phalanx import SimulatedPLFM, b210_plfm
from phalanx.hardware.simulator import PLFMSimConfig


def _plfm_test_params(**kw):
    """Small enough to run in well under a second, realistic enough to mean something:
    16 MHz → 9.4 m range res; 64 pulses at 2 kHz → 1.9 m/s velocity res."""
    base = dict(fs_hz=20e6, bandwidth_hz=16e6, pulse_width_s=100e-6, prf_hz=2e3, n_pulses=64)
    base.update(kw)
    return b210_plfm(**base)


def test_reference_chirp_is_unit_amplitude_lfm():
    p = _plfm_test_params()
    s = p.reference_chirp()
    assert s.dtype == np.complex64 and len(s) == 2000
    np.testing.assert_allclose(np.abs(s), 1.0, atol=1e-5)
    inst_f = np.diff(np.unwrap(np.angle(s))) * p.fs_hz / (2 * np.pi)
    assert inst_f[0] == pytest.approx(-p.bandwidth_hz / 2, rel=0.02)
    assert inst_f[-1] == pytest.approx(+p.bandwidth_hz / 2, rel=0.02)


@pytest.mark.parametrize("seed", range(3))
def test_plfm_targets_detected_with_correct_range_and_signed_velocity(seed):
    p = _plfm_test_params()
    truth = [Target(45.0, 5.0, 25.0), Target(150.0, -8.0, 20.0)]
    sim = SimulatedPLFM(p, PLFMSimConfig(targets=truth, seed=seed))
    proc = RangeDopplerProcessor(p, ProcessorConfig(min_range_m=20.0, max_range_m=400.0))
    r = proc.process(sim.capture())
    assert r.diag["ramp_offset"] == 0                    # SDR frames start on a pulse edge
    for t in truth:
        best = min(r.detections, key=lambda d: abs(d.range_m - t.range_m), default=None)
        assert best is not None, (t, r.detections)
        assert abs(best.range_m - t.range_m) < p.range_resolution_m, (t, best)
        assert abs(best.velocity_mps - t.velocity_mps) < p.velocity_resolution_mps, (t, best)
        assert best.snr_db > 12


def test_plfm_leakage_is_removed_by_mti_not_reported():
    p = _plfm_test_params()
    sim = SimulatedPLFM(p, PLFMSimConfig(targets=[], leakage_db=50.0, seed=1))
    r = RangeDopplerProcessor(p, ProcessorConfig(min_range_m=20.0, max_range_m=400.0)).process(sim.capture())
    assert all(d.snr_db < 15 for d in r.detections), r.detections   # nothing but CFAR noise hits


def test_b210_default_params_are_sane():
    p = b210_plfm()
    assert p.fs_hz == 30.72e6 and p.n_fast == 30720 and p.n_pulses == 128
    assert p.range_resolution_m == pytest.approx(6.0, abs=0.01)
    assert 0.4 < p.velocity_resolution_mps < 0.5
    assert p.duty_cycle == pytest.approx(0.5)
    assert p.bandwidth_hz < p.fs_hz               # chirp fits inside the complex band