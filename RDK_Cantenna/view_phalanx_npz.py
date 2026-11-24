"""Inspect a PHALANX .session frame without changing the recorded data.

Usage:
    python view_phalanx_npz.py gui_1794927292761833900 --frame 0
    python view_phalanx_npz.py gui_1794927471871900300 --frame 5 --save

The script plots stored sample values. It does not assume uint16 signedness,
ADC offset, or voltage calibration; consult driver before interpreting amplitude.
"""
from pathlib import Path
import argparse
import json
import numpy as np
import matplotlib.pyplot as plt


def scalar(data, name, default=None):
    if name not in data.files:
        return default
    a = np.asarray(data[name])
    return a.item() if a.size == 1 else a.tolist()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('session', help='Session ID, .session directory, or frame .npz path')
    ap.add_argument('--frame', type=int, default=0, help='Zero-based frame index')
    ap.add_argument('--root', type=Path, default=Path.home() / 'PHALANX_data' / 'captures')
    ap.add_argument('--save', action='store_true', help='Save PNG in PHALANX_data/analysis/raw_views')
    args = ap.parse_args()

    path = Path(args.session)
    if not path.exists():
        path = args.root / args.session
    if not path.exists() and not str(path).endswith('.session'):
        path = Path(str(path) + '.session')
    if path.is_file() and path.suffix == '.npz':
        selected = path
    elif path.is_dir():
        frames = sorted(path.glob('frame_*.npz'))
        if not frames:
            ap.error(f'No frame_*.npz files in {path}')
        if not (0 <= args.frame < len(frames)):
            ap.error(f'Frame index must be 0 to {len(frames)-1}')
        selected = frames[args.frame]
    else:
        ap.error(f'Could not find recording: {path}')

    with np.load(selected, allow_pickle=False) as data:
        if 'samples' not in data.files:
            ap.error(f'No samples array; available keys: {data.files}')
        raw = np.asarray(data['samples']).copy()
        fs = scalar(data, 'fs_hz')
        signedness = scalar(data, 'signedness', 'unknown')
        declared_dtype = scalar(data, 'dtype', 'unknown')
        frame_meta = scalar(data, 'frame_meta_json', '{}')
        print('File:', selected)
        print('Keys:', ', '.join(data.files))
        print('Stored shape:', raw.shape, 'dtype:', raw.dtype)
        print('Declared dtype:', declared_dtype, 'signedness:', signedness)
        print('Sampling rate (Hz):', fs)
        print('Frame metadata:', frame_meta)

    if raw.ndim == 2 and raw.shape[0] == 1:
        samples = raw[0]
    elif raw.ndim == 1:
        samples = raw
    else:
        ap.error(f'Expected single-channel samples (1,N) or (N,), got {raw.shape}')
    if not fs or not np.isfinite(fs) or fs <= 0:
        ap.error('Missing/invalid fs_hz metadata')
    if not np.issubdtype(samples.dtype, np.number):
        ap.error(f'Unsupported sample dtype {samples.dtype}')
    if not np.all(np.isfinite(samples)):
        print('WARNING: nonfinite samples present')
    print('Stored sample min/max/mean:', np.min(samples), np.max(samples), np.mean(samples))
    print('First 20 stored samples:', samples[:20])

    # Convert to float for plotting, but preserve numeric values and do not guess
    # whether uint16 is offset-binary or bitwise-reinterpreted signed int16.
    x = samples.astype(np.complex128 if np.iscomplexobj(samples) else np.float64)
    time_s = np.arange(x.size) / float(fs)
    fig, ax = plt.subplots(3, 1, figsize=(12, 9), constrained_layout=True)
    if np.iscomplexobj(x):
        ax[0].plot(time_s, x.real, label='Real/I', linewidth=.8)
        ax[0].plot(time_s, x.imag, label='Imag/Q', linewidth=.8, alpha=.7)
        ax[0].legend()
    else:
        ax[0].plot(time_s, x, linewidth=.8)
    ax[0].set(title='Stored samples (uncalibrated)', xlabel='Time (s)', ylabel='Stored amplitude')

    # Remove constant offset for visualization of AC content only.
    centered = x - np.mean(x)
    window = np.hanning(x.size)
    if np.iscomplexobj(x):
        spec = np.fft.fftshift(np.fft.fft(centered * window))
        freq = np.fft.fftshift(np.fft.fftfreq(x.size, d=1/fs))
    else:
        spec = np.fft.rfft(centered * window)
        freq = np.fft.rfftfreq(x.size, d=1/fs)
    ax[1].plot(freq, 20 * np.log10(np.maximum(np.abs(spec), 1e-12)), linewidth=.8)
    ax[1].set(title='Mean-subtracted spectrum (relative, uncalibrated)', xlabel='Frequency (Hz)', ylabel='Magnitude (dB)')

    ax[2].hist(np.abs(x), bins=60)
    ax[2].set(title='Stored absolute amplitude distribution', xlabel='Absolute stored amplitude', ylabel='Samples')
    fig.suptitle(f'PHALANX raw frame: {selected.parent.name} / {selected.stem}')
    if args.save:
        output_dir = Path.home() / 'PHALANX_data' / 'analysis' / 'raw_views'
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f'{selected.parent.name}_{selected.stem}.png'
        fig.savefig(output, dpi=160)
        print('Saved plot:', output)
    plt.show()


if __name__ == '__main__':
    main()
