r"""B210 + cans in FMCW mode, run through the unchanged Part 1 chain (range FFT, MTI, Doppler, CFAR).

    python Part2_B210_Cantenna\scripts\fmcw_check.py                 # receive-only: nothing transmits
    python Part2_B210_Cantenna\scripts\fmcw_check.py --arm           # 20 frames at TX gain 40 dB
    python Part2_B210_Cantenna\scripts\fmcw_check.py --arm --tx-gain 44 --frames 100

The B210 transmits the kit's kind of triangle chirp and the software does the mixing the kit does
in hardware (phalanx/hardware/b210_fmcw.py), so every frame has the kit's shape: real, 20 kS/s,
4096 samples. For each frame this prints the measured TX->RX latency (the zero-range calibration;
it should be the same number every frame and every start), the leakage level, and the detections.
The frames are saved as one recording that the Part 1 tools replay:

    python RDK_Cantenna_DSP.py --replay <file.npz> --offset 0 --parity 0
    python RDK_Cantenna_GUI.py        -> source "Replay recording" -> the .npz

TX gain is limited to the no-coupling-credit budget limit (tx_ramp_calibration.py; 44.8 dB on the
B210) unless --calibration names a completed cans calibration, which raises the limit to the cap
that run reached (e.g. --calibration latest --tx-gain 60). The transmitter stays off without --arm. What to expect at 4 MS/s / 3 MHz: range cells
are 50 m (the kit's 100 MHz sweep gives 1.5 m), so a person walking a few metres away shows up as
a moving detection in the first cell or two with the right speed and sign -- velocity is what
this run can confirm, not fine range.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # RDK_Cantenna/ (the phalanx package)

from phalanx import RangeDopplerProcessor, save_capture                       # noqa: E402
from phalanx.hardware.b210_fmcw import (B210FmcwRadar, b210_fmcw_params,          # noqa: E402
                                        b210_fmcw_processor_config, blind_range_m)
from phalanx.paths import captures_dir                                           # noqa: E402
from phalanx.tx_budget import NO_CREDIT_LIMIT_DB, gain_limit                    # noqa: E402,F401

def save_plot(path, result, frame, params):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    fig, (ax_b, ax_rd) = plt.subplots(1, 2, figsize=(12, 4.5))
    t_ms = np.arange(len(frame.samples)) / params.fs_hz * 1e3
    ax_b.plot(t_ms[:480], np.real(frame.samples[:480]), lw=0.8, label="I")
    if np.iscomplexobj(frame.samples):
        ax_b.plot(t_ms[:480], np.imag(frame.samples[:480]), lw=0.8, label="Q")
        ax_b.legend(loc="upper right")
    ax_b.set(title="dechirped beat (first 6 ramps)", xlabel="time [ms]", ylabel="amplitude [full scale]")
    vmax = float(np.percentile(result.rd_db, 99.9))
    im = ax_rd.imshow(result.rd_db, aspect="auto", origin="lower", cmap="viridis",
                      vmin=vmax - 50, vmax=vmax,
                      extent=[result.range_axis[0], result.range_axis[-1],
                              result.velocity_axis[0], result.velocity_axis[-1]])
    ax_rd.scatter([d.range_m for d in result.detections], [d.velocity_mps for d in result.detections],
                  s=120, facecolors="none", edgecolors="r")
    ax_rd.set(title=f"range-Doppler, frame {frame.seq}", xlabel="range [m]",
              ylabel="velocity [m/s] (+ = closing)")
    fig.colorbar(im, ax=ax_rd, label="dB")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arm", action="store_true",
                        help="ARM THE TRANSMITTER. Without it one receive-only frame is taken.")
    parser.add_argument("--frames", type=int, default=20, help="transmitting frames to capture")
    parser.add_argument("--tx-gain", type=float, default=40.0)
    parser.add_argument("--rx-gain", type=float, default=40.0)
    parser.add_argument("--serial", default="")
    parser.add_argument("--rate", type=float, default=4e6, help="B210 sample rate, S/s")
    parser.add_argument("--bw-mhz", type=float, default=3.0, help="sweep bandwidth, MHz")
    parser.add_argument("--ramp-ms", type=float, default=4.0, help="ramp time, ms (kit default 4)")
    parser.add_argument("--latency", type=int, default=None,
                        help="fix the TX->RX latency in samples instead of measuring it")
    parser.add_argument("--calibration", default="",
                        help="txramp_cans log ('latest' or a path): allows TX gain up to the cap "
                             "that calibration reached. Only valid if the cans have not moved.")
    parser.add_argument("--max-range", type=float, default=None,
                        help="ignore detections beyond this range, m (default: 300 m or 12 cells)")
    parser.add_argument("--min-range", type=float, default=None,
                        help="ignore detections closer than this, m (default: the blind range, "
                             "1.5 range cells, where the leakage hides everything)")
    parser.add_argument("--min-speed", type=float, default=0.3, help="ignore slower detections, m/s")
    parser.add_argument("--real-beat", action="store_true",
                        help="keep only the real part of the beat, like the kit (loses the sign "
                             "of the speed in range cell 0); default keeps I/Q")
    parser.add_argument("--no-mti", action="store_true")
    parser.add_argument("--pfa", type=float, default=1e-4)
    parser.add_argument("--out", default="", help="recording path (default: PHALANX_data/captures)")
    args = parser.parse_args(argv)
    try:
        limit_db, limit_source = gain_limit(args.calibration, captures_dir())
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.tx_gain > limit_db:
        parser.error(f"--tx-gain {args.tx_gain} is above {limit_db:.1f} dB ({limit_source}). Higher "
                     "gain needs tx_ramp_calibration.py (--extend) and --calibration latest.")
    if not 1 <= args.frames <= 2000:
        parser.error("--frames must be 1..2000")

    params = b210_fmcw_params(args.ramp_ms, args.bw_mhz)
    print(params.summary())
    print(f"TX gain limit: {limit_db:.1f} dB ({limit_source})")
    cfg = b210_fmcw_processor_config(params, pfa=args.pfa, mti=not args.no_mti,
                                     min_range_m=args.min_range, max_range_m=args.max_range,
                                     min_speed_mps=args.min_speed)
    print(f"detections reported from {cfg.min_range_m:.0f} m (blind range "
          f"{blind_range_m(params):.0f} m) to {cfg.max_range_m:.0f} m, |speed| >= {cfg.min_abs_velocity_mps} m/s")
    proc = RangeDopplerProcessor(params, cfg)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) if args.out else captures_dir() / f"b210_fmcw_{stamp}.npz"

    frames, latencies, problems = [], set(), []
    result = None
    with B210FmcwRadar(params, raw_fs_hz=args.rate, serial=args.serial, tx_gain_db=args.tx_gain,
                       rx_gain_db=args.rx_gain, max_tx_gain_db=float(np.floor(limit_db)),
                       transmit=False, latency_samples=args.latency,
                       beat="real" if args.real_beat else "complex") as radar:
        radar.configure()
        info = radar.info
        print(f"{info.get('model')} serial {info.get('serial')}  raw {info['raw_rate_hz']/1e6:g} MS/s "
              f"-> beat {info['beat_rate_hz']/1e3:g} kS/s (decimate by {info['decimation']})  "
              f"rx gain {info.get('rx_gain_db')} dB  analog bw {info.get('rx_bandwidth_hz', 0)/1e6:g} MHz")
        try:
            quiet = radar.capture(transmit=False)
        except RuntimeError as error:
            print(f"STOPPED before transmitting (receive-only frame failed): {error}")
            print("An overflow here means the computer cannot take samples from the B210 at "
                  f"{args.rate/1e6:g} MS/s; nothing was transmitted.")
            return 1
        print(f"TX off : beat rms {np.std(quiet.samples):.2e}  raw rx peak {quiet.meta['rx_peak']:.4f}  "
              f"rx_errors={quiet.meta['rx_errors']}")
        if not args.arm:
            print("Transmitter not armed (no --arm): nothing transmitted.")
            return 0
        for i in range(args.frames):
            try:
                frame = radar.capture(transmit=True)
            except (RuntimeError, ValueError) as error:
                problems.append(f"frame {i + 1}: {error}")
                print("STOPPED:", problems[-1])
                break
            result = proc.process(frame)
            frames.append(frame)
            latencies.add(frame.meta["latency_samples"])
            if frame.meta["rx_errors"]:
                problems.append(f"frame {frame.seq}: rx_errors {frame.meta['rx_errors']}")
            top = ", ".join(f"{d.range_m:.0f} m @ {d.velocity_mps:+.2f} m/s ({d.snr_db:.0f} dB)"
                            for d in sorted(result.detections, key=lambda d: -d.snr_db)[:3])
            print(f"[{frame.seq:3d}] latency {frame.meta['latency_samples']} samp  "
                  f"leakage {frame.meta['leakage_dbfs']:6.1f} dBFS  raw peak {frame.meta['rx_peak']:.4f}  "
                  f"{len(result.detections)} det: {top or '-'}")

    print()
    if frames:
        saved = save_capture(out, frames, params, label="b210_fmcw", gain_db=args.rx_gain,
                             extra={"tx_gain_db": args.tx_gain, "raw_rate_hz": args.rate,
                                    "tx_gain_limit_db": limit_db, "tx_gain_limit_source": limit_source,
                                    "latency_samples": frames[0].meta["latency_samples"],
                                    "ramp_offset": 0, "ramp_parity": 0})
        print(f"{len(frames)} frames saved: {saved}")
        png = save_plot(Path(str(saved)).with_suffix(".png"), result, frames[-1], params)
        if png:
            print(f"plot of the last frame: {png}")
        print(f"latency: {sorted(latencies)} samples "
              f"({frames[0].meta['latency_samples'] / args.rate * 1e6:.2f} us at {args.rate/1e6:g} MS/s)")
        print(f"replay : python RDK_Cantenna_DSP.py --replay \"{saved}\" --offset 0 --parity 0")
    print("problems:", problems or "none")
    return 0 if frames and not problems else 1


if __name__ == "__main__":
    sys.exit(main())
