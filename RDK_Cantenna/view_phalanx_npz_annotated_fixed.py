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
import textwrap


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

    # Separate plot and annotation rows prevent text from colliding with axes.
    plt.rcParams.update({'font.size': 10})
    fig = plt.figure(figsize=(14.5, 12.5), facecolor='white')
    gs = fig.add_gridspec(
        6, 1, left=0.12, right=0.96, top=0.78, bottom=0.12,
        height_ratios=[3.2, 0.65, 3.2, 0.65, 2.7, 0.65], hspace=0.56
    )
    axes = [fig.add_subplot(gs[i]) for i in (0, 2, 4)]
    note_axes = [fig.add_subplot(gs[i]) for i in (1, 3, 5)]
    for note_ax in note_axes:
        note_ax.axis('off')

    fig.suptitle('PROJECT PHALANX | Raw Radar Recording Explained',
                 fontsize=18, fontweight='bold', y=.975)
    fig.text(.12, .935, f'Trial: {args.scenario}', fontsize=12, fontweight='bold')
    fig.text(.12, .907, f'Session: {selected.parent.name}  |  File: {selected.name}', fontsize=9)
    metadata_line = (f'{x.size:,} samples  |  {float(fs):,.0f} samples/sec  |  '
                     f'{x.size / float(fs):.3f} sec of recorded signal  |  '
                     f'Stored format: {samples.dtype} ({signedness})')
    fig.text(.12, .881, metadata_line, fontsize=9)
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
        fig.text(.12, .855, '  |  '.join(optional), fontsize=9)

    def add_note(note_ax, message):
        """Explanations have their own layout slot, not an axes coordinate."""
        note_ax.text(0.0, 0.85, textwrap.fill('HOW TO READ IT: ' + message, width=115),
                     fontsize=9, va='top', ha='left', transform=note_ax.transAxes,
                     color='#303030', linespacing=1.3)

    # Panel 1: stored samples (not calibrated ADC volts).
    ax = axes[0]
    if complex_samples:
        ax.plot(t_ms, x.real, linewidth=.9, label='Real (I)')
        ax.plot(t_ms, x.imag, linewidth=.9, alpha=.75, label='Imaginary (Q)')
    else:
        ax.plot(t_ms, x, linewidth=.9, label='Stored samples')
        ax.axhline(float(np.mean(x)), color='tab:orange', linestyle='--', linewidth=1.25,
                   label=f'Mean stored value: {np.mean(x):,.1f}')
    ax.legend(loc='upper right', fontsize=8, framealpha=.88)
    ax.set_title('1 | WAVEFORM — How does the signal vary over time?',
                 loc='left', fontweight='bold', fontsize=12, pad=9)
    ax.set_xlabel('Time within this frame (milliseconds)', labelpad=4)
    ax.set_ylabel('Stored digital value\n(not volts)', labelpad=8)
    ax.set_xlim(t_ms[0], t_ms[-1])
    ax.grid(alpha=.20)
    add_note(note_axes[0], 'Look for repeating patterns, changes in amplitude, and unusually flat regions. '
             'Large stored numbers do not necessarily indicate nearby targets.')

    # Panel 2: simple spectrum; not range or Doppler output.
    ax = axes[1]
    ax.plot(frequencies / 1000.0, magnitude_db, linewidth=.9)
    ax.set_title('2 | FREQUENCY CONTENT — Which frequencies are strongest?',
                 loc='left', fontweight='bold', fontsize=12, pad=9)
    ax.set_xlabel('Frequency in recorded signal (kHz)', labelpad=4)
    ax.set_ylabel('Relative FFT magnitude\n(dB, not dBm)', labelpad=8)
    ax.grid(alpha=.20)
    add_note(note_axes[1], 'Taller peaks represent stronger periodic signal components. '
             'The mean was removed for this graph. Peaks alone do not directly reveal target range or speed.')

    # Panel 3: histogram of stored values.
    ax = axes[2]
    hist_data = np.abs(x) if complex_samples else x
    ax.hist(hist_data, bins=60, edgecolor='none')
    ax.set_title('3 | SAMPLE DISTRIBUTION — Which values occur most often?',
                 loc='left', fontweight='bold', fontsize=12, pad=9)
    ax.set_xlabel('Stored magnitude' if complex_samples else 'Stored digital sample value', labelpad=4)
    ax.set_ylabel('Number of samples', labelpad=8)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.grid(axis='y', alpha=.20)
    add_note(note_axes[2], 'A narrow cluster means samples are concentrated around a few values. '
             'A wider spread means more variation; compare runs made with the same acquisition settings.')

    fig.text(.12, .072, 'LIMITATION: Raw data only. Stored uint16 encoding has not been confirmed against the driver.',
             fontsize=9, fontweight='bold', color='#343434')
    fig.text(.12, .051, 'Do not infer target presence, distance, speed, or false-alarm rate from these plots alone.', fontsize=9)
    fig.text(.12, .032, 'For radar detections, use the PHALANX Range-Doppler and CFAR replay views.', fontsize=9)

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
