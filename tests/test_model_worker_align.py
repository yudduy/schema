from __future__ import annotations

import os
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

import schema_harness.model_worker as model_worker
from schema_harness.locus import COMMIT_MESSAGE, LocusService


def _load_model(tmp_path: Path, source: str) -> ModuleType:
    path = tmp_path / "world_model.py"
    path.write_text(source, encoding="utf-8")
    return model_worker.load_model(path)


def _transition(
    action: int,
    grid: list[list[int]],
    *,
    level: int = 0,
) -> dict[str, object]:
    return {
        "action": action,
        "x": None,
        "y": None,
        "grid": grid,
        "level": level,
        "level_up": False,
        "dead": False,
        "win": False,
    }


def _history(*actions: dict[str, object]) -> dict[str, object]:
    return {
        "initial_turn": {"grid": [[0]], "level": 0, "win_levels": 1},
        "actions": list(actions),
    }


def test_align_model_propagates_predict_crash(tmp_path):
    model = _load_model(
        tmp_path,
        "def init_state(entry_grid):\n"
        "    return 0\n\n"
        "def predict(state, grid, action, x=None, y=None):\n"
        "    if action == 4:\n"
        "        raise RuntimeError('predict alignment crash')\n"
        "    predicted = [[value + 1 for value in row] for row in grid]\n"
        "    return predicted, {}, state + 1\n",
    )
    history = _history(
        _transition(3, [[1]]),
        _transition(4, [[2]]),
    )

    with pytest.raises(RuntimeError, match="predict alignment crash"):
        model_worker._align_model(model, history)


def test_align_model_propagates_ingest_crash(tmp_path):
    model = _load_model(
        tmp_path,
        "def init_state(entry_grid):\n"
        "    return 0\n\n"
        "def predict(state, grid, action, x=None, y=None):\n"
        "    predicted = [[value + 1 for value in row] for row in grid]\n"
        "    return predicted, {}, state + 1\n\n"
        "def ingest(state, actual_grid):\n"
        "    if actual_grid == [[2]]:\n"
        "        raise ValueError('ingest alignment crash')\n"
        "    return state\n",
    )
    history = _history(
        _transition(3, [[1]]),
        _transition(4, [[2]]),
    )

    with pytest.raises(ValueError, match="ingest alignment crash"):
        model_worker._align_model(model, history)


def test_align_model_reset_transitions_still_reinit(tmp_path):
    model = _load_model(
        tmp_path,
        "_INIT_CALLS = []\n\n"
        "def init_state(entry_grid):\n"
        "    _INIT_CALLS.append((CURRENT_LEVEL, [row[:] for row in entry_grid]))\n"
        "    return entry_grid[0][0]\n\n"
        "def predict(state, grid, action, x=None, y=None):\n"
        "    assert action != 0\n"
        "    next_state = state + 1\n"
        "    return [[next_state]], {}, next_state\n",
    )
    history = _history(
        _transition(3, [[1]]),
        _transition(0, [[10]], level=1),
        _transition(3, [[11]], level=1),
    )

    assert model_worker._align_model(model, history) == (11, 1, [[11]])
    assert model._INIT_CALLS == [(0, [[0]]), (1, [[10]])]
    assert model.CURRENT_LEVEL == 1


class _FakeEnvironment:
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


class _FakeArcade:
    def __init__(self) -> None:
        self.environment = _FakeEnvironment()

    def make(self, game, seed):
        assert game == "jail-test"
        assert seed == 0
        assert os.environ["ONLY_RESET_LEVELS"] == "true"
        return self.environment


def test_commit_with_crashing_model_halts_nondeterministic(tmp_path):
    """Native sandbox integration: restricted runners may fail at sandbox_apply."""

    with LocusService(
        tmp_path,
        "jail-test",
        "turn-1",
        arcade=_FakeArcade(),
        experimental_tooling=False,
    ) as service:
        assert service.commit_actions([{"action": 3}], "seed history") == (
            COMMIT_MESSAGE.format(count=1)
        )

    source = (
        "def init_state(entry_grid):\n"
        "    return None\n\n"
        "def predict(state, grid, action, x=None, y=None):\n"
        "    if grid[0][0] == 0:\n"
        "        raise RuntimeError('historical alignment crash')\n"
        "    predicted = [[value + 1 for value in row] for row in grid]\n"
        "    return predicted, {}, state\n"
    )
    with LocusService(
        tmp_path,
        "jail-test",
        "turn-2",
        arcade=_FakeArcade(),
        experimental_tooling=False,
    ) as service:
        service.write_file("world_model_v1.py", source)
        assert service.commit_actions([{"action": 3}], "exercise alignment") == (
            COMMIT_MESSAGE.format(count=1)
        )
        assert service.last_result is not None
        assert service.last_result.halt_reason == "nondeterministic-model"
        assert service.last_result.executed == 0
        assert len(service.gateway.timeline) == 1
