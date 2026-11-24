"""Shared transmit budget: arithmetic and the calibrated gain limit read from a ramp log."""
import json

import pytest

from phalanx.tx_budget import (NO_CREDIT_LIMIT_DB, gain_limit, highest_gain_within_budget,
                               rx_power_bound_dbm)


def test_budget_numbers():
    assert NO_CREDIT_LIMIT_DB == pytest.approx(44.8)
    assert rx_power_bound_dbm(44.8) == pytest.approx(-25.0)
    assert highest_gain_within_budget(coupling_credit_db=26.8) == pytest.approx(71.6)
    assert rx_power_bound_dbm(70.0, coupling_credit_db=26.8) == pytest.approx(-26.6)


def test_gain_limit_from_calibration_logs(tmp_path):
    limit, source = gain_limit("")
    assert limit == pytest.approx(44.8) and source == "no-credit budget limit"
    with pytest.raises(FileNotFoundError):
        gain_limit("latest", tmp_path)
    (tmp_path / "txramp_cans_1.json").write_text(json.dumps(
        {"mode": "cans", "recommended_max_tx_gain_db": None}))          # a run that stopped early
    (tmp_path / "txramp_cans_broken.json").write_text("{not json")
    with pytest.raises(FileNotFoundError):
        gain_limit("latest", tmp_path)
    (tmp_path / "txramp_cans_2.json").write_text(json.dumps(
        {"mode": "cans", "recommended_max_tx_gain_db": 70.0}))
    assert gain_limit("latest", tmp_path) == (70.0, "calibration txramp_cans_2.json")
    loop = tmp_path / "txramp_loopback_3.json"
    loop.write_text(json.dumps({"mode": "loopback", "recommended_max_tx_gain_db": None}))
    with pytest.raises(ValueError):
        gain_limit(str(loop))
