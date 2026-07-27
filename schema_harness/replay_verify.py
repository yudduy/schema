"""Plan-driven replay and bp35 scorer acceptance verification."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .events import (
    EventLog,
    RunFinished,
    RunStarted,
    TurnCommitted,
    TurnStarted,
    iter_json_objects,
)
from .game_identity import short_game_id
from .gateway import Gateway, QueuedAction, Transition


RELEASE_COLLECTIONS = ("gpt_5_6_sol", "claude_fable_opus")
DEFAULT_COLLECTION = "claude_fable_opus"
DEFAULT_EFFORT = "max"
BP35_SCORE = 93.51
BP35_LEVELS = 9


@dataclass(frozen=True, slots=True)
class ReplayCommit:
    turn: int
    plan: tuple[QueuedAction, ...]
    executed_plan: tuple[QueuedAction, ...]
    reason: str
    released_actions: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ReleasedTrace:
    run_started: dict[str, Any]
    initial_turn: dict[str, Any]
    run_finished: dict[str, Any]
    commits: tuple[ReplayCommit, ...]
    actions: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ReplayVerification:
    events_path: Path
    run_path: Path
    scorer_stdout: str
    action_count: int
    levels: int
    score: float


@dataclass(slots=True)
class FakeClock:
    """Deterministic monotonic clock used by acceptance replays."""

    value: float = 0.0
    step: float = 1.0

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current


def _one_event(events: Iterable[dict[str, Any]], kind: str) -> dict[str, Any]:
    matches = [event for event in events if event.get("kind") == kind]
    if len(matches) != 1:
        raise ValueError(f"released trace must contain exactly one {kind} event")
    return matches[0]


def _released_action(event: dict[str, Any]) -> QueuedAction:
    return QueuedAction.parse([event.get("action"), event.get("x"), event.get("y")])


def _executed_plan_prefix(
    plan: tuple[QueuedAction, ...],
    actions: Sequence[dict[str, Any]],
    *,
    turn: int,
) -> tuple[QueuedAction, ...]:
    """Align released actions to their committed prefix, excluding injected resets."""

    consumed = 0
    previous_dead = False
    for event in actions:
        actual = _released_action(event)
        if previous_dead and actual.action == 0:
            previous_dead = False
            continue
        if consumed >= len(plan) or actual != plan[consumed]:
            raise ValueError(
                f"turn {turn}: released action {actual.released()} is not the next "
                "committed plan action"
            )
        consumed += 1
        previous_dead = event.get("dead") is True

    if previous_dead:
        raise ValueError(f"turn {turn}: death was not followed by a scored RESET")
    return plan[:consumed]


def load_released_trace(path: str | Path) -> ReleasedTrace:
    """Load committed plans and validate their released execution prefixes."""

    source = Path(path)
    events = [event for _, event in iter_json_objects(source)]
    run_started = _one_event(events, "run_started")
    run_finished = _one_event(events, "run_finished")
    turn_starts = [event for event in events if event.get("kind") == "turn_started"]
    if not turn_starts or "grid" not in turn_starts[0]:
        raise ValueError("released trace has no initial turn_started grid")

    actions = tuple(event for event in events if event.get("kind") == "action_taken")
    if not actions:
        raise ValueError("released trace has no action_taken events")
    for expected, event in enumerate(actions):
        if type(event.get("step_index")) is not int or event["step_index"] != expected:
            raise ValueError(
                f"non-contiguous released step_index: expected {expected}, "
                f"got {event.get('step_index')!r}"
            )

    actions_by_turn: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for event in actions:
        turn = event.get("turn")
        if type(turn) is not int:
            raise ValueError("released action_taken event has no integer turn")
        actions_by_turn[turn].append(event)

    commits: list[ReplayCommit] = []
    committed_turns: set[int] = set()
    for event in events:
        if event.get("kind") != "turn_committed":
            continue
        turn = event.get("turn")
        if type(turn) is not int or turn in committed_turns:
            raise ValueError(f"invalid or duplicate committed turn: {turn!r}")
        raw_plan = event.get("plan")
        if not isinstance(raw_plan, list):
            raise ValueError(f"turn {turn}: committed plan must be a list")
        plan = tuple(QueuedAction.parse(action) for action in raw_plan)
        released_actions = tuple(actions_by_turn.get(turn, ()))
        executed_plan = _executed_plan_prefix(plan, released_actions, turn=turn)
        commits.append(
            ReplayCommit(
                turn=turn,
                plan=plan,
                executed_plan=executed_plan,
                reason=str(event.get("reason") or ""),
                released_actions=released_actions,
            )
        )
        committed_turns.add(turn)

    uncommitted_actions = sorted(set(actions_by_turn) - committed_turns)
    if uncommitted_actions:
        raise ValueError(f"actions found on uncommitted turns: {uncommitted_actions}")
    if sum(len(commit.released_actions) for commit in commits) != len(actions):
        raise ValueError("not all released actions were assigned to committed plans")

    return ReleasedTrace(
        run_started=run_started,
        initial_turn=turn_starts[0],
        run_finished=run_finished,
        commits=tuple(commits),
        actions=actions,
    )


def _grid_bytes(grid: Any) -> bytes:
    return json.dumps(grid, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def assert_transition_parity(
    transitions: Sequence[Transition], released_actions: Sequence[dict[str, Any]]
) -> None:
    """Assert action identity, terminal metadata, and byte-identical settled grids."""

    if len(transitions) != len(released_actions):
        raise AssertionError(
            f"replayed {len(transitions)} transitions, expected {len(released_actions)}"
        )
    for transition, released in zip(transitions, released_actions, strict=True):
        expected_action = _released_action(released)
        actual_action = QueuedAction(transition.action, transition.x, transition.y)
        if actual_action != expected_action:
            raise AssertionError(
                f"action mismatch at step {transition.step_index}: "
                f"ours={actual_action.released()}, released={expected_action.released()}"
            )
        if _grid_bytes(transition.grid) != _grid_bytes(released["grid"]):
            raise AssertionError(f"grid byte mismatch at step {transition.step_index}")
        for field in ("level", "state", "level_up", "dead", "win"):
            if getattr(transition, field) != released.get(field):
                raise AssertionError(
                    f"{field} mismatch at step {transition.step_index}: "
                    f"ours={getattr(transition, field)!r}, released={released.get(field)!r}"
                )


def execute_commit(
    gateway: Gateway,
    commit: ReplayCommit,
    *,
    max_actions: int = 3000,
) -> None:
    """Purely replay one committed prefix through single-step no-model calls."""

    start = len(gateway.timeline)
    for action in commit.executed_plan:
        result = gateway.execute_queue(
            [action],
            live_model=None,
            max_actions=max_actions,
            turn=commit.turn,
        )
        if result.halt_reason in {"dead", "level_up", "completed", "max_actions"}:
            break
    assert_transition_parity(gateway.timeline[start:], commit.released_actions)


def _prepare_output(
    output_root: Path,
    *,
    collection: str,
    model: str,
    effort: str,
    game: str,
    score: float,
    baseline_path: Path,
) -> Path:
    if collection not in RELEASE_COLLECTIONS:
        raise ValueError(f"collection must be one of {RELEASE_COLLECTIONS}")
    output_root.mkdir(parents=True, exist_ok=True)
    for name in RELEASE_COLLECTIONS:
        (output_root / name).mkdir(exist_ok=True)
    output_baseline = output_root / "baseline_actions.csv"
    if output_baseline.exists():
        if output_baseline.read_bytes() != baseline_path.read_bytes():
            raise FileExistsError(f"refusing to replace different baseline {output_baseline}")
    else:
        shutil.copyfile(baseline_path, output_baseline)
    trajectory_dir = output_root / collection / f"{model}_{effort}_{game}_{score:.2f}"
    trajectory_dir.mkdir(parents=True, exist_ok=True)
    events_path = trajectory_dir / "events.jsonl"
    if events_path.exists() and events_path.stat().st_size:
        raise FileExistsError(f"refusing to append a second replay to {events_path}")
    return trajectory_dir


def _assert_scorer_acceptance(
    scorer: Path,
    output_root: Path,
    *,
    expected_score: float,
    expected_levels: int,
) -> str:
    completed = subprocess.run(
        [
            sys.executable,
            str(scorer),
            "--root",
            str(output_root),
            "--expected",
            "0",
            "--no-manifest-check",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"vendored scorer failed with exit {completed.returncode}: {completed.stderr.strip()}"
        )
    score_text = f"{expected_score:.2f}%"
    levels_text = f"{expected_levels}/{expected_levels}"
    bp35_rows = [
        line
        for line in completed.stdout.splitlines()
        if re.search(r"\|\s*BP35\s*\|", line)
    ]
    if not bp35_rows or any(
        score_text not in row or levels_text not in row for row in bp35_rows
    ):
        raise AssertionError(
            f"vendored scorer did not report bp35 {score_text} and {levels_text}:\n"
            f"{completed.stdout}"
        )
    return completed.stdout


def _mirror_for_two_collection_scorer(
    trajectory_dir: Path,
    output_root: Path,
    *,
    collection: str,
) -> Path:
    """Populate the scorer's other mandatory collection with the same bp35 replay."""

    companion = next(name for name in RELEASE_COLLECTIONS if name != collection)
    mirror = output_root / companion / trajectory_dir.name
    if mirror.exists():
        raise FileExistsError(f"refusing to replace scorer mirror {mirror}")
    mirror.mkdir(parents=True)
    for filename in ("events.jsonl", "run.json"):
        shutil.copyfile(trajectory_dir / filename, mirror / filename)
    return mirror


