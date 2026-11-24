# RF-01 — USRP B210 + Cantenna interface requirements

Status: **draft for the RF-01 [Research] card** (20 Oct 2026; replaces the B310 draft of 16 Oct —
the team is using the **B210**). Items marked ☐ are decisions or measurements still owed;
everything else comes from the Ettus B200/B210 spec sheet, the UHD B200/B210 manual, or what we
measured on the Pasternack kit.

Sources: [Ettus B200/B210 spec sheet](https://mm.digikey.com/Volume0/opasdata/d220001/medias/docus/7161/b200_b210_spec_sheet-3236990.pdf),
[UHD manual — USRP B2x0 series](https://uhd.readthedocs.io/en/latest/page_usrp_b200.html) (both accessed 20 Oct 2026).

**Update 27 Oct 2026.** The receive side has now run on hardware: the GUI's *B210 RX monitor*
(receive only, no transmitter) recorded 228 blocks / 22.8 s at 1 MS/s on serial 326C061 with
UHD 4.11.0.0 under Python 3.13.14, and `verify_session.py` passed it (22 Oct; see
`PHALANX_B210_Engineering_Record_v1.0_2026-10-22.docx`). That closes the UHD/Python 3.13 question
in sections 3 and 6. The transmit-and-receive radar (`phalanx/hardware/usrp.py`, `b210_plfm()`)
has still not run on hardware, so every TX-related item below is still open.

**Update 5 Nov 2026.** Receive acceptance closed on a second host (Brandon's laptop, UHD 4.11.0.0,
Python 3.13.9, USB 3, same board/FW 8.0/FPGA 16.0): three consecutive GUI recordings of 71.0, 70.9
and 71.3 s at 1 MS/s, each `verify_session.py` PASS, and `readback_sessions.py` reports identical
device read-back across the three (2.45 GHz, 1 MS/s, gain 10 dB, RX2, internal clock). One run at
UHD's default USB buffering overflowed after ~30 s; `B210ReceiveRadar` now requests
`num_recv_frames=512` (see CHANGELOG). Items 3 and 4 in section 5 are therefore met for receive-only
operation; they must be repeated with `UsrpRadar` once transmit runs.

## 1. What changes when the radar becomes the B210

| | Pasternack RDK (Phase 1, working) | B210 + the two cans |
|---|---|---|
| Transmitter | VCO/PLL sweep, 1 W (+30 dBm) | B210 DAC, > +10 dBm max (TX gain 0–89.8 dB) |
| Waveform | triangle FMCW, 2.4–2.5 GHz, analog mixer dechirp | pulsed LFM at 2.45 GHz, digital matched filter |
| What the host receives | real beat signal, 16-bit, 20 kS/s, ≤ 4096 samples/frame | complex I/Q, 12-bit converters, fc32 on host (sc16 on the wire) |
| Host link | USB 2.0 (USBTMC, NI-VISA) | **USB 3.0** (UHD) |
| Range from | FFT of the beat (`range_fft`) | correlation with the TX chirp (`pulse_compress`) |
| Pulse/sweep alignment | estimated from data (symmetry) | exact: timed TX + RX commands on the device clock |
| DSP after range stage | identical | identical |

The matched-filter path is the Phase-2 (CN0566) chain; building it on the B210 rehearses Phase 2
with hardware already on hand. The B210 was also the SDR named at the CDR, so the Phase-2 numbers
briefed there (30.72 MS/s per channel for two coherent RX channels) apply directly.

## 2. Connections

```
                     SMA            ┌──────────────────┐
  TX can  ◄──── coax ──────────────┤ RF A: TX/RX      │
                                   │                  │
  RX can  ◄──── coax ──────────────┤ RF A: RX2        ├──── USB 3.0 (Type-B) ──► laptop
                                   │                  │
                                   │ RF B: TX/RX, RX2 │  (spare: second RX channel for Phase-2 tests)
                                   │ 10 MHz / PPS in  │  (not needed: single device)
                                   └────────┬─────────┘
                                     6 V 3 A supply (included with the B210)
```

* **Ports.** TX on **RF A: TX/RX**, receive on **RF A: RX2** (receive-only). Only the TX/RX ports
  can transmit. Channel B is free; its RX2 can later take a second can to rehearse the CN0566's
  two-channel receive.
* **Receive damage level: never more than −15 dBm into any RF input** (UHD manual). This is the
  single most important constraint in this document. With +10 dBm into one can and the other can
  a few centimetres away, direct coupling could reach it.
  ☐ Measure can-to-can isolation before raising TX gain: start at TX gain 40 dB (tens of dB below
  full output), RX gain low, read the received peak (`python -m phalanx.hardware.usrp` prints it),
  then step TX gain up in 5 dB increments and stop well short of RX full scale. Separate and angle
  the cans to improve isolation. The driver refuses TX gain above 60 dB unless the cap is raised
  deliberately.
* **Always terminate the TX port** with the can or a 50 Ω load before transmitting (UHD manual).
* **Power.** Use the included 6 V / 3 A supply when transmitting. It is mandatory on USB 2.0 or
  with a GPSDO installed; on USB 3.0 the board can run from the bus, but transmit current makes
  the supply the safer choice.
* **Cables.** ☐ Confirm SMA coax on hand (RG-316/RG-58, ≤ 1 m; ~0.5–1 dB/m at 2.45 GHz).
* **USB.** Must be a USB 3.0 (SuperSpeed, blue) port for the rates below; the spec sheet notes
  full-rate streaming works "on select USB 3.0 chipsets".

## 3. Host software

* UHD with Python bindings (`import uhd`). The B210 is supported from UHD 3.8.4; use the current
  Ettus Windows installer, which includes the USB driver. ☐ Confirm the bindings import under the
  team's Python 3.13 venv; if they do not, the fallback is the matching Python version or a Linux
  host. This is the biggest schedule risk on RF-01.
* Device check: `uhd_find_devices` then `uhd_usrp_probe`.
* Throughput check: `benchmark_rate --rx_rate 30.72e6 --tx_rate 30.72e6` (1 RX + 1 TX); repeat at
  61.44e6 to see whether the wider waveform is possible on this laptop.
* First contact: `python -m phalanx.hardware.usrp` (5 Nov 2026 version) opens the board with
  explicit USB buffering, receives one CPI with the transmitter silent, then one with TX at 40 dB,
  and prints the received peak, RX/TX error lists and the measured zero-range (leakage) offset —
  at 4 MS/s / 3 MHz, not the full preset. `scripts/isolation_sweep.py` then steps TX gain with a
  half-scale safety stop (section 2's ☐ isolation item) and logs the evidence to JSON.
* Drivers: `phalanx/hardware/b210.py` (`B210ReceiveRadar`, receive-only monitor) — **run on
  hardware 22 Oct**; `phalanx/hardware/usrp.py` (`UsrpRadar`, pulsed-LFM transmit + receive) —
  same `RadarSource` interface as the kit, **not yet run on hardware**.

## 4. Proposed operating configuration (`phalanx.params.b210_plfm()`)

| parameter | value | why |
|---|---|---|
| center frequency | 2.45 GHz | cans rated 2.4–2.5 GHz; ISM band |
| sample rate | 30.72 MS/s | 1 TX + 1 RX streaming at once ≈ 2 × 123 MB/s on USB 3.0; also the 2x2 ceiling |
| bandwidth | 25 MHz | fits inside the 30.72 MS/s complex band → 6 m range resolution |
| pulse width | 0.5 ms | energy: TBP 12 500 → 41 dB compression gain |
| PRF | 1 kHz | 50 % duty; ±30 m/s unambiguous |
| pulses per CPI | 128 | 128 ms CPI, 0.48 m/s velocity resolution |
| TX gain | start 40 dB, cap 60 dB | raise only after the isolation measurement |
| RX gain | start 10 dB | keep direct path below full scale |
| ports | TX on RF A TX/RX, RX on RF A RX2 | full duplex on one channel |

**Upgrade path:** if `benchmark_rate` sustains 61.44 MS/s for 1 TX + 1 RX, call
`b210_plfm(fs_hz=61.44e6, bandwidth_hz=50e6)` for 3 m resolution (the 1x1 limit is 56 MHz).

Link budget sanity (walker, RCS 1 m², 30 m, ~10 dBi cans, +10 dBm, NF 8 dB, B 25 MHz): the
radar equation gives roughly +10 dB SNR per sample before processing; 41 dB of compression gain
and 21 dB of coherent integration over 128 pulses add ~60 dB. Noise is not the limit — the
direct TX→RX coupling and the −15 dBm input limit are.

## 5. Acceptance evidence to collect (maps to the RF-01 cards)

1. **[Research]** this document reviewed by the team.
2. **[Design]** connection diagram (section 2) approved + photo of the wiring.
3. **[Integrate] 60 s**: 60 s of consecutive CPIs from `UsrpRadar` with `rx_errors == []` on every
   frame and `rx_clipping` false; evidence = the log.
4. **[Test] three restarts**: power-cycle, run the probe ×3, read-back of rate/freq/gain identical
   each time; evidence = three printouts.
5. **[Audit]**: a teammate reproduces the probe from the README on a clean checkout.

## 6. Open questions

☑ UHD Python bindings under Python 3.13 on the team laptop — works (UHD 4.11.0.0, Python 3.13.14, 22 Oct).
☐ Can-to-can isolation, and the TX/RX gain pair that keeps the RX input safely below −15 dBm and
   the received peak below full scale.
☑ Receive-only 60 s runs and three restarts with identical read-back — done 5 Nov 2026 on a second
   laptop (see update above); the TX+RX version of the same checks remains open.
☒ **USB throughput measured 10 Nov 2026 on Brandon's laptop (Windows, UHD 4.11, `benchmark_rate`,
   sc16): 30.72 MS/s is NOT sustainable** (RX only: 77 % of samples dropped; TX+RX: 941 overruns,
   12 701 underruns). 16 MS/s TX+RX is marginal (19 overruns, 1.6 % dropped). 8 MS/s TX+RX runs
   clean in the PHALANX FMCW mode. Consequence: on this host the sweep is limited to ~6 MHz
   (25 m range cells) unless the rate limit is lifted (sc12 wire format, another USB port or
   machine, or a Linux host). The 30.72 MS/s / 25 MHz preset in section 4 cannot be used here.
☐ (superseded by the line above) USB 3.0 throughput at 1 TX + 1 RX: 30.72 MS/s (needed) and 61.44 MS/s (nice to have).
☐ Does a 2.45 GHz, 25 MHz, ~+10 dBm transmission on campus need a spectrum-management
   coordination note? (Licence-exempt band and lower power than the Pasternack kit, but the B210
   is not a certified transmitter.)
