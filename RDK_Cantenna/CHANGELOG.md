# Phase 1 change log

Newest first. Add an entry whenever you move files or change shared code (`phalanx/`), so the
next team can see what changed and why without reading every commit.

## 2026-11-12 — Stage-by-stage check of the B210 chain; design document sections written

No hardware on hand; nothing here has run on the B210. Tests 115 → 118.

* `Part2_B210_Cantenna/scripts/stage_dump.py` (new): runs one frame through the B210 FMCW chain one
  stage at a time (F1 raw I/Q, F2 latency, F3 dechirp, F4 filter + decimate, then Part 1's stages
  1, 3 to 9), prints expected against obtained, and checks the result is identical to
  `RangeDopplerProcessor.process`. `--out` writes a figure and a `.json` of the table.
* **Simulated frame** (8 MS/s, 6 MHz, leakage −24 dBFS, latency 83 samples, walker at 80 m closing
  at 1.4 m/s): 16 of 16 checks pass. Latency 83 samples; leakage at DC −24.0 dBFS; frame 4096
  complex at 20 kS/s; sweep matrix 25 × 80; range profiles 25 × 41, strongest cell 3 (75 m); MTI
  gain at the target +0.7 dB as predicted; map 24 × 41; noise floor −72.7 dB; threshold 9.8 dB
  over the local mean; one detection at 79 m, +1.40 m/s, 27 dB. With `--speed 0` the detection
  goes away (MTI), and the script exits non-zero.
* **Recorded frame** (`b210_fmcw_20261110T163939Z.npz`, frame index 5 = seq 7, 8 MS/s, real beat;
  raw I/Q is not stored, so the check starts after F4): latency 83, leakage −26.2 dBFS, shapes as
  designed, strongest cell beyond 0 is cell 1 (inside the blind range), cell 0 −13.1 → −34.2 dB
  after MTI, noise floor −53.8 dB, no cell over threshold, no detections, identical to the pipeline.
* Figures and tables: `Part2_B210_Cantenna/docs/dsp_stages_simulated_frame.png/.json` and
  `dsp_stages_recorded_frame.png/.json`.
* `SimulatedB210Fmcw.last_raw`: the last frame's raw I/Q, kept for the script. No behaviour change.
* Design document (Word file in the XE401 folder): "Signal processing chain" and "Operator display"
  written from the above; the placeholder sentences and the last limitations row updated.
* Not confirmed, unchanged from the entry below: no real target detected, complex beat and the
  display's live source not yet run on the B210, CDT Klopman has not reviewed the display's arming.

## 2026-11-12 — Part 2 DSP and operator display, first draft (no hardware on hand)

Written and tested against the B210 simulator and the three hardware recordings of 10 Nov; **none
of it has run on the B210 yet.** Package version 0.3.0 → 0.4.0. Tests 98 → 115.

### DSP

* **I/Q beat kept by default** (`B210FmcwRadar(beat="complex")`; `"real"` reproduces the kit's IF
  and the 10 Nov runs). The real part cannot give the sign of the speed in range cell 0, which is
  where a nearby person is with the B210's coarse cells. `pipeline.range_profiles` takes the
  positive-frequency half of a complex range FFT, so everything after the range FFT is unchanged.
* **Detector settings for the B210** (`b210_fmcw_processor_config`): Part 1's CA-CFAR with the gates
  scaled to the range cell. Detections start at the **blind range = 1.5 range cells**
  (`blind_range_m`): 75 m at 4 MS/s / 3 MHz, 37 m at 8 MS/s / 6 MHz (the kit: 2 m). Checked on the
  10 Nov recordings (140 frames, nobody beyond the blind range): 0 false alarms.
* `CfarConfig.range_mode="soca"` (smallest-of along range) added as an option. It sees closer in on
  simulated data but gave 110 false alarms on the same 140 real frames, so it is not the default.
* `SimulatedB210Fmcw` / `SimWalker`: synthetic raw I/Q (leakage, TX→RX latency, walkers that turn
  around, noise) through the real latency estimate and dechirp. Its leakage is perfectly stable, so
  it is kinder than the hardware close in.
* `phalanx/tx_budget.py`: the attenuation budget and `gain_limit()` (reads the cap from the newest
  completed ramp calibration) moved into the package so the scripts and the GUI share one copy.
* `fmcw_check.py`: uses the shared detector settings; `--min-range`, `--min-speed`, `--real-beat`.

### Operator display (`phalanx/gui/app.py`)

