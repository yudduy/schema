from __future__ import annotations

import hashlib
import json
import subprocess
import sys

import pytest

import schema_harness.runner as runner_module
import spikes.driver_probe as driver_probe
from schema_harness.gateway import ExecutionResult, GatewaySnapshot
from schema_harness.runner import (
    CommittedTurn,
    build_mid_session_message,
    build_session_start_message,
    build_turn_message,
    initialize_workdir,
    load_driver_session,
    load_previous_committed_turn,
    parse_args,
    write_mcp_config,
)


def _snapshot(*, history_len=0):
    return GatewaySnapshot(
        game_id="bp35-0a0ad940",
        grid=[[0, 1], [10, 15]],
        level=1 if history_len else 0,
        state="NOT_FINISHED",
        win_levels=9,
        legal=[3, 4, 6, 7],
        history_len=history_len,
    )


def test_session_start_message_matches_contract_section_3(tmp_path):
    message = build_session_start_message(
        _snapshot(),
        tmp_path,
        "# Notes\nprobe",
    )
    assert message == (
        "State: NOT_FINISHED | level 0/9\n"
        "Legal actions: [3, 4, 6, 7]  (action 6 is a click: also give x,y in 0..63)\n"
        "World model: NONE yet; history: 0 transitions.\n"
        f"Files: workdir (read/write) = {tmp_path}; framework source (read-only) = "
        f"{tmp_path}/framework.\n\n"
        "Your notes (notes.md — maintain it with write_file/edit_file; keep it concise):\n"
        "# Notes\nprobe\n\n"
        "Current grid:\n"
        "shape=2x2 (values 0-15 as hex)\n"
        "01\naf\n\n"
        "Decide the next action(s). Update your world model / notes, run a backtest or BFS "
        "as needed, then end by calling commit_actions."
    )


def test_mid_session_message_matches_contract_and_does_not_reinject_notes_or_files():
    previous = CommittedTurn(
        plan=[[6, 39, 33], [3, None, None], [6, 33, 33], [6, 33, 33]],
        reason="retry probe",
        result=ExecutionResult(
            committed=4,
            executed=1,
            halt_reason="dead",
            start_level=1,
            end_level=1,
            start_state="NOT_FINISHED",
            end_state="GAME_OVER",
        ),
    )
    message = build_mid_session_message(
        _snapshot(history_len=26),
        previous,
        has_world_model=True,
        model_filename="world_model_v5.py",
    )
    assert message == (
        "Result of your last commit: committed 4 action(s) "
        "[6@39,33 3 6@33,33 6@33,33] — executed 1; stopped because you DIED "
        "(game over) — RESET to retry the level. Net: level 1→1, state "
        'NOT_FINISHED→GAME_OVER. Your stated intent was: "retry probe"\n'
        "State: NOT_FINISHED | level 1/9\n"
        "Legal actions: [3, 4, 6, 7]  (action 6 is a click: also give x,y in 0..63)\n"
        "World model: installed; history: 26 transitions.\n\n"
        "Current grid:\n"
        "shape=2x2 (values 0-15 as hex)\n"
        "01\naf\n\n"
        "Decide the next action(s) (update model/notes, backtest or BFS as needed), then "
        "end by calling commit_actions. If your memory of a rule/layout is fuzzy after a "
        "long session, re-read notes.md / world_model_v5.py / read_history before deciding."
    )
    assert "Files:" not in message
    assert "Your notes" not in message


