"""Timeline reconstruction and deterministic world-model backtesting."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np

from .events import Grid
from .model_loader import call_init_state, call_predict, set_current_level


_REPORT_CLAUSE = (
    "grid on non-terminal steps + level_up/dead/win flags on EVERY step"
)
_MISMATCH_KINDS = ("grid", "level_up", "dead", "win")
# Internal repair-gate mode; public selectors and report strings stay unchanged.
ALIGNMENT_BACKTEST_SELECTOR = "__full_history_alignment__"


@dataclass(frozen=True, slots=True)
class BacktestMismatch:
    index: int
    kinds: tuple[str, ...]
    error: str = ""

    @property
    def kind(self) -> str:
        return self.kinds[0]


@dataclass(frozen=True, slots=True)
class BacktestReport:
    selector: str
    correct: int
    checked: int
    mismatches: int
    skipped: int
    details: tuple[BacktestMismatch, ...]
    scope: str

    @property
    def mismatch_count(self) -> int:
        return self.mismatches

    @property
    def transitions(self) -> int:
        return self.checked

    @property
    def fully_correct(self) -> int:
        return self.correct

    @property
    def ok(self) -> bool:
        return self.mismatches == 0

    def __str__(self) -> str:
        prefix = (
            f"backtest {self.selector}: {self.correct}/{self.checked} transitions fully "
            f"correct ({_REPORT_CLAUSE}); {self.mismatches} mismatch(es), "
            f"{self.skipped} skipped (resets / no prior grid). "
        )
        if self.ok:
            return prefix + f"Model predicts ALL checkable transitions in {self.scope}"
        failures = ", ".join(
            f"#{detail.index}:{kind}"
            for detail in self.details
            for kind in detail.kinds
        )
        return prefix + f"Mismatched transitions (index:error-kind): {failures}"

    @property
    def output(self) -> str:
        return str(self)


@dataclass(frozen=True, slots=True)
class _RecordedTransition:
    index: int
    action: int
    x: int | None
    y: int | None
    grid: Grid
    level: int
    level_up: bool
    dead: bool
    win: bool

    @property
    def terminal(self) -> bool:
        return self.level_up or self.dead or self.win


@dataclass(frozen=True, slots=True)
class _Timeline:
    transitions: tuple[_RecordedTransition, ...]
    initial_grid: Grid | None
    initial_level: int | None
    win_levels: int | None


@dataclass(frozen=True, slots=True)
class _Selection:
    indices: frozenset[int]
    label: str
    scope: str


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
            if not isinstance(event, dict):
                raise ValueError(f"{path}:{line_number}: event must be an object")
            events.append(event)
    return events


def _timeline_source(timeline: Any) -> tuple[list[Any], Mapping[str, Any] | None]:
    if isinstance(timeline, (str, PathLike, Path)):
        return _read_jsonl(Path(timeline)), None
    initial = getattr(timeline, "initial_turn", None)
    actions = getattr(timeline, "actions", None)
    if actions is not None:
        return list(actions), initial if isinstance(initial, Mapping) else None
    if isinstance(timeline, Mapping) and "actions" in timeline:
        raw_initial = timeline.get("initial_turn")
        return list(timeline["actions"]), raw_initial if isinstance(raw_initial, Mapping) else None
    if isinstance(timeline, Iterable):
        return list(timeline), None
    raise TypeError("timeline must be a JSONL path or an iterable of transition records")


def _bool_field(record: Any, name: str) -> bool:
    value = record.get(name, False) if isinstance(record, Mapping) else getattr(record, name)
    if type(value) is not bool:
        raise ValueError(f"transition {name} must be bool")
    return value


def _recorded_transition(record: Any, position: int) -> _RecordedTransition:
    if isinstance(record, Mapping):
        field = record.get
        index = field("step_index", position)
        action = field("action")
        x = field("x")
        y = field("y")
        grid = field("grid")
        level = field("level")
    else:
        index = getattr(record, "step_index", position)
        action = getattr(record, "action")
        x = getattr(record, "x", None)
        y = getattr(record, "y", None)
        grid = getattr(record, "grid")
        level = getattr(record, "level")
    if type(index) is not int or type(action) is not int or type(level) is not int:
        raise ValueError("transition step_index, action, and level must be integers")
    array = np.asarray(grid)
    if array.ndim != 2 or not np.issubdtype(array.dtype, np.integer):
        raise ValueError(f"transition #{index} has an invalid grid")
    return _RecordedTransition(
        index=index,
        action=action,
        x=x,
        y=y,
        grid=array.astype(int, copy=False).tolist(),
        level=level,
        level_up=_bool_field(record, "level_up"),
        dead=_bool_field(record, "dead"),
        win=_bool_field(record, "win"),
    )


def _load_timeline(timeline: Any) -> _Timeline:
    records, supplied_initial = _timeline_source(timeline)
    initial = supplied_initial
    if initial is None:
        initial = next(
            (
                record
                for record in records
                if isinstance(record, Mapping)
                and record.get("kind") == "turn_started"
                and "grid" in record
            ),
            None,
        )
    action_records = [
        record
        for record in records
        if not isinstance(record, Mapping)
        or record.get("kind") in (None, "action_taken")
    ]
    transitions = tuple(
        _recorded_transition(record, position)
        for position, record in enumerate(action_records)
    )
    indices = [transition.index for transition in transitions]
    if len(indices) != len(set(indices)):
        raise ValueError("timeline contains duplicate step_index values")

    initial_grid: Grid | None = None
    initial_level: int | None = None
    win_levels: int | None = None
    if initial is not None:
        grid = initial.get("grid")
        array = np.asarray(grid)
        if array.ndim != 2 or not np.issubdtype(array.dtype, np.integer):
            raise ValueError("initial turn_started event has an invalid grid")
        initial_grid = array.astype(int, copy=False).tolist()
        level = initial.get("level")
        initial_level = level if type(level) is int else None
        raw_win_levels = initial.get("win_levels")
        win_levels = raw_win_levels if type(raw_win_levels) is int else None
    return _Timeline(transitions, initial_grid, initial_level, win_levels)


def _normalize_requested_index(index: int, transitions: Sequence[_RecordedTransition]) -> int:
    if type(index) is not int:
        raise ValueError("selector indices must be integers")
    if index >= 0:
        return index
    try:
        return transitions[index].index
    except IndexError as exc:
        raise ValueError(f"selector index {index} is out of range") from exc


def _selection(selector: Any, transitions: Sequence[_RecordedTransition]) -> _Selection:
    available = {transition.index for transition in transitions}
    if selector is None or (isinstance(selector, str) and selector.strip() in {"all", "all transitions", "[all transitions]"}):
        indices = frozenset(available)
        scope = (
            f"#{transitions[0].index}..#{transitions[-1].index}"
            if transitions
            else "empty scope"
        )
        return _Selection(indices, "[all transitions]", scope)

    requested: list[int]
    label: str
    scope: str
    if isinstance(selector, range):
        requested = [_normalize_requested_index(index, transitions) for index in selector]
        label = f"[indices {requested}]"
        scope = f"indices {requested}"
    elif isinstance(selector, Sequence) and not isinstance(selector, (str, bytes)):
        requested = [_normalize_requested_index(index, transitions) for index in selector]
        label = f"[indices {requested}]"
        scope = f"indices {requested}"
    elif isinstance(selector, str):
        value = selector.strip()
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1].strip()
        range_match = re.fullmatch(r"(?:range\s+)?#?(-?\d+)\.\.#?(-?\d+)", value)
        indices_match = re.fullmatch(r"indices\s*\[(.*)\]", value)
        if range_match:
            start = _normalize_requested_index(int(range_match.group(1)), transitions)
            end = _normalize_requested_index(int(range_match.group(2)), transitions)
            if end < start:
                raise ValueError("selector range end must not precede its start")
            requested = list(range(start, end + 1))
            label = f"[range #{start}..#{end}]"
            scope = f"#{start}..#{end}"
        elif indices_match:
            body = indices_match.group(1).strip()
            raw_indices = [] if not body else [part.strip() for part in body.split(",")]
            try:
                requested = [
                    _normalize_requested_index(int(index), transitions)
                    for index in raw_indices
                ]
            except ValueError as exc:
                raise ValueError(f"invalid indices selector: {selector!r}") from exc
            label = f"[indices {requested}]"
            scope = f"indices {requested}"
        else:
            raise ValueError(f"unsupported backtest selector: {selector!r}")
    else:
        raise TypeError("selector must be 'all', a range selector, or a sequence of indices")

    missing = sorted(set(requested) - available)
    if missing:
        raise ValueError(f"selector references missing transition indices: {missing}")
    return _Selection(frozenset(requested), label, scope)


def _actual_flags(transition: _RecordedTransition) -> dict[str, bool]:
    return {
        "level_up": transition.level_up,
        "dead": transition.dead,
        "win": transition.win,
    }


def run_backtest(
    model: ModuleType,
    timeline: Any,
    *,
    selector: Any = "all",
) -> BacktestReport:
    """Backtest a model against selected transitions while threading full history state."""

    history = _load_timeline(timeline)
    align_to_actual = (
        isinstance(selector, str)
        and selector == ALIGNMENT_BACKTEST_SELECTOR
    )
    selection = _selection(
        "all" if align_to_actual else selector,
        history.transitions,
    )
    if align_to_actual:
        selection = _Selection(
            selection.indices,
            "[full-history alignment]",
            selection.scope,
        )
    if not history.transitions:
        return BacktestReport(selection.label, 0, 0, 0, 0, (), selection.scope)

    first = history.transitions[0]
    segment_level = history.initial_level
    if segment_level is None:
        segment_level = first.level - int(first.level_up)
    previous_grid = history.initial_grid
    state: Any = None
    if previous_grid is not None:
        set_current_level(model, segment_level)
        state = call_init_state(model, previous_grid)

    checked = 0
    skipped = 0
    mismatches: list[BacktestMismatch] = []

    for transition in history.transitions:
        selected = transition.index in selection.indices
        before_grid = previous_grid

        if transition.action == 0 or before_grid is None:
            if selected:
                skipped += 1
            segment_level = transition.level
            set_current_level(model, segment_level)
            state = call_init_state(model, transition.grid)
            previous_grid = transition.grid
            continue

        if state is None:
            set_current_level(model, segment_level)
            state = call_init_state(model, before_grid)

        set_current_level(model, segment_level)
        prediction_error = ""
        try:
            predicted_grid, predicted_flags, next_state = call_predict(
                model,
                state,
                before_grid,
                transition.action,
                transition.x,
                transition.y,
            )
        except Exception as exc:  # A model failure is data in a backtest, not a harness crash.
            predicted_grid = []
            predicted_flags = {}
            next_state = None
            prediction_error = f"{type(exc).__name__}: {exc}"

        if selected:
            checked += 1
            kinds: list[str] = []
            if prediction_error:
                kinds.append("raised")
            else:
                if not transition.terminal and not np.array_equal(
                    np.asarray(predicted_grid), np.asarray(transition.grid)
                ):
                    kinds.append("grid")
                actual_flags = _actual_flags(transition)
                kinds.extend(
                    name
                    for name in ("level_up", "dead", "win")
                    if predicted_flags[name] != actual_flags[name]
                )
            if kinds:
                mismatches.append(
                    BacktestMismatch(transition.index, tuple(kinds), prediction_error)
                )

        state = next_state
        if prediction_error:
            set_current_level(model, segment_level)
            state = call_init_state(model, transition.grid)
        elif align_to_actual:
            ingest = getattr(model, "ingest", None)
            if callable(ingest):
                try:
                    ingested = ingest(state, transition.grid)
                    if ingested is not None:
                        state = ingested
                except Exception as exc:
                    if selected and not any(
                        mismatch.index == transition.index
                        for mismatch in mismatches
                    ):
                        mismatches.append(
                            BacktestMismatch(
                                transition.index,
                                ("ingest",),
                                f"ingest: {type(exc).__name__}: {exc}",
                            )
                        )
                    set_current_level(model, segment_level)
                    state = call_init_state(model, transition.grid)
        if transition.level_up:
            segment_level = transition.level
            set_current_level(model, segment_level)
            state = call_init_state(model, transition.grid)
        previous_grid = transition.grid

    mismatch_count = len(mismatches)
    return BacktestReport(
        selector=selection.label,
        correct=checked - mismatch_count,
        checked=checked,
        mismatches=mismatch_count,
        skipped=skipped,
        details=tuple(mismatches),
        scope=selection.scope,
    )


__all__ = [
    "ALIGNMENT_BACKTEST_SELECTOR",
    "BacktestMismatch",
    "BacktestReport",
    "run_backtest",
]
