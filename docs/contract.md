# Schema harness — reverse-engineered contract (frozen spec)

Source: released run artifacts at HF `schema-harness/arc-agi-3-schema-traces`. All strings below are
**verbatim** from the `bp35` trajectory (our designated *contaminated dev game* — mining its protocol
strings is allowed; its game mechanics/solutions are quarantined from clean claims). These are the
golden fixtures our implementation is tested against. `N` = a number, `<..>` = a slot.

> Contamination rule: only game-**agnostic** protocol material goes here. Never transcribe another
> game's notes.md / world model / action plan. Pilot games (r11l, tu93, vc33) stay unopened.

---

## 0. Environment config (VERIFIED via `spikes/replay_parity.py`, GREEN on 566/566 grids)

- Local `arc-agi` env is byte-identical to the hosted env behind the traces.
- **Required:** run the game env with `ONLY_RESET_LEVELS=true`. Then `handle_reset()` always does
  `level_reset()` (restart current level, keep `levels_completed`) unless state==WIN. Without it, a
  RESET with `_action_count==0` (e.g. a second consecutive RESET after a death) triggers `full_reset()`
  → `levels_completed` back to 0, diverging from the official env. (`arcengine/base_game.py:305-323`.)
- RESET (`action 0`) is a scored action for RHAE (counts in `actions`), though it does not increment
  the engine's internal `_action_count`.

## 0b. Headless driver recipe (VERIFIED via `spikes/driver_probe.py`, GREEN)

Per-turn spawn of `claude -p`, one process per turn, CLI `2.1.214`:
```
env: CLAUDE_CONFIG_DIR=<workdir>/config   # isolated per game (no host hooks/skills leak)
     CLAUDE_CODE_OAUTH_TOKEN=<token>       # REQUIRED under isolated config dir — see below
     LOCUS_LOG / socket path for the gateway
claude -p "<turn message>" --model <m>
  --session-id <uuid>            # first turn;  --resume <uuid> thereafter
  --mcp-config <workdir>/mcp.json --strict-mcp-config    # only the locus server
  --permission-mode bypassPermissions                    # zero permission prompts
  --disallowed-tools "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Task,TodoWrite,NotebookEdit,MultiEdit,BashOutput"
  --output-format json           # result JSON carries session_id, usage, total_cost_usd, num_turns, is_error
cwd = <workdir>
```
- **Auth gotcha (load-bearing):** an isolated `CLAUDE_CONFIG_DIR` reads as *"not logged in · please run /login"* because the OAuth account marker lives in `~/.claude.json` (not inherited) while the token is in the macOS Keychain (`security find-generic-password -s "Claude Code-credentials" -w` → JSON → `claudeAiOauth.accessToken`). Fix = pass that token as `CLAUDE_CODE_OAUTH_TOKEN` (subscription billing, no API key needed). Copying `~/.claude.json` alone does NOT fix it.
- Built-ins are absent with `--disallowed-tools` (agent that tries Bash gets tool-unavailable). `--strict-mcp-config` blocks host MCP servers. MCP tools are callable in-turn; `commit_actions`'s "Stop now" reliably ends the turn.
- The `usage`/`total_cost_usd` in each result JSON is the per-turn telemetry the runner logs (cost caps, projections).

## 1. Per-run layout (`~/agent-<game>/`)

```
run.json            {"game_id","provider","model","max_actions":3000,"win_levels","workdir","started_at"}
events.jsonl        append-only event log (schema §4)
notes.md            agent scratchpad (template §3); injected at session start only
world_model_v<N>.py the live world model (auto-installed on write/edit, §2)
framework/          read-only: contract + backtest + run_bfs + history loader
config/claude/      per-game CLAUDE_CONFIG_DIR (isolated; built-ins disabled; auto-compact off)
runtime/artifact    scratch file backtest writes full output to (agent greps/heads it)
snapshots/cleared_level_<i>.py   world-model copy at each level clear
sessions/session-00<n>.jsonl     Claude Code transcript(s); sessions.json = {cwd, sid}
```

## 2. World-model contract

Filename `world_model_v<N>.py`. Writing or editing it **installs it as the live model**; the framework
introspects the interface. Two shapes accepted:

- **stateful**: `predict(state, grid, action, x=None, y=None) -> (predicted_grid, info, next_state)`
  plus `init_state(entry_grid) -> state` and (optionally) `ingest(state, grid)`.
- **stateless**: `step(...)`.

`info` carries the terminal flags (present-when-true): `level_up`, `dead`, `win`. `is_goal(state)`
presence gates BFS. Harness **injects module global `CURRENT_LEVEL`** (used undefined in the file);
`init_state` is called at each level boundary / after reset; latent state (world map beyond viewport,
camera offset, gravity) lives in `state`.

