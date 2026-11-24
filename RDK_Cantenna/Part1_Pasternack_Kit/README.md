# Phase 1 Part 1 — Pasternack Cantenna kit → computer (DSP & GUI)

**Status: complete (Oct 2026).** The Pasternack PEM11002 "Cantenna" radar kit is driven directly
over USB by our own driver, DSP and operator display — no Pasternack software. Verified on kit
s/n 5035. Code frozen at git tag `phase1-part1-complete`; later changes to shared code must keep
this folder's tests passing.

Shared code lives in `../phalanx/` (see `../README.md`). This folder holds only what is specific
to the kit:

```
Part1_Pasternack_Kit/
├── README.md               this file
├── scripts/
│   ├── calibrate.py        first-contact checks with the kit: idn / raw / edge / sign
│   ├── live_rd.py          matplotlib diagnostic display (--sim | --replay FILE | hardware), --record FILE
│   ├── record_dataset.py   labelled present/absent recordings for DSP-01
│   └── score_dataset.py    frozen-settings scoring, 8/10 & ≤2/10 gates, repeatability check
├── docs/                   kit figures (sim_range_doppler.png); export the design document's PDF here at milestones
├── tests/test_kit_fmcw.py  ADC decode, sweep alignment, ramp parity, FMCW range-Doppler, MTI, CW mode
└── dataset/                (created by record_dataset.py) recordings git-ignored; settings + results committed
```

Kit-specific code inside the shared package: `phalanx/hardware/cantenna.py` (pyvisa + SCPI driver)
and `phalanx/hardware/codec.py` (ADC word decode); presets `cantenna_fmcw()` / `cantenna_cw()`.

## Run it

All commands from `RDK_Cantenna\` with the venv active.

```
python -m phalanx.hardware.cantenna                       # probe the kit: IDN, read-back, frame timing
python Part1_Pasternack_Kit\scripts\calibrate.py idn      # also: raw | edge | sign
python RDK_Cantenna_GUI.py                                 # source "Cantenna kit (USB)"
python Part1_Pasternack_Kit\scripts\live_rd.py --sim       # matplotlib diagnostic display
python Part1_Pasternack_Kit\scripts\live_rd.py --replay "C:\Users\<you>\PHALANX_data\captures\FILE.npz"
```

NI-VISA is on the team laptop; the kit enumerates as `USB0::0x2012::0x0017::5035::INSTR`.

## Operator GUI (UI-01)

Sources: Simulator, Replay recording, Cantenna kit (USB), B210 RX monitor (Part 2). The state banner
shows STOPPED / STARTING / LIVE / REPLAY / SIMULATOR / STOPPING / END OF FILE / ERROR; Stop is
acknowledged immediately. Each detection row carries decision, range [m], velocity [m/s]
(+ = closing), SNR [dB] and the source. Tabs: Scope (operator view — arcs, because one receive
channel gives range but no bearing), Range–Doppler, Raw signal.

* The **Simulator** runs at the kit's real rate (one frame per 0.8 s), so what you see and record
  matches what the kit would give.
* In **Replay**, ramp time and bandwidth come from the recording (locked), but the detector
  settings — MTI, Pfa, ramp parity, min/max range — stay editable so an old recording can be
  re-tested with new settings. Settings apply when you press Start.

## Recording and replay (AQ-01)

`calibrate.py` runs, `live_rd.py --record FILE.npz`, `record_dataset.py` and the GUI's **Record**
box write schema-v1 captures (`phalanx/capture.py` documents every key). The GUI writes a session
folder (`session.json` + one `.npz` per frame) to `PHALANX_data\captures`; scripts write single
`.npz` files. Both replay with the GUI's "Replay recording" source (choose the `.npz` or the
`session.json`). `tests/test_capture.py` proves replay is bit-exact and gives identical detections.

## Dataset scoring (DSP-01)

```
python Part1_Pasternack_Kit\scripts\record_dataset.py --label absent  --count 10    # nobody moving
python Part1_Pasternack_Kit\scripts\record_dataset.py --label present --count 10    # one walker, 4–8 m
python Part1_Pasternack_Kit\scripts\score_dataset.py --freeze                       # lock detector settings ONCE
python Part1_Pasternack_Kit\scripts\score_dataset.py --repeats 3                    # table + gates + repeatability
```

The settings file is hashed into the report; recordings made before freezing are flagged.

## What was measured on the kit (s/n 5035, Oct 2026)

| item | result |
|---|---|
| link | USBTMC/SCPI via NI-VISA; all configure commands accepted; `-500 Power on` drained at open |
| ADC words | offset-binary (unsigned); ~2.5× headroom on a static scene |
| frame timing | 4096 samples = 801 ms (205 ms acquisition + 133 × ~4.5 ms USB queries) → ~1.2 fps |
| sweep/capture | capture starts on a sweep edge: alignment offset 79 every frame, symmetry corr 0.98 |
| coherence | pulse matrix rows identical on a static scene → Doppler processing valid |
| parity | `ramp_parity = 1` (walk test, 12/12 frames; closing = positive) |
| spurs | family at (n+½)·PRF (3437.5 Hz ± k·125 Hz) → alias to ±v_max; edge Doppler rows gated |
| dynamic range | strongest clutter 1.5–4.5 m; noise floor ≈ −40 dB beyond 35 m (~48 dB) |

## Parameter trade-off (`ramp_time_ms`)

| T | R_max | ±v_max | pulses / frame | Doppler res |
|---|---|---|---|---|
| 2 ms | 30 m | 7.6 m/s | 51 | 0.30 m/s |
| **4 ms** | **60 m** | **3.8 m/s** | **25** | **0.31 m/s** |
| 8 ms | 120 m | 1.9 m/s | 12 | 0.32 m/s |

## Signal chain

```
real IF 20 kS/s ─► remove_dc ─► align sweep (mirror symmetry) ─► reshape (pulses × samples)
   ─► range_fft ─► MTI ─► Doppler FFT ─► power ─► clutter map ─► CA-CFAR ─► gates ─► detections
```

The kit's IF is real, but the range FFT makes each bin complex, so slow-time phase gives signed
Doppler; only CW mode is sign-blind. The analog mixer's convention gives `doppler_sign = −1`.

## Documentation

The stakeholder design document ("PHALANX Phase 1 Part 1 Design Document — Cantenna Kit (with
Pasternack SDR) to Computer (DSP & GUI)") is kept in the team's XE401 folder in Word; export a PDF
into `docs/` at each milestone so the version that matches the code is in git.
