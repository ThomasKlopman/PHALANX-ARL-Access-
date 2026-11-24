# B210 RX monitor and independent display updates

> **27 Oct 2026 reorganization note.** Paths in this record were updated: this file moved
> from `docs/` to `Part2_B210_Cantenna/docs/`; recordings now go to `PHALANX_data/captures`
> in your home folder (not `RDK_Cantenna/captures`, which is in OneDrive); the session
> verifier is `Part2_B210_Cantenna/scripts/verify_session.py`; and `b310_plfm()` was removed
> (the team uses the B210). The full test record of the first hardware run is
> `PHALANX_B210_Engineering_Record_v1.0_2026-10-22.docx` in this folder. See
> `RDK_Cantenna/CHANGELOG.md` for everything that changed.

Revision 2, prepared 22 October 2026 against the repository snapshot uploaded 20 October,
merged with the current app.py and params.py uploaded after the installer conflict.
The team's b210_plfm() preset and B210 Phaser documentation are preserved. (The
b310_plfm() factory kept here for compatibility was removed on 27 Oct 2026.)
The new GUI RX monitor is independent of the preserved PLFM preset; this update
continues to provide receive-only operation rather than enabling PLFM transmission.

## Behavior

- Simulator acquisition and its DSP run without the temporary 200 ms sleep. (27 Oct: the GUI's
  simulator is now paced at the kit's real 0.8 s per frame inside the simulator itself, so
  recording a simulator run no longer overflows the 16-block writer queue.)
- The worker handles every acquired block. A lock-protected, one-item preview slot is the only data that can be replaced before display.
- A Qt timer draws at up to 10 Hz. Late paints do not produce a growing queue of per-frame Qt events. Display timing is best effort, not a real-time guarantee.
- The B210 option opens a single receive channel, starts one continuous stream 0.5 seconds ahead, and reads successive blocks without rearming between blocks. Default: serial 326C061, 2.45 GHz, 1 MSPS, channel 0, RX2, 10 dB gain, 100 ms per block. No TX stream is created.
- B210 mode displays live I and Q plus a preview FFT. Range/Doppler/scope views are disabled because raw I/Q alone does not establish a radar waveform, range, or velocity.
- Simulator/Cantenna radar processing runs for every acquired frame; preview selection happens after processing. B210 raw mode performs receive validation and records full blocks; it does not run a radar detector.
- The plotting preview uses up to 4096 time samples and 8192 FFT samples. These limits affect drawings only. Saved blocks contain every received sample in the block.

## Recording

Check **Record all blocks (session in PHALANX_data/captures)** before Start. A bounded background writer owns a copy of each submitted block. It writes one schema-v1 NPZ per block and a `session.json` manifest. Recording never selects data according to the display refresh rate.

Example:

```text
PHALANX_data/captures/gui_....session/
  session.json
  frame_00000000.npz
  frame_00000001.npz
  ...
```

- The writer queue holds at most 16 pending blocks, plus the current write. RAM does not accumulate for the entire recording. A separate single preview and current acquisition block also consume RAM.
- At the default 1 MSPS, complex64 sample payload is 8 MB/s, approximately 480 MB/minute. Queue sample payload is up to approximately 12.8 MB at this setting, excluding the current write, acquisition buffers, preview, and Python/GUI overhead. This is not a measured total application RAM requirement.
- Raw words/complex samples and per-frame source metadata, device timestamps, actual settings, and error fields are preserved. Existing schema-v1 files without the new optional frame metadata field still load.
- Normal Stop finishes the current complete block and drains the writer before marking the session complete.
- Reported RX errors, device-time gaps, a full writer queue, and disk errors stop the run explicitly. Such sessions are marked invalid; interrupted sessions remain incomplete. Normal session replay rejects both states. Already accepted blocks are drained after queue exhaustion when the disk remains writable. A failed block/partial RX cannot be recovered by this software.
- A complete session means no loss was reported or detected by these checks and all acquired complete blocks were committed. It does not prove RF sensitivity, calibrated timing, absence of all possible hardware faults, or unlimited host throughput.
- Open `session.json` with **Replay recording**. Session replay loads one block at a time; the older single-NPZ replay path retains its earlier in-memory behavior.
- Session files are a new PHALANX wrapper around the existing NPZ schema. They are not SigMF files and do not open directly in GNU Radio File Source.
- The per-block NPZ layout favors bench validation and compatibility. File count and I/O overhead need evaluation for long recordings or higher sample rates.

Check a completed session from `RDK_Cantenna`:

```powershell
.\.venv\Scripts\python.exe Part2_B210_Cantenna\scripts\verify_session.py "$HOME\PHALANX_data\captures\gui_....session\session.json"
```

The command checks block/sample totals, parameters, sequence numbers, archive loading/CRC, and available device timestamps. It does not compare to an independent original sample stream or authenticate provenance.

## First hardware check

1. Run `python -m pytest -q` using the repository virtual environment.
2. Run `RDK_Cantenna_GUI.py` and verify Simulator remains responsive with no sleep added.
3. Stop. Select **B210 RX monitor**, confirm serial 326C061, 1 MSPS, 2.45 GHz, and 10 dB gain.
4. Check recording and click Start. Close other applications holding the radio first.
5. Run 10 seconds initially, then Stop. Confirm acquired and saved block counts match after the writer drains. **Preview replacements** may increase normally and are not discarded acquired/recorded samples.
6. Run the session verifier, then select the session manifest in Replay recording. Inspect I/Q and spectrum.
7. Repeat for 60 seconds. Retain console/log output, session manifest, settings, runtime, and verifier output. Increase rate only after measuring stable behavior at the current setting.

Any overflow is an acquisition failure to investigate, not something the timer fixes or ignores. Windows scheduling, CPU contention, USB controllers, disk speed, and recording format still limit sustainable operation. The application is not a hard real-time system.

## Validation performed here

53 tests passed in a Linux/Python 3.12 headless review environment: the original 39 plus 14 new checks. The additions cover continuous mocked UHD reception, partial receives, actual readbacks and owned buffers, overflow and timestamp gaps, recording queue exhaustion/disk errors, invalid/incomplete session rejection, metadata and bit-exact replay, a stalled GUI with every block recorded, raw-I/Q GUI behavior, non-default replay parameters, source-failure invalidation, and session verification.

The key stalled-display test acquires and records 50 synthetic blocks while the GUI event loop is not serviced. All 50 replay byte-for-byte; 49 old previews are superseded. Its writer queue is sized for that finite, unpaced test burst; a separate test verifies explicit failure when queue capacity is exceeded.

No B210 is attached to this environment. The continuous driver and new GUI mode require your Windows/B210 acceptance tests. The earlier user-provided 0.1-second captures do not validate this revised continuous path.

## Architecture and future work

Acquisition/DSP and the recorder run outside the GUI thread. For the current raw-I/Q monitor there is no heavy radar DSP in the receive loop. Future high-rate radar processing should use a separately bounded DSP pipeline with explicit overload policy; heavy processing must not starve UHD receives. Keep the latest-only rule confined to preview data unless an approved processing specification deliberately permits other data reduction.

The earlier B310 transceiver module remains in the repository, but the GUI's USRP selection now targets B210 receive-only operation. Timed PLFM transmission, RF loopback, drone detection, and multi-channel angle estimation require separate integration and calibration.

References:
- https://doc.qt.io/qt-6/qtimer.html
- https://files.ettus.com/manual/page_stream.html
- https://files.ettus.com/manual/structuhd_1_1rx__metadata__t.html
