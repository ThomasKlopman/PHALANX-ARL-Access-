r"""Can-to-can isolation sweep for the B210 radar (RF-01 "calibration" evidence).

    python Part2_B210_Cantenna\scripts\isolation_sweep.py                 # 40 → 60 dB in 5 dB steps
    python Part2_B210_Cantenna\scripts\isolation_sweep.py --stop 50 --step 2.5
    python Part2_B210_Cantenna\scripts\isolation_sweep.py --rate 30.72e6 --bandwidth 25e6

Nothing is transmitted unless --arm is given. Read docs/usrp_damage_review_2026-11-05.md first:
the margin below the −15 dBm RX limit must be established with a power meter or spectrum
analyser on the RX can's cable (B210 RX2 on a 50 Ω load) BEFORE the B210's own RX port is
connected during transmit. This script's half-scale stop is a last line of defence, not the
measurement.

What it does, in order:
  1. opens the B210 with the transmitter silent and records one RX-only CPI (noise floor);
  2. steps TX gain from --start to --stop in --step dB, one CPI per step, printing the
     received peak (fraction of ADC full scale), RMS, the leakage bin and its level above
     the median range bin;
  3. STOPS EARLY the moment the received peak reaches --stop-peak (default 0.5 of full
     scale), or UHD reports an RX/TX error — and never asks for more than --max-tx-gain;
  4. writes every step to a JSON file in PHALANX_data\captures (the evidence for the card).

Why the limits are what they are: the B210's RX ports are damaged above −15 dBm at the
connector (UHD manual). The ADC cannot tell you the port power directly — gain between the
port and the ADC is what you set — so the rule here is conservative: keep RX gain low
(default 10 dB), stop at half scale, and separate/angle the cans before trying higher TX
gain. If the lab has a power meter or spectrum analyser, a direct measurement at the RX
cable beats any inference from the ADC.

The highest TX gain that stayed below --stop-peak is printed at the end; that is the
operating TX gain for this physical can arrangement, and the measured leakage bin is the
zero-range offset for the pipeline (ramp_offset).
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # RDK_Cantenna/ (the phalanx package)

from phalanx.hardware.usrp import (UsrpRadar, estimate_zero_range_offset,   # noqa: E402
                                   first_contact_params, CLIP_FRACTION,
                                   ABSOLUTE_TX_GAIN_CEILING_DB, BRINGUP_NUM_SEND_FRAMES)
from phalanx.paths import captures_dir                                     # noqa: E402


def step_record(frame, params, tx_gain):
    if frame.meta.get("tx_waveform") == "plfm":
        k, lvl = estimate_zero_range_offset(frame.samples, params)
    else:
        k, lvl = None, None       # tone: no range information
    return {"tx_gain_db": tx_gain, "tx_enabled": frame.meta["tx_enabled"],
            "rx_peak": frame.meta["rx_peak"], "rx_rms": frame.meta["rx_rms"],
            "rx_clipping": frame.meta["rx_clipping"],
            "leakage_bin": k, "leakage_delay_us": None if k is None else k / params.fs_hz * 1e6,
            "leakage_above_median_db": lvl,
            "rx_errors": frame.meta["rx_errors"], "tx_errors": frame.meta["tx_errors"],
            "capture_s": frame.meta["capture_s"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serial", default="")
    parser.add_argument("--rate", type=float, default=4e6)
    parser.add_argument("--bandwidth", type=float, default=3e6)
    parser.add_argument("--rx-gain", type=float, default=10.0)
    parser.add_argument("--start", type=float, default=40.0, help="first TX gain, dB")
    parser.add_argument("--stop", type=float, default=44.0, help="last TX gain, dB (inclusive)")
    parser.add_argument("--step", type=float, default=2.0)
    parser.add_argument("--max-tx-gain", type=float, default=60.0,
                        help="hard cap passed to the driver; raise only deliberately")
    parser.add_argument("--stop-peak", type=float, default=0.5,
                        help="stop when the received peak reaches this fraction of full scale")
    parser.add_argument("--settle", type=float, default=0.3, help="seconds between steps")
    parser.add_argument("--out", default="", help="JSON log path (default: PHALANX_data/captures)")
    parser.add_argument("--waveform", choices=["tone", "plfm"], default="tone",
                        help="tone (default; steady peak reading) or plfm (also gives the zero-range bin)")
    parser.add_argument("--arm", action="store_true",
                        help="ARM THE TRANSMITTER. Without it the script records the noise floor and exits.")
    args = parser.parse_args(argv)
    if args.max_tx_gain > ABSOLUTE_TX_GAIN_CEILING_DB:
        parser.error(f"--max-tx-gain above {ABSOLUTE_TX_GAIN_CEILING_DB} dB is not available from "
                     "this script")
    if args.stop > args.max_tx_gain:
        parser.error(f"--stop {args.stop} exceeds --max-tx-gain {args.max_tx_gain}")
    # Attenuation budget (tx_ramp_calibration.py): with no measured coupling, TX gain is limited
    # to gain_max - 45 dB. This quick sweep takes no coupling credit, so it stays under that.
    from phalanx.tx_budget import NO_CREDIT_LIMIT_DB as no_credit_limit
    if args.stop > no_credit_limit:
        parser.error(f"--stop {args.stop} is above {no_credit_limit:.1f} dB, the limit without a "
                     "measured coupling. Use tx_ramp_calibration.py (loopback reference, then "
                     "--mode cans --extend) to go higher.")
    if not 0 < args.stop_peak <= CLIP_FRACTION:
        parser.error(f"--stop-peak must be in (0, {CLIP_FRACTION}]")

    params = first_contact_params(fs_hz=args.rate, bandwidth_hz=args.bandwidth)
    gains = list(np.round(np.arange(args.start, args.stop + 1e-9, args.step), 2))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) if args.out else captures_dir() / f"isolation_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    log = {"started_utc": stamp, "args": vars(args), "params": params.summary(),
           "fs_hz": params.fs_hz, "n_fast": params.n_fast, "steps": [], "stopped_because": None,
           "highest_safe_tx_gain_db": None, "zero_range_offset": None}

    def save():
        out.write_text(json.dumps(log, indent=2), encoding="utf-8")

    print(params.summary())
    with UsrpRadar(params, serial=args.serial, tx_gain_db=gains[0], rx_gain_db=args.rx_gain,
                   max_tx_gain_db=args.max_tx_gain, transmit=True,
                   tx_waveform=args.waveform, num_send_frames=BRINGUP_NUM_SEND_FRAMES) as radar:
        radar.configure()
        log["device"] = dict(radar.info)
        print({k: radar.info[k] for k in ("model", "serial", "rx_rate_hz", "rx_frequency_hz",
                                          "rx_gain_db", "rx_bandwidth_hz", "antenna",
                                          "tx_antenna", "uhd_version", "device_args")})
        quiet = radar.capture(transmit=False)
        rec = step_record(quiet, params, None)
        log["noise_floor"] = rec
        save()
        print(f"TX off      : peak={rec['rx_peak']:.4f}  rms={rec['rx_rms']:.5f}  "
              f"rx_errors={rec['rx_errors']}")
        if rec["rx_errors"]:
            log["stopped_because"] = "RX errors with the transmitter silent"
            save()
            print("Stopped:", log["stopped_because"]); return 1
        if rec["rx_peak"] >= args.stop_peak:
            log["stopped_because"] = "receiver already near full scale with TX off: lower --rx-gain"
            save()
            print("Stopped:", log["stopped_because"]); return 1
        if not args.arm:
            log["stopped_because"] = "transmitter not armed (no --arm)"
            save()
            print("Transmitter not armed (no --arm): noise floor recorded, nothing transmitted."); return 0

        for g in gains:
            actual = radar.set_tx_gain(float(g))
            time.sleep(args.settle)
            frame = radar.capture(transmit=True)
            rec = step_record(frame, params, actual)
            log["steps"].append(rec)
            save()
            leak = ("" if rec["leakage_bin"] is None else
                    f"leak bin {rec['leakage_bin']} ({rec['leakage_delay_us']:.2f} us) "
                    f"{rec['leakage_above_median_db']:.1f} dB  ")
            print(f"TX {actual:5.1f} dB : peak={rec['rx_peak']:.4f}  rms={rec['rx_rms']:.5f}  "
                  f"{leak}rx_err={rec['rx_errors']} tx_err={rec['tx_errors']}")
            if rec["rx_errors"] or rec["tx_errors"]:
                log["stopped_because"] = f"UHD reported errors at TX gain {actual} dB"
                break
            if rec["rx_peak"] >= args.stop_peak:
                log["stopped_because"] = (f"received peak {rec['rx_peak']:.3f} reached "
                                          f"--stop-peak {args.stop_peak} at TX gain {actual} dB")
                break
            log["highest_safe_tx_gain_db"] = actual
            log["zero_range_offset"] = rec["leakage_bin"]
        else:
            log["stopped_because"] = "sweep completed without reaching --stop-peak"
    save()
    print()
    print("Stopped:", log["stopped_because"])
    print(f"Highest TX gain below {args.stop_peak} of full scale: {log['highest_safe_tx_gain_db']} dB")
    print(f"Zero-range offset (leakage bin) at that gain: {log['zero_range_offset']} samples")
    print(f"Log: {out}")
    return 0 if log["highest_safe_tx_gain_db"] is not None else 1


if __name__ == "__main__":
    sys.exit(main())
