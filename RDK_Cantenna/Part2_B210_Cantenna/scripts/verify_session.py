r"""Check a completed GUI recording (Part 2 acceptance evidence).

    python Part2_B210_Cantenna\scripts\verify_session.py "C:\Users\<you>\PHALANX_data\captures\gui_....session"

Checks archived block counts, NPZ CRCs via loading, sample totals, parameter
consistency, sequence numbers, and device-time continuity when timestamps exist.
It is not an independent reference-sample comparison or an RF calibration.
The checks themselves live in phalanx.session.verify_session (shared, tested).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # RDK_Cantenna/ (the phalanx package)

from phalanx.session import verify_session  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session", help="Session folder or session.json")
    args = parser.parse_args()
    try:
        result = verify_session(args.session)
    except Exception as error:
        parser.exit(1, f"FAIL: {error}\n")
    for name, value in result.items():
        print(f"{name}: {value}")
    print("PASS: complete session; block/sample totals and available timing agree")


if __name__ == "__main__":
    main()
