"""stage_dump.py: the stage-by-stage check of the B210 FMCW chain on a simulated frame.

These are the checks quoted in the Part 2 design document (Signal processing chain)."""
import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "stage_dump.py"
spec = importlib.util.spec_from_file_location("stage_dump", SCRIPT)
stage_dump = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage_dump)


def test_every_stage_matches_its_expected_value_on_a_simulated_walker(capsys):
    assert stage_dump.main([]) == 0
    out = capsys.readouterr().out
    assert "16 of 16 checks passed" in out
    assert "stage-by-stage equals the pipeline" in out and "FAIL" not in out


def test_a_stationary_target_is_removed_by_mti_and_the_check_says_so(capsys):
    assert stage_dump.main(["--speed", "0"]) == 1          # no detection: the script must not pass
    assert "FAILED" in capsys.readouterr().out


def test_figure_and_table_are_written(tmp_path, capsys):
    png = tmp_path / "stages.png"
    assert stage_dump.main(["--out", str(png)]) == 0
    capsys.readouterr()
    assert png.stat().st_size > 10_000
    rows = json.loads(png.with_suffix(".json").read_text(encoding="utf-8"))
    assert [r["stage"] for r in rows][:2] == ["F1", "F1"] and rows[-1]["obtained"] == "identical"
