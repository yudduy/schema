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


def _claude_stream(*, tools, calls=(), session_id="session", include_result=True):
    records = [
        {
            "type": "system",
            "subtype": "init",
            "session_id": session_id,
            "tools": list(tools),
        }
    ]
    records.extend(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": f"call-{index}", "name": name, "input": {}}
                ]
            },
        }
        for index, name in enumerate(calls)
    )
    if include_result:
        records.append(
            {
                "type": "result",
                "session_id": session_id,
                "usage": {},
                "total_cost_usd": 0.0,
                "num_turns": 1,
                "is_error": False,
                "result": "done",
            }
        )
    return "\n".join(json.dumps(record) for record in records)


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
    assert server["alwaysLoad"] is True
    assert server["env"]["LOCUS_WORKDIR"] == str(tmp_path)
    assert server["env"]["LOCUS_TURN_ID"] == "turn-000007"
    assert server["env"]["LOCUS_TURN"] == "7"
    assert server["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert server["env"]["SCHEMA_EXPERIMENTAL_TOOLING"] == "false"


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


def test_default_system_prompt_is_v9_matched_transfer():
    assert runner_module.DEFAULT_SYSTEM_PROMPT.name == "physicist_v9_matched_transfer.md"
    assert runner_module.DEFAULT_SYSTEM_PROMPT.is_file()


def test_short_public_game_id_is_canonicalized_for_scoring(tmp_path):
    args = parse_args(["--dry-run", "--game", "r11l", "--workdir", str(tmp_path)])

    assert args.game == "r11l-495a7899"
    assert args.system_prompt_file == runner_module.DEFAULT_SYSTEM_PROMPT


def test_driver_appends_method_prompt_file(monkeypatch, tmp_path):
    prompt = tmp_path / "method.md"
    prompt.write_text("model, verify, plan, act", encoding="utf-8")
    observed = {}
    allowed_tools = ("mcp__locus__commit_actions",)

    def fake_run(command, **kwargs):
        observed["command"] = command
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=_claude_stream(tools=allowed_tools, session_id="session"),
            stderr="",
        )

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
        allowed_tools=allowed_tools,
    )

    flag = observed["command"].index("--append-system-prompt-file")
    assert observed["command"][flag + 1] == str(prompt)
    tools = observed["command"].index("--tools")
    assert observed["command"][tools + 1] == ""
    assert "--disable-slash-commands" in observed["command"]
    assert result["session_id"] == "session"
    assert result["is_error"] is False


def test_driver_loads_locus_tools_upfront_without_native_tool_search(
    monkeypatch, tmp_path
):
    observed = {}
    allowed_tools = ("mcp__locus__commit_actions",)

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["env"] = kwargs["env"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=_claude_stream(tools=allowed_tools, session_id="session"),
            stderr="",
        )

    monkeypatch.setattr(driver_probe.subprocess, "run", fake_run)
    driver_probe.run_turn(
        "turn",
        session_id="session",
        resume=False,
        cwd=tmp_path,
        config_dir=tmp_path,
        locus_log=tmp_path / "locus.jsonl",
        mcp_cfg=tmp_path / "mcp.json",
        model="test-model",
        token=None,
        allowed_tools=allowed_tools,
    )

    assert observed["env"]["ENABLE_TOOL_SEARCH"] == "false"
    denied = observed["command"].index("--disallowed-tools")
    assert observed["command"][denied + 1] == "Skill,ToolSearch,MCPSearch"
    assert "--verbose" in observed["command"]
    output = observed["command"].index("--output-format")
    assert observed["command"][output + 1] == "stream-json"


@pytest.mark.parametrize("name", runner_module.CLAUDE_LOCUS_TOOLS)
def test_claude_stream_accepts_each_exact_locus_tool(name):
    result = driver_probe.parse_stream(
        _claude_stream(tools=runner_module.CLAUDE_LOCUS_TOOLS, calls=(name,)),
        "",
        returncode=0,
        timed_out=False,
        expected_session_id="session",
        allowed_tools=runner_module.CLAUDE_LOCUS_TOOLS,
    )

    assert result["is_error"] is False
    assert "violations" not in result


@pytest.mark.parametrize(
    "name",
    (
        "Skill",
        "ToolSearch",
        "ScheduleWakeup",
        "Bash",
        "mcp__foreign__read_file",
        "mcp__locus__fabricated",
    ),
)
def test_claude_stream_rejects_non_locus_tool_calls(name):
    result = driver_probe.parse_stream(
        _claude_stream(tools=runner_module.CLAUDE_LOCUS_TOOLS, calls=(name,)),
        "",
        returncode=0,
        timed_out=False,
        expected_session_id="session",
        allowed_tools=runner_module.CLAUDE_LOCUS_TOOLS,
    )

    assert result["is_error"] is True
    assert result["violations"] == [f"unapproved Claude tool call: {name!r}"]


def test_claude_stream_rejects_advertised_native_tool_without_call():
    result = driver_probe.parse_stream(
        _claude_stream(tools=(*runner_module.CLAUDE_LOCUS_TOOLS, "ToolSearch")),
        "",
        returncode=0,
        timed_out=False,
        expected_session_id="session",
        allowed_tools=runner_module.CLAUDE_LOCUS_TOOLS,
    )

    assert result["is_error"] is True
    assert "unexpected=['ToolSearch']" in result["violations"][0]


def test_claude_stream_rejects_malformed_or_incomplete_output():
    result = driver_probe.parse_stream(
        '{"type":"system"}\nnot-json',
        "",
        returncode=0,
        timed_out=False,
        expected_session_id="session",
        allowed_tools=runner_module.CLAUDE_LOCUS_TOOLS,
    )

    assert result["is_error"] is True
    assert result["violations"] == [
        "malformed Claude stream record at line 2",
        "Claude stream has 0 init records; expected exactly 1",
        "Claude stream ended without a result record",
    ]


def test_claude_stream_rejects_native_activity_before_timeout():
    result = driver_probe.parse_stream(
        _claude_stream(
            tools=runner_module.CLAUDE_LOCUS_TOOLS,
            calls=("Skill",),
            include_result=False,
        ),
        "",
        returncode=-9,
        timed_out=True,
        expected_session_id="session",
        allowed_tools=runner_module.CLAUDE_LOCUS_TOOLS,
    )

    assert result["timed_out"] is True
    assert result["is_error"] is True
    assert result["violations"] == [
        "unapproved Claude tool call: 'Skill'",
        "Claude exited -9 without a result record",
    ]


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
    observed_tools = None

    def fake_initialize(*_args, **_kwargs):
        (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")
        return tmp_path, snapshot

    def fake_run_turn(*_args, **_kwargs):
        nonlocal calls, observed_tools
        calls += 1
        observed_tools = _kwargs["allowed_tools"]
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
            "--provider",
            "claude",
            "--game",
            "bp35",
            "--workdir",
            str(tmp_path),
            "--max-turns",
            "5",
            "--no-system-prompt",
        ]
    )

    assert runner_module._run_live(args) == 1
    assert calls == 1
    assert observed_tools == runner_module.CLAUDE_LOCUS_TOOLS
    assert len(observed_tools) == 14
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [event["kind"] for event in events].count("turn_started") == 1
    assert events[-1]["kind"] == "run_finished"


def test_live_run_lock_rejects_overlap_and_releases(tmp_path):
    lock_path = tmp_path / "schema-live.lock"

    with runner_module._live_run_lock(lock_path):
        with pytest.raises(RuntimeError, match="another live Schema harness run is active"):
            with runner_module._live_run_lock(lock_path):
                pass

    with runner_module._live_run_lock(lock_path):
        pass


