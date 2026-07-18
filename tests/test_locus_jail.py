from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from schema_harness.locus import (
    COMMIT_MESSAGE,
    CROSS_TRANSITION_GATE_MESSAGE,
    LOCK_MESSAGE,
    LocusService,
    mcp,
)


class FakeEnvironment:
    def __init__(self) -> None:
        self.counter = 0
        self.observation_space = self._frame()

    def _frame(self):
        frame = SimpleNamespace(
            frame=[np.full((2, 2), self.counter, dtype=int)],
            levels_completed=0,
            win_levels=1,
            state=SimpleNamespace(value="NOT_FINISHED"),
            available_actions=[3, 6],
        )
        self.observation_space = frame
        return frame

    def step(self, _action, _data=None):
        self.counter += 1
        return self._frame()

    def reset(self):
        return self._frame()


class FakeArcade:
    def __init__(self) -> None:
        self.environment = FakeEnvironment()

    def make(self, game, seed):
        assert game == "jail-test"
        assert seed == 0
        assert os.environ["ONLY_RESET_LEVELS"] == "true"
        return self.environment


def _service(tmp_path: Path, **kwargs) -> LocusService:
    return LocusService(
        tmp_path,
        "jail-test",
        "turn-1",
        arcade=FakeArcade(),
        **kwargs,
    )


def test_exact_fourteen_tool_names_and_fixed_signatures():
    tools = asyncio.run(mcp.list_tools())
    assert [tool.name for tool in tools] == [
        "commit_actions",
        "run_backtest",
        "run_bfs",
        "read_history",
        "run_python",
        "run_shell",
        "write_file",
        "edit_file",
        "read_file",
        "grep",
        "find",
        "cp",
        "mv",
        "rm",
    ]
    schemas = {tool.name: tool.inputSchema for tool in tools}
    assert set(schemas["commit_actions"]["properties"]) == {
        "actions",
        "reason",
        "suggestion",
    }
    assert set(schemas["run_backtest"]["properties"]) == {
        "start",
        "indices",
        "max_details",
    }
    assert set(schemas["run_bfs"]["properties"]) == {
        "target",
        "clicks",
        "max_depth",
        "max_nodes",
    }
    assert set(schemas["read_history"]["properties"]) == {"indices", "detail"}


def test_file_tools_reject_parent_absolute_and_symlink_escapes(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "escape-link").symlink_to(outside)
    with _service(tmp_path) as service:
        with pytest.raises(ValueError, match="escapes workdir"):
            service.write_file("../escaped.txt", "bad")
        with pytest.raises(ValueError, match="escapes workdir"):
            service.read_file(str(outside))
        with pytest.raises(ValueError, match="escapes workdir"):
            service.read_file("escape-link")
        assert service.grep("outside", ".") == "No matches."
    assert outside.read_text(encoding="utf-8") == "outside"


def test_world_model_write_installs_exact_interface_string_and_commit_locks(tmp_path):
    source = (
        "def step(grid, action, x=None, y=None):\n"
        "    return grid, {}\n"
    )
    with _service(tmp_path) as service:
        output = service.write_file("world_model_v1.py", source)
        assert output == (
            f"OK: wrote {len(source.encode('utf-8'))} bytes to world_model_v1.py. "
            "Installed as the live world model [stateless (step)]; no is_goal "
            "(BFS disabled). Run run_backtest to check it against history."
        )
        assert service.gateway.live_model_path() == tmp_path / "world_model_v1.py"
        assert service.commit_actions([{"action": 3}], "probe") == (
            "Committed 1 action(s). Stop now — end your turn, do not call more tools."
        )
        assert service.read_file("world_model_v1.py") == LOCK_MESSAGE


def test_run_bfs_wall_clock_timeout_string(tmp_path):
    source = (
        "import time\n\n"
        "def step(grid, action, x=None, y=None):\n"
        "    time.sleep(1)\n"
        "    return grid, {}\n\n"
        "def is_goal(state):\n"
        "    return False\n"
    )
    with _service(tmp_path, bfs_timeout=0.01) as service:
        service.write_file("world_model_v1.py", source)
        assert service.run_bfs("advance", [], max_depth=1) == (
            "ERROR: run_bfs timed out after 0.01s."
        )


