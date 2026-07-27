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

import schema_harness.locus as locus
from schema_harness.backtest import ALIGNMENT_BACKTEST_SELECTOR
from schema_harness.locus import (
    COMMIT_MESSAGE,
    CROSS_TRANSITION_GATE_MESSAGE,
    LOCK_MESSAGE,
    LocusService,
    MODEL_REPAIR_GATE_MESSAGE,
    RESET_BOUNDARY_GATE_MESSAGE,
    mcp,
)


@pytest.fixture(autouse=True)
def _enable_legacy_experimental_tooling(monkeypatch):
    """Keep existing intervention tests explicit while production defaults to core."""

    monkeypatch.setenv("SCHEMA_EXPERIMENTAL_TOOLING", "true")


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


def _green_backtest(selector: object) -> str:
    label = (
        "[all transitions]"
        if selector == "all"
        else "[full-history alignment]"
    )
    return (
        f"backtest {label}: green; 0 mismatch(es), 0 skipped. "
        "Model predicts ALL checkable transitions"
    )


def _install_stub_model(service: LocusService, source: str = "# model\n") -> Path:
    path = service.workdir / "world_model_v1.py"
    path.write_text(source, encoding="utf-8")
    service.gateway.set_live_model(path)
    return path


def _matching_model(grid, _action, _x=None, _y=None):
    return [[value + 1 for value in row] for row in grid], {}


def _seed_stubbed_model_surprise(monkeypatch, tmp_path: Path) -> Path:
    with _service(tmp_path, experimental_tooling=False) as service:
        model_path = _install_stub_model(service)
        monkeypatch.setattr(
            service,
            "_model_session",
            lambda: lambda grid, _action, _x=None, _y=None: (grid, {}),
        )
        service.commit_actions([{"action": 3}], "seed surprise")
        assert service.last_result is not None
        assert service.last_result.halt_reason == "surprise"
    return model_path


def _seed_non_reset_model_surprise(tmp_path: Path) -> None:
    source = "def step(grid, action, x=None, y=None):\n    return grid, {}\n"
    with _service(tmp_path) as service:
        service.write_file("world_model_v1.py", source)
        service.commit_actions([{"action": 3}], "seed surprise")
        assert service.last_result is not None
        assert service.last_result.halt_reason == "surprise"


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


def test_locus_service_default_bfs_timeout_is_300(tmp_path):
    # Measured from banked traces: the slowest BFS call in any run scoring >=92.91
    # took 293.9s (lp85), so 300s preserves every search that contributed to a win
    # while still cutting the pathological thrashers (sc25 502.8s, sp80 497.5s).
    with _service(tmp_path) as service:
        assert service.bfs_timeout == 300


def test_locus_service_default_backtest_timeout_is_120(tmp_path):
    with _service(tmp_path) as service:
        assert service.backtest_timeout == 120


