"""tx_ramp_calibration.py against a simulated radar: budget arithmetic, tone measurement,
arming, the stops (budget, linearity, peak), coupling from a loopback reference, --extend."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tx_ramp_calibration.py"
spec = importlib.util.spec_from_file_location("tx_ramp_calibration", SCRIPT)
ramp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ramp)


class FakeRadar:
    """Tone level (dBFS) = gain - 89.8 + 10 (true max output) - path loss + 25 (receiver)."""
    B210_TX_GAIN_RANGE_DB = (0.0, 89.8)
    path_loss_db = 31.0          # set per test: attenuator + cable, or the cans' coupling
    compress_above_db = None     # gains above this add nothing (breaks linearity)
    tx_captures = []             # every gain actually transmitted, across instances

    def __init__(self, params, **kw):
        self.params, self.kw = params, kw
        self.gain = kw["tx_gain_db"]
        self.cap = kw["max_tx_gain_db"]
        self.info = {"model": "B210", "tx_gain_range_db": self.B210_TX_GAIN_RANGE_DB}
    def __enter__(self): return self
    def __exit__(self, *exc): pass
    def configure(self): pass
    def set_tx_gain(self, g):
        if g > self.cap:
            raise ValueError("over the driver cap")
        self.gain = float(g)
        return self.gain
    def capture(self, transmit=None):
        p = self.params
        n = p.n_samples
        rng = np.random.default_rng(0)
        x = 1e-4 / np.sqrt(2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
        if transmit:
            type(self).tx_captures.append(self.gain)
            g = self.gain
            if self.compress_above_db is not None:
                g = min(g, self.compress_above_db)
            dbfs = g - 89.8 + 10.0 - self.path_loss_db + 25.0
            t = np.arange(n) / p.fs_hz
            x = x + 10 ** (dbfs / 20) * np.exp(2j * np.pi * ramp.TONE_OFFSET_HZ * t)
        return SimpleNamespace(samples=x.astype(np.complex64),
                               meta={"rx_errors": [], "tx_errors": [], "tx_enabled": bool(transmit)})


@pytest.fixture
def sim(monkeypatch, tmp_path):
    monkeypatch.setattr(ramp, "UsrpRadar", FakeRadar)
    monkeypatch.setattr(ramp, "captures_dir", lambda: tmp_path)
    FakeRadar.tx_captures = []
    FakeRadar.path_loss_db = 31.0
    FakeRadar.compress_above_db = None
    return tmp_path


def run(*argv):
    return ramp.main(["--settle", "0", *argv])


def newest(folder, mode):
    return json.loads(sorted(folder.glob(f"txramp_{mode}_*.json"))[-1].read_text(encoding="utf-8"))


ARM = ["--arm", "--accepted-by", "T. Klopman"]


def test_budget_arithmetic():
    assert ramp.highest_gain_within_budget(89.8) == pytest.approx(44.8)
    assert ramp.rx_power_bound_dbm(44.8, 89.8) == pytest.approx(-25.0)
    assert ramp.rx_power_bound_dbm(60, 89.8, coupling_credit_db=20) == pytest.approx(-29.8)
    assert ramp.highest_gain_within_budget(89.8, coupling_credit_db=10) == pytest.approx(54.8)
    assert ramp.plan_gains(0, 44.8, 2)[-1] == 44.0 and ramp.plan_gains(50, 44, 2) == []


def test_tone_level_is_amplitude_accurate():
    fs, n = 4e6, 200_000
    t = np.arange(n) / fs
    x = 0.01 * np.exp(2j * np.pi * (ramp.TONE_OFFSET_HZ + 300) * t)
    level, found = ramp.tone_level_dbfs(x, fs)
    assert level == pytest.approx(-40.0, abs=1.6)          # worst-case Hann scalloping is 1.4 dB
    assert abs(found - (ramp.TONE_OFFSET_HZ + 300)) < 30


def test_nothing_transmits_without_arm(sim):
    assert run("--mode", "cans") == 0
    assert FakeRadar.tx_captures == []
    assert "not armed" in newest(sim, "cans")["stopped_because"]


def test_arm_needs_a_named_acceptor_and_loopback_needs_an_attenuator(sim):
    with pytest.raises(SystemExit):
        run("--mode", "cans", "--arm")
    with pytest.raises(SystemExit):
        run("--mode", "loopback", "--attenuator-db", "12", *ARM)
    with pytest.raises(SystemExit):
        run("--mode", "cans", "--extend", *ARM)            # --extend without a reference
    assert FakeRadar.tx_captures == []


def test_cans_without_reference_never_exceeds_the_no_credit_limit(sim):
    FakeRadar.path_loss_db = 37.0
    assert run("--mode", "cans", "--stop", "60", *ARM) == 0   # asking for 60 does not get 60
    assert max(FakeRadar.tx_captures) == 44.0
    assert FakeRadar.tx_captures == sorted(FakeRadar.tx_captures)          # monotonic ramp
    assert np.allclose(np.diff(FakeRadar.tx_captures), 2.0)                # 2 dB steps from 0
    log = newest(sim, "cans")
    assert log["accepted_by"] == "T. Klopman" and log["coupling"] is None
    assert log["recommended_max_tx_gain_db"] == 44.0
    assert all(s["bound_at_rx_dbm"] <= -25.0 for s in log["steps"])


def test_loopback_then_cans_measures_coupling(sim):
    FakeRadar.path_loss_db = 31.0                           # 30 dB pad + 1 dB of cable
    assert run("--mode", "loopback", "--attenuator-db", "30", *ARM) == 0
    FakeRadar.path_loss_db = 37.0                           # the cans
    FakeRadar.tx_captures = []
    assert run("--mode", "cans", "--reference", "latest", *ARM) == 0
    log = newest(sim, "cans")
    # 30 + (loopback - cans) = 30 + 6 = 36: the loopback cable makes it 1 dB conservative
    assert log["coupling"]["min_db"] == pytest.approx(36.0, abs=0.3)
    assert log["coupling"]["budget_cap_db"] == 70.0         # 44.8 + 33 credit, held by the ceiling
    assert max(FakeRadar.tx_captures) == 44.0               # no --extend: nothing above the limit
    assert log["recommended_max_tx_gain_db"] == 44.0        # only what was ramped through


def test_extend_ramps_to_the_budget_cap_and_no_further(sim):
    assert run("--mode", "loopback", "--attenuator-db", "30", *ARM) == 0
    FakeRadar.path_loss_db = 12.0                           # cans close together: little isolation
    FakeRadar.tx_captures = []
    assert run("--mode", "cans", "--reference", "latest", "--extend", *ARM) == 0
    log = newest(sim, "cans")
    # coupling 30 + (−31 − (−12)) = 11 → credit 8 → 52.8 → 52 on the 2 dB grid
    assert log["coupling"]["budget_cap_db"] == 52.0
    assert max(FakeRadar.tx_captures) == 52.0
    assert log["recommended_max_tx_gain_db"] == 52.0
    assert all(s["bound_at_rx_dbm"] <= -25.0 for s in log["steps"])


def test_loopback_with_24_db_stays_far_inside_the_budget(sim):
    FakeRadar.path_loss_db = 25.0                           # two 12 dB pads + 1 dB of cable
    assert run("--mode", "loopback", "--attenuator-db", "24", *ARM) == 0
    log = newest(sim, "loopback")
    assert max(FakeRadar.tx_captures) == 44.0
    # the logged bound takes no credit for the pads; with them it is 24 dB lower still
    assert max(s["bound_at_rx_dbm"] for s in log["steps"]) - 24.0 <= -45.0


def test_linearity_failure_stops_the_ramp(sim):
    FakeRadar.compress_above_db = 20.0
    assert run("--mode", "cans", *ARM) == 1
    log = newest(sim, "cans")
    assert "linearity" in log["stopped_because"]
    assert max(FakeRadar.tx_captures) == 22.0               # the first step that failed to add 2 dB
    assert log["recommended_max_tx_gain_db"] is None


def test_peak_stop(sim):
    FakeRadar.path_loss_db = -45.0                          # absurdly strong coupling
    assert run("--mode", "cans", *ARM) == 1
    assert "stop-peak" in newest(sim, "cans")["stopped_because"]
    assert max(FakeRadar.tx_captures) < 44.0


def test_reference_must_match_settings(sim):
    assert run("--mode", "loopback", "--attenuator-db", "30", *ARM) == 0
    with pytest.raises(SystemExit):
        run("--mode", "cans", "--reference", "latest", "--rx-gain", "30", *ARM)