def test_default_live_provider_is_sol_max(tmp_path):
    args = parse_args(["--workdir", str(tmp_path), "--no-system-prompt"])

    assert args.provider == "codex"
    assert args.model == "gpt-5.6-sol"
    assert args.effort == "max"
    assert args.experimental_tooling is False


def test_experimental_tooling_requires_explicit_flag(tmp_path):
    args = parse_args(
        [
            "--workdir",
            str(tmp_path),
            "--no-system-prompt",
            "--experimental-tooling",
        ]
    )

    assert args.experimental_tooling is True


def test_claude_model_infers_provider_for_legacy_commands(tmp_path):
    args = parse_args(
        [
            "--model",
            "claude-opus-4-8",
            "--effort",
            "max",
            "--workdir",
            str(tmp_path),
            "--no-system-prompt",
        ]
    )

    assert args.provider == "claude"
    assert args.model == "claude-opus-4-8"
    assert args.effort == "max"


@pytest.mark.parametrize("value", [None, "TRUE", " true ", "1"])
def test_codex_requires_exact_reset_only_environment_before_initialization(
    monkeypatch, tmp_path, value
):
    if value is None:
        monkeypatch.delenv("ONLY_RESET_LEVELS", raising=False)
    else:
        monkeypatch.setenv("ONLY_RESET_LEVELS", value)
    monkeypatch.setattr(
        runner_module,
        "initialize_workdir",
        lambda *_a, **_k: pytest.fail("invalid reset mode must not initialize a run"),
    )
    args = parse_args(
        ["--workdir", str(tmp_path), "--no-system-prompt", "--max-turns", "1"]
    )

    with pytest.raises(RuntimeError, match="ONLY_RESET_LEVELS=true"):
        runner_module._run_live(args)


def test_invalid_codex_effort_does_not_initialize_workdir(monkeypatch, tmp_path):
    monkeypatch.setenv("ONLY_RESET_LEVELS", "true")
    monkeypatch.setattr(
        runner_module,
        "initialize_workdir",
        lambda *_a, **_k: pytest.fail("invalid effort must not initialize a run"),
    )
    args = parse_args(
        [
            "--workdir",
            str(tmp_path),
            "--effort",
            "hyper",
            "--no-system-prompt",
        ]
    )

    with pytest.raises(ValueError, match="reasoning effort"):
        runner_module._run_live(args)


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--turn-timeout", "0"), ("--context-rollover-tokens", "-1")],
)
def test_runtime_timeouts_and_rollover_thresholds_must_be_positive(
    tmp_path, flag, value
):
    with pytest.raises(SystemExit):
        parse_args(
            ["--workdir", str(tmp_path), "--no-system-prompt", flag, value]
        )


def test_codex_command_is_persistent_read_only_and_locus_only(monkeypatch, tmp_path):
    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/opt/bin/codex")
    monkeypatch.setenv("SCHEMA_ENVIRONMENTS_DIR", "/protected/environments")
    monkeypatch.setenv("ONLY_RESET_LEVELS", "true")
    monkeypatch.setenv("PYTHONPATH", "/supplemental/one:/supplemental/two")
    command = runner_module._codex_command(
        driver_cwd=tmp_path / "driver",
        workdir=tmp_path,
        game="bp35-0a0ad940",
        turn=3,
        turn_id="turn-000003",
        max_actions=20,
        model="gpt-5.6-luna",
        effort="max",
        method_prompt="method",
        model_catalog=tmp_path / "catalog.json",
    )

    assert command[:2] == ["/opt/bin/codex", "exec"]
    assert "--ephemeral" not in command
    assert command[command.index("-C") + 1] == str(tmp_path / "driver")
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert command[-1] == "-"
    overrides = {
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "-c"
    }
    assert 'sandbox_mode="read-only"' in overrides
    assert 'web_search="disabled"' in overrides
    assert 'approval_policy="never"' in overrides
    assert f'model_catalog_json="{tmp_path / "catalog.json"}"' in overrides
    assert "project_doc_max_bytes=0" in overrides
    assert "include_permissions_instructions=false" in overrides
    assert "include_apps_instructions=false" in overrides
    assert "skills.include_instructions=false" in overrides
    assert 'mcp_servers.locus.default_tools_approval_mode="approve"' in overrides
    assert "mcp_servers.locus.tool_timeout_sec=1200" in overrides
    assert (
        'mcp_servers.locus.env.SCHEMA_ENVIRONMENTS_DIR="/protected/environments"'
        in overrides
    )
    assert 'mcp_servers.locus.env.ONLY_RESET_LEVELS="true"' in overrides
    assert (
        'mcp_servers.locus.env.SCHEMA_EXPERIMENTAL_TOOLING="false"'
        in overrides
    )
    assert (
        "mcp_servers.locus.env.PYTHONPATH="
        + json.dumps(
            f"{runner_module.REPO_ROOT}{runner_module.os.pathsep}"
            "/supplemental/one:/supplemental/two"
        )
    ) in overrides
    assert (
        "mcp_servers.locus.enabled_tools="
        + json.dumps(list(runner_module.CODEX_LOCUS_TOOLS), separators=(",", ":"))
    ) in overrides
    assert not any("view_image" in value for value in command)
    disabled = tuple(
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--disable"
    )
    assert disabled == runner_module._CODEX_DISABLED_FEATURES
    assert "code_mode_host" in disabled
    developer = next(value for value in overrides if value.startswith("developer_instructions="))
    instructions = json.loads(developer.split("=", 1)[1])
    assert instructions.startswith("method\n")
    assert runner_module.CODEX_DRIVER_POLICY in instructions

    child_environment = runner_module._codex_environment(tmp_path / "codex-home")
    assert "SCHEMA_ENVIRONMENTS_DIR" not in child_environment
    assert "ONLY_RESET_LEVELS" not in child_environment


def test_codex_resume_command_preserves_strict_boundary(monkeypatch, tmp_path):
    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/opt/bin/codex")
    monkeypatch.setenv("ONLY_RESET_LEVELS", "true")

    command = runner_module._codex_command(
        driver_cwd=tmp_path / "driver",
        workdir=tmp_path,
        game="bp35-0a0ad940",
        turn=4,
        turn_id="turn-000004",
        max_actions=20,
        model="gpt-5.6-luna",
        effort="max",
        method_prompt="method",
        model_catalog=tmp_path / "catalog.json",
        session_id="thread-1",
        resume=True,
    )

    assert command[:3] == ["/opt/bin/codex", "exec", "resume"]
    assert "-C" not in command
    assert "--color" not in command
    assert "--strict-config" in command
    assert "--ignore-user-config" in command
    assert command[-2:] == ["thread-1", "-"]
    disabled = tuple(
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--disable"
    )
    assert disabled == runner_module._CODEX_DISABLED_FEATURES


def test_codex_jsonl_parser_accepts_only_locus_calls():
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "id": "item-0",
                        "type": "mcp_tool_call",
                        "server_name": "locus",
                        "tool_name": "read_history",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.updated",
                    "item": {
                        "id": "item-0",
                        "type": "mcp_tool_call",
                        "server_name": "locus",
                        "tool_name": "read_history",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-0",
                        "type": "mcp_tool_call",
                        "server_name": "locus",
                        "tool_name": "read_history",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-1",
                        "type": "agent_message",
                        "text": "done",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 12, "output_tokens": 3},
                }
            ),
        ]
    )

    result = runner_module._parse_codex_jsonl(stdout, "", returncode=0)

    assert result["session_id"] == "thread-1"
    assert result["result"] == "done"
    assert result["usage"]["cost_available"] is False
    assert result["total_cost_usd"] == 0.0
    assert result["is_error"] is False


