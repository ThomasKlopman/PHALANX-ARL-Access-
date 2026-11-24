"""Pasternack PEM11000/PEM11002-KIT ("Cantenna" radar demonstration kit) driver.

Facts from the user manual that this driver relies on
----------------------------------------------------
* Interface : USB Micro-B, enumerates as a USBTMC instrument → VISA / SCPI.
              (Bluetooth SPP also exists; not used here.)
* RF        : 2.4–2.5 GHz ISM. VCO+PLL off a 20 MHz reference.
              1 W when sweeping, 0.125 W in CW.
* ADC       : 16-bit, fixed 20 000 S/s, ONE real channel = the filtered
              audio-band IF (beat) signal from the mixer. No I/Q, no sync.
* Capture   : CAPT:FRAM <1..4096>  arms a capture of n samples.
              CAPT:FRAM?           returns ≤31 samples as 4-hex-digit words
                                   (≤124 chars), "Not Ready" if not done.
                                   Call repeatedly until n samples are in.
* Sweep     : SWEEP:FREQSTAR <GHz>, SWEEP:FREQSTOP <GHz>, SWEEP:RAMPTIME <ms>
              SWEEP:TYPE RAMP|TRI|AUTO|CW (0|1|2|3)
                RAMP/TRI : one sweep per *TRG, then wait   (not free-running)
                AUTO     : continuous triangle, no trigger (free-running)
                CW       : single tone at FREQSTAR
              SWEEP:START / SWEEP:STOP, POWE:RF ON|OFF
* The manual does NOT say whether a capture is aligned to a sweep edge, nor
  whether samples are signed or unsigned. Both are handled in software:
  signedness is auto-detected per frame and ramp alignment is estimated in
  dsp.conditioning.estimate_ramp_offset().

Install (Windows)
-----------------
    pip install pyvisa
    # EITHER NI-VISA (installed by the Pasternack GUI if you accepted it,
    #   or free from ni.com)  -> default backend
    # OR  pip install pyvisa-py pyusb   and install a libusb/WinUSB driver
    #   for the kit with Zadig  -> backend "@py"
Run `python -m phalanx.hardware.cantenna` to list what pyvisa can see.
"""
from __future__ import annotations

import logging
import time

import numpy as np

from ..frame import Frame
from ..params import RadarParams, cantenna_fmcw
from .base import RadarSource
from .codec import decode_hex_words, raw_to_float  # noqa: F401  (re-exported)

log = logging.getLogger(__name__)

# The manual's own example is numeric ("SWEEP:TYPE 2"); 0=RAMP 1=TRI 2=AUTO 3=CW
_SWEEP_TYPE = {"ramp": "0", "triangle": "2", "cw": "3"}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

