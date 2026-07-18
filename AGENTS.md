# Repository Guidelines

## Multi-agent coordination — READ FIRST

Two agents run in this **same working tree at once**, each with a different goal.
Identify yourself by your active goal file, then stay inside that lane:

- **METHOD agent** (`GOAL1.md`) — owns the agent's *reasoning*. Edit only
  `schema_harness/prompts/**`, the system-prompt wiring in `runner.py`,
  `docs/RESULTS-method.md`, and method-focused tests.
- **TOOLING agent** (`GOAL2.md`) — owns the harness *machinery*. Edit only
  `schema_harness/{backtest,bfs,model_loader,locus,gateway,inspectors,guard}.py`,
  `spikes/driver_probe.py`, the runtime/session/budget wiring in `runner.py`,
  `docs/RESULTS-tooling.md`, and tooling tests.

Rules that keep the shared tree from corrupting (all mandatory):

1. **Stay in your lane.** Never edit the other agent's files or its RESULTS ledger.
   `docs/contract.md` is frozen — treat it as read-only.
2. **Never `git add -A` / `git add .` / `git commit -a`.** The other agent has
   uncommitted work in this tree; stage only your own files by explicit path.
3. **`runner.py` is shared** — edit only your own section, keep it small, and commit
   it immediately so the other agent rebuilds on your version.
4. **Commit small and often**, green tests first (`uv run pytest -q`), tagging your
   direction in the Conventional-Commit scope: `feat(method): …` / `fix(tooling): …`.
5. **One live subscription run at a time.** Another Opus pilot may already be running;
   never launch an overlapping live game run — record your live runs in your ledger
   and check the other ledger before starting one.

## Project Structure & Module Organization

`schema_harness/` contains harness code: event logging, runner orchestration, model loading, gateway isolation, replay verification, backtesting, and BFS support. Root entry points are `play.py`, a short interactive smoke test, and `agent.py`, a minimal random agent. Tests live in `tests/` and mirror harness modules with `test_<behavior>.py` names. Protocol and contract details belong in `docs/`; exploratory validation scripts belong in `spikes/`. Treat `vendor/` traces, models, and scoring utilities as reference fixtures. `environment_files/` and `recordings/` are generated and ignored by Git.

## Build, Test, and Development Commands

- `uv sync` installs the Python 3.12 project and development dependencies from `uv.lock`.
- `uv run pytest` runs the complete test suite.
- `uv run pytest tests/test_events.py -q` runs one focused test module.
- `uv run play.py` renders the LS20 quickstart in the terminal.
- `uv run agent.py --game vc33 --render` runs the sample agent on a selected game.

There is no separate build step. Run Python commands through `uv run` so they use the locked environment.

## Coding Style & Naming Conventions

Use four-space indentation, type annotations, and standard Python naming: `snake_case` for modules, functions, and variables; `PascalCase` for classes; and uppercase names for constants. Keep imports grouped as standard library, third-party, then local. Prefer small typed functions, frozen/slot dataclasses for immutable records, and `pathlib.Path` for paths, following existing code. No formatter or linter is configured, so match nearby style and keep lines readable.

## Testing Guidelines

Tests use pytest. Name files and functions `test_*.py` and `test_<expected_behavior>`. Add focused regression coverage for contract, replay, state-machine, or serialization changes; use deterministic fixtures and pytest helpers such as `tmp_path` and `monkeypatch`. No coverage threshold is configured, but the full 30-test suite must pass before review. Preserve the contamination boundaries documented in `docs/contract.md`.

## Commit & Pull Request Guidelines

The repository has no commit history yet. Start with concise Conventional Commit messages such as `fix: preserve replay sequence numbers`, and keep each commit scoped to one concern. Pull requests should explain the motivation and behavioral impact, link relevant issues, list verification commands, and call out contract or fixture changes. Include terminal screenshots only when rendered output changes.

## Security & Configuration

Copy `.env.example` to `.env` when an ARC API key is needed; anonymous access is supported. Never commit `.env`, API keys, downloaded environments, recordings, or per-run credentials. Use `ONLY_RESET_LEVELS=true` for parity-sensitive harness runs as specified in `docs/contract.md`.
