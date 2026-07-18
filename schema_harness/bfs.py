"""Bounded breadth-first planning over an in-process Schema world model."""

from __future__ import annotations

import copy
import hashlib
import pickle
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import numpy as np

from .events import Grid
from .gateway import QueuedAction
from .model_loader import TERMINAL_FLAGS, call_predict


@dataclass(slots=True)
class BfsReport:
    found: bool
    goal: str | None
    plan: list[dict[str, int]]
    expanded: int
    distinct_states: int
    actions: tuple[int, ...]
    click_count: int
    flags: dict[str, bool]
    max_depth: int
    max_nodes: int

    @property
    def steps(self) -> int:
        return len(self.plan)

    @property
    def reached_goal(self) -> bool:
        return self.found

    @property
    def goal_flag(self) -> str | None:
        return self.goal

    def __str__(self) -> str:
        search_space = (
            f"actions={list(self.actions)} + {self.click_count} click(s) + "
            "RESET-first option"
        )
        if not self.found:
            return (
                f"BFS: no goal within depth {self.max_depth}; expanded {self.expanded} nodes, "
                f"{self.distinct_states} distinct states ({search_space})."
            )
        return (
            f"BFS: goal in {self.steps} step(s) via {self.goal}; expanded "
            f"{self.expanded} nodes, {self.distinct_states} distinct states "
            f"({search_space}). Plan (-> commit_actions): {self.plan}"
        )

    @property
    def output(self) -> str:
        return str(self)


@dataclass(slots=True)
class _Node:
    state: Any
    grid: Grid
    plan: list[dict[str, int]]


def _state_digest(state: Any) -> bytes:
    try:
        payload = pickle.dumps(state, protocol=5)
    except (pickle.PickleError, TypeError, AttributeError):
        payload = repr(state).encode("utf-8", errors="backslashreplace")
    return hashlib.blake2b(payload, digest_size=16).digest()


def _node_key(grid: Grid, state: Any) -> tuple[tuple[int, ...], bytes, bytes]:
    array = np.asarray(grid)
    if array.ndim != 2 or not np.issubdtype(array.dtype, np.integer):
        raise TypeError("BFS grids must be two-dimensional integer arrays")
    normalized = array.astype(np.int64, copy=False)
    return normalized.shape, normalized.tobytes(), _state_digest(state)


def _discrete_actions(actions: Iterable[Any]) -> tuple[QueuedAction, ...]:
    normalized: list[QueuedAction] = []
    seen: set[int] = set()
    for raw in actions:
        action = QueuedAction.parse(raw)
        if action.action in (0, 6):
            raise ValueError("actions must contain only non-RESET discrete actions; use click_targets for action 6")
        if action.action not in seen:
            normalized.append(action)
            seen.add(action.action)
    return tuple(normalized)


def _click_actions(click_targets: Sequence[Sequence[int]]) -> tuple[QueuedAction, ...]:
    clicks: list[QueuedAction] = []
    seen: set[tuple[int, int]] = set()
    for target in click_targets:
        if len(target) != 2:
            raise ValueError("each click target must contain exactly (x, y)")
        click = QueuedAction.parse({"action": 6, "x": target[0], "y": target[1]})
        coordinates = (click.x, click.y)
        if coordinates not in seen:
            clicks.append(click)
            seen.add(coordinates)
    return tuple(clicks)


def _plan_action(action: QueuedAction) -> dict[str, int]:
    planned = {"action": action.action}
    if action.action == 6:
        assert action.x is not None and action.y is not None
        planned.update(x=action.x, y=action.y)
    return planned


def _goal_hit(flags: dict[str, bool], goals: frozenset[str]) -> str | None:
    return next((name for name in TERMINAL_FLAGS if name in goals and flags[name]), None)


def run_bfs(
    model: ModuleType,
    start_state: Any,
    start_grid: Grid,
    *,
    actions: Iterable[Any],
    click_targets: Sequence[Sequence[int]] = (),
    max_nodes: int,
    max_depth: int,
    goal: Iterable[str] = frozenset(("level_up", "win")),
) -> BfsReport:
    """Search model transitions for the shortest flag-reaching action plan."""

    if type(max_nodes) is not int or max_nodes < 0:
        raise ValueError("max_nodes must be a non-negative integer")
    if type(max_depth) is not int or max_depth < 0:
        raise ValueError("max_depth must be a non-negative integer")
    goals = frozenset(goal)
    unknown_goals = goals - set(TERMINAL_FLAGS)
    if not goals or unknown_goals:
        raise ValueError(f"goal must contain terminal flags only, got {sorted(goals)!r}")

    discrete = _discrete_actions(actions)
    clicks = _click_actions(click_targets)
    base_candidates = discrete + clicks
    reset = QueuedAction(0)
    root = _Node(copy.deepcopy(start_state), copy.deepcopy(start_grid), [])
    queue = deque([root])
    seen = {_node_key(root.grid, root.state)}
    expanded = 0

    while queue and expanded < max_nodes:
        node = queue.popleft()
        depth = len(node.plan)
        if depth >= max_depth:
            continue
        candidates = base_candidates + ((reset,) if depth == 0 else ())
        for action in candidates:
            if expanded >= max_nodes:
                break
            expanded += 1
            branch_state = copy.deepcopy(node.state)
            branch_grid = copy.deepcopy(node.grid)
            predicted_grid, flags, next_state = call_predict(
                model,
                branch_state,
                branch_grid,
                action.action,
                action.x,
                action.y,
            )
            plan = [*node.plan, _plan_action(action)]
            key = _node_key(predicted_grid, next_state)
            is_new = key not in seen
            if is_new:
                seen.add(key)

            reached = _goal_hit(flags, goals)
            if reached is not None:
                return BfsReport(
                    found=True,
                    goal=reached,
                    plan=plan,
                    expanded=expanded,
                    distinct_states=len(seen),
                    actions=tuple(action.action for action in discrete),
                    click_count=len(clicks),
                    flags=flags,
                    max_depth=max_depth,
                    max_nodes=max_nodes,
                )
            if any(flags.values()) or not is_new:
                continue
            queue.append(_Node(next_state, predicted_grid, plan))

    return BfsReport(
        found=False,
        goal=None,
        plan=[],
        expanded=expanded,
        distinct_states=len(seen),
        actions=tuple(action.action for action in discrete),
        click_count=len(clicks),
        flags={name: False for name in TERMINAL_FLAGS},
        max_depth=max_depth,
        max_nodes=max_nodes,
    )


__all__ = ["BfsReport", "run_bfs"]
