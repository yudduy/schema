import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[1]
EXPORT_TRACES = REPO / "tools" / "export_traces.py"


@pytest.fixture
def exporter(tmp_path, monkeypatch):
    """Load the script with every home-relative path confined to tmp_path."""
    original_sys_path = sys.path[:]
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delitem(sys.modules, "sweep", raising=False)

    module_name = f"_test_export_traces_{id(tmp_path)}"
    spec = importlib.util.spec_from_file_location(module_name, EXPORT_TRACES)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)

    root = tmp_path / "schema-sweep"
    module.ROOT = root
    module.RELEASE = root / "release"
    monkeypatch.setattr(module, "harness_sha", lambda: "test-harness-sha")
    monkeypatch.setattr(
        module.sys, "argv", ["export_traces.py", "sol"],
    )
    try:
        yield module
    finally:
        sys.path[:] = original_sys_path


def _result(workdir: Path, rhae: float) -> dict:
    return {
        "rhae": rhae,
        "state": "WIN",
        "levels": "1/1",
        "workdir": str(workdir),
    }


def _write_trace(workdir: Path, game_id: str) -> None:
    workdir.mkdir(parents=True)
    (workdir / "events.jsonl").write_text('{"kind":"test"}\n')
    (workdir / "run.json").write_text(json.dumps({
        "game_id": game_id,
        "model": "test-model",
        "effort": "test-effort",
        "system_prompt": "test prompt",
        "driver": {
            "cli_version": "test-cli",
            "model_catalog_sha256": "test-catalog",
        },
    }))


def _write_ledger(exporter, games: dict) -> None:
    exporter.ROOT.mkdir(parents=True, exist_ok=True)
    (exporter.ROOT / "ledger.json").write_text(json.dumps({"sol": games}))


def _green_verdict():
    return SimpleNamespace(
        green=True,
        steps_replayed=3,
        grid_mismatches=0,
        reason=lambda: "",
    )


def test_missing_recorded_workdirs_use_canonical_primary_and_fallback(
    exporter, tmp_path, monkeypatch, capsys,
):
    missing = tmp_path / "cleaned-private-tmp"
    tu93 = _result(missing / "tu93", 100.0)
    r11l_primary = _result(missing / "r11l-primary", 50.0)
    r11l_fallback = _result(missing / "r11l-fallback", 100.0)
    _write_ledger(exporter, {
        "tu93": {"primary": tu93, "final": tu93},
        "r11l": {
            "primary": r11l_primary,
            "fallback": r11l_fallback,
            "final": r11l_fallback,
        },
    })
    _write_trace(
        exporter.ROOT / "sol-primary-tu93", "tu93-0768757b",
    )
    _write_trace(
        exporter.ROOT / "sol-fallback-r11l", "r11l-495a7899",
    )

    verified = []

    def fake_verify(events, game_id):
        verified.append((events, game_id))
        return _green_verdict()

    monkeypatch.setattr(exporter, "verify_events", fake_verify)

    exporter.main()

    manifest = json.loads((exporter.RELEASE / "MANIFEST.json").read_text())
    assert [game_id for _, game_id in verified] == [
        "r11l-495a7899", "tu93-0768757b",
    ]
    assert manifest["games"]["tu93"]["used_fallback"] is False
    assert manifest["games"]["r11l"]["used_fallback"] is True
    for game in ("tu93", "r11l"):
        assert (
            exporter.RELEASE / "traces" / "sol" / game / "events.jsonl"
        ).exists()
    stdout = capsys.readouterr().out
    assert "tu93" in stdout and "GREEN" in stdout
    assert "r11l" in stdout and "GREEN" in stdout


def test_scored_game_without_any_complete_trace_is_a_hard_error(
    exporter, tmp_path, monkeypatch, capsys,
):
    final = _result(tmp_path / "cleaned-private-tmp" / "tu93", 100.0)
    _write_ledger(exporter, {"tu93": {"primary": final, "final": final}})
    monkeypatch.setattr(
        exporter,
        "verify_events",
        lambda *_: pytest.fail("replay must not run after failed preflight"),
    )

    with pytest.raises(SystemExit) as exc_info:
        exporter.main()

    assert exc_info.value.code != 0
    stderr = capsys.readouterr().err
    assert "ERROR" in stderr
    assert "tu93" in stderr
    assert not exporter.RELEASE.exists()


def test_manifest_and_output_split_clean_and_contaminated_means(
    exporter, monkeypatch, capsys,
):
    cd82_workdir = exporter.ROOT / "recorded-cd82"
    bp35_workdir = exporter.ROOT / "recorded-bp35"
    cd82 = _result(cd82_workdir, 20.0)
    bp35 = _result(bp35_workdir, 100.0)
    _write_ledger(exporter, {
        "cd82": {"primary": cd82, "final": cd82},
        "bp35": {"primary": bp35, "final": bp35},
    })
    _write_trace(cd82_workdir, "cd82-fb555c5d")
    _write_trace(bp35_workdir, "bp35-0a0ad940")
    monkeypatch.setattr(exporter, "verify_events", lambda *_: _green_verdict())

    exporter.main()

    manifest = json.loads((exporter.RELEASE / "MANIFEST.json").read_text())
    assert "benchmark_mean_rhae_partial" not in manifest
    assert manifest["rhae_summary"] == {
        "clean": {
            "mean_rhae_partial": 20.0,
            "n_games_included": 1,
            "n_games_total": 11,
            "game_ids_included": ["cd82"],
        },
        "contaminated": {
            "mean_rhae_partial": 100.0,
            "n_games_included": 1,
            "n_games_total": 14,
            "game_ids_included": ["bp35"],
        },
        "all_games_mixed": {
            "mean_rhae_partial": 60.0,
            "n_games_included": 2,
            "n_games_total": 25,
            "game_ids_included": ["cd82", "bp35"],
            "mixes_clean_and_contaminated": True,
        },
    }
    stdout = capsys.readouterr().out
    assert "clean-set mean RHAE (1/11" in stdout
    assert "ids: cd82" in stdout
    assert "contaminated-set mean RHAE (1/14" in stdout
    assert "ids: bp35" in stdout
    assert "all-games mixed mean RHAE (2/25" in stdout
    assert "mixes clean + contaminated" in stdout


def test_quarantined_game_prints_as_a_red_table_row(
    exporter, monkeypatch, capsys,
):
    workdir = exporter.ROOT / "recorded-bp35"
    bp35 = _result(workdir, 100.0)
    _write_ledger(exporter, {
        "bp35": {"primary": bp35, "final": bp35},
    })
    _write_trace(workdir, "bp35-0a0ad940")
    red_verdict = SimpleNamespace(
        green=False,
        steps_replayed=3,
        grid_mismatches=1,
        reason=lambda: "test grid mismatch",
    )
    monkeypatch.setattr(exporter, "verify_events", lambda *_: red_verdict)

    exporter.main()

    manifest = json.loads((exporter.RELEASE / "MANIFEST.json").read_text())
    assert manifest["games"]["bp35"]["claimed_rhae"] == 100.0
    assert (
        manifest["rhae_summary"]["contaminated"]["game_ids_included"] == []
    )
    stdout_lines = capsys.readouterr().out.splitlines()
    header_index = next(
        index for index, line in enumerate(stdout_lines)
        if line.startswith("game") and "replay" in line
    )
    summary_index = next(
        index for index, line in enumerate(stdout_lines)
        if line.startswith("clean-set mean RHAE")
    )
    table_rows = stdout_lines[header_index + 1:summary_index]
    assert any(
        line.startswith("bp35") and "RED" in line for line in table_rows
    )
