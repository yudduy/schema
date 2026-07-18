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

All prompt hashes match their snapshotted `run.json` values, and the vendored scorer accepts every event stream. The current v5 dry copy matches source hash `ca868338…`. The v4 Fable total spans four invocations; two settled turns were subscription-limit errors. Its configured run-cost cap reset on resume, so this run is not clean budget evidence. The short v4 stream lacks final telemetry, making its true cost unprovable.

## Bottleneck Diagnosis

- The control and v1 often acted without an executable model.
- V2 modeled earlier but committed to an unsupported “clear everything” goal.
- V3 repeatedly repaired pixels instead of revising its causal representation.
- V4 discovered topology, passive ascent, click affordances, and a replay-green model, but spent heavily reconstructing the renderer. It never used BFS, accumulated eight mispredictions, and remained at level 1 after 23 actions.
- Local BFS does not search `is_goal`; that function only enables the tool. Search succeeds only when `predict()` emits `level_up` or `win`. A replay-green model can therefore be non-plannable.

This supports the executable-world-model distinction between data fit and plannability described by [Schema Harness](https://schema-harness.github.io/), [WorldCoder](https://arxiv.org/abs/2402.12275), and [Executable World Models](https://arxiv.org/abs/2605.05138).

## Current Experiment Portfolio

Before the next prompt change, the candidate distribution is:

- v5 unchanged causal compression — 35%: measure first to preserve attribution.
- provisional terminal flags plus BFS — 30%: strongest targeted fix if v5 is replay-green but does not search.
- event-conditioned surprise ownership — 15%: use only for repeated patch cycles or replay-green stalls.
- one generic worked example — 10%: higher variance; may improve compliance but risks imitation.
- a shorter imperative/minimal nudge — 10%: cheaper, but earlier terse prompts under-specified goal discovery.

Changes remain sequential. Do not combine the BFS and surprise-routing ablations unless their individual signatures have first been measured.

## Status and Uncertainty

M0 remains proven. M1 is not satisfied: no clean game has cleared a level. The next required evidence is a live v5 BP35 diagnostic, followed by a fresh held-out run of the selected unchanged method. No claim of generalization or ~99% reproduction is currently supported.
