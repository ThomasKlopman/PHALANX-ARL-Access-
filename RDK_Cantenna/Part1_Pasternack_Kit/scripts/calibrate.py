"""First-contact checks with the real kit. Run these before trusting the DSP.

    python Part1_Pasternack_Kit/scripts/calibrate.py idn        # can we talk to it?
    python Part1_Pasternack_Kit/scripts/calibrate.py raw        # capture one frame, save + plot it, print stats
    python Part1_Pasternack_Kit/scripts/calibrate.py edge       # can we align on the ramp turnarounds?
    python Part1_Pasternack_Kit/scripts/calibrate.py sign       # walk TOWARD the kit: decide ramp_parity

Every `raw`/`edge` run also saves its frames to PHALANX_data/captures/*.npz (phalanx/paths.py)
(git-ignored) so they can be analysed offline / shared. Close the plot window
to end the run — the script keeps the RF on until the window is closed.

Everything the manual leaves unspecified (sample signedness, ramp alignment,
up/down parity, Doppler sign) gets pinned down here, once, and then goes into
ProcessorConfig / RadarParams.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # RDK_Cantenna/ (the phalanx package)

import numpy as np                                                      # noqa: E402
import matplotlib.pyplot as plt                                         # noqa: E402

from phalanx import ProcessorConfig, RangeDopplerProcessor, cantenna_fmcw  # noqa: E402
from phalanx.hardware.cantenna import CantennaRDK  # noqa: E402
from phalanx.dsp.conditioning import estimate_ramp_offset, remove_dc  # noqa: E402
from phalanx.paths import captures_dir  # noqa: E402
CAPTURES = captures_dir()                # PHALANX_data/captures — outside git and OneDrive


def save_frames(tag, frames, params):
    """Write a schema-v1 capture (phalanx.capture) into PHALANX_data/captures/."""
    from phalanx.capture import save_capture
    CAPTURES.mkdir(parents=True, exist_ok=True)
    out = CAPTURES / f"{tag}_{time.strftime('%Y%m%d_%H%M%S')}.npz"
    save_capture(out, frames, params, label=tag, extra={"script": "calibrate.py"})
    print(f"saved {len(frames)} frame(s) → {out}")


def cmd_idn(kit):
    print("IDN :", kit.idn)
    for q in ("SWEEP:FREQSTAR?", "SWEEP:FREQSTOP?", "SWEEP:RAMPTIME?", "SWEEP:TYPE?",
              "FREQ:REF:DIV?", "POWE:RF?", "FREQ:LOCK?", "SYST:TEMP?"):
        try:
            print(f"{q:18s} {kit.query(q)}")
        except Exception as e:                                          # noqa: BLE001
            print(f"{q:18s} ! {e}")
    print("errors:", kit.errors() or "none")


def cmd_raw(kit):
    kit.configure()
    kit.start()
    f = kit.capture()
    raw = f.meta["raw"]
    print(f"signedness={f.meta['signedness']}  raw min/max/mean = {raw.min()}/{raw.max()}/"
          f"{raw.mean():.0f}  float std={f.samples.std():.4f}  capture {f.meta['capture_s']*1e3:.0f} ms")
    save_frames("raw", [f], kit.params)
    print("(close the plot window to finish)")
    fig, ax = plt.subplots(2, 1, figsize=(10, 6))
    ax[0].plot(f.samples); ax[0].set(title="one frame", xlabel="sample")
    spec = 20 * np.log10(np.abs(np.fft.rfft(remove_dc(f.samples) * np.hanning(len(f)))) + 1e-9)
    ax[1].plot(np.fft.rfftfreq(len(f), 1 / f.fs_hz), spec); ax[1].set(xlabel="Hz", ylabel="dB")
    plt.tight_layout(); plt.show()


def cmd_edge(kit):
    kit.configure()
    kit.start()
    p = kit.params
    fig, ax = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
    frames = []
    for i in range(5):
        f = kit.capture()
        frames.append(f)
        al = estimate_ramp_offset(remove_dc(f.samples), p.n_fast, "symmetry")
        print(f"frame {i}: symmetry offset={al.offset:3d} corr={al.confidence:.2f} | "
              f"transient offset={al.alt_offset:3d} conf={al.alt_confidence:.1f} | "
              f"segment means (first 4) = {np.round(al.segment_means[:4], 4)}")
        ax[0].plot(al.score, label=f"frame {i}")
        ax[1].plot(estimate_ramp_offset(remove_dc(f.samples), p.n_fast, "transient").score)
    ax[0].set(title="mirror-symmetry score (peak = turnaround; ≳0.8 is trustworthy)")
    ax[1].set(title="folded |diff| score (peak only if the turnaround has a transient)",
              xlabel="sample within ramp")
    ax[0].legend(); plt.tight_layout()
    save_frames("edge", frames, p)
    print("(close the plot window to finish)")
    plt.show()
    print("Symmetry corr ≳0.8 and a stable offset across frames → alignment is usable.\n"
          "If it drifts frame to frame the sweep and the ADC clock are not locked; "
          "then we align per frame (the default) rather than once.")


KNOWN_SPURS = (3440.0,)      # kit s/n 5035: CW spur in the IF, see dsp.conditioning.notch
WALK_FRAMES = 6              # ≈5 s of frames per direction


def _best_walker(res, v_min=0.3, v_max=3.5, r_min=1.0, r_max=25.0):
    """Strongest detection that could plausibly be a walking person."""
    cands = [d for d in res.detections
             if v_min <= abs(d.velocity_mps) <= v_max and r_min <= d.range_m <= r_max]
    return max(cands, key=lambda d: d.snr_db) if cands else None


def cmd_sign(kit):
    """Record one approach and one retreat, then score both parities offline on
    the SAME frames. Stand 4–5 m from the antennas, in their beam."""
    kit.configure()
    kit.start()
    p = kit.params
    frames = {"toward": [], "away": []}
    for direction, prompt in (("toward", "walk TOWARD the antennas"),
                              ("away", "now walk AWAY from the antennas")):
        for n in (3, 2, 1):
            print(f"  {prompt} in {n} ...")
            time.sleep(1)
        print(f"  GO — {prompt}, steady normal pace, ~5 s")
        for _ in range(WALK_FRAMES):
            frames[direction].append(kit.capture())
        print("  stop.")
    save_frames("sign", frames["toward"] + frames["away"], p)

    print()
    print("parity | toward (median v, n) | away (median v, n)")
    verdict = {}
    for parity in (0, 1):
        proc = RangeDopplerProcessor(p, ProcessorConfig(ramp_parity=parity, notch_hz=KNOWN_SPURS))
        med = {}
        for direction, fr in frames.items():
            v = [d.velocity_mps for d in (_best_walker(proc.process(f)) for f in fr) if d]
            med[direction] = (float(np.median(v)) if v else float("nan"), len(v))
        verdict[parity] = med
        print(f"  {parity}    | {med['toward'][0]:+6.2f} m/s ({med['toward'][1]})     "
              f"| {med['away'][0]:+6.2f} m/s ({med['away'][1]})")
    ok = [k for k, m in verdict.items()
          if m["toward"][0] > 0 and m["away"][0] < 0]
    if len(ok) == 1:
        print(f"\n→ ramp_parity = {ok[0]}  (closing = positive, opening = negative). "
              f"Use  live_rd.py --parity {ok[0]}")
    elif all(m["toward"][0] < 0 and m["away"][0] > 0 for m in verdict.values()
             if not np.isnan(m["toward"][0])):
        print("\n→ both parities read inverted: set RadarParams.doppler_sign=+1 and rerun.")
    else:
        print("\n→ inconclusive — too few walker detections. Stand further back (4–5 m), "
              "stay in the beam, walk at a normal pace, and rerun.")


if __name__ == "__main__":
    cmds = {"idn": cmd_idn, "raw": cmd_raw, "edge": cmd_edge, "sign": cmd_sign}
    which = sys.argv[1] if len(sys.argv) > 1 else "idn"
    with CantennaRDK(cantenna_fmcw()) as kit:
        cmds[which](kit)
