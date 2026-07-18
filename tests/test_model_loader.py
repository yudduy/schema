from __future__ import annotations

import sys

from schema_harness.model_loader import (
    call_init_state,
    call_predict,
    detect_interface,
    load_model,
    set_current_level,
)


def test_stateful_interface_isolated_modules_and_normalized_calls(tmp_path):
    source = tmp_path / "stateful_model.py"
    source.write_text(
        """
CALLS = 0

def init_state(entry_grid):
    return {"level": CURRENT_LEVEL, "calls": 0}

def predict(state, grid, action, x=None, y=None):
    global CALLS
    CALLS += 1
    next_state = dict(state, calls=state["calls"] + 1)
    return grid, {"level_up": action == 4}, next_state

def is_goal(state):
    return state["calls"] > 2
""",
        encoding="utf-8",
    )
    modules_before = set(sys.modules)

    first = load_model(source)
    second = load_model(source)

    assert set(sys.modules) == modules_before
    assert first is not second
    assert first.__name__ != second.__name__
    interface = detect_interface(first)
    assert (interface.kind, interface.entrypoint, interface.has_is_goal) == (
        "stateful",
        "predict",
        True,
    )
    assert interface.description == "stateful (predict)"

    set_current_level(first, 3)
    state = call_init_state(first, [[1]])
    grid, flags, next_state = call_predict(first, state, [[1]], 4)

    assert state == {"level": 3, "calls": 0}
    assert grid == [[1]]
    assert flags == {"level_up": True, "dead": False, "win": False}
    assert next_state == {"level": 3, "calls": 1}
    assert first.CALLS == 1
    assert second.CALLS == 0


def test_stateless_step_detection_without_is_goal(tmp_path):
    source = tmp_path / "stateless_model.py"
    source.write_text(
        """
def step(grid, action, x=None, y=None):
    return grid, {"dead": action == 7}
""",
        encoding="utf-8",
    )
    model = load_model(source)

    interface = detect_interface(model)
    grid, flags, next_state = call_predict(model, None, [[2]], 7)

    assert (interface.kind, interface.entrypoint, interface.has_is_goal) == (
        "stateless",
        "step",
        False,
    )
    assert call_init_state(model, [[2]]) is None
    assert grid == [[2]]
    assert flags == {"level_up": False, "dead": True, "win": False}
    assert next_state is None