def test_codex_jsonl_parser_rejects_resume_session_fork():
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-other"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
    )

    result = runner_module._parse_codex_jsonl(
        stdout, "", returncode=0, expected_session_id="thread-trusted"
    )

    assert result["is_error"] is True
    assert result["violations"] == [
        "Codex resumed a different session: expected 'thread-trusted', got 'thread-other'"
    ]


@pytest.mark.parametrize("item_type", ["command_execution", "file_change", "view_image"])
def test_codex_jsonl_parser_rejects_native_tools(item_type):
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {"type": "item.completed", "item": {"type": item_type}}
            ),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
    )

    result = runner_module._parse_codex_jsonl(stdout, "", returncode=0)

    assert result["is_error"] is True
    assert result["violations"]


def test_codex_jsonl_parser_rejects_unknown_records_and_redacts_stderr():
    secret = "/protected/environments/secret-token"
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "native.future_tool", "payload": secret}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
    )

    result = runner_module._parse_codex_jsonl(stdout, secret, returncode=0)

    assert result["is_error"] is True
    assert result["violations"] == ["unknown Codex record: 'native.future_tool'"]
    assert secret not in result["result"]
    assert "private driver logs" in result["result"]


def test_codex_jsonl_parser_rejects_unhashable_record_type():
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": ["native"]}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
    )

    result = runner_module._parse_codex_jsonl(stdout, "", returncode=0)

    assert result["is_error"] is True
    assert result["violations"] == ["unknown Codex record: ['native']"]


def test_codex_jsonl_parser_rejects_incomplete_stream_after_lag_error():
    warning = "in-process app-server event stream lagged; dropped 3 events"
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item-0", "type": "error", "message": warning},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-1",
                        "type": "agent_message",
                        "text": "done",
                    },
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
    )

    result = runner_module._parse_codex_jsonl(stdout, "", returncode=0)

    assert result["is_error"] is True
    assert result["violations"] == ["native or unknown Codex item: error"]


def test_codex_jsonl_parser_rejects_stream_lag_reported_on_stderr():
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
    )

    result = runner_module._parse_codex_jsonl(
        stdout,
        "in-process app-server event stream lagged; dropped 3 events",
        returncode=0,
    )

    assert result["is_error"] is True
    assert result["violations"] == ["Codex event stream dropped records"]


def test_codex_jsonl_parser_accepts_context_compaction_item():
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item-0", "type": "context_compaction"},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
    )

    result = runner_module._parse_codex_jsonl(stdout, "", returncode=0)

    assert result["is_error"] is False
    assert result["violations"] == []


def test_codex_jsonl_parser_accepts_completion_only_reasoning_summary():
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-0",
                        "type": "reasoning",
                        "text": "summary",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-1",
                        "type": "agent_message",
                        "text": "done",
                    },
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
    )

    result = runner_module._parse_codex_jsonl(stdout, "", returncode=0)

    assert result["is_error"] is False
    assert result["violations"] == []


@pytest.mark.parametrize("value", ["oops", -1, 1.5, True])
def test_codex_jsonl_parser_rejects_invalid_token_usage(value):
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": value},
                }
            ),
        ]
    )

    result = runner_module._parse_codex_jsonl(stdout, "", returncode=0)

    assert result["is_error"] is True
    assert result["violations"] == ["Codex emitted invalid input_tokens usage"]
    assert result["usage"] == {"cost_available": False}


@pytest.mark.parametrize(
    "item_records, expected",
    [
        (
            [
                {
                    "type": "item.started",
                    "item": {
                        "id": "item-0",
                        "type": "mcp_tool_call",
                        "server": "locus",
                        "tool": "read_history",
                    },
                }
            ],
            "Codex item 'item-0' has 0 completion records; expected exactly 1",
        ),
        (
            [
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-0",
                        "type": "mcp_tool_call",
                        "server": "locus",
                        "tool": "read_history",
                    },
                }
            ],
            "Codex MCP item 'item-0' has 0 start records; expected exactly 1",
        ),
        (
            [
                {
                    "type": "item.started",
                    "item": {
                        "id": "item-0",
                        "type": "mcp_tool_call",
                        "server": "locus",
                        "tool": "read_history",
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-0",
                        "type": "agent_message",
                        "text": "done",
                    },
                },
            ],
            "Codex item 'item-0' changed type",
        ),
    ],
)
def test_codex_jsonl_parser_rejects_incomplete_item_lifecycle(
    item_records, expected
):
    records = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        *item_records,
        {"type": "turn.completed", "usage": {}},
    ]

    result = runner_module._parse_codex_jsonl(
        "\n".join(json.dumps(record) for record in records), "", returncode=0
    )

    assert result["is_error"] is True
    assert expected in result["violations"]


@pytest.mark.parametrize(
    "records, expected",
    [
        (
            [
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.completed", "usage": {}},
            ],
            "Codex stream has 0 turn.started records; expected exactly 1",
        ),
        (
            [
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.started"},
                {"type": "turn.started"},
                {"type": "turn.completed", "usage": {}},
            ],
            "Codex stream has 2 turn.started records; expected exactly 1",
        ),
        (
            [
                {"type": "turn.started"},
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.completed", "usage": {}},
            ],
            "Codex lifecycle records are out of order",
        ),
    ],
)
def test_codex_jsonl_parser_requires_exact_ordered_lifecycle(records, expected):
    stdout = "\n".join(json.dumps(record) for record in records)

    result = runner_module._parse_codex_jsonl(stdout, "", returncode=0)

    assert result["is_error"] is True
    assert expected in result["violations"]


def test_codex_timeout_recovers_thread_id_from_partial_jsonl():
    stdout = json.dumps({"type": "thread.started", "thread_id": "thread-1"}) + "\n{"

    result = runner_module._parse_codex_jsonl(
        stdout, "", returncode=-9, timed_out=True
    )

    assert result["session_id"] == "thread-1"
    assert result["timed_out"] is True
    assert result["is_error"] is True
    assert result["violations"] == [
        "Codex turn timed out before complete stream audit"
    ]


def test_workdir_rejects_provider_model_and_action_cap_drift(tmp_path):
    workdir = tmp_path / "run"
    runner_module.initialize_workdir(
        workdir,
        game="bp35-0a0ad940",
        provider="claude",
        model="claude-test",
        max_actions=10,
        system_prompt_file=None,
    )

    with pytest.raises(ValueError, match="provider"):
        runner_module.initialize_workdir(
            workdir,
            game="bp35-0a0ad940",
            provider="codex",
            model="gpt-5.6-luna",
            max_actions=10,
            effort="max",
            system_prompt_file=None,
        )
    with pytest.raises(ValueError, match="model"):
        runner_module.initialize_workdir(
            workdir,
            game="bp35-0a0ad940",
            provider="claude",
            model="different",
            max_actions=10,
            system_prompt_file=None,
        )
    with pytest.raises(ValueError, match="max_actions"):
        runner_module.initialize_workdir(
            workdir,
            game="bp35-0a0ad940",
            provider="claude",
            model="claude-test",
            max_actions=11,
            system_prompt_file=None,
        )
    with pytest.raises(ValueError, match="experimental-tooling"):
        runner_module.initialize_workdir(
            workdir,
            game="bp35-0a0ad940",
            provider="claude",
            model="claude-test",
            max_actions=10,
            system_prompt_file=None,
            experimental_tooling=True,
        )


