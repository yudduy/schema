from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np

from schema_harness.bfs import run_bfs
from schema_harness.inspectors import discover_click_targets
from schema_harness.model_loader import (
    call_init_state,
    call_predict,
    load_model,
    set_current_level,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
BP35_EVENTS = REPO_ROOT / "vendor" / "bp35_events.jsonl"
BP35_MODEL = REPO_ROOT / "vendor" / "bp35_world_model_v5.py"


def _bp35_actions():
    return [
        event
        for line in BP35_EVENTS.read_text(encoding="utf-8").splitlines()
        if (event := json.loads(line))["kind"] == "action_taken"
    ]


def _state_before_first_level_up():
    actions = _bp35_actions()
    model = load_model(BP35_MODEL)
    set_current_level(model, 1)

    # Thread L1 (steps 19..64) to the state just before the L1->L2 level-up at #65,
    # using the same rules as the backtest: skip RESET steps (action 0) and re-init from
    # their post-reset grid; skip the grid check on terminal steps. L1 here includes a
    # death at #24 and its auto-RESET at #25.
    boundary = actions[18]
    state = call_init_state(model, boundary["grid"])
    grid = boundary["grid"]
    for transition in actions[19:65]:
        if transition["action"] == 0:  # RESET: not a predictable step; re-init same level
            state = call_init_state(model, transition["grid"])
            grid = transition["grid"]
            continue
        predicted, flags, state = call_predict(
            model,
            state,
            grid,
            transition["action"],
            transition["x"],
            transition["y"],
        )
        assert flags == {
            "level_up": transition["level_up"],
            "dead": transition["dead"],
            "win": transition["win"],
        }
        terminal = transition["level_up"] or transition["dead"] or transition["win"]
        if not terminal:  # grid compared only on non-terminal steps
            assert np.array_equal(predicted, transition["grid"])
        grid = transition["grid"]
    return model, state, grid


def test_bfs_finds_and_reverified_click_plan_that_levels_up():
    model, state, grid = _state_before_first_level_up()

    report = run_bfs(
        model,
        state,
        grid,
        actions=(3, 4, 7),
        click_targets=((33, 33),),
        max_nodes=20,
        max_depth=1,
    )

    assert report.found
    assert report.goal == "level_up"
    assert report.plan == [{"action": 6, "x": 33, "y": 33}]
    assert "actions=[3, 4, 7] + 1 click(s) + RESET-first option" in str(report)
    assert "Plan (-> commit_actions): [{'action': 6, 'x': 33, 'y': 33}]" in str(report)

    verify_state = copy.deepcopy(state)
    verify_grid = copy.deepcopy(grid)
    reached = {"level_up": False, "dead": False, "win": False}
    for planned in report.plan:
        verify_grid, reached, verify_state = call_predict(
            model,
            verify_state,
            verify_grid,
            planned["action"],
            planned.get("x"),
            planned.get("y"),
        )
    assert reached[report.goal]


def test_discovered_bp35_click_target_finds_the_released_level_up():
    model, state, grid = _state_before_first_level_up()

    targets = discover_click_targets(grid)
    assert [33, 33] in targets
    report = run_bfs(
        model,
        state,
        grid,
        actions=(3, 4, 7),
        click_targets=targets,
        max_nodes=40,
        max_depth=1,
    )

    assert report.found
    assert report.plan == [{"action": 6, "x": 33, "y": 33}]