Install result strings (verbatim):
```
OK: wrote N bytes to world_model_vN.py. Installed as the live world model [stateful (predict)]; no is_goal (BFS disabled). Run run_backtest to check it against history.
OK: wrote N bytes to world_model_vN.py. Installed as the live world model [stateless (step)]; no is_goal (BFS disabled). Run run_backtest to check it against history.
OK: replaced N occurrence(s) in world_model_vN.py. Installed as the live world model [stateful (predict)]; no is_goal (BFS disabled). Run run_backtest to check it against history.
OK: wrote N bytes to notes.md.
OK: replaced N occurrence(s) in notes.md.
```

## 3. Turn protocol

**MCP tools** (`mcp__locus__*`, deferred; agent loads via ToolSearch):
`commit_actions, read_history, run_backtest, run_bfs, run_python, run_shell, write_file, edit_file,
read_file, grep, find, cp, mv, rm`.

**Session-start user message:**
```
State: NOT_FINISHED | level 0/9
Legal actions: [3, 4, 6, 7]  (action 6 is a click: also give x,y in 0..63)
World model: NONE yet; history: 0 transitions.
Files: workdir (read/write) = ~/agent-<game>; framework source (read-only) = ~/agent-<game>/framework.

Your notes (notes.md — maintain it with write_file/edit_file; keep it concise):
<notes.md contents — template below>

Current grid:
shape=64x64 (values 0-15 as hex)
<64 rows of 64 hex chars, one char per cell>

Decide the next action(s). Update your world model / notes, run a backtest or BFS as needed, then end by calling commit_actions.
```

**notes.md template (session start):**
```
# Notes — your living scratchpad (shown to you every turn).
# Keep it CONCISE; edit and PRUNE stale entries with write_file / edit_file as you learn.

## Action semantics (confirmed / guessed)
<!-- e.g. "confirmed: action 1 does X"; "guess: action 5 does Y" -->

## Current level
<!-- same vs previous levels; new motifs; goal hypothesis; current plan -->

## Hypotheses to test
<!-- short list of things to probe next -->

## Confirmed facts
<!-- durable, cross-level truths about this game -->
```

**Mid-session user message** (no Files line, no notes re-injection — cache-friendly):
```
Result of your last commit: committed N action(s) [<a,..>] — executed K; stopped because <reason>. Net: level a→b, state X→Y. Your stated intent was: "<reason text>"
State: NOT_FINISHED | level L/T
Legal actions: [..]  (action 6 is a click: also give x,y in 0..63)
World model: <path> ; history: N transitions.

Current grid:
shape=64x64 (values 0-15 as hex)
<grid>

Decide the next action(s) (update model/notes, backtest or BFS as needed), then end by calling commit_actions. If your memory of a rule/layout is fuzzy after a long session, re-read notes.md / world_model_v5.py / read_history before deciding.
```

Grid encoding: 64 lines × 64 hex chars (values 0–15 as `0-9a-f`), row 63 is the HUD row.

**Executor semantics** (queue runs step-by-step with per-step self-check vs the live model):
- No world model yet ⇒ **only 1 action per commit executes** ("exploring"). Narration reason:
  `no world model to self-check yet, so only this one step ran (exploring)`.
- First misprediction halts and drops the rest of the plan (surprise, §4 `model_mispredicted`).
- Halt reasons (enumerate): completed, surprise/mispredict, nondeterministic-model, dead,
  level_up, no-world-model-single-step, max_actions.
- `level_up := levels_completed increased OR state → WIN` (scorer-parity trap: the final WIN step must
  carry `level_up:true` or `score_trajectories.py` errors).

## 4. events.jsonl schema

