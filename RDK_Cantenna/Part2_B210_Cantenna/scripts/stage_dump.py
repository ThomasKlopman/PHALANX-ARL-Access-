r"""Stage-by-stage check of the B210 FMCW processing chain on one frame.

    python Part2_B210_Cantenna\scripts\stage_dump.py                       # simulated frame, known target
    python Part2_B210_Cantenna\scripts\stage_dump.py --range 120 --speed -1.0
    python Part2_B210_Cantenna\scripts\stage_dump.py --recording "<file.npz>" --frame 5

Simulated frame (default): a walker at a known range and speed is put through every stage, and
the output of each stage is compared with the value the physics predicts (delay, beat frequency,
range cell, MTI gain, Doppler cell). Every line prints PASS or FAIL; the figure shows the signal
at six points along the chain. Nothing is transmitted and no hardware is needed.

Recorded frame: the recording holds the beat signal (the output of stage F4), so stages 1 to 9
are run on it and their outputs printed; F1 to F3 cannot be repeated because the raw B210
samples are not stored. There is no "expected" column unless the scene is known.

The stages (F = front end done in software for the B210, numbers = the stages shared with Part 1):
  F1 capture raw I/Q   F2 measure TX->RX latency   F3 dechirp   F4 low-pass then decimate
  1 remove DC   3 cut into sweeps   4 range FFT   5 MTI   6 Doppler FFT   7 power
  8 CA-CFAR   9 gate and extract            (Part 1's stage 2, the sweep search, is not needed)
The last line checks that running the stages one by one gives exactly what the one-call
pipeline (RangeDopplerProcessor.process) gives.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # RDK_Cantenna/ (the phalanx package)

from phalanx import load_capture                                                    # noqa: E402
from phalanx.dsp import conditioning as cond                                        # noqa: E402
from phalanx.dsp.detect import ca_cfar                                               # noqa: E402
from phalanx.dsp.doppler import doppler_fft, mti_cancel, power                       # noqa: E402
from phalanx.dsp.pipeline import RangeDopplerProcessor                               # noqa: E402
from phalanx.hardware.b210_fmcw import (SimulatedB210Fmcw, SimWalker, b210_fmcw_params,   # noqa: E402
                                        b210_fmcw_processor_config, blind_range_m, dechirp,
                                        estimate_latency)
from phalanx.params import C0                                                        # noqa: E402

INK, INK2, SURFACE = "#0b0b0b", "#52514e", "#fcfcfb"
BLUE, ORANGE, GRID = "#2a78d6", "#eb6834", "#e4e3de"
BLUES = ["#f3f8fe", "#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]


def db(x):
    return 20 * np.log10(np.maximum(np.abs(x), 1e-12))


class Report:
    def __init__(self):
        self.rows = []

    def add(self, stage, what, obtained, expected=None, ok=None):
        self.rows.append({"stage": stage, "what": what, "obtained": obtained,
                          "expected": expected, "ok": ok})
        verdict = "" if ok is None else ("PASS" if ok else "FAIL")
        exp = "" if expected is None else f"  expected {expected}"
        print(f"  {stage:<3} {what:<34} {obtained}{exp}  {verdict}")

    @property
    def failed(self):
        return [r for r in self.rows if r["ok"] is False]


def shared_stages(frame, params, cfg, rep, truth=None):
    """Stages 1 to 9, one call per stage, then the same frame through the one-call pipeline."""
    proc = RangeDopplerProcessor(params, cfg)
    cell = params.range_resolution_m
    x = cond.remove_dc(frame.samples)
    rep.add("1", "DC removed: |mean| after", f"{abs(x.mean()):.1e}", "< 1e-6", abs(x.mean()) < 1e-6)
    offset, _ = proc.align(x)
    pulses, is_down = proc.pulse_matrix(x, offset)
    want = (params.n_pulses, params.n_fast)
    rep.add("3", "sweep matrix (sweeps x samples)", pulses.shape, want, pulses.shape == want)
    rp = proc.range_profiles(pulses, is_down)
    want = (params.n_pulses, params.n_fast // 2 + 1)
    rep.add("4", "range profiles (sweeps x cells)", rp.shape, want, rp.shape == want)
    prof_before = np.sqrt((np.abs(rp) ** 2).mean(axis=0))
    k_peak = int(np.argmax(prof_before[1:])) + 1
    if truth:
        k_true = int(round(truth["range_m"] / cell))
        rep.add("4", "strongest cell beyond cell 0", f"cell {k_peak} ({k_peak * cell:.0f} m)",
                f"cell {k_true} ({truth['range_m']:.0f} m / {cell:.1f} m)", k_peak == k_true)
    else:
        rep.add("4", "strongest cell beyond cell 0", f"cell {k_peak} ({k_peak * cell:.0f} m)")
    mti = mti_cancel(rp, cfg.mti_order) if cfg.mti_order else rp
    want = (params.n_pulses - cfg.mti_order, rp.shape[1])
    rep.add("5", "after MTI (sweeps x cells)", mti.shape, want, mti.shape == want)
    prof_after = np.sqrt((np.abs(mti) ** 2).mean(axis=0))
    if truth and cfg.mti_order == 1:
        f_d = 2 * truth["velocity_mps"] / params.wavelength_m
        gain = 20 * np.log10(max(2 * abs(np.sin(np.pi * f_d * params.pri_s)), 1e-12))
        got = db(prof_after[k_peak]) - db(prof_before[k_peak])
        rep.add("5", "MTI gain at the target's cell", f"{got:+.1f} dB", f"{gain:+.1f} dB (2|sin(pi f_d PRI)|)",
                abs(got - gain) < 1.0)
    rep.add("5", "cell 0 level, before -> after MTI", f"{db(prof_before[0]):.1f} -> {db(prof_after[0]):.1f} dB")
    rd = doppler_fft(mti, params.doppler_window, params.n_fft_doppler)
    rep.add("6", "range-Doppler map (speeds x cells)", rd.shape, mti.shape, rd.shape == mti.shape)
    pw = power(rd)
    v_axis = params.velocity_axis(pw.shape[0])
    order = np.argsort(v_axis, kind="stable")
    pw, v_axis = pw[order], v_axis[order]
    row = int(np.argmax(pw[:, k_peak]))
    if truth:
        rep.add("6", "speed of the peak in that cell", f"{v_axis[row]:+.2f} m/s",
                f"{truth['velocity_mps']:+.2f} m/s (cell {params.velocity_resolution_mps:.2f} m/s)",
                abs(v_axis[row] - truth["velocity_mps"]) <= params.velocity_resolution_mps)
    else:
        rep.add("6", "speed of the peak in that cell", f"{v_axis[row]:+.2f} m/s")
    floor = float(np.median(pw))
    rep.add("7", "power map: median (noise floor)", f"{10 * np.log10(floor):.1f} dB")
    mask, noise, alpha = ca_cfar(pw, cfg.cfar)
    rep.add("8", "CA-CFAR threshold factor", f"{10 * np.log10(alpha):.1f} dB over the local mean",
            f"Pfa {cfg.cfar.pfa:g}")
    rep.add("8", "cells over threshold (before gates)", int(mask.sum()))
    result = proc.process(frame)
    dets = sorted(result.detections, key=lambda d: -d.snr_db)
    rep.add("9", "gates", f"range {cfg.min_range_m:.0f} to {cfg.max_range_m:.0f} m, "
            f"|speed| >= {cfg.min_abs_velocity_mps} m/s")
    if truth:
        ok = bool(dets) and abs(dets[0].range_m - truth["range_m"]) <= cell \
            and abs(dets[0].velocity_mps - truth["velocity_mps"]) <= params.velocity_resolution_mps
        got = "none" if not dets else (f"{dets[0].range_m:.0f} m, {dets[0].velocity_mps:+.2f} m/s, "
                                       f"{dets[0].snr_db:.0f} dB")
        rep.add("9", f"detections: {len(dets)}; strongest", got,
                f"{truth['range_m']:.0f} m, {truth['velocity_mps']:+.2f} m/s", ok)
    else:
        rep.add("9", "detections", [f"{d.range_m:.0f} m {d.velocity_mps:+.2f} m/s {d.snr_db:.0f} dB"
                                    for d in dets] or "none")
    same = (np.allclose(result.range_profiles, rp) and np.allclose(result.rd_power, pw, rtol=1e-5))
    rep.add("=", "stage-by-stage equals the pipeline", "identical" if same else "DIFFERENT",
            "identical", same)
    return dict(pulses=pulses, prof_before=prof_before, prof_after=prof_after, pw=pw, v_axis=v_axis,
                noise=noise, alpha=alpha, k_peak=k_peak, row=row, result=result, dets=dets)


def figure(path, params, st, front=None, title=""):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap
    plt.rcParams.update({"font.size": 8.5, "font.family": "DejaVu Sans", "axes.edgecolor": INK2,
                         "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "axes.titlesize": 9.5, "axes.titleweight": "bold", "axes.titlecolor": INK,
                         "axes.titlelocation": "left", "figure.facecolor": SURFACE,
                         "axes.facecolor": SURFACE, "axes.grid": True, "grid.color": GRID,
                         "grid.linewidth": 0.6, "legend.frameon": False})
    cell = params.range_resolution_m
    rows = 3 if front else 2
    fig, axes = plt.subplots(rows, 2, figsize=(7.6, 2.75 * rows + 0.4), layout="constrained")

    def legend(a):
        """Legend in a strip of headroom above the data, so it never sits on a trace."""
        lo, hi = a.get_ylim()
        a.set_ylim(lo, hi + 0.20 * (hi - lo))
        a.legend(loc="upper right", ncols=2, handlelength=1.6, borderaxespad=0.2)

    ax = list(axes.ravel())
    if front:
        a = ax.pop(0)
        # The middle of the first up-ramp, where the chirp passes through the carrier and the
        # sweep is slow enough to see; the rest of the ramp is the same thing, faster.
        mid = int(round(params.ramp_time_s * front["fs"] / 2)) + front["latency"]
        half = int(round(120e-6 * front["fs"]))
        seg = front["raw"][mid - half:mid + half]
        t_us = (np.arange(len(seg)) - half) / front["fs"] * 1e6
        a.plot(t_us, seg.real, color=BLUE, lw=1.0, label="I")
        a.plot(t_us, seg.imag, color=ORANGE, lw=1.0, label="Q")
        a.set(title="F1  Raw I/Q, mid-sweep", xlabel="time from mid-sweep [µs]",
              ylabel="amplitude [full scale]")
        legend(a)
        a = ax.pop(0)
        lags = np.arange(len(front["xcorr"]))
        a.plot(lags, front["xcorr"], color=BLUE, lw=1.2)
        a.plot([front["latency"]], [front["xcorr"][front["latency"]]], "o", color=BLUE, ms=6,
               mec=SURFACE, mew=1.5)
        a.annotate(f"{front['latency']} samples", (front["latency"], front["xcorr"][front["latency"]]),
                   textcoords="offset points", xytext=(8, -4), color=INK)
        a.set(title="F2  Latency from the leakage", xlabel="delay [samples]",
              ylabel="correlation [relative]", xlim=(0, 200))
    a = ax.pop(0)
    beat = front["beat"] if front else st["frame"].samples
    # Three up/down pairs, starting one pair in (clear of the filter's start-up), with the
    # mean taken off: the leakage sits at 0 Hz and is ~40 dB above anything else in the trace.
    n0, n = 2 * params.n_fast, 3 * 2 * params.n_fast
    seg = beat[n0:n0 + n] - np.mean(beat)
    t_ms = (n0 + np.arange(n)) / params.fs_hz * 1e3
    a.plot(t_ms, np.real(seg), color=BLUE, lw=1.0, label="I")
    if np.iscomplexobj(beat):
        a.plot(t_ms, np.imag(seg), color=ORANGE, lw=1.0, label="Q")
        legend(a)
    a.set(title="F3–F4  Beat signal, mean removed" if front else "Beat signal, mean removed",
          xlabel="time [ms]", ylabel="amplitude [full scale]")
    a = ax.pop(0)
    r_axis = np.arange(len(st["prof_before"])) * cell
    shown = r_axis <= 400
    a.plot(r_axis[shown], db(st["prof_before"][shown]), color=INK2, lw=1.2, label="4 range FFT")
    a.plot(r_axis[shown], db(st["prof_after"][shown]), color=BLUE, lw=1.8, label="5 after MTI")
    a.axvspan(0, st["blind"], color=GRID, alpha=0.6, lw=0)
    a.text(st["blind"] / 2, a.get_ylim()[0] + 3, "blind", ha="center", color=INK2)
    a.set(title="4–5  Level by range", xlabel="range [m]", ylabel="level [dB]")
    legend(a)
    a = ax.pop(0)
    pw_db = 10 * np.log10(np.maximum(st["pw"], 1e-20))
    floor = float(np.median(pw_db))
    cmap = LinearSegmentedColormap.from_list("blues", BLUES)
    ncell = int(np.searchsorted(r_axis, 400)) + 1
    im = a.imshow(pw_db[:, :ncell], aspect="auto", origin="lower", cmap=cmap, vmin=floor, vmax=floor + 40,
                  extent=[-cell / 2, (ncell - 0.5) * cell, st["v_axis"][0], st["v_axis"][-1]])
    for d in st["dets"]:
        a.plot([d.range_m], [d.velocity_mps], "o", mfc="none", mec=ORANGE, mew=1.8, ms=12)
    a.grid(False)
    a.set(title="6–7  Range–Doppler map" + ("  (ring = detection)" if st["dets"] else ""), xlabel="range [m]",
          ylabel="velocity [m/s], + = closing")
    cb = fig.colorbar(im, ax=a, pad=0.02)
    cb.set_label("power [dB]", color=INK2)
    cb.outline.set_visible(False)
    a = ax.pop(0)
    k = st["dets"][0].range_bin if st["dets"] else st["k_peak"]
    a.plot(st["v_axis"], pw_db[:, k], color=BLUE, lw=1.8, label="power")
    a.plot(st["v_axis"], 10 * np.log10(np.maximum(st["noise"][:, k] * st["alpha"], 1e-20)),
           color=ORANGE, lw=1.4, ls=(0, (4, 2)), label="threshold")
    a.set(title=f"8–9  Decision, cell at {k * cell:.0f} m", xlabel="velocity [m/s], + = closing",
          ylabel="power [dB]")
    legend(a)
    fig.suptitle(title, x=0.012, ha="left", fontsize=10.5, fontweight="bold", color=INK)
    fig.savefig(path, dpi=200)
    plt.close(fig)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recording", default="", help="a B210 FMCW recording (.npz); default: simulated frame")
    ap.add_argument("--frame", type=int, default=0, help="which frame of the recording (0 = first)")
    ap.add_argument("--rate", type=float, default=8e6, help="simulated: B210 sample rate, S/s")
    ap.add_argument("--bw-mhz", type=float, default=6.0, help="simulated: sweep, MHz")
    ap.add_argument("--range", type=float, default=80.0, dest="range_m", help="simulated walker range, m")
    ap.add_argument("--speed", type=float, default=1.4, help="simulated walker speed, m/s (+ = closing)")
    ap.add_argument("--latency", type=float, default=83.0, help="simulated TX->RX latency, samples")
    ap.add_argument("--out", default="", help="figure path (.png); a .json of the table is written beside it")
    args = ap.parse_args(argv)
    rep = Report()
    front = None

    if args.recording:
        frames, params, meta = load_capture(args.recording)
        frame = frames[args.frame]
        cfg = b210_fmcw_processor_config(params)
        print(f"Recorded frame {frame.seq} of {Path(args.recording).name}  ({len(frames)} frames)")
        print(params.summary())
        rep.add("F2", "latency stored with the frame", f"{frame.meta.get('latency_samples')} samples")
        rep.add("F3", "leakage (DC of the beat)", f"{frame.meta.get('leakage_dbfs', float('nan')):.1f} dBFS")
        rep.add("F4", "beat frame", f"{len(frame)} samples, {frame.samples.dtype}, {params.fs_hz/1e3:g} kS/s")
        truth = None
        title = f"One recorded frame, stage by stage ({Path(args.recording).name}, frame {frame.seq})"
    else:
        params = b210_fmcw_params(bandwidth_mhz=args.bw_mhz)
        cfg = b210_fmcw_processor_config(params)
        truth = {"range_m": args.range_m, "velocity_mps": args.speed}
        sim = SimulatedB210Fmcw(params, raw_fs_hz=args.rate, frame_period_s=0, latency_samples=args.latency,
                                walkers=[SimWalker(range_m=args.range_m, velocity_mps=args.speed,
                                                   near_m=0.0, far_m=1e6)])
        sim.start()
        frame = sim.capture()
        raw, ref, fs = sim.last_raw, sim._ref, sim.raw.fs_hz
        print(f"Simulated frame: walker at {args.range_m:g} m, {args.speed:+g} m/s; "
              f"B210 at {fs/1e6:g} MS/s, leakage {sim.leak_db:g} dBFS, latency {args.latency:g} samples")
        print(params.summary())
        rep.add("F1", "raw I/Q", f"{len(raw):,} samples, {raw.dtype}, {fs/1e6:g} MS/s",
                f"{params.n_samples * sim.decim:,} samples", len(raw) == params.n_samples * sim.decim)
        level = 20 * np.log10(np.sqrt(np.mean(np.abs(raw) ** 2)))
        rep.add("F1", "level (the leakage dominates)", f"{level:.1f} dBFS", f"{sim.leak_db:.1f} dBFS",
                abs(level - sim.leak_db) < 0.5)
        lat, peak_db = estimate_latency(raw, ref)
        rep.add("F2", "TX->RX latency", f"{lat} samples ({lat / fs * 1e6:.2f} us), peak {peak_db:.0f} dB",
                f"{args.latency:g} samples", lat == round(args.latency))
        n = min(len(raw), 1 << 17)
        nfft = 1 << 18
        xc = np.abs(np.fft.ifft(np.fft.fft(raw[:n], nfft) * np.conj(np.fft.fft(ref[:n], nfft))))[:256]
        beat = dechirp(raw, ref, lat, sim.decim)
        dc = 20 * np.log10(abs(beat.mean()))
        rep.add("F3", "leakage lands at DC: level", f"{dc:.1f} dBFS", f"{sim.leak_db:.1f} dBFS",
                abs(dc - sim.leak_db) < 0.5)
        f_beat = params.bandwidth_hz / params.ramp_time_s * 2 * args.range_m / C0
        up = cond.remove_dc(beat)[:params.n_fast]
        spec = np.abs(np.fft.fft(up * np.hanning(len(up)), 4096))[:2048]
        f_got = float(np.argmax(spec[8:]) + 8) * params.fs_hz / 4096
        rep.add("F3", "target's beat frequency", f"{f_got:.0f} Hz", f"{f_beat:.0f} Hz ((B/T) x 2R/c)",
                abs(f_got - f_beat) < 0.5 / params.ramp_time_s)
        rep.add("F4", "after low-pass and decimation", f"{len(beat)} samples, {beat.dtype}, "
                f"{params.fs_hz/1e3:g} kS/s (1/{sim.decim})", f"{params.n_samples} samples",
                len(beat) == params.n_samples and np.iscomplexobj(beat))
        front = dict(raw=raw, fs=fs, xcorr=xc / xc.max(), latency=lat, beat=beat)
        title = (f"One simulated frame, stage by stage: walker at {args.range_m:g} m, "
                 f"{args.speed:+g} m/s, {fs/1e6:g} MS/s / {args.bw_mhz:g} MHz")

    st = shared_stages(frame, params, cfg, rep, truth)
    st.update(frame=frame, blind=blind_range_m(params))
    checked = [r for r in rep.rows if r["ok"] is not None]
    print(f"\n{len(checked) - len(rep.failed)} of {len(checked)} checks passed"
          + ("" if not rep.failed else f"; FAILED: {[r['stage'] + ' ' + r['what'] for r in rep.failed]}"))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        figure(out, params, st, front, title)
        out.with_suffix(".json").write_text(json.dumps(rep.rows, indent=2, default=str), encoding="utf-8")
        print(f"figure: {out}\ntable : {out.with_suffix('.json')}")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
