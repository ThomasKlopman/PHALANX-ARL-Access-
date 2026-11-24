# Phase 1 Part 2 — USRP B210 + the kit's cans

**Status (5 Nov 2026): receive verified on two laptops (60 s runs, three restarts); transmit not yet run.**

Part 2 replaces the Pasternack radar board with an Ettus USRP B210 software-defined radio
connected to the kit's two can antennas. The B210 hands the laptop complex I/Q, and range comes
from a digital matched filter instead of the kit's analog mixer — the same chain Phase 2's CN0566
will use, rehearsed on hardware we already have.

> **Safety: never put more than −15 dBm into a B210 RX port** (UHD manual). Two cans side by
> side can couple that much at full transmit power. Read `docs/rf01_b210_cantenna_interface.md`
> section 2 before transmitting. The transmit driver starts at TX gain 40 dB and refuses more than
> 60 dB unless the cap is raised deliberately.

```
Part2_B210_Cantenna/
├── README.md                    this file
├── scripts/verify_session.py    check a recorded session is complete (acceptance evidence)
├── scripts/readback_sessions.py compare the B210's reported settings across sessions (three-restart evidence)
├── scripts/fmcw_check.py        B210 in FMCW mode through the unchanged Part 1 chain; saves a recording the Part 1 tools replay
├── scripts/stage_dump.py        one frame through the B210 FMCW chain stage by stage, expected vs obtained; --out writes the figure in docs/
├── scripts/tx_ramp_calibration.py  slow TX ramp from 0 dB under an attenuation budget; measures can-to-can coupling → TX gain cap
├── scripts/isolation_sweep.py   short sweep inside the no-credit limit (40 → 44 dB); with --waveform plfm gives the zero-range offset
├── docs/
│   ├── rf01_b210_cantenna_interface.md               RF-01 interface requirements: ports, −15 dBm limit, rates, config
│   ├── b210_gui_update.md                             design notes for the RX monitor, recorder and display decoupling
│   ├── usrp_damage_review_2026-11-05.md               transmit-code damage review + pre-transmit checklist + sign-off
│   └── PHALANX_B210_Engineering_Record_v1.0_2026-10-22.docx   first hardware run: setup, results, fixes, environment
└── tests/
    ├── test_b210_gui_streaming.py   continuous receive, recorder, sessions, display decoupling (mocked UHD)
    ├── test_usrp_driver.py          TX+RX driver: device args, gain cap, bandwidth, burst timing, zero-range (mocked UHD)
    ├── test_b210_fmcw.py            FMCW mode: triangle waveform, latency estimate, Part 1 chain on simulated B210 data
    ├── test_tx_ramp_calibration.py  ramp calibration: budget, stops, coupling, --extend (simulated radar)
    ├── test_b210_fmcw_gui.py        GUI: B210 FMCW sources, arming, TX gain limit, replay (headless Qt)
    └── test_plfm_chain.py           pulsed-LFM matched filter, signed Doppler, leakage removal (simulated echoes)
```

B210 code inside the shared package: `phalanx/hardware/b210.py` (`B210ReceiveRadar`, receive-only
monitor), `phalanx/hardware/usrp.py` (`UsrpRadar`, pulsed-LFM transmit + receive), presets
`b210_rx_params()` and `b210_plfm()`, and the recorder in `phalanx/session.py`.

## What has been done

