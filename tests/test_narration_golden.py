from __future__ import annotations

from schema_harness.gateway import ExecutionResult
from schema_harness.narration import (
    COMPLETED_REASON,
    DEATH_REASON,
    MISPREDICTION_REASON,
    NO_WORLD_MODEL_REASON,
    action_list_text,
    commit_result_narration,
    halt_reason_text,
    surprise_message,
    world_model_line,
)


def _result(reason, *, start_level=1, end_level=1, end_state="NOT_FINISHED", surprise=""):
    return ExecutionResult(
        committed=4,
        executed=1,
        halt_reason=reason,
        start_level=start_level,
        end_level=end_level,
        start_state="NOT_FINISHED",
        end_state=end_state,
        surprise=surprise,
    )


def test_action_list_and_death_commit_narration_are_verbatim():
    plan = [
        {"action": 6, "x": 39, "y": 33},
        {"action": 3},
        {"action": 6, "x": 33, "y": 33},
        {"action": 6, "x": 33, "y": 33},
    ]
    assert action_list_text(plan) == "6@39,33 3 6@33,33 6@33,33"
    assert commit_result_narration(
        plan,
        _result("dead", end_state="GAME_OVER"),
        "retry probe",
    ) == (
        "Result of your last commit: committed 4 action(s) "
        "[6@39,33 3 6@33,33 6@33,33] — executed 1; stopped because you DIED "
        "(game over) — RESET to retry the level. Net: level 1→1, state "
        'NOT_FINISHED→GAME_OVER. Your stated intent was: "retry probe"'
    )


def test_every_halt_reason_mapping_is_verbatim():
    assert halt_reason_text(_result("no-world-model-single-step")) == NO_WORLD_MODEL_REASON
    assert halt_reason_text(_result("surprise", surprise=surprise_message(3))) == (
        MISPREDICTION_REASON
    )
    assert halt_reason_text(_result("dead", end_state="GAME_OVER")) == DEATH_REASON
    assert halt_reason_text(_result("level_up", start_level=2, end_level=3)) == (
        "you cleared a level (advanced 2→3)"
    )
    assert halt_reason_text(_result("completed")) == COMPLETED_REASON


def test_surprise_and_world_model_lines_are_verbatim():
    assert surprise_message(6, 12, 34) == (
        "world model MISPREDICTED the step just taken (action 6 @(12,34)); the rest "
        "of the committed plan was dropped. Run run_backtest to see the mismatch and "
        "fix the model before planning again."
    )
    assert surprise_message(3) == (
        "world model MISPREDICTED the step just taken (action 3); the rest of the "
        "committed plan was dropped. Run run_backtest to see the mismatch and fix the "
        "model before planning again."
    )
    assert world_model_line(True, 26) == (
        "World model: installed; history: 26 transitions."
    )
    assert world_model_line(False, 0) == (
        "World model: NONE yet; history: 0 transitions."
    )
