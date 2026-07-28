# Schema — an open reconstruction of the ARC-AGI-3 harness

Schema is an independent, clean-room reconstruction of Impossible Research's
[Schema harness](https://schema-harness.github.io/). It has frontier models approach
ARC-AGI-3 games like physicists: describe the game's mechanism as an executable world
model, validate that model against recorded transitions, plan inside it, and act only
through a gated commit channel.

The original harness was closed; its traces were released. This repository rebuilds the
protocol, tool-result interface, and world-model contract from those artifacts without
using game solutions. Credit for the method and released traces belongs to Impossible
Research. This is a reproduction, not their code.

## How it works

An ARC-AGI-3 game supplies a 64×64 grid of colour indices and a set of legal actions —
no object list, no rule sheet, no stated goal, no shaped reward. The harness makes the
model treat that as a physics problem, and enforces the discipline in the tool surface
rather than trusting the model to keep it:

1. **Write the theory as a program.** The agent maintains `world_model_v<N>.py`
   containing `step(state, action)` and `is_goal(state)`. The world model is source
   code, so it can be read, diffed, executed, and replayed.
2. **Certify it against the whole history.** `run_backtest` replays the program over
   every recorded transition. It either reproduces all of them or reports the first
   divergence.
3. **Plan inside the certified model.** Because the model is a simulator, `run_bfs`
   searches it without spending environment actions. Only the resulting plan is
   executed.
4. **Act through one gated channel.** `commit_actions` is the sole path to the
   environment. Every executed step is checked against the model's prediction, and a
   single mismatch discards the remaining plan and returns the agent to modelling.

The scoring metric squares the action-efficiency ratio, so exploration is expensive and
brute force is self-defeating. The efficiency comes from paying to discover a mechanism
once and then computing subsequent plans inside the model.

## Results so far

8 of 11 held-out games done, averaging **90.78%**. Every run below was re-verified on the
real game engine.

`actions` is how many moves the agent used; `human` is what a first-time human needed.
**Fewer is better.**

| game | score | levels | actions | human |
|------|------:|-------:|--------:|------:|
| cd82 | 100.00 | 6/6 | **99** | 171 |
| lp85 | 100.00 | 8/8 | **109** | 388 |
| su15 | 100.00 | 9/9 | **176** | 361 |
| tr87 | 100.00 | 6/6 | **176** | 414 |
| tu93 | 100.00 | 9/9 | **224** | 462 |
| vc33 | 92.91 | 7/7 | **363** | 447 |
| tn36 | 71.93 | 7/7 | 2364 | 317 |
| sc25 | 61.44 | 6/6 | 530 | 350 |

Six of the eight beat the human, some by 3×. The two low scores are games where the agent
burned a lot of moves before it worked out the mechanism — the score squares that penalty.

Still to run: `sp80`, `s5i5`, `cn04`. Both low scores have already had their retry — a
second attempt with more reasoning effort — and neither beat the first attempt, so those
two numbers are settled. On `tn36` the retry cleared no levels at all where the first run
won 7/7, which is worth stating plainly: more thinking did not rescue a game once the
agent had committed to a wrong idea of the mechanism. The protocol keeps whichever
attempt scored higher, so a published score can never drop. Live numbers live in the
`MANIFEST.json` that `tools/export_traces.py` writes.

The other 14 public games aren't listed: the source material explains how they work, so
they can't be honest replication evidence. They're scored separately, never mixed in.

## Evidence chain

Every accepted result has four independently inspectable layers:

1. **Harness:** the repository commit used for the run. Stock OpenAI Codex or Claude
   Code runs headlessly against the `locus` MCP server and cannot read game source.
2. **Trace:** the workdir contains the append-only `events.jsonl`, configuration and
   pinned driver metadata in `run.json`, the agent's notes and world-model versions,
   and raw per-turn sessions.
3. **Score:** `vendor/score_trajectories.py` and `vendor/baseline_actions.csv` compute
   Regret-Human-Action-Efficiency (RHAE) from the released scoring procedure and human
   action baselines.
4. **Replay verification:** `verify.py` re-executes every recorded action
   on the ground-truth engine and requires the initial frame, every settled grid,
   running level and state, every `level_up`, and the final outcome to match.

`tools/intake.py` applies the scorer and replay gate to contributed workdirs.
`tools/export_traces.py` publishes and averages only replay-verified traces; failed
verification is quarantined rather than included in the bundle or mean. The trace's
full versioned game ID is bound to both replay and scoring, preventing a trace from
being relabeled against another game's baseline.

## Setup

Live game runs require:

- macOS, because the deny-by-default agent sandbox uses `sandbox-exec`
- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Node.js 22 or newer
- a ChatGPT subscription for Codex quota
- an ARC-AGI-3 API key

```bash
git clone https://github.com/yudduy/schema
cd schema
uv sync
npm install -g @openai/codex@0.144.1
codex login
cp .env.example .env
```

Edit `.env` and set `ARC_API_KEY`. The Codex CLI is pinned to exactly `0.144.1`;
the runner refuses other versions.

Games can only be RUN on macOS (the agent sandbox uses sandbox-exec and fails closed
elsewhere); traces can be verified/scored anywhere.

## Basic usage

Exercise the ARC environment directly:

```bash
uv run play.py
uv run agent.py --game vc33 --render
```

Run one game through the canonical Sol sweep protocol:

```bash
ONLY_RESET_LEVELS=true caffeinate -is uv run python tools/sweep.py sol <game>
```

`ONLY_RESET_LEVELS=true` is mandatory for live runs (the runner refuses without it).
The command performs the xhigh primary run, grows budgets when necessary, scores the
trace, and runs the Sol-max fallback if the primary score is below 80. It is resumable:
after an interruption, run the same command again. Progress is written to
`~/schema-sweep/progress.log`, and durable workdirs live under `~/schema-sweep/`.

A direct runner invocation is useful for controlled experiments. Keep its workdir
outside the repository:

```bash
ONLY_RESET_LEVELS=true uv run python -m schema_harness.runner \
  --provider codex --model gpt-5.6-sol --effort xhigh --game tu93 \
  --max-turns 80 --max-actions 3000 --turn-timeout 3600 \
  --no-progress-turns 8 --turn-token-cap 20000000 --run-token-cap 0 \
  --workdir /tmp/repro-tu93
```

Re-running the same command with the same workdir resumes its durable game and driver
state. Raise `--max-turns` if a long run exhausts its turn budget.

## Contribute one game run

One hard game can take 2–8 hours or longer and consume substantial subscription quota.

1. Open a GitHub issue titled `claim: <game>` so two contributors do not spend quota
   on the same game. Ask which games remain open if needed.
2. Run the one-command sweep invocation above. Use only one live game per machine.
   Network failures back off and retry; quota exhaustion idles until the window resets.
3. Do not edit anything in the workdir. Package every workdir for that game:

   ```bash
   cd ~/schema-sweep
   tar -czf <game>.tgz sol-*-<game>
   ```

4. Attach `<game>.tgz` to the claim issue. Central intake replays and re-scores the
   result, records the `events.jsonl` SHA-256, and checks the model, effort, CLI, and
   catalog pins in `run.json`.

Integrity rules are load-bearing: never change the pinned CLI during a run, never
bypass the single-live-run lock, never edit sweep workdirs, never look up a solution or
hint the agent, and always retain `ONLY_RESET_LEVELS=true`.

## What the numbers do and do not claim

Results are self-reported on the ARC-AGI-3 public set and re-verified locally by
re-executing every trace on the ground-truth engine; they are NOT ARC Prize verified.

The honest replication split treats 11 games as held out from harness and prompt
design: `cd82`, `cn04`, `lp85`, `s5i5`, `sc25`, `sp80`, `su15`, `tn36`, `tr87`,
`vc33`, and `tu93`. The other 14 public games are not clean replication evidence:
the source material discussed or mentioned 12 of them, `bp35` is the replay-parity
development game, and `r11l` was used during design iteration. Full-public-set and
clean-set means must therefore be reported separately.

Replay verification proves that the recorded actions produced the claimed grids,
levels, state transitions, and final state. It does not prove that a trace is complete:
removing an engine-inert no-op can leave every later frame identical. Consequently,
per-level action counts—and therefore RHAE—are pinned only up to inert no-ops.
Repository runs are not pruned; independent leaderboard submissions would still
benefit from spot reruns.

The public set was known in advance, and the protocol's fallback retains the better of
two attempts — effectively pass@2. Those limitations apply even when every trace is
open, and they apply to the original result as much as to this reconstruction.

Accordingly, a quoted score is incomplete unless it states the number and identity of
games covered, whether they are the held-out set or the full public set, and whether the
`<80` fallback pass ran. A partially complete sweep must not be reported as a benchmark
result, and games that are still failing must not be omitted from the mean that is
quoted.
