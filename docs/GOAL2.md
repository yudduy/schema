# GOAL — Give the agent a better laboratory

> Paste the `/goal` block into Codex to activate. This objective bets that the harness's live
> performance is bottlenecked by its **tools and search machinery**, not by the model's reasoning.
> Work on branch `iter/tooling`.

---

## The Goal (paste into `/goal`)

```
/goal Raise the Schema harness's live ARC-AGI-3 performance by improving its TOOLS AND SEARCH — the
framework the agent works through — so a live frontier model (Opus 4.8 / Fable 5 via `claude -p`)
clears levels efficiently. Verified by RHAE from `vendor/score_trajectories.py` vs the human
baselines in `vendor/baseline_actions.csv`, with `uv run pytest tests/ -q` green and
`schema_harness.guard.audit_events` clean after every measured run. The harness MUST stay strictly
game-agnostic (no per-game code, data, or hints) and honest (never read game source in
`environment_files/`, never read any released Schema trajectory except bp35's in `vendor/`). Your
primary levers are FRAMEWORK ERGONOMICS (make `run_backtest`/`run_bfs`/the world-model install
frictionless and instructive), PLANNER POWER (BFS click-target enumeration, stronger/deeper search,
subgoals), and EXECUTOR/RUNTIME robustness (session rollover, budget pacing, richer read-only
inspectors). At each decision, do NOT jump to the single obvious change: verbalize 4–5 candidate
mechanisms with rough plausibilities and pursue a diverse spread across runs so the search does not
collapse to one mode. Between iterations record what you changed, RHAE and levels-cleared
before/after, and the mechanisms you did and did not try. Complete only when a held-out clean game
is cleared with RHAE competitive with its human baseline AND the winning change is shown to
generalize across ≥2 games. If blocked (budget, or the only gains left require forbidden material or
game-specific hints), STOP with a claim-by-claim ledger separating reproduced mechanics, measured
performance, blocked claims, and uncertainty.
```

---

## Mission context (read first)

Impossible Research's **Schema** harness reached ~99% RHAE on ARC-AGI-3 by having a frontier model
play like a physicist: write the game's mechanism as an executable Python world model, backtest it
against the whole interaction history, plan inside it with BFS, and commit actions only through a
misprediction-gated executor. This repo reconstructs that harness from its leaked run artifacts.

- `docs/contract.md` — the frozen turn protocol (message templates, tool strings, world-model
  contract, halt narrations, tool signatures, driver + auth recipe). **This is the spec; conform.**
- `docs/step1_spec.md` / `docs/step2_spec.md` / `docs/step3_spec.md` — how each layer was built.
- Framework internals to study: `schema_harness/backtest.py`, `schema_harness/bfs.py`,
  `schema_harness/model_loader.py`, `schema_harness/locus.py`, `schema_harness/gateway.py`.

## Current state — PROVEN vs the GAP

