"""Entry point: PHALANX operator GUI (UI-01).  Run from RDK_Cantenna/:  python RDK_Cantenna_GUI.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from phalanx.gui.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