def test_process_tools_apply_os_sandbox_and_keep_normal_workdir_use(tmp_path):
    outside = tmp_path.parent / "locus-secret.txt"
    outside.write_text("sandbox-secret", encoding="utf-8")
    with _service(tmp_path) as service:
        python_output = service.run_python(
            "import numpy as np; open('made.txt', 'w').write('ok'); "
            "print(np.array([1, 2]).sum())"
        )
        assert "exit=0" in python_output and "\n3\n" in python_output
        assert (tmp_path / "made.txt").read_text() == "ok"

        shell_output = service.run_shell("head -1 made.txt")
        assert "exit=0" in shell_output and "\nok" in shell_output

        blocked = service.run_shell("cat ../locus-secret.txt")
        assert "exit=0" not in blocked
        assert "sandbox-secret" not in blocked

        ctypes_output = service.run_python(
            "import ctypes, os; libc=ctypes.CDLL(None); "
            "libc.open.argtypes=[ctypes.c_char_p, ctypes.c_int]; "
            f"fd=libc.open({str(outside).encode()!r}, 0); "
            "assert fd >= 0; print(os.read(fd, 100))"
        )
        assert "exit=0" not in ctypes_output
        assert "sandbox-secret" not in ctypes_output


def test_harness_state_is_readable_but_immutable_to_agent_tools(tmp_path):
    events = tmp_path / "events.jsonl"
    with _service(tmp_path, events_path=events) as service:
        state_path = tmp_path / "runtime" / "gateway_state.json"
        state_before = state_path.read_bytes()
        state_alias = tmp_path / "state-alias.json"
        os.link(state_path, state_alias)

        with pytest.raises(ValueError, match="harness-managed path is read-only"):
            service.write_file("events.jsonl", "forged\n")
        with pytest.raises(ValueError, match="harness-managed path is read-only"):
            service.write_file("runtime/gateway_state.json", "forged\n")
        with pytest.raises(ValueError, match="harness-managed path is read-only"):
            service.rm("events.jsonl")
        with pytest.raises(ValueError, match="harness-managed path is read-only"):
            service.edit_file("state-alias.json", "", "")

        python_output = service.run_python(
            "open('events.jsonl', 'w').write('forged')"
        )
        assert "exit=0" not in python_output
        shell_output = service.run_shell(
            "printf forged > runtime/gateway_state.json"
        )
        assert "exit=0" not in shell_output
        hardlink_output = service.run_shell(
            "ln events.jsonl audit-link && printf forged > audit-link"
        )
        assert "exit=0" not in hardlink_output
        state_alias_output = service.run_shell(
            "printf forged > state-alias.json"
        )
        assert "exit=0" not in state_alias_output

        assert state_path.read_bytes() == state_before
        records = [json.loads(line) for line in events.read_text().splitlines()]
        assert records
        assert all(isinstance(record.get("seq"), int) for record in records)
        assert any(
            record.get("kind") == "tool_started"
            and record.get("name") == "write_file"
            for record in records
        )


def test_raw_history_is_private_but_current_snapshot_is_readable(tmp_path):
    events = tmp_path / "raw-events.jsonl"
    debug = tmp_path / "runtime" / "locus.jsonl"
    with LocusService(
        tmp_path,
        "jail-test",
        "turn-1",
        turn=1,
        events_path=events,
        debug_log=debug,
        arcade=FakeArcade(),
    ) as service:
        service.commit_actions([{"action": 3}], "create raw history")

    event_alias = tmp_path / "event-alias.jsonl"
    os.link(events, event_alias)
    with LocusService(
        tmp_path,
        "jail-test",
        "turn-2",
        turn=2,
        events_path=events,
        debug_log=debug,
        arcade=FakeArcade(),
    ) as service:
        private_paths = (
            "raw-events.jsonl",
            "event-alias.jsonl",
            "runtime/gateway_timeline.jsonl",
            "runtime/turn_ledger.json",
            "runtime/locus.jsonl",
        )
        for path in private_paths:
            with pytest.raises(ValueError, match="harness-private path"):
                service.read_file(path)
        with pytest.raises(ValueError, match="harness-managed path is read-only"):
            service.edit_file("event-alias.jsonl", "", "")
        assert event_alias.samefile(events)

        listing = service.find("*")
        assert all(path not in listing for path in private_paths)
        assert service.grep("create raw history", ".") == "No matches."

        snapshot = service.read_file("runtime/gateway_state.json")
        assert '"history_len":1' in snapshot
        python_snapshot = service.run_python(
            "import json; print(json.load(open('runtime/gateway_state.json'))['history_len'])"
        )
        assert "exit=0" in python_snapshot and "\n1\n" in python_snapshot

        python_raw = service.run_python(
            "print(open('raw-events.jsonl').read())"
        )
        shell_raw = service.run_shell("cat runtime/gateway_timeline.jsonl")
        hardlink_raw = service.run_shell(
            "ln raw-events.jsonl fresh-alias && cat fresh-alias"
        )
        for output in (python_raw, shell_raw, hardlink_raw):
            assert "exit=0" not in output
            assert "create raw history" not in output


