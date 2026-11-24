"""B210 channel-gap isolation tests. Receive only: this script never transmits.

  python b210_isolation_tests.py highgain             # generator RF OFF
  python b210_isolation_tests.py sweep --pad-db 12    # you step the generator level

Keep b210_2200mhz_snr_rx2.py in the same folder (its spectrum code is reused).
Safety: refuses any generator level that would put more than -30 dBm at a B210 RX
port (Ettus rating: -15 dBm max), steps levels upward, and stops on clipping.
"""
import argparse
import csv
from datetime import datetime
from pathlib import Path
import re
import shutil
import subprocess
import sys

import numpy as np

from b210_2200mhz_snr_rx2 import PORTS, CENTER, TONE, RATE, FFT, SAMPLES, db, spectrum

MAX_PORT_DBM = -30.0
OFFSET = np.fft.fftshift(np.fft.fftfreq(FFT, 1 / RATE))
BAND = (np.abs(OFFSET) <= 200_000) & (np.abs(OFFSET) >= 100)


def grab(exe, folder, port, tag, gain):
    subdev, ant = PORTS[port]
    path = folder / f"rf_{port}_{tag}.dat"
    cmd = [exe, "--args", "type=b200", "--subdev", subdev, "--channels", "0",
           "--ant", ant, "--freq", str(CENTER), "--rate", str(RATE), "--gain", str(gain),
           "--nsamps", str(SAMPLES), "--type", "float", "--file", str(path), "--stats"]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, errors="replace", timeout=60)
    log = path.with_suffix(".log")
    log.write_text(subprocess.list2cmdline(cmd) + "\n\n" + r.stdout, encoding="utf-8")
    if r.returncode or re.search(r"overflow|timeout|dropped samples|receiver error",
                                 r.stdout, re.IGNORECASE):
        raise RuntimeError(f"Invalid capture, see {log}")
    m = re.search(r"Actual RX Gain:\s*([\d.]+)", r.stdout)
    if not m or abs(float(m[1]) - gain) > 1e-5:
        raise RuntimeError(f"Gain readback mismatch, see {log}")
    if not path.is_file() or path.stat().st_size != SAMPLES * 8:
        raise RuntimeError(f"Missing or incomplete IQ file: {path}")
    spec, peak = spectrum(path)
    if peak >= 0.9:
        raise RuntimeError(f"Clipping risk (peak {peak:.2f} FS) on RF {port.upper()}: "
                           "lower the level or the gain")
    return spec


def floor(spec):
    return float(db(np.median(spec[BAND])))


def tone_dbfs(on, off):
    near = np.flatnonzero(np.abs(OFFSET - (TONE - CENTER)) <= 5_000)
    i = near[np.argmax(on[near])]
    power = np.sum((on - off)[np.abs(np.arange(FFT) - i) <= 5]) * RATE / FFT
    return float(db(power)) if power > 0 else float("nan")


def highgain(exe, folder, gains):
    input("Set generator RF OFF (feeds stay connected), then press Enter: ")
    rows = []
    for g in gains:
        f = {p: floor(grab(exe, folder, p, f"off_g{g}", g)) for p in PORTS}
        rows.append((g, f["a"], f["b"], f["a"] - f["b"]))
        print(f"gain {g:>2} dB: RF A {f['a']:.2f}, RF B {f['b']:.2f} dBFS/Hz, "
              f"A-B {f['a'] - f['b']:+.2f} dB")
    diff = rows[-1][3]
    if diff <= -5:
        print("RF A floor is lower at high gain: RF A has less gain (gain-stage or calibration fault).")
    elif abs(diff) < 2:
        print("Floors match at high gain: consistent with loss ahead of RF A's first gain stage.")
    else:
        print("Inconclusive: repeat, then compare with the level sweep.")
    return ["gain_db", "rf_a_floor_dbfs_hz", "rf_b_floor_dbfs_hz", "a_minus_b_db"], rows


def sweep(exe, folder, levels, pad, splitter, gain):
    for lvl in levels:
        if lvl - pad - splitter > MAX_PORT_DBM:
            sys.exit(f"Refused: {lvl} dBm gives {lvl - pad - splitter:.1f} dBm at the "
                     f"B210 (limit {MAX_PORT_DBM} dBm).")
    input("Set generator RF OFF, then press Enter: ")
    off = {p: grab(exe, folder, p, "off", gain) for p in PORTS}
    rows = []
    for lvl in sorted(levels):
        input(f"Set {lvl} dBm at {TONE / 1e9:.4f} GHz, RF ON "
              f"({lvl - pad - splitter:.1f} dBm at the B210), then press Enter: ")
        t = {p: tone_dbfs(grab(exe, folder, p, f"on_{lvl:g}", gain), off[p]) for p in PORTS}
        rows.append((lvl, t["a"], t["b"], t["b"] - t["a"]))
        print(f"{lvl:>6} dBm: RF A {t['a']:.2f}, RF B {t['b']:.2f} dBFS, B-A {t['b'] - t['a']:+.2f} dB")
    good = np.array([r for r in rows if not np.isnan(r[3])])
    if len(good) >= 2:
        print(f"B-A offset: mean {good[:, 3].mean():.2f} dB, spread {good[:, 3].std():.2f} dB")
        for col, name in ((1, "A"), (2, "B")):
            print(f"RF {name} slope: {np.polyfit(good[:, 0], good[:, col], 1)[0]:.2f} dB/dB (1.00 expected)")
        print("Constant offset: fixed loss or gain error. Changing offset: compression or a gain fault.")
    return ["generator_dbm", "rf_a_tone_dbfs", "rf_b_tone_dbfs", "b_minus_a_db"], rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["highgain", "sweep"])
    ap.add_argument("--uhd-exe", help="Full path to rx_samples_to_file if not on PATH")
    ap.add_argument("--pad-db", type=float, default=12.0)
    ap.add_argument("--splitter-db", type=float, default=0.0,
                    help="loss of any splitter between generator and pads")
    ap.add_argument("--gains", type=int, nargs="+", default=[10, 30, 50, 60])
    ap.add_argument("--levels", type=float, nargs="+", default=[-36, -30, -24, -18],
                    help="generator levels in dBm")
    ap.add_argument("--gain", type=int, default=10, help="RX gain for the sweep (max 40)")
    args = ap.parse_args()
    if any(g < 0 or g > 76 for g in args.gains) or not 0 <= args.gain <= 40:
        ap.error("Gains must be 0-76 dB (sweep gain 0-40 dB).")
    exe = shutil.which(args.uhd_exe or "rx_samples_to_file") or shutil.which(
        r"C:\Program Files\UHD\lib\uhd\examples\rx_samples_to_file.exe")
    if not exe:
        ap.error("Cannot find rx_samples_to_file. Add UHD examples to PATH or use --uhd-exe.")
    folder = Path("results") / datetime.now().strftime(f"{args.mode}_%Y%m%d_%H%M%S")
    folder.mkdir(parents=True)
    try:
        if args.mode == "highgain":
            header, rows = highgain(str(Path(exe).resolve()), folder, args.gains)
        else:
            header, rows = sweep(str(Path(exe).resolve()), folder, args.levels,
                                 args.pad_db, args.splitter_db, args.gain)
    finally:
        print("Turn generator RF OFF now.")
    with (folder / "summary.csv").open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(header)
        w.writerows(rows)
    print(f"Saved {(folder / 'summary.csv').resolve()}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.TimeoutExpired, KeyboardInterrupt, EOFError) as e:
        sys.exit(f"Stopped: {e}")
