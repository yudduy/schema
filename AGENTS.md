# Agent guidance

## Architectural constraints

- `docs/contract.md` is frozen and read-only. Preserve its contamination and
  game-agnostic boundaries: never inspect game source under `environment_files/`,
  derive solutions from fixtures, or expose repository internals to the playing agent.
- Treat `vendor/` as immutable reference evidence. Keep live workdirs outside the
  repository, and never commit generated environments, recordings, credentials, or
  sweep artifacts.
- Only one live run may exist per machine. The runner owns the global
  `$TMPDIR/schema-harness-live-<uid>.lock`; never bypass the lock or terminate a run
  merely to acquire it.
- Live games require macOS because the agent jail uses `sandbox-exec`.
  `ONLY_RESET_LEVELS=true` is mandatory, and live Codex runs require exactly
  `codex-cli 0.144.1`.
- A workdir is a durable trajectory identity: `run.json` pins its game, provider,
  model, effort, prompt, driver policy, CLI, and model catalog. Resume it with matching
  settings instead of repurposing it.
- Deep BFS has a 600-second default budget. Use `--turn-timeout 3600` so BFS and the
  surrounding model turn fit within the driver timeout.

## Canonical implementation paths

- Run lifecycle, short-to-versioned game-ID resolution, resume metadata, provider
  policy, and the default prompt: `schema_harness/runner.py`.
- Ground-truth engine state and transitions: `schema_harness/gateway.py`. Agent tool
  authorization and commit gates: `schema_harness/locus.py` and
  `schema_harness/guard.py`.
- Event records and durable JSONL writes: `schema_harness/events.py`.
- World-model loading/execution, parity checks, planning, and search:
  `schema_harness/model_loader.py`, `schema_harness/model_worker.py`,
  `schema_harness/backtest.py`, and `schema_harness/bfs.py`.
- Contributor/release replay gate: `spikes/replay_parity.py`. Sweep orchestration,
  intake, and verified export: `spikes/sweep.py`, `spikes/intake.py`, and
  `spikes/export_traces.py`.
- RHAE semantics and human baselines: `vendor/score_trajectories.py` and
  `vendor/baseline_actions.csv`. Wrappers may adapt inputs and output, but must not
  reimplement the score.

## Validation and commits

Run the complete suite before handoff:

```bash
uv run pytest -q
```

For replay or scoring changes, also run the focused acceptance checks:

```bash
uv run pytest -q tests/test_replay_bp35.py tests/test_replay_verify_gate.py
ONLY_RESET_LEVELS=true uv run python spikes/replay_parity.py \
  vendor/bp35_events.jsonl --game bp35-0a0ad940
```

Commit with `committer "<conventional message>" <exact paths...>`. Never stage the
whole tree, amend, push without instruction, or absorb unknown working-tree changes.

## Common failure modes

- Linux live play fails closed; verification and scoring remain portable.
- A missing `ONLY_RESET_LEVELS=true`, a Codex version mismatch, a changed catalog, or
  mismatched resume settings is a hard refusal, not a reason to weaken validation.
- Live-lock contention means another run owns the machine. Wait for it; do not work
  around the lock.
- A deep BFS under the ordinary short turn timeout can be killed before model work
  resumes.
- Under machine load,
  `tests/test_locus_jail.py::test_commit_predictions_interleave_with_real_steps_and_timeout_durably`
  can flake. Re-run that exact test in isolation to confirm; do not change its behavior
  as part of unrelated work.
