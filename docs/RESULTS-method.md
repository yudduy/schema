# Method Results

This ledger covers GOAL1 prompt/reasoning experiments. BP35 is the contaminated development game; it is diagnostic evidence, not a held-out result. Its human action baseline is `21/48/44/38/33/87/86/131/163` (651 total).

## Measured Runs

| Method | Model | Turns settled | Actions | Cost | Result | Audit |
| --- | --- | ---: | ---: | ---: | --- | --- |
| No standing prompt | Opus 4.8 | 5 | 6 | $3.5256175 | 0/9, 0.00% RHAE | clean |
| v1 physicist loop | Opus 4.8 | 5 | 5 | $1.426043 | 0/9, 0.00% RHAE | clean |
| v2 falsification | Opus 4.8 | 14 | 28 | $10.926530 | 0/9, 0.00% RHAE | clean |
| v3 state machine | Opus 4.8 | 9 | 15 | $7.700376 | 0/9, 0.00% RHAE | clean |
| v4 affordance, short diagnostic | Opus 4.8 | 5 of 6 started | 6 | at least $1.7059355 | incomplete stream; scorer derives 0/9 | clean |
| v4 affordance, frontier | Fable 5 | 11 | 23 | $42.698812 | 0/9, 0.00% RHAE | clean |
| v5 causal compression | scripted dry stub | 1 | 1 | $0 | current prompt plumbing only; no live evidence | clean |
| v5 causal compression, live | Fable 5 | 5 attempted, 4 committed | 8 | $16.775050 | 0/9; outcome invalidated by turn-5 OAuth 401 | clean |
| v6 surprise ownership, live | Fable 5 | 5 | 7 | $18.327839 | 0/9, 0.00% RHAE | clean |

All prompt hashes match their snapshotted `run.json` values, and the vendored scorer accepts every event stream. The current v5 dry and live copies match source hash `ca868338…`. The v4 Fable total spans four invocations; two settled turns were subscription-limit errors. Its configured run-cost cap reset on resume, so this run is not clean budget evidence. The short v4 stream lacks final telemetry, making its true cost unprovable. GOAL2 launched a concurrent paid run after the v5 live run began, so v5 is diagnostic rather than concurrency-controlled evidence.

## Bottleneck Diagnosis

- The control and v1 often acted without an executable model.
- V2 modeled earlier but committed to an unsupported “clear everything” goal.
- V3 repeatedly repaired pixels instead of revising its causal representation.
- V4 discovered topology, passive ascent, click affordances, and a replay-green model, but spent heavily reconstructing the renderer. It never used BFS, accumulated eight mispredictions, and remained at level 1 after 23 actions.
- V5 installed its first model after one real probe and immediately backtested each revision green (`1/1`, `4/4`, `5/5`). Its prediction gate stopped all three surprises, so action discipline improved.
- V5 still copied three literal 64×64 frames (12,477 bytes), grew the model from 3.4 KB to 8.3 KB, and carried the full pixel map as latent state. One copied row had 76 cells and needed repair. It inferred coupled click, passive-rise, and camera effects without isolating which changed regions the click caused. No model exposed `is_goal`, and BFS was never called.
- Turn 5 failed with an expired OAuth token after $1.669633 and no action. Therefore 0/9 at eight actions is observed, but the pre-registered no-clear-by-action-21 outcome was not measured.
- V6 explicitly aligned the first surprise in persistent world coordinates, separated sprite, camera, HUD, and world changes, marked one-shot rules provisional, and used single-action probes for undo and click semantics. Its observed click rule was supported by exactly 35 changed world cells rather than already-equal pixels.
- V6 remained inefficient: six 4,159-byte full-grid files, full-map latent state, four mispredictions, and no fully green replay after the first surprise (`3/4` through `5/6`). Models were smaller than v5 (1.7 KB to 5.6 KB), but no model exposed `is_goal`, BFS was never called, and the post-hoc $18 cap overshot by $0.327839.
- Local BFS does not search `is_goal`; that function only enables the tool. Search succeeds only when `predict()` emits `level_up` or `win`. A replay-green model can therefore be non-plannable.

This supports the executable-world-model distinction between data fit and plannability described by [Schema Harness](https://schema-harness.github.io/), [WorldCoder](https://arxiv.org/abs/2402.12275), and [Executable World Models](https://arxiv.org/abs/2605.05138).

## Current Experiment Portfolio

Before the v5 live run, the candidate distribution was:

- v5 unchanged causal compression — 35%: measure first to preserve attribution.
- provisional terminal flags plus BFS — 30%: strongest targeted fix if v5 is replay-green but does not search.
- event-conditioned surprise ownership — 15%: use only for repeated patch cycles or replay-green stalls.
- one generic worked example — 10%: higher variance; may improve compliance but risks imitation.
- a shorter imperative/minimal nudge — 10%: cheaper, but earlier terse prompts under-specified goal discovery.

Changes remain sequential. Do not combine the BFS and surprise-routing ablations unless their individual signatures have first been measured.

After v5, evidence shifted the next-change distribution to: identifiability-gated surprise ownership 40%, terminal-flag/BFS coupling 25%, a stronger structural-map rule 20%, a shorter phase checklist 10%, and a worked example 5%. V6 selects only the first. It requires persistent-coordinate alignment, explicit ownership of each changed region, and refuses to generalize from cells that already had the post-action value. The terminal/BFS correction remains untested and separate.

After v6, the distribution shifted again: local-equivariance/coordinate alternatives 37%, terminal-flag/BFS coupling 25%, phase/cost control 18%, raw-literal budgeting 14%, and a worked example 6%. V7 selects only local equivariance. It treats a one-off global shift or deletion of repeated static geometry as a camera-coordinate hypothesis until a matched second transition forces world mutation.

## Status and Uncertainty

M0 remains proven. M1 is not satisfied: no clean game has cleared a level. The next required evidence is a fresh v7 BP35 diagnostic, followed by a held-out run only if replay becomes fully green and the predicted process signature appears. No claim of generalization or ~99% reproduction is currently supported.