def test_locus_factory_env_default_bfs_timeout_is_300(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCUS_WORKDIR", str(tmp_path))
    monkeypatch.setenv("LOCUS_GAME", "jail-test")
    monkeypatch.setenv("LOCUS_TURN_ID", "turn-1")
    monkeypatch.delenv("LOCUS_BFS_TIMEOUT", raising=False)
    monkeypatch.delenv("LOCUS_BACKTEST_TIMEOUT", raising=False)

    def fake_service(*_args, **kwargs):
        return SimpleNamespace(
            bfs_timeout=kwargs["bfs_timeout"],
            backtest_timeout=kwargs["backtest_timeout"],
        )

    monkeypatch.setattr(locus, "LocusService", fake_service)
    monkeypatch.setattr(locus, "_SERVICE", None)
    assert locus._service().bfs_timeout == 300
    assert locus._service().backtest_timeout == 120

    monkeypatch.setattr(locus, "_SERVICE", None)
    monkeypatch.setenv("LOCUS_BFS_TIMEOUT", "45")
    monkeypatch.setenv("LOCUS_BACKTEST_TIMEOUT", "15")
    assert locus._service().bfs_timeout == 45.0
    assert locus._service().backtest_timeout == 15.0


def test_run_bfs_default_node_budget_is_fifty_thousand(monkeypatch, tmp_path):
    source = (
        "def step(grid, action, x=None, y=None):\n"
        "    return grid, {}\n\n"
        "def is_goal(state):\n"
        "    return False\n"
    )
    captured = []

    def capture_worker(operation, _model_path, payload=None, *, timeout):
        if operation == "probe":
            return {
                "kind": "stateless",
                "entrypoint": "step",
                "has_is_goal": True,
            }
        if operation == "backtest":
            assert payload is not None
            return _green_backtest(payload["selector"])
        assert operation == "bfs"
        assert payload is not None
        captured.append((payload["max_nodes"], timeout))
        return {"output": "captured"}

    with _service(tmp_path) as service:
        monkeypatch.setattr(service, "_run_model_worker", capture_worker)
        service.write_file("world_model_v1.py", source)

        assert service.run_bfs("advance", [], max_depth=1) == "captured"
        assert captured[-1] == (50_000, service.bfs_timeout)

        assert service.run_bfs(
            "advance", [], max_depth=1, max_nodes=500
        ) == "captured"
        assert captured[-1] == (500, service.bfs_timeout)


def test_core_multi_action_commit_requires_green_model(monkeypatch, tmp_path):
    events = tmp_path / "events.jsonl"
    red_report = (
        "backtest [all transitions]: 0/1 transitions fully correct; "
        "1 mismatch(es), repair required"
    )

    def red_worker(operation, _model_path, payload=None, *, timeout):
        assert operation == "backtest"
        return red_report

    with _service(
        tmp_path,
        events_path=events,
        experimental_tooling=False,
    ) as service:
        _install_stub_model(service)
        monkeypatch.setattr(service, "_run_model_worker", red_worker)

        output = service.commit_actions(
            [{"action": 3}, {"action": 3}],
            "unvalidated plan",
        )
        assert output.startswith(locus.MODEL_VALIDATE_GATE_MESSAGE + "\n")
        assert red_report in output
        assert service._committed is False
        assert service.last_result is None
        assert service.gateway.timeline == ()
        records = [json.loads(line) for line in events.read_text().splitlines()]
        assert not any(record["kind"] == "turn_committed" for record in records)

        retry = service.commit_actions(
            [{"action": 3}, {"action": 3}],
            "retry unvalidated plan",
        )
        assert retry == output
        assert retry != LOCK_MESSAGE


def test_core_multi_action_commit_rejects_model_with_crashing_ingest(tmp_path):
    with _service(tmp_path, experimental_tooling=False) as service:
        assert service.commit_actions([{"action": 3}], "seed history") == (
            COMMIT_MESSAGE.format(count=1)
        )

    source = (
        "def init_state(entry_grid):\n"
        "    return entry_grid[0][0]\n\n"
        "def predict(state, grid, action, x=None, y=None):\n"
        "    predicted = [[value + 1 for value in row] for row in grid]\n"
        "    return predicted, {}, state + 1\n\n"
        "def ingest(state, actual_grid):\n"
        "    raise RuntimeError('ingest alignment crash')\n"
    )
    with LocusService(
        tmp_path,
        "jail-test",
        "turn-2",
        turn=2,
        arcade=FakeArcade(),
        experimental_tooling=False,
    ) as service:
        service.write_file("world_model_v1.py", source)
        output = service.commit_actions(
            [{"action": 3}, {"action": 3}],
            "reject ingest crash",
        )

        assert output.startswith(locus.MODEL_VALIDATE_GATE_MESSAGE + "\n")
        assert "backtest [full-history alignment]: 0/1" in output
        assert "; 1 mismatch(es)," in output
        assert "#0:ingest" in output
        assert service._committed is False
        assert service.last_result is None
        assert len(service.gateway.timeline) == 1


def test_core_multi_action_commit_with_green_model_executes(monkeypatch, tmp_path):
    def green_worker(operation, _model_path, payload=None, *, timeout):
        assert operation == "backtest"
        assert payload is not None
        return _green_backtest(payload["selector"])

    with _service(tmp_path, experimental_tooling=False) as service:
        _install_stub_model(service)
        monkeypatch.setattr(service, "_run_model_worker", green_worker)
        monkeypatch.setattr(service, "_model_session", lambda: _matching_model)

        assert service.commit_actions(
            [{"action": 3}, {"action": 3}],
            "validated plan",
        ) == COMMIT_MESSAGE.format(count=2)
        assert service.last_result is not None
        assert service.last_result.executed == 2
        assert len(service.gateway.timeline) == 2


def test_core_single_action_commit_allowed_with_red_model(monkeypatch, tmp_path):
    operations = []

    def red_worker(operation, _model_path, payload=None, *, timeout):
        operations.append(operation)
        return "red"

    with _service(tmp_path, experimental_tooling=False) as service:
        _install_stub_model(service)
        monkeypatch.setattr(service, "_run_model_worker", red_worker)
        monkeypatch.setattr(service, "_model_session", lambda: _matching_model)

        assert service.commit_actions([{"action": 3}], "probe") == (
            COMMIT_MESSAGE.format(count=1)
        )
        assert len(service.gateway.timeline) == 1
        assert "backtest" not in operations


def test_core_multi_action_commit_without_model_rejected(tmp_path):
    with _service(tmp_path, experimental_tooling=False) as service:
        assert service.commit_actions(
            [{"action": 3}, {"action": 3}],
            "model-free plan",
        ) == locus.MODEL_REQUIRED_GATE_MESSAGE
        assert service._committed is False
        assert service.gateway.timeline == ()

        assert service.commit_actions([{"action": 3}], "single probe") == (
            COMMIT_MESSAGE.format(count=1)
        )
        assert len(service.gateway.timeline) == 1


def test_core_pure_reset_queue_bypasses_validate_gate(tmp_path):
    for index, actions in enumerate(
        ([{"action": 0}], [{"action": 0}, {"action": 0}])
    ):
        with _service(
            tmp_path / f"pure-reset-{index}",
            experimental_tooling=False,
        ) as service:
            assert service.commit_actions(actions, "pure reset") == (
                COMMIT_MESSAGE.format(count=len(actions))
            )

    with _service(tmp_path / "mixed-reset", experimental_tooling=False) as service:
        assert service.commit_actions(
            [{"action": 0}, {"action": 3}],
            "mixed reset queue",
        ) == locus.MODEL_REQUIRED_GATE_MESSAGE
        assert service.gateway.timeline == ()


def test_validate_gate_result_cached_per_model_and_history(monkeypatch, tmp_path):
    operations = []

    def worker(operation, _model_path, payload=None, *, timeout):
        assert payload is not None
        if operation == "backtest":
            operations.append((operation, payload["selector"]))
            return _green_backtest(payload["selector"])
        assert operation == "bfs"
        operations.append((operation, None))
        return {"output": "bfs ran"}

    with _service(tmp_path, experimental_tooling=False) as service:
        _install_stub_model(service)
        monkeypatch.setattr(service, "_run_model_worker", worker)
        monkeypatch.setattr(service, "_model_session", lambda: _matching_model)

        assert service.run_bfs("advance", [], max_depth=1) == "bfs ran"
        assert service.run_bfs("advance", [], max_depth=1) == "bfs ran"
        assert service.commit_actions(
            [{"action": 3}, {"action": 3}],
            "cached validation",
        ) == COMMIT_MESSAGE.format(count=2)

    assert [item for item in operations if item[0] == "backtest"] == [
        ("backtest", "all"),
        ("backtest", ALIGNMENT_BACKTEST_SELECTOR),
    ]
    assert sum(item[0] == "bfs" for item in operations) == 2


def test_validate_gate_cache_invalidated_on_model_change(monkeypatch, tmp_path):
    selectors = []

    def worker(operation, _model_path, payload=None, *, timeout):
        assert payload is not None
        if operation == "backtest":
            selectors.append(payload["selector"])
            return _green_backtest(payload["selector"])
        assert operation == "bfs"
        return {"output": "bfs ran"}

    with _service(tmp_path, experimental_tooling=False) as service:
        model_path = _install_stub_model(service, "# model version one\n")
        monkeypatch.setattr(service, "_run_model_worker", worker)
        assert service.run_bfs("advance", [], max_depth=1) == "bfs ran"

        model_path.write_text("# model version two\n", encoding="utf-8")
        assert service.run_bfs("advance", [], max_depth=1) == "bfs ran"

    assert selectors == [
        "all",
        ALIGNMENT_BACKTEST_SELECTOR,
        "all",
        ALIGNMENT_BACKTEST_SELECTOR,
    ]


def test_validate_gate_cache_invalidated_on_history_growth(monkeypatch, tmp_path):
    backtest_history_lengths = []

    def worker(operation, _model_path, payload=None, *, timeout):
        assert payload is not None
        if operation == "backtest":
            backtest_history_lengths.append(len(payload["history"]["actions"]))
            return _green_backtest(payload["selector"])
        assert operation == "bfs"
        return {"output": "bfs ran"}

    with _service(tmp_path, experimental_tooling=False) as service:
        _install_stub_model(service)
        monkeypatch.setattr(service, "_run_model_worker", worker)
        assert service.run_bfs("advance", [], max_depth=1) == "bfs ran"

        service.gateway.commit(
            "history-growth",
            [{"action": 3}],
            "grow history",
            live_model=_matching_model,
        )
        assert service.run_bfs("advance", [], max_depth=1) == "bfs ran"

    assert backtest_history_lengths == [0, 0, 1, 1]


def test_validate_gate_timeout_rejects_without_caching(monkeypatch, tmp_path):
    calls = []

    def timeout_worker(operation, _model_path, payload=None, *, timeout):
        assert operation == "backtest"
        calls.append(timeout)
        raise locus._ModelWorkerTimeout

    with _service(
        tmp_path,
        experimental_tooling=False,
        backtest_timeout=0.25,
    ) as service:
        _install_stub_model(service)
        monkeypatch.setattr(service, "_run_model_worker", timeout_worker)

        for reason in ("first attempt", "second attempt"):
            output = service.commit_actions(
                [{"action": 3}, {"action": 3}],
                reason,
            )
            assert output.startswith(locus.MODEL_VALIDATE_GATE_MESSAGE + "\n")
            assert "timed out after 0.25s" in output
            assert service._committed is False

    assert calls == [0.25, 0.25]


def test_run_bfs_rejected_on_red_model_with_repair_report(monkeypatch, tmp_path):
    red_report = (
        "backtest [all transitions]: 0/1 transitions fully correct; "
        "1 mismatch(es), repair required"
    )
    operations = []

    def worker(operation, _model_path, payload=None, *, timeout):
        operations.append(operation)
        assert operation == "backtest"
        return red_report

    with _service(tmp_path, experimental_tooling=False) as service:
        _install_stub_model(service)
        monkeypatch.setattr(service, "_run_model_worker", worker)
        output = service.run_bfs("advance", [], max_depth=1)

    assert output == locus.MODEL_VALIDATE_GATE_MESSAGE + "\n" + red_report
    assert operations == ["backtest"]


def test_run_bfs_runs_on_green_model(monkeypatch, tmp_path):
    operations = []

    def worker(operation, _model_path, payload=None, *, timeout):
        assert payload is not None
        operations.append((operation, payload.get("selector"), timeout))
        if operation == "backtest":
            return _green_backtest(payload["selector"])
        assert operation == "bfs"
        return {"output": "bfs ran"}

    with _service(
        tmp_path,
        experimental_tooling=False,
        backtest_timeout=12,
        bfs_timeout=34,
    ) as service:
        _install_stub_model(service)
        monkeypatch.setattr(service, "_run_model_worker", worker)
        assert service.run_bfs("advance", [], max_depth=1) == "bfs ran"

    assert operations == [
        ("backtest", "all", 12),
        ("backtest", ALIGNMENT_BACKTEST_SELECTOR, 12),
        ("bfs", None, 34),
    ]


def test_model_without_is_goal_green_backtest_commits_but_no_bfs(
    monkeypatch,
    tmp_path,
):
    backtest_selectors = []

    def worker(operation, _model_path, payload=None, *, timeout):
        assert payload is not None
        if operation == "backtest":
            backtest_selectors.append(payload["selector"])
            return _green_backtest(payload["selector"])
        assert operation == "bfs"
        return {"error": "no_is_goal"}

    with _service(tmp_path, experimental_tooling=False) as service:
        _install_stub_model(
            service,
            "def step(grid, action, x=None, y=None):\n    return grid, {}\n",
        )
        monkeypatch.setattr(service, "_run_model_worker", worker)
        monkeypatch.setattr(service, "_model_session", lambda: _matching_model)

        with pytest.raises(RuntimeError, match="no is_goal"):
            service.run_bfs("advance", [], max_depth=1)
        assert service.commit_actions(
            [{"action": 3}, {"action": 3}],
            "model without goal predicate",
        ) == COMMIT_MESSAGE.format(count=2)
        assert service.last_result is not None
        assert service.last_result.executed == 2

    assert backtest_selectors == ["all", ALIGNMENT_BACKTEST_SELECTOR]


def test_experimental_mode_does_not_duplicate_repair_report(monkeypatch, tmp_path):
    _seed_stubbed_model_surprise(monkeypatch, tmp_path)

    red_report = (
        "backtest [all transitions]: 0/1 transitions fully correct; "
        "1 mismatch(es), repair required"
    )
    backtest_calls = []

    def red_worker(operation, _model_path, payload=None, *, timeout):
        assert operation == "backtest"
        backtest_calls.append(operation)
        return red_report

    with LocusService(
        tmp_path,
        "jail-test",
        "turn-2",
        arcade=FakeArcade(),
        experimental_tooling=True,
    ) as service:
        service._full_history_read = True
        monkeypatch.setattr(service, "_run_model_worker", red_worker)
        output = service.commit_actions(
            [{"action": 3}, {"action": 3}],
            "retry stale model",
        )

    assert output.startswith(MODEL_REPAIR_GATE_MESSAGE + "\n")
    assert output.count(red_report) == 1
    assert locus.MODEL_VALIDATE_GATE_MESSAGE not in output
    assert backtest_calls == ["backtest"]


def test_experimental_repair_gate_without_model_keeps_core_required_gate(
    monkeypatch,
    tmp_path,
):
    model_path = _seed_stubbed_model_surprise(monkeypatch, tmp_path)
    model_path.unlink()

    with LocusService(
        tmp_path,
        "jail-test",
        "turn-2",
        arcade=FakeArcade(),
        experimental_tooling=True,
    ) as service:
        service._full_history_read = True
        output = service.commit_actions(
            [{"action": 3}, {"action": 3}],
            "retry without model",
        )

    assert output.startswith(MODEL_REPAIR_GATE_MESSAGE + "\n")
    assert locus.MODEL_REQUIRED_GATE_MESSAGE in output


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

        metadata_output = service.run_python(
            "from pathlib import Path; "
            f"print([p.name for p in Path({str(tmp_path.parent)!r}).iterdir()])"
        )
        assert outside.name not in metadata_output


def test_process_tools_redact_harness_path_and_use_private_home(tmp_path):
    repo = str(Path(__file__).resolve().parents[1])
    sibling = repo + "0"
    with _service(tmp_path) as service:
        output = service.run_python(
            "from pathlib import Path; "
            f"print({repo!r}); print({sibling!r}); print(Path.home())"
        )

    assert "<harness-repo>" in output
    assert sibling in output
    assert "<harness-repo>0" not in output
    assert f"\n{Path.home()}\n" not in output
    assert str(tmp_path / ".agent_scratch" / "home") in output
    assert (tmp_path / ".agent_scratch" / "home").stat().st_mode & 0o777 == 0o700


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


def test_faithful_core_omits_history_appendix_and_commit_gates(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        LocusService,
        "_new_affordance_topology",
        lambda _service: "new topology",
    )
    with _service(tmp_path, experimental_tooling=False) as service:
        assert service.commit_actions([{"action": 3}], "probe") == (
            COMMIT_MESSAGE.format(count=1)
        )

    with LocusService(
        tmp_path,
        "jail-test",
        "turn-2",
        turn=2,
        arcade=FakeArcade(),
        experimental_tooling=False,
    ) as service:
        output = service.read_history(detail="full")

    assert "Inspector:" not in output
    assert "Model gate:" not in output
    assert "click target proposals" not in output
    assert "Cross-transition" not in output


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
    ("actions", "expected"),
    [
        ([], COMMIT_MESSAGE.format(count=0)),
        # Mixed RESET bypasses the experimental topology gate, but not the core gate.
        ([{"action": 0}, {"action": 3}], None),
    ],
)
def test_cross_transition_gate_exempts_empty_and_reset_first_queues(
    monkeypatch,
    tmp_path,
    actions,
    expected,
):
    monkeypatch.setattr(
        LocusService,
        "_new_affordance_topology",
        lambda _service: "new topology",
    )
    if expected is None:
        expected = locus.MODEL_REQUIRED_GATE_MESSAGE
    with _service(tmp_path) as service:
        assert service.commit_actions(actions, "reset or stop") == expected


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


