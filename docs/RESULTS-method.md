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
| v7 local equivariance, live | Fable 5 | 8 across 2 invocations | 15 | $19.694335 | 0/9, 0.00% RHAE | clean |
| v8 observed geometry | scripted dry stub | 1 | 1 | $0 | prompt plumbing only; no live evidence | clean |
| v8 observed geometry, live | Fable 5 | 7 settled + 1 timeout | 17 | at least $28.000493 | 1/9, 2.22% RHAE; L0 cleared in 17 vs human 21 | clean |
| v8 observed geometry, held-out vc33 | Fable 5 | 4 committed + 3 quota errors | 4 | $9.809703 | censored at 0/7; subscription limit before action-4 repair | clean |
| v8 observed geometry, vc33 resumed endpoint | Fable 5 | 8 productive + 3 quota errors across 2 invocations | 9 | $17.747390 | resumed after quota; fixed-turn stop at 0/7, 0.00% RHAE | clean* |
| v9 local repair and matched transfer | scripted dry stub | 1 | 1 | $0 | prompt plumbing only; no live evidence | clean |

All prompt hashes match their snapshotted `run.json` values, and the vendored scorer accepts every event stream. The v7 source and frozen run prompt match `bbd457ff…`; the v8 source, dry snapshot, BP35 snapshot, and vc33 snapshot match `934dd71e…`; the v9 source and dry snapshot match `d77045dd…`. The v4 Fable total spans four invocations; two settled turns were subscription-limit errors. Its configured run-cost cap reset on resume, so this run is not clean budget evidence. The short v4 stream lacks final telemetry, making its true cost unprovable. GOAL2 launched a concurrent paid run after the v5 live run began, so v5 is diagnostic rather than concurrency-controlled evidence. V7 waited for the other live runner to exit, but its continuation cap also reset; the final $7.000283 turn exceeded the $5 turn cap and brought the cumulative run $1.694335 above the intended $18 ceiling. V8's first invocation reached $24.656318 against a $22 cap. Its two-turn continuation added $3.344175 of settled telemetry, but GOAL2 launched another runner eight seconds after it began, and its final 1,200-second turn timed out without cost telemetry. The clear is therefore valid game evidence but not concurrency-controlled or exact total-cost evidence.

The first frozen vc33 measurement used a fresh workdir, no released trajectory or environment source, and no overlapping game runner. It produced a contiguous 78-event stream, 21 paired tool calls, four legal actions, no deaths/resets, and exact settled telemetry of $9.809703. The scorer accepted `STOPPED`, 0/7, 0.00% RHAE; the guard was clean. Turn 5 then returned `You've hit your session limit · resets 6:40am`, and two immediate identical refusals triggered the configured no-progress stop. Because only four of the pre-registered 21 actions executed, this is a valid partial trajectory but a censored held-out outcome, not an M1 pass or failure.

The unchanged continuation ran after the provider reset and after the competing runner exited. It added four productive turns, five actions, and $7.937687, producing a combined exact cost of $17.747390. Known Schema runs ended 49 seconds before and began 89 seconds after this continuation, so there was no measured overlap. The continuation restored `4/4` before action 5, then reached `5/5`, `6/6`, and `8/8`; the final action mispredicted and the fixed turn cap ended the run at nine actions, 0/7, and 0.00% RHAE. The guard remained clean, all eight durable turns are complete, and event/action indices are contiguous. One malformed turn-11 `commit_actions` request used `id` rather than `action`, leaving 38 tool starts versus 37 finishes; the corrected request committed exactly once, and scorer, ledger, timeline, and gateway state agree. This logging blemish does not change the result, but the stream is not described as fully paired.

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
- V7 fixed the targeted v6 representation error. It maintained persistent world coordinates, inferred a vertical camera offset rather than deleting world rows, and stayed replay-green at `2/2`, `4/4`, `7/7`, `8/8`, and `9/9`. Its first five turns cost $8.957332, 51% less than v6, and its then-current model was 3.4 KB.
- Probe ordering remained wasteful: three executed undo diagnostics delayed the first crate interaction until action 9. Across the full run, 32 queued actions produced 15 executions because six surprises dropped 17 suffix actions.
- On the first large scroll at action 14, v7 correctly aligned the views but saved the observation beside the model instead of seeding its newly revealed rows into initialization. It accepted a `13/14` backtest as a “permanent documented mismatch,” then acted while replay was red. The next click also mispredicted, leaving the final model at most `13/15`; no level cleared and no terminal flag, `is_goal`, or BFS call appeared.
- Local BFS does not search `is_goal`; that function only enables the tool. Search succeeds only when `predict()` emits `level_up` or `win`. A replay-green model can therefore be non-plannable.

The released successful BP35 artifact resolves the reveal-replay ambiguity. Its level-0 snapshot is 10,061 bytes and contains 60 exact observed `LEVEL0_TOP` rows; its final 92,468-byte model contains 1,016 literal map rows. Its notes explicitly describe ingesting observations and seeding newly revealed per-level geometry before returning to a green backtest. Thus v7's blanket large-frame prohibition contradicted the published method on the contaminated development game.

V8 matched that published level-0 representation pattern and passed the development gate:

- The first scroll occurred at action 4. V8 uniquely registered camera offset `-18`, mechanically extracted 18 newly observed static rows, injected them into `init_state`, locally replayed all four transitions with zero diffs, and obtained `4/4` from the harness backtest before acting.
- The next click-and-scroll added 24 more observed rows and returned `5/5`. Subsequent checkpoints were `6/6`, `12/12`, and `13/13`; no reveal was accepted as a permanent exception.
- The level cleared at action 17, four actions below the human baseline and two below the released Fable trajectory. The scorer reports 1/9 and 2.22% RHAE. The contact initially missed only the terminal flag (`16/17`, `#16:level_up`); the timed-out final turn still installed the flag and restored `17/17` before falling back without an action.
- The final 13,180-byte model contains exactly 60 literal observed rows, matching the released level-0 seed count. Across seven committed turns, 25 proposed actions produced 17 executions; six surprises dropped eight suffix actions. No `is_goal` or BFS call appeared.
- Phase latency is now dominant: several repair turns spent long periods before their first tool call, and the level-1 terminal repair exhausted the 1,200-second timeout despite requiring only one evidenced flag. Geometry fidelity no longer explains the stall.

The partial vc33 trace exercised a different failure mode before provider censoring:

- The first model backtested `2/2`; action 3 falsified it. The replacement backtested `3/3` before action 4, preserving the fail-closed replay discipline.
- Action 4 changed 265 cells and disagreed with the model on 264 of them. The subscription limit arrived before the informative transition could be inspected or repaired, so neither predictive-world-model failure nor recovery can yet be judged.
- An independent Codex subscription review (`gpt-5.6-sol` through Omnigent 0.5.1, conversation `conv_fcffb42e9de944a38928bb981a8dd798`) agreed that the run is censored and should resume unchanged with `4/4` replay required before action 5. Its proposed removal of observed-geometry seeding is not adopted: vc33 has shown no camera reveal, so that change would not target the observed mismatch.

The completed vc33 continuation isolated the next bottleneck:

- V3 repaired the 264-cell action-4 surprise immediately and returned to `4/4`. Its conveyor geometry then predicted all five resumed actions exactly, including an unobserved inverse transition and a reduced-change edge regime.
- The only later errors were single HUD cells. Observed increments were `1,2,1,1,1,2,1,1,2`. After the first repeat mismatch, the agent promoted a sparse global rule, “`+2` when global click index is 2 modulo 4,” even though object-local interaction history remained replay-consistent. Two upper-control actions happened to fit; the lower control's second activation falsified the global clock.
- Exact replay and the prediction gate worked: both failed batches stopped after one action. Generalization selection failed. Rejecting one narrow per-button rival did not eliminate the broader matched-control family, and an auxiliary HUD coincidence blocked six otherwise modeled progress actions.
- Turn 10 reached 155,928 context tokens and correctly rolled to a fresh session for turn 11. That final turn spent 514 seconds and $2.944915 planning a seven-action queue; its first one-cell HUD mismatch ended the fixed-turn run before repair.

This supports a local counterexample-repair prior rather than a broader disagreement prompt. [OPINE-World](https://arxiv.org/abs/2607.01531) likewise factors effects by object type, action, and local context, but its published coarse effect signatures discard exact magnitudes and it reports no component ablation. V9 therefore keeps magnitude and adds only matched-object transfer: preserve unaffected rules, prefer typed/local/per-object state over absolute step index or periodicity, and state a held-out prediction before one isolated probe.

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

After v7 and inspection of the released BP35 success, the next-change distribution is: retrospective exact geometry seeding 55%, decision-relevant probe ordering 20%, phase/cost control 12%, neighbor-conditioned rule repair 8%, and terminal-flag/BFS coupling 5%. V8 changes only v7's no-frame paragraph. It permits mechanically extracted, observed-only static row or tile seeds, requires persistent-coordinate overlap and dimension assertions, and forbids inventing unseen cells or mixing sprites, HUD, and mutable objects into static seeds. The decisive signature is that a first-scroll `N-1/N` mismatch becomes fully green on the next repair cycle.

V8 produced that signature and a development-game clear, so it remained frozen through the administratively interrupted vc33 continuation. The completed endpoint makes the next distribution: local repair plus matched-object transfer 42%, decision-targeted nuisance gating 22%, magnitude-sensitive typed effect tables 16%, phase/short-batch control 12%, and terminal-flag/BFS coupling 8%. V9 selects only the first. Its decisive signature is that a localized counterexample does not trigger a global clock: notes retain the matched-control prediction, and the first unqueried same-type control transfers without a HUD-only surprise. Generic disagreement is not selected because v8 already maintained and tested rivals; its candidate class, not its willingness to compare, was wrong.

## Status and Uncertainty

M0 remains proven, and the contaminated development gate passes: v8 cleared BP35 level 0 within its 21-action boundary while maintaining exact retrospective replay. M1 is still not satisfied because the resumed-after-quota vc33 measurement ended at 0/7 after nine actions. That trajectory is now development evidence for v9 and will not be reused as held-out validation. V9 has passed only the scripted prompt/scorer/guard check; its next required evidence is a bounded run on an untouched game, with a clean level clear before any full-game claim. No claim of generalization or ~99% reproduction is currently supported.