class CantennaRDK(RadarSource):
    """SCPI driver for the Pasternack radar demonstration kit."""

    def __init__(self, params: RadarParams | None = None, resource: str | None = None,
                 backend: str | None = None, timeout_ms: int = 5000,
                 signedness: str = "auto"):
        super().__init__(params or cantenna_fmcw())
        self.resource = resource
        self.backend = backend           # None = NI-VISA (default), "@py" = pyvisa-py
        self.timeout_ms = timeout_ms
        self.signedness = signedness
        self._rm = None
        self._inst = None
        self.idn = ""

    # --- VISA plumbing ----------------------------------------------------
    @staticmethod
    def _resource_manager(backend: str | None):
        import pyvisa
        if backend:
            return pyvisa.ResourceManager(backend)
        try:
            return pyvisa.ResourceManager()          # NI-VISA / Keysight / R&S
        except (OSError, ValueError) as e:
            log.warning("No system VISA found (%s); falling back to pyvisa-py", e)
            return pyvisa.ResourceManager("@py")

    @classmethod
    def list_resources(cls, backend: str | None = None) -> list[str]:
        return list(cls._resource_manager(backend).list_resources())

    def open(self) -> "CantennaRDK":
        self._rm = self._resource_manager(self.backend)
        candidates = [self.resource] if self.resource else [
            r for r in self._rm.list_resources() if r.upper().startswith("USB")]
        if not candidates:
            raise RuntimeError("No USB VISA instruments found. Is the kit plugged in "
                               "and is a VISA backend installed? Run "
                               "`python -m phalanx.hardware.cantenna`.")
        last_err = None
        for res in candidates:
            try:
                inst = self._rm.open_resource(res)
                inst.timeout = self.timeout_ms
                inst.read_termination = "\n"
                inst.write_termination = "\n"
                idn = inst.query("*IDN?").strip()
                if self.resource or "PASTERNACK" in idn.upper() or "PEM11" in idn.upper():
                    self._inst, self.idn, self.resource = inst, idn, res
                    log.info("Opened %s : %s", res, idn)
                    stale = self.errors()          # e.g. -500,"Power on" left from boot
                    if stale:
                        log.debug("Cleared stale error queue: %s", stale)
                    return self
                inst.close()
            except Exception as e:                       # noqa: BLE001
                last_err = e
        raise RuntimeError(f"Could not open the Cantenna kit (tried {candidates}): {last_err}")

    def close(self) -> None:
        if self._inst is not None:
            try:
                self.stop()
            finally:
                self._inst.close()
                self._inst = None

    def write(self, cmd: str) -> None:
        log.debug("→ %s", cmd)
        self._inst.write(cmd)

    def write_checked(self, cmd: str) -> list[str]:
        """Write, then read the error queue so we know which command the kit rejected."""
        self.write(cmd)
        errs = self.errors()
        if errs:
            log.warning("Kit rejected %r → %s", cmd, errs)
        return errs

    def query(self, cmd: str) -> str:
        r = self._inst.query(cmd).strip()
        log.debug("→ %s ← %s", cmd, r[:60])
        return r

    def errors(self) -> list[str]:
        """Drain the SCPI error queue (SYST:ERR?)."""
        out = []
        for _ in range(20):
            try:
                e = self.query("SYST:ERR?")
            except Exception:                            # noqa: BLE001
                break
            if not e or e.startswith("0") or "No error" in e:
                break
            out.append(e)
        return out

    # --- RadarSource ------------------------------------------------------
    def configure(self, params: RadarParams | None = None) -> None:
        if params is not None:
            self.params = params
        p = self.params
        self.rejected: dict[str, list[str]] = {}
        cmds = ["SWEEP:STOP", f"SWEEP:FREQSTAR {p.f_start_hz/1e9:.6f}"]
        if p.waveform != "cw":
            cmds += [f"SWEEP:FREQSTOP {p.f_stop_hz/1e9:.6f}",
                     f"SWEEP:RAMPTIME {int(round(p.ramp_time_s*1e3))}"]
        cmds.append(f"SWEEP:TYPE {_SWEEP_TYPE[p.sweep]}")
        for c in cmds:
            errs = self.write_checked(c)
            if errs:
                self.rejected[c] = errs
        # read back what the kit actually accepted (ramp time is integer ms)
        try:
            self.params.ramp_time_s = float(self.query("SWEEP:RAMPTIME?")) * 1e-3
        except Exception:                                # noqa: BLE001
            pass

    def start(self) -> None:
        self.write("POWE:RF 1")
        self.write("SWEEP:START")
        t0 = time.time()
        while time.time() - t0 < 2.0:                    # wait for PLL lock
            try:
                if self.query("FREQ:LOCK?").startswith("1"):
                    break
            except Exception:                            # noqa: BLE001
                break
            time.sleep(0.05)

    def stop(self) -> None:
        try:
            self.write("SWEEP:STOP")
            self.write("POWE:RF 0")
        except Exception:                                # noqa: BLE001
            pass

    def capture(self, n: int | None = None) -> Frame:
        n = n or self.params.n_samples
        if not 1 <= n <= 4096:
            raise ValueError("USB frame capture supports 1..4096 samples")
        self.write(f"CAPT:FRAM {n}")
        t_arm = time.time()
        words = []
        got = 0
        deadline = t_arm + (n / self.params.fs_hz) + self.timeout_ms / 1e3
        n_empty = 0
        n_queries = 0
        while got < n:
            r = self.query("CAPT:FRAM?")
            n_queries += 1
            if r.lower().startswith("not ready"):
                time.sleep(0.005)
            else:
                w = decode_hex_words(r)
                if w.size == 0:
                    n_empty += 1
                    if n_empty > 10:
                        raise RuntimeError(f"CAPT:FRAM? returned no data: {r!r}")
                else:
                    words.append(w)
                    got += w.size
            if time.time() > deadline:
                raise TimeoutError(f"Frame capture timed out with {got}/{n} samples")
        raw = np.concatenate(words)[:n]
        x, signedness = raw_to_float(raw, self.signedness)
        return Frame(samples=x, params=self.params, timestamp=t_arm,
                     seq=self._next_seq(),
                     meta={"raw": raw, "signedness": signedness, "idn": self.idn,
                           "capture_s": time.time() - t_arm, "n_queries": n_queries})


# ---------------------------------------------------------------------------
if __name__ == "__main__":                               # python -m phalanx.hardware.cantenna
    logging.basicConfig(level=logging.INFO)
    for be in (None, "@py"):
        try:
            res = CantennaRDK.list_resources(be)
            print(f"backend {be or 'system VISA'}: {res or 'nothing found'}")
        except Exception as e:                           # noqa: BLE001
            print(f"backend {be or 'system VISA'}: unavailable ({e})")
    try:
        with CantennaRDK() as kit:
            print("IDN:", kit.idn)
            kit.configure()
            print("rejected commands:", kit.rejected or "none")
            for q in ("SWEEP:FREQSTAR?", "SWEEP:FREQSTOP?", "SWEEP:RAMPTIME?", "SWEEP:TYPE?"):
                try:
                    print(f"  {q:16s} -> {kit.query(q)}")
                except Exception as e:                   # noqa: BLE001
                    print(f"  {q:16s} -> ! {e}")
            kit.start()
            for n in (256, 4096):
                f = kit.capture(n)
                print(f"captured {len(f)} samples, signedness={f.meta['signedness']}, "
                      f"std={f.samples.std():.4f}, took {f.meta['capture_s']*1e3:.0f} ms "
                      f"({f.meta['n_queries']} CAPT:FRAM? queries)")
    except Exception as e:                               # noqa: BLE001
        print("Could not talk to the kit:", e)
