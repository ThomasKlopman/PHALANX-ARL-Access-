# B210 transmit code — "no damage" review (PBI-1 / PBI-6 cards)

Scope: `phalanx/hardware/usrp.py` (`UsrpRadar`), `scripts/isolation_sweep.py`, and the
receive-only `phalanx/hardware/b210.py`, as of 5 Nov 2026. Reviewer: Claude (code review,
with Brandon Cheng); **teammate sign-off required below before the first transmit** — the
Definition of Done asks for a teammate or technical lead review, and this card is Thomas's.

What can damage the board, and what the code does about each:

| # | Mechanism | Limit (source) | Where it is enforced | Residual risk |
|---|---|---|---|---|
| 1 | RF power into an RX port | never > −15 dBm at the connector (UHD B2x0 manual, "Proper Care and Handling") | TX gain starts at 40 dB, per-instance cap `max_tx_gain_db` (default 60 dB), and an absolute ceiling `ABSOLUTE_TX_GAIN_CEILING_DB = 70` that needs `unlock_full_power=True` to exceed; the first-contact command and the sweep refuse caps above the ceiling. `isolation_sweep.py` stops at half of ADC full scale or on any UHD error. | The ADC cannot measure connector power; half scale at RX gain 10 dB is a conservative proxy, not a measurement. Cans side by side with no separation are the worst case. A power meter on the RX cable is the only direct check. |
| 2 | Transmitting into an open TX port | "always terminate" (UHD manual) | Not enforceable in software. | **Procedure item:** TX can or 50 Ω load on RF A TX/RX before any `transmit=True` run. |
| 3 | RF leaving the board when nobody intended it | — | `UsrpRadar(transmit=False)` is now the default; RF only with `transmit=True` or `capture(transmit=True)`. `B210ReceiveRadar` never creates a TX stream (its test `FakeRadio` has no TX API). The GUI has no transmit source yet. | A future GUI source must keep the default off and expose the cap as a read-only value, not a free spin box. |
| 4 | DAC overdrive / spectral splatter | DAC full scale = 1.0 | `tx_amplitude` validated in (0, 1], default 0.5 (−6 dBFS). | Splatter is a spectrum-management concern, not board damage. |
| 5 | Wrong port assignment | RX2 cannot transmit (hardware) | `tx_antenna="TX/RX"`, `rx_antenna="RX2"` defaults; UHD rejects invalid names. | Full duplex on one channel is the B210's normal mode. |
| 6 | Thermal / power | 6 V 3 A external input (manual) | Not software. | **Procedure item:** external supply connected for every transmit run; bus power is only verified for receive-only. |
| 7 | Overflow/underflow misread as "working" | RF-01: zero reported errors over 60 s | RX errors and TX async errors (underflow, late packet) are collected per CPI; the sweep stops on any. | — |
| 8 | ESD / hot-plugging SMA | general RF practice | Not software. | **Procedure item:** connect/disconnect coax only with the GUI/script stopped. |

**Uncalibrated expectation only** (to be replaced by the measured table in step 1 below):
if the AD9361's attenuator is close to 1 dB per dB of "gain" and the board's maximum is about
+10 dBm, then 70 dB would be around −10 dBm, 60 dB around −20 dBm, 40 dB around −40 dBm at the
connector. None of this is a limit or a safety argument; it is the number the power meter
should roughly agree with, and if it does not, the meter wins.

## Teammate review (Thomas Klopman, 5 Nov 2026) and what changed because of it

Thomas's review (full text kept with the card) found: the command-line program transmitted by
default; the tone transmits for the whole capture; the gain limits reduce output but are not a
calibrated power limit; the clipping warning and RX gain cannot protect the connector; and the
transmit path had no cleanup on errors. He recommended explicit transmit opt-in, validating gains
and waveform samples before transmitting, TX cleanup on errors, and — the key procedural point —
establishing the margin below −15 dBm by measurement, not from antenna separation or digital
sample levels. All accepted:

| Finding | Change |
|---|---|
| CLI transmits by default | `python -m phalanx.hardware.usrp` is receive-only unless `--tx`; `isolation_sweep.py` records the noise floor and exits unless `--arm`. The class default was already `transmit=False`. |
| Validate gains and samples before RF | `_validate_gains()` (TX gain within [0, cap], cap within the ceiling unless unlocked, RX gain within 0–76, **device TX-gain read-back must be under the cap**) and `_validate_waveform()` (finite, ≤ DAC full scale) run immediately before every burst, not only at configure. |
| TX cleanup on errors | `_send_cpi` closes the burst (`end_of_burst` with zero samples) on abort or exception; an RX failure sets the abort flag and joins the TX thread before re-raising; `close()` aborts any burst and sets TX gain to 0 dB (maximum attenuation). |
| Gain limit is not a calibrated power limit | Agreed; it remains a guard, not a measurement. The calibrated limit comes from the procedure below. The "rough TX power scale" above is an expectation to sanity-check the meter against, nothing more. |
| Tone is 100 % duty | By design for bring-up (steady reading on a meter); it is used only inside the measured-safe gain range. |

