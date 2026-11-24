r"""Slow TX ramp calibration for the B210 + cans (the "calibration" card; replaces guessing a gain).

Nothing is transmitted without --arm. Read docs/usrp_damage_review_2026-11-05.md first.

    # 1. reference: TX/RX --> fixed SMA attenuator(s), >= 20 dB in total --> RX2, by cable (no cans)
    #    (two 12 dB parts in series = 24)
    python Part2_B210_Cantenna\scripts\tx_ramp_calibration.py --mode loopback --attenuator-db 24 --arm --accepted-by "T. Klopman"
    # 2. the cans, same settings: measures can-to-can coupling against that reference
    python Part2_B210_Cantenna\scripts\tx_ramp_calibration.py --mode cans --reference latest --arm --accepted-by "T. Klopman"
    # 3. only if step 2 printed a cap above 44 dB: continue the ramp up to that cap
    python Part2_B210_Cantenna\scripts\tx_ramp_calibration.py --mode cans --reference latest --extend --arm --accepted-by "T. Klopman"

Step 2 also runs without --reference (no attenuator on hand yet): it then ramps only as far as
the budget allows with no credit for the cans' separation, and reports no coupling.

The ramp: a tone, TX gain from 0 dB upward in 2 dB steps, one capture per step, a pause between
steps. At each step the script measures the received tone level (dBFS) and stops at once if
  * UHD reports any RX or TX error (the driver fails the capture and attenuates);
  * the received peak reaches --stop-peak of ADC full scale (clipping is not damage, but the
    readings stop being usable);
  * the level does not rise by the step size within --linearity-tol dB (the relative accuracy of
    the gain steps is one of the two assumptions below, so it is checked on every step);
  * the next step would exceed the attenuation budget. That step is never taken.

The attenuation budget (an engineering justification, NOT a measurement -- the teammate named in
--accepted-by is accepting its two assumptions, and the name is written to the log):
  (a) the board never puts out more than P_MAX_DBM = +20 dBm at full TX gain (the TX connector
      maximum listed in the UHD B2x0 manual);
  (b) TX gain steps are accurate relative to each other, so at gain g the output is at most
      P_MAX_DBM - (gain_max - g).
  Power at an RX connector <= P_MAX_DBM - (gain_max - g) - fixed attenuation - coupling credit,
  and it must stay <= RX_LIMIT_DBM - MARGIN_DB = -25 dBm.
With no attenuator and no coupling credit that allows g <= gain_max - 45 (44.8 dB on the B210):
below that, even a bare cable from TX/RX to RX2 would be inside the margin. Going higher needs
the measured coupling: C = attenuator + (loopback level - cans level) at the same gain, taken as
the smallest value seen, minus --coupling-uncertainty-db. The cap is then 44.8 + credit, and never
above the driver's absolute ceiling. If the cans move, repeat steps 2-3.

A spectrum analyser on the RX can's cable is stronger evidence than this budget; use one if the
lab has it. The JSON log of every step goes to PHALANX_data\captures.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # RDK_Cantenna/ (the phalanx package)

from phalanx.hardware.usrp import (UsrpRadar, first_contact_params, TONE_OFFSET_HZ,   # noqa: E402
                                   ABSOLUTE_TX_GAIN_CEILING_DB, BRINGUP_NUM_SEND_FRAMES)
from phalanx.paths import captures_dir                                               # noqa: E402
from phalanx.tx_budget import (P_MAX_DBM, RX_LIMIT_DBM, MARGIN_DB,                   # noqa: E402,F401
                               rx_power_bound_dbm, highest_gain_within_budget)

MIN_SNR_DB = 10.0              # a step counts as a valid reading above this tone-to-floor ratio
# Minimum in-line attenuation for the cabled loopback in THIS script. The loopback ramp never
# goes above the no-credit limit (44.8 dB), where the budget already bounds a bare cable at
# -25 dBm; 20 dB in line puts the bound at -45 dBm or lower. This is not the general rule: a
# loopback at higher TX gain or full power needs 30 dB or more (Ettus guidance, T. Klopman's
# review) -- never reuse this cabling with other commands at higher gain.
MIN_LOOPBACK_ATTEN_DB = 20.0


def plan_gains(start_db, stop_db, step_db):
    """start, start+step, ... never above stop."""
    if stop_db < start_db:
        return []
    return [float(g) for g in np.round(np.arange(start_db, stop_db + 1e-9, step_db), 2)]


# --- measurement ------------------------------------------------------------------------------
def tone_level_dbfs(samples, fs_hz, tone_hz=TONE_OFFSET_HZ, search_hz=2e3):
    """(level in dB relative to ADC full scale, frequency found) of the strongest line within
    +-search_hz of the transmitted tone. Hann-windowed FFT, amplitude-normalised."""
    x = np.asarray(samples)
    n = len(x)
    w = np.hanning(n)
    spec = np.fft.fft(x * w)
    freqs = np.fft.fftfreq(n, 1.0 / fs_hz)
    idx = np.flatnonzero(np.abs(freqs - tone_hz) <= search_hz)
    k = idx[int(np.argmax(np.abs(spec[idx])))]
    amp = float(np.abs(spec[k]) / w.sum())
    return 20.0 * np.log10(max(amp, 1e-12)), float(freqs[k])


def coupling_losses_db(reference_steps, cans_steps, attenuator_db):
    """Per-gain can-to-can coupling loss: attenuator + (loopback level - cans level), for gains
    where both runs have a valid reading. Loopback cable loss makes this smaller, i.e. conservative."""
    ref = {round(s["tx_gain_db"], 2): s["level_dbfs"] for s in reference_steps if s.get("valid")}
    out = {}
    for s in cans_steps:
        g = round(s["tx_gain_db"], 2)
        if s.get("valid") and g in ref:
            out[g] = attenuator_db + (ref[g] - s["level_dbfs"])
    return out


def load_reference(spec):
    """A loopback log: a path, or 'latest' for the newest txramp_loopback_*.json in captures."""
    if spec == "latest":
        found = sorted(captures_dir().glob("txramp_loopback_*.json"), key=lambda p: p.stat().st_mtime)
        if not found:
            raise FileNotFoundError(f"no txramp_loopback_*.json in {captures_dir()}")
        path = found[-1]
    else:
        path = Path(spec)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("mode") != "loopback":
        raise ValueError(f"{path} is not a loopback reference")
    if not str(data.get("stopped_because", "")).startswith("ramp completed"):
        raise ValueError(f"{path}: the loopback run did not complete ({data.get('stopped_because')})")
    if sum(1 for s in data["steps"] if s.get("valid")) < 3:
        raise ValueError(f"{path}: fewer than three valid readings")
    data["path"] = str(path)
    return data


# --- the ramp ---------------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["loopback", "cans"], required=True)
    parser.add_argument("--attenuator-db", type=float, default=0.0,
                        help="loopback: total value of the fixed in-line attenuator(s), dB (>= 20)")
    parser.add_argument("--reference", default="",
                        help="cans: loopback log to measure coupling against (path or 'latest')")
    parser.add_argument("--extend", action="store_true",
                        help="cans + reference: continue above the no-credit limit up to the budget cap")
    parser.add_argument("--serial", default="")
    parser.add_argument("--rate", type=float, default=4e6)
    parser.add_argument("--bandwidth", type=float, default=3e6)
    parser.add_argument("--rx-gain", type=float, default=40.0,
                        help="RX gain, dB. Must match between the loopback and cans runs.")
    parser.add_argument("--start", type=float, default=0.0, help="first TX gain, dB")
    parser.add_argument("--stop", type=float, default=None,
                        help="last TX gain, dB (default: as far as the budget allows)")
    parser.add_argument("--step", type=float, default=2.0, help="TX gain step, dB (0.2 to 5)")
    parser.add_argument("--settle", type=float, default=0.5, help="pause between steps, s")
    parser.add_argument("--stop-peak", type=float, default=0.5,
                        help="stop when the received peak reaches this fraction of full scale")
    parser.add_argument("--linearity-tol", type=float, default=1.5,
                        help="allowed error, dB, between a gain step and the change it produces")
    parser.add_argument("--coupling-uncertainty-db", type=float, default=3.0,
                        help="subtracted from the measured coupling before it is credited")
    parser.add_argument("--out", default="", help="JSON log path (default: PHALANX_data/captures)")
    parser.add_argument("--arm", action="store_true",
                        help="ARM THE TRANSMITTER. Without it only the noise floor is recorded.")
    parser.add_argument("--accepted-by", default="",
                        help="teammate who accepted the budget assumptions (required with --arm; logged)")
    args = parser.parse_args(argv)

    if not 0.2 <= args.step <= 5.0:
        parser.error("--step must be between 0.2 and 5 dB")
    if args.start < 0:
        parser.error("--start must be >= 0")
    if not 0 < args.stop_peak <= 0.9:
        parser.error("--stop-peak must be in (0, 0.9]")
    if args.arm and not args.accepted_by.strip():
        parser.error("--arm needs --accepted-by: the name of the teammate who accepted the "
                     "attenuation-budget assumptions (it is written to the log)")
    if args.mode == "loopback":
        if args.attenuator_db < MIN_LOOPBACK_ATTEN_DB:
            parser.error(f"loopback needs --attenuator-db >= {MIN_LOOPBACK_ATTEN_DB:g} "
                         "(a fixed attenuator in line; never a bare cable)")
        if args.reference or args.extend:
            parser.error("--reference / --extend apply to --mode cans")
    else:
        if args.attenuator_db:
            parser.error("--attenuator-db applies to --mode loopback")
        if args.extend and not args.reference:
            parser.error("--extend needs --reference (the coupling must be measured first)")

    reference = None
    if args.reference:
        try:
            reference = load_reference(args.reference)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        for key, mine in (("rx_gain_db", args.rx_gain), ("rate_hz", args.rate)):
            if abs(reference["settings"][key] - mine) > 1e-6:
                parser.error(f"reference was taken with {key}={reference['settings'][key]}, "
                             f"this run uses {mine}: they must match")

    params = first_contact_params(fs_hz=args.rate, bandwidth_hz=args.bandwidth)
    gain_max = float(UsrpRadar.B210_TX_GAIN_RANGE_DB[1])
    # Zero credit in both modes for the first ramp: in loopback the attenuator is extra margin.
    no_credit_limit = highest_gain_within_budget(gain_max)
    stop = no_credit_limit if args.stop is None else min(args.stop, no_credit_limit)
    gains = plan_gains(args.start, stop, args.step)
    if not gains:
        parser.error(f"nothing to do: --start {args.start} is above the limit {stop:.1f} dB")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) if args.out else captures_dir() / f"txramp_{args.mode}_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    log = {"format": "phalanx-txramp", "version": 1, "mode": args.mode, "started_utc": stamp,
           "accepted_by": args.accepted_by.strip(), "armed": bool(args.arm),
           "settings": {"rx_gain_db": args.rx_gain, "rate_hz": args.rate,
                        "bandwidth_hz": args.bandwidth, "tone_offset_hz": TONE_OFFSET_HZ,
                        "step_db": args.step, "attenuator_db": args.attenuator_db,
                        "stop_peak": args.stop_peak, "linearity_tol_db": args.linearity_tol},
           "assumptions": {"p_max_dbm": P_MAX_DBM, "rx_limit_dbm": RX_LIMIT_DBM,
                           "margin_db": MARGIN_DB, "tx_gain_max_db": gain_max,
                           "no_credit_gain_limit_db": no_credit_limit},
           "reference": reference["path"] if reference else None,
           "noise_floor": None, "steps": [], "stopped_because": None,
           "highest_gain_reached_db": None, "coupling": None,
           "recommended_max_tx_gain_db": None}

    def save():
        out.write_text(json.dumps(log, indent=2, default=str), encoding="utf-8")

    print(params.summary())
    print(f"budget: no-credit TX gain limit {no_credit_limit:.1f} dB "
          f"(bound <= {RX_LIMIT_DBM - MARGIN_DB:g} dBm at an RX connector)")
    driver_cap = ABSOLUTE_TX_GAIN_CEILING_DB if args.extend else max(gains)
    state = {"prev": None, "credit": 0.0}

    with UsrpRadar(params, serial=args.serial, tx_gain_db=gains[0], rx_gain_db=args.rx_gain,
                   max_tx_gain_db=driver_cap, transmit=False, tx_waveform="tone",
                   num_send_frames=BRINGUP_NUM_SEND_FRAMES) as radar:
        radar.configure()
        log["device"] = dict(radar.info)
        device_max = float(radar.info.get("tx_gain_range_db", (0.0, gain_max))[1])
        if abs(device_max - gain_max) > 0.5:
            log["stopped_because"] = (f"device reports TX gain max {device_max} dB, budget assumed "
                                      f"{gain_max} dB: recheck the budget before transmitting")
            save()
            print("Stopped:", log["stopped_because"])
            return 1

        quiet = radar.capture(transmit=False)
        noise_dbfs, _ = tone_level_dbfs(quiet.samples, params.fs_hz)
        peak = float(np.abs(quiet.samples).max())
        log["noise_floor"] = {"level_dbfs": noise_dbfs, "rx_peak": peak,
                              "rx_errors": quiet.meta.get("rx_errors", [])}
        save()
        print(f"TX off      : tone-bin floor {noise_dbfs:7.1f} dBFS  peak={peak:.4f}  "
              f"rx_errors={quiet.meta.get('rx_errors', [])}")
        if quiet.meta.get("rx_errors"):
            log["stopped_because"] = "RX errors with the transmitter silent"
        elif peak >= args.stop_peak:
            log["stopped_because"] = "receiver near full scale with TX off: lower --rx-gain"
        elif not args.arm:
            log["stopped_because"] = "transmitter not armed (no --arm)"
        if log["stopped_because"]:
            save()
            print("Stopped:", log["stopped_because"], "- nothing transmitted.")
            return 0 if not args.arm and "not armed" in log["stopped_because"] else 1

        def ramp(gain_list, fixed_atten, credit):
            """Returns a stop reason, or None if every gain in the list passed."""
            for g in gain_list:
                bound = rx_power_bound_dbm(g, gain_max, fixed_atten, credit)
                if bound > RX_LIMIT_DBM - MARGIN_DB + 1e-9:        # never take the step
                    return f"budget: TX gain {g} dB would allow {bound:.1f} dBm at an RX connector"
                try:
                    actual = radar.set_tx_gain(g)
                    time.sleep(args.settle)
                    frame = radar.capture(transmit=True)
                except (RuntimeError, ValueError) as error:
                    return f"driver stopped at TX gain {g} dB: {error}"
                level, found = tone_level_dbfs(frame.samples, params.fs_hz)
                peak = float(np.abs(frame.samples).max())
                snr = level - noise_dbfs
                rec = {"tx_gain_db": actual, "bound_at_rx_dbm": bound, "level_dbfs": level,
                       "tone_hz": found, "snr_db": snr, "rx_peak": peak,
                       "valid": bool(snr >= MIN_SNR_DB and peak < args.stop_peak),
                       "rx_errors": frame.meta.get("rx_errors", [])}
                log["steps"].append(rec)
                save()
                print(f"TX {actual:5.1f} dB : level {level:7.1f} dBFS  snr {snr:5.1f} dB  "
                      f"peak={peak:.4f}  bound {bound:6.1f} dBm")
                if rec["rx_errors"]:
                    return f"UHD reported RX errors at TX gain {actual} dB"
                if peak >= args.stop_peak:
                    return (f"received peak {peak:.3f} reached --stop-peak {args.stop_peak} at TX gain "
                            f"{actual} dB (not damage; lower --rx-gain to read higher gains)")
                prev = state["prev"]
                if rec["valid"] and prev is not None:
                    expected = actual - prev["tx_gain_db"]
                    got = level - prev["level_dbfs"]
                    if abs(got - expected) > args.linearity_tol:
                        return (f"linearity: {expected:.1f} dB of gain changed the level by {got:.1f} dB "
                                f"at TX gain {actual} dB (assumption b not holding)")
                if rec["valid"]:
                    state["prev"] = rec
                log["highest_gain_reached_db"] = actual
            return None

        fixed = args.attenuator_db if args.mode == "loopback" else 0.0
        reason = ramp(gains, fixed, 0.0)
        log["stopped_because"] = reason or "ramp completed to the no-credit limit"
        valid = [s for s in log["steps"] if s["valid"]]
        if reason is None and len(valid) < 3:
            log["stopped_because"] = ("no usable tone seen (fewer than three valid readings): "
                                      "check the cabling / raise --rx-gain")
            reason = log["stopped_because"]
        base_completed = reason is None
        save()

        cap = None
        if args.mode == "cans" and reference is not None and reason is None:
            losses = coupling_losses_db(reference["steps"], log["steps"],
                                        reference["settings"]["attenuator_db"])
            if len(losses) < 3:
                log["stopped_because"] = "fewer than three gains in common with the reference"
            else:
                values = np.array(list(losses.values()))
                credit = float(values.min() - args.coupling_uncertainty_db)
                raw_cap = min(ABSOLUTE_TX_GAIN_CEILING_DB,
                              highest_gain_within_budget(gain_max, 0.0, max(credit, 0.0)))
                cap = float(gains[0] + np.floor((raw_cap - gains[0]) / args.step + 1e-9) * args.step)
                log["coupling"] = {"per_gain_db": {str(k): v for k, v in losses.items()},
                                   "min_db": float(values.min()), "median_db": float(np.median(values)),
                                   "spread_db": float(values.max() - values.min()),
                                   "uncertainty_db": args.coupling_uncertainty_db,
                                   "credit_db": max(credit, 0.0), "budget_cap_db": cap}
                print(f"coupling loss: min {values.min():.1f} dB, median {np.median(values):.1f} dB, "
                      f"spread {values.max() - values.min():.1f} dB over {len(values)} gains "
                      f"-> credit {max(credit, 0.0):.1f} dB -> budget cap {cap:.1f} dB")
                if values.max() - values.min() > 2 * args.linearity_tol:
                    log["stopped_because"] = ("coupling readings disagree by more than "
                                              f"{2 * args.linearity_tol:g} dB: not extending")
                    cap = float(max(gains))
                    log["coupling"]["budget_cap_db"] = cap
                elif args.extend and cap > max(gains):
                    more = plan_gains(max(gains) + args.step, cap, args.step)
                    reason = ramp(more, 0.0, max(credit, 0.0))
                    log["stopped_because"] = reason or "ramp completed to the budget cap"
            save()

    # The operating cap is only ever a gain this run actually ramped through cleanly.
    if args.mode == "cans" and base_completed:
        log["recommended_max_tx_gain_db"] = log["highest_gain_reached_db"]
    save()
    print()
    print("Stopped:", log["stopped_because"])
    print(f"Highest TX gain reached: {log['highest_gain_reached_db']} dB")
    if args.mode == "loopback":
        if base_completed:
            print("Reference saved. Now connect the cans and run --mode cans --reference latest")
    elif log["recommended_max_tx_gain_db"] is not None:
        print(f"Operating TX gain cap for this can arrangement: "
              f"{log['recommended_max_tx_gain_db']:.1f} dB (ramped through cleanly)")
        if cap is not None and cap > log["recommended_max_tx_gain_db"] and not args.extend:
            print(f"The budget with the measured coupling allows up to {cap:.1f} dB: "
                  "rerun with --extend to ramp up to it.")
    print(f"Log: {out}")
    return 0 if str(log["stopped_because"]).startswith("ramp completed") else 1


if __name__ == "__main__":
    sys.exit(main())