* Two new sources: **B210 FMCW radar (TX+RX)** and **B210 FMCW simulator**. Rate 4 / 8 / 12 / 16
  MS/s, sweep up to 80 % of the rate, ramp time; only the rows that belong to the selected source
  are shown. Min range follows the blind range; new **Min speed** control (all sources).
* **Transmit safety in the GUI:** nothing is transmitted unless "ARM transmitter" is ticked before
  Start, and it unticks itself whenever a run ends. The TX gain box cannot be set above the limit
  shown beside it: 44.8 dB, or the cap of the latest completed ramp calibration when that box is
  ticked (valid only if the cans have not moved). All of `UsrpRadar`'s guards still apply underneath.
* The status area shows TX ON / OFF, the measured latency, the leakage level and the blind range.
* B210 FMCW recordings replay without touching the kit's ramp-parity setting (the worker detects
  them from the frame metadata).
* (For Thomas: the GUI is now a second way to arm the transmitter, alongside the scripts.)

## 2026-11-10 — Higher sample rates: the laptop does not keep up (yet)

`fmcw_check.py` at 16 MS/s: the receive-only frame was clean, the first TX+RX frame ended in
`ERROR_CODE_OVERFLOW` (driver aborted TX and failed the frame, as designed). At 30.72 MS/s even
the receive-only frame overflowed after 54 747 of 6.29 M samples. So the host/USB path is the
limit above 4 MS/s on Brandon's laptop under Windows; whether that is the USB link or the Python
loops is to be settled with UHD's own `benchmark_rate`. This is RF-01's open "USB 3.0 throughput"
question, and it applies to the 30.72 MS/s pulsed-LFM preset just the same. `fmcw_check.py` now
reports a failed receive-only frame cleanly instead of a traceback.

Settled the same day with UHD's own `benchmark_rate` (C++, no Python; 10 s each, sc16,
`num_recv_frames=512`): 16 MS/s TX+RX → 19 overruns, 1.6 % of RX samples dropped, TX clean;
30.72 MS/s RX only → 945 overruns, 77 % dropped; 30.72 MS/s TX+RX → 941 overruns and 12 701
underruns. So the limit is the laptop's USB path under Windows, not the PHALANX code. Our own
FMCW run at 8 MS/s / 6 MHz was clean (20 frames, latency 83 samples = 10.4 µs, leakage −26.2
dBFS). Usable ceiling on this host: above 8 and below 16 MS/s with sc16 samples.

## 2026-11-10 — FMCW walking test at 70 dB: no detections, and why

100 frames at TX gain 70 dB (calibrated cap), latency 62 samples throughout, leakage about −24
dBFS, no errors — and no detections while a teammate walked toward and away from the cans.
Reading the recording (`b210_fmcw_20261110T162403Z.npz`): with a 3 MHz sweep the range cells are
50 m, so a person a few metres away is in cell 0 with the leakage. The Part 1 detector drops cell
0 on purpose (min-range gate) and CA-CFAR cannot work in cell 1 beside it. Cell 0 itself does
respond — its energy varies by ~30 dB over the run, mostly at |v| ≤ 0.6 m/s — but the real-valued
beat at zero range cannot give the sign of the speed, and no clean line at walking speed is
visible. So this bandwidth cannot reproduce Part 1's result; the sweep has to be wider, which
needs a higher sample rate.

To make higher rates possible, `usrp.py` now passes samples to UHD in ~1 ms chunks of whole
packets (`chunk_time_s`) instead of one packet per call; at 4 MS/s a chunk is still one packet, so
the calibrated runs are unchanged. (Driver change after the review closed: Thomas to look.)

## 2026-11-10 — FMCW mode: first run on hardware

`fmcw_check.py --arm` at TX gain 40 dB, cans in the calibrated arrangement: 20 frames, no RX/TX
errors, TX→RX latency 62 samples (15.5 µs at 4 MS/s) on every frame, leakage −51.1 to −51.2 dBFS
(the tone read −50.3 dBFS at the same gain). Three single-frame detections at 1.1–1.7 km with
10–11 dB SNR: CFAR false alarms at the expected rate (Pfa 1e-4 × ~1000 cells × 20 frames ≈ 2), so
`fmcw_check.py` now gates detections to `--max-range` (default 300 m). Recording
`b210_fmcw_20261110T155430Z.npz`. No moving target yet.

## 2026-11-10 — TX gain calibrated on hardware: coupling 30 dB, cap 70 dB

