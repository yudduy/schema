# Tooling Experiment Results

Status: active on `iter/tooling`. No clean-game milestone is claimed yet.

## Reproduced mechanics

- `uv run pytest tests/ -q`: **51 passed**.
- Released bp35 replay: **9/9 levels, 93.51% RHAE**, with all **566 grids byte-identical**. Level actions were `19/47/36/22/59/42/57/67/217` versus the human baseline `21/48/44/38/33/87/86/131/163`.
- Final scripted dry run: vendored scorer accepted; `audit_events` returned `clean=True` with no violations.
- The public [Schema Harness report](https://schema-harness.github.io/) and its [aggregate trace manifest](https://huggingface.co/datasets/schema-harness/arc-agi-3-schema-traces) support score reproduction, but do not publish a runnable harness or enough discarded-attempt data to reproduce the reported live run procedure exactly.

## Candidate portfolio

Before the first live diagnostic, the candidate distribution was:

| Mechanism | Prior | Status |
|---|---:|---|
| Automatic click-target discovery | 35% | Not tried; next planner experiment |
| Localized model-mismatch diagnostics | 25% | Implemented in `read_history`; consumed in both live diagnostics |
| Goal-directed/deeper search | 20% | Not tried |
| Frame/object diff inspector | 12% | Implemented first as the lower-probability ergonomic bet |
| Session pacing/rollover | 8% | Not tried |

Security isolation was added as a non-negotiable validity prerequisite after adversarial review, rather than counted as a performance hypothesis.

## Measured runs

| Run | Change under test | Model / budget | Result | Audit / status |
|---|---|---|---|---|
| `/tmp/schema-live-bp35-base.qhdrvf` | Pre-change baseline | Opus 4.8, low, 5 turns, 6 actions, **$3.5256** | **0/9, 0.00% RHAE**; no completed level | Dev-game diagnostic only. The old lexical audit said clean, but later testing proved its process boundary was bypassable, so this is not certifying evidence. |
| `/tmp/schema-live-bp35-inspector.Msg3Tt` | Region/value-pair history inspector | Opus 4.8, low, 5 turns, 5 actions, **$2.4871** | **0/9, 0.00% RHAE**; no completed level | Vendored scorer accepted; hardened audit clean. Same cap, one replicate, contaminated dev game. |
| `/tmp/schema-live-bp35-sol2.zD9mZf` | Codex driver + inspector smoke | GPT-5.6 Sol, xhigh, 3 turns, 4 actions | **0/9, 0.00% RHAE**; no completed level | Vendored scorer accepted; audit clean. Different driver/model, so not part of the A/B. |

The matched Opus run used 21 tools versus 45 at baseline (**53.3% fewer**) and `run_python` twice versus 12 times (**83.3% fewer**). Its measured cost was **29.5% lower**. It consumed the inspector on turns 2 and 4, backtested on four turns instead of one, and stayed in one Claude session instead of rolling over after every turn. These are strong ergonomic/context signals, but there was no gameplay gain and one replicate cannot establish causality.

The new `read_history(detail="full")` suffix reports bounded 4-connected regions, bounding boxes, per-region value counts, exact small before/after patches, and global value-pair counts. On baseline transition `#2`, it directly exposed two 23-cell regions plus one independent status cell, without game-specific semantics. In the Sol smoke, its 1,000-character turn-2 report supplied the same movement structure, but Sol also read the recursively growing raw event log: **11,317 → 104,528 → 278,797 characters** over three turns. That dominated 85.6% and 92.6% of tool-output characters on turns 2 and 3, identifying raw-log visibility as the next bottleneck.

## Validity hardening in this iteration

- `run_python`, `run_shell`, model installation, backtesting, BFS, and commit-time prediction now execute behind a deny-by-default macOS process boundary.
- Commit prediction uses a persistent per-action worker, preserving exact predict/real-step interleaving and arbitrary latent state.
- Harness-owned event, gateway, prompt, configuration, and session paths are immutable to agent tools. Raw events, timeline, ledger, live-model pointer, debug logs, credentials, and sessions are unreadable through file tools or sandboxed children; `runtime/gateway_state.json` remains readable as a computational form of the current observation already present in the prompt. Tests cover traversal, variables, symlinks, pre-existing hard links, edit-clone/write aliases, nested interpreters, `ctypes`, subprocesses, network, worker timeout, and durable ledger completion.
- `run_backtest(start=...)` again emits the required `[range #a..#b]` prefix; all 14 public tool schemas remain unchanged.
- Final structured Codex autoreview (GPT-5.5, high): **clean, no actionable findings** (confidence 0.82).

## Clean-evaluation ledger

- Discarded from clean evidence: any externally exposed trajectory details, and the concurrently running pre-existing game session.
- Candidate held-out sequence: `tu93` plus a second untouched public game, after the next bp35 planner iteration.
- M1–M3 remain unproven. Competitive held-out RHAE and two-game generalization are still required.