def test_session_checkpoint_is_provider_and_model_bound(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "sessions.json").write_text(
        json.dumps(
            {
                "cwd": str(tmp_path.resolve()),
                "provider": "codex",
                "model": "gpt-5.6-luna",
                "sid": "thread-1",
                "resume": False,
            }
        ),
        encoding="utf-8",
    )

    assert load_driver_session(
        tmp_path, provider="codex", model="gpt-5.6-luna"
    ) == ("thread-1", False)
    assert load_driver_session(tmp_path, provider="claude", model="gpt-5.6-luna") is None
    assert load_driver_session(tmp_path, provider="codex", model="gpt-5.6-sol") is None


def test_codex_home_is_private_and_copies_only_subscription_auth(monkeypatch, tmp_path):
    fake_home = tmp_path / "user"
    auth = fake_home / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {"access_token": "subscription-token"},
            }
        ),
        encoding="utf-8",
    )
    auth.chmod(0o600)
    homes = tmp_path / "isolated"
    monkeypatch.setattr(runner_module.Path, "home", lambda: fake_home)
    monkeypatch.setattr(runner_module.tempfile, "gettempdir", lambda: str(homes))

    codex_home = runner_module._prepare_codex_home(tmp_path / "trajectory")

    assert codex_home.parent == homes
    assert codex_home.stat().st_mode & 0o077 == 0
    isolated_auth = codex_home / "auth.json"
    assert isolated_auth.is_file()
    assert not isolated_auth.is_symlink()
    assert isolated_auth.stat().st_mode & 0o077 == 0
    assert isolated_auth.read_bytes() == auth.read_bytes()


def test_codex_home_preserves_valid_isolated_auth_refresh(monkeypatch, tmp_path):
    fake_home = tmp_path / "user"
    auth = fake_home / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    original = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {"access_token": "original-token"},
    }
    auth.write_text(json.dumps(original), encoding="utf-8")
    auth.chmod(0o600)
    homes = tmp_path / "isolated"
    monkeypatch.setattr(runner_module.Path, "home", lambda: fake_home)
    monkeypatch.setattr(runner_module.tempfile, "gettempdir", lambda: str(homes))
    codex_home = runner_module._prepare_codex_home(tmp_path / "trajectory")
    isolated_auth = codex_home / "auth.json"
    refreshed = {
        **original,
        "tokens": {"access_token": "refreshed-token"},
    }
    isolated_auth.write_text(json.dumps(refreshed), encoding="utf-8")
    isolated_auth.chmod(0o600)

    assert runner_module._prepare_codex_home(tmp_path / "trajectory") == codex_home
    assert json.loads(isolated_auth.read_text(encoding="utf-8")) == refreshed

    # Once isolated, unrelated host-auth changes cannot replace the trajectory's
    # valid refreshable subscription credential.
    auth.write_text(
        json.dumps(
            {"auth_mode": "apikey", "OPENAI_API_KEY": "key", "tokens": None}
        ),
        encoding="utf-8",
    )
    assert runner_module._prepare_codex_home(tmp_path / "trajectory") == codex_home
    assert json.loads(isolated_auth.read_text(encoding="utf-8")) == refreshed


@pytest.mark.parametrize(
    "payload",
    [
        {"auth_mode": "apikey", "OPENAI_API_KEY": "key", "tokens": None},
        {"auth_mode": "chatgpt", "OPENAI_API_KEY": "key", "tokens": {}},
        {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": {}},
    ],
)
def test_codex_home_rejects_non_subscription_auth(monkeypatch, tmp_path, payload):
    fake_home = tmp_path / "user"
    auth = fake_home / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps(payload), encoding="utf-8")
    auth.chmod(0o600)
    monkeypatch.setattr(runner_module.Path, "home", lambda: fake_home)

    with pytest.raises(RuntimeError, match="subscription authentication"):
        runner_module._prepare_codex_home(tmp_path / "trajectory")


def test_codex_session_availability_requires_local_rollout_file(tmp_path):
    home = tmp_path / "codex-home"
    rollout = home / "sessions" / "2026" / "07" / "18"
    rollout.mkdir(parents=True)
    session_id = "019f-session"

    assert runner_module._codex_session_available(home, session_id) is False
    candidate = rollout / "rollout-different-file-id.jsonl"
    candidate.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "different-session"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert runner_module._codex_session_available(home, session_id) is False
    candidate.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": session_id},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert runner_module._codex_session_available(home, session_id) is True


def test_codex_catalog_pins_text_only_luna_metadata(monkeypatch, tmp_path):
    catalog = {
        "models": [
            {
                "slug": "gpt-5.6-luna",
                "input_modalities": ["text", "image"],
                "supports_image_detail_original": True,
                "multi_agent_version": "v1",
                "tool_mode": "code_mode_only",
                "experimental_supported_tools": ["native"],
                "supported_reasoning_levels": [{"effort": "max"}],
            }
        ]
    }

    def fake_run(command, **_kwargs):
        if command[-1] == "--version":
            return subprocess.CompletedProcess(
                command, 0, f"{runner_module.VALIDATED_CODEX_CLI_VERSION}\n", ""
            )
        return subprocess.CompletedProcess(command, 0, json.dumps(catalog), "")

    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/opt/bin/codex")
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    path, digest, version = runner_module._prepare_codex_catalog(
        tmp_path,
        codex_home=tmp_path / "home",
        model="gpt-5.6-luna",
        effort="max",
    )

    selected = json.loads(path.read_text(encoding="utf-8"))["models"][0]
    assert selected["input_modalities"] == ["text"]
    assert selected["supports_image_detail_original"] is False
    assert selected["multi_agent_version"] is None
    assert selected["tool_mode"] is None
    assert selected["experimental_supported_tools"] == []
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert version == runner_module.VALIDATED_CODEX_CLI_VERSION
    assert path.stat().st_mode & 0o222 == 0

    with pytest.raises(RuntimeError, match="does not support"):
        runner_module._prepare_codex_catalog(
            tmp_path,
            codex_home=tmp_path / "home",
            model="gpt-5.6-luna",
            effort="ultra",
        )


def test_ultra_effort_accepted_when_catalog_supports_it(monkeypatch, tmp_path):
    catalog = {
        "models": [
            {
                "slug": "gpt-5.6-sol",
                "input_modalities": ["text", "image"],
                "supports_image_detail_original": True,
                "multi_agent_version": "v1",
                "tool_mode": "code_mode_only",
                "experimental_supported_tools": ["native"],
                "supported_reasoning_levels": [
                    {"effort": "max"},
                    {"effort": "ultra"},
                ],
            }
        ]
    }

    def fake_run(command, **_kwargs):
        if command[-1] == "--version":
            return subprocess.CompletedProcess(
                command, 0, f"{runner_module.VALIDATED_CODEX_CLI_VERSION}\n", ""
            )
        return subprocess.CompletedProcess(command, 0, json.dumps(catalog), "")

    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/opt/bin/codex")
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    path, _digest, _version = runner_module._prepare_codex_catalog(
        tmp_path,
        codex_home=tmp_path / "home",
        model="gpt-5.6-sol",
        effort="ultra",
    )

    selected = json.loads(path.read_text(encoding="utf-8"))["models"][0]
    assert selected["multi_agent_version"] is None


