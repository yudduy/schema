# Step 1 spec — gateway + event log + replay verifier (no LLM)

Build the deterministic backbone of the Schema harness: the process that owns the ARC-AGI-3 env,
executes committed action queues with world-model self-check, and writes the released `events.jsonl`
schema — verified by replaying the released bp35 trajectory through it and reproducing bp35's RHAE via
the vendored scorer. NO LLM anywhere in this step.

Read first: `docs/contract.md` (frozen protocol), `spikes/replay_parity.py` (working env-replay
reference), `vendor/score_trajectories.py` (scoring source of truth), `.venv/.../arc_agi/` (toolkit).

## Files to create (package `schema_harness/`)

1. `schema_harness/events.py` — event dataclasses + an append-only `EventLog` writer that emits exactly
   the released schema (kinds: run_started, turn_started, text_delta, tool_started, tool_finished,
   model_mispredicted, turn_committed, turn_fallback, action_taken, run_finished; common fields
   kind/seq/ts). `ts` MUST be injectable (pass a clock; do not call time.time() implicitly) so replays
   are deterministic. Atomic append: write line + `flush()` + `os.fsync()`.

2. `schema_harness/gateway.py` — `Gateway` owning one `arc_agi.Arcade` env for one game:
   - Construct env with `ONLY_RESET_LEVELS=true` set in `os.environ` BEFORE `arc.make(game, seed=0)`
     (VERIFIED-required; see contract §0). Assert it's set.
   - Maintain an append-only in-memory timeline of transitions `(step_index, action, x, y, grid,
     level, state, level_up, dead, win)`. `grid` = `np.asarray(frame.frame[-1], dtype=int).tolist()`.
   - `level_up := (levels_completed increased) OR (state transitioned to WIN)`;
     `dead := state==GAME_OVER`; `win := state==WIN`. RESET = action 0 is a scored action_taken.
   - `execute_queue(actions, live_model=None, max_actions=3000)`:
       * If `live_model is None`: execute exactly ONE action, halt reason
         `no-world-model-single-step` ("...only this one step ran (exploring)").
       * Else before each real step, ask the model to predict the next grid+flags; take the real
         step; compare. First mismatch ⇒ emit `model_mispredicted` (predicted+actual grids), stop,
         halt reason `surprise`. Surface both surprise string variants from contract §5.
       * Stop on level_up, dead(after emitting), win(completed), or max_actions.
       * Return a structured result: executed count, halt reason, net level a→b, net state X→Y —
         enough to render the exact "Result of your last commit: ..." narration (contract §3).
   - For THIS step, `live_model` is a simple Python callable interface (defined in a `WorldModel`
     Protocol); the sandboxed-subprocess execution comes in Step 2 — keep the call site behind the
     Protocol so Step 2 swaps the implementation.

3. `schema_harness/narration.py` — pure functions building the commit-result narration and the
   surprise strings, matching contract §3/§5 byte-for-byte (parameterized by the golden templates).

4. `schema_harness/replay_verify.py` — the acceptance harness (no LLM):
   - Load a released `events.jsonl`, extract its `turn_committed` plans in order (the exact action
     queues the agent committed).
   - Drive the Gateway with those queues (no model — pure env replay, like the spike) and emit OUR
     `events.jsonl` into an output dir laid out for the vendored scorer
     (`<out>/<collection>/<model>_<effort>_<game>_<score>/events.jsonl` + a `run.json`).
   - Then run `vendor/score_trajectories.py --root <out> --expected 0 --no-manifest-check` as a
     subprocess and assert it reports bp35's RHAE (93.51) and 9/9 levels.
   - Also assert our emitted `action_taken` grids byte-match the released ones (reuse spike logic).

## Tests (`tests/`, pytest, no network beyond the one env download already cached)
- `test_events.py`: EventLog emits valid JSONL, seq monotonic, fsync called (monkeypatch), schema fields present.
- `test_gateway_reset.py`: with ONLY_RESET_LEVELS=true, two consecutive RESETs keep the level (regression for the parity bug).
- `test_replay_bp35.py`: end-to-end — replay bp35's committed plans, our events.jsonl scores to 93.51 via the vendored scorer, and grids byte-match. This is the Step 1 gate.

## Constraints
- Deterministic: no implicit wall-clock; inject a monotonic fake clock in replay/tests.
- Only depend on: stdlib, numpy, arc_agi. No new pip deps.
- Do NOT read any game's source in `environment_files/`. Do NOT read released notes/world-models of
  any game other than bp35. Keep functions small; match existing repo style (uv project, py3.12).
- Do NOT run the vendored scorer's manifest check against released CSVs (that tests their data).
