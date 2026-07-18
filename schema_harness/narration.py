"""Pure renderers for the frozen turn-protocol narration."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


COMMIT_RESULT_TEMPLATE = (
    'Result of your last commit: committed {committed} action(s) [{actions}] — executed '
    '{executed}; stopped because {reason}. Net: level {start_level}→{end_level}, state '
    '{start_state}→{end_state}. Your stated intent was: "{intent}"'
)
NO_WORLD_MODEL_REASON = (
    "no world model to self-check yet, so only this one step ran (exploring)"
)
MISPREDICTION_REASON = (
    "the world model MISPREDICTED this step (see NOTE) — rest of the plan dropped"
)
DEATH_REASON = "you DIED (game over) — RESET to retry the level"
LEVEL_CLEARED_TEMPLATE = "you cleared a level (advanced {start_level}→{end_level})"
COMPLETED_REASON = "ran the whole committed plan"
SURPRISE_CLICK_TEMPLATE = (
    "world model MISPREDICTED the step just taken (action {action} @({x},{y})); the rest "
    "of the committed plan was dropped. Run run_backtest to see the mismatch and fix the "
    "model before planning again."
)
SURPRISE_SIMPLE_TEMPLATE = (
    "world model MISPREDICTED the step just taken (action {action}); the rest of the "
    "committed plan was dropped. Run run_backtest to see the mismatch and fix the model "
    "before planning again."
)


def surprise_message(
    action: int,
    x: int | None = None,
    y: int | None = None,
    *,
    click_template: str = SURPRISE_CLICK_TEMPLATE,
    simple_template: str = SURPRISE_SIMPLE_TEMPLATE,
) -> str:
    """Render the exact click or non-click world-model surprise string."""

    if action == 6:
        if x is None or y is None:
            raise ValueError("click surprise requires x and y")
        return click_template.format(action=action, x=x, y=y)
    return simple_template.format(action=action)


def halt_reason_text(result: Any) -> str:
    """Map an execution result's machine halt reason to protocol narration."""

    if result.halt_reason == "no-world-model-single-step":
        return NO_WORLD_MODEL_REASON
    if result.halt_reason == "surprise":
        return MISPREDICTION_REASON
    if result.halt_reason == "dead":
        return DEATH_REASON
    if result.halt_reason == "level_up":
        return LEVEL_CLEARED_TEMPLATE.format(
            start_level=result.start_level,
            end_level=result.end_level,
        )
    if result.halt_reason == "completed":
        return COMPLETED_REASON
    return str(result.halt_reason)


def _action_id(action: Any) -> int:
    if isinstance(action, int):
        return action
    if isinstance(action, dict):
        return int(action["action"])
    if hasattr(action, "action"):
        return int(action.action)
    return int(action[0])


def _action_coordinates(action: Any) -> tuple[int | None, int | None]:
    if isinstance(action, dict):
        return action.get("x"), action.get("y")
    if hasattr(action, "x") and hasattr(action, "y"):
        return action.x, action.y
    if isinstance(action, Sequence) and not isinstance(action, (str, bytes)):
        return (action[1], action[2]) if len(action) >= 3 else (None, None)
    return None, None


def action_list_text(plan: Sequence[Any]) -> str:
    """Render a committed plan using the released space-separated action syntax."""

    rendered: list[str] = []
    for action in plan:
        action_id = _action_id(action)
        if action_id == 6:
            x, y = _action_coordinates(action)
            if x is None or y is None:
                raise ValueError("click action narration requires x and y")
            rendered.append(f"6@{x},{y}")
        else:
            rendered.append(str(action_id))
    return " ".join(rendered)


def world_model_line(installed: bool, transitions: int) -> str:
    """Render the exact world-model/history status line for a turn prompt."""

    if installed:
        return f"World model: installed; history: {transitions} transitions."
    return f"World model: NONE yet; history: {transitions} transitions."


def commit_result_narration(
    plan: Sequence[Any],
    result: Any,
    intent: str,
    *,
    template: str = COMMIT_RESULT_TEMPLATE,
) -> str:
    """Render the exact mid-session ``Result of your last commit`` line."""

    actions = action_list_text(plan)
    return template.format(
        committed=len(plan),
        actions=actions,
        executed=result.executed,
        reason=halt_reason_text(result),
        start_level=result.start_level,
        end_level=result.end_level,
        start_state=result.start_state,
        end_state=result.end_state,
        intent=intent,
    )


build_commit_result = commit_result_narration
build_surprise = surprise_message


__all__ = [
    "COMMIT_RESULT_TEMPLATE",
    "COMPLETED_REASON",
    "DEATH_REASON",
    "LEVEL_CLEARED_TEMPLATE",
    "MISPREDICTION_REASON",
    "NO_WORLD_MODEL_REASON",
    "SURPRISE_CLICK_TEMPLATE",
    "SURPRISE_SIMPLE_TEMPLATE",
    "action_list_text",
    "build_commit_result",
    "build_surprise",
    "commit_result_narration",
    "halt_reason_text",
    "surprise_message",
    "world_model_line",
]