* Loopback reference through two Mini-Circuits VAT-12 attenuators (24 dB; the script's loopback
  minimum is now 20 dB because that run never exceeds 44 dB TX gain — noted in the review doc for
  Thomas): 0 → 44 dB, −84.1 → −40.3 dBFS, no errors.
* Cans against that reference: coupling loss 29.8 dB (min) / 30.0 dB (median), spread 0.3 dB over
  23 gains; an earlier run gave 29.7 / 29.9. Credit 26.8 dB → budget cap 70 dB (the driver ceiling).
* `--extend`: 44 → 70 dB in 2 dB steps, no errors, received tone −20.2 dBFS at 70 dB (RX gain 40),
  worst-case bound at RX2 −26.6 dBm. Operating cap 70 dB for this can arrangement. Bus-powered
  (no 6 V supply; USB 3.0, no GPSDO).
* `fmcw_check.py --calibration latest` reads the cap from the newest completed cans log; without
  it the limit stays 44.8 dB.

## 2026-11-10 — B210 FMCW mode: Part 1's chain on B210 data

Decision (Brandon): bring Part 2 up with FMCW first, as in Part 1, then pulsed LFM.

* `phalanx/hardware/b210_fmcw.py` (new): `B210FmcwRadar` wraps `UsrpRadar`, transmits a triangle
  chirp (100 % duty) and returns kit-style frames (real, 20 kS/s, 4096 samples, `waveform="fmcw"`):
  latency from the leakage (`estimate_latency`, measured on the first transmitting frame and
  held — the FMCW mode's zero-range calibration), `dechirp` = delayed reference × conj(received),
  FIR low-pass then decimate (`resample_poly`), real part. Mixing order chosen so the beat phase is
  +2π·fc·τ, the kit's convention: `doppler_sign` stays −1. `b210_fmcw_params()` mirrors
  `cantenna_fmcw()` (default 4 ms ramps, 3 MHz sweep centred on 2.45 GHz).
* `phalanx/hardware/usrp.py`: new transmit waveform kind `"fmcw"` in `build_cpi_waveform`
  (phase-continuous triangle or sawtooth); `tx_waveform="fmcw"` accepted and must match the
  parameters' waveform; `meta["signal_mode"]` now reports the parameters' waveform. No change to
  any transmit guard. (A change to the reviewed driver: Thomas to look at these ~25 lines.)
* `Part2_B210_Cantenna/scripts/fmcw_check.py` (new): receive-only without `--arm`; with it, N
  frames through `RangeDopplerProcessor` exactly as Part 1 configures it, printing latency,
  leakage level and detections; saves one recording that `RDK_Cantenna_DSP.py --replay` and the
  GUI's "Replay recording" open; TX gain refused above the no-credit limit (44.8 dB).
* Tests 90 → 96 (`test_b210_fmcw.py` ×5, one driver test). Simulated data only so far.

## 2026-11-10 — First TX attempt on hardware: send timeout

First armed run of `tx_ramp_calibration.py --mode cans` on the B210 (Brandon's laptop) stopped at
TX gain 0 dB with `tx short send 0/2040`. Cause: `tx.send()` was called with UHD's default 0.1 s
timeout, but the burst is scheduled `lead_time_s` (0.2 s) ahead and the device does not consume
samples before then, so once the 64 requested USB send frames were full the next send timed out.
The driver behaved as designed (stopped sending, requested attenuation, failed the capture; no
step above 0 dB was attempted). Fix: every send now gets `lead_time_s + 1.0` s
(`UsrpRadar._send_timeout_s()`). Test added (79 + 10 + 1 = 90).

Re-run after the fix: ramp completed 0 → 44 dB (the no-credit limit), 23 steps, no RX or TX errors
(no underflow at 4 MS/s). Received tone −90.1 → −46.2 dBFS at RX gain 40 dB: 43.9 dB for 44 dB of
gain, every step within 0.6 dB of its 2 dB — the gain-step assumption held over this range. The
received peak (0.003–0.023 of full scale) did not track the tone: it is ambient 2.4 GHz traffic.
Log `txramp_cans_20261110T151343Z.json`. Going above 44 dB needs the loopback reference (fixed
attenuator ≥ 30 dB) and a teammate's acceptance of the budget.

## 2026-11-10 — Slow TX ramp calibration under an attenuation budget

Thomas's sixth pass closed the software review of `usrp.py` (no further blocking issue in the
tested paths); authorizing TX now rests on the physical RF setup. With no analyser on hand:

* `Part2_B210_Cantenna/scripts/tx_ramp_calibration.py` (new): tone, TX gain from 0 dB in 2 dB
  steps. Budget: power at an RX connector ≤ 20 − (89.8 − g) − attenuation − coupling credit, kept
  ≤ −25 dBm → 44.8 dB with no credit. `--mode loopback --attenuator-db 30` records a reference;
  `--mode cans --reference latest` measures can-to-can coupling against it; `--extend` continues
  to the resulting cap. Stops on UHD errors, half-scale peak, a step that does not change the
  level by its own size (±1.5 dB), or the budget. Will not arm without `--accepted-by <name>`.
  The operating cap is only ever a gain the ramp passed through cleanly.
* `isolation_sweep.py`: defaults now 40 → 44 dB in 2 dB steps and it refuses a `--stop` above the
  no-credit limit (it used to default to 60 dB).
* `Part2_B210_Cantenna/tests/test_tx_ramp_calibration.py` (new, simulated radar): budget
  arithmetic, tone level, arming, each stop, coupling from a reference, `--extend`. Tests 79 → 89.
* Review doc: sixth pass recorded; "Attenuation budget and slow ramp" section; a sign-off row for
  whoever accepts the budget's two assumptions.

## 2026-11-10 — TX worker cannot fail silently (Thomas's fifth pass)

`phalanx/hardware/usrp.py`: the thread target is now `_tx_worker()`, which wraps the whole of
`_send_cpi()` including its setup (`TXMetadata`, `get_max_num_samps()`); any exception records an
error, requests attenuation and attempts end-of-burst via `_close_burst()`, which tolerates missing
metadata. The worker sets `_tx_done` only after a clean, complete burst and `capture()` fails the
CPI without it. Before this, a setup exception ended the worker with empty `tx_errors` and a frame
was returned. Tests 75 → 79 (three setup-failure cases, one clean-burst case).

## 2026-11-05 — B210 USB receive buffering (Brandon Cheng, with Claude)

First B210 runs on Brandon's laptop (UHD 4.11.0.0, Python 3.13.9, serial 326C061, USB 3, FW 8.0 /
FPGA 16.0 — same as the team laptop). First GUI recording at 1 MS/s: 324 blocks / 32.4 s, PASS.
Second run ended with `ERROR_CODE_OVERFLOW` after ~95k samples of a block.