## Teammate re-review (Thomas Klopman, 5 Nov 2026, second pass) and what changed

Thomas confirmed the first-round changes and found six remaining gaps. All addressed:

| Finding | Change |
|---|---|
| `stop()` did nothing; `close()` did not join the TX thread before releasing handles | `stop()` = `_abort_tx()`: sets the abort flag, **writes TX gain 0 dB first**, joins the TX thread (2 s, logged if it will not die), clears the handle. `close()` calls `stop()` in a `try/finally` before releasing the UHD handles. |
| RX timeouts and zero-length receives did not trigger TX abort immediately | Both now call `_abort_tx()` before leaving the receive loop, then the CPI is reported short. |
| Setup / buffer allocation after TX start, outside the protected block | All allocation (buffer, metadata, stream command) and all validation happen **before** the TX thread exists; the TX start, RX loop and join are inside one `try/except BaseException` that aborts TX and re-raises. |
| Gain validation before `configure()` / `set_tx_gain()` write to hardware; finite checks; device-supported limits | `_validate_gains()` runs in `__init__`, at the top of `configure()` before the first hardware write, in `set_tx_gain()` (which now only stores the value — nothing is written), and before every burst. It checks finiteness, cap, ceiling and the device's own gain range (`get_tx_gain_range` / `get_rx_gain_range` read back live, with the probe's 0–89.8 / 0–76 dB as fallback). The TX gain is written to hardware **only inside `capture(transmit=True)`**, immediately before the burst, and read back; `open()` and `configure()` leave it at 0 dB. |
| Stopping host sends / end-of-burst does not prove queued RF has stopped | Correct, and now stated in code. Mitigations: the abort path attenuates first (TX gain 0 dB) so anything still queued plays at maximum attenuation; the bring-up commands use a small TX queue (`BRINGUP_NUM_SEND_FRAMES = 64` ≈ 33 ms at 4 MS/s) to bound what can be queued at all. The residual is documented as a bound, not eliminated. |
| Comments estimating connector power from TX gain | Removed from the code; the comment now says the settings are relative and uncalibrated, and that connector power comes only from measurement. The "rough scale" in this document is kept only as an expectation to check the meter against, and labelled as such. |

Outcome: the software guards are as tight as a static review can make them. **This still does not
establish that transmitting through the cans is safe.** Only the measurement below does.

## Teammate third pass (Thomas Klopman, 5 Nov 2026) and what changed

Thomas confirmed the second-round changes and found three remaining issues. All addressed:

| Finding | Change |
|---|---|
| `_abort_tx()` discarded the TX thread reference even when the thread was still alive; further captures and handle release were not blocked | A worker that does not exit within the abort timeout is **kept** (`_tx_thread` preserved) and the driver is marked faulted: every later `capture()` (RX-only included) raises, and `close()` waits `close_timeout_s` (10 s) then **refuses to release the UHD handles** and raises rather than pulling them out from under a live `send()`. Test: a fake device that never accepts the send. |
| The "33 ms residual" was not verified; requested USB frame counts do not establish device buffering or shutdown latency; 0 dB gain is maximum attenuation, not RF off | Agreed. The code comment and this document now say exactly that: `BRINGUP_NUM_SEND_FRAMES` is a *requested* host-side parameter, the true residual is unknown until measured, and attenuation is a mitigation, not RF off. Added as measurement step 3 below: abort during a tone and time how long RF persists on the analyser. |
| TX short sends and exceptions only appeared in `tx_errors`; a frame could still be returned | Any TX error — short send, exception, abort, overrun, or a device-reported underflow / late packet from the async queue — now fails the CPI: the driver attenuates and raises `RuntimeError("transmit failed: …")`; no frame is returned. The sweep therefore stops on the first such event. |

Outcome unchanged in substance: software is as tight as static review allows; **the hardware
requirement (measured coupling or a justified attenuation budget for the actual can arrangement)
is still the gate for `--tx`.**

## Teammate fourth pass (Thomas Klopman, 5 Nov 2026) and what changed

Thomas ran isolated tests of the fault handling (all PASS except one OPEN ISSUE) and found three
remaining items. All addressed:

