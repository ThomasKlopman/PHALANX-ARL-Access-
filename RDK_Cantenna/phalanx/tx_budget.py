"""Transmit attenuation budget for the B210 + cans, shared by the scripts and the GUI.

The B210's RF inputs must never see more than -15 dBm (UHD manual). The budget bounds the power
at an RX connector from the TX gain setting under two assumptions:
  (a) the board never puts out more than P_MAX_DBM at full TX gain (the TX connector maximum
      listed in the UHD B2x0 manual);
  (b) TX gain steps are accurate relative to each other (checked on every step of the ramp).
  power at RX  <=  P_MAX_DBM - (gain_max - gain) - fixed attenuation - coupling credit,
and it has to stay  <=  RX_LIMIT_DBM - MARGIN_DB = -25 dBm.

With no credit that allows gain_max - 45 dB (44.8 dB on the B210). A completed ramp calibration
(Part2_B210_Cantenna/scripts/tx_ramp_calibration.py) measures the can-to-can coupling and raises
the limit for that can arrangement only. This is an engineering justification, not a measurement
of power; see Part2_B210_Cantenna/docs/usrp_damage_review_2026-11-05.md.
"""
from __future__ import annotations

import json
from pathlib import Path

from .paths import captures_dir

P_MAX_DBM = 20.0               # assumption (a): worst-case output at full TX gain
RX_LIMIT_DBM = -15.0           # UHD manual: never more than this into any RF input
MARGIN_DB = 10.0               # stay this far below the limit
B210_TX_GAIN_MAX_DB = 89.8


def rx_power_bound_dbm(gain_db, gain_max_db=B210_TX_GAIN_MAX_DB, fixed_atten_db=0.0,
                       coupling_credit_db=0.0):
    """Upper bound on the power at an RX connector at TX gain `gain_db` (assumptions a, b)."""
    return P_MAX_DBM - (gain_max_db - gain_db) - fixed_atten_db - coupling_credit_db


def highest_gain_within_budget(gain_max_db=B210_TX_GAIN_MAX_DB, fixed_atten_db=0.0,
                               coupling_credit_db=0.0):
    """Largest TX gain whose bound is still <= RX_LIMIT_DBM - MARGIN_DB."""
    return (gain_max_db - (P_MAX_DBM - (RX_LIMIT_DBM - MARGIN_DB))
            + fixed_atten_db + coupling_credit_db)


NO_CREDIT_LIMIT_DB = highest_gain_within_budget()


def gain_limit(calibration: str = "", folder: Path | None = None) -> tuple[float, str]:
    """(TX gain limit in dB, where it comes from).

    calibration ""       -> the no-credit budget limit.
    calibration "latest" -> the newest completed txramp_cans_*.json in `folder`
                            (default PHALANX_data/captures).
    calibration <path>   -> that log.
    The limit from a log is the operating cap that ramp actually reached, and it is valid only
    while the cans stay where they were when it was measured."""
    if not calibration:
        return NO_CREDIT_LIMIT_DB, "no-credit budget limit"
    if calibration == "latest":
        folder = Path(folder) if folder is not None else captures_dir()
        found = []
        for q in sorted(folder.glob("txramp_cans_*.json"), key=lambda q: q.stat().st_mtime):
            try:
                if json.loads(q.read_text(encoding="utf-8")).get("recommended_max_tx_gain_db"):
                    found.append(q)
            except (OSError, ValueError):
                continue
        if not found:
            raise FileNotFoundError(f"no completed txramp_cans_*.json in {folder}")
        path = found[-1]
    else:
        path = Path(calibration)
    log = json.loads(path.read_text(encoding="utf-8"))
    cap = log.get("recommended_max_tx_gain_db")
    if log.get("mode") != "cans" or not cap:
        raise ValueError(f"{path} is not a completed cans calibration")
    return float(cap), f"calibration {path.name}"