* `phalanx/hardware/b210.py`: `B210ReceiveRadar` now passes `num_recv_frames` in the UHD device
  arguments (`DEFAULT_NUM_RECV_FRAMES = 512`). UHD's B200 default of 16 × 8192-byte USB transfers
  gives only ~32 ms of host-side slack at 1 MS/s, so any host stall longer than that is reported as
  an overflow and (by design) ends the run. 512 frames ≈ 1 s of slack at 1 MS/s. The device args
  used are recorded in `info["device_args"]` so they appear in session metadata.
* `Part2_B210_Cantenna/scripts/readback_sessions.py` (new): re-verifies the newest N sessions and
  compares the device read-back stored in their first block; three 71 s sessions on Brandon's
  laptop → "READ-BACK IDENTICAL" (RF-01 three-restart evidence).
* `phalanx/hardware/usrp.py` (TX+RX driver, still not run on hardware) reviewed and fixed before
  its first run: device args now `type=b200[,serial][,num_recv_frames=512,num_send_frames=512]`
  instead of `""` (any device, default buffering); analog TX/RX bandwidth set to 1.2 × chirp
  bandwidth (was left at the 56 MHz maximum); TX async messages (underflow, late packets) drained
  after the burst has played instead of right after `send()` returned, which missed them;
  `capture(transmit=False)` receives with the transmitter silent (noise floor and the safe first
  step); `set_tx_gain()` for stepping gain between CPIs under the same cap; `first_contact_params()`
  = 4 MS/s / 3 MHz so the first transmit tests timing and leakage with a 4 MB CPI instead of 31 MB;
  `estimate_zero_range_offset()` finds the direct-path leakage bin — the B210 has no analog mixer
  to calibrate, so this measurement *is* the "calibration" card and its result is the pipeline's
  `ramp_offset`. Read-back keys match `B210ReceiveRadar` so `readback_sessions.py` works on both.
  `python -m phalanx.hardware.usrp` now does TX-off, then TX-on at 40 dB, then prints the offset.
* `Part2_B210_Cantenna/scripts/isolation_sweep.py` (new): noise floor, then TX gain 40 → 60 dB
  in 5 dB steps, stopping at half of ADC full scale or on any UHD error; JSON log in
  `PHALANX_data/captures`. Output = highest safe TX gain for the can arrangement + zero-range offset.