| Finding | Change |
|---|---|
| A short send only logged an error; the worker kept sending the next chunks, so cleanup was delayed until the main thread noticed | The worker now acts on its own failure: on the first short send or exception it raises the abort flag, requests TX gain 0 dB itself, closes the burst with an end-of-burst packet, and returns without sending another chunk. Test: with a device that accepts one sample fewer than offered, exactly one non-empty chunk is ever sent. |
| `stop()` docstring still said "RF off" and claimed a `num_send_frames` bound | Rewritten: best-effort shutdown, 0 dB gain is maximum attenuation, residual RF duration is unknown until measured (step 4). |
| Fault message said "close() and reopen" but nothing cleared `_tx_faulted` | `close()` is the recovery path: once the worker is verified to have exited it releases the handles **and clears the fault**; `open()` + `configure()` are then required before any capture. If the worker is still alive, `close()` still refuses and the fault stays latched. The message now says so. Test: stuck worker → close refused → device recovers → close clears → reopen, configure, capture succeeds. |

## Teammate fifth pass (Thomas Klopman) and what changed

Thomas confirmed the fourth-round changes (all isolated tests PASS) and found one remaining gap:
`TXMetadata` creation and `get_max_num_samps()` ran outside `_send_cpi()`'s try block, so an
exception there ended the worker without recording an error or requesting attenuation, and
`capture()` returned a frame with empty `tx_errors`.

| Finding | Change |
|---|---|
| Worker setup exceptions escaped silently | New outer wrapper `_tx_worker()` is the thread target and owns all exception handling: any failure in setup or sending records an error, requests attenuation and attempts end-of-burst. `_close_burst()` tolerates missing or half-initialised metadata (builds its own; records, never raises, if it cannot). A streamer reporting `max_num_samps <= 0` is treated as a failure. Belt and braces: the worker sets `_tx_done` only after a complete, clean burst, and `capture()` fails the CPI if the worker ended without it, even with an empty error list. Tests: metadata creation raising, `get_max_num_samps()` raising, and `max_num_samps == 0` — each fails the capture, sends no samples, and leaves TX gain at 0 dB. |

## Teammate sixth pass (Thomas Klopman) — software review closed

Thomas's sixth pass found **no additional blocking software issue in the tested paths** (13
isolated checks PASS against mock dependencies). His stated limits stand: no UHD was imported and
no B210 accessed, so actual UHD behaviour, async hardware reporting, USB buffering, RF output
power and shutdown latency remain unverified; maximum attenuation is not guaranteed RF off. His
conclusion: "the remaining basis for authorizing TX is verification of the physical RF setup and
controlled hardware testing, not further reliance on software checks alone."

## Attenuation budget and slow ramp (`scripts/tx_ramp_calibration.py`)

With no spectrum analyser or power meter on hand, the route to "a justified attenuation budget
for the actual cantenna arrangement" is a slow ramp under an explicit budget. **The budget is an
engineering justification, not a measurement, and needs a teammate's acceptance** (the script
will not arm without `--accepted-by <name>`, which is written to the log).

Assumptions: (a) the board never exceeds +20 dBm at full TX gain (the TX connector maximum listed
in the UHD B2x0 manual); (b) TX gain steps are accurate relative to each other. Then the power at
an RX connector is at most `20 − (89.8 − g) − fixed attenuation − coupling credit` dBm, and it
must stay ≤ −25 dBm (10 dB under the limit).

* **No credit at all → TX gain ≤ 44.8 dB.** Below that, even a bare cable from TX/RX to RX2 would
  be inside the margin, so the ramp from 0 to 44 dB does not depend on the cans' separation.
* **Above 44.8 dB needs measured coupling.** Loopback through a known fixed attenuator (≥ 30 dB)
  gives a reference level; the cans at the same settings give another; coupling loss =
  attenuator + (loopback − cans), smallest value seen, minus 3 dB of uncertainty. Cap =
  44.8 + credit, never above the 70 dB ceiling, and the operating cap is only ever a gain the
  ramp actually passed through cleanly. Re-run if the cans move.
* **The ramp:** tone, 0 dB upward in 2 dB steps, 0.5 s between steps. Stops on any UHD error, on
  the received peak reaching half scale, when a step does not change the level by its own size
  within 1.5 dB (assumption (b) is checked on every step), or before any step that would exceed
  the budget. Every step is logged to JSON in `PHALANX_data/captures`.

Loopback attenuation (10 Nov): the team has two 12 dB SMA attenuators (24 dB in series), not a
30 dB part. The ramp script's loopback run never exceeds 44 dB TX gain, where the budget bounds
even a bare cable at −25 dBm, so with 24 dB in line the bound is about −50 dBm; the script's
minimum for this run is therefore 20 dB. This departs from the 30 dB loopback figure cited in
the first review, which still applies to any loopback at higher gain or full power — **Thomas to
accept or reject this for the ramp's loopback run.**

What the budget does not cover: residual RF after an abort (measurement step 4 below still
needs an instrument), and assumption (a) itself. A spectrum analyser remains stronger evidence.

