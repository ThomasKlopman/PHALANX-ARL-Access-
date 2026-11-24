"""Where recordings are written — deliberately OUTSIDE the git clone and OneDrive.

Why: the team's clone lives in OneDrive. A B210 session at 1 MS/s writes about
8 MB/s (≈480 MB per minute) as ~10 small files per second; OneDrive tries to
sync every one and can hold a file open while the recorder renames it, which
leaves an invalid session. Recordings are data, not source code, so they
belong in a local folder that neither git nor OneDrive touches.

Default:  <your home folder>/PHALANX_data      (e.g. C:\\Users\\<you>\\PHALANX_data)
Override: set the environment variable PHALANX_DATA to any folder, e.g. in
          PyCharm → Run Configuration → Environment variables: PHALANX_DATA=D:\\radar

Layout inside it:
    captures/   GUI sessions (B210 and kit) and calibrate.py captures

The small DSP-01 kit dataset stays in Part1_Pasternack_Kit/dataset/ so its frozen
detector settings and results files can be committed as test evidence (the .npz
recordings themselves are git-ignored).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

ENV_VAR = "PHALANX_DATA"
_warned = False


def data_root() -> Path:
    """The folder that holds captures/ (not created here)."""
    global _warned
    env = os.environ.get(ENV_VAR, "").strip()
    root = Path(env).expanduser() if env else Path.home() / "PHALANX_data"
    if "onedrive" in str(root).lower() and not _warned:
        _warned = True
        log.warning("%s is inside OneDrive; large recordings may be corrupted by sync. "
                    "Set %s to a local folder.", root, ENV_VAR)
    return root


def captures_dir() -> Path:
    return data_root() / "captures"