def test_harness_private_config_is_unreadable_to_agent_tools(tmp_path):
    private = tmp_path / "config" / "claude" / ".claude.json"
    private.parent.mkdir(parents=True)
    private.write_text("credential-material", encoding="utf-8")
    alias = tmp_path / "credential-alias"
    os.link(private, alias)
    with _service(tmp_path) as service:
        with pytest.raises(ValueError, match="harness-private path"):
            service.read_file("config/claude/.claude.json")
        with pytest.raises(ValueError, match="harness-private path"):
            service.read_file("credential-alias")
        assert "config/claude/.claude.json" not in service.find("*")
        assert "credential-alias" not in service.find("*")
        assert "credential-material" not in service.grep("credential-material", ".")
        python_output = service.run_python(
            "print(open('config/claude/.claude.json').read())"
        )
        shell_output = service.run_shell("cat config/claude/.claude.json")
        python_alias = service.run_python(
            "print(open('credential-alias').read())"
        )
        shell_alias = service.run_shell("cat credential-alias")
    assert "credential-material" not in python_output
    assert "exit=0" not in python_output
    assert "credential-material" not in shell_output
    assert "exit=0" not in shell_output
    assert "credential-material" not in python_alias
    assert "exit=0" not in python_alias
    assert "credential-material" not in shell_alias
    assert "exit=0" not in shell_alias


def test_agent_world_model_exec_is_sandboxed_during_probe_and_prediction(tmp_path):
    outside = tmp_path.parent / "model-secret.txt"
    outside.write_text("sandbox-secret", encoding="utf-8")
    private = tmp_path / "config" / "claude" / ".claude.json"
    private.parent.mkdir(parents=True)
    private.write_text("credential-material", encoding="utf-8")
    source = f"""
import ctypes
import os
import subprocess

def try_leak(name):
    libc = ctypes.CDLL(None)
    libc.open.argtypes = [ctypes.c_char_p, ctypes.c_int]
    fd = libc.open({str(outside).encode()!r}, 0)
    if fd >= 0:
        leaked = os.read(fd, 100)
        open(name, "wb").write(leaked)

try:
    try_leak("probe-leak.txt")
    open("runtime/gateway_state.json", "w").write("tampered")
except Exception:
    pass
try:
    private = open("config/claude/.claude.json").read()
    open("runtime/model_scratch/config-leak.txt", "w").write(private)
except Exception:
    pass
try:
    leaked = subprocess.run(
        ["/bin/cat", {str(outside)!r}], capture_output=True, check=True
    ).stdout
    open("subprocess-leak.txt", "wb").write(leaked)
except Exception:
    pass

def step(grid, action, x=None, y=None):
    try_leak("predict-leak.txt")
    return [[value + 1 for value in row] for row in grid], {{}}
"""
    with _service(tmp_path) as service:
        service.write_file("world_model_v1.py", source)
        assert json.loads((tmp_path / "runtime" / "gateway_state.json").read_text())
        service.commit_actions([{"action": 3}], "sandbox check")
        assert service.last_result is not None
        assert service.last_result.executed == 1

    assert not (tmp_path / "probe-leak.txt").exists()
    assert not (tmp_path / "predict-leak.txt").exists()
    assert not (tmp_path / "subprocess-leak.txt").exists()
    assert not (tmp_path / "runtime" / "model_scratch" / "config-leak.txt").exists()