## Establishing the −15 dBm margin by measurement (before the B210 RX port ever sees TX)

1. **TX output vs gain, at the connector.** Spectrum analyser or power meter on RF A TX/RX
   (through an attenuator rated for the instrument), tone, TX gain 40, 50, 60, 70 dB
   (`--max-tx-gain 70` is the ceiling). Record dBm per setting → the calibrated table that
   replaces the "rough scale".
2. **Coupling into the RX can.** TX can on TX/RX, B210 **RX2 on a 50 Ω load**, the RX can's
   cable into the spectrum analyser. Transmit at each gain; record the received dBm. The
   highest gain at which the RX can delivers ≤ −25 dBm (10 dB margin under −15 dBm) is the
   operating cap, for this spacing and orientation only — re-measure if the cans move.
3. **Only then** connect the RX can to RX2 and run `isolation_sweep.py --arm` up to that cap.
4. **Shutdown latency.** With the analyser still on the RX can, run the tone and abort it
   (Ctrl-C the first-contact command, or `stop()`); note how long RF persists after the abort
   and at what level once TX gain is 0 dB. That number replaces the unverified "33 ms".
5. **Cable loopback** (TX/RX → RX2 by cable, for timing/zero-range work without antennas)
   needs **at least 30 dB of attenuation in line** (UHD manual) — never a bare cable.

## Pre-transmit checklist (physical; the code cannot check these)

- [ ] Teammate sign-off below.
- [ ] 6 V supply connected; B210 on a blue USB 3.0 port, no hub.
- [ ] TX can (or 50 Ω load) on **RF A TX/RX**; RX can on **RF A RX2**; cans separated and angled apart.
- [ ] Adviser answer on 2.45 GHz transmission on campus (RF-01 doc, section 6).
- [ ] Nothing else on the bench's RF inputs within a few metres (other SDRs, the Pasternack board).
- [ ] Steps 1–2 of the measurement procedure above done and recorded (dBm table, coupling table).
- [ ] First run is `python -m phalanx.hardware.usrp` without `--tx` (receive-only), then with `--tx`
      (tone, 40 dB), then `isolation_sweep.py --arm` up to the measured cap.

## Sign-off

| Reviewer | Date | Result | Notes |
|---|---|---|---|
| Claude (code review) | 2026-11-05 | code paths reviewed; guards above added this day | tests: `tests/test_usrp_driver.py` (13) |
| Thomas Klopman | 2026-11-05 | **not yet confirmed safe** — findings above; recommendations implemented the same day | re-review of the implemented changes still owed |
| Thomas Klopman (re-review) | 2026-11-05 | software guards improved; **transmission through the cans not yet established as safe** — six remaining gaps listed above, all addressed the same day | third pass on the restructured `capture()` / `stop()` owed; measurement procedure still owed |
| Thomas Klopman (third pass) | 2026-11-05 | most software concerns addressed; three remaining (live-thread shutdown, unverified residual, TX errors not failing the capture) — all addressed the same day; **RF input power must be verified before `--tx`** | fourth pass on the fault-handling changes owed; measurement still owed |
| Thomas Klopman (fourth pass) | 2026-11-05 | isolated tests PASS except "short send logs an error but sends the next chunk"; three findings, all addressed the same day; **RF input power must be established by measurement before `--tx`** | fifth pass owed on the worker self-abort; measurement still the gate |
| Thomas Klopman (fifth pass) | 2026-11 | fourth-round items confirmed; one gap (worker setup exceptions escaped; frame returned with empty `tx_errors`) — addressed; **does not certify damage-free operation; RF input power still to be established** | sixth pass owed on `_tx_worker()`; measurement or attenuation budget still the gate |
| Thomas Klopman (sixth pass) | 2026-11 | **no additional blocking software issue in the tested paths**; software review closed. Not a hardware validation; does not authorize TX | TX authorization now rests on the physical RF setup: measurement, or acceptance of the attenuation budget above |
| Budget acceptance (teammate / adviser) | | | assumptions (a) and (b); name also goes in `--accepted-by` |
| GUI can arm the transmitter (12 Nov): `phalanx/gui/app.py`, source "B210 FMCW radar (TX+RX)" | | | Off unless "ARM transmitter" is ticked before Start; unticks after every run; TX gain box capped at the budget limit or the latest calibration's cap; builds `B210FmcwRadar(transmit=armed, max_tx_gain_db=limit)`, so the driver's guards apply unchanged. Tests: `test_b210_fmcw_gui.py`. Thomas to confirm. |
| Driver change after the review closed: `"fmcw"` waveform kind (10 Nov) | | | ~25 lines in `build_cpi_waveform` / `__init__` / `configure`; constant envelope at `tx_amplitude`, 100 % duty like the tone; no transmit guard touched. Thomas to confirm. |
