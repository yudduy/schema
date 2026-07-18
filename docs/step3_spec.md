# Step 3 spec — locus MCP server + runner (DRAFT; finalize signatures after Step 2 review)

Wire the verified pieces into a working turn loop: a long-lived Gateway process per game, a `locus`
stdio MCP server exposing the 14 tools over the workdir, and a runner that drives headless
`claude -p` turn-by-turn. Acceptance: a short **live** smoke on bp35 (contaminated dev game) —
the agent loads tools, writes a model, backtests, commits, and the loop advances at least one turn
with well-formed events.jsonl the vendored scorer accepts.

Read first: `docs/contract.md` (§0b driver recipe, §2 world-model install, §3 turn protocol, §5 tool
strings, §6 tool signatures, §7 death handling), `schema_harness/{gateway,backtest,bfs,model_loader,
narration,events}.py` (reuse — do not reimplement).

## Architecture (SIMPLIFIED for Step 3 — in-process gateway; socket deferred)
```
runner.py (orchestrator)
  └─ spawns per turn:  claude -p ... (headless agent; built-ins disabled)
         └─ spawns (via --mcp-config):  locus.py (stdio MCP)  ── owns ──> Gateway (arc_agi env, in-proc)
```
The locus MCP server is ALREADY a separate process from the agent (claude spawns it), so having locus
own the Gateway in-process keeps game-source exec out of the *agent's* process — sufficient isolation
for the bp35 smoke. **Deferred to the pre-pilot sandbox-hardening task** (required before the Step-4
CLEAN pilot): (a) split the Gateway into its own long-lived process behind a unix socket so it survives
per-turn locus re-spawns and matches the empty `port` column; (b) sandbox `run_python`/`run_shell` so
`environment_files/` is unreachable. For the bp35 smoke these gaps are acceptable (contaminated game,
loop-under-test). **Persist gateway state to the workdir** (timeline + `world_model` file + turn_id
ledger) so a fresh locus per turn rebuilds the Gateway by replaying the persisted timeline.

## Files
1. `schema_harness/gateway_server.py` — wrap `Gateway` behind a unix-socket JSON-RPC loop in the
   workdir (`<workdir>/gateway.sock`). Requests: `state` (current grid/level/state/legal/history-len),
   `commit(actions, reason, suggestion)` → run `Gateway.execute_queue` with the live model (loaded via
   `model_loader` from `<workdir>/world_model_v*.py` if present) → return the ExecutionResult +
   next-turn narration inputs, `backtest(...)`, `bfs(...)`, `read_history(...)`. Enforce the turn state
   machine + idempotent turn_id + post-commit lock here (server-authoritative).
   - **FIX from Step 1 review**: run env in NORMAL mode OR pre-download the game before OFFLINE (so
     not-yet-downloaded pilot games work). Set `ONLY_RESET_LEVELS=true` (already in Gateway).
2. `schema_harness/locus.py` — FastMCP stdio server exposing the 14 tools (names/strings from
   contract §5/§6). File tools (`write_file/edit_file/read_file/grep/find/cp/mv/rm`) operate on the
   workdir (jailed; reject paths escaping it) and, on `write_file`/`edit_file` of `world_model_v*.py`,
   return the exact install string (contract §2) after a syntax check + interface probe via
   `model_loader`. `commit_actions/run_backtest/run_bfs/read_history` proxy to the gateway socket.
   `run_python/run_shell` execute in the workdir (SANDBOX DEFERRED to Step 2c/later — for the bp35
   smoke, run in a plain subprocess with a timeout + workdir cwd; mark clearly as not-yet-sandboxed).
   Post-commit: every tool after `commit_actions` returns `"Already committed this turn — end your turn now."`