def replay_and_verify(
    released_events: str | Path,
    output_root: str | Path,
    *,
    collection: str = DEFAULT_COLLECTION,
    effort: str = DEFAULT_EFFORT,
    expected_score: float = BP35_SCORE,
    expected_levels: int = BP35_LEVELS,
    clock: Callable[[], float] | None = None,
    scorer_path: str | Path | None = None,
    baseline_path: str | Path | None = None,
) -> ReplayVerification:
    """Replay released bp35 commits, verify parity, and run the vendored scorer."""

    source = Path(released_events)
    trace = load_released_trace(source)
    run = trace.run_started
    game_id = str(run.get("game_id") or "")
    try:
        game = short_game_id(game_id)
    except ValueError:
        game = ""
    if game != "bp35":
        raise ValueError("Step 1 replay acceptance is restricted to bp35")

    repo_root = Path(__file__).resolve().parents[1]
    scorer = Path(scorer_path) if scorer_path is not None else repo_root / "vendor/score_trajectories.py"
    baseline = Path(baseline_path) if baseline_path is not None else repo_root / "vendor/baseline_actions.csv"
    output = Path(output_root)
    trajectory_dir = _prepare_output(
        output,
        collection=collection,
        model=str(run.get("model") or "unknown-model"),
        effort=effort,
        game="bp35",
        score=expected_score,
        baseline_path=baseline,
    )
    events_path = trajectory_dir / "events.jsonl"
    run_path = trajectory_dir / "run.json"
    replay_clock = clock or FakeClock()

    with EventLog(events_path, clock=replay_clock) as event_log:
        event_log.append(
            RunStarted(
                game_id=game_id,
                provider=str(run.get("provider") or "-"),
                model=str(run.get("model") or "-"),
                max_actions=int(run.get("max_actions") or 3000),
                win_levels=int(run.get("win_levels") or 0),
                workdir=str(run.get("workdir") or ""),
                resumed=False,
                resumed_transitions=0,
            )
        )
        gateway = Gateway(game_id, event_log=event_log)
        if _grid_bytes(gateway.grid) != _grid_bytes(trace.initial_turn["grid"]):
            raise AssertionError("initial bp35 grid does not byte-match the released trace")

        for commit in trace.commits:
            event_log.append(
                TurnStarted(
                    turn=commit.turn,
                    env_step=len(gateway.timeline),
                    state=gateway.state,
                    level=gateway.level,
                    win_levels=gateway.win_levels,
                    legal=gateway.legal_actions,
                    grid=gateway.grid,
                    has_world_model=False,
                    surprise="",
                )
            )
            event_log.append(
                TurnCommitted(
                    turn=commit.turn,
                    plan=[action.released() for action in commit.plan],
                    reason=commit.reason,
                )
            )
            execute_commit(
                gateway,
                commit,
                max_actions=int(run.get("max_actions") or 3000),
            )

        assert_transition_parity(gateway.timeline, trace.actions)
        event_log.append(
            RunFinished(
                state=gateway.state,
                levels=gateway.level,
                win_levels=gateway.win_levels,
                actions=len(gateway.timeline),
                transitions=len(gateway.timeline),
                has_world_model=False,
            )
        )

    expected_final = trace.run_finished
    if gateway.state != expected_final.get("state") or gateway.level != expected_final.get("levels"):
        raise AssertionError(
            f"final replay state ({gateway.level}, {gateway.state}) does not match released "
            f"({expected_final.get('levels')}, {expected_final.get('state')})"
        )
    if len(gateway.timeline) != len(trace.actions):
        raise AssertionError("replay action count does not match released action count")

    run_metadata = {
        "game_id": game_id,
        "provider": str(run.get("provider") or "-"),
        "model": str(run.get("model") or "-"),
        "max_actions": int(run.get("max_actions") or 3000),
        "win_levels": gateway.win_levels,
        "workdir": str(run.get("workdir") or ""),
        "started_at": run.get("ts", 0),
    }
    run_path.write_text(
        json.dumps(run_metadata, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    # The vendored scorer always summarizes both released collections and divides
    # by each collection's trajectory count. Mirror this bp35 replay so neither
    # hard-coded collection is empty; no non-bp35 trajectory is introduced.
    scorer_mirror = _mirror_for_two_collection_scorer(
        trajectory_dir,
        output,
        collection=collection,
    )
    try:
        scorer_stdout = _assert_scorer_acceptance(
            scorer,
            output,
            expected_score=expected_score,
            expected_levels=expected_levels,
        )
    finally:
        shutil.rmtree(scorer_mirror)
    return ReplayVerification(
        events_path=events_path,
        run_path=run_path,
        scorer_stdout=scorer_stdout,
        action_count=len(gateway.timeline),
        levels=gateway.level,
        score=expected_score,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("released_events", type=Path)
    parser.add_argument("output_root", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result = replay_and_verify(args.released_events, args.output_root)
    print(result.scorer_stdout, end="")
    print(f"Replay grids: byte-identical across {result.action_count} actions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BP35_LEVELS",
    "BP35_SCORE",
    "FakeClock",
    "ReleasedTrace",
    "ReplayCommit",
    "ReplayVerification",
    "assert_transition_parity",
    "execute_commit",
    "load_released_trace",
    "replay_and_verify",
]
