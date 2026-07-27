from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from schema_harness.events import EventLog
from schema_harness.gateway import Gateway, WorldModelPrediction
from schema_harness.narration import (
    NO_WORLD_MODEL_REASON,
    commit_result_narration,
    surprise_message,
)
from schema_harness.replay_verify import execute_commit, load_released_trace
from spikes import replay_parity


REPO_ROOT = Path(__file__).resolve().parents[1]
BP35_EVENTS = REPO_ROOT / "vendor" / "bp35_events.jsonl"


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
            available_actions=[3, 4, 6],
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
        assert os.environ["ONLY_RESET_LEVELS"] == "true"
        assert game == "bp35-test"
        assert seed == 0
        return self.environment


@pytest.mark.parametrize("cache_source", ["absolute", "home", "repository", "cwd"])
def test_gateway_and_replay_resolve_environment_cache_identically(
    cache_source, tmp_path, monkeypatch
):
    repository_environments = REPO_ROOT / "environment_files"
    original_is_dir = Path.is_dir
    work_cwd = tmp_path / "cwd"
    work_cwd.mkdir()
    monkeypatch.chdir(work_cwd)

    if cache_source == "absolute":
        expected = tmp_path / "absolute-cache"
        monkeypatch.setenv("SCHEMA_ENVIRONMENTS_DIR", str(expected))
    elif cache_source == "home":
        home = tmp_path / "home"
        home.mkdir()
        expected = home / "configured-cache"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("SCHEMA_ENVIRONMENTS_DIR", "~/configured-cache")
    elif cache_source == "repository":
        expected = repository_environments
        monkeypatch.delenv("SCHEMA_ENVIRONMENTS_DIR", raising=False)
        monkeypatch.setattr(
            Path,
            "is_dir",
            lambda path: True
            if path == repository_environments
            else original_is_dir(path),
        )
    else:
        expected = Path("environment_files")
        monkeypatch.delenv("SCHEMA_ENVIRONMENTS_DIR", raising=False)
        monkeypatch.setattr(
            Path,
            "is_dir",
            lambda path: False
            if path == repository_environments
            else original_is_dir(path),
        )

    constructed = []

    class RecordingArcade:
        def __init__(self, *, operation_mode=None, environments_dir):
            constructed.append((operation_mode, environments_dir))
            self.environment = FakeEnvironment()

        def make(self, _game, seed):
            assert seed == 0
            return self.environment

    monkeypatch.setattr(replay_parity.arc_agi, "Arcade", RecordingArcade)

    Gateway("path-test")
    replay_parity._open_env("path-test", offline=True)
    replay_parity._open_env("path-test", offline=False)

    assert [Path(environments_dir) for _, environments_dir in constructed] == [
        expected,
        expected,
        expected,
    ]


def test_gateway_falls_back_online_while_replay_offline_is_fail_closed(monkeypatch):
    constructed_modes = []
    make_results = [None, FakeEnvironment()]

    class RecordingArcade:
        def __init__(self, *, operation_mode=None, environments_dir):
            assert environments_dir
            constructed_modes.append(operation_mode)

        def make(self, _game, seed):
            assert seed == 0
            return make_results.pop(0)

    monkeypatch.setattr(replay_parity.arc_agi, "Arcade", RecordingArcade)
    Gateway("path-test")

    assert constructed_modes == [replay_parity.arc_agi.OperationMode.OFFLINE, None]

    constructed_modes.clear()
    make_results.append(None)
    assert replay_parity._open_env("path-test", offline=True) is None
    assert constructed_modes == [replay_parity.arc_agi.OperationMode.OFFLINE]


def test_no_world_model_executes_exactly_one_action(monkeypatch):
    monkeypatch.delenv("ONLY_RESET_LEVELS", raising=False)
    gateway = Gateway("bp35-test", arcade=FakeArcade())

    result = gateway.execute_queue([[3, None, None], [4, None, None]])

    assert result.executed == 1
    assert result.halt_reason == "no-world-model-single-step"
    assert [transition.action for transition in gateway.timeline] == [3]
    assert NO_WORLD_MODEL_REASON in commit_result_narration(
        [[3, None, None], [4, None, None]], result, "probe"
    )


def test_first_model_mismatch_emits_action_then_surprise(tmp_path, monkeypatch):
    monkeypatch.delenv("ONLY_RESET_LEVELS", raising=False)
    with EventLog(tmp_path / "events.jsonl", clock=iter(range(10)).__next__) as event_log:
        gateway = Gateway("bp35-test", event_log=event_log, arcade=FakeArcade())
        result = gateway.execute_queue(
            [[3, None, None], [4, None, None]],
            live_model=lambda *_: WorldModelPrediction(grid=[[0, 0], [0, 0]]),
            turn=7,
        )

    payloads = [
        json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    assert result.executed == 1
    assert result.halt_reason == "surprise"
    assert [payload["kind"] for payload in payloads] == [
        "action_taken",
        "model_mispredicted",
    ]
    assert payloads[-1]["surprise"] == surprise_message(3)
    assert surprise_message(6, 12, 34) == (
        "world model MISPREDICTED the step just taken (action 6 @(12,34)); the rest "
        "of the committed plan was dropped. Run run_backtest to see the mismatch and "
        "fix the model before planning again."
    )


def test_two_consecutive_resets_keep_current_bp35_level(monkeypatch):
    monkeypatch.delenv("ONLY_RESET_LEVELS", raising=False)
    trace = load_released_trace(BP35_EVENTS)
    gateway = Gateway(str(trace.run_started["game_id"]))
    for commit in trace.commits:
        execute_commit(gateway, commit)
        if gateway.level == 1:
            break

    assert gateway.level == 1
    first = gateway.execute_queue([[0, None, None]], turn=8)
    second = gateway.execute_queue([[0, None, None]], turn=9)

    assert os.environ["ONLY_RESET_LEVELS"] == "true"
    assert first.end_level == second.start_level == second.end_level == 1
    assert [transition.action for transition in gateway.timeline[-2:]] == [0, 0]
