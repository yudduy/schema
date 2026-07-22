# Schema — open reconstruction of the ARC-AGI-3 harness

An independent, clean-room reconstruction of **Schema**
([schema-harness.github.io](https://schema-harness.github.io/), Impossible Research) —
the harness that has frontier models play ARC-AGI-3 like physicists: write each game's
mechanism as an executable program, validate it against every recorded transition, plan
inside it, and act only through a gated commit channel. The original method was reported
with a closed harness (98.98% RHAE with Opus 4.8 + Fable 5; 95.35% with GPT-5.6 Sol);
only its traces were released. This repo rebuilds the harness from those released
artifacts and reproduces the reported behavior on live play, with every claim checkable
from committed code and emitted traces. Credit for the method belongs to Impossible
Research; this is a reproduction, not their code.

**Status (2026-07-21, sweep in progress):** 7 of 25 public games scored with GPT-5.6 Sol —
five at 100.0 (ar25 8/8, r11l 6/6, tu93 9/9, cd82 6/6, su15 9/9), tn36 71.93 (7/7) and
sc25 61.44 (6/6) pending their protocol fallback reruns. Zero DNFs so far. Live numbers
live in the sweep manifest (`spikes/export_traces.py`), not in this README.

## Run one game yourself

See [REPRODUCE.md](REPRODUCE.md) for the full evidence chain (vendored official scorer,
replay parity against the released bp35 trace, honesty notes on contamination and
self-reporting). Short version:

```bash
uv sync && npm install -g @openai/codex@0.144.1 && codex login
cp .env.example .env      # paste your ARC key
ONLY_RESET_LEVELS=true uv run python spikes/sweep.py sol tu93
```

## Help run the sweep

The bottleneck is subscription quota, not machines — see [HELPER.md](HELPER.md) to claim
a game and contribute a verified datapoint.

## Layout

- `schema_harness/` — runner (Claude/Codex headless drivers), `locus` MCP server
  (14 tools: commit_actions, run_backtest, run_bfs, …), world-model worker, BFS, backtest
- `vendor/` — official trajectory scorer + human baselines + released bp35 trace
- `prompts/` — the physicist method prompts (`physicist_v9_matched_transfer.md` is live)
- `spikes/` — sweep orchestration, trace export, intake verification
- `docs/` — lane charters, results ledgers, consolidation history

---

## The underlying ARC-AGI-3 environment

[ARC-AGI-3](https://arcprize.org/arc-agi/3) interactive-reasoning environments via the
official [ARC-AGI Toolkit](https://github.com/arcprize/arc-agi). Games execute on the
local engine (~2K FPS, no rate limits); the API supplies game files, scorecards, replays.

```bash
uv run play.py                            # quickstart: LS20 rendered in the terminal
uv run agent.py --game vc33 --render      # random agent, watch it play
```

- `arc_agi.Arcade()` — client. Reads `ARC_API_KEY` from env/`.env`, else fetches an
  anonymous key. Downloads game files to `environment_files/` on first use, then runs
  them locally. `operation_mode=OperationMode.OFFLINE` skips the API entirely.
- `env = arc.make(game_id, seed=0, render_mode=None)` — `"terminal"` to watch.
- `frame = env.step(action, data=None)` → `.frame` (64×64 grids, colors 0–15), `.state`
  (`NOT_FINISHED`/`WIN`/`GAME_OVER`), `.levels_completed`, `.available_actions`.
- Actions: `GameAction.ACTION1–5,7` simple; `ACTION6` takes `data={"x": .., "y": ..}`;
  `RESET` starts/restarts. `arc.get_scorecard()` — score, levels, action/reset counts.

Docs: <https://docs.arcprize.org> · Games: <https://arcprize.org/tasks> · Agent
templates: <https://docs.arcprize.org/llm_agents>