def test_model_repair_gate_is_transactional_and_auto_certifies_repair(tmp_path):
    _seed_non_reset_model_surprise(tmp_path)
    events = tmp_path / "events.jsonl"
    with LocusService(
        tmp_path,
        "jail-test",
        "turn-2",
        turn=2,
        events_path=events,
        arcade=FakeArcade(),
    ) as service:
        output = service.commit_actions([{"action": 3}], "stale probe")
        assert output.startswith(MODEL_REPAIR_GATE_MESSAGE + "\n")
        assert "backtest [all transitions]: 0/1 transitions fully correct" in output
        assert "; 1 mismatch(es)," in output
        assert service._committed is False
        assert service.last_result is None
        assert len(service.gateway.timeline) == 1
        records = [json.loads(line) for line in events.read_text().splitlines()]
        assert [record["kind"] for record in records] == [
            "tool_started",
            "tool_finished",
        ]
        assert records[-1]["output"] == output
        assert records[-1]["is_error"] is False

        repaired = (
            "def step(grid, action, x=None, y=None):\n"
            "    return [[value + 1 for value in row] for row in grid], {}\n"
        )
        service.write_file("world_model_v2.py", repaired)
        assert service.commit_actions(
            [{"action": 3}, {"action": 3}],
            "repaired plan",
        ) == COMMIT_MESSAGE.format(count=2)
        assert service.last_result is not None
        assert service.last_result.executed == 2

    records = [json.loads(line) for line in events.read_text().splitlines()]
    assert sum(record["kind"] == "turn_committed" for record in records) == 1
    assert sum(record["kind"] == "action_taken" for record in records) == 2


