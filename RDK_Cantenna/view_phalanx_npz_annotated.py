"""Beginner-friendly, read-only viewer for a PHALANX recorded NPZ frame.

Examples (PowerShell, from RDK_Cantenna):
    python .\view_phalanx_npz_annotated.py gui_1794927292761833900 --frame 0 --scenario "Blank / no intentional target"
    python .\view_phalanx_npz_annotated.py gui_1794927471871900300 --frame 0 --scenario "Single cadet walking toward radar" --save

Shows *stored* samples, not calibrated voltages, range, or velocity.
"""
import argparse
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator


def scalar(data, key, default=None):
    if key not in data.files:
        return default
    item = np.asarray(data[key])
    return item.item() if item.size == 1 else default


def resolve_file(session, frame, root, parser):
    path = Path(session).expanduser()
    if not path.exists():
        path = root / session
    if not path.exists() and path.suffix != '.session':
        path = Path(str(path) + '.session')
    if path.is_file() and path.suffix.lower() == '.npz':
        return path
    if not path.is_dir():
        parser.error(f'Cannot find recording: {path}')
    frames = sorted(path.glob('frame_*.npz'))
    if not frames:
        parser.error(f'No frame_*.npz files in {path}')
    if frame < 0 or frame >= len(frames):
        parser.error(f'Frame index must be between 0 and {len(frames)-1}')
    return frames[frame]