| item | status | evidence |
|---|---|---|
| UHD Python bindings on the team laptop | ✅ UHD 4.11.0.0, Python 3.13.14 | engineering record |
| UHD Python bindings on a second laptop (Brandon) | ✅ UHD 4.11.0.0 (VS2022 installer, all components), Python 3.13.9, `pip install uhd==4.11.0.0` | 5 Nov 2026 |
| B210 receive-only monitor in the GUI | ✅ ran on hardware (serial 326C061, hw rev 4) | engineering record |
| Continuous capture with no overflow | ✅ 228 blocks / 22.8 s at 1 MS/s, stop in 230 ms | `verify_session.py` PASS |
| **60 s receive run, three restarts, identical read-back** | ✅ 710 / 709 / 713 blocks (71.0 / 70.9 / 71.3 s) at 1 MS/s, 5 Nov 2026, Brandon's laptop | `readback_sessions.py`: READ-BACK IDENTICAL across 3 complete sessions |
| **TX gain calibration (ramp + measured coupling)** | ✅ 10 Nov 2026, Brandon's laptop, bus-powered. Loopback through 2 × VAT-12 (24 dB): 0 → 44 dB, −84.1 → −40.3 dBFS. Cans: can-to-can coupling loss 29.8 dB min / 30.0 dB median, 0.3 dB spread over 23 gains (two runs agree within 0.1 dB). Budget cap 70 dB (the ceiling; worst-case bound −26.6 dBm at RX2); ramp extended 44 → 70 dB cleanly, −20.2 dBFS at 70 dB, every step 2 dB ± 0.1. **Operating cap 70 dB for this can arrangement only.** Budget acceptance by a teammate still owed (runs logged with Brandon's own name). | `txramp_loopback_20261110T154305Z.json`, `txramp_cans_20261110T154855Z.json` in `PHALANX_data\captures` |
| USB overflow root cause | ✅ UHD's B200 default of 16 × 8 KB USB transfers ≈ 32 ms of slack at 1 MS/s; driver now passes `num_recv_frames=512` (≈1 s) | one overflow seen at default, none after (CHANGELOG 2026-11-05) |
| Session recording and verifier | ✅ every block saved; device-time continuity checked | tests + hardware run |
| Pulsed-LFM matched-filter chain | ✅ on simulated echoes only | `tests/test_plfm_chain.py` |
| Pulsed-LFM transmit + receive (`usrp.py`) | ☐ software review closed (six passes, T. Klopman); chirps not yet run on hardware | `tests/test_usrp_driver.py`, `docs/usrp_damage_review_2026-11-05.md` |
| First transmit: tone ramp through the cans, TX gain 0 → 44 dB in 2 dB steps | ✅ 10 Nov 2026, Brandon's laptop: 23 steps, no RX/TX errors, received tone −90.1 → −46.2 dBFS (43.9 dB for 44 dB of gain; every step within 0.6 dB of 2 dB), RX gain 40 dB. Cap 44 dB = the no-coupling-credit budget limit. Budget acceptance by a teammate still owed (run logged with Brandon's own name). | `PHALANX_data\captures\txramp_cans_20261110T151343Z.json` |

## What remains (RF-01 acceptance)

1. ~~60 s receive run, then three restarts with identical read-back~~ — done 5 Nov 2026 (table above).
2. **Higher rates.** Recording is verified at 1 MS/s only (`TESTED_RECORD_RATE_SPS` in `b210.py`).
   The GUI allows up to 30.72 MS/s but logs a warning above the verified rate; a session whose
   writer falls behind is marked invalid, never silently gappy. Raise the constant only after a
   longer run at the new rate passes `verify_session.py`.