def test_commit_predictions_interleave_with_real_steps_and_timeout_durably(tmp_path):
    source = """
import time
from pathlib import Path

def step(grid, action, x=None, y=None):
    marker = Path("runtime/model_scratch/calls.txt")
    calls = int(marker.read_text()) + 1 if marker.exists() else 1
    marker.write_text(str(calls))
    if calls == 2:
        time.sleep(5)
    return [[value + 1 for value in row] for row in grid], {}
"""
    with _service(tmp_path, process_timeout=1) as service:
        service.write_file("world_model_v1.py", source)
        service.commit_actions([{"action": 3}, {"action": 3}], "interleave")
        assert service.last_result is not None
        assert service.last_result.executed == 1
        assert service.last_result.halt_reason == "nondeterministic-model"

    assert (tmp_path / "runtime" / "model_scratch" / "calls.txt").read_text() == "2"
    ledger = json.loads((tmp_path / "runtime" / "turn_ledger.json").read_text())
    assert ledger["turns"]["turn-1"]["phase"] == "COMPLETE"


def test_locus_backtest_range_and_bfs_run_inside_model_worker(tmp_path):
    source = (
        "def step(grid, action, x=None, y=None):\n"
        "    predicted = [[value + 1 for value in row] for row in grid]\n"
        "    return predicted, {'level_up': action == 6}\n\n"
        "def is_goal(state):\n"
        "    return False\n"
    )
    with _service(tmp_path) as service:
        service.write_file("world_model_v1.py", source)
        no_click_bfs = service.run_bfs("level_up", [], max_depth=1)
        assert no_click_bfs.startswith("BFS: no goal within depth 1")
        assert "+ 0 click(s)" in no_click_bfs
        bfs = service.run_bfs("level_up", [[1, 1]], max_depth=1)
        assert bfs.startswith("BFS: goal in 1 step(s) via level_up")
        assert "{'action': 6, 'x': 1, 'y': 1}" in bfs
        service.commit_actions([{"action": 3}], "create history")

    with LocusService(
        tmp_path,
        "jail-test",
        "turn-2",
        turn=2,
        arcade=FakeArcade(),
    ) as service:
        backtest = service.run_backtest(start=0)
    assert backtest.startswith("backtest [range #0..#0]: 1/1 transitions fully correct")


def test_read_history_keeps_original_prefix_and_appends_grid_inspector(tmp_path):
    with _service(tmp_path) as service:
        service.commit_actions([{"action": 3}], "probe")
    with LocusService(
        tmp_path,
        "jail-test",
        "turn-2",
        turn=2,
        arcade=FakeArcade(),
    ) as service:
        output = service.read_history()

    assert output.startswith(
        "1 transitions total. Summary: level_ups=0 deaths=0 wins=0 "
        "resets(action0)=0 clicks(action6)=0; by-action={3: 1}; "
        "max_level=0; showing indices [0, 0] -> 1 steps; detail=full: "
        "#0 action=3; 4 cells changed; state=NOT_FINISHED; level=0; "
        "level_up=False dead=False win=False"
    )
    assert output.endswith(
        "Inspector: showing 1/1 selected transition(s)\n"
        "#0 diff: shape=2x2; changed=4/4; bbox=[r0..1,c0..1]; regions=1\n"
        "  4@[r0..1,c0..1] from={0:4} to={1:4}\n"
        "    patch before=[[0,0],[0,0]] after=[[1,1],[1,1]]\n"
        "value_pairs={0->1:4}\n"
        "Model gate: NONE after 1 transition. Write a minimal world_model_v1.py "
        "now and run run_backtest; without an installed model, commit_actions can "
        "execute only one probe. Model unknown actions conservatively instead of "
        "waiting to solve every control.\n"
        "Current-grid click target proposals (1; component-based, unverified; "
        "pass selected coordinates as run_bfs clicks): [[0,0]]"
    )


def test_read_history_omits_model_gate_once_a_model_is_installed(tmp_path):
    source = "def step(grid, action, x=None, y=None):\n    return grid, {}\n"
    with _service(tmp_path) as service:
        service.write_file("world_model_v1.py", source)
        service.commit_actions([{"action": 3}], "seed history")
    with LocusService(
        tmp_path,
        "jail-test",
        "turn-2",
        turn=2,
        arcade=FakeArcade(),
    ) as service:
        output = service.read_history()

    assert "Model gate:" not in output


def test_read_history_appends_pending_cross_transition_hint(monkeypatch, tmp_path):
    hint = "Cross-transition inspector: pending paired movement."
    monkeypatch.setattr(
        "schema_harness.locus.pending_actor_affordance_hint",
        lambda _initial, _observations: hint,
    )
    with _service(tmp_path) as service:
        service.commit_actions([{"action": 3}], "seed history")
    with LocusService(
        tmp_path,
        "jail-test",
        "turn-2",
        turn=2,
        arcade=FakeArcade(),
    ) as service:
        output = service.read_history()

    assert hint in output