def test_codex_catalog_replaces_fresh_preseed_and_detects_resume_tamper(
    monkeypatch, tmp_path
):
    bundled = {
        "models": [
            {
                "slug": "gpt-5.6-luna",
                "base_instructions": "bundled",
                "input_modalities": ["text", "image"],
                "supports_image_detail_original": True,
                "multi_agent_version": "v1",
                "tool_mode": "code_mode_only",
                "experimental_supported_tools": ["native"],
                "supported_reasoning_levels": [{"effort": "max"}],
            }
        ]
    }

    def fake_run(command, **_kwargs):
        output = (
            runner_module.VALIDATED_CODEX_CLI_VERSION
            if command[-1] == "--version"
            else json.dumps(bundled)
        )
        return subprocess.CompletedProcess(command, 0, output, "")

    catalog_path = tmp_path / "config" / "codex" / "model-catalog.json"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-5.6-luna",
                        "base_instructions": "preseeded",
                        "input_modalities": ["text"],
                        "supports_image_detail_original": False,
                        "multi_agent_version": None,
                        "tool_mode": None,
                        "experimental_supported_tools": [],
                        "supported_reasoning_levels": [{"effort": "max"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/opt/bin/codex")
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)

    path, digest, _version = runner_module._prepare_codex_catalog(
        tmp_path,
        codex_home=tmp_path / "home",
        model="gpt-5.6-luna",
        effort="max",
    )
    selected = json.loads(path.read_text(encoding="utf-8"))["models"][0]
    assert selected["base_instructions"] == "bundled"

    (tmp_path / "run.json").write_text(
        json.dumps(
            {
                "driver": {
                    "cli_version": runner_module.VALIDATED_CODEX_CLI_VERSION,
                    "model_catalog_sha256": digest,
                }
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o644)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="catalog changed"):
        runner_module._prepare_codex_catalog(
            tmp_path,
            codex_home=tmp_path / "home",
            model="gpt-5.6-luna",
            effort="max",
        )


def test_codex_catalog_rejects_unvalidated_cli_version(monkeypatch, tmp_path):
    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/opt/bin/codex")
    monkeypatch.setattr(
        runner_module.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, "codex-cli 0.145.0", ""
        ),
    )

    with pytest.raises(RuntimeError, match="outside the validated driver boundary"):
        runner_module._prepare_codex_catalog(
            tmp_path,
            codex_home=tmp_path / "home",
            model="gpt-5.6-luna",
            effort="max",
        )


def test_codex_metadata_records_and_freezes_security_boundary(tmp_path):
    (tmp_path / "run.json").write_text("{}", encoding="utf-8")
    arguments = {
        "catalog_digest": "catalog-digest",
        "cli_version": runner_module.VALIDATED_CODEX_CLI_VERSION,
        "max_turns": 120,
        "turn_timeout": 1200,
        "turn_token_cap": 1_000_000,
        "run_token_cap": 60_000_000,
        "no_progress_turns": 5,
        "only_reset_levels": "true",
        "model": "gpt-5.6-luna",
        "effort": "max",
        "experimental_tooling": False,
    }

    runner_module._record_codex_metadata(tmp_path, **arguments)

    driver = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))["driver"]
    assert driver["cli_version"] == runner_module.VALIDATED_CODEX_CLI_VERSION
    assert driver["ephemeral_turns"] is False
    assert driver["disabled_features"] == list(runner_module._CODEX_DISABLED_FEATURES)
    assert driver["enabled_locus_tools"] == list(runner_module.CODEX_LOCUS_TOOLS)
    assert driver["experimental_tooling"] is False
    assert len(driver["driver_policy_sha256"]) == 64
    assert len(driver["policy_config_sha256"]) == 64
    runner_module._record_codex_metadata(tmp_path, **arguments)

    with pytest.raises(ValueError, match="different Codex driver metadata"):
        runner_module._record_codex_metadata(
            tmp_path, **{**arguments, "turn_timeout": 1201}
        )


