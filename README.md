# ARC-AGI-3 environment

[ARC-AGI-3](https://arcprize.org/arc-agi/3) interactive-reasoning environments via the official [ARC-AGI Toolkit](https://github.com/arcprize/arc-agi). Games execute on the local engine (~2K FPS, no rate limits); the API supplies game files, scorecards, and replays.

## Setup

```bash
uv sync
```

Optional — anonymous access works, but a registered key unlocks the full set of public games ([get one](https://arcprize.org/platform), log in with Google/GitHub):

```bash
cp .env.example .env   # then uncomment and paste your key
```

## Run

```bash
uv run play.py                            # quickstart: LS20 rendered in the terminal
uv run agent.py                           # random agent on LS20, full speed
uv run agent.py --game vc33 --render      # another game, watch it play
```

List available games:

```bash
uv run python -c "import arc_agi; [print(e.game_id) for e in arc_agi.Arcade().get_environments()]"
```

## API in one screen

- `arc_agi.Arcade()` — client. Reads `ARC_API_KEY` from env/`.env`, else fetches an anonymous key. Downloads game files to `environment_files/` on first use, then runs them locally. `operation_mode=OperationMode.OFFLINE` skips the API entirely (needs downloaded games).
- `env = arc.make(game_id, seed=0, render_mode=None)` — `"terminal"` to watch; `None` for full speed.
- `frame = env.step(action, data=None)` → `FrameDataRaw`:
  - `.frame` — list of 64×64 numpy grids (color indices 0–15)
  - `.state` — `NOT_FINISHED` / `WIN` / `GAME_OVER` (reset on `GAME_OVER`)
  - `.levels_completed` / `.win_levels`, `.available_actions`
- Actions: `GameAction.ACTION1–5,7` are simple; `ACTION6` is complex — pass `data={"x": .., "y": ..}`; `RESET` starts/restarts.
- `arc.get_scorecard()` — score, levels, action/reset counts per run.

Docs: <https://docs.arcprize.org> · Games: <https://arcprize.org/tasks> · Agent templates: <https://docs.arcprize.org/llm_agents>
