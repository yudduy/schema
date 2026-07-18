"""Minimal ARC-AGI-3 agent: random actions until WIN or the budget runs out.

Usage:
    uv run agent.py                       # random agent on LS20, full speed
    uv run agent.py --game vc33 --render  # another game, rendered in the terminal
"""

import argparse
import random

import arc_agi
from arcengine import GameAction, GameState


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", default="ls20", help="game id, e.g. ls20 (see README to list)")
    parser.add_argument("--actions", type=int, default=1000, help="action budget")
    parser.add_argument("--seed", type=int, default=0, help="env + rng seed")
    parser.add_argument("--render", action="store_true", help="render frames in the terminal")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    arc = arc_agi.Arcade()
    env = arc.make(args.game, seed=args.seed, render_mode="terminal" if args.render else None)
    if env is None:
        raise SystemExit(f"unknown game {args.game!r} — see README to list games")

    frame = env.observation_space  # make() already reset the env
    for _ in range(args.actions):
        if frame is None or frame.state is GameState.WIN:
            break
        if frame.state in (GameState.NOT_PLAYED, GameState.GAME_OVER):
            frame = env.reset()
            continue
        choices = [a for a in frame.available_actions if a != GameAction.RESET.value]
        if not choices:
            frame = env.reset()
            continue
        action = GameAction.from_id(rng.choice(choices))
        data = None
        if action is GameAction.ACTION6:  # complex action: needs grid coordinates
            height, width = frame.frame[-1].shape if frame.frame else (64, 64)
            data = {"x": rng.randrange(width), "y": rng.randrange(height)}
        frame = env.step(action, data)

    if frame is not None:
        print(f"{args.game}: {frame.state.value}, levels {frame.levels_completed}/{frame.win_levels}")
    print(arc.get_scorecard())


if __name__ == "__main__":
    main()
