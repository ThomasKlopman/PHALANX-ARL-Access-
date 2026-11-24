"""Make `import phalanx` work no matter how the tests are launched
(PyCharm test runner, `pytest` from the repo root, or from RDK_Cantenna/)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
