"""Stage 2 — fast-time processing: pulse matrix → range-profile matrix (N_p, N_r).

Two ways to get range, same output shape and dtype (complex64) so the rest
of the chain does not care which one ran:

* FMCW dechirp (Cantenna, Phase 1): the mixer already multiplied RX by TX,
  so each ramp is a sum of beat tones; one FFT along fast time IS the range
  profile.  Real input → rfft → positive beat frequencies only (a real
  signal's spectrum is Hermitian, the negative half carries no new info).

* PLFM matched filter (CN0566, Phase 2): the receiver gives the raw delayed
  chirp, so range comes from correlating with the reference chirp s[n]:
      Y = IFFT( FFT(x) · conj(FFT(s)) )          ("pulse compression")

Important consequence for Phase 1: the Cantenna gives a *real* IF, but the
range FFT output is *complex*. From here on the chain is I/Q, exactly like
the Phaser — the phase of each range bin across pulses is what carries
Doppler, including its sign. (The "I/Q preservation" rule from the design
notes is satisfied at this stage, not at the ADC.)
"""
from __future__ import annotations

import numpy as np
from scipy import fft as sfft

from .conditioning import window


def range_fft(pulses: np.ndarray, win: str | None = "hann", n_fft: int | None = None) -> np.ndarray:
    """FMCW dechirp range transform along fast time.

    pulses : (N_p, N_f) real or complex
    returns: (N_p, N_r) complex64, N_r = n_fft//2+1 for real input, n_fft for complex
    """
    pulses = np.asarray(pulses)
    n_f = pulses.shape[-1]
    n_fft = n_fft or n_f
    w = window(n_f, win)
    xw = pulses * w
    if np.iscomplexobj(pulses):
        return sfft.fft(xw.astype(np.complex64), n=n_fft, axis=-1).astype(np.complex64)
    return sfft.rfft(xw.astype(np.float32), n=n_fft, axis=-1).astype(np.complex64)


def pulse_compress(pulses: np.ndarray, ref: np.ndarray, n_fft: int | None = None,
                   win: str | None = None) -> np.ndarray:
    """PLFM matched filter (Phase 2). pulses (N_p, N_f) complex, ref (N_s,) complex.

    Returns (N_p, N_f) complex64 range profiles; bin k ↔ delay k/fs.
    A window on the reference tapers the compressed-pulse sidelobes
    (at the usual cost of ~1.4× main-lobe width for Hann).
    """
    pulses = np.asarray(pulses, dtype=np.complex64)
    ref = np.asarray(ref, dtype=np.complex64)
    n_f = pulses.shape[-1]
    n_fft = n_fft or sfft.next_fast_len(n_f + len(ref) - 1)
    if win:
        ref = ref * window(len(ref), win)
    S = sfft.fft(ref, n=n_fft)
    X = sfft.fft(pulses, n=n_fft, axis=-1)
    y = sfft.ifft(X * np.conj(S)[None, :], axis=-1)
    return y[:, :n_f].astype(np.complex64)


def conjugate_rows(rp: np.ndarray, rows: np.ndarray) -> np.ndarray:
    """Triangle 'both' mode: a down-ramp's beat is cos(−2π f_b t + 2π f_c τ) so its
    positive-frequency bin carries e^{−j2π f_c τ} — the conjugate of the up-ramp's.
    Conjugating those rows makes the slow-time phase rotate consistently."""
    rp = rp.copy()
    rp[rows] = np.conj(rp[rows])
    return rp