def test_read_history_summary_remains_appendix_free(tmp_path):
    with _service(tmp_path) as service:
        service.commit_actions([{"action": 3}], "seed history")
    with LocusService(
        tmp_path,
        "jail-test",
        "turn-2",
        turn=2,
        arcade=FakeArcade(),
    ) as service:
        output = service.read_history(detail="summary")

    assert output == (
        "1 transitions total. Summary: level_ups=0 deaths=0 wins=0 "
        "resets(action0)=0 clicks(action6)=0; by-action={3: 1}; "
        "max_level=0; showing indices [0, 0] -> 1 steps."
    )


def test_cross_transition_gate_is_transactional_and_full_history_unlocks(
    monkeypatch,
    tmp_path,
):
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr(
        LocusService,
        "_new_affordance_topology",
        lambda _service: "new topology",
    )
    with _service(tmp_path, events_path=events) as service:
        assert service.commit_actions([{"action": 3}], "probe") == (
            CROSS_TRANSITION_GATE_MESSAGE
        )
        assert service._committed is False
        assert service.last_result is None
        assert service.gateway.timeline == ()
        records = [json.loads(line) for line in events.read_text().splitlines()]
        assert [record["kind"] for record in records] == [
            "tool_started",
            "tool_finished",
        ]
        assert records[-1]["output"] == CROSS_TRANSITION_GATE_MESSAGE
        assert records[-1]["is_error"] is False

        service.read_history(detail="summary")
        assert service.commit_actions([{"action": 3}], "probe") == (
            CROSS_TRANSITION_GATE_MESSAGE
        )
        with pytest.raises(ValueError, match="detail must be"):
            service.read_history(detail="bad")
        assert service.commit_actions([{"action": 3}], "probe") == (
            CROSS_TRANSITION_GATE_MESSAGE
        )
        records = [json.loads(line) for line in events.read_text().splitlines()]
        assert not any(
            record["kind"] in {"turn_committed", "action_taken"}
            for record in records
        )

        service.read_history(detail="full")
        assert service.commit_actions([{"action": 3}], "probe") == (
            COMMIT_MESSAGE.format(count=1)
        )
        assert service.last_result is not None
        assert service.last_result.executed == 1
        assert service.commit_actions([{"action": 3}], "again") == LOCK_MESSAGE

    records = [json.loads(line) for line in events.read_text().splitlines()]
    assert sum(record["kind"] == "turn_committed" for record in records) == 1
    assert sum(record["kind"] == "action_taken" for record in records) == 1


@pytest.mark.parametrize(
    ("actions", "count"),
    [([], 0), ([{"action": 0}, {"action": 3}], 2)],
)
def test_cross_transition_gate_exempts_empty_and_reset_first_queues(
    monkeypatch,
    tmp_path,
    actions,
    count,
):
    monkeypatch.setattr(
        LocusService,
        "_new_affordance_topology",
        lambda _service: "new topology",
    )
    with _service(tmp_path) as service:
        assert service.commit_actions(actions, "reset or stop") == (
            COMMIT_MESSAGE.format(count=count)
        )


def test_cross_transition_gate_fails_open_on_inspector_error(monkeypatch, tmp_path):
    def fail(_service):
        raise RuntimeError("inspector failed")

    monkeypatch.setattr(LocusService, "_new_affordance_topology", fail)
    with _service(tmp_path) as service:
        assert service.commit_actions([{"action": 3}], "probe") == (
            COMMIT_MESSAGE.format(count=1)
        )