**Proven (keep green, don't redo):** the machinery is faithful — replaying Schema's own bp35 run
reproduces their exact score (93.51, 566/566 grids); their world models backtest green through our
framework; `pytest` = 30+ green. The live loop works — a real agent loads the `locus` MCP tools,
writes an executable `world_model_v*.py`, backtests it, and commits; events are scorer-valid.

**The GAP (your target):** our *live* runs on *unseen* games do not yet reproduce that performance.
The blog credits the **executable harness itself** — persistent, exactly-verifiable, searchable —
for the action-efficiency, so the tools' quality is a first-class lever. Likely friction in your
direction: does the agent mis-call `run_backtest`/`run_bfs`? Are error/install messages instructive
enough to self-correct from? Is BFS tractable on click-only games (it needs a good `clicks=[[x,y]…]`
target set)? Does the planner find plans the agent then trusts? Do long runs degrade (session
rollover, notes bloat, budget)? Because the exact original tools are unrecoverable, the honest
target is **strongest measured performance + honest audit**, not an unconditional 99%.

## Your lever region (in priority order)

1. **Framework ergonomics** — frictionless, self-explaining `run_backtest` (localized mismatch
   diagnostics), `run_bfs`, and world-model install messages; instructive errors the agent can
   recover from without a human.
2. **Planner power** — click-target enumeration for click-only games; stronger/deeper search
   (frontier ordering, iterative deepening, subgoal decomposition) while keeping the released
   output-string contract; make plans the agent will actually commit.
3. **New read-only primitives** — inspectors that make discovery cheaper (frame-diff, object/motion
   isolation, replay-a-transition), so long as they stay game-agnostic and leak no game source.
4. **Executor / runtime robustness** — session rollover near context limits, budget pacing,
   idempotent recovery, notes-size handling under long runs.

Stay out of the system prompt / method wording unless a tooling change requires a matching
one-line hint; your bet is that the laboratory, not the mind, is the bottleneck.

## Explore diversely (verbalized sampling — avoid mode collapse)

Aligned models collapse to one "obvious" answer. Counter it: before each change, **verbalize a
small distribution** — e.g. 4–5 candidate mechanisms (richer backtest diagnostics · smarter BFS
frontier · a new inspector tool · a subgoal planner · better error-message coaching · session/budget
pacing) — each with a rough plausibility, then deliberately pursue a **diverse subset across runs**,
including at least one lower-probability, higher-variance bet. Record the alternatives you didn't
take; revisit them when the modal choice stalls. The point is a portfolio of experiments, not the
single most likely one.

---

## Operating contract

- **Verification surface:** RHAE via `vendor/score_trajectories.py` (+ `baseline_actions.csv`);
  `pytest tests/` green; `audit_events(<workdir>/events.jsonl, '.')` clean after measured runs.
- **Constraints (must not regress):** all tests green; `docs/contract.md` fidelity (message
  templates, tool strings, world-model interface, scorer parity, `run_bfs`/`run_backtest` output
  formats); anti-cheat guard intact.
- **Boundaries — edit:** `schema_harness/**`, `spikes/driver_probe.py`, `tests/**`, `docs/**`.
  **Never read:** game source (`environment_files/**/*.py`) or any non-bp35 Schema trajectory.
  Reading OUR OWN agent's run transcripts is expected.
- **Blocked stop:** budget reached, or remaining gains need forbidden material or game-specific
  logic. A budget stop is not completion — summarize progress, blocker, and the unlocking input.

## Milestone ladder (report only what evidence proves)

- **M0** mechanically faithful — DONE. · **M1** clear ≥1 level on a clean game (RHAE > 0). ·
  **M2** clear a full clean game. · **M3** M2 on ≥2 clean games with the same game-agnostic harness
  (generalization). · **M4** per-game RHAE approaching the blog's, then a full-set sweep (separate go).

## Hard rules

- **Game-agnostic always** — no per-game constants/branches/hints. The agent discovers each game.
  This is both the honesty guarantee and the research claim.
- **Contamination** — diagnose on bp35 (contaminated dev game; its artifacts are in `vendor/`);
  measure on clean held-out games; never hand-tune to a measurement game's specifics.
- **Evidence over belief** — a milestone is reached only when the scorer + transcript prove it.

## Run / score / diagnose

```bash
# Cheap diagnostic on the contaminated dev game (low effort, short cap):
ONLY_RESET_LEVELS=true uv run python -m schema_harness.runner \
  --game bp35 --model claude-opus-4-8 --effort low --turn-timeout 1200 \
  --max-turns 20 --run-cost-cap 8 --workdir ~/agent-bp35-dev

# Bounded MEASUREMENT on a clean game (faithful pairing = Opus 4.8 max):
ONLY_RESET_LEVELS=true uv run python -m schema_harness.runner \
  --game r11l --model claude-opus-4-8 --effort max --turn-timeout 1200 \
  --max-turns 120 --turn-cost-cap 4 --run-cost-cap 40 --workdir ~/agent-r11l

# Score:
mkdir -p /tmp/scr/claude_fable_opus/run && cp ~/agent-r11l/events.jsonl ~/agent-r11l/run.json /tmp/scr/claude_fable_opus/run/
cp vendor/baseline_actions.csv /tmp/scr/claude_fable_opus/ && cp -r /tmp/scr/claude_fable_opus /tmp/scr/gpt_5_6_sol
python3 vendor/score_trajectories.py --root /tmp/scr --expected 0 --no-manifest-check
```
Also exercise the framework directly against the released bp35 model/timeline in `vendor/` (that is
how backtest/BFS fidelity is proven) and read the agent's own `events.jsonl` to find *tooling*
friction (mis-calls, unhelpful errors, intractable BFS). Frontier runs are ~$1.5/turn: diagnose
cheap, measure bounded.

## Final artifact

End every stop (complete or blocked) with `docs/RESULTS-tooling.md`: per-game entries naming the
tooling/search change under test, the run (model/effort/turns/cost/audit), levels cleared + RHAE +
per-level actions vs the human baseline, status, and remaining uncertainty. Keep
reproduced-mechanics, measured-performance, blocked, and uncertain distinct — never flatten into one
"success."

## Work hygiene

Commit scoped changes on branch `iter/tooling` behind green tests. Keep `main` untouched until a
change is generalization-validated.
