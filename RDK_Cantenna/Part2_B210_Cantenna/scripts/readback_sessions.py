r"""Compare the B210's reported settings across recorded sessions (RF-01 "three restarts").

    python Part2_B210_Cantenna\scripts\readback_sessions.py            # newest 3 sessions
    python Part2_B210_Cantenna\scripts\readback_sessions.py --last 5
    python Part2_B210_Cantenna\scripts\readback_sessions.py <session> <session> ...

For each session this prints the verify_session summary and the device read-back the
driver stored in the first block (rate, frequency, gain, bandwidth, antenna, clock/time
source, UHD version, device args), then says whether every session's read-back is
identical. Sessions that are incomplete or invalid are reported and excluded.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # RDK_Cantenna/ (the phalanx package)

from phalanx.capture import load_capture        # noqa: E402
from phalanx.paths import captures_dir          # noqa: E402
from phalanx.session import verify_session      # noqa: E402

READBACK_KEYS = ("model", "serial", "rx_rate_hz", "rx_frequency_hz", "rx_gain_db",
                 "rx_bandwidth_hz", "antenna", "channel", "clock_source", "time_source",
                 "uhd_version", "device_args")


def newest_sessions(n):
    folders = sorted(captures_dir().glob("gui_*.session"), key=lambda p: p.stat().st_mtime)
    return folders[-n:]


def readback(session):
    first = sorted(Path(session).glob("frame_*.npz"))[0]
    frames, _, _ = load_capture(first)
    device = frames[0].meta.get("device", {})
    return {k: device.get(k) for k in READBACK_KEYS}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sessions", nargs="*", help="Session folders (default: newest --last)")
    parser.add_argument("--last", type=int, default=3)
    args = parser.parse_args()
    sessions = [Path(s) for s in args.sessions] or newest_sessions(args.last)
    if not sessions:
        parser.exit(1, f"No sessions found in {captures_dir()}\n")

    readbacks = []
    for session in sessions:
        session = session if session.is_dir() else session.parent
        print(f"== {session.name}")
        try:
            summary = verify_session(session)
        except Exception as error:
            print(f"   FAIL: {error}  (excluded from comparison)")
            continue
        print(f"   {summary['blocks']} blocks, {summary['sample_duration_s']:.1f} s at "
              f"{summary['sample_rate_hz']/1e6:g} MS/s, {summary['device_timed_blocks']} device-timed: PASS")
        rb = readback(session)
        for key in READBACK_KEYS:
            print(f"   {key:>16}: {rb[key]}")
        readbacks.append(rb)

    print()
    if len(readbacks) < 2:
        print("Fewer than two complete sessions; nothing to compare.")
        sys.exit(1)
    differing = [k for k in READBACK_KEYS if len({repr(rb[k]) for rb in readbacks}) > 1]
    if differing:
        print(f"READ-BACK DIFFERS across {len(readbacks)} sessions in: {', '.join(differing)}")
        sys.exit(1)
    print(f"READ-BACK IDENTICAL across {len(readbacks)} complete sessions.")


if __name__ == "__main__":
    main()
