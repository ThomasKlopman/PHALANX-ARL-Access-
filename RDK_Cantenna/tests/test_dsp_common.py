"""Shared DSP stages used by both Part 1 (kit) and Part 2 (B210) waveforms."""
import numpy as np
import pytest

from phalanx.dsp.detect import CfarConfig, ca_cfar, cfar_alpha


def test_cfar_false_alarm_rate_on_noise():
    rng = np.random.default_rng(0)
    pfa = 1e-3
    p = rng.exponential(1.0, size=(200, 2000)).astype(np.float32)   # square-law noise
    mask, noise, alpha = ca_cfar(p, CfarConfig(guard=(1, 2), train=(3, 8), pfa=pfa))
    measured = mask[10:-10, 20:-20].mean()
    assert 0.3 * pfa < measured < 3 * pfa
    assert alpha == pytest.approx(cfar_alpha((9 * 21) - (3 * 5), pfa))