def main():
    parser = argparse.ArgumentParser(description='Annotated PHALANX NPZ raw-data viewer')
    parser.add_argument('session', help='Session ID, .session folder, or frame .npz file')
    parser.add_argument('--frame', type=int, default=0, help='Frame index, starting at zero')
    parser.add_argument('--scenario', default='Not yet labeled', help='Your trial annotation, e.g. "Blank run"')
    parser.add_argument('--root', type=Path, default=Path.home() / 'PHALANX_data' / 'captures')
    parser.add_argument('--save', action='store_true', help='Save annotated PNG in PHALANX_data/analysis/raw_views')
    parser.add_argument('--no-show', action='store_true', help='Do not open an interactive plot window')
    args = parser.parse_args()

    selected = resolve_file(args.session, args.frame, args.root, parser)
    with np.load(selected, allow_pickle=False) as data:
        if 'samples' not in data.files:
            parser.error(f'No samples array in file. Keys: {data.files}')
        raw = np.asarray(data['samples']).copy()
        fs = scalar(data, 'fs_hz')
        signedness = str(scalar(data, 'signedness', 'unknown'))
        declared_dtype = str(scalar(data, 'dtype', 'unknown'))
        waveform = str(scalar(data, 'waveform', 'unknown'))
        ramp_time = scalar(data, 'ramp_time_s')
        center_frequency = scalar(data, 'center_frequency_hz')
        bandwidth = scalar(data, 'bandwidth_hz')

    if raw.ndim == 2 and raw.shape[0] == 1:
        samples = raw[0]
    elif raw.ndim == 1:
        samples = raw
    else:
        parser.error(f'Only single-channel (1,N) or (N,) samples supported; got {raw.shape}')
    if samples.size < 4:
        parser.error('Frame has too few samples for a meaningful plot')
    if not isinstance(fs, (int, float, np.number)) or not np.isfinite(fs) or fs <= 0:
        parser.error('No valid fs_hz in metadata')
    if not np.issubdtype(samples.dtype, np.number):
        parser.error(f'Samples must be numeric; got {samples.dtype}')
    if not np.all(np.isfinite(samples)):
        parser.error('Samples contain NaN or infinity')

    complex_samples = np.iscomplexobj(samples)
    x = samples.astype(np.complex128 if complex_samples else np.float64)
    t_ms = np.arange(x.size) * (1000.0 / float(fs))
    centered = x - np.mean(x)    # only for the spectrum: removes stored DC offset
    tapered = centered * np.hanning(x.size)
    if complex_samples:
        spectrum = np.fft.fftshift(np.fft.fft(tapered))
        frequencies = np.fft.fftshift(np.fft.fftfreq(x.size, d=1/fs))
    else:
        spectrum = np.fft.rfft(tapered)
        frequencies = np.fft.rfftfreq(x.size, d=1/fs)
    magnitude_db = 20 * np.log10(np.maximum(np.abs(spectrum), 1e-12))

    plt.rcParams.update({'font.size': 10})
    fig, axes = plt.subplots(3, 1, figsize=(13.2, 11.2),
                             gridspec_kw={'height_ratios': [1.1, 1, 0.85]})
    fig.subplots_adjust(left=.09, right=.96, top=.77, bottom=.13, hspace=.77)

    fig.suptitle('PROJECT PHALANX  |  Raw Radar Recording Explained',
                 fontsize=17, fontweight='bold', y=.975)
    fig.text(.09, .932, f'Trial: {args.scenario}', fontsize=12, fontweight='bold')
    fig.text(.09, .903, f'Session: {selected.parent.name}  |  File: {selected.name}', fontsize=9)
    metadata_line = (f'{x.size:,} samples  •  {float(fs):,.0f} samples/sec  •  '
                     f'{x.size / float(fs):.3f} sec of recorded signal  •  '
                     f'Stored format: {samples.dtype} ({signedness})')
    fig.text(.09, .878, metadata_line, fontsize=9)
    optional = []
    if waveform != 'unknown':
        optional.append(f'Waveform: {waveform}')
    if isinstance(center_frequency, (float, int)) and np.isfinite(center_frequency) and center_frequency > 0:
        optional.append(f'RF center: {center_frequency/1e9:.3f} GHz')
    if isinstance(bandwidth, (float, int)) and np.isfinite(bandwidth) and bandwidth > 0:
        optional.append(f'Sweep bandwidth: {bandwidth/1e6:.2f} MHz')
    if isinstance(ramp_time, (float, int)) and np.isfinite(ramp_time) and ramp_time > 0:
        optional.append(f'Ramp: {ramp_time * 1e3:.2f} ms')
    if optional:
        fig.text(.09, .853, '  |  '.join(optional), fontsize=9)

    # Panel 1: time-domain data, exactly as stored; no guessed ADC conversion.
    ax = axes[0]
    if complex_samples:
        ax.plot(t_ms, x.real, linewidth=.9, label='Real (I)')
        ax.plot(t_ms, x.imag, linewidth=.9, alpha=.75, label='Imaginary (Q)')
        ax.legend(loc='upper right')
    else:
        ax.plot(t_ms, x, linewidth=.9, label='Stored samples')
        ax.axhline(float(np.mean(x)), color='tab:orange', linestyle='--', linewidth=1.25,
                   label=f'Mean stored value: {np.mean(x):,.1f}')
        ax.legend(loc='upper right')
    ax.set_title('1  |  WAVEFORM — How does the recorded signal vary over time?',
                 loc='left', fontweight='bold', pad=12)
    ax.set_xlabel('Time within this frame (milliseconds)')
    ax.set_ylabel('Stored digital sample value (not volts)')
    ax.set_xlim(t_ms[0], t_ms[-1])
    ax.grid(alpha=.22)
    ax.text(0, -0.43, 'HOW TO READ IT: Look for changes, repeating patterns, and unusually flat or saturated segments. '
            'A larger number does not automatically mean a closer target.',
            transform=ax.transAxes, va='top', fontsize=9, wrap=True)

    # Panel 2: relative spectrum, NOT a range/velocity Doppler chart.
    ax = axes[1]
    ax.plot(frequencies / 1000.0, magnitude_db, linewidth=.9)
    ax.set_title('2  |  FREQUENCY CONTENT — Which signal frequencies are strongest?',
                 loc='left', fontweight='bold', pad=12)
    ax.set_xlabel('Frequency within the recorded signal (kHz)')
    ax.set_ylabel('Relative FFT magnitude (dB; not dBm)')
    ax.grid(alpha=.22)
    ax.text(0, -0.43, 'HOW TO READ IT: Peaks indicate strong repeated variations at those frequencies. '
            'The constant offset is removed before this FFT. Peaks are not directly target speed or distance.',
            transform=ax.transAxes, va='top', fontsize=9, wrap=True)

    # Panel 3: histogram of the stored samples, not absolute amplitude.
    ax = axes[2]
    hist_data = np.abs(x) if complex_samples else x
    ax.hist(hist_data, bins=60, edgecolor='none')
    ax.set_title('3  |  SAMPLE DISTRIBUTION — Which recorded values occur most often?',
                 loc='left', fontweight='bold', pad=12)
    ax.set_xlabel('Stored sample magnitude' if complex_samples else 'Stored digital sample value')
    ax.set_ylabel('Number of samples')
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.grid(axis='y', alpha=.22)
    ax.text(0, -0.43, 'HOW TO READ IT: A narrow cluster means many similar stored values; a broad spread means '
            'more variation. Compare recordings using the same settings and identical axis scales.',
            transform=ax.transAxes, va='top', fontsize=9, wrap=True)

    fig.text(.09, .055, 'IMPORTANT  |  This is a raw-data inspection tool, NOT a calibrated radar measurement. '
             'The uint16 encoding has not been verified against the driver. ',
             fontsize=9, fontweight='bold')
    fig.text(.09, .037, 'Do not infer target presence, range, radial velocity, or false-alarm rate from these plots alone. '
             'Use the PHALANX Range–Doppler/CFAR replay for those questions.', fontsize=9)

    print('File:', selected)
    print('Trial label:', args.scenario)
    print('Shape:', raw.shape, '| dtype:', raw.dtype, '| declared dtype:', declared_dtype)
    print('Signedness:', signedness, '| sample rate:', float(fs), 'Hz')
    print('Stored min / max / mean:', np.min(samples), np.max(samples), np.mean(samples))
    print('First 20 stored samples:', samples[:20])
    if args.save:
        output_dir = Path.home() / 'PHALANX_data' / 'analysis' / 'raw_views'
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f'{selected.parent.name}_{selected.stem}_annotated.png'
        fig.savefig(output, dpi=170)
        print('Saved plot:', output)
    if not args.no_show:
        plt.show()
    plt.close(fig)


if __name__ == '__main__':
    main()