def test_codex_timeout_kills_the_driver_process_group(monkeypatch, tmp_path):
    class FakeProcess:
        pid = 4321
        returncode = -9

        def __init__(self):
            self.calls = 0

        def communicate(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("codex", 1)
            return (
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                "",
            )

    process = FakeProcess()
    killed = []
    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/opt/bin/codex")
    monkeypatch.setattr(runner_module.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(
        runner_module.os, "killpg", lambda pid, sig: killed.append((pid, sig))
    )
    monkeypatch.setattr(
        runner_module, "_prepare_codex_home", lambda _workdir: tmp_path / "home"
    )

    catalog = tmp_path / "catalog.json"
    catalog.write_text("{}", encoding="utf-8")
    catalog_digest = hashlib.sha256(catalog.read_bytes()).hexdigest()
    result = runner_module.run_codex_turn(
        "turn",
        workdir=tmp_path,
        codex_home=tmp_path / "home",
        model_catalog=catalog,
        catalog_digest=catalog_digest,
        game="bp35-0a0ad940",
        turn=1,
        turn_id="turn-000001",
        max_actions=10,
        model="gpt-5.6-luna",
        effort="max",
        session_id="",
        resume=False,
        timeout=1,
        system_prompt_file=None,
    )

    assert killed == [(4321, runner_module.signal.SIGKILL)]
    assert result["timed_out"] is True
    assert result["session_id"] == "thread-1"
    raw = tmp_path / "sessions" / "codex-turn-000001.jsonl"
    assert raw.is_file()
    assert raw.stat().st_mode & 0o077 == 0
    assert raw.parent.stat().st_mode & 0o077 == 0


def test_codex_catalog_digest_is_rechecked_before_process_start(monkeypatch, tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text("changed", encoding="utf-8")
    monkeypatch.setattr(
        runner_module.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("tampered catalog must fail before inference"),
    )

    with pytest.raises(RuntimeError, match="catalog changed during the run"):
        runner_module.run_codex_turn(
            "turn",
            workdir=tmp_path,
            codex_home=tmp_path / "home",
            model_catalog=catalog,
            catalog_digest=hashlib.sha256(b"original").hexdigest(),
            game="bp35-0a0ad940",
            turn=1,
            turn_id="turn-000001",
            max_actions=10,
            model="gpt-5.6-luna",
            effort="max",
            session_id="",
            resume=False,
            timeout=1,
            system_prompt_file=None,
        )


def test_codex_live_path_skips_claude_auth_and_enforces_token_cap(monkeypatch, tmp_path):
    snapshot = _snapshot()
    calls = 0
    monkeypatch.setenv("ONLY_RESET_LEVELS", "true")

    def fake_initialize(*_args, **_kwargs):
        (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")
        return tmp_path, snapshot

    def fake_codex_turn(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "session_id": f"thread-{calls}",
            "usage": {
                "input_tokens": 300_000,
                "output_tokens": 1,
                "cost_available": False,
            },
            "total_cost_usd": 0.0,
            "cost_available": False,
            "num_turns": 1,
            "is_error": False,
            "result": "no commit",
        }

    monkeypatch.setattr(runner_module, "initialize_workdir", fake_initialize)
    monkeypatch.setattr(runner_module, "load_snapshot", lambda _workdir: snapshot)
    monkeypatch.setattr(
        runner_module,
        "oauth_token",
        lambda: pytest.fail("Codex must not request Claude authentication"),
    )
    monkeypatch.setattr(
        runner_module, "_prepare_codex_home", lambda _workdir: tmp_path / "home"
    )
    monkeypatch.setattr(
        runner_module,
        "_prepare_codex_catalog",
        lambda *_a, **_k: (tmp_path / "catalog.json", "digest", "codex-cli test"),
    )
    monkeypatch.setattr(runner_module, "_record_codex_metadata", lambda *_a, **_k: None)
    monkeypatch.setattr(runner_module, "run_codex_turn", fake_codex_turn)
    args = parse_args(
        [
            "--game",
            "bp35",
            "--workdir",
            str(tmp_path),
            "--max-turns",
            "5",
            "--turn-token-cap",
            "300000",
            "--no-system-prompt",
        ]
    )

    assert runner_module._run_live(args) == 0
    assert calls == 1
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    started = next(event for event in events if event["kind"] == "run_started")
    assert started["provider"] == "codex"
    assert started["model"] == "gpt-5.6-sol"
    telemetry = next(event for event in events if event["kind"] == "turn_telemetry")
    assert telemetry["usage"]["cost_available"] is False


@pytest.mark.parametrize(
    ("cumulative_totals", "expected_calls"),
    [
        ([900_000, 1_050_000], 2),
        ([100_000, 1_200_001, 1_300_000], 2),
    ],
)
def test_codex_live_token_cap_uses_same_session_deltas(
    monkeypatch, tmp_path, cumulative_totals, expected_calls
):
    snapshot = _snapshot()
    calls = 0
    monkeypatch.setenv("ONLY_RESET_LEVELS", "true")

    def fake_initialize(*_args, **_kwargs):
        (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")
        return tmp_path, snapshot

    def fake_codex_turn(*_args, **_kwargs):
        nonlocal calls
        total = cumulative_totals[calls]
        calls += 1
        return {
            "session_id": "thread-stable",
            "usage": {
                "input_tokens": total,
                "output_tokens": 0,
                "cost_available": False,
            },
            "total_cost_usd": 0.0,
            "cost_available": False,
            "num_turns": 1,
            "is_error": False,
            "result": "no commit",
        }

    monkeypatch.setattr(runner_module, "initialize_workdir", fake_initialize)
    monkeypatch.setattr(runner_module, "load_snapshot", lambda _workdir: snapshot)
    monkeypatch.setattr(
        runner_module, "_prepare_codex_home", lambda _workdir: tmp_path / "home"
    )
    monkeypatch.setattr(
        runner_module,
        "_prepare_codex_catalog",
        lambda *_a, **_k: (tmp_path / "catalog.json", "digest", "codex-cli test"),
    )
    monkeypatch.setattr(runner_module, "_record_codex_metadata", lambda *_a, **_k: None)
    monkeypatch.setattr(runner_module, "run_codex_turn", fake_codex_turn)
    args = parse_args(
        [
            "--game",
            "bp35",
            "--workdir",
            str(tmp_path),
            "--max-turns",
            str(len(cumulative_totals)),
            "--turn-token-cap",
            "1000000",
            "--no-system-prompt",
        ]
    )

    assert runner_module._run_live(args) == 0
    assert calls == expected_calls


def test_codex_live_cumulative_usage_regression_invalidates_session(
    monkeypatch, tmp_path
):
    snapshot = _snapshot()
    totals = iter((100, 99))
    monkeypatch.setenv("ONLY_RESET_LEVELS", "true")

    def fake_initialize(*_args, **_kwargs):
        (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")
        return tmp_path, snapshot

    def fake_codex_turn(*_args, **_kwargs):
        return {
            "session_id": "thread-stable",
            "usage": {
                "input_tokens": next(totals),
                "output_tokens": 0,
                "cost_available": False,
            },
            "total_cost_usd": 0.0,
            "cost_available": False,
            "num_turns": 1,
            "is_error": False,
            "result": "no commit",
        }

    monkeypatch.setattr(runner_module, "initialize_workdir", fake_initialize)
    monkeypatch.setattr(runner_module, "load_snapshot", lambda _workdir: snapshot)
    monkeypatch.setattr(
        runner_module, "_prepare_codex_home", lambda _workdir: tmp_path / "home"
    )
    monkeypatch.setattr(
        runner_module,
        "_prepare_codex_catalog",
        lambda *_a, **_k: (tmp_path / "catalog.json", "digest", "codex-cli test"),
    )
    monkeypatch.setattr(runner_module, "_record_codex_metadata", lambda *_a, **_k: None)
    monkeypatch.setattr(runner_module, "run_codex_turn", fake_codex_turn)
    args = parse_args(
        [
            "--game",
            "bp35",
            "--workdir",
            str(tmp_path),
            "--max-turns",
            "2",
            "--no-system-prompt",
        ]
    )

    assert runner_module._run_live(args) == 1
    checkpoint = json.loads(
        (tmp_path / "sessions" / "sessions.json").read_text(encoding="utf-8")
    )
    assert checkpoint["invalidated"] is True
    records = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    telemetry = [record for record in records if record["kind"] == "turn_telemetry"]
    assert telemetry[-1]["is_error"] is True


def test_missing_codex_rollout_starts_fresh_session(monkeypatch, tmp_path):
    monkeypatch.setenv("ONLY_RESET_LEVELS", "true")
    snapshot = _snapshot()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "sessions.json").write_text(
        json.dumps(
            {
                "cwd": str(tmp_path.resolve()),
                "provider": "codex",
                "model": "gpt-5.6-luna",
                "sid": "thread-missing",
                "resume": True,
            }
        ),
        encoding="utf-8",
    )
    observed = []

    def fake_initialize(*_args, **_kwargs):
        (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")
        return tmp_path, snapshot

    def fake_codex_turn(*_args, **kwargs):
        observed.append((kwargs["session_id"], kwargs["resume"]))
        return {
            "session_id": "thread-fresh",
            "usage": {"cost_available": False},
            "total_cost_usd": 0.0,
            "cost_available": False,
            "num_turns": 1,
            "is_error": False,
            "result": "no commit",
        }

    monkeypatch.setattr(runner_module, "initialize_workdir", fake_initialize)
    monkeypatch.setattr(runner_module, "load_snapshot", lambda _workdir: snapshot)
    monkeypatch.setattr(runner_module, "_prepare_codex_home", lambda _workdir: codex_home)
    monkeypatch.setattr(
        runner_module,
        "_prepare_codex_catalog",
        lambda *_a, **_k: (tmp_path / "catalog.json", "digest", "codex-cli test"),
    )
    monkeypatch.setattr(runner_module, "_record_codex_metadata", lambda *_a, **_k: None)
    monkeypatch.setattr(runner_module, "run_codex_turn", fake_codex_turn)
    args = parse_args(
        [
            "--game",
            "bp35",
            "--workdir",
            str(tmp_path),
            "--max-turns",
            "1",
            "--no-system-prompt",
        ]
    )

    assert runner_module._run_live(args) == 0
    assert observed == [("", False)]
    checkpoint = json.loads((sessions / "sessions.json").read_text(encoding="utf-8"))
    assert checkpoint["sid"] == "thread-fresh"
    assert checkpoint["resume"] is True


def test_historical_driver_totals_recover_cost_tokens_and_turns(tmp_path):
    records = [
        {"kind": "turn_started", "turn": 1},
        {
            "kind": "turn_telemetry",
            "turn": 1,
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "total_cost_usd": 1.25,
        },
        {"kind": "turn_started", "turn": 2},
        {
            "kind": "turn_telemetry",
            "turn": 2,
            "usage": {
                "input_tokens": 20,
                "output_tokens": 3,
                "cost_available": False,
            },
            "total_cost_usd": 0.0,
        },
    ]
    (tmp_path / "events.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )

    costs, tokens, turns, session_usage = runner_module._historical_driver_totals(
        tmp_path
    )

    assert costs == [1.25]
    assert tokens == 35
    assert turns == 2
    assert session_usage == {}


def test_codex_cumulative_usage_recovers_ar25_turn_deltas(tmp_path):
    totals = [
        165_819,
        345_926,
        450_596,
        610_085,
        739_660,
        876_435,
        1_020_602,
    ]
    expected_deltas = [
        165_819,
        180_107,
        104_670,
        159_489,
        129_575,
        136_775,
        144_167,
    ]
    records = []
    for turn, total in enumerate(totals, start=1):
        records.extend(
            [
                {"kind": "turn_started", "turn": turn},
                {
                    "kind": "turn_telemetry",
                    "turn": turn,
                    "session_id": "thread-ar25",
                    "usage": {
                        "input_tokens": total,
                        "cached_input_tokens": 0,
                        "output_tokens": 0,
                        "cost_available": False,
                    },
                    "total_cost_usd": 0.0,
                },
            ]
        )
    (tmp_path / "events.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )

    costs, tokens, turns, session_usage = runner_module._historical_driver_totals(
        tmp_path
    )
    deltas = []
    previous = None
    for total in totals:
        delta, previous = runner_module._codex_usage_delta_tokens(
            {"input_tokens": total}, previous
        )
        deltas.append(delta)

    assert costs == []
    assert tokens == 1_020_602
    assert turns == 7
    assert deltas == expected_deltas
    assert max(deltas) == 180_107
    assert session_usage["thread-ar25"]["input_tokens"] == 1_020_602


def test_codex_cumulative_usage_resets_for_a_new_session(tmp_path):
    records = [
        {
            "kind": "turn_telemetry",
            "turn": 1,
            "session_id": "thread-a",
            "usage": {"input_tokens": 100, "cost_available": False},
        },
        {
            "kind": "turn_telemetry",
            "turn": 2,
            "session_id": "thread-a",
            "usage": {"input_tokens": 250, "cost_available": False},
        },
        {
            "kind": "turn_telemetry",
            "turn": 3,
            "session_id": "thread-b",
            "usage": {"input_tokens": 40, "cost_available": False},
        },
    ]
    (tmp_path / "events.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )

    _, tokens, _, session_usage = runner_module._historical_driver_totals(tmp_path)

    assert tokens == 290
    assert set(session_usage) == {"thread-a", "thread-b"}


def test_codex_cumulative_usage_preserves_omitted_counters():
    first_delta, previous = runner_module._codex_usage_delta_tokens(
        {"input_tokens": 100, "output_tokens": 10}, None
    )
    second_delta, current = runner_module._codex_usage_delta_tokens(
        {"input_tokens": 150}, previous
    )

    assert first_delta == 110
    assert second_delta == 50
    assert current["input_tokens"] == 150
    assert current["output_tokens"] == 10


def test_historical_codex_cumulative_usage_regression_fails_closed(tmp_path):
    records = [
        {
            "kind": "turn_telemetry",
            "turn": 1,
            "session_id": "thread-a",
            "usage": {"input_tokens": 100, "cost_available": False},
        },
        {
            "kind": "turn_telemetry",
            "turn": 2,
            "session_id": "thread-a",
            "usage": {"input_tokens": 99, "cost_available": False},
        },
    ]
    (tmp_path / "events.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="regressed Codex token usage"):
        runner_module._historical_driver_totals(tmp_path)


def test_historical_codex_unavailable_usage_preserves_session_baseline(tmp_path):
    records = [
        {
            "kind": "turn_telemetry",
            "turn": 1,
            "session_id": "thread-a",
            "usage": {"input_tokens": 100, "cost_available": False},
        },
        {
            "kind": "turn_telemetry",
            "turn": 2,
            "session_id": "thread-a",
            "usage": {"cost_available": False},
        },
        {
            "kind": "turn_telemetry",
            "turn": 3,
            "session_id": "thread-a",
            "usage": {"input_tokens": 160, "cost_available": False},
        },
    ]
    (tmp_path / "events.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )

    _, tokens, _, session_usage = runner_module._historical_driver_totals(tmp_path)

    assert tokens == 160
    assert session_usage["thread-a"]["input_tokens"] == 160


def test_historical_no_progress_recovers_only_trailing_stalled_turns(tmp_path):
    records = [
        {"kind": "turn_started", "turn": 1, "level": 0, "env_step": 0},
        {"kind": "turn_started", "turn": 2, "level": 0, "env_step": 0},
        {"kind": "turn_started", "turn": 3, "level": 1, "env_step": 1},
    ]
    (tmp_path / "events.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )

    assert runner_module._historical_no_progress(
        tmp_path, _snapshot(history_len=1)
    ) == 1


def test_run_started_marks_restart_without_actions_as_resumed(tmp_path):
    (tmp_path / "events.jsonl").write_text(
        json.dumps({"seq": 1, "kind": "run_finished"}) + "\n",
        encoding="utf-8",
    )

    runner_module._run_started(
        tmp_path,
        _snapshot(),
        provider="codex",
        model="gpt-5.6-luna",
        max_actions=10,
    )

    records = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["kind"] == "run_started"
    assert records[-1]["resumed"] is True
    assert records[-1]["resumed_transitions"] == 0


@pytest.mark.parametrize(
    ("max_turns", "run_token_cap", "records"),
    [
        (1, 1_000, [{"kind": "turn_started", "turn": 1}]),
        (
            5,
            100,
            [
                {"kind": "turn_started", "turn": 1},
                {
                    "kind": "turn_telemetry",
                    "turn": 1,
                    "usage": {
                        "input_tokens": 90,
                        "output_tokens": 10,
                        "cost_available": False,
                    },
                    "total_cost_usd": 0.0,
                },
            ],
        ),
    ],
)
def test_codex_preflight_does_not_run_after_trajectory_cap(
    monkeypatch, tmp_path, max_turns, run_token_cap, records
):
    snapshot = _snapshot()
    monkeypatch.setenv("ONLY_RESET_LEVELS", "true")
    events = tmp_path / "events.jsonl"
    events.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    before = events.read_bytes()

    def fake_initialize(*_args, **_kwargs):
        (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")
        return tmp_path, snapshot

    monkeypatch.setattr(runner_module, "initialize_workdir", fake_initialize)
    monkeypatch.setattr(
        runner_module, "_prepare_codex_home", lambda _workdir: tmp_path / "home"
    )
    monkeypatch.setattr(
        runner_module,
        "_prepare_codex_catalog",
        lambda *_a, **_k: (tmp_path / "catalog.json", "digest", "codex-cli test"),
    )
    monkeypatch.setattr(runner_module, "_record_codex_metadata", lambda *_a, **_k: None)
    monkeypatch.setattr(
        runner_module,
        "run_codex_turn",
        lambda *_a, **_k: pytest.fail("driver must not run after a trajectory cap"),
    )
    args = parse_args(
        [
            "--game",
            "bp35",
            "--workdir",
            str(tmp_path),
            "--max-turns",
            str(max_turns),
            "--run-token-cap",
            str(run_token_cap),
            "--no-system-prompt",
        ]
    )

    assert runner_module._run_live(args) == 0
    assert events.read_bytes() == before


def test_claude_cost_cap_remains_per_invocation(monkeypatch, tmp_path):
    snapshot = _snapshot()
    calls = 0
    records = [
        {"seq": 1, "kind": "turn_started", "turn": 1},
        {
            "seq": 2,
            "kind": "turn_telemetry",
            "turn": 1,
            "usage": {},
            "total_cost_usd": 5.0,
        },
    ]
    (tmp_path / "events.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )

    def fake_initialize(*_args, **_kwargs):
        (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")
        return tmp_path, snapshot

    monkeypatch.setattr(runner_module, "initialize_workdir", fake_initialize)
    monkeypatch.setattr(runner_module, "load_snapshot", lambda _workdir: snapshot)
    monkeypatch.setattr(runner_module, "oauth_token", lambda: "test-token")

    def fake_claude_turn(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "session_id": "claude-session",
            "usage": {"input_tokens": 10},
            "total_cost_usd": 0.0,
            "num_turns": 1,
            "is_error": False,
            "result": "no commit",
        }

    monkeypatch.setattr(runner_module, "run_claude_turn", fake_claude_turn)
    args = parse_args(
        [
            "--provider",
            "claude",
            "--game",
            "bp35",
            "--workdir",
            str(tmp_path),
            "--max-turns",
            "2",
            "--run-cost-cap",
            "5",
            "--turn-token-cap",
            "1",
            "--run-token-cap",
            "1",
            "--no-system-prompt",
        ]
    )

    assert runner_module._run_live(args) == 0
    assert calls == 2


def test_timeout_with_completed_commit_stops_without_emitting_fallback(
    monkeypatch, tmp_path
):
    snapshot = _snapshot()
    monkeypatch.setenv("ONLY_RESET_LEVELS", "true")
    (tmp_path / "events.jsonl").write_text(
        json.dumps(
            {
                "seq": 1,
                "kind": "turn_telemetry",
                "turn": 1,
                "session_id": "thread-1",
                "usage": {"input_tokens": 100, "cost_available": False},
                "total_cost_usd": 0.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    committed = CommittedTurn(
        plan=[[3, None, None]],
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

    def fake_initialize(*_args, **_kwargs):
        (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")
        return tmp_path, snapshot

    monkeypatch.setattr(runner_module, "initialize_workdir", fake_initialize)
    monkeypatch.setattr(runner_module, "load_snapshot", lambda _workdir: snapshot)
    monkeypatch.setattr(
        runner_module, "_prepare_codex_home", lambda _workdir: tmp_path / "home"
    )
    monkeypatch.setattr(
        runner_module,
        "_prepare_codex_catalog",
        lambda *_a, **_k: (tmp_path / "catalog.json", "digest", "codex-cli test"),
    )
    monkeypatch.setattr(runner_module, "_record_codex_metadata", lambda *_a, **_k: None)
    monkeypatch.setattr(
        runner_module,
        "run_codex_turn",
        lambda *_a, **_k: {
            "session_id": "thread-1",
            "usage": {"cost_available": False},
            "total_cost_usd": 0.0,
            "cost_available": False,
            "num_turns": 0,
            "is_error": True,
            "timed_out": True,
            "result": "Codex driver rejected the turn.",
            "violations": ["Codex turn timed out before complete stream audit"],
        },
    )
    monkeypatch.setattr(
        runner_module, "load_committed_turn", lambda _workdir, _turn_id: committed
    )
    args = parse_args(
        [
            "--game",
            "bp35",
            "--workdir",
            str(tmp_path),
            "--max-turns",
            "1",
            "--no-system-prompt",
        ]
    )

    assert runner_module._run_live(args) == 1
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert all(event["kind"] != "turn_fallback" for event in events)
    telemetry = [event for event in events if event["kind"] == "turn_telemetry"]
    assert not any(
        "regressed" in str(violation)
        for violation in telemetry[-1].get("violations", [])
    )
    _, tokens, _, session_usage = runner_module._historical_driver_totals(tmp_path)
    assert tokens == 100
    assert session_usage["thread-1"]["input_tokens"] == 100
    # A timeout recovers the thread id for diagnostics only. Its incomplete audit
    # stream makes that session untrusted, even when Locus durably committed.
    checkpoint = json.loads(
        (tmp_path / "sessions" / "sessions.json").read_text(encoding="utf-8")
    )
    assert checkpoint["invalidated"] is True
    assert checkpoint["sid"] == ""
    assert load_driver_session(
        tmp_path, provider="codex", model="gpt-5.6-luna"
    ) is None


def test_rejected_resume_invalidates_trusted_session_checkpoint(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("ONLY_RESET_LEVELS", "true")
    snapshot = _snapshot()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    checkpoint = {
        "cwd": str(tmp_path.resolve()),
        "provider": "codex",
        "model": "gpt-5.6-luna",
        "sid": "thread-trusted",
        "resume": True,
    }
    (sessions / "sessions.json").write_text(
        json.dumps(checkpoint), encoding="utf-8"
    )
    def fake_initialize(*_args, **_kwargs):
        (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")
        return tmp_path, snapshot

    monkeypatch.setattr(runner_module, "initialize_workdir", fake_initialize)
    monkeypatch.setattr(runner_module, "load_snapshot", lambda _workdir: snapshot)
    monkeypatch.setattr(
        runner_module, "_prepare_codex_home", lambda _workdir: tmp_path / "home"
    )
    monkeypatch.setattr(
        runner_module,
        "_prepare_codex_catalog",
        lambda *_a, **_k: (tmp_path / "catalog.json", "digest", "codex-cli test"),
    )
    monkeypatch.setattr(runner_module, "_record_codex_metadata", lambda *_a, **_k: None)
    monkeypatch.setattr(
        runner_module,
        "run_codex_turn",
        lambda *_a, **_k: {
            "session_id": "thread-other",
            "usage": {"cost_available": False},
            "total_cost_usd": 0.0,
            "cost_available": False,
            "num_turns": 0,
            "is_error": True,
            "timed_out": False,
            "result": "rejected",
            "violations": ["session mismatch"],
        },
    )
    args = parse_args(
        [
            "--game",
            "bp35",
            "--workdir",
            str(tmp_path),
            "--max-turns",
            "1",
            "--no-system-prompt",
        ]
    )

    assert runner_module._run_live(args) == 1
    invalidated = json.loads(
        (sessions / "sessions.json").read_text(encoding="utf-8")
    )
    assert invalidated["invalidated"] is True
    assert invalidated["sid"] == ""
    assert load_driver_session(
        tmp_path, provider="codex", model="gpt-5.6-luna"
    ) is None


def test_auth_bridge_change_becomes_structured_driver_error(monkeypatch, tmp_path):
    class FakeProcess:
        pid = 4321
        returncode = 0

        def communicate(self, **_kwargs):
            return (
                "\n".join(
                    [
                            json.dumps(
                                {"type": "thread.started", "thread_id": "thread-1"}
                            ),
                            json.dumps({"type": "turn.started"}),
                            json.dumps({"type": "turn.completed", "usage": {}}),
                    ]
                ),
                "",
            )

    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/opt/bin/codex")
    monkeypatch.setattr(
        runner_module.subprocess, "Popen", lambda *_a, **_k: FakeProcess()
    )
    monkeypatch.setattr(
        runner_module,
        "_prepare_codex_home",
        lambda _workdir: (_ for _ in ()).throw(RuntimeError("secret path")),
    )

    catalog = tmp_path / "catalog.json"
    catalog.write_text("{}", encoding="utf-8")
    catalog_digest = hashlib.sha256(catalog.read_bytes()).hexdigest()
    result = runner_module.run_codex_turn(
        "turn",
        workdir=tmp_path,
        codex_home=tmp_path / "home",
        model_catalog=catalog,
        catalog_digest=catalog_digest,
        game="bp35-0a0ad940",
        turn=1,
        turn_id="turn-000001",
        max_actions=10,
        model="gpt-5.6-luna",
        effort="max",
        session_id="",
        resume=False,
        timeout=1,
        system_prompt_file=None,
    )

    assert result["is_error"] is True
    assert result["violations"] == ["Codex credential bridge changed during the turn"]
    assert "secret path" not in result["result"]


def test_codex_live_workdir_must_be_outside_repository(tmp_path):
    runner_module._validate_codex_workdir(tmp_path)
    with pytest.raises(ValueError, match="outside"):
        runner_module._validate_codex_workdir(runner_module.REPO_ROOT / "nested-run")
    with pytest.raises(ValueError, match="must not contain"):
        runner_module._validate_codex_workdir(runner_module.REPO_ROOT.parent)