def test_model_repair_gate_checks_fresh_worker_history_alignment(tmp_path):
    surprising = (
        "def init_state(entry_grid):\n"
        "    return {'calls': 0}\n\n"
        "def predict(state, grid, action, x=None, y=None):\n"
        "    calls = state['calls']\n"
        "    predicted = (\n"
        "        [[value + 1 for value in row] for row in grid]\n"
        "        if calls == 0 else grid\n"
        "    )\n"
        "    return predicted, {}, {'calls': calls + 1}\n"
    )
    with _service(tmp_path) as service:
        service.write_file("world_model_v1.py", surprising)
        service.commit_actions(
            [{"action": 3}, {"action": 3}],
            "second action surprises",
        )
        assert service.last_result is not None
        assert service.last_result.executed == 2
        assert service.last_result.halt_reason == "surprise"

    repaired_open_loop = (
        "def init_state(entry_grid):\n"
        "    return {'seen': entry_grid[0][0]}\n\n"
        "def predict(state, grid, action, x=None, y=None):\n"
        "    seen = state['seen'] + 1\n"
        "    predicted = [[seen for _ in row] for row in grid]\n"
        "    return predicted, {}, {'seen': seen}\n\n"
        "def ingest(state, actual_grid):\n"
        "    return {'seen': 0}\n"
    )
    with LocusService(
        tmp_path,
        "jail-test",
        "turn-2",
        arcade=FakeArcade(),
    ) as service:
        service.write_file("world_model_v2.py", repaired_open_loop)
        assert "2/2 transitions fully correct" in service.run_backtest()
        service.read_history(detail="full")
        output = service.commit_actions([{"action": 3}], "retry")

    assert output.startswith(MODEL_REPAIR_GATE_MESSAGE + "\n")
    assert "backtest [full-history alignment]: 1/2" in output
    assert "#1:grid" in output


