"""Deterministic owner and checked action executor for one ARC environment."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NamedTuple, Protocol, TypeAlias, runtime_checkable

import arc_agi
import numpy as np
from arcengine import GameAction

from .events import ActionTaken, EventLog, Grid, ModelMispredicted
from .narration import surprise_message


HaltReason: TypeAlias = Literal[
    "completed",
    "surprise",
    "nondeterministic-model",
    "dead",
    "level_up",
    "no-world-model-single-step",
    "max_actions",
]


@dataclass(frozen=True, slots=True)
class QueuedAction:
    action: int
    x: int | None = None
    y: int | None = None

    @classmethod
    def parse(cls, raw: Any) -> "QueuedAction":
        if isinstance(raw, cls):
            parsed = raw
        elif type(raw) is int:
            parsed = cls(raw)
        elif isinstance(raw, Mapping):
            parsed = cls(raw["action"], raw.get("x"), raw.get("y"))
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            if len(raw) == 1:
                parsed = cls(raw[0])
            elif len(raw) == 3:
                parsed = cls(raw[0], raw[1], raw[2])
            else:
                raise ValueError("action sequences must contain [action] or [action, x, y]")
        else:
            raise TypeError(f"unsupported action value: {raw!r}")
        return parsed._validated()

    def _validated(self) -> "QueuedAction":
        if type(self.action) is not int or not 0 <= self.action <= 7:
            raise ValueError(f"action must be an integer in 0..7, got {self.action!r}")
        if self.action == 6:
            for name, value in (("x", self.x), ("y", self.y)):
                if type(value) is not int or not 0 <= value <= 63:
                    raise ValueError(f"click {name} must be an integer in 0..63")
            return self
        return QueuedAction(self.action)

    def released(self) -> list[int | None]:
        return [self.action, self.x, self.y]


class Transition(NamedTuple):
    step_index: int
    action: int
    x: int | None
    y: int | None
    grid: Grid
    level: int
    state: str
    level_up: bool
    dead: bool
    win: bool


@dataclass(frozen=True, slots=True)
class WorldModelPrediction:
    grid: Grid
    level_up: bool = False
    dead: bool = False
    win: bool = False


@runtime_checkable
class WorldModel(Protocol):
    """Step-1 callable boundary; Step 2 can replace its implementation."""

    def __call__(
        self,
        grid: Grid,
        action: int,
        x: int | None = None,
        y: int | None = None,
    ) -> WorldModelPrediction: ...


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    committed: int
    executed: int
    halt_reason: HaltReason
    start_level: int
    end_level: int
    start_state: str
    end_state: str
    surprise: str = ""

    @property
    def executed_count(self) -> int:
        return self.executed

    @property
    def net_level(self) -> tuple[int, int]:
        return self.start_level, self.end_level

    @property
    def net_state(self) -> tuple[str, str]:
        return self.start_state, self.end_state


class Gateway:
    """Own one local ARC environment and its append-only transition timeline."""

    def __init__(
        self,
        game: str,
        event_log: EventLog | None = None,
        *,
        arcade: Any | None = None,
    ) -> None:
        os.environ["ONLY_RESET_LEVELS"] = "true"
        assert os.environ.get("ONLY_RESET_LEVELS") == "true"

        self.game = game
        self.event_log = event_log
        if arcade is None:
            configured_environments = os.environ.get("SCHEMA_ENVIRONMENTS_DIR")
            repository_environments = Path(__file__).resolve().parents[1] / "environment_files"
            environments_dir = (
                Path(configured_environments).expanduser()
                if configured_environments
                else repository_environments
                if repository_environments.is_dir()
                else Path("environment_files")
            )
            # Prefer the deterministic local cache. Fall back to NORMAL mode so a
            # not-yet-downloaded public game can be fetched before later offline turns.
            self.arcade = arc_agi.Arcade(
                operation_mode=arc_agi.OperationMode.OFFLINE,
                environments_dir=str(environments_dir),
            )
            self.env = self.arcade.make(game, seed=0)
            if self.env is None:
                self.arcade = arc_agi.Arcade(environments_dir=str(environments_dir))
                self.env = self.arcade.make(game, seed=0)
        else:
            self.arcade = arcade
            self.env = self.arcade.make(game, seed=0)
        if self.env is None:
            raise RuntimeError(f"could not create game {game!r}")
        frame = self.env.observation_space
        if frame is None or not frame.frame:
            raise RuntimeError(f"game {game!r} did not provide an initial frame")
        self._frame = frame
        self._initial_grid = np.asarray(frame.frame[-1], dtype=int).tolist()
        self._timeline: list[Transition] = []

    @property
    def timeline(self) -> tuple[Transition, ...]:
        return tuple(self._timeline)

    @property
    def initial_grid(self) -> Grid:
        return [row[:] for row in self._initial_grid]

    @property
    def frame(self) -> Any:
        return self._frame

    @property
    def grid(self) -> Grid:
        return np.asarray(self._frame.frame[-1], dtype=int).tolist()

    @property
    def level(self) -> int:
        return int(self._frame.levels_completed)

    @property
    def state(self) -> str:
        return self._state_value(self._frame.state)

    @property
    def win_levels(self) -> int:
        return int(self._frame.win_levels)

    @property
    def legal_actions(self) -> list[int]:
        return [int(action) for action in self._frame.available_actions]

    @staticmethod
    def _state_value(state: Any) -> str:
        return str(state.value) if hasattr(state, "value") else str(state)

    @staticmethod
    def _prediction(raw: Any) -> WorldModelPrediction:
        if isinstance(raw, WorldModelPrediction):
            prediction = raw
        elif isinstance(raw, Mapping):
            prediction = WorldModelPrediction(
                grid=raw["grid"],
                level_up=raw.get("level_up", False),
                dead=raw.get("dead", False),
                win=raw.get("win", False),
            )
        elif isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], Mapping):
            flags = raw[1]
            prediction = WorldModelPrediction(
                grid=raw[0],
                level_up=flags.get("level_up", False),
                dead=flags.get("dead", False),
                win=flags.get("win", False),
            )
        elif isinstance(raw, tuple) and len(raw) == 4:
            prediction = WorldModelPrediction(raw[0], raw[1], raw[2], raw[3])
        else:
            raise TypeError("world model must return WorldModelPrediction or grid/flags")

        flags = (prediction.level_up, prediction.dead, prediction.win)
        if any(type(flag) is not bool for flag in flags):
            raise TypeError("world-model flags must be bool")
        return WorldModelPrediction(
            grid=np.asarray(prediction.grid, dtype=int).tolist(),
            level_up=prediction.level_up,
            dead=prediction.dead,
            win=prediction.win,
        )

    def _step(self, queued: QueuedAction, *, turn: int) -> Transition:
        before_level = self.level
        before_state = self.state
        game_action = GameAction.from_id(queued.action)
        data = {"x": queued.x, "y": queued.y} if queued.action == 6 else None
        if queued.action == 0:
            frame = self.env.step(GameAction.RESET)
            if frame is None:
                frame = self.env.reset()
        else:
            frame = self.env.step(game_action, data)
        if frame is None or not frame.frame:
            raise RuntimeError(f"game returned no frame for action {queued.action}")

        self._frame = frame
        state = self.state
        transition = Transition(
            step_index=len(self._timeline),
            action=queued.action,
            x=queued.x,
            y=queued.y,
            grid=self.grid,
            level=self.level,
            state=state,
            level_up=(self.level > before_level or (before_state != "WIN" and state == "WIN")),
            dead=state == "GAME_OVER",
            win=state == "WIN",
        )
        self._timeline.append(transition)
        if self.event_log is not None:
            self.event_log.append(
                ActionTaken(
                    turn=turn,
                    step_index=transition.step_index,
                    action=transition.action,
                    x=transition.x,
                    y=transition.y,
                    grid=transition.grid,
                    level_up=transition.level_up,
                    dead=transition.dead,
                    win=transition.win,
                    state=transition.state,
                    level=transition.level,
                )
            )
        return transition

    @staticmethod
    def _matches(predicted: WorldModelPrediction, actual: Transition) -> bool:
        terminal = actual.level_up or actual.dead or actual.win
        return (
            (
                terminal
                or np.array_equal(
                    np.asarray(predicted.grid, dtype=int),
                    np.asarray(actual.grid, dtype=int),
                )
            )
            and predicted.level_up == actual.level_up
            and predicted.dead == actual.dead
            and predicted.win == actual.win
        )

    def _result(
        self,
        *,
        committed: int,
        executed: int,
        halt_reason: HaltReason,
        start_level: int,
        start_state: str,
        end_level: int | None = None,
        end_state: str | None = None,
        surprise: str = "",
    ) -> ExecutionResult:
        return ExecutionResult(
            committed=committed,
            executed=executed,
            halt_reason=halt_reason,
            start_level=start_level,
            end_level=self.level if end_level is None else end_level,
            start_state=start_state,
            end_state=self.state if end_state is None else end_state,
            surprise=surprise,
        )

    def replay_transition(self, record: Mapping[str, Any], *, turn: int = 0) -> Transition:
        """Replay one persisted action and verify its deterministic settled result."""

        expected_index = record.get("step_index")
        if type(expected_index) is not int or expected_index != len(self._timeline):
            raise ValueError(
                f"persisted step_index must be {len(self._timeline)}, got {expected_index!r}"
            )
        transition = self._step(QueuedAction.parse(record), turn=turn)
        for field in ("action", "x", "y", "level", "state", "level_up", "dead", "win"):
            if field in record and getattr(transition, field) != record[field]:
                raise RuntimeError(
                    f"persisted transition #{transition.step_index} {field} mismatch: "
                    f"replayed={getattr(transition, field)!r}, stored={record[field]!r}"
                )
        if "grid" in record and not np.array_equal(
            np.asarray(transition.grid, dtype=int), np.asarray(record["grid"], dtype=int)
        ):
            raise RuntimeError(
                f"persisted transition #{transition.step_index} grid hash mismatch"
            )
        return transition

    def execute_queue(
        self,
        actions: Sequence[Any],
        live_model: WorldModel | None = None,
        max_actions: int = 3000,
        *,
        turn: int = 0,
    ) -> ExecutionResult:
        """Execute a checked queue until its first mandated halt condition."""

        if type(max_actions) is not int or max_actions < 0:
            raise ValueError("max_actions must be a non-negative integer")
        queue = tuple(QueuedAction.parse(action) for action in actions)
        start_level = self.level
        start_state = self.state
        executed = 0

        if len(self._timeline) >= max_actions:
            return self._result(
                committed=len(queue),
                executed=executed,
                halt_reason="max_actions",
                start_level=start_level,
                start_state=start_state,
            )

        for queued in queue:
            prediction: WorldModelPrediction | None = None
            if live_model is not None:
                try:
                    prediction = self._prediction(
                        live_model(self.grid, queued.action, queued.x, queued.y)
                    )
                except Exception:
                    return self._result(
                        committed=len(queue),
                        executed=executed,
                        halt_reason="nondeterministic-model",
                        start_level=start_level,
                        start_state=start_state,
                    )

            actual = self._step(queued, turn=turn)
            executed += 1
            mismatch = prediction is not None and not self._matches(prediction, actual)
            surprise = ""
            if mismatch:
                surprise = surprise_message(queued.action, queued.x, queued.y)
                if self.event_log is not None:
                    self.event_log.append(
                        ModelMispredicted(
                            turn=turn,
                            step_index=actual.step_index,
                            surprise=surprise,
                            predicted=prediction.grid,
                            actual=actual.grid,
                        )
                    )

            # The automatic RESET is a scored timeline action, but it is not one of
            # the agent's executed planned actions. Narration also reports the
            # pre-reset GAME_OVER state, while the gateway itself advances to the
            # post-reset frame for the next turn.
            terminal_level = actual.level
            terminal_state = actual.state
            if actual.dead and len(self._timeline) < max_actions:
                self._step(QueuedAction(0), turn=turn)

            if mismatch:
                return self._result(
                    committed=len(queue),
                    executed=executed,
                    halt_reason="surprise",
                    start_level=start_level,
                    start_state=start_state,
                    end_level=terminal_level,
                    end_state=terminal_state,
                    surprise=surprise,
                )
            if actual.dead:
                return self._result(
                    committed=len(queue),
                    executed=executed,
                    halt_reason="dead",
                    start_level=start_level,
                    start_state=start_state,
                    end_level=terminal_level,
                    end_state=terminal_state,
                )
            if actual.win:
                return self._result(
                    committed=len(queue),
                    executed=executed,
                    halt_reason="completed",
                    start_level=start_level,
                    start_state=start_state,
                )
            if actual.level_up:
                return self._result(
                    committed=len(queue),
                    executed=executed,
                    halt_reason="level_up",
                    start_level=start_level,
                    start_state=start_state,
                )
            if len(self._timeline) >= max_actions:
                return self._result(
                    committed=len(queue),
                    executed=executed,
                    halt_reason="max_actions",
                    start_level=start_level,
                    start_state=start_state,
                )
            if live_model is None:
                return self._result(
                    committed=len(queue),
                    executed=executed,
                    halt_reason="no-world-model-single-step",
                    start_level=start_level,
                    start_state=start_state,
                )

        return self._result(
            committed=len(queue),
            executed=executed,
            halt_reason="completed",
            start_level=start_level,
            start_state=start_state,
        )


@dataclass(frozen=True, slots=True)
class GatewaySnapshot:
    """Serializable state needed to construct the next agent turn."""

    game_id: str
    grid: Grid
    level: int
    state: str
    win_levels: int
    legal: list[int]
    history_len: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "game_id": self.game_id,
            "grid": self.grid,
            "level": self.level,
            "state": self.state,
            "win_levels": self.win_levels,
            "legal": self.legal,
            "history_len": self.history_len,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "GatewaySnapshot":
        return cls(
            game_id=str(raw["game_id"]),
            grid=np.asarray(raw["grid"], dtype=int).tolist(),
            level=int(raw["level"]),
            state=str(raw["state"]),
            win_levels=int(raw["win_levels"]),
            legal=[int(action) for action in raw["legal"]],
            history_len=int(raw["history_len"]),
        )


def grid_hash(grid: Grid) -> str:
    """Return a stable hash for a normalized integer grid."""

    normalized = np.asarray(grid, dtype=int).tolist()
    payload = json.dumps(normalized, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        json.dump(payload, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _execution_payload(result: ExecutionResult) -> dict[str, Any]:
    return {
        "committed": result.committed,
        "executed": result.executed,
        "halt_reason": result.halt_reason,
        "start_level": result.start_level,
        "end_level": result.end_level,
        "start_state": result.start_state,
        "end_state": result.end_state,
        "surprise": result.surprise,
    }


def _execution_result(raw: Mapping[str, Any]) -> ExecutionResult:
    return ExecutionResult(
        committed=int(raw["committed"]),
        executed=int(raw["executed"]),
        halt_reason=raw["halt_reason"],
        start_level=int(raw["start_level"]),
        end_level=int(raw["end_level"]),
        start_state=str(raw["start_state"]),
        end_state=str(raw["end_state"]),
        surprise=str(raw.get("surprise") or ""),
    )


class PersistentGateway:
    """Gateway owner with a replay timeline and idempotent durable turn ledger."""

    TIMELINE_NAME = "gateway_timeline.jsonl"
    LEDGER_NAME = "turn_ledger.json"
    STATE_NAME = "gateway_state.json"
    MODEL_NAME = "live_model.json"
    _MODEL_PATTERN = re.compile(r"world_model_v(\d+)\.py")

    def __init__(
        self,
        game: str,
        workdir: str | os.PathLike[str],
        event_log: EventLog | None = None,
        *,
        arcade: Any | None = None,
        max_actions: int = 3000,
    ) -> None:
        if type(max_actions) is not int or max_actions < 0:
            raise ValueError("max_actions must be a non-negative integer")
        self.game = game
        self.workdir = Path(workdir).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir = self.workdir / "runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.timeline_path = self.runtime_dir / self.TIMELINE_NAME
        self.ledger_path = self.runtime_dir / self.LEDGER_NAME
        self.state_path = self.runtime_dir / self.STATE_NAME
        self.model_path = self.runtime_dir / self.MODEL_NAME
        self.max_actions = max_actions
        self._ledger = self._read_ledger()

        self.gateway = Gateway(game, event_log=None, arcade=arcade)
        self._validate_existing_state()
        self._replay_timeline()
        self.gateway.event_log = event_log
        self._write_snapshot()

    @property
    def timeline(self) -> tuple[Transition, ...]:
        return self.gateway.timeline

    @property
    def snapshot(self) -> GatewaySnapshot:
        return GatewaySnapshot(
            game_id=self.game,
            grid=self.gateway.grid,
            level=self.gateway.level,
            state=self.gateway.state,
            win_levels=self.gateway.win_levels,
            legal=self.gateway.legal_actions,
            history_len=len(self.gateway.timeline),
        )

    def _validate_existing_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid gateway state file: {self.state_path}") from exc
        if raw.get("game_id") != self.game:
            raise ValueError(
                f"workdir belongs to game {raw.get('game_id')!r}, not {self.game!r}"
            )
        expected = raw.get("initial_grid_hash")
        actual = grid_hash(self.gateway.initial_grid)
        if expected is not None and expected != actual:
            raise RuntimeError("initial game grid hash changed; refusing nondeterministic replay")

    def _read_ledger(self) -> dict[str, Any]:
        if not self.ledger_path.exists():
            return {"version": 1, "turns": {}}
        try:
            raw = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid turn ledger: {self.ledger_path}") from exc
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise ValueError(f"unsupported turn ledger: {self.ledger_path}")
        turns = raw.get("turns")
        if not isinstance(turns, dict):
            raise ValueError(f"turn ledger has no turns object: {self.ledger_path}")
        return raw

    def _save_ledger(self) -> None:
        _atomic_json(self.ledger_path, self._ledger)

    def _timeline_records(self) -> list[dict[str, Any]]:
        if not self.timeline_path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.timeline_path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"{self.timeline_path}:{line_number}: invalid JSON"
                    ) from exc
                if not isinstance(record, dict):
                    raise ValueError(
                        f"{self.timeline_path}:{line_number}: transition must be an object"
                    )
                records.append(record)
        return records

    def _replay_timeline(self) -> None:
        for record in self._timeline_records():
            turn = record.get("turn", 0)
            self.gateway.replay_transition(
                record,
                turn=turn if type(turn) is int else 0,
            )

    def _write_snapshot(self) -> None:
        payload = self.snapshot.as_dict()
        payload.update(
            version=1,
            initial_grid_hash=grid_hash(self.gateway.initial_grid),
            grid_hash=grid_hash(self.gateway.grid),
        )
        _atomic_json(self.state_path, payload)

    def _append_transitions(
        self,
        transitions: Sequence[Transition],
        *,
        turn_id: str,
        turn: int,
    ) -> None:
        if not transitions:
            return
        with self.timeline_path.open("a", encoding="utf-8", newline="") as handle:
            for transition in transitions:
                record = {
                    "step_index": transition.step_index,
                    "action": transition.action,
                    "x": transition.x,
                    "y": transition.y,
                    "grid": transition.grid,
                    "level": transition.level,
                    "state": transition.state,
                    "level_up": transition.level_up,
                    "dead": transition.dead,
                    "win": transition.win,
                    "turn_id": turn_id,
                    "turn": turn,
                }
                handle.write(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _commit_payload(
        actions: Sequence[QueuedAction],
        reason: str,
        suggestion: str | None,
    ) -> dict[str, Any]:
        return {
            "actions": [action.released() for action in actions],
            "reason": reason,
            "suggestion": suggestion,
        }

    def turn_record(self, turn_id: str) -> dict[str, Any] | None:
        raw = self._ledger["turns"].get(turn_id)
        if raw is None:
            return None
        return json.loads(json.dumps(raw))

    def is_turn_complete(self, turn_id: str) -> bool:
        record = self._ledger["turns"].get(turn_id)
        return isinstance(record, dict) and record.get("phase") == "COMPLETE"

    def _after_commit_durable(self, turn_id: str) -> None:
        """Fault-injection seam: COMMIT_DURABLE is fsynced before this hook runs."""

    def commit(
        self,
        turn_id: str,
        actions: Sequence[Any],
        reason: str,
        suggestion: str | None = None,
        *,
        live_model: WorldModel | None = None,
        turn: int = 0,
    ) -> ExecutionResult:
        """Execute a turn once; duplicate ``turn_id`` calls return its prior result."""

        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError("turn_id must be a non-empty string")
        if not isinstance(reason, str):
            raise TypeError("reason must be a string")
        if suggestion is not None and not isinstance(suggestion, str):
            raise TypeError("suggestion must be a string when provided")
        queue = tuple(QueuedAction.parse(action) for action in actions)
        payload = self._commit_payload(queue, reason, suggestion)
        turns: dict[str, Any] = self._ledger["turns"]
        record = turns.get(turn_id)

        if record is not None:
            prior_payload = {
                name: record.get(name) for name in ("actions", "reason", "suggestion")
            }
            if prior_payload != payload:
                raise ValueError(f"turn_id {turn_id!r} was already committed with different input")
            if record.get("phase") == "COMPLETE":
                result = record.get("result")
                if not isinstance(result, Mapping):
                    raise ValueError(f"completed turn {turn_id!r} has no result")
                return _execution_result(result)
            expected_step = record.get("pre_step_index")
            expected_hash = record.get("pre_grid_hash")
            if expected_step != len(self.gateway.timeline) or expected_hash != grid_hash(
                self.gateway.grid
            ):
                raise RuntimeError(
                    f"pending turn {turn_id!r} no longer matches its durable pre-commit grid"
                )
        else:
            record = {
                **payload,
                "phase": "COMMIT_DURABLE",
                "turn": turn,
                "pre_step_index": len(self.gateway.timeline),
                "pre_grid_hash": grid_hash(self.gateway.grid),
            }
            turns[turn_id] = record
            self._save_ledger()
            self._after_commit_durable(turn_id)

        record["phase"] = "EXECUTING"
        self._save_ledger()
        start = len(self.gateway.timeline)
        result = self.gateway.execute_queue(
            queue,
            live_model=live_model,
            max_actions=self.max_actions,
            turn=turn,
        )
        new_transitions = self.gateway.timeline[start:]
        self._append_transitions(new_transitions, turn_id=turn_id, turn=turn)
        record.update(
            phase="COMPLETE",
            result=_execution_payload(result),
            post_step_index=len(self.gateway.timeline),
            post_grid_hash=grid_hash(self.gateway.grid),
        )
        self._save_ledger()
        self._write_snapshot()
        return result

    def history(self) -> dict[str, Any]:
        return {
            "initial_turn": {
                "grid": self.gateway.initial_grid,
                "level": 0,
                "win_levels": self.gateway.win_levels,
            },
            "actions": list(self.gateway.timeline),
        }

    def set_live_model(self, path: str | os.PathLike[str]) -> None:
        model = Path(path).resolve()
        if model.parent != self.workdir or not self._MODEL_PATTERN.fullmatch(model.name):
            raise ValueError("live world model must be a workdir world_model_v<N>.py file")
        if not model.is_file():
            raise FileNotFoundError(model)
        _atomic_json(self.model_path, {"version": 1, "path": model.name})

    def live_model_path(self) -> Path | None:
        if self.model_path.exists():
            try:
                raw = json.loads(self.model_path.read_text(encoding="utf-8"))
                candidate = self.workdir / str(raw["path"])
            except (OSError, KeyError, TypeError, json.JSONDecodeError):
                candidate = Path()
            if candidate.parent == self.workdir and candidate.is_file():
                return candidate

        candidates: list[tuple[int, Path]] = []
        for candidate in self.workdir.glob("world_model_v*.py"):
            match = self._MODEL_PATTERN.fullmatch(candidate.name)
            if match and candidate.is_file():
                candidates.append((int(match.group(1)), candidate))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]


__all__ = [
    "ExecutionResult",
    "Gateway",
    "GatewaySnapshot",
    "HaltReason",
    "PersistentGateway",
    "QueuedAction",
    "Transition",
    "WorldModel",
    "WorldModelPrediction",
    "grid_hash",
]
