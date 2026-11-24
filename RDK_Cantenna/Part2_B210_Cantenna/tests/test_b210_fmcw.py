"""B210 FMCW mode (digital dechirp → kit-style frames): waveform, latency estimate, and the
unchanged Part 1 chain finding a simulated target at the right range and velocity."""
from types import SimpleNamespace

import numpy as np
import pytest

from phalanx import ProcessorConfig, RangeDopplerProcessor
from phalanx.hardware import b210_fmcw as mod
from phalanx.hardware.b210_fmcw import (B210FmcwRadar, b210_fmcw_params, decimation, dechirp,
                                        estimate_latency, raw_params)
from phalanx.hardware.usrp import build_cpi_waveform
from phalanx.params import C0

LATENCY = 37                      # samples the simulated hardware delays TX → RX


def simulate_rx(raw, ref, targets=(), leak_db=-40.0, noise_rms=1e-4, seed=0):
    """Raw I/Q a B210 would return: leakage + echoes (delay, Doppler) + noise."""
    n = len(ref)
    rng = np.random.default_rng(seed)
    x = noise_rms / np.sqrt(2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    x[LATENCY:] += 10 ** (leak_db / 20) * ref[:n - LATENCY]
    t = np.arange(n) / raw.fs_hz
    for range_m, velocity_mps, level_db in targets:
        m = LATENCY + int(round(2 * range_m / C0 * raw.fs_hz))
        r_t = range_m - velocity_mps * t                    # closing target: range shrinks
        echo = np.zeros(n, dtype=complex)
        echo[m:] = ref[:n - m]
        x += 10 ** (level_db / 20) * echo * np.exp(-2j * np.pi * raw.fc_hz * 2 * r_t / C0)
    return x.astype(np.complex64)


def test_params_and_decimation():
    p = b210_fmcw_params()
    assert (p.waveform, p.sweep, p.fs_hz, p.n_samples) == ("fmcw", "triangle", 20e3, 4096)
    assert p.n_fast == 80 and p.n_pulses == 25                 # same shape as the kit's frames
    assert p.fc_hz == pytest.approx(2.45e9) and p.range_resolution_m == pytest.approx(49.97, abs=0.01)
    raw = raw_params(p, 4e6)
    assert raw.n_samples == 4096 * 200 and raw.n_fast == 16000 and decimation(4e6) == 200
    with pytest.raises(ValueError):
        decimation(4.01e6)
    with pytest.raises(ValueError, match="does not fit"):
        raw_params(b210_fmcw_params(bandwidth_mhz=3.9), 4e6)


def test_triangle_waveform():
    raw = raw_params(b210_fmcw_params(), 4e6)
    wf = build_cpi_waveform(raw, "fmcw")
    assert wf.dtype == np.complex64 and len(wf) == raw.n_samples
    np.testing.assert_allclose(np.abs(wf), 1.0, atol=1e-5)     # constant envelope, 100 % duty
    f = np.diff(np.unwrap(np.angle(wf[:3 * raw.n_fast]))) * raw.fs_hz / (2 * np.pi)
    assert f.min() > -1.51e6 and f.max() < 1.51e6              # stays inside ±B/2
    assert f[100] < f[8000] < f[15900]                         # ramp 0 sweeps up
    assert f[16100] > f[24000] > f[31900]                      # ramp 1 sweeps down
    with pytest.raises(ValueError):
        build_cpi_waveform(raw_params(b210_fmcw_params(), 4e6).__class__(waveform="plfm",
                           pulse_width_s=1e-4, prf_hz=1e3, fs_hz=4e6), "fmcw")


def test_latency_is_found_from_the_leakage():
    raw = raw_params(b210_fmcw_params(), 4e6)
    ref = build_cpi_waveform(raw, "fmcw")
    k, peak_db = estimate_latency(simulate_rx(raw, ref), ref)
    assert k == LATENCY and peak_db > 20


def test_part1_chain_finds_a_target_in_b210_fmcw_data():
    p = b210_fmcw_params()
    raw = raw_params(p, 4e6)
    ref = build_cpi_waveform(raw, "fmcw")
    rx = simulate_rx(raw, ref, targets=[(300.0, +1.5, -70.0)])          # 30 dB below the leakage
    beat = dechirp(rx, ref, LATENCY, 200)
    assert len(beat) == p.n_samples
    frame = SimpleNamespace(samples=beat.real.astype(np.float32), params=p, seq=1, meta={})

    # Zero-range calibration: with the latency removed, the leakage lands at zero beat
    # frequency, i.e. it is the DC level of the beat (−40 dB = 0.01), which is zero range.
    assert abs(beat.mean()) == pytest.approx(0.01, rel=0.1)
    # The chain removes DC (as it does for the kit), so without MTI the strongest range cell
    # left is the target's: 300 m / 50 m per cell = cell 6.
    still = RangeDopplerProcessor(p, ProcessorConfig(ramp_offset=0, ramp_parity=0, mti_order=0))
    profile = np.abs(still.process(frame).range_profiles).mean(axis=0)
    assert int(np.argmax(profile)) == 6

    # With MTI (Part 1 defaults otherwise): the moving target, right range cell, right sign.
    proc = RangeDopplerProcessor(p, ProcessorConfig(ramp_offset=0, ramp_parity=0, mti_order=1))
    dets = proc.process(frame).detections
    assert dets, "no detection"
    best = max(dets, key=lambda d: d.snr_db)
    assert abs(best.range_m - 300.0) <= p.range_resolution_m
    assert best.velocity_mps == pytest.approx(+1.5, abs=0.35)           # + = closing, as in Part 1


def test_radar_wrapper_returns_kit_style_frames(monkeypatch):
    p = b210_fmcw_params()

    class FakeUsrp:
        def __init__(self, params, **kw):
            self.params, self.kw, self.info = params, kw, {"model": "B210"}
            self.captures = []
        def open(self): return self
        def close(self): pass
        def configure(self): pass
        def start(self): pass
        def stop(self): pass
        def set_tx_gain(self, g): return g
        def capture(self, transmit=None):
            tx_on = self.kw["transmit"] if transmit is None else transmit
            self.captures.append(tx_on)
            ref = build_cpi_waveform(self.params, "fmcw")
            x = simulate_rx(self.params, ref, leak_db=-40.0 if tx_on else -300.0)
            return SimpleNamespace(samples=x, timestamp=1.0, meta={
                "rx_errors": [], "tx_errors": [], "device_time_s": 0.2, "rx_peak": 0.01,
                "rx_clipping": False, "tx_enabled": tx_on, "tx_gain": 40.0, "rx_gain": 40.0,
                "capture_s": 0.3, "idn": "B210"})

    monkeypatch.setattr(mod, "UsrpRadar", FakeUsrp)
    with B210FmcwRadar(p) as radar:                         # default beat: complex I/Q
        radar.configure()
        f = radar.capture(transmit=True)
        assert f.samples.dtype == np.complex64 and len(f) == 4096 and f.meta["signedness"] == "complex64"
    with pytest.raises(ValueError):
        B210FmcwRadar(p, beat="iq")
    with B210FmcwRadar(p, beat="real") as radar:            # kit-style frames; receive-only by default
        radar.configure()
        assert radar._usrp.kw["transmit"] is False and radar._usrp.kw["tx_waveform"] == "fmcw"
        assert radar._usrp.kw["max_tx_gain_db"] == 44.0
        quiet = radar.capture()
        assert quiet.meta["tx_enabled"] is False and radar.latency_samples is None
        f = radar.capture(transmit=True)
        assert f.samples.dtype == np.float32 and not np.iscomplexobj(f.samples)
        assert len(f) == 4096 and f.params is p and f.meta["signal_mode"] == "fmcw_dechirped"
        assert radar.latency_samples == LATENCY and f.meta["latency_samples"] == LATENCY
        radar.capture(transmit=True)
        assert radar.latency_samples == LATENCY             # measured once, then held


# ---------------------------------------------------------------- Part 2 DSP draft (12 Nov)

from phalanx.dsp.detect import CfarConfig, ca_cfar                                     # noqa: E402
from phalanx.hardware.b210_fmcw import (SimulatedB210Fmcw, SimWalker, beat_samples,   # noqa: E402
                                        b210_fmcw_processor_config, blind_range_m)


def _frame_with_target(range_m, velocity, beat, bandwidth_mhz=3.0, fs=4e6, level_db_at_10m=-62.0):
    p = b210_fmcw_params(bandwidth_mhz=bandwidth_mhz)
    sim = SimulatedB210Fmcw(p, raw_fs_hz=fs, frame_period_s=0, beat=beat, latency_samples=LATENCY,
                            walkers=[SimWalker(range_m=range_m, velocity_mps=velocity, far_m=500,
                                               level_db_at_10m=level_db_at_10m)])
    sim.start()
    return p, sim.capture(), sim


def test_complex_beat_gives_the_sign_of_the_speed_even_in_cell_zero():
    """A walker 10 m away is inside range cell 0 at 3 MHz. Real beat: both signs appear.
    Complex beat: only the true one."""
    look_inside = dict(min_range_m=0.0, min_speed_mps=0.8)
    p, frame, _ = _frame_with_target(10.0, +1.4, "complex")
    dets = RangeDopplerProcessor(p, b210_fmcw_processor_config(p, **look_inside)).process(frame).detections
    strong = [d for d in dets if d.snr_db > max(q.snr_db for q in dets) - 10]
    assert strong and all(d.velocity_mps == pytest.approx(+1.4, abs=0.35) for d in strong)
    p, frame, _ = _frame_with_target(10.0, +1.4, "real")
    dets = RangeDopplerProcessor(p, b210_fmcw_processor_config(p, **look_inside)).process(frame).detections
    signs = {np.sign(d.velocity_mps) for d in dets if d.snr_db > max(q.snr_db for q in dets) - 10}
    assert signs == {-1.0, 1.0}                                   # the kit-style ambiguity


def test_complex_range_profile_keeps_the_positive_half():
    p, frame, _ = _frame_with_target(300.0, -1.0, "complex", level_db_at_10m=-10.0)   # -69 dBFS at 300 m
    r = RangeDopplerProcessor(p, b210_fmcw_processor_config(p)).process(frame)
    assert r.range_profiles.shape[1] == p.n_fast // 2 + 1 == len(r.range_axis)
    best = max(r.detections, key=lambda d: d.snr_db)
    assert abs(best.range_m - 300.0) <= p.range_resolution_m
    assert best.velocity_mps == pytest.approx(-1.0, abs=0.35)      # receding reads negative


def test_default_config_has_a_blind_range_of_one_and_a_half_cells():
    p = b210_fmcw_params(bandwidth_mhz=6.0)
    cfg = b210_fmcw_processor_config(p)
    assert blind_range_m(p) == pytest.approx(1.5 * 24.98, abs=0.1)
    assert cfg.min_range_m == pytest.approx(blind_range_m(p))
    assert (cfg.ramp_offset, cfg.ramp_parity, cfg.cfar.range_mode) == (0, 0, "ca")
    assert cfg.max_range_m == 300.0 and b210_fmcw_processor_config(b210_fmcw_params()).max_range_m > 300


def test_simulator_walker_is_tracked_beyond_the_blind_range_and_not_inside_it():
    p = b210_fmcw_params(bandwidth_mhz=6.0)                       # 8 MS/s, 25 m cells
    sim = SimulatedB210Fmcw(p, raw_fs_hz=8e6, frame_period_s=0, latency_samples=83,
                            walkers=[SimWalker(range_m=80.0, velocity_mps=1.4)])
    proc = RangeDopplerProcessor(p, b210_fmcw_processor_config(p))
    sim.start()
    frame = sim.capture()
    assert sim.latency_samples == 83 and frame.meta["sim"] and frame.meta["tx_enabled"]
    assert frame.meta["leakage_dbfs"] == pytest.approx(-24.0, abs=0.5)
    best = max(proc.process(frame).detections, key=lambda d: d.snr_db)
    assert abs(best.range_m - 80.0) <= p.range_resolution_m
    assert best.velocity_mps == pytest.approx(+1.4, abs=0.35)
    sim.walkers[0].range_m = 12.0                                 # well inside the blind range
    assert all(d.range_m >= blind_range_m(p) for d in proc.process(sim.capture()).detections)
    quiet = sim.capture(transmit=False)                           # receive-only: no leakage at all
    assert not quiet.meta["tx_enabled"] and quiet.meta["leakage_dbfs"] < -60


def test_simulator_walker_turns_around_at_the_near_end():
    sim = SimulatedB210Fmcw(frame_period_s=0, walkers=[SimWalker(range_m=5.2, velocity_mps=1.4)])
    sim.start()
    sim.capture()
    assert sim.walkers[0].velocity_mps < 0 and sim.walkers[0].range_m >= 5.0


def test_soca_ignores_a_window_that_runs_off_the_map():
    rng = np.random.default_rng(0)
    p = rng.exponential(1.0, size=(25, 41)).astype(np.float32)
    p[:, 0] *= 1e4                                                 # leakage cell, 40 dB up
    p[12, 3] = 400.0                                               # a target three cells away, 26 dB
    ca, _, _ = ca_cfar(p, CfarConfig())
    soca, noise, _ = ca_cfar(p, CfarConfig(range_mode="soca"))
    assert not ca[12, 3]                                           # hidden by the repeated edge cell
    assert soca[12, 3]
    assert noise[12, 3] < 3.0                                      # threshold from the far side only
    with pytest.raises(ValueError):
        ca_cfar(p, CfarConfig(range_mode="os"))


def test_beat_samples_kinds():
    b = (np.arange(4) * (1 + 2j)).astype(np.complex64)
    assert beat_samples(b, "complex").dtype == np.complex64
    r = beat_samples(b, "real")
    assert r.dtype == np.float32 and np.array_equal(r, [0, 1, 2, 3])