def test_mcp_config_is_strictly_one_locus_server_with_turn_environment(tmp_path):
    path = write_mcp_config(
        tmp_path,
        game="bp35-0a0ad940",
        turn=7,
        turn_id="turn-000007",
        max_actions=3000,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert list(payload["mcpServers"]) == ["locus"]
    server = payload["mcpServers"]["locus"]
    assert server["command"] == sys.executable
    assert server["args"] == ["-m", "schema_harness.locus"]
    assert server["env"]["LOCUS_WORKDIR"] == str(tmp_path)
    assert server["env"]["LOCUS_TURN_ID"] == "turn-000007"
    assert server["env"]["LOCUS_TURN"] == "7"
    assert server["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == ""


def test_fresh_rollover_session_reinjects_notes_and_file_map(tmp_path):
    previous = CommittedTurn(
        plan=[[6, 39, 33]],
        reason="probe",
        result=ExecutionResult(
            committed=1,
            executed=1,
            halt_reason="completed",
            start_level=0,
            end_level=0,
            start_state="NOT_FINISHED",
            end_state="NOT_FINISHED",
        ),
    )

    message = build_turn_message(
        _snapshot(history_len=1),
        tmp_path,
        notes="# Notes\ncurrent theory",
        previous=previous,
        has_world_model=False,
        session_start=True,
    )

    assert "Files: workdir" in message
    assert "Your notes" in message
    assert "current theory" in message
    assert "Result of your last commit" in message


def test_context_rollover_uses_iteration_occupancy_not_aggregate_usage():
    usage = {
        "input_tokens": 22,
        "cache_creation_input_tokens": 56_773,
        "cache_read_input_tokens": 619_047,
        "output_tokens": 22_906,
        "iterations": [
            {
                "input_tokens": 2,
                "cache_creation_input_tokens": 488,
                "cache_read_input_tokens": 77_517,
                "output_tokens": 534,
            }
        ],
    }

    assert runner_module._usage_tokens(usage) == 78_541


def test_short_public_game_id_is_canonicalized_for_scoring(tmp_path):
    args = parse_args(["--dry-run", "--game", "r11l", "--workdir", str(tmp_path)])

    assert args.game == "r11l-495a7899"
    assert args.system_prompt_file == runner_module.DEFAULT_SYSTEM_PROMPT


def test_driver_appends_method_prompt_file(monkeypatch, tmp_path):
    prompt = tmp_path / "method.md"
    prompt.write_text("model, verify, plan, act", encoding="utf-8")
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout='{"session_id":"s"}', stderr="")

    monkeypatch.setattr(driver_probe.subprocess, "run", fake_run)
    result = driver_probe.run_turn(
        "turn",
        session_id="session",
        resume=False,
        cwd=tmp_path,
        config_dir=tmp_path,
        locus_log=tmp_path / "locus.jsonl",
        mcp_cfg=tmp_path / "mcp.json",
        model="test-model",
        token=None,
        system_prompt_file=prompt,
    )

    flag = observed["command"].index("--append-system-prompt-file")
    assert observed["command"][flag + 1] == str(prompt)
    assert result == {"session_id": "s"}


def test_workdir_snapshots_prompt_identity_and_rejects_method_drift(tmp_path):
    prompt = tmp_path / "physicist.md"
    prompt.write_text("model, verify, plan, act\n", encoding="utf-8")
    workdir = tmp_path / "run"

    initialize_workdir(
        workdir,
        game="bp35-0a0ad940",
        provider="claude",
        model="test-model",
        max_actions=10,
        system_prompt_file=prompt,
    )

    metadata = json.loads((workdir / "run.json").read_text(encoding="utf-8"))
    assert metadata["system_prompt"] == "method_prompt.md"
    assert metadata["system_prompt_sha256"] == hashlib.sha256(prompt.read_bytes()).hexdigest()
    assert (workdir / "method_prompt.md").read_bytes() == prompt.read_bytes()
    assert (workdir / "method_prompt.md").stat().st_mode & 0o222 == 0

    # The live loop anchors this digest in memory and checks it before every subprocess.
    assert runner_module._verified_method_prompt(
        workdir, metadata["system_prompt_sha256"]
    ) == workdir / "method_prompt.md"
    (workdir / "method_prompt.md").chmod(0o644)
    (workdir / "method_prompt.md").write_text("mutated mid-run\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="changed during the run"):
        runner_module._verified_method_prompt(workdir, metadata["system_prompt_sha256"])

    (workdir / "method_prompt.md").write_bytes(prompt.read_bytes())

    prompt.write_text("a different method\n", encoding="utf-8")
    with pytest.raises(ValueError, match="different system prompt"):
        initialize_workdir(
            workdir,
            game="bp35-0a0ad940",
            provider="claude",
            model="test-model",
            max_actions=10,
            system_prompt_file=prompt,
        )


def test_process_restart_recovers_session_and_last_commit(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "turn_ledger.json").write_text(
        json.dumps(
            {
                "turns": {
                    "turn-000002": {
                        "actions": [[6, 15, 3]],
                        "reason": "test a candidate object",
                        "phase": "COMPLETE",
                        "turn": 2,
                        "result": {
                            "committed": 1,
                            "executed": 1,
                            "halt_reason": "mispredicted",
                            "start_level": 0,
                            "end_level": 0,
                            "start_state": "NOT_FINISHED",
                            "end_state": "NOT_FINISHED",
                            "surprise": "grid mismatch",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "sessions.json").write_text(
        json.dumps({"cwd": str(tmp_path.resolve()), "sid": "session-2", "resume": True}),
        encoding="utf-8",
    )

    previous = load_previous_committed_turn(tmp_path, 3)
    assert previous is not None
    assert previous.plan == [[6, 15, 3]]
    assert previous.result.surprise == "grid mismatch"
    assert load_driver_session(tmp_path) == ("session-2", True)

    message = build_turn_message(
        _snapshot(history_len=1),
        tmp_path,
        notes="# Notes\ncurrent",
        previous=previous,
        has_world_model=True,
        session_start=False,
    )
    assert "Result of your last commit" in message
    assert "stopped because mispredicted" in message


def test_live_runner_stops_after_first_driver_error(monkeypatch, tmp_path):
    snapshot = _snapshot()
    calls = 0

    def fake_initialize(*_args, **_kwargs):
        (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")
        return tmp_path, snapshot

    def fake_run_turn(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "session_id": "failed-session",
            "usage": {},
            "total_cost_usd": 0.0,
            "num_turns": 1,
            "is_error": True,
            "result": "authentication failed",
        }

    monkeypatch.setattr(runner_module, "initialize_workdir", fake_initialize)
    monkeypatch.setattr(runner_module, "load_snapshot", lambda _workdir: snapshot)
    monkeypatch.setattr(runner_module, "oauth_token", lambda: "test-token")
    monkeypatch.setattr(runner_module, "run_claude_turn", fake_run_turn)
    args = parse_args(
        [
            "--game",
            "bp35",
            "--workdir",
            str(tmp_path),
            "--max-turns",
            "5",
            "--no-system-prompt",
        ]
    )

    assert runner_module.run_live(args) == 1
    assert calls == 1
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [event["kind"] for event in events].count("turn_started") == 1
    assert events[-1]["kind"] == "run_finished"
