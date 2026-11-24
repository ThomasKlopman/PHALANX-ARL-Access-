"""Entry point: live range-Doppler diagnostic display (matplotlib).
Run from RDK_Cantenna/:  python RDK_Cantenna_DSP.py [--sim | --replay file.npz] [--no-mti] ...
(The DSP itself lives in the `phalanx.dsp` package; this just launches
Part1_Pasternack_Kit/scripts/live_rd.py.)"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "Part1_Pasternack_Kit" / "scripts"))

from live_rd import main  # noqa: E402

if __name__ == "__main__":
    main()
