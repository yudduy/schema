"""Replay a released gameplay trace and compare settled grids.

Usage: python spikes/replay_parity.py EVENTS_JSONL [--game bp35]
"""

import argparse
import json

import arc_agi
import numpy as np
from arcengine import GameAction


def load_trace(lines):
    actions, first_turn = [], None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("kind") == "action_taken":
            actions.append(event)
        elif event.get("kind") == "turn_started" and first_turn is None:
            first_turn = event

    if first_turn is None or "grid" not in first_turn:
        raise SystemExit("trace has no turn_started event with an initial grid")
    if not actions:
        raise SystemExit("trace has no action_taken events")
    for expected, event in enumerate(actions):
        actual = event.get("step_index")
        if type(actual) is not int or actual != expected:
            raise SystemExit(f"non-contiguous step_index: expected {expected}, got {actual!r}")
    return first_turn["grid"], actions


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


def note_divergence(label, index, ours, recorded, divergences):
    if ours != recorded:
        divergences.append(index)
        if len(divergences) == 1:
            print(f"{label} divergence at step {index}: ours={ours}, recorded={recorded}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("events_jsonl", metavar="EVENTS_JSONL",
                        type=argparse.FileType(encoding="utf-8"))
    parser.add_argument("--game", default="bp35")
    args = parser.parse_args()

    initial_grid, actions = load_trace(args.events_jsonl)
    arc = arc_agi.Arcade()
    env = arc.make(args.game, seed=0)
    if env is None:
        raise SystemExit(f"could not create game {args.game!r}")

    obs = env.observation_space
    initial_ours = np.asarray(obs.frame[-1], dtype=int)
    initial_theirs = np.asarray(initial_grid, dtype=int)
    initial_equal = np.array_equal(initial_ours, initial_theirs)
    initial_diff_count, _ = grid_differences(initial_ours, initial_theirs)
    print(f"Initial frame: equal={'yes' if initial_equal else 'no'}, differing cells={initial_diff_count}")
    if initial_ours.shape != initial_theirs.shape:
        print(f"  shapes: ours={initial_ours.shape}, recorded={initial_theirs.shape}")

    grid_mismatches = steps_replayed = 0
    level_divergences, state_divergences = [], []
    ours_level_ups = recorded_level_ups = 0
    previous_level, last_frame = obs.levels_completed, obs

    for event in actions:
        step_index, action = event["step_index"], event["action"]
        ga = GameAction.from_id(action)
        data = {"x": event["x"], "y": event["y"]} if action == 6 else None
        if action == 0:
            frame = env.step(GameAction.RESET)
            if frame is None:
                frame = env.reset()
        else:
            frame = env.step(ga, data)
        steps_replayed += 1
        recorded_level_ups += int(event["level_up"])

        if frame is None:
            grid_mismatches += 1
            print(f"Grid mismatch at step {step_index}, action {action}: step returned None")
            break

        last_frame = frame
        ours_level_ups += int(frame.levels_completed > previous_level)
        previous_level = frame.levels_completed
        note_divergence("Level", step_index, frame.levels_completed, event["level"], level_divergences)
        note_divergence("State", step_index, frame.state.value, event["state"], state_divergences)

        ours = np.asarray(frame.frame[-1], dtype=int)
        theirs = np.asarray(event["grid"], dtype=int)
        if ours.shape != theirs.shape or not np.array_equal(ours, theirs):
            grid_mismatches += 1
            diff_count, samples = grid_differences(ours, theirs)
            print(f"Grid mismatch at step {step_index}, action {action}: {diff_count} differing cells")
            if ours.shape != theirs.shape:
                print(f"  shapes: ours={ours.shape}, recorded={theirs.shape}")
            for sample in samples:
                print(f"  sample (row, col, ours, recorded): {sample}")
            if grid_mismatches >= 3:
                break

    recorded_last = actions[-1]
    first_level = level_divergences[0] if level_divergences else "none"
    first_state = state_divergences[0] if state_divergences else "none"
    green = grid_mismatches == 0 and initial_equal
    print("\nSummary")
    print(f"steps replayed / total recorded: {steps_replayed} / {len(actions)}")
    print(f"initial-frame parity: {'yes' if initial_equal else 'no'}")
    print(f"grid mismatches: {grid_mismatches}")
    print(f"level divergences: {len(level_divergences)} (first index: {first_level})")
    print(f"state divergences: {len(state_divergences)} (first index: {first_state})")
    print(f"level-up moments (replayed): ours={ours_level_ups}, recorded={recorded_level_ups}")
    print(f"final: ours ({last_frame.levels_completed}, {last_frame.state.value}) "
          f"vs recorded last ({recorded_last['level']}, {recorded_last['state']})")
    print(f"PARITY: {'GREEN' if green else 'RED'}")
    return 0 if green else 1


if __name__ == "__main__":
    raise SystemExit(main())
