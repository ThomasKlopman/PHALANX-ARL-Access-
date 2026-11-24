"""Live range-Doppler display — the stand-in for the GUI until it exists.

    python Part1_Pasternack_Kit/scripts/live_rd.py --sim                 # synthetic targets
    python Part1_Pasternack_Kit/scripts/live_rd.py                       # real Cantenna over USB
    python Part1_Pasternack_Kit/scripts/live_rd.py --ramp-ms 8 --no-mti

Left: aligned pulse matrix (what the ADC saw, cut into ramps).
Right: range-Doppler map with CFAR detections circled.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # RDK_Cantenna/ (the phalanx package)

import numpy as np                                                      # noqa: E402
import matplotlib.pyplot as plt                                         # noqa: E402

from phalanx import (ProcessorConfig, RangeDopplerProcessor, RecordedRadar, SimConfig,  # noqa: E402
                     SimulatedRadar, Target, cantenna_fmcw, save_capture)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--replay", default=None, help="replay a captures/*.npz file instead of hardware")
    ap.add_argument("--ramp-ms", type=float, default=4.0)
    ap.add_argument("--bw-mhz", type=float, default=100.0)
    ap.add_argument("--parity", type=int, default=1, help="kit 5035: 1 (calibrate.py sign)")
    ap.add_argument("--offset", type=int, default=None, help="fix ramp offset instead of estimating")
    ap.add_argument("--both", action="store_true", help="use up AND down ramps")
    ap.add_argument("--no-mti", action="store_true")
    ap.add_argument("--pfa", type=float, default=1e-4)
    ap.add_argument("--resource", default=None, help="VISA resource string")
    ap.add_argument("--record", default=None, help="save every raw frame to this .npz")
    ap.add_argument("--notch", type=float, nargs="*", default=[],
                    help="IF spur frequencies to notch out, Hz (kit 5035: 3440)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)

    params = cantenna_fmcw(args.ramp_ms, args.bw_mhz,
                           segment_select="both" if args.both else "up")
    print(params.summary())
    cfg = ProcessorConfig(mti_order=0 if args.no_mti else 1, ramp_parity=args.parity,
                          ramp_offset=args.offset, notch_hz=tuple(args.notch))
    cfg.cfar.pfa = args.pfa
    proc = RangeDopplerProcessor(params, cfg)

    if args.replay:
        radar = RecordedRadar(args.replay, loop=True, realtime=True)
        params = radar.params
        proc = RangeDopplerProcessor(params, cfg)
        print(f"replaying {args.replay}: {len(radar)} frames, label={radar.label!r}")
    elif args.sim:
        radar = SimulatedRadar(params, SimConfig(
            targets=[Target(30.0, 1.5, 20), Target(12.0, -2.5, 15)], seed=0))
    else:
        from phalanx.hardware.cantenna import CantennaRDK
        radar = CantennaRDK(params, resource=args.resource)

    fig, (ax_p, ax_rd) = plt.subplots(1, 2, figsize=(12, 5))
    im_p = im_rd = None
    scat = None
    recorded = []

    with radar:
        radar.configure()
        radar.start()
        plt.ion()
        for frame in radar.frames():
            t0 = time.perf_counter()
            r = proc.process(frame)
            dt = (time.perf_counter() - t0) * 1e3
            if args.record:
                recorded.append(frame)

            if im_p is None:
                im_p = ax_p.imshow(r.pulses, aspect="auto", cmap="RdBu_r",
                                   extent=[0, params.n_fast, r.pulses.shape[0], 0])
                ax_p.set(title="pulse matrix", xlabel="fast-time sample", ylabel="pulse")
                im_rd = ax_rd.imshow(r.rd_db, aspect="auto", origin="lower", cmap="viridis",
                                     extent=[r.range_axis[0], r.range_axis[-1],
                                             r.velocity_axis[0], r.velocity_axis[-1]])
                ax_rd.set(xlabel="range [m]", ylabel="velocity [m/s] (+ = closing)")
                fig.colorbar(im_rd, ax=ax_rd, label="dB")
            else:
                im_p.set_data(r.pulses)
                im_p.set_clim(-np.abs(r.pulses).max(), np.abs(r.pulses).max())
                im_rd.set_data(r.rd_db)
            vmax = float(np.percentile(r.rd_db, 99.9))
            im_rd.set_clim(vmax - 50, vmax)
            if scat is not None:
                scat.remove()
            scat = ax_rd.scatter([d.range_m for d in r.detections],
                                 [d.velocity_mps for d in r.detections],
                                 s=120, facecolors="none", edgecolors="r")
            conf = r.diag.get("align_confidence", float("nan"))
            ax_rd.set_title(f"frame {frame.seq}  offset={r.diag['ramp_offset']} "
                            f"conf={conf:.1f}  dsp={dt:.1f} ms  {len(r.detections)} det")
            top = ", ".join(f"{d.range_m:.1f} m @ {d.velocity_mps:+.1f} m/s ({d.snr_db:.0f} dB)"
                            for d in r.detections[:3])
            print(f"[{frame.seq:4d}] {top or '-'}")
            fig.canvas.draw_idle()
            plt.pause(0.001)
            if not plt.fignum_exists(fig.number):
                break
    if args.record and recorded:
        out = save_capture(args.record, recorded, params, label="live_rd",
                           extra={"mti_order": cfg.mti_order, "pfa": cfg.cfar.pfa,
                                  "ramp_parity": cfg.ramp_parity})
        print(f"saved {len(recorded)} frames to {out}")


if __name__ == "__main__":
    main()
