"""Stage 1 — signal conditioning: the stream of samples → the (N_p, N_f) pulse matrix.

CN0566 equivalent: "Channel cal + Σ" and "Reshape CPI". On the Phaser the
pulse boundaries are known exactly (the host sends the timed stream_cmd that
starts the chirp). On the Cantenna the kit free-runs and the capture starts
wherever it starts, so the boundaries have to be *estimated* from the data.
That estimation, and the up/down parity it cannot resolve on its own, is the
only piece of Phase-1-specific DSP in the whole chain.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import filtfilt, get_window, iirnotch


def remove_dc(x: np.ndarray) -> np.ndarray:
    """Remove the ADC offset / mixer DC. Real or complex."""
    x = np.asarray(x)
    return (x - x.mean()).astype(np.complex64 if np.iscomplexobj(x) else np.float32)


def notch(x: np.ndarray, fs_hz: float, freqs_hz, q: float = 30.0) -> np.ndarray:
    """Zero-phase IIR notch(es) on the raw IF — for fixed spurious tones.

    Kit s/n 5035 shows a CW spur at ≈3.44 kHz (≈17 dB above the noise floor)
    that is NOT a radar return: it sits off the 125 Hz sweep comb and aliases
    in slow time to ≈60 Hz, i.e. it masquerades as a 20 m target at ±3.7 m/s.
    A notch of Q=30 at 3.44 kHz is ~115 Hz wide — under half a range bin.
    filtfilt keeps zero phase so the ramp symmetry/alignment is untouched.
    """
    y = np.asarray(x, dtype=np.float64)
    for f0 in np.atleast_1d(freqs_hz):
        b, a = iirnotch(f0, q, fs=fs_hz)
        y = filtfilt(b, a, y)
    return y.astype(np.float32)


def window(n: int, name: str | None) -> np.ndarray:
    if not name or name == "rect":
        return np.ones(n, dtype=np.float32)
    return get_window(name, n, fftbins=True).astype(np.float32)


@dataclass
class RampAlignment:
    offset: int            # sample index of a segment start (turnaround) in the frame
    score: np.ndarray      # folded score, length n_fast (for plotting)
    confidence: float      # symmetry: peak correlation (≳0.8 good); transient: peak/typical (≳3 good)
    segment_means: np.ndarray  # mean of each segment after alignment (parity hint)
    method: str = "symmetry"
    alt_offset: int | None = None      # what the other method said (sanity check)
    alt_confidence: float | None = None


def _transient_score(x: np.ndarray, n_fast: int) -> np.ndarray:
    """|first difference| folded modulo n_fast: peaks at a PLL/VCO retune glitch."""
    d = np.abs(np.diff(x, prepend=x[0]))
    n_full = (len(d) // n_fast) * n_fast
    return d[:n_full].reshape(-1, n_fast).mean(axis=0)


def _symmetry_score(x: np.ndarray, n_fast: int, half_width: int | None = None) -> np.ndarray:
    """Mirror-symmetry of the waveform about each fold index, modulo n_fast.

    Triangle FMCW with stationary returns: the down-ramp beat phase is
        phi_down(t_l) = phi_up(T - t_l)
    so the IF waveform is an exact mirror image about every turnaround
    (both the f_stop and the f_start ones). The index of best even symmetry
    is therefore a segment start — no transient needed. Doppler breaks the
    symmetry only slightly (phase drift of a few degrees per ms).
    """
    m = half_width or max(4, n_fast // 2 - 1)
    n = len(x)
    score = np.zeros(n_fast, dtype=np.float64)
    norm = np.zeros(n_fast, dtype=np.float64)
    for k in range(n_fast):
        centres = np.arange(k, n, n_fast)
        centres = centres[(centres - m >= 0) & (centres + m < n)]
        if centres.size == 0:
            continue
        idx = centres[:, None] + np.arange(1, m + 1)[None, :]
        right = x[idx]
        left = x[centres[:, None] - np.arange(1, m + 1)[None, :]]
        score[k] = np.sum(left * right)
        norm[k] = np.sqrt(np.sum(left ** 2) * np.sum(right ** 2)) + 1e-12
    return score / norm      # normalised correlation in [-1, 1]


def estimate_ramp_offset(x: np.ndarray, n_fast: int, method: str = "symmetry") -> RampAlignment:
    """Find where ramp segments start inside a free-running capture.

    method="symmetry" (default): mirror symmetry about the triangle turnaround
        (see _symmetry_score). Works on the real Cantenna, whose turnaround
        leaves no visible transient. Confidence = peak normalised correlation
        (1.0 = perfect mirror; ≳0.8 is a trustworthy alignment).
    method="transient": folded |diff| — for hardware with a flyback / PLL
        glitch (sawtooth sweeps, or if a future kit shows one).

    Both are computed; the other method's answer is returned as alt_offset so
    `calibrate.py edge` can show whether they agree.

    Neither can tell which segments are up-ramps and which are down-ramps:
    that parity is a one-time calibration (walk toward the radar, check the
    sign) stored as ProcessorConfig.ramp_parity.
    """
    x = np.asarray(x, dtype=np.float32)
    x = x - x.mean()
    sym = _symmetry_score(x, n_fast)
    tr = _transient_score(x, n_fast)

    def _conf(sc):
        return float((sc.max() - np.median(sc)) / (sc.std() + 1e-12))

    sym_off, tr_peak = int(np.argmax(sym)), int(np.argmax(tr))
    # a ringing transient can put its largest step a sample or two after the
    # edge: walk back (circularly) to the start of the burst
    tr_off, base = tr_peak, np.median(tr)
    half = base + 0.5 * (tr[tr_peak] - base)
    for _ in range(min(5, n_fast - 1)):
        prev = (tr_off - 1) % n_fast
        if tr[prev] > half:
            tr_off = prev
        else:
            break
    # symmetry confidence = the normalised correlation itself (1.0 = perfect mirror);
    # transient confidence = how far the peak stands above the typical fold bin
    if method == "symmetry":
        offset, score, conf, alt, alt_conf = sym_off, sym, float(sym[sym_off]), tr_off, _conf(tr)
    else:
        offset, score, conf, alt, alt_conf = tr_off, tr, _conf(tr), sym_off, float(sym[sym_off])
    n_seg = (len(x) - offset) // n_fast
    seg = x[offset:offset + n_seg * n_fast].reshape(n_seg, n_fast)
    return RampAlignment(offset, score, conf, seg.mean(axis=1), method, alt, alt_conf)


def reshape_cpi(x: np.ndarray, n_fast: int, offset: int = 0, segments_per_pri: int = 1,
                parity: int = 0) -> np.ndarray:
    """Cut the sample stream into the slow-time × fast-time pulse matrix (N_p, n_fast).

    Row m is x[start + m*step : ... + n_fast] with
        start = offset + parity*n_fast       (parity picks up- vs down-ramps)
        step  = n_fast * segments_per_pri    (2 → every other triangle segment)
    On the CN0566 this is a plain reshape with offset=0, segments_per_pri=1.
    """
    x = np.asarray(x)
    start = offset + (parity % 2) * n_fast
    step = n_fast * segments_per_pri
    n_p = (len(x) - start) // step
    if n_p < 1:
        raise ValueError(f"frame too short: {len(x)} samples, need ≥{start + step}")
    idx = start + np.arange(n_p)[:, None] * step + np.arange(n_fast)[None, :]
    return x[idx]