3. **Can-to-can isolation** measured before raising TX gain (`rf01` doc, section 2).
4. **First transmit** — see "First transmit" below; then add a "B210 radar (TX+RX)" source to the
   GUI *next to* the RX monitor (don't replace it).
5. Qt window-size warnings seen on the team laptop (engineering record, open limitations).

## Run it

From `RDK_Cantenna\` with the venv active and the B210 on a USB 3.0 port:

```
uhd_find_devices                                   # should list serial 326C061
python RDK_Cantenna_GUI.py                         # source "B210 RX monitor", 1 MSPS, 2.45 GHz, gain 10
                                                   # tick Record, Start, ~60 s, Stop
python Part2_B210_Cantenna\scripts\verify_session.py "C:\Users\<you>\PHALANX_data\captures\gui_<number>.session"
```

Recordings go to `PHALANX_data\captures` in your home folder, not into the repo or OneDrive:
at 1 MS/s a session is ≈480 MB per minute written as ~10 files per second, which OneDrive sync
can corrupt. Set `PHALANX_DATA` to use another local disk.

## FMCW first, pulsed LFM after (decision 10 Nov 2026)

Part 2 is brought up in two waveform steps. **Step A — FMCW, like Part 1:** the B210 transmits the
kit's kind of triangle chirp and `phalanx/hardware/b210_fmcw.py` does in software what the kit's
mixer and ADC do in hardware (measure the TX→RX latency from the leakage, dechirp, low-pass,
decimate to 20 kS/s, take the real part). The frames have exactly the kit's shape, so the Part 1
chain and tools run on them unchanged — that is the direct comparison PBI-4 asks for.
**Step B — pulsed LFM with the matched filter** (`usrp.py` + `pulse_compress`), the Phase-2 chain.

```
python Part2_B210_Cantenna\scripts\fmcw_check.py                    # receive-only
python Part2_B210_Cantenna\scripts\fmcw_check.py --arm              # 20 frames, TX gain 40 dB
python RDK_Cantenna_DSP.py --replay "<saved .npz>" --offset 0 --parity 0
python Part2_B210_Cantenna\scripts\stage_dump.py                    # simulated frame: 16 checks, no hardware
python Part2_B210_Cantenna\scripts\stage_dump.py --recording "<saved .npz>" --frame 5
```

Limits versus the kit: sweep bandwidth is bounded by the sample rate (3 MHz at 4 MS/s → 50 m range
cells; the kit's 100 MHz gives 1.5 m) and TX power by the gain cap. Velocity resolution and span
match the kit's for the same ramp time. Status: simulated data only (target at the right range
cell, +1.5 m/s closing reads +1.5); **not yet run on hardware.**

## Operator display for the B210 (draft, 12 Nov 2026 — not yet run on hardware)

```
python RDK_Cantenna_GUI.py
```

* Source **B210 FMCW simulator**: no hardware, nothing transmitted; a simulated walker goes back
  and forth so the display, recording and replay can be tried.
* Source **B210 FMCW radar (TX+RX)**: receive-only unless **ARM transmitter** is ticked before
  Start (it unticks itself after every run). TX gain cannot exceed the limit shown; tick "Use the
  latest ramp calibration" to raise it from 44.8 dB to the calibrated cap — only if the cans have
  not moved since that calibration.
* Detections are reported from the **blind range** outward (1.5 range cells: 37 m at 8 MS/s /
  6 MHz, 75 m at 4 MS/s / 3 MHz). Inside it the direct can-to-can leakage hides everything.

## First transmit

Prerequisites: the measurement procedure, checklist and teammate sign-off in
`docs/usrp_damage_review_2026-11-05.md` (the −15 dBm margin is established with a spectrum analyser
or power meter on the RX can, RX2 on a 50 Ω load, before the RX port sees any transmission); the 6 V supply
connected; TX can on **RF A TX/RX** (never leave that port open), RX can on **RF A RX2**; the
cans separated and angled apart; the regulatory question in the RF-01 doc answered by the adviser.

```
python -m phalanx.hardware.usrp                      # receive-only CPI (nothing transmits without --tx)
python -m phalanx.hardware.usrp --tx                 # tone, 4 MS/s: TX-off CPI, then TX-on at 40 dB
python -m phalanx.hardware.usrp --tx --waveform plfm # chirps; also prints the zero-range offset
# calibration: slow ramp from 0 dB in 2 dB steps (docstring of the script explains the budget)
python Part2_B210_Cantenna\scripts\tx_ramp_calibration.py --mode loopback --attenuator-db 24 --arm --accepted-by "<teammate>"   # two 12 dB pads in series
python Part2_B210_Cantenna\scripts\tx_ramp_calibration.py --mode cans --reference latest --arm --accepted-by "<teammate>"
python Part2_B210_Cantenna\scripts\tx_ramp_calibration.py --mode cans --reference latest --extend --arm --accepted-by "<teammate>"
python Part2_B210_Cantenna\scripts\isolation_sweep.py --arm --waveform plfm  # chirps, 40 → 44 dB: the zero-range bin
benchmark_rate --rx_rate 30.72e6 --tx_rate 30.72e6   # only then: can this laptop do the full preset?
```

The first contact deliberately uses `first_contact_params()` (4 MS/s, 3 MHz, 50 m resolution): one
CPI is 4 MB instead of 31 MB and the USB load is 1/8 of the final preset, so timing, leakage level
and gain safety are tested one unknown at a time. "Calibration" for the B210 means the sweep's two
outputs: the highest TX gain that keeps the receiver below half scale for this can arrangement,
and the leakage bin (`zero_range_offset`), which becomes the pipeline's `ramp_offset`. Repeat
`readback_sessions.py`-style three-restart evidence in TX+RX mode once it runs.

## Configuration for the transmit radar (`b210_plfm()`)

2.45 GHz, 25 MHz chirp (6 m range resolution), 30.72 MS/s, 0.5 ms pulses at 1 kHz, 128 pulses
per CPI (0.48 m/s velocity resolution, ±30 m/s). TX on RF A TX/RX, RX on RF A RX2, the included
6 V supply connected. Full rationale and link budget: `docs/rf01_b210_cantenna_interface.md`.
