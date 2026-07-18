# Tooling Experiment Results

Status: active on `iter/tooling`. No clean-game milestone is claimed yet.

## Reproduced mechanics

- `uv run pytest tests/ -q`: **50 passed**.
- Released bp35 replay: **9/9 levels, 93.51% RHAE**, with all **566 grids byte-identical**. Level actions were `19/47/36/22/59/42/57/67/217` versus the human baseline `21/48/44/38/33/87/86/131/163`.
- Final scripted dry run: vendored scorer accepted; `audit_events` returned `clean=True` with no violations.
- The public [Schema Harness report](https://schema-harness.github.io/) and its [aggregate trace manifest](https://huggingface.co/datasets/schema-harness/arc-agi-3-schema-traces) support score reproduction, but do not publish a runnable harness or enough discarded-attempt data to reproduce the reported live run procedure exactly.

## Candidate portfolio

Before the first live diagnostic, the candidate distribution was:

| Mechanism | Prior | Status |
|---|---:|---|
| Automatic click-target discovery | 35% | Not tried; next planner experiment |
| Localized model-mismatch diagnostics | 25% | Not tried |
| Goal-directed/deeper search | 20% | Not tried |
| Frame/object diff inspector | 12% | Implemented first as the lower-probability ergonomic bet |
| Session pacing/rollover | 8% | Not tried |

Security isolation was added as a non-negotiable validity prerequisite after adversarial review, rather than counted as a performance hypothesis.

## Measured runs

| Run | Change under test | Model / budget | Result | Audit / status |
|---|---|---|---|---|
| `/tmp/schema-live-bp35-base.qhdrvf` | Pre-change baseline | Opus 4.8, low, 5 turns, 6 actions, **$3.5256** | **0/9, 0.00% RHAE**; no completed level | Dev-game diagnostic only. The old lexical audit said clean, but later testing proved its process boundary was bypassable, so this is not certifying evidence. |
| Post-change bp35 | Region/value-pair history inspector | Same cap planned | Pending | Waiting to avoid overlapping another live subscription run. |

The baseline used `run_python` 12 times; at least 9 calls reparsed the agent's own event grids to recover coordinates, bounds, motion, local shapes, or value transitions. The new `read_history(detail="full")` suffix reports bounded 4-connected regions, bounding boxes, per-region value counts, exact small before/after patches, and global value-pair counts. On baseline transition `#2`, it directly exposed two 23-cell regions plus one independent status cell, without game-specific semantics.

## Validity hardening in this iteration

- `run_python`, `run_shell`, model installation, backtesting, BFS, and commit-time prediction now execute behind a deny-by-default macOS process boundary.
- Commit prediction uses a persistent per-action worker, preserving exact predict/real-step interleaving and arbitrary latent state.
- Harness-owned event, gateway, prompt, configuration, and session paths are immutable to agent tools; private configuration is unreadable. Tests cover traversal, variables, symlinks, hard links, nested interpreters, `ctypes`, subprocesses, network, worker timeout, and durable ledger completion.
- `run_backtest(start=...)` again emits the required `[range #a..#b]` prefix; all 14 public tool schemas remain unchanged.
- Final structured Codex autoreview: **clean, no actionable findings**.

## Clean-evaluation ledger

- Discarded from clean evidence: any externally exposed trajectory details, and the concurrently running pre-existing game session.
- Candidate held-out sequence: `tu93` plus a second untouched public game, only after the bp35 A/B run establishes whether the inspector changes behavior.
- M1–M3 remain unproven. Competitive held-out RHAE and two-game generalization are still required.