Kinds (counts from bp35's 146-turn run): `run_started`(1), `turn_started`(146), `text_delta`(939),
`tool_started`/`tool_finished`(558 each), `model_mispredicted`(110), `turn_committed`(145),
`turn_fallback`(1), `action_taken`(566), `run_finished`(1). Common fields: `kind, seq, ts`.

- `run_started`: `game_id, provider, model, max_actions, win_levels, workdir, resumed, resumed_transitions`
- `turn_started`: `turn, env_step, state, level, win_levels, legal, grid, has_world_model, surprise`
- `text_delta`: `turn, text`
- `tool_started`: `turn, call_id, name, args` · `tool_finished`: `..., output, is_error`
- `model_mispredicted`: `turn, step_index, surprise, predicted(grid), actual(grid)`
- `turn_committed`: `turn, plan` (`[[action,x,y],...]`), `reason`
- `turn_fallback`: `turn, reason`
- `action_taken`: `turn, step_index, action, x, y, grid, level_up, dead, win, state, level`
- `run_finished`: terminal record

The stdlib scorer (`score_trajectories.py`, vendored) consumes only
`run_started` / `action_taken`(+`level_up`) / `run_finished`; errors on non-contiguous `step_index`
or a WIN run whose final level actions are unassigned.

## 5. Tool result strings (verbatim golden fixtures)

```
# commit_actions
Committed N action(s). Stop now — end your turn, do not call more tools.
Already committed this turn — end your turn now.

# surprise (in turn_started.surprise and the next narration)
world model MISPREDICTED the step just taken (action N @(x,y)); the rest of the committed plan was dropped. Run run_backtest to see the mismatch and fix the model before planning again.
world model MISPREDICTED the step just taken (action N); the rest of the committed plan was dropped. Run run_backtest to see the mismatch and fix the model before planning again.

# turn_fallback (ended turn without commit_actions)
ended without commit_actions — no action taken, game state unchanged (warned next turn)

# run_backtest  (selectors: [all transitions] | [range #a..#b] | [indices [..]])
backtest [all transitions]: X/Y transitions fully correct (grid on non-terminal steps + level_up/dead/win flags on EVERY step); M mismatch(es), K skipped (resets / no prior grid). Model predicts ALL checkable transitions in <range>
backtest [all transitions]: X/Y transitions fully correct (...); M mismatch(es), K skipped (...). Mismatched transitions (index:error-kind): #i:<kind> ...

# run_bfs
BFS: goal in N step(s) via level_up; expanded N nodes, N distinct states (actions=[3, 4, ...] + N click(s) + RESET-first option). Plan (-> commit_actions): [{'action': N}, {'action': 6, 'x': N, 'y': N}, ...]
ERROR: run_bfs timed out after Ns.

# read_history
N transitions total. Summary: level_ups=.. deaths=.. wins=.. resets(action0)=.. clicks(action6)=..; by-action={..}; max_level=..; showing indices [a, b] -> N steps; detail=full: #i action=A(x,y); C cells changed ...

# run_python / run_shell
$ <cmd> exit=C in T.Ts --- stdout --- <output>
ERROR: timed out after Ns — process killed. Partial output below.
```

## 6. locus MCP tool signatures (RESOLVED — from bp35 tool_started args)

```
commit_actions(actions: list[{"action": int, "x"?: int, "y"?: int}], reason: str, suggestion?: str)
run_backtest(start?: int=None, indices?: list[int]=None, max_details?: int=None)
    # no args = [all transitions]; start (negative = from end) = range; indices = specific
run_bfs(target: str, clicks: list[[int,int]]=[], max_depth: int, max_nodes?: int)
    # target e.g. "advance" (= reach level_up); clicks = the small enumerated (x,y) target set
    #   (this is how the 4096-way click space is bounded); max_nodes seen up to 300000
read_history(indices?: list[int]=None, detail?: str="full")
```
Framework `run_bfs` internal naming may differ; the locus tool MUST expose `target`/`clicks`/
`max_depth`/`max_nodes` per above.

## 7. Death / GAME_OVER handling (RESOLVED — from bp35 session, 7 deaths)

- The executor **auto-RESETs the level after a death** (agent does NOT commit the RESET itself). The
  commit narration reports the death; the *next* turn's `State:` line already shows the post-reset
  NOT_FINISHED at the same level. Verbatim:
  ```
  Result of your last commit: committed 4 action(s) [6@39,33 3 6@33,33 6@33,33] — executed 1; stopped because you DIED (game over) — RESET to retry the level. Net: level 1→1, state NOT_FINISHED→GAME_OVER. Your stated intent was: "..."
  State: NOT_FINISHED | level 1/9
  Legal actions: [3, 4, 6, 7]  (action 6 is a click: also give x,y in 0..63)
  World model: installed; history: 26 transitions.
  ```
- **Death halt-reason narration:** `you DIED (game over) — RESET to retry the level`.
- **Commit-narration `Net: state` is the DEATH state (GAME_OVER)** — reported pre-auto-reset — even
  though the next turn's `State:` shows the post-reset NOT_FINISHED.
- **Action-list format in the narration** (Step 1 got this wrong): **space-separated**, clicks as
  `6@x,y`, simple actions as bare ids — e.g. `[6@39,33 3 6@33,33 6@33,33]` (NOT comma-joined ids).
- **World-model line** once a model exists: `World model: installed; history: N transitions.`

## 8. Halt-reason → commit-narration mapping (RESOLVED — all from bp35)

The `stopped because <reason>` slot in the mid-session commit narration (contract §3):
| halt reason | narration `<reason>` (verbatim) |
|---|---|
| no world model yet | `no world model to self-check yet, so only this one step ran (exploring)` |
| misprediction | `the world model MISPREDICTED this step (see NOTE) — rest of the plan dropped` |
| death | `you DIED (game over) — RESET to retry the level` |
| level cleared | `you cleared a level (advanced {a}→{b})` |
| full queue ran | `ran the whole committed plan` |
| max_actions | (not seen in bp35 — infer/mine later) |

Note there are TWO distinct mispredict strings: this commit-narration reason, and the
`turn_started.surprise` field (`world model MISPREDICTED the step just taken (action N[ @(x,y)]); …`,
contract §5). Both exist in the protocol.

Remaining open: session-rollover bootstrap message (bp35 is single-session / 146 turns — infer from
the session-start template, or mine a multi-session game later); max_actions narration.
