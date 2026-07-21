# Reproducing the Schema ARC-AGI-3 harness

This repository is an **independent, open reconstruction** of the "Schema" ARC-AGI-3
harness described by Impossible Research ([schema-harness.github.io](https://schema-harness.github.io/)).
The original method was reported with a closed harness; only its run *traces* were
released ([HF: `schema-harness/arc-agi-3-schema-traces`](https://huggingface.co/datasets/schema-harness/arc-agi-3-schema-traces)).
This harness was rebuilt **clean-room from those released artifacts** — protocol,
tool-result strings, and world-model contract only, never game solutions — and it
reproduces the reported Regret-Human-Action-Efficiency (RHAE) on live play.

Credit for the original method and the released traces belongs to Impossible Research.
This is a reproduction, not their code.

## The evidence chain

Every claim here is checkable from committed code + emitted traces:

1. **Harness** — this repo at the commit you check out (`git rev-parse HEAD`). The
   agent runs stock Claude Code / OpenAI Codex headless against the `locus` MCP
   server (14 tools); it never sees game source (deny-by-default sandbox).
2. **Per-game traces** — each run writes `events.jsonl` (the official trajectory
   schema: `run_started`/`turn_started`/`tool_started`/`turn_committed`/
   `action_taken`/`model_mispredicted`/`run_finished`), `run.json` (config + a pinned
   `driver` block: model, effort, CLI version, catalog SHA), the agent's `notes.md`
   and every `world_model_v*.py`, and the raw per-turn reasoning in `sessions/`.
3. **Scoring** — the vendored, stdlib-only `vendor/score_trajectories.py` with
   `vendor/baseline_actions.csv` (human baselines). RHAE is not our formula; it is
   the released scorer consuming our events.
4. **Contract fidelity** — `spikes/replay_parity.py` replays the released bp35 trace
   through our env: **566/566 grids byte-identical** (requires `ONLY_RESET_LEVELS=true`).

## Reproduce one game

```bash
uv sync
ONLY_RESET_LEVELS=true uv run python -m schema_harness.runner \
  --provider codex --model gpt-5.6-sol --effort xhigh --game tu93 \
  --max-turns 80 --max-actions 3000 --turn-timeout 3600 --no-progress-turns 8 \
  --turn-token-cap 20000000 --run-token-cap 0 \
  --workdir /tmp/repro-tu93        # workdir must be OUTSIDE the repo
```

Deep-planning games can exceed a single invocation's turn budget; re-run the same
`--workdir` (the game state and driver session are durable) to resume, raising
`--max-turns` as needed. Score any workdir with:

```bash
uv run python spikes/score_run.py /tmp/repro-tu93     # per-level actions + vendored RHAE
```

## Full sweep

`spikes/sweep.py {sol|opus}` runs all 25 public games serially (self-healing on
rate limits, resumable), then the blog's `<80` fallback pass, and aggregates the
benchmark mean. Results checkpoint to `~/schema-sweep/ledger.json`.
`spikes/export_traces.py` assembles the HF-style evidence package
(`~/schema-sweep/release/`, with a `MANIFEST.json` carrying per-game RHAE and
`events.jsonl` SHA-256s). Current results are in that manifest — treat it as the
source of truth, not any number hardcoded in prose.

## Honesty notes (read before citing a number)

- **Configuration.** Sol phase = GPT-5.6-Sol xhigh primary + Sol max fallback (the
  blog's Sol pairing, target 95.35%). Opus phase = Opus 4.8 max + Fable 5 fallback
  (the flagship pairing, target 98.98%). Effort `ultra` runs but is too token-heavy
  to finish long games; xhigh is the blog's own primary.
- **Contamination.** 14 of the 25 public games are not clean for a replication
  claim: the blog text read during reconstruction contains detailed mechanism
  case studies for ls20, ft09, wa30, m0r0, re86, ka59, dc22, lf52, and sb26, plus
  backtest/plan-size mentions of sk48, g50t, and ar25 (2026-07-20 re-audit of the
  full page text — earlier partial fetches missed the expandable case studies);
  bp35 is the designated dev game (its released trace drives replay parity and
  smokes); r11l was the design-iteration game. The agent itself never sees the
  blog — the contamination channel is harness/prompt design, not gameplay hints —
  but the honest split weights the **11 fully-held-out games: cd82, cn04, lp85,
  s5i5, sc25, sp80, su15, tn36, tr87, vc33, tu93** (tu93, never touched in design,
  scores 9/9 = 100%). Report the full-25 mean and the clean-11 mean separately.
- **Known critiques** of the original (self-reported; public-set-known-in-advance;
  fallback ≈ pass@2) apply to any reproduction. The mitigation here is that the
  harness and every trace are open: re-run it yourself.
