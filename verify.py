"""Replay a released gameplay trace and compare settled grids.

Usage: python verify.py EVENTS_JSONL [--online]

Also exposes ``verify_events(events_path, game) -> Verdict``, the reusable gate the
intake and release-export paths use to re-execute a submitted trajectory on the
ground-truth ``arc_agi`` engine before trusting its score. A trace is green only if
every settled grid, the running level/state, and the final (levels, state) all
reproduce byte-for-byte over the whole recorded action sequence — the only way to
pass is to submit a real playthrough. Verification always runs under level-only
resets (``ONLY_RESET_LEVELS=true``), the reset semantics traces are recorded with;
without it genuine traces diverge at level boundaries.
"""

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import arc_agi
import numpy as np
from arcengine import GameAction

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from schema_harness.environment_cache import resolve_environments_dir
from schema_harness.events import iter_json_objects


def load_trace(events):
    actions, first_turn = [], None
    for event in events:
        if event.get("kind") == "action_taken":
            actions.append(event)
        elif event.get("kind") == "turn_started" and first_turn is None:
            first_turn = event

    if first_turn is None or "grid" not in first_turn:
        raise ValueError("trace has no turn_started event with an initial grid")
    if not actions:
        raise ValueError("trace has no action_taken events")
    for expected, event in enumerate(actions):
        actual = event.get("step_index")
        if type(actual) is not int or actual != expected:
            raise ValueError(f"non-contiguous step_index: expected {expected}, got {actual!r}")
    return first_turn["grid"], actions


def game_from_trace(events_path) -> str:
    """Return the full versioned game id the trace records for itself.

    A trace is self-describing, so the operator never has to name the game — and
    must not be able to name the wrong one. Replaying a trajectory on another
    game's engine diverges at the first frame and reports a confident RED that
    says nothing about the trace.
    """
    for _, payload in iter_json_objects(events_path):
        if payload.get("kind") == "run_started":
            game_id = payload.get("game_id")
            if not isinstance(game_id, str) or not game_id:
                raise ValueError("trace run_started event has no game_id")
            return game_id
    raise ValueError("trace has no run_started event naming its game")


def grid_differences(ours, theirs):
    """Count coordinate differences, including cells outside either shape."""
    differences = []
    for row in range(max(ours.shape[0], theirs.shape[0])):
        for col in range(max(ours.shape[1], theirs.shape[1])):
            ours_value = int(ours[row, col]) if row < ours.shape[0] and col < ours.shape[1] else None
            theirs_value = int(theirs[row, col]) if row < theirs.shape[0] and col < theirs.shape[1] else None
            if ours_value != theirs_value:
                differences.append((row, col, ours_value, theirs_value))
    return len(differences), differences[:5]


@dataclass
class Verdict:
    """Outcome of re-executing a trace on the ground-truth engine."""

    green: bool
    game: str
    steps_replayed: int
    total_actions: int
    grid_mismatches: int
    initial_equal: bool
    final_levels: int
    final_state: str
    recorded_final_levels: int
    recorded_final_state: str
    level_divergences: list = field(default_factory=list)
    state_divergences: list = field(default_factory=list)
    level_up_divergences: list = field(default_factory=list)
    error: str | None = None

    def reason(self) -> str:
        """One-line failure reason for logs and manifests (empty when green).

        Single source of truth so intake, export, and the CLI never drift out of
        sync with the green condition (they did once, omitting level_up)."""
        if self.green:
            return ""
        if self.error:
            return f"verifier-error: {self.error}"
        return (
            f"grid_mism={self.grid_mismatches} "
            f"level_div={len(self.level_divergences)} "
            f"state_div={len(self.state_divergences)} "
            f"level_up_div={len(self.level_up_divergences)} "
            f"steps={self.steps_replayed}/{self.total_actions} "
            f"final=({self.final_levels},{self.final_state}) vs "
            f"recorded=({self.recorded_final_levels},{self.recorded_final_state})")


def _open_env(game: str, offline: bool):
    """Open a game on the local engine and RESET-less-ly hand back the env.

    Offline verifies deterministically against the installed environment package
    and never downloads a possibly-different version. We do NOT silently fall back
    across modes: a mode that cannot open the game raises, and the caller records
    that error rather than quietly verifying against a different engine (which would
    blame a genuine trace for a version mismatch)."""
    environments_dir = resolve_environments_dir()
    if offline:
        from arc_agi import OperationMode

        arc = arc_agi.Arcade(operation_mode=OperationMode.OFFLINE,
                             environments_dir=str(environments_dir))
    else:
        arc = arc_agi.Arcade(environments_dir=str(environments_dir))
    return arc.make(game, seed=0)


