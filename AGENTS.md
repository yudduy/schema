# Repository Guidelines

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