def test_commit_combines_topology_and_model_repair_gates(monkeypatch, tmp_path):
    _seed_non_reset_model_surprise(tmp_path)
    monkeypatch.setattr(
        LocusService,
        "_new_affordance_topology",
        lambda _service: "new topology",
    )
    with LocusService(
        tmp_path,
        "jail-test",
        "turn-2",
        arcade=FakeArcade(),
    ) as service:
        output = service.commit_actions([{"action": 3}], "stale probe")

    assert output.startswith(
        CROSS_TRANSITION_GATE_MESSAGE + "\n" + MODEL_REPAIR_GATE_MESSAGE + "\n"
    )
    assert "backtest [all transitions]: 0/1 transitions fully correct" in output


def test_model_repair_gate_empty_defers_and_mixed_reset_does_not_bypass(tmp_path):
    _seed_non_reset_model_surprise(tmp_path)
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
        output = service.commit_actions(
            [{"action": 0}, {"action": 3}],
            "mixed reset queue",
        )
        assert output.startswith(MODEL_REPAIR_GATE_MESSAGE + "\n")
        assert RESET_BOUNDARY_GATE_MESSAGE in output
        assert len(service.gateway.timeline) == 1
        assert service.commit_actions([{"action": 0}], "pure reset") == (
            COMMIT_MESSAGE.format(count=1)
        )

    with LocusService(
        tmp_path,
        "jail-test",
        "turn-4",
        arcade=FakeArcade(),
    ) as service:
        assert service.commit_actions([{"action": 3}], "after reset") == (
            COMMIT_MESSAGE.format(count=1)
        )


