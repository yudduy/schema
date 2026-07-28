"""Released Schema event records and a durable append-only JSONL writer."""

from __future__ import annotations

import json
import math
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, ClassVar, TypeAlias


Grid: TypeAlias = list[list[int]]
Plan: TypeAlias = list[list[int | None]]
JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)


class Event:
    """Base class for event payloads; ``EventLog`` supplies common fields."""

    kind: ClassVar[str]


@dataclass(frozen=True, slots=True, kw_only=True)
class RunStarted(Event):
    kind: ClassVar[str] = "run_started"
    game_id: str
    provider: str
    model: str
    max_actions: int
    win_levels: int
    workdir: str
    resumed: bool
    resumed_transitions: int


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnStarted(Event):
    kind: ClassVar[str] = "turn_started"
    turn: int
    env_step: int
    state: str
    level: int
    win_levels: int
    legal: list[int]
    grid: Grid
    has_world_model: bool
    surprise: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TextDelta(Event):
    kind: ClassVar[str] = "text_delta"
    turn: int
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolStarted(Event):
    kind: ClassVar[str] = "tool_started"
    turn: int
    call_id: str
    name: str
    args: JSONValue


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolFinished(Event):
    kind: ClassVar[str] = "tool_finished"
    turn: int
    call_id: str
    name: str
    output: JSONValue
    is_error: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelMispredicted(Event):
    kind: ClassVar[str] = "model_mispredicted"
    turn: int
    step_index: int
    surprise: str
    predicted: Grid
    actual: Grid


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnCommitted(Event):
    kind: ClassVar[str] = "turn_committed"
    turn: int
    plan: Plan
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnFallback(Event):
    kind: ClassVar[str] = "turn_fallback"
    turn: int
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnTelemetry(Event):
    kind: ClassVar[str] = "turn_telemetry"
    turn: int
    session_id: str
    usage: JSONValue
    total_cost_usd: float
    num_turns: int
    is_error: bool
    projected_run_cost_usd: float


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionTaken(Event):
    kind: ClassVar[str] = "action_taken"
    turn: int
    step_index: int
    action: int
    x: int | None
    y: int | None
    grid: Grid
    level_up: bool
    dead: bool
    win: bool
    state: str
    level: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RunFinished(Event):
    kind: ClassVar[str] = "run_finished"
    state: str
    levels: int
    win_levels: int
    actions: int
    transitions: int
    has_world_model: bool


EventRecord: TypeAlias = (
    RunStarted
    | TurnStarted
    | TextDelta
    | ToolStarted
    | ToolFinished
    | ModelMispredicted
    | TurnCommitted
    | TurnFallback
    | TurnTelemetry
    | ActionTaken
    | RunFinished
)

EVENT_TYPES: dict[str, type[EventRecord]] = {
    event_type.kind: event_type
    for event_type in (
        RunStarted,
        TurnStarted,
        TextDelta,
        ToolStarted,
        ToolFinished,
        ModelMispredicted,
        TurnCommitted,
        TurnFallback,
        TurnTelemetry,
        ActionTaken,
        RunFinished,
    )
}


def iter_json_objects(
    path: str | os.PathLike[str],
) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield each nonblank JSON object with its physical source line number."""
    source = Path(path)
    with source.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{source}:{line_number}: invalid JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError(
                    f"{source}:{line_number}: event must be an object"
                )
            yield line_number, payload


class EventLog:
    """Append compact JSON events, flushing and syncing every complete line."""

    def __init__(self, path: str | os.PathLike[str], clock: Callable[[], float]) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._seq = self._last_sequence()
        self._handle = self.path.open("a", encoding="utf-8", newline="")
        self._lock = threading.Lock()

    @property
    def sequence(self) -> int:
        """The last sequence number durably appended by this writer."""

        return self._seq

    def _last_sequence(self) -> int:
        if not self.path.exists():
            return 0
        # Resume from the highest seq on disk. File order is deliberately NOT required:
        # a tool that finishes late can flush its already-allocated event after events
        # with higher seq are written, which loses and duplicates nothing. Demanding
        # strict line order turned that benign interleaving into a crash, and the crashed
        # workdir was then scored as a real result (cost the sp80 and sc25 runs).
        # Duplicates remain fatal — that is a real identity collision a resumed writer
        # would overwrite.
        highest = 0
        seen: set[int] = set()
        for line_number, payload in iter_json_objects(self.path):
            seq = payload.get("seq")
            if type(seq) is not int or seq <= 0:
                raise ValueError(
                    f"{self.path}:{line_number}: seq must be a positive integer"
                )
            if seq in seen:
                raise ValueError(f"{self.path}:{line_number}: duplicate seq {seq}")
            seen.add(seq)
            highest = max(highest, seq)
        return highest

    def emit(self, event: EventRecord | str, /, **payload: Any) -> dict[str, Any]:
        """Append an event record, or construct one by released kind name."""

        if isinstance(event, str):
            try:
                event_type = EVENT_TYPES[event]
            except KeyError as exc:
                raise ValueError(f"unknown event kind: {event!r}") from exc
            record = event_type(**payload)
        else:
            if payload:
                raise TypeError("payload keywords are only valid when emitting by kind")
            if not isinstance(event, Event):
                raise TypeError("event must be an Event record or released kind name")
            record = event
        return self.append(record)

    def append(self, event: EventRecord) -> dict[str, Any]:
        """Append one event as one compact, newline-terminated JSON object."""

        if not isinstance(event, Event):
            raise TypeError("event must be an Event record")
        with self._lock:
            ts = self._clock()
            if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                raise TypeError("clock must return an int or float timestamp")
            if not math.isfinite(ts):
                raise ValueError("clock must return a finite timestamp")

            seq = self._seq + 1
            serialized: dict[str, Any] = {
                "kind": event.kind,
                "seq": seq,
                "ts": ts,
            }
            serialized.update({field.name: getattr(event, field.name) for field in fields(event)})
            line = json.dumps(
                serialized,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ) + "\n"
            self._handle.write(line)
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._seq = seq
            return serialized

    def write(self, event: EventRecord | str, /, **payload: Any) -> dict[str, Any]:
        """Compatibility spelling for :meth:`emit`."""

        return self.emit(event, **payload)

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = [
    "ActionTaken",
    "EVENT_TYPES",
    "Event",
    "EventLog",
    "EventRecord",
    "Grid",
    "ModelMispredicted",
    "Plan",
    "RunFinished",
    "RunStarted",
    "TextDelta",
    "ToolFinished",
    "ToolStarted",
    "TurnCommitted",
    "TurnFallback",
    "TurnStarted",
    "TurnTelemetry",
]
