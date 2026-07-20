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
- **Contamination.** Several public games' mechanics were visible in the blog text
  read during reconstruction (bp35 is the designated dev game; ls20/ft09/wa30/m0r0
  and trace-card mentions sk48/g50t/ar25 were discussed). The agent plays them fresh,
  but a clean-room replication should weight the fully-held-out games — e.g. **tu93,
  never touched in design, scores 9/9 = 100%** — over the spoiled ones. Report the
  full-25 mean and a clean-subset mean separately.
- **Known critiques** of the original (self-reported; public-set-known-in-advance;
  fallback ≈ pass@2) apply to any reproduction. The mitigation here is that the
  harness and every trace are open: re-run it yourself.