def test_live_model_requires_reset_to_end_queue_before_real_actions(tmp_path):
    source = (
        "def init_state(entry_grid):\n"
        "    return {'poisoned': False}\n\n"
        "def predict(state, grid, action, x=None, y=None):\n"
        "    predicted = (\n"
        "        grid if state['poisoned'] else "
        "[[value + 1 for value in row] for row in grid]\n"
        "    )\n"
        "    return predicted, {}, {'poisoned': action == 0}\n"
    )
    with _service(tmp_path) as service:
        service.write_file("world_model_v1.py", source)
        output = service.commit_actions(
            [{"action": 0}, {"action": 3}],
            "reset then retry",
        )
        assert output == RESET_BOUNDARY_GATE_MESSAGE
        assert service._committed is False
        assert service.last_result is None
        assert len(service.gateway.timeline) == 0
        assert service.commit_actions([{"action": 0}], "reset only") == (
            COMMIT_MESSAGE.format(count=1)
        )

    with LocusService(
        tmp_path,
        "jail-test",
        "turn-2",
        arcade=FakeArcade(),
    ) as service:
        assert service.commit_actions([{"action": 3}], "retry after reset") == (
            COMMIT_MESSAGE.format(count=1)
        )
        assert service.last_result is not None
        assert service.last_result.halt_reason == "completed"


