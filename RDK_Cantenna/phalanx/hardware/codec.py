"""Pure sample-decoding helpers for the Cantenna kit (no pyvisa needed).

Kept separate from the driver so the simulator and the tests can share them
without importing the VISA layer, and so `python -m phalanx.hardware.cantenna`
does not import its own module twice.
"""
from __future__ import annotations

import re

import numpy as np

_HEX_RE = re.compile(r"[0-9A-Fa-f]{4}")


def decode_hex_words(text: str) -> np.ndarray:
    """'0F3A7FFF...' → uint16 array. Ignores whitespace / terminators."""
    words = _HEX_RE.findall(text.replace(" ", "").replace("\r", "").replace("\n", ""))
    return np.array([int(w, 16) for w in words], dtype=np.uint16)


def raw_to_float(raw: np.ndarray, signedness: str = "auto") -> tuple[np.ndarray, str]:
    """uint16 ADC words → float32 centred on zero, full scale = ±1.0.

    The manual does not state whether the words are offset-binary (0..65535,
    mid-scale 32768) or two's complement. With 'auto' we pick whichever
    interpretation gives the smaller standard deviation: a real audio-band
    signal is tightly clustered around its own mid-scale, while the wrong
    interpretation splits it into two clusters ±32768 apart.

    First contact with kit s/n 5035 (8 Oct 2026): auto-detect said 'unsigned'.
    """
    raw = np.asarray(raw, dtype=np.uint16)
    as_unsigned = raw.astype(np.float32) - 32768.0
    as_signed = raw.view(np.int16).astype(np.float32)
    if signedness == "auto":
        signedness = "unsigned" if as_unsigned.std() <= as_signed.std() else "signed"
    x = as_unsigned if signedness == "unsigned" else as_signed
    return (x / 32768.0).astype(np.float32), signedness