3. `schema_harness/runner.py` — the orchestrator:
   - Workdir init: `~/agent-<game>` (or a runs/ dir), `notes.md` template (contract §3), copy
     `framework/` read-only, isolated `config/claude` CONFIG_DIR, write `mcp.json` (locus stdio +
     `LOCUS_SOCKET`/`LOCUS_LOG` env), `run.json`.
   - Auth: `CLAUDE_CODE_OAUTH_TOKEN` from Keychain (contract §0b) injected into the claude env.
   - Turn loop: build the turn message (session-start vs mid-session per contract §3; the mid-session
     commit narration from `narration.py`), spawn `claude -p --model <m> --session-id/--resume <sid>
     --mcp-config mcp.json --strict-mcp-config --permission-mode bypassPermissions --disallowed-tools
     <all builtins> --output-format json` (cwd=workdir), parse session_id/usage/total_cost_usd →
     append per-turn telemetry to events.jsonl; loop until WIN / GAME_OVER-unrecoverable / max_actions
     / turn budget. Proactive session rollover near context limit.
   - **Cost guardrails**: per-turn, per-run dollar caps + no-progress circuit breaker (N turns with no
     level gain or new transition ⇒ stop). Log projected cost each turn.

## Narration fidelity FIXES (from contract §7/§8 — apply in narration.py, add golden-string tests)
- Action list = space-separated, clicks as `6@x,y`, simple as bare id: `[6@39,33 3 6@33,33]`
  (narration.py currently comma-joins bare ids — fix).
- Halt-reason `stopped because <reason>` mapping (verbatim):
  - no world model → `no world model to self-check yet, so only this one step ran (exploring)`
  - misprediction → `the world model MISPREDICTED this step (see NOTE) — rest of the plan dropped`
  - death → `you DIED (game over) — RESET to retry the level`
  - level cleared → `you cleared a level (advanced {a}→{b})`
  - full queue ran → `ran the whole committed plan`
- Death: `Net: state → GAME_OVER` (pre-reset); the executor auto-RESETs so the NEXT turn's `State:`
  shows post-reset NOT_FINISHED at the same level (Gateway already auto-RESETs — verified faithful).
- World-model line once installed: `World model: installed; history: N transitions.`
- `run_bfs` wrapper needs a wall-clock timeout → `ERROR: run_bfs timed out after Ns.` (contract §5).
- locus tool signatures are FIXED in contract §6 — expose exactly `target`/`clicks`/`max_depth`/`max_nodes`
  for run_bfs, `start`/`indices`/`max_details` for run_backtest, `indices`/`detail` for read_history.

## Split: Codex builds + unit-tests; Claude runs the live smoke
**Codex (this task) — NO live `claude` (sandbox has no network/auth):**
- Build `locus.py`, `runner.py`, gateway persistence, narration fixes.
- Unit tests Codex runs to green (`uv run pytest tests/ -q`):
  - `test_locus_jail.py`: file tools reject paths outside the workdir; `write_file world_model_v1.py`
    returns the correct install string + interface tag; post-commit lock string.
  - `test_turn_state_machine.py`: idempotent turn_id (duplicate commit → prior result); crash between
    COMMIT_DURABLE and EXECUTING recovers by grid-hash (reuse Step 1 machinery).
  - `test_narration_golden.py`: every §7/§8 string renders verbatim (incl. `6@x,y` action list).
  - `test_runner_message.py`: session-start vs mid-session message assembly matches contract §3 (feed a
    canned gateway result; assert the built prompt string).
- Provide a `runner.py --dry-run` path that drives ONE turn with a STUB agent (scripted tool calls, no
  claude) end-to-end through locus+gateway on bp35, emitting events.jsonl the vendored scorer parses.

**Claude (separate, after review) — live smoke:** run `runner.py` for a few real `claude -p` turns on
bp35 with a cheap model (haiku) using the §0b auth recipe; verify the agent loads tools, writes+backtests
a model, commits, and events.jsonl is scorer-valid. This is the real Step-3 gate for Step 4.

## Constraints
- Reuse Step 1/2 modules; do not duplicate scoring (vendored scorer only).
- Sandbox (sandbox-exec) is a SEPARATE task after the smoke works — but the anti-cheat that IS in
  scope now: built-ins disabled (`--disallowed-tools`), `--strict-mcp-config`, workdir-jailed file
  tools, game env in a separate process. Note the run_python/run_shell sandbox gap explicitly.
- Deterministic where possible; live turns are the only nondeterminism.
