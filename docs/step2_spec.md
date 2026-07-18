# Step 2 spec — framework: model loader + run_backtest + run_bfs (no LLM)

Build the world-model execution + verification layer. Acceptance gate: the RELEASED bp35
`world_model_v5.py` must **backtest green** against the released bp35 timeline through OUR framework,
and run_bfs must find a level-up plan on it. Sandboxing (sandbox-exec) is a SEPARATE later task — do
not build it here; run models in-process for now behind a clean function boundary.

Read first: `docs/contract.md` (§2 world-model contract, §5 tool strings), `schema_harness/gateway.py`
(Transition/Grid types, reuse them), and `vendor/bp35_events.jsonl` (the released timeline + the
released world model is fetched below).

## Inputs available
- `vendor/bp35_events.jsonl` — released bp35 run (action_taken events carry the AFTER grid per step;
  the first `turn_started` carries the initial grid).
- `vendor/bp35_world_model_v5.py` — the released final world model (copy it in from
  `/private/tmp/.../hf_peek/bp35_world_model_v5.py`; a task step below tells you the path).

## World-model contract (exact, from the released model)
- `init_state(entry_grid) -> state` (reads module global `CURRENT_LEVEL`).
- `predict(state, grid, action, x=None, y=None) -> (predicted_after_grid, info, new_state)`.
  `grid` is the BEFORE grid (the frame the action was taken from). `info` is a dict with optional
  keys `level_up` / `dead` / `win` (present only when True). State threads forward via `new_state`.
- Module global `CURRENT_LEVEL` (int, 0-based) must be set on the module BEFORE `init_state`/`predict`
  for each level segment. Some models also define `step(...)` (stateless) instead of `predict`.

## Files to create (`schema_harness/`)
1. `model_loader.py` — load a world-model `.py` as an isolated module object (exec into a fresh module
   namespace, NOT `sys.modules` pollution). Expose helpers to set `CURRENT_LEVEL` and to call
   `init_state` / `predict` (or `step`) uniformly, returning a normalized
   `(predicted_grid, {level_up,dead,win}, new_state)`. Detect interface shape
   (stateful `predict` vs stateless `step`; presence of `is_goal`) and surface it (the write_file
   install-string in contract §2 depends on this).
2. `backtest.py` — `run_backtest(model, timeline, *, selector="all") -> BacktestReport`:
   - Reconstruct per-step (before_grid, action, x, y, after_grid, level, level_up, dead, win) from the
     timeline. before_grid[i] = recorded grid[i-1] (or the initial grid for i=0).
   - Segment by level: at i=0 and after any `level_up` step, call `init_state(after_grid_of_boundary)`
     and set CURRENT_LEVEL to that segment's level; on a RESET step (action 0) re-`init_state` on the
     reset's after grid, same level.
   - For each checkable transition: call predict(state, before_grid, action, x, y); thread new_state.
     **Compare `info` flags (level_up/dead/win) on EVERY checkable step; compare the predicted grid vs
     after_grid ONLY on non-terminal steps** (steps where level_up/dead/win are all false).
   - **Skip** RESET steps (action 0) and any step with no prior grid.
   - Produce the exact contract §5 string:
     `backtest [all transitions]: X/Y transitions fully correct (grid on non-terminal steps + level_up/dead/win flags on EVERY step); M mismatch(es), K skipped (resets / no prior grid). <tail>`
     where tail is `Model predicts ALL checkable transitions in <range>` when M==0 else
     `Mismatched transitions (index:error-kind): #i:<grid|level_up|dead|win> ...`.
   - Support selectors `[all transitions]`, `[range #a..#b]`, `[indices [..]]`.
   - Guard each predict() call with a wall-clock timeout is NOT needed here (in-process); but wrap in
     try/except and report a model exception as a mismatch of kind `raised`.
3. `bfs.py` — `run_bfs(model, start_state, start_grid, *, actions, click_targets=(), max_nodes, max_depth, goal={"level_up","win"}) -> BfsReport`. BFS over predict() from the start state; the action
   set is the discrete `actions` plus one click action per `click_target` (x,y) plus an optional
   RESET-first branch. Hash states for dedup via the after-grid bytes (+ any cheap state signature).
   Stop at the first state whose transition info hits a goal flag. Produce the contract §5 string:
   `BFS: goal in N step(s) via level_up; expanded N nodes, N distinct states (actions=[..] + K click(s) + RESET-first option). Plan (-> commit_actions): [{'action':..}, {'action':6,'x':..,'y':..}, ...]`.

## Tests (`tests/`)
- `test_backtest_bp35.py` (THE GATE): load `vendor/bp35_world_model_v5.py`, run_backtest over the full
  released bp35 timeline, assert **0 mismatches** (all checkable transitions correct) and that the
  report string matches the contract format. This proves our model-exec + backtest contract matches theirs.
- `test_bfs_levelup.py`: on some bp35 level where the released model + a small click-target set reach a
  level_up, assert run_bfs returns a plan whose flags reach the goal, and re-verify that plan by
  stepping predict() forward.
- `test_model_loader.py`: interface detection (predict vs step, is_goal present/absent) + module isolation.

## Constraints
- Deterministic; stdlib + numpy only (no arc_agi needed here — backtest is pure model-vs-timeline).
- Do NOT read game source in environment_files/. bp35 is the only released trajectory you may open.
- Reuse `Grid`/`Transition` types from gateway/events; match repo style; keep functions small.
- If the released model does NOT go green immediately, the bug is almost surely in the harness
  reconstruction (level segmentation / CURRENT_LEVEL / before-grid threading / terminal-grid skip),
  NOT the model — debug the harness, never edit the released model.