def test_model_repair_gate_ignores_reset_caused_surprise(tmp_path):
    source = (
        "def step(grid, action, x=None, y=None):\n"
        "    if action == 0:\n"
        "        return grid, {}\n"
        "    return [[value + 1 for value in row] for row in grid], {}\n"
    )
    with _service(tmp_path) as service:
        service.write_file("world_model_v1.py", source)
        service.commit_actions(
            [{"action": 3}, {"action": 0}],
            "second action is a surprising reset",
        )
        assert service.last_result is not None
        assert service.last_result.executed == 2
        assert service.last_result.halt_reason == "surprise"

    with LocusService(
        tmp_path,
        "jail-test",
        "turn-2",
        arcade=FakeArcade(),
    ) as service:
        assert service.commit_actions([{"action": 3}], "probe") == (
            COMMIT_MESSAGE.format(count=1)
        )


def test_model_repair_gate_uses_the_action_that_surprised_in_a_batch(tmp_path):
    _seed_non_reset_model_surprise(tmp_path)
    ledger_path = tmp_path / "runtime" / "turn_ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["turns"]["turn-1"]["actions"] = [[0], [3]]
    ledger["turns"]["turn-1"]["result"]["executed"] = 2
    ledger_path.write_text(json.dumps(ledger) + "\n", encoding="utf-8")

    with LocusService(
        tmp_path,
        "jail-test",
        "turn-2",
        arcade=FakeArcade(),
    ) as service:
        service.read_history(detail="full")
        output = service.commit_actions([{"action": 3}], "retry")

    assert output.startswith(MODEL_REPAIR_GATE_MESSAGE + "\n")
    assert "backtest [all transitions]: 0/1 transitions fully correct" in output
    assert "#0:grid" in output


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