def test_cross_transition_gate_covers_mid_batch_activation_only_once(
    monkeypatch,
    tmp_path,
):
    source = (
        "def step(grid, action, x=None, y=None):\n"
        "    return [[value + 1 for value in row] for row in grid], {}\n"
    )
    monkeypatch.setattr(
        "schema_harness.locus.describe_actor_affordances",
        lambda _initial, frames: "topology" if len(frames) >= 2 else None,
    )
    with _service(tmp_path) as service:
        service.write_file("world_model_v1.py", source)
        assert service.commit_actions(
            [{"action": 3}, {"action": 3}, {"action": 3}],
            "seed one batch",
        ) == COMMIT_MESSAGE.format(count=3)
        assert service.last_result is not None
        assert service.last_result.executed == 3

    with LocusService(
        tmp_path,
        "jail-test",
        "turn-2",
        arcade=FakeArcade(),
    ) as service:
        assert service.commit_actions([], "defer") == COMMIT_MESSAGE.format(count=0)

    with LocusService(
        tmp_path,
        "jail-test",
        "turn-3",
        arcade=FakeArcade(),
    ) as service:
        assert service.commit_actions([{"action": 3}], "probe") == (
            CROSS_TRANSITION_GATE_MESSAGE
        )
        service.read_history(detail="full")
        assert service.commit_actions([{"action": 3}], "probe") == (
            COMMIT_MESSAGE.format(count=1)
        )

    with LocusService(
        tmp_path,
        "jail-test",
        "turn-4",
        arcade=FakeArcade(),
    ) as service:
        assert service.commit_actions([{"action": 3}], "already delivered") == (
            COMMIT_MESSAGE.format(count=1)
        )


def test_stdio_wire_is_not_corrupted_by_arc_runtime_logging(tmp_path):
    (tmp_path / "notes.md").write_text("ok\n", encoding="utf-8")
    repo = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment.update(
        PYTHONPATH=str(repo),
        LOCUS_WORKDIR=str(tmp_path),
        LOCUS_GAME="bp35-0a0ad940",
        LOCUS_TURN_ID="turn-1",
        LOCUS_TURN="1",
        LOCUS_EVENTS=str(tmp_path / "events.jsonl"),
    )
    environment.setdefault("MPLCONFIGDIR", str(tmp_path / "matplotlib"))
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "schema_harness.locus"],
        env=environment,
        cwd=tmp_path,
    )

    async def exercise_server():
        with (tmp_path / "stderr.log").open("w", encoding="utf-8") as errors:
            async with stdio_client(parameters, errlog=errors) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    result = await session.call_tool("read_file", {"path": "notes.md"})
        return tools, result

    tools, result = asyncio.run(exercise_server())
    assert len(tools.tools) == 14
    assert result.isError is False
    assert result.content[0].text.endswith("1\tok")
    assert "Successfully loaded game class" in (tmp_path / "stderr.log").read_text()


def test_stdio_cross_transition_gate_unlocks_with_full_history_in_same_session(
    monkeypatch,
    tmp_path,
):
    repo = Path(__file__).resolve().parents[1]
    environments = repo / "environment_files"
    if not environments.is_dir():
        environments = tmp_path / "environments"
    monkeypatch.setenv("SCHEMA_ENVIRONMENTS_DIR", str(environments))
    game = "bp35-0a0ad940"
    with LocusService(tmp_path, game, "turn-1") as service:
        assert service.commit_actions([{"action": 3}], "seed left") == (
            COMMIT_MESSAGE.format(count=1)
        )
    with LocusService(tmp_path, game, "turn-2") as service:
        assert service.commit_actions([{"action": 4}], "seed right") == (
            COMMIT_MESSAGE.format(count=1)
        )

    environment = dict(os.environ)
    environment.update(
        PYTHONPATH=str(repo),
        LOCUS_WORKDIR=str(tmp_path),
        LOCUS_GAME=game,
        LOCUS_TURN_ID="turn-3",
        LOCUS_TURN="3",
        LOCUS_EVENTS=str(tmp_path / "events.jsonl"),
    )
    environment.setdefault("MPLCONFIGDIR", str(tmp_path / "matplotlib"))
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "schema_harness.locus"],
        env=environment,
        cwd=tmp_path,
    )

    async def exercise_server():
        with (tmp_path / "stderr.log").open("w", encoding="utf-8") as errors:
            async with stdio_client(parameters, errlog=errors) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    rejected = await session.call_tool(
                        "commit_actions",
                        {"actions": [{"action": 7}], "reason": "probe"},
                    )
                    history = await session.call_tool(
                        "read_history",
                        {"detail": "full"},
                    )
                    committed = await session.call_tool(
                        "commit_actions",
                        {"actions": [{"action": 7}], "reason": "probe"},
                    )
        return rejected, history, committed

    rejected, history, committed = asyncio.run(exercise_server())
    assert rejected.isError is False
    assert rejected.content[0].text == CROSS_TRANSITION_GATE_MESSAGE
    assert history.isError is False
    assert "Translated-footprint topology" in history.content[0].text
    assert committed.isError is False
    assert committed.content[0].text == COMMIT_MESSAGE.format(count=1)