def _red(game: str, error: str) -> Verdict:
    return Verdict(
        green=False, game=game, steps_replayed=0, total_actions=0, grid_mismatches=0,
        initial_equal=False, final_levels=-1, final_state="",
        recorded_final_levels=-1, recorded_final_state="", error=error,
    )


def verify_events(events_path, game, *, offline: bool = True, verbose: bool = False) -> Verdict:
    """Re-execute a recorded trace on the true engine and judge its fidelity.

    Green requires: initial-frame parity, zero grid mismatches, zero level/state/
    level_up divergences, the full action sequence replayed, and the replayed final
    (levels, state) matching what the trace records. Any parse or replay failure is
    red — an unreproducible trace is not trusted.

    `game` MUST be the full versioned `run.json.game_id` that identifies the
    environment replayed. Callers derive it from run.json, not the workdir name, so
    the replay engine and the RHAE baseline reference the same game. Otherwise a
    genuine easy-game trace could be scored against a harder game's baseline.

    Known limit: replay proves the submitted actions really occurred and produced the
    claimed result; it cannot prove the trace is COMPLETE. Deleting a provably-inert
    action (one that leaves every later frame byte-identical) still replays green, so
    per-level action counts are pinned only up to engine-inert no-ops.
    """
    # Replay is only meaningful under the reset semantics traces are recorded with;
    # force them for the duration, then restore the caller's env so this gate leaves
    # no global side effect.
    prev_reset = os.environ.get("ONLY_RESET_LEVELS")
    os.environ["ONLY_RESET_LEVELS"] = "true"
    try:
        return _verify_inner(events_path, game, offline=offline, verbose=verbose)
    finally:
        if prev_reset is None:
            os.environ.pop("ONLY_RESET_LEVELS", None)
        else:
            os.environ["ONLY_RESET_LEVELS"] = prev_reset


