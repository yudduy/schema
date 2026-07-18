from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from types import ModuleType
from typing import Any

from schema_harness.bfs import run_bfs


def _stateful_model(predict: Callable[..., Any]) -> ModuleType:
    model = ModuleType("test_bfs_model")
    model.init_state = lambda _grid: ()
    model.predict = predict
    return model


def test_bfs_distributes_a_tight_node_budget_across_the_depth_frontier():
    calls: list[tuple[tuple, int, int | None, int | None]] = []

    def predict(state, grid, action, x=None, y=None):
        calls.append((state, action, x, y))
        reached = state == ((6, 0, 0),)
        next_state = (*state, (action, x, y))
        return grid, {"level_up": reached}, next_state

    report = run_bfs(
        _stateful_model(predict),
        (),
        [[0]],
        actions=(1,),
        click_targets=tuple((x, 0) for x in range(32)),
        max_nodes=36,
        max_depth=2,
    )

    assert report.found
    assert report.plan == [
        {"action": 6, "x": 0, "y": 0},
        {"action": 1},
    ]
    assert report.expanded == len(calls) == 36
    assert report.distinct_states == 37


def test_bfs_checks_goal_flags_before_discarding_a_duplicate_state():
    def predict(_state, grid, action, x=None, y=None):
        return grid, {"level_up": action == 2}, ("same",)

    report = run_bfs(
        _stateful_model(predict),
        (),
        [[0]],
        actions=(1, 2),
        max_nodes=2,
        max_depth=1,
    )

    assert report.found
    assert report.plan == [{"action": 2}]
    assert report.expanded == 2
    assert report.distinct_states == 2


def test_bfs_covers_each_layer_edge_once_and_keeps_reset_at_the_root():
    calls: list[tuple[tuple, tuple[int, int | None, int | None]]] = []

    def predict(state, grid, action, x=None, y=None):
        token = (action, x, y)
        calls.append((state, token))
        return grid, {}, (*state, token)

    report = run_bfs(
        _stateful_model(predict),
        (),
        [[0]],
        actions=(1, 2),
        click_targets=((3, 0),),
        max_nodes=16,
        max_depth=2,
    )

    root_tokens = [token for state, token in calls if not state]
    assert root_tokens == [
        (1, None, None),
        (2, None, None),
        (6, 3, 0),
        (0, None, None),
    ]
    layer_edges = [(state[0], token) for state, token in calls if state]
    assert len(layer_edges) == len(set(layer_edges)) == 12
    assert Counter(parent for parent, _ in layer_edges) == Counter(
        {token: 3 for token in root_tokens}
    )
    assert Counter(token for _, token in layer_edges) == Counter(
        {(1, None, None): 4, (2, None, None): 4, (6, 3, 0): 4}
    )
    assert all(token[0] != 0 for _, token in layer_edges)
    assert report.expanded == 16
    assert report.distinct_states == 17
