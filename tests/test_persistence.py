from __future__ import annotations

import json
import os
import pytest

from schema_harness import persistence
from spikes import sweep


ATOMIC_WRITERS = (persistence.atomic_json,)


@pytest.mark.parametrize(
    "payload",
    [
        {"autoCompactEnabled": False},
        {
            "game_id": "bp35-0a0ad940",
            "model": "modèle-雪",
            "driver": {"max_turns": 80, "enabled_locus_tools": ["commit_actions"]},
        },
        {
            "version": 1,
            "turns": {
                "turn-000001": {
                    "phase": "COMPLETE",
                    "actions": [[6, 12, 34]],
                    "result": {"halt_reason": "level_up"},
                }
            },
        },
        {
            "cwd": "/tmp/run",
            "provider": "codex",
            "sid": "",
            "resume": False,
            "invalidated": True,
        },
    ],
)
def test_atomic_json_writer_parity(payload, tmp_path, monkeypatch):
    expected = (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")

    for index, writer in enumerate(ATOMIC_WRITERS):
        fsynced = []
        monkeypatch.setattr(os, "fsync", fsynced.append)
        path = tmp_path / str(index) / "payload.json"

        writer(path, payload)

        assert path.read_bytes() == expected
        assert json.loads(path.read_text(encoding="utf-8")) == payload
        assert len(fsynced) == 1


@pytest.mark.parametrize("writer", ATOMIC_WRITERS)
def test_atomic_json_replace_failure_preserves_previous_file(
    writer, tmp_path, monkeypatch
):
    path = tmp_path / "payload.json"
    previous = b'{"previous":true}\n'
    path.write_bytes(previous)

    def fail_replace(_source, _destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        writer(path, {"replacement": True})

    assert path.read_bytes() == previous
    assert json.loads(path.read_text(encoding="utf-8")) == {"previous": True}


@pytest.mark.parametrize("writer", ATOMIC_WRITERS)
def test_atomic_json_rejects_nan_without_replacing_previous_file(writer, tmp_path):
    path = tmp_path / "payload.json"
    previous = b'{"previous":true}\n'
    path.write_bytes(previous)

    with pytest.raises(ValueError, match="Out of range float values"):
        writer(path, {"not_finite": float("nan")})

    assert path.read_bytes() == previous


def test_sweep_ledger_and_budget_update_preserve_existing_format_and_fields(
    tmp_path, monkeypatch
):
    ledger_path = tmp_path / "ledger.json"
    monkeypatch.setattr(sweep, "LEDGER", ledger_path)
    fsynced = []
    monkeypatch.setattr(os, "fsync", fsynced.append)
    ledger = {"sol": {"bp35": {"note": "café", "rhae": 93.51}}}

    sweep.save_ledger(ledger)

    assert ledger_path.read_text(encoding="utf-8") == json.dumps(ledger, indent=2)
    assert len(fsynced) == 1

    workdir = tmp_path / "workdir"
    workdir.mkdir()
    run_path = workdir / "run.json"
    original = {
        "game_id": "bp35-0a0ad940",
        "provider": "codex",
        "unknown": {"preserve": [1, 2, 3]},
        "driver": {
            "cli_version": "codex-cli 0.144.1",
            "max_turns": 80,
            "turn_token_cap": 123,
            "other": "unchanged",
        },
    }
    run_path.write_text(json.dumps(original, indent=2), encoding="utf-8")

    sweep.reconcile_budget(workdir, 120)

    updated = json.loads(run_path.read_text(encoding="utf-8"))
    expected = json.loads(json.dumps(original))
    expected["driver"]["max_turns"] = 120
    expected["driver"]["turn_token_cap"] = sweep.TURN_TOKEN_CAP
    assert updated == expected
    assert run_path.read_text(encoding="utf-8") == json.dumps(expected, indent=2)
    assert len(fsynced) == 2


def test_pretty_atomic_json_rejects_nan_without_replacing_previous_file(tmp_path):
    path = tmp_path / "payload.json"
    previous = b'{\n  "previous": true\n}'
    path.write_bytes(previous)

    with pytest.raises(ValueError, match="Out of range float values"):
        persistence.atomic_json(path, {"not_finite": float("nan")}, pretty=True)

    assert path.read_bytes() == previous


def test_reconcile_budget_replace_failure_preserves_run_metadata(
    tmp_path, monkeypatch
):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    run_path = workdir / "run.json"
    original = {
        "game_id": "bp35-0a0ad940",
        "unknown": {"preserve": True},
        "driver": {
            "max_turns": 80,
            "turn_token_cap": 123,
            "other": "unchanged",
        },
    }
    previous = json.dumps(original, indent=2).encode("utf-8")
    run_path.write_bytes(previous)

    def fail_replace(_source, _destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        sweep.reconcile_budget(workdir, 120)

    assert run_path.read_bytes() == previous
    assert json.loads(run_path.read_text(encoding="utf-8")) == original