def _verify_inner(events_path, game, *, offline: bool, verbose: bool) -> Verdict:
    try:
        events = (event for _, event in iter_json_objects(events_path))
        initial_grid, actions = load_trace(events)
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # a trace we cannot even parse is unverifiable => reject
        return _red(game, f"{type(exc).__name__}: {exc}")

    try:
        env = _open_env(game, offline)
        if env is None:
            return _red(game, f"could not open game {game!r} "
                              f"({'offline' if offline else 'local'} engine returned no env)")
        obs = env.observation_space

        initial_ours = np.asarray(obs.frame[-1], dtype=int)
        initial_theirs = np.asarray(initial_grid, dtype=int)
        initial_equal = bool(np.array_equal(initial_ours, initial_theirs))
        if verbose:
            diff, _ = grid_differences(initial_ours, initial_theirs)
            print(f"Initial frame: equal={'yes' if initial_equal else 'no'}, differing cells={diff}")
            if initial_ours.shape != initial_theirs.shape:
                print(f"  shapes: ours={initial_ours.shape}, recorded={initial_theirs.shape}")

        grid_mismatches = steps_replayed = ours_level_ups = recorded_level_ups = 0
        level_divergences, state_divergences, level_up_divergences = [], [], []
        previous_level = obs.levels_completed
        previous_state = obs.state.value
        last_frame = obs

        for event in actions:
            step_index, action = event["step_index"], event["action"]
            data = {"x": event["x"], "y": event["y"]} if action == 6 else None
            if action == 0:
                frame = env.step(GameAction.RESET)
                if frame is None:
                    frame = env.reset()
            else:
                frame = env.step(GameAction.from_id(action), data)
            steps_replayed += 1
            recorded_level_ups += int(event.get("level_up", False))

            if frame is None:
                grid_mismatches += 1
                if verbose:
                    print(f"Grid mismatch at step {step_index}, action {action}: step returned None")
                break

            last_frame = frame
            # Mirror gateway.py's contract exactly: a WIN transition is a level_up
            # even when it does not increment levels_completed. Using only the
            # increment would false-RED a genuine trace on its final winning step.
            engine_level_up = (frame.levels_completed > previous_level
                               or (previous_state != "WIN"
                                   and frame.state.value == "WIN"))
            ours_level_ups += int(engine_level_up)
            previous_level = frame.levels_completed
            previous_state = frame.state.value
            # The scorer partitions actions into per-level counts SOLELY by the
            # level_up flag, so pin it to the engine's real increments — otherwise
            # actions could be re-attributed across levels (inflating RHAE) without
            # breaking grid, level, or state parity.
            if bool(event.get("level_up", False)) != engine_level_up:
                level_up_divergences.append(step_index)
                if verbose and len(level_up_divergences) == 1:
                    print(f"level_up divergence at step {step_index}: "
                          f"ours={engine_level_up}, recorded={event.get('level_up')}")
            if frame.levels_completed != event.get("level"):
                level_divergences.append(step_index)
                if verbose and len(level_divergences) == 1:
                    print(f"Level divergence at step {step_index}: "
                          f"ours={frame.levels_completed}, recorded={event.get('level')}")
            if frame.state.value != event.get("state"):
                state_divergences.append(step_index)
                if verbose and len(state_divergences) == 1:
                    print(f"State divergence at step {step_index}: "
                          f"ours={frame.state.value}, recorded={event.get('state')}")

            ours = np.asarray(frame.frame[-1], dtype=int)
            theirs = np.asarray(event["grid"], dtype=int)
            if ours.shape != theirs.shape or not np.array_equal(ours, theirs):
                grid_mismatches += 1
                if verbose:
                    diff, samples = grid_differences(ours, theirs)
                    print(f"Grid mismatch at step {step_index}, action {action}: {diff} differing cells")
                    if ours.shape != theirs.shape:
                        print(f"  shapes: ours={ours.shape}, recorded={theirs.shape}")
                    for sample in samples:
                        print(f"  sample (row, col, ours, recorded): {sample}")
                if grid_mismatches >= 3:
                    break
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # any replay failure => unverifiable => reject
        return _red(game, f"{type(exc).__name__}: {exc}")

    recorded_last = actions[-1]
    recorded_final_levels = recorded_last.get("level")
    recorded_final_state = recorded_last.get("state")
    final_levels = last_frame.levels_completed
    final_state = last_frame.state.value
    green = bool(
        initial_equal
        and grid_mismatches == 0
        and not level_divergences
        and not state_divergences
        and not level_up_divergences
        and steps_replayed == len(actions)
        and final_levels == recorded_final_levels
        and final_state == recorded_final_state
    )
    if verbose:
        first_level = level_divergences[0] if level_divergences else "none"
        first_state = state_divergences[0] if state_divergences else "none"
        print("\nSummary")
        print(f"steps replayed / total recorded: {steps_replayed} / {len(actions)}")
        print(f"initial-frame parity: {'yes' if initial_equal else 'no'}")
        print(f"grid mismatches: {grid_mismatches}")
        print(f"level divergences: {len(level_divergences)} (first index: {first_level})")
        print(f"state divergences: {len(state_divergences)} (first index: {first_state})")
        print(f"level_up divergences: {len(level_up_divergences)}")
        print(f"level-up moments (replayed): ours={ours_level_ups}, recorded={recorded_level_ups}")
        print(f"final: ours ({final_levels}, {final_state}) "
              f"vs recorded last ({recorded_final_levels}, {recorded_final_state})")
        print(f"PARITY: {'GREEN' if green else 'RED'}")

    return Verdict(
        green=green, game=game, steps_replayed=steps_replayed, total_actions=len(actions),
        grid_mismatches=grid_mismatches, initial_equal=initial_equal,
        final_levels=final_levels, final_state=final_state,
        recorded_final_levels=recorded_final_levels, recorded_final_state=recorded_final_state,
        level_divergences=level_divergences, state_divergences=state_divergences,
        level_up_divergences=level_up_divergences,
    )


def main():
    parser = argparse.ArgumentParser(description="Replay a trace and verify engine parity.")
    parser.add_argument("events_jsonl", metavar="EVENTS_JSONL")
    parser.add_argument("--online", action="store_true",
                        help="verify against the default local-first engine (downloads the "
                             "game if absent) instead of the hermetic offline engine")
    args = parser.parse_args()
    try:
        game = game_from_trace(args.events_jsonl)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    verdict = verify_events(args.events_jsonl, game, offline=not args.online, verbose=True)
    if verdict.error:
        print(f"error: {verdict.error}")
    return 0 if verdict.green else 1


if __name__ == "__main__":
    raise SystemExit(main())
