from __future__ import annotations

import json
import os
from types import SimpleNamespace

import numpy as np
import pytest
from arcengine import GameAction

from schema_harness.gateway import Gateway, PersistentGateway


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
            available_actions=[3],
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
        assert game == "ledger-test"
        assert seed == 0
        assert os.environ["ONLY_RESET_LEVELS"] == "true"
        return self.environment


def _gateway(tmp_path):
    return PersistentGateway("ledger-test", tmp_path, arcade=FakeArcade())


def test_duplicate_turn_id_returns_prior_result_without_reexecution(tmp_path):
    first = _gateway(tmp_path)
    expected = first.commit("turn-1", [{"action": 3}], "probe", turn=1)
    assert len(first.timeline) == 1

    rebuilt = _gateway(tmp_path)
    duplicate = rebuilt.commit("turn-1", [{"action": 3}], "probe", turn=1)

    assert duplicate == expected
    assert len(rebuilt.timeline) == 1
    assert rebuilt.snapshot.grid == first.snapshot.grid
    assert len((tmp_path / "runtime" / "gateway_timeline.jsonl").read_text().splitlines()) == 1


def test_crash_after_commit_durable_recovers_from_matching_grid_hash(tmp_path):
    crashed = _gateway(tmp_path)

    def fail_after_durable(_turn_id):
        raise RuntimeError("simulated crash")

    crashed._after_commit_durable = fail_after_durable
    with pytest.raises(RuntimeError, match="simulated crash"):
        crashed.commit("turn-1", [{"action": 3}], "probe", turn=1)

    ledger_path = tmp_path / "runtime" / "turn_ledger.json"
    pending = json.loads(ledger_path.read_text())
    assert pending["turns"]["turn-1"]["phase"] == "COMMIT_DURABLE"
    assert not (tmp_path / "runtime" / "gateway_timeline.jsonl").exists()

    recovered = _gateway(tmp_path)
    result = recovered.commit("turn-1", [{"action": 3}], "probe", turn=1)

    assert result.executed == 1
    assert len(recovered.timeline) == 1
    complete = json.loads(ledger_path.read_text())
    assert complete["turns"]["turn-1"]["phase"] == "COMPLETE"
    assert complete["turns"]["turn-1"]["post_grid_hash"] != (
        complete["turns"]["turn-1"]["pre_grid_hash"]
    )


class DeathEnvironment(FakeEnvironment):
    def _frame_with_state(self, state):
        frame = self._frame()
        frame.state = SimpleNamespace(value=state)
        return frame

    def step(self, action, _data=None):
        self.counter += 1
        if action == GameAction.RESET:
            return self._frame_with_state("NOT_FINISHED")
        return self._frame_with_state("GAME_OVER")


class DeathArcade(FakeArcade):
    def __init__(self):
        self.environment = DeathEnvironment()


def test_death_result_reports_pre_reset_state_and_excludes_auto_reset_from_executed():
    gateway = Gateway("ledger-test", arcade=DeathArcade())

    result = gateway.execute_queue([{"action": 3}], turn=4)

    assert result.halt_reason == "dead"
    assert result.executed == 1
    assert result.end_state == "GAME_OVER"
    assert gateway.state == "NOT_FINISHED"
    assert [transition.action for transition in gateway.timeline] == [3, 0]