* Damage review of the transmit code: `Part2_B210_Cantenna/docs/usrp_damage_review_2026-11-05.md`
  (mechanisms, where each is enforced, residual risks, pre-transmit checklist, teammate sign-off
  table). Guards added from it: `transmit=False` is now the default (RF only when asked);
  `ABSOLUTE_TX_GAIN_CEILING_DB = 70` that needs `unlock_full_power=True` to exceed, honoured by
  the first-contact command and the sweep; `tx_amplitude` validated in (0, 1]; `tx_waveform="tone"`
  (CW at fc + 100 kHz) as the bring-up waveform — the default for first contact and the sweep;
  `plfm` gives the zero-range bin as well.
* Thomas Klopman's safety review of the above (same day; recorded in the review doc): CLI now
  receive-only unless `--tx`, sweep transmits only with `--arm`; gains, the device's TX-gain
  read-back and the waveform samples are validated immediately before every burst; TX cleanup on
  errors (burst closed on abort/exception, RX failure aborts TX, `close()` sets TX gain to 0 dB);
  and the −15 dBm margin is to be established with a spectrum analyser / power meter on the RX
  can (RX2 on a 50 Ω load) before the RX port is connected during transmit — the sweep's
  half-scale stop is a last line of defence, not the measurement.
* Thomas's second pass (same day): `stop()` now aborts, attenuates (TX gain 0 dB) and joins the
  TX thread; `close()` calls it before releasing handles; RX timeout / zero-length receive abort
  TX immediately; all allocation and validation precede TX start inside one protected block;
  gains validated (finite, cap, ceiling, device range) in `__init__`, before `configure()`'s first
  write, in `set_tx_gain()` (store only) and before every burst — the TX gain reaches the hardware
  only inside `capture(transmit=True)`; queued-RF residual stated and bounded
  (`BRINGUP_NUM_SEND_FRAMES = 64`); power-from-gain comments removed from code.
* Thomas's third pass (same day): a TX worker that will not exit is kept, the driver is marked
  faulted (all further captures refused) and `close()` refuses to release the UHD handles under
  it; any TX error (short send, exception, abort, overrun, device-reported underflow/late) now
  fails the CPI with `RuntimeError` after attenuating — no frame is returned; the "33 ms" residual
  is restated as an unverified requested parameter, to be measured (review doc, step 4).
* Thomas's fourth pass (same day): the TX worker now stops itself on the first short send or
  exception (abort flag, TX gain 0 dB requested from the worker, burst closed, no further
  chunks); `stop()` docstring corrected (best effort, attenuation ≠ RF off, residual unmeasured);
  `close()` clears the fault latch once the worker is verified dead, which is the documented
  recovery path (then open() + configure() again).
* `Part2_B210_Cantenna/tests/test_usrp_driver.py` (new, mocked UHD): device args, gain cap and
  ceiling, bandwidth, TX-off default, tone envelope, timed burst + drain ordering, zero-range
  estimate, RX-failure abort + close cleanup, read-back over cap refused, bad samples refused,
  gain zero until armed, validation before any write, RX timeout aborts TX, short send stops the
  worker at once, stuck worker kept / captures blocked / close refuses / recovery after exit.
  Tests 57 → 75.

## 2026-10-27 — Part 1 / Part 2 folders, and fixes after the first B210 hardware run

Brandon Cheng (with Claude), reviewing Thomas Klopman's 22 Oct B210 commit (d049673) and his
`PHALANX_B210_Engineering_Record` v1.0. Baseline before this change: git tag `phase1-part1-complete`.

### Reorganization (files moved with `git mv`, so `git log --follow` keeps their history)

| old path | new path |
|---|---|
| `README_Phase1.md` | `README.md` (rewritten as the Phase 1 map) |
| `scripts/calibrate.py`, `live_rd.py`, `record_dataset.py`, `score_dataset.py` | `Part1_Pasternack_Kit/scripts/` |
| `scripts/verify_session.py` | `Part2_B210_Cantenna/scripts/verify_session.py` (checks moved into `phalanx.session.verify_session`) |
| `scripts/__init__.py` | removed (scripts are run as files, not imported as a package) |
| `docs/sim_range_doppler.png` | `Part1_Pasternack_Kit/docs/` |
| `docs/rf01_b210_cantenna_interface.md`, `docs/b210_gui_update.md` | `Part2_B210_Cantenna/docs/` |
| `tests/test_pipeline.py` | split: `Part1_Pasternack_Kit/tests/test_kit_fmcw.py` (kit FMCW), `Part2_B210_Cantenna/tests/test_plfm_chain.py` (pulsed LFM), `tests/test_dsp_common.py` (CFAR) |
| `tests/test_b210_gui_streaming.py` | `Part2_B210_Cantenna/tests/` |
| — | new: `Part1_Pasternack_Kit/README.md`, `Part2_B210_Cantenna/README.md`, this file |
| — | new: `Part2_B210_Cantenna/docs/PHALANX_B210_Engineering_Record_v1.0_2026-10-22.docx` |

The `phalanx/` package was **not** split: both parts (and Phase 2) run the same DSP and GUI, and
two copies would drift apart. Script commands changed accordingly, e.g.
`python scripts\live_rd.py` → `python Part1_Pasternack_Kit\scripts\live_rd.py`, and
`python -m scripts.verify_session X` → `python Part2_B210_Cantenna\scripts\verify_session.py X`.
`pytest` now collects `tests/`, `Part1_Pasternack_Kit/tests/` and `Part2_B210_Cantenna/tests/`.

### Fixes

1. **GUI simulator is paced like the kit.** The simulator had no delay between frames, so
   ticking Record in Simulator mode filled the 16-block recorder queue at once and the session
   failed. `SimConfig.frame_period_s` (and `PLFMSimConfig`) added; the GUI uses
   `KIT_FRAME_PERIOD_S = 0.8` s (the kit's measured 801 ms per frame). Tests and scoring stay
   unpaced (default 0).
2. **B210 rate above the verified recording rate is flagged.** `TESTED_RECORD_RATE_SPS = 1e6` in
   `phalanx/hardware/b210.py`; the GUI's rate box tooltip says so and Start logs a warning when
   recording above it. (The recorder already marks a session invalid if the disk falls behind.)
3. **Recordings moved out of the repo and OneDrive.** New `phalanx/paths.py`: GUI sessions and
   `calibrate.py` captures go to `PHALANX_data\captures` in the user's home folder (override with
   the `PHALANX_DATA` environment variable). Reason: 1 MS/s sessions are ≈480 MB/min as ~10 files/s,
   and OneDrive sync can lock a file mid-write. The small DSP-01 dataset stays in
   `Part1_Pasternack_Kit/dataset/` so its frozen settings and results can be committed.
4. **`b310_plfm()` removed** from `phalanx/params.py` (it had been restored for compatibility, but
   the team uses the B210). The test now checks it stays gone.
5. **`.gitattributes` added at the repo root** (`* text=auto`). Ten files were showing as modified
   on one laptop with only line-ending differences.

Also:

* **Replay keeps detector settings editable** (regression from d049673 restored). Ramp time and
  bandwidth stay locked in Replay because the recording fixes them; MTI, Pfa, ramp parity and
  min/max range are editable again so old recordings can be re-tested.
* **`calibrate.py` imports restored.** The committed file was missing its `phalanx` imports
  (missing since the 16 Oct commits) and failed with `NameError: CantennaRDK` before reaching the kit.
* `rf01_b210_cantenna_interface.md` and `b210_gui_update.md`: paths updated; RF-01 notes that
  receive has run on hardware and UHD works under Python 3.13; transmit items remain open.
* Package version 0.2.0 → 0.3.0. Tests: 54 → 57 (new: replay settings editable, simulator
  pacing, recordings default outside the repo).

### Kept from the 22 Oct B210 commit (d049673)

Display decoupled from acquisition (`LatestPreview`, 10 Hz timer), controls locked while running,
window close waits for a clean stop, frame/save counters, session recorder with bounded queue and
verifier, B210 receive-only monitor (run on hardware 22 Oct: 228 blocks / 22.8 s at 1 MS/s, PASS).

## 2026-10-22 — B210 receive monitor (Thomas Klopman, d049673)

Continuous receive-only B210 source, session recording, display decoupling. See
`Part2_B210_Cantenna/docs/b210_gui_update.md` and the engineering record in that folder.

## 2026-10-22 — B310 → B210 (Brandon Cheng, 8cb16c0)

The team's SDR is the USRP B210, not the B310: presets, driver safety limits and RF-01 document
renamed and corrected.

## 2026-10-08 … 10-16 — Part 1 built and calibrated on kit s/n 5035

Driver, DSP chain, GUI (scope / range–Doppler / raw tabs), recording + replay, dataset scoring.
