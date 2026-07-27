"""Turn-by-turn Schema harness runner for headless subscription agents."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "schema_harness"

from spikes.driver_probe import oauth_token, run_turn as run_claude_turn

from .events import (
    EventLog,
    RunFinished,
    RunStarted,
    TextDelta,
    TurnFallback,
    TurnStarted,
    TurnTelemetry,
    iter_json_objects,
)
from .game_identity import canonical_game_id as _canonical_game_id
from .game_identity import short_game_id
from .gateway import ExecutionResult, GatewaySnapshot, PersistentGateway
from .locus import LocusService
from .narration import commit_result_narration, world_model_line


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GAME = "bp35-0a0ad940"
DEFAULT_PROVIDER = "codex"
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MODEL = DEFAULT_CODEX_MODEL
DEFAULT_CODEX_EFFORT = "max"
DEFAULT_CODEX_COMPACT_TOKENS = 240_000
VALIDATED_CODEX_CLI_VERSION = "codex-cli 0.144.1"
_CODEX_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
DEFAULT_SYSTEM_PROMPT = (
    REPO_ROOT / "schema_harness" / "prompts" / "physicist_v9_matched_transfer.md"
)
NOTES_TEMPLATE = """# Notes — your living scratchpad (shown to you every turn).
# Keep it CONCISE; edit and PRUNE stale entries with write_file / edit_file as you learn.

## Action semantics (confirmed / guessed)
<!-- e.g. "confirmed: action 1 does X"; "guess: action 5 does Y" -->

## Current level
<!-- same vs previous levels; new motifs; goal hypothesis; current plan -->

## Hypotheses to test
<!-- short list of things to probe next -->

## Confirmed facts
<!-- durable, cross-level truths about this game -->
"""
FALLBACK_REASON = "ended without commit_actions — no action taken, game state unchanged (warned next turn)"
_MODEL_PATTERN = re.compile(r"world_model_v(\d+)\.py")
_LIVE_RUN_LOCK = Path(tempfile.gettempdir()) / f"schema-harness-live-{os.getuid()}.lock"
CODEX_LOCUS_TOOLS = (
    "commit_actions",
    "run_backtest",
    "run_bfs",
    "read_history",
    "run_python",
    "run_shell",
    "write_file",
    "edit_file",
    "read_file",
    "grep",
    "find",
    "cp",
    "mv",
    "rm",
)
CLAUDE_LOCUS_TOOLS = tuple(f"mcp__locus__{name}" for name in CODEX_LOCUS_TOOLS)
_CODEX_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "shell_snapshot",
    "code_mode",
    "code_mode_host",
    "code_mode_only",
    "apps",
    "plugins",
    "multi_agent",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "image_generation",
    "goals",
    "memories",
    "tool_suggest",
    "hooks",
    "in_app_browser",
    "remote_plugin",
    "plugin_sharing",
    "skill_mcp_dependency_install",
    "workspace_dependencies",
)
_CODEX_ALLOWED_ITEM_TYPES = frozenset(
    {"agent_message", "reasoning", "mcp_tool_call", "context_compaction"}
)
_CODEX_ALLOWED_RECORD_TYPES = frozenset(
    {
        "thread.started",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "item.started",
        "item.updated",
        "item.completed",
        "error",
    }
)
_CODEX_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
CODEX_DRIVER_POLICY = (
    "Schema driver boundary: use only tools from the locus MCP server. Never call native "
    "Codex shell, patch, image, browser, app, planning, question, goal, or collaboration "
    "tools. End the turn after locus.commit_actions, or explain why no safe commit is ready."
)


def _codex_policy_config_digest(
    *, model: str, effort: str, experimental_tooling: bool
) -> str:
    """Identify the static, security-relevant portion of the Codex boundary."""

    payload = {
        "schema": 1,
        "model": model,
        "effort": effort,
        "approval_policy": "never",
        "sandbox_mode": "read-only",
        "web_search": "disabled",
        "auto_compact_token_limit": DEFAULT_CODEX_COMPACT_TOKENS,
        "disabled_features": list(_CODEX_DISABLED_FEATURES),
        "enabled_locus_tools": list(CODEX_LOCUS_TOOLS),
        "experimental_tooling": experimental_tooling,
        "driver_policy": CODEX_DRIVER_POLICY,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@contextmanager
def _live_run_lock(path: Path = _LIVE_RUN_LOCK) -> Iterator[None]:
    """Prevent overlapping subscription game runs across worktrees."""

    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    handle = os.fdopen(descriptor, "r+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError(
            f"another live Schema harness run is active (lock: {path})"
        ) from None
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


@dataclass(frozen=True, slots=True)
class CommittedTurn:
    plan: list[list[int | None]]
    reason: str
    result: ExecutionResult


def encode_grid(grid: list[list[int]]) -> str:
    """Encode an ARC grid as one lower-case hexadecimal character per cell."""

    if not grid or not grid[0]:
        raise ValueError("grid must be non-empty")
    width = len(grid[0])
    rows: list[str] = []
    alphabet = "0123456789abcdef"
    for row in grid:
        if len(row) != width:
            raise ValueError("grid rows must have equal width")
        if any(type(value) is not int or not 0 <= value <= 15 for value in row):
            raise ValueError("grid values must be integers in 0..15")
        rows.append("".join(alphabet[value] for value in row))
    return "\n".join(rows)


def _grid_block(grid: list[list[int]]) -> str:
    return (
        f"shape={len(grid)}x{len(grid[0])} (values 0-15 as hex)\n"
        f"{encode_grid(grid)}"
    )


def _legal_line(legal: list[int]) -> str:
    suffix = (
        "  (action 6 is a click: also give x,y in 0..63)"
        if 6 in legal
        else ""
    )
    return f"Legal actions: {legal}{suffix}"


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(Path.home())
    except ValueError:
        return str(resolved)
    return f"~/{relative.as_posix()}"


def build_session_start_message(
    snapshot: GatewaySnapshot,
    workdir: str | os.PathLike[str],
    notes: str,
    *,
    has_world_model: bool = False,
) -> str:
    """Render the frozen §3 first-turn user message."""

    root = Path(workdir)
    return (
        f"State: {snapshot.state} | level {snapshot.level}/{snapshot.win_levels}\n"
        f"{_legal_line(snapshot.legal)}\n"
        f"{world_model_line(has_world_model, snapshot.history_len)}\n"
        f"Files: workdir (read/write) = {_display_path(root)}; framework source (read-only) = "
        f"{_display_path(root / 'framework')}.\n\n"
        "Your notes (notes.md — maintain it with write_file/edit_file; keep it concise):\n"
        f"{notes.rstrip()}\n\n"
        "Current grid:\n"
        f"{_grid_block(snapshot.grid)}\n\n"
        "Decide the next action(s). Update your world model / notes, run a backtest or BFS as "
        "needed, then end by calling commit_actions."
    )


def build_mid_session_message(
    snapshot: GatewaySnapshot,
    previous: CommittedTurn,
    *,
    has_world_model: bool,
    model_filename: str = "world_model_v5.py",
) -> str:
    """Render the frozen §3 continuation message after a committed turn."""

    return (
        f"{commit_result_narration(previous.plan, previous.result, previous.reason)}\n"
        f"State: {snapshot.state} | level {snapshot.level}/{snapshot.win_levels}\n"
        f"{_legal_line(snapshot.legal)}\n"
        f"{world_model_line(has_world_model, snapshot.history_len)}\n\n"
        "Current grid:\n"
        f"{_grid_block(snapshot.grid)}\n\n"
        "Decide the next action(s) (update model/notes, backtest or BFS as needed), then end by "
        "calling commit_actions. If your memory of a rule/layout is fuzzy after a long session, "
        f"re-read notes.md / {model_filename} / read_history before deciding."
    )


def build_turn_message(
    snapshot: GatewaySnapshot,
    workdir: str | os.PathLike[str],
    *,
    notes: str,
    previous: CommittedTurn | None,
    has_world_model: bool,
    model_filename: str = "world_model_v5.py",
    session_start: bool = False,
) -> str:
    if previous is None or session_start:
        message = build_session_start_message(
            snapshot,
            workdir,
            notes,
            has_world_model=has_world_model,
        )
        if session_start and previous is not None:
            return (
                f"{commit_result_narration(previous.plan, previous.result, previous.reason)}\n"
                f"{message}"
            )
        return message
    return build_mid_session_message(
        snapshot,
        previous,
        has_world_model=has_world_model,
        model_filename=model_filename,
    )


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        json.dump(payload, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_text(path: Path, value: str) -> None:
    parent = path.parent
    if os.path.lexists(parent) and (parent.is_symlink() or not parent.is_dir()):
        raise RuntimeError(f"private output directory is not a real directory: {parent}")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()


def _copy_framework(workdir: Path) -> None:
    framework = workdir / "framework"
    framework.mkdir(exist_ok=True)
    sources = {
        "contract.md": REPO_ROOT / "docs" / "contract.md",
        "backtest.py": REPO_ROOT / "schema_harness" / "backtest.py",
        "bfs.py": REPO_ROOT / "schema_harness" / "bfs.py",
        "model_loader.py": REPO_ROOT / "schema_harness" / "model_loader.py",
        "events.py": REPO_ROOT / "schema_harness" / "events.py",
    }
    for name, source in sources.items():
        destination = framework / name
        if not destination.exists():
            shutil.copy2(source, destination)
        destination.chmod(0o444)
    framework.chmod(0o555)


def initialize_workdir(
    workdir: str | os.PathLike[str],
    *,
    game: str,
    provider: str,
    model: str,
    max_actions: int,
    effort: str | None = None,
    system_prompt_file: str | os.PathLike[str] | None = None,
    experimental_tooling: bool = False,
) -> tuple[Path, GatewaySnapshot]:
    """Create durable run files without replacing agent-authored notes or models."""

    root = Path(workdir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    prompt_bytes: bytes | None = None
    prompt_digest: str | None = None
    if system_prompt_file is not None:
        prompt_bytes = Path(system_prompt_file).resolve().read_bytes()
        prompt_digest = hashlib.sha256(prompt_bytes).hexdigest()

    run_path = root / "run.json"
    if run_path.exists():
        existing = json.loads(run_path.read_text(encoding="utf-8"))
        if existing.get("game_id") != game:
            raise ValueError(
                f"workdir run.json belongs to {existing.get('game_id')!r}, not {game!r}"
            )
        if existing.get("provider") != provider:
            raise ValueError(
                f"workdir provider is {existing.get('provider')!r}, not {provider!r}"
            )
        if existing.get("model") != model:
            raise ValueError(
                f"workdir model is {existing.get('model')!r}, not {model!r}"
            )
        if existing.get("max_actions") != max_actions:
            raise ValueError(
                f"workdir max_actions is {existing.get('max_actions')!r}, not {max_actions!r}"
            )
        if "effort" in existing and existing.get("effort") != effort:
            raise ValueError(
                f"workdir effort is {existing.get('effort')!r}, not {effort!r}"
            )
        if existing.get("system_prompt_sha256") != prompt_digest:
            raise ValueError("workdir was created with a different system prompt")
        if existing.get("experimental_tooling") is not experimental_tooling:
            raise ValueError(
                "workdir was created with a different experimental-tooling mode"
            )

    notes = root / "notes.md"
    if not notes.exists():
        notes.write_text(NOTES_TEMPLATE, encoding="utf-8", newline="")
    _copy_framework(root)

    if provider == "claude":
        config = root / "config" / "claude"
        config.mkdir(parents=True, exist_ok=True)
        settings = config / "settings.json"
        if not settings.exists():
            _atomic_json(settings, {"autoCompactEnabled": False})
        account_marker = Path.home() / ".claude.json"
        isolated_marker = config / ".claude.json"
        if account_marker.exists() and not isolated_marker.exists():
            shutil.copy2(account_marker, isolated_marker)
    elif provider == "codex":
        (root / "config" / "codex" / "driver").mkdir(parents=True, exist_ok=True)
    elif provider != "stub":
        raise ValueError(f"unsupported live provider: {provider!r}")

    gateway = PersistentGateway(game, root, max_actions=max_actions)
    snapshot = gateway.snapshot
    if prompt_bytes is not None:
        prompt_copy = root / "method_prompt.md"
        if prompt_copy.exists() and prompt_copy.read_bytes() != prompt_bytes:
            raise ValueError("workdir method_prompt.md differs from the requested system prompt")
        if not prompt_copy.exists():
            prompt_copy.write_bytes(prompt_bytes)
        prompt_copy.chmod(0o444)
    metadata = {
        "game_id": game,
        "provider": provider,
        "model": model,
        "effort": effort,
        "max_actions": max_actions,
        "win_levels": snapshot.win_levels,
        "workdir": str(root),
        "started_at": time.time(),
        "system_prompt": "method_prompt.md" if prompt_digest else None,
        "system_prompt_sha256": prompt_digest,
        "experimental_tooling": experimental_tooling,
    }
    if not run_path.exists():
        _atomic_json(run_path, metadata)
    initial_turn = next_turn_number(root)
    write_mcp_config(
        root,
        game=game,
        turn=initial_turn,
        turn_id=f"turn-{initial_turn:06d}",
        max_actions=max_actions,
        experimental_tooling=experimental_tooling,
    )
    return root, snapshot


def load_snapshot(workdir: str | os.PathLike[str]) -> GatewaySnapshot:
    path = Path(workdir) / "runtime" / PersistentGateway.STATE_NAME
    return GatewaySnapshot.from_mapping(json.loads(path.read_text(encoding="utf-8")))


def live_model_path(workdir: str | os.PathLike[str]) -> Path | None:
    root = Path(workdir).resolve()
    pointer = root / "runtime" / PersistentGateway.MODEL_NAME
    if pointer.exists():
        try:
            candidate = root / json.loads(pointer.read_text(encoding="utf-8"))["path"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            candidate = Path()
        if candidate.parent == root and candidate.is_file():
            return candidate
    candidates: list[tuple[int, Path]] = []
    for candidate in root.glob("world_model_v*.py"):
        match = _MODEL_PATTERN.fullmatch(candidate.name)
        if match and candidate.is_file():
            candidates.append((int(match.group(1)), candidate))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _execution_from_mapping(raw: dict[str, Any]) -> ExecutionResult:
    return ExecutionResult(
        committed=int(raw["committed"]),
        executed=int(raw["executed"]),
        halt_reason=raw["halt_reason"],
        start_level=int(raw["start_level"]),
        end_level=int(raw["end_level"]),
        start_state=str(raw["start_state"]),
        end_state=str(raw["end_state"]),
        surprise=str(raw.get("surprise") or ""),
    )


def load_committed_turn(
    workdir: str | os.PathLike[str],
    turn_id: str,
) -> CommittedTurn | None:
    ledger_path = Path(workdir) / "runtime" / PersistentGateway.LEDGER_NAME
    if not ledger_path.exists():
        return None
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    record = ledger.get("turns", {}).get(turn_id)
    if not isinstance(record, dict) or record.get("phase") != "COMPLETE":
        return None
    result = record.get("result")
    if not isinstance(result, dict):
        return None
    return CommittedTurn(
        plan=[list(action) for action in record["actions"]],
        reason=str(record["reason"]),
        result=_execution_from_mapping(result),
    )


def load_previous_committed_turn(
    workdir: str | os.PathLike[str], next_turn: int
) -> CommittedTurn | None:
    """Recover the last completed commit when a bounded runner process restarts."""

    if next_turn <= 1:
        return None
    return load_committed_turn(workdir, f"turn-{next_turn - 1:06d}")


def load_driver_session(
    workdir: str | os.PathLike[str],
    *,
    provider: str | None = None,
    model: str | None = None,
) -> tuple[str, bool] | None:
    """Load a provider-bound durable driver session checkpoint."""

    root = Path(workdir).resolve()
    path = root / "sessions" / "sessions.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("cwd") != str(root):
        return None
    recorded_provider = payload.get("provider")
    recorded_model = payload.get("model")
    if provider is not None:
        if recorded_provider is None and provider != "claude":
            return None
        if recorded_provider is not None and recorded_provider != provider:
            return None
    if model is not None:
        if recorded_model is None and provider not in (None, "claude"):
            return None
        if recorded_model is not None and recorded_model != model:
            return None
    session_id = payload.get("sid")
    resume = payload.get("resume")
    if not isinstance(session_id, str) or not session_id or not isinstance(resume, bool):
        return None
    return session_id, resume


def next_turn_number(workdir: str | os.PathLike[str]) -> int:
    """Return a turn number not already present in the durable event/ledger state."""

    root = Path(workdir)
    seen: list[int] = []
    events_path = root / "events.jsonl"
    if events_path.exists():
        for _, event in iter_json_objects(events_path):
            turn = event.get("turn")
            if type(turn) is int:
                seen.append(turn)
    ledger_path = root / "runtime" / PersistentGateway.LEDGER_NAME
    if ledger_path.exists():
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        for record in ledger.get("turns", {}).values():
            turn = record.get("turn") if isinstance(record, dict) else None
            if type(turn) is int:
                seen.append(turn)
    return max(seen, default=0) + 1


def write_mcp_config(
    workdir: Path,
    *,
    game: str,
    turn: int,
    turn_id: str,
    max_actions: int,
    experimental_tooling: bool = False,
) -> Path:
    """Write the strict one-server MCP config inherited by the headless driver."""

    environment = _locus_environment(
        workdir,
        game=game,
        turn=turn,
        turn_id=turn_id,
        max_actions=max_actions,
        experimental_tooling=experimental_tooling,
    )
    config = {
        "mcpServers": {
            "locus": {
                "command": sys.executable,
                "args": ["-m", "schema_harness.locus"],
                "env": environment,
                "alwaysLoad": True,
            }
        }
    }
    path = workdir / "mcp.json"
    _atomic_json(path, config)
    return path


def _locus_environment(
    workdir: Path,
    *,
    game: str,
    turn: int,
    turn_id: str,
    max_actions: int,
    experimental_tooling: bool = False,
) -> dict[str, str]:
    """Return the exact environment required by the isolated Locus server."""

    inherited_pythonpath = os.environ.get("PYTHONPATH")
    pythonpath = str(REPO_ROOT)
    if inherited_pythonpath:
        pythonpath = os.pathsep.join((pythonpath, inherited_pythonpath))
    environment = {
        "PYTHONPATH": pythonpath,
        "LOCUS_WORKDIR": str(workdir),
        "LOCUS_GAME": game,
        "LOCUS_TURN": str(turn),
        "LOCUS_TURN_ID": turn_id,
        "LOCUS_MAX_ACTIONS": str(max_actions),
        "LOCUS_EVENTS": str(workdir / "events.jsonl"),
        "LOCUS_LOG": str(workdir / "runtime" / "locus.jsonl"),
        "SCHEMA_EXPERIMENTAL_TOOLING": (
            "true" if experimental_tooling else "false"
        ),
        # Claude needs the OAuth token; the spawned MCP/tool subprocesses do not.
        "CLAUDE_CODE_OAUTH_TOKEN": "",
    }
    for key in (
        "SCHEMA_ENVIRONMENTS_DIR",
        "ONLY_RESET_LEVELS",
        "LOCUS_BFS_TIMEOUT",
        "LOCUS_BACKTEST_TIMEOUT",
        "LOCUS_PROCESS_TIMEOUT",
    ):
        if key in os.environ:
            environment[key] = os.environ[key]
    return environment


def _run_started(
    workdir: Path,
    snapshot: GatewaySnapshot,
    *,
    provider: str,
    model: str,
    max_actions: int,
) -> int:
    resumed = snapshot.history_len > 0 or _has_prior_run_events(workdir)
    with EventLog(workdir / "events.jsonl", clock=time.time) as event_log:
        event_log.append(
            RunStarted(
                game_id=snapshot.game_id,
                provider=provider,
                model=model,
                max_actions=max_actions,
                win_levels=snapshot.win_levels,
                workdir=str(workdir),
                resumed=resumed,
                resumed_transitions=snapshot.history_len,
            )
        )
    return snapshot.history_len


def _has_prior_run_events(workdir: Path) -> bool:
    events = workdir / "events.jsonl"
    if not events.is_file():
        return False
    has_prior = False
    for _, record in iter_json_objects(events):
        if record.get("kind") in {
            "run_started",
            "turn_started",
            "turn_telemetry",
            "run_finished",
        }:
            has_prior = True
    return has_prior


def _turn_started(
    workdir: Path,
    snapshot: GatewaySnapshot,
    *,
    turn: int,
    surprise: str,
) -> None:
    with EventLog(workdir / "events.jsonl", clock=time.time) as event_log:
        event_log.append(
            TurnStarted(
                turn=turn,
                env_step=snapshot.history_len,
                state=snapshot.state,
                level=snapshot.level,
                win_levels=snapshot.win_levels,
                legal=snapshot.legal,
                grid=snapshot.grid,
                has_world_model=live_model_path(workdir) is not None,
                surprise=surprise,
            )
        )


def _finish_run(
    workdir: Path,
    snapshot: GatewaySnapshot,
    *,
    start_history_len: int,
) -> None:
    with EventLog(workdir / "events.jsonl", clock=time.time) as event_log:
        event_log.append(
            RunFinished(
                state=snapshot.state,
                levels=snapshot.level,
                win_levels=snapshot.win_levels,
                actions=snapshot.history_len - start_history_len,
                transitions=snapshot.history_len,
                has_world_model=live_model_path(workdir) is not None,
            )
        )


class ScriptedAgent:
    """Deterministic one-turn dry-run agent; never launches the Claude CLI."""

    def run(self, _message: str, service: LocusService) -> str:
        service.read_file("notes.md", start_line=1, end_line=4)
        legal = service.gateway.snapshot.legal
        action = next((value for value in legal if value != 6), legal[0])
        service.commit_actions(
            [{"action": action}],
            "dry-run scripted probe",
            "One deterministic action validates the Step 3 turn plumbing.",
        )
        return "Stub agent committed one deterministic legal action."


def _validate_with_vendored_scorer(workdir: Path) -> str:
    scorer = REPO_ROOT / "vendor" / "score_trajectories.py"
    baseline = REPO_ROOT / "vendor" / "baseline_actions.csv"
    with tempfile.TemporaryDirectory(prefix="schema-dry-scorer-") as temporary:
        root = Path(temporary)
        shutil.copy2(baseline, root / "baseline_actions.csv")
        for dataset in ("gpt_5_6_sol", "claude_fable_opus"):
            trajectory = root / dataset / "stub_max_bp35_dry"
            trajectory.mkdir(parents=True)
            shutil.copy2(workdir / "events.jsonl", trajectory / "events.jsonl")
            shutil.copy2(workdir / "run.json", trajectory / "run.json")
        completed = subprocess.run(
            [
                sys.executable,
                str(scorer),
                "--root",
                str(root),
                "--expected",
                "0",
                "--no-manifest-check",
                "--compact",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"vendored scorer rejected dry-run events: {completed.stderr.strip()}"
            )
        return completed.stdout


def run_dry(args: argparse.Namespace) -> int:
    provider = "stub"
    workdir, snapshot = initialize_workdir(
        args.workdir,
        game=args.game,
        provider=provider,
        model="scripted-agent",
        max_actions=args.max_actions,
        system_prompt_file=args.system_prompt_file,
        experimental_tooling=args.experimental_tooling,
    )
    start_history_len = _run_started(
        workdir,
        snapshot,
        provider=provider,
        model="scripted-agent",
        max_actions=args.max_actions,
    )
    turn = next_turn_number(workdir)
    turn_id = f"turn-{turn:06d}"
    _turn_started(workdir, snapshot, turn=turn, surprise="")
    message = build_session_start_message(
        snapshot,
        workdir,
        (workdir / "notes.md").read_text(encoding="utf-8"),
        has_world_model=live_model_path(workdir) is not None,
    )
    with LocusService(
        workdir,
        args.game,
        turn_id,
        turn=turn,
        max_actions=args.max_actions,
        events_path=workdir / "events.jsonl",
        experimental_tooling=args.experimental_tooling,
    ) as service:
        text = ScriptedAgent().run(message, service)
    with EventLog(workdir / "events.jsonl", clock=time.time) as event_log:
        event_log.append(TextDelta(turn=turn, text=text))
        event_log.append(
            TurnTelemetry(
                turn=turn,
                session_id="stub-session",
                usage={},
                total_cost_usd=0.0,
                num_turns=1,
                is_error=False,
                projected_run_cost_usd=0.0,
            )
        )
    final_snapshot = load_snapshot(workdir)
    _finish_run(workdir, final_snapshot, start_history_len=start_history_len)
    scorer_output = _validate_with_vendored_scorer(workdir)
    print(f"Dry run complete: {workdir}")
    print(f"events: {workdir / 'events.jsonl'}")
    print("Vendored scorer: accepted")
    for line in scorer_output.splitlines():
        if "BP35" in line.upper():
            print(line)
            break
    return 0


def _usage_tokens(usage: Any) -> int:
    """Estimate active context occupancy, not aggregate per-turn billing tokens."""

    if not isinstance(usage, dict):
        return 0

    def total(bucket: dict[str, Any]) -> int:
        return sum(
            int(bucket.get(key) or 0)
            for key in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "output_tokens",
            )
        )

    # Claude's top-level usage accumulates every model call made during a tool-using
    # turn. Its per-iteration entries describe individual context windows, which is
    # what the rollover threshold is intended to bound.
    iterations = usage.get("iterations")
    if isinstance(iterations, list):
        contexts = [total(item) for item in iterations if isinstance(item, dict)]
        if contexts:
            return max(contexts)
    return total(usage)


def _aggregate_usage_tokens(usage: Any) -> int:
    """Return billable-style aggregate tokens without double-counting cache subsets."""

    if not isinstance(usage, dict):
        return 0
    return sum(
        int(usage.get(key) or 0)
        for key in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        )
    )


def _codex_usage_snapshot(
    usage: Any, previous: dict[str, int] | None = None
) -> dict[str, int]:
    """Return the cumulative counters emitted by pinned Codex JSONL."""

    prior = previous or {}
    current = usage if isinstance(usage, dict) else {}
    return {
        key: int(current[key]) if key in current else int(prior.get(key, 0))
        for key in _CODEX_USAGE_FIELDS
    }


def _codex_usage_has_counters(usage: Any) -> bool:
    """Return whether a Codex record contains a cumulative token snapshot."""

    return isinstance(usage, dict) and any(key in usage for key in _CODEX_USAGE_FIELDS)


def _codex_usage_delta_tokens(
    usage: Any,
    previous: dict[str, int] | None,
) -> tuple[int, dict[str, int]]:
    """Convert one thread-cumulative Codex snapshot into runner-turn tokens."""

    current = _codex_usage_snapshot(usage, previous)
    if previous is not None:
        regressions = [
            key for key in _CODEX_USAGE_FIELDS if current[key] < previous.get(key, 0)
        ]
        if regressions:
            fields = ", ".join(regressions)
            raise ValueError(f"Codex cumulative token usage regressed: {fields}")
    prior_total = _aggregate_usage_tokens(previous)
    return _aggregate_usage_tokens(current) - prior_total, current


def _codex_usage_violation(usage: Any) -> str | None:
    if not isinstance(usage, dict):
        return "Codex emitted malformed token usage"
    for key in _CODEX_USAGE_FIELDS:
        if key not in usage:
            continue
        value = usage[key]
        if type(value) is not int or value < 0:
            return f"Codex emitted invalid {key} usage"
    return None


def _verified_method_prompt(
    workdir: str | os.PathLike[str], expected_digest: str | None
) -> Path | None:
    """Return the snapshotted prompt only if its process-anchored digest still matches."""

    if expected_digest is None:
        return None
    prompt = Path(workdir) / "method_prompt.md"
    try:
        actual_digest = hashlib.sha256(prompt.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeError(f"snapshotted method prompt is unavailable: {prompt}") from exc
    if actual_digest != expected_digest:
        raise RuntimeError("snapshotted method prompt changed during the run")
    return prompt


def _verify_digest(path: Path, expected_digest: str, *, label: str) -> None:
    try:
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeError(f"{label} is unavailable: {path}") from exc
    if actual_digest != expected_digest:
        raise RuntimeError(f"{label} changed during the run")


def _validate_codex_workdir(workdir: Path) -> None:
    """Keep the native Codex process and repository in disjoint trees."""

    root = workdir.resolve()
    repository = REPO_ROOT.resolve()
    if root == repository or root.is_relative_to(repository):
        raise ValueError("Codex live workdir must be outside the harness repository")
    if repository.is_relative_to(root):
        raise ValueError("Codex live workdir must not contain the harness repository")


def _read_codex_subscription_auth(auth: Path) -> str:
    if not auth.is_file():
        raise RuntimeError(f"Codex subscription credential was not found: {auth}")
    if auth.stat().st_mode & 0o077:
        raise RuntimeError(
            f"Codex subscription credential is not private (expected 0600): {auth}"
        )
    try:
        auth_text = auth.read_text(encoding="utf-8")
        auth_payload = json.loads(auth_text)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Codex subscription credential is malformed") from exc
    if (
        not isinstance(auth_payload, dict)
        or auth_payload.get("auth_mode") != "chatgpt"
        or bool(auth_payload.get("OPENAI_API_KEY"))
        or not isinstance(auth_payload.get("tokens"), dict)
        or not bool(auth_payload["tokens"].get("access_token"))
    ):
        raise RuntimeError(
            "Codex gameplay requires ChatGPT subscription authentication; "
            "API-key authentication is not allowed"
        )
    return auth_text


def _prepare_codex_home(workdir: Path) -> Path:
    """Create an isolated Codex home with a refreshable subscription credential."""

    auth = Path.home() / ".codex" / "auth.json"
    identity = hashlib.sha256(str(workdir.resolve()).encode()).hexdigest()[:16]
    home = Path(tempfile.gettempdir()) / f"schema-codex-home-{os.getuid()}-{identity}"
    if os.path.lexists(home) and (home.is_symlink() or not home.is_dir()):
        raise RuntimeError(f"unexpected Codex home path: {home}")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    isolated_auth = home / "auth.json"
    if os.path.lexists(isolated_auth):
        if isolated_auth.is_symlink():
            if isolated_auth.resolve() != auth.resolve():
                raise RuntimeError(f"unexpected Codex credential path: {isolated_auth}")
            # Migrate the earlier bridge layout. A private copy lets Codex refresh
            # credentials atomically without modifying or replacing the host file.
            _atomic_text(isolated_auth, _read_codex_subscription_auth(auth))
        elif not isolated_auth.is_file():
            raise RuntimeError(f"unexpected Codex credential path: {isolated_auth}")
        else:
            _read_codex_subscription_auth(isolated_auth)
    else:
        _atomic_text(isolated_auth, _read_codex_subscription_auth(auth))
    return home


def _codex_session_available(codex_home: Path, session_id: str) -> bool:
    """Return whether the isolated temporary home still has a resumable rollout."""

    sessions = codex_home / "sessions"
    if not session_id or not sessions.is_dir() or sessions.is_symlink():
        return False
    sessions_root = sessions.resolve()
    for candidate in sessions.rglob("*.jsonl"):
        if not candidate.is_file() or candidate.is_symlink():
            continue
        try:
            if not candidate.resolve().is_relative_to(sessions_root):
                continue
            with candidate.open("r", encoding="utf-8") as handle:
                header_line = handle.readline(1_000_001)
            if len(header_line) > 1_000_000:
                continue
            header = json.loads(header_line)
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        payload = header.get("payload") if isinstance(header, dict) else None
        if (
            isinstance(header, dict)
            and header.get("type") == "session_meta"
            and isinstance(payload, dict)
            and payload.get("id") == session_id
        ):
            return True
    return False


def _prepare_codex_catalog(
    workdir: Path,
    *,
    codex_home: Path,
    model: str,
    effort: str,
) -> tuple[Path, str, str]:
    """Pin Luna metadata while making the native image viewer mechanically inert."""

    codex = shutil.which("codex")
    if codex is None:
        raise RuntimeError("codex executable was not found on PATH")
    version_result = subprocess.run(
        [codex, "--version"],
        env=_codex_environment(codex_home),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if version_result.returncode != 0:
        raise RuntimeError(f"codex --version failed: {version_result.stderr.strip()}")
    cli_version = version_result.stdout.strip()
    if cli_version != VALIDATED_CODEX_CLI_VERSION:
        raise RuntimeError(
            "Codex CLI version is outside the validated driver boundary: "
            f"expected {VALIDATED_CODEX_CLI_VERSION!r}, got {cli_version!r}"
        )
    path = workdir / "config" / "codex" / "model-catalog.json"
    run_path = workdir / "run.json"
    run_payload = (
        json.loads(run_path.read_text(encoding="utf-8")) if run_path.is_file() else {}
    )
    existing_driver = run_payload.get("driver")
    if existing_driver is not None and not isinstance(existing_driver, dict):
        raise RuntimeError("run.json contains malformed Codex driver metadata")
    if isinstance(existing_driver, dict):
        if existing_driver.get("cli_version") != cli_version:
            raise RuntimeError("Codex CLI version changed since trajectory initialization")
        expected_digest = existing_driver.get("model_catalog_sha256")
        if not isinstance(expected_digest, str) or not path.is_file():
            raise RuntimeError("pinned Codex catalog is unavailable on trajectory resume")
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_digest != expected_digest:
            raise RuntimeError("pinned Codex catalog changed since trajectory initialization")
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        # A fresh trajectory never trusts a catalog preseeded in its workdir.
        result = subprocess.run(
            [codex, "debug", "models", "--bundled"],
            env=_codex_environment(codex_home),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"codex model catalog failed: {result.stderr.strip()}")
        payload = json.loads(result.stdout)
        matches = [entry for entry in payload.get("models", []) if entry.get("slug") == model]
        if len(matches) != 1:
            raise RuntimeError(f"Codex model catalog does not contain exactly one {model!r}")
        selected = matches[0]
        selected["input_modalities"] = ["text"]
        selected["supports_image_detail_original"] = False
        selected["multi_agent_version"] = None
        selected["tool_mode"] = None
        selected["experimental_supported_tools"] = []
        _atomic_json(path, payload)
        path.chmod(0o444)

    matches = [entry for entry in payload.get("models", []) if entry.get("slug") == model]
    if len(matches) != 1:
        raise RuntimeError(f"pinned Codex catalog does not contain exactly one {model!r}")
    selected = matches[0]
    efforts = {
        level.get("effort")
        for level in selected.get("supported_reasoning_levels", [])
        if isinstance(level, dict)
    }
    if effort not in efforts:
        raise RuntimeError(f"{model} does not support {effort!r} effort")
    if selected.get("input_modalities") != ["text"]:
        raise RuntimeError("pinned Codex catalog did not disable image inputs")
    if selected.get("supports_image_detail_original") is not False:
        raise RuntimeError("pinned Codex catalog did not disable original image detail")
    if selected.get("multi_agent_version") is not None:
        raise RuntimeError("pinned Codex catalog did not disable model-level multi-agent helpers")
    if selected.get("tool_mode") is not None:
        raise RuntimeError("pinned Codex catalog did not disable model-level code mode")
    if selected.get("experimental_supported_tools") not in (None, []):
        raise RuntimeError("pinned Codex catalog retained experimental native tools")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, digest, cli_version


def _record_codex_metadata(
    workdir: Path,
    *,
    catalog_digest: str,
    cli_version: str,
    max_turns: int,
    turn_timeout: int,
    turn_token_cap: int,
    run_token_cap: int,
    no_progress_turns: int,
    only_reset_levels: str | None,
    model: str,
    effort: str,
    experimental_tooling: bool,
) -> None:
    path = workdir / "run.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    driver = {
        "cli_version": cli_version,
        "ephemeral_turns": False,
        "auto_compact_token_limit": DEFAULT_CODEX_COMPACT_TOKENS,
        "model_catalog": "config/codex/model-catalog.json",
        "model_catalog_sha256": catalog_digest,
        "disabled_features": list(_CODEX_DISABLED_FEATURES),
        "enabled_locus_tools": list(CODEX_LOCUS_TOOLS),
        "driver_policy_sha256": hashlib.sha256(CODEX_DRIVER_POLICY.encode()).hexdigest(),
        "policy_config_sha256": _codex_policy_config_digest(
            model=model,
            effort=effort,
            experimental_tooling=experimental_tooling,
        ),
        "native_image_input": False,
        "usd_cost_available": False,
        "max_turns": max_turns,
        "turn_timeout_seconds": turn_timeout,
        "turn_token_cap": turn_token_cap,
        "run_token_cap": run_token_cap,
        "no_progress_turns": no_progress_turns,
        "only_reset_levels": only_reset_levels,
        "experimental_tooling": experimental_tooling,
    }
    existing = payload.get("driver")
    if existing is not None and existing != driver:
        raise ValueError("workdir was created with different Codex driver metadata")
    if existing is None:
        payload["driver"] = driver
        _atomic_json(path, payload)


def _codex_environment(codex_home: Path) -> dict[str, str]:
    """Build a minimal child environment without forwarding API keys or tokens."""

    allowed = (
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "PATH",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
        "TMPDIR",
        "USER",
    )
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment["CODEX_HOME"] = str(codex_home)
    return environment


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (int, float)):
        return str(value)
    raise TypeError(f"unsupported TOML override value: {type(value).__name__}")


def _codex_command(
    *,
    driver_cwd: Path,
    workdir: Path,
    game: str,
    turn: int,
    turn_id: str,
    max_actions: int,
    model: str,
    effort: str,
    method_prompt: str,
    model_catalog: Path,
    session_id: str = "",
    resume: bool = False,
    tool_timeout: int = 1200,
    experimental_tooling: bool = False,
) -> list[str]:
    """Construct a strict new or resumed Codex invocation for one Locus turn."""

    codex = shutil.which("codex")
    if codex is None:
        raise RuntimeError("codex executable was not found on PATH")
    if resume and not session_id:
        raise ValueError("a Codex session id is required to resume")
    if tool_timeout < 1:
        raise ValueError("Codex MCP tool timeout must be positive")
    command = [codex, "exec"]
    if resume:
        command.append("resume")
    command.extend(
        [
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--skip-git-repo-check",
        "--json",
        "--model",
        model,
        ]
    )
    if not resume:
        command.extend(("--color", "never", "-C", str(driver_cwd)))
    for feature in _CODEX_DISABLED_FEATURES:
        command.extend(("--disable", feature))

    environment = _locus_environment(
        workdir,
        game=game,
        turn=turn,
        turn_id=turn_id,
        max_actions=max_actions,
        experimental_tooling=experimental_tooling,
    )
    developer_instructions = f"{method_prompt.rstrip()}\n\n{CODEX_DRIVER_POLICY}\n"
    overrides: list[tuple[str, Any]] = [
        ("model_reasoning_effort", effort),
        ("model_catalog_json", str(model_catalog)),
        ("model_auto_compact_token_limit", DEFAULT_CODEX_COMPACT_TOKENS),
        ("approval_policy", "never"),
        ("sandbox_mode", "read-only"),
        ("web_search", "disabled"),
        ("allow_login_shell", False),
        ("check_for_update_on_startup", False),
        ("analytics.enabled", False),
        ("feedback.enabled", False),
        ("project_doc_max_bytes", 0),
        ("project_root_markers", []),
        ("include_permissions_instructions", False),
        ("include_apps_instructions", False),
        ("include_collaboration_mode_instructions", False),
        ("include_environment_context", False),
        ("skills.include_instructions", False),
        ("skills.bundled.enabled", False),
        ("developer_instructions", developer_instructions),
        ("mcp_servers.locus.command", sys.executable),
        ("mcp_servers.locus.args", ["-m", "schema_harness.locus"]),
        ("mcp_servers.locus.required", True),
        ("mcp_servers.locus.startup_timeout_sec", 30),
        ("mcp_servers.locus.tool_timeout_sec", tool_timeout),
        ("mcp_servers.locus.default_tools_approval_mode", "approve"),
        ("mcp_servers.locus.enabled_tools", list(CODEX_LOCUS_TOOLS)),
    ]
    overrides.extend(
        (f"mcp_servers.locus.env.{key}", value)
        for key, value in sorted(environment.items())
    )
    for key, value in overrides:
        command.extend(("-c", f"{key}={_toml_value(value)}"))
    if resume:
        command.append(session_id)
    command.append("-")
    return command


def _parse_codex_jsonl(
    stdout: str,
    stderr: str,
    *,
    returncode: int,
    timed_out: bool = False,
    expected_session_id: str = "",
) -> dict[str, Any]:
    """Normalize Codex JSONL and reject any non-Locus native tool activity."""

    records: list[dict[str, Any]] = []
    malformed = False
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            malformed = True
            continue
        if not isinstance(record, dict):
            malformed = True
            continue
        records.append(record)

    session_id = ""
    usage: dict[str, Any] = {}
    final_text = ""
    completed = False
    failed = False
    violations: list[str] = []
    lifecycle_indices: dict[str, list[int]] = {
        "thread.started": [],
        "turn.started": [],
        "turn.completed": [],
        "turn.failed": [],
    }
    item_indices: list[int] = []
    item_lifecycles: dict[str, list[tuple[str, int, str]]] = {}
    for index, record in enumerate(records):
        record_type = record.get("type")
        if (
            not isinstance(record_type, str)
            or record_type not in _CODEX_ALLOWED_RECORD_TYPES
        ):
            violations.append(f"unknown Codex record: {record_type!r}")
            continue
        if record_type in lifecycle_indices:
            lifecycle_indices[record_type].append(index)
        if record_type == "thread.started" and isinstance(record.get("thread_id"), str):
            session_id = record["thread_id"]
        elif record_type == "turn.completed":
            completed = True
            raw_usage = record.get("usage")
            usage_violation = _codex_usage_violation(raw_usage)
            if usage_violation is not None:
                violations.append(usage_violation)
            else:
                usage = dict(raw_usage)
        elif record_type in {"turn.failed", "error"}:
            failed = True
        if not str(record_type).startswith("item."):
            continue
        item_indices.append(index)
        item = record.get("item")
        if not isinstance(item, dict):
            violations.append("malformed item event")
            continue
        item_type = str(item.get("type") or "")
        # Even a known stream-lag error remains fatal: dropped events make it
        # impossible to prove that no native or unapproved tool call occurred.
        if item_type not in _CODEX_ALLOWED_ITEM_TYPES:
            violations.append(f"native or unknown Codex item: {item_type or '<missing>'}")
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            violations.append("allowed Codex item is missing a stable id")
        else:
            item_lifecycles.setdefault(item_id, []).append(
                (record_type, index, item_type)
            )
        if item_type == "agent_message" and record_type == "item.completed":
            final_text = str(item.get("text") or "")
        if item_type == "mcp_tool_call":
            server = item.get("server") or item.get("server_name")
            tool = item.get("tool") or item.get("tool_name") or item.get("name")
            if server != "locus" or tool not in CODEX_LOCUS_TOOLS:
                violations.append(f"unapproved MCP call: {server!r}/{tool!r}")

    if expected_session_id and session_id and session_id != expected_session_id:
        violations.append(
            "Codex resumed a different session: "
            f"expected {expected_session_id!r}, got {session_id!r}"
        )

    if not timed_out:
        for record_type in ("thread.started", "turn.started"):
            count = len(lifecycle_indices[record_type])
            if count != 1:
                violations.append(
                    f"Codex stream has {count} {record_type} records; expected exactly 1"
                )
        terminal_indices = (
            lifecycle_indices["turn.completed"] + lifecycle_indices["turn.failed"]
        )
        if len(terminal_indices) != 1:
            violations.append(
                "Codex stream has "
                f"{len(terminal_indices)} terminal turn records; expected exactly 1"
            )
        if (
            len(lifecycle_indices["thread.started"]) == 1
            and len(lifecycle_indices["turn.started"]) == 1
            and len(terminal_indices) == 1
        ):
            thread_index = lifecycle_indices["thread.started"][0]
            turn_index = lifecycle_indices["turn.started"][0]
            terminal_index = terminal_indices[0]
            if not thread_index < turn_index < terminal_index:
                violations.append("Codex lifecycle records are out of order")
            elif any(
                item_index <= turn_index or item_index >= terminal_index
                for item_index in item_indices
            ):
                violations.append("Codex item record lies outside the active turn")
        for item_id, events in item_lifecycles.items():
            event_types = [event[0] for event in events]
            item_types = {event[2] for event in events}
            if len(item_types) != 1:
                violations.append(f"Codex item {item_id!r} changed type")
                continue
            item_type = events[0][2]
            started_count = event_types.count("item.started")
            completed_count = event_types.count("item.completed")
            updated_count = event_types.count("item.updated")
            if completed_count != 1:
                violations.append(
                    f"Codex item {item_id!r} has {completed_count} completion records; "
                    "expected exactly 1"
                )
                continue
            # Pinned CLI 0.144.1 emits reasoning and agent-message summaries as
            # completion-only items. MCP calls are the allowed effectful items and
            # must carry a correlated start and completion.
            if item_type == "mcp_tool_call" and started_count != 1:
                violations.append(
                    f"Codex MCP item {item_id!r} has {started_count} start records; "
                    "expected exactly 1"
                )
                continue
            if started_count > 1 or (started_count == 0 and updated_count):
                violations.append(f"Codex item {item_id!r} has an invalid lifecycle")
                continue
            if started_count == 1 and (
                event_types[0] != "item.started"
                or event_types[-1] != "item.completed"
            ):
                violations.append(f"Codex item {item_id!r} lifecycle is out of order")

    stderr_lower = stderr.lower()
    if "replaced unavailable requested model" in stderr_lower:
        violations.append("Codex replaced the requested model")
    if "tools::router" in stderr_lower:
        violations.append("Codex attempted a rejected native tool")
    if "event stream lagged" in stderr_lower and "dropped" in stderr_lower:
        violations.append("Codex event stream dropped records")
    if timed_out:
        violations.append("Codex turn timed out before complete stream audit")
    if malformed and not timed_out:
        violations.append("Codex emitted malformed JSONL")
    violations = list(dict.fromkeys(violations))
    usage["cost_available"] = False
    is_error = bool(violations or failed)
    if not timed_out:
        is_error = is_error or returncode != 0 or malformed or not completed or not session_id
    if is_error:
        reason = "; ".join(violations) if violations else "turn failed"
        final_text = f"Codex driver rejected the turn: {reason}. See private driver logs."
    return {
        "session_id": session_id,
        "usage": usage,
        "total_cost_usd": 0.0,
        "cost_available": False,
        "num_turns": 1 if completed else 0,
        "is_error": is_error,
        "timed_out": timed_out,
        "result": final_text,
        "violations": violations,
    }


def run_codex_turn(
    message: str,
    *,
    workdir: Path,
    codex_home: Path,
    model_catalog: Path,
    catalog_digest: str,
    game: str,
    turn: int,
    turn_id: str,
    max_actions: int,
    model: str,
    effort: str,
    session_id: str,
    resume: bool,
    timeout: int,
    system_prompt_file: Path | None,
    experimental_tooling: bool = False,
) -> dict[str, Any]:
    """Run one Codex turn and persist its raw private driver transcript."""

    _verify_digest(model_catalog, catalog_digest, label="pinned Codex catalog")
    driver_cwd = workdir / "config" / "codex" / "driver"
    driver_cwd.mkdir(parents=True, exist_ok=True)
    method_prompt = (
        system_prompt_file.read_text(encoding="utf-8")
        if system_prompt_file is not None
        else ""
    )
    command = _codex_command(
        driver_cwd=driver_cwd,
        workdir=workdir,
        game=game,
        turn=turn,
        turn_id=turn_id,
        max_actions=max_actions,
        model=model,
        effort=effort,
        method_prompt=method_prompt,
        model_catalog=model_catalog,
        session_id=session_id,
        resume=resume,
        tool_timeout=timeout,
        experimental_tooling=experimental_tooling,
    )
    process = subprocess.Popen(
        command,
        cwd=str(driver_cwd),
        env=_codex_environment(codex_home),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(input=message, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()

    raw = workdir / "sessions" / f"codex-turn-{turn:06d}.jsonl"
    _atomic_text(raw, stdout)
    if stderr:
        _atomic_text(raw.with_suffix(".stderr"), stderr)
    credential_bridge_changed = False
    try:
        credential_bridge_changed = _prepare_codex_home(workdir) != codex_home
    except RuntimeError:
        credential_bridge_changed = True
    result = _parse_codex_jsonl(
        stdout,
        stderr,
        returncode=int(process.returncode or 0),
        timed_out=timed_out,
        expected_session_id=session_id if resume else "",
    )
    if credential_bridge_changed:
        result["violations"].append("Codex credential bridge changed during the turn")
        result["is_error"] = True
        result["result"] = (
            "Codex driver rejected the turn: credential bridge changed. "
            "See private driver logs."
        )
    return result


def _record_driver_result(
    workdir: Path,
    *,
    turn: int,
    result: dict[str, Any],
    projected: float,
) -> float:
    cost = float(result.get("total_cost_usd") or 0.0)
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    text = str(result.get("result") or "")
    with EventLog(workdir / "events.jsonl", clock=time.time) as event_log:
        if text:
            event_log.append(TextDelta(turn=turn, text=text))
        event_log.append(
            TurnTelemetry(
                turn=turn,
                session_id=str(result.get("session_id") or ""),
                usage=usage,
                total_cost_usd=cost,
                num_turns=int(result.get("num_turns") or 0),
                is_error=bool(result.get("is_error")),
                projected_run_cost_usd=projected,
            )
        )
    return cost


def _historical_driver_totals(
    workdir: Path,
) -> tuple[list[float], int, int, dict[str, dict[str, int]]]:
    """Recover enforceable trajectory totals across bounded runner restarts."""

    costs: list[float] = []
    tokens = 0
    turns: set[int] = set()
    session_usage: dict[str, dict[str, int]] = {}
    events = workdir / "events.jsonl"
    if not events.is_file():
        return costs, tokens, 0, session_usage
    for _, record in iter_json_objects(events):
        if record.get("kind") == "turn_started" and type(record.get("turn")) is int:
            turns.add(record["turn"])
        if record.get("kind") != "turn_telemetry":
            continue
        usage = record.get("usage")
        usage_violation = _codex_usage_violation(usage)
        if usage_violation is not None:
            raise RuntimeError("events.jsonl contains malformed Codex token usage")
        session_id = record.get("session_id")
        if isinstance(session_id, str) and session_id:
            if _codex_usage_has_counters(usage):
                try:
                    turn_tokens, snapshot = _codex_usage_delta_tokens(
                        usage, session_usage.get(session_id)
                    )
                except ValueError as exc:
                    raise RuntimeError(
                        "events.jsonl contains regressed Codex token usage"
                    ) from exc
                tokens += turn_tokens
                session_usage[session_id] = snapshot
        else:
            # Older telemetry did not identify a session and cannot be safely
            # differenced, so preserve its original additive interpretation.
            tokens += _aggregate_usage_tokens(usage)
        if not isinstance(usage, dict) or usage.get("cost_available") is not False:
            costs.append(float(record.get("total_cost_usd") or 0.0))
    return costs, tokens, len(turns), session_usage


def _historical_no_progress(workdir: Path, snapshot: GatewaySnapshot) -> int:
    """Recover the trailing count of turns that committed no state transition."""

    starts: list[tuple[int, int]] = []
    events = workdir / "events.jsonl"
    if not events.is_file():
        return 0
    for _, record in iter_json_objects(events):
        if record.get("kind") != "turn_started":
            continue
        level = record.get("level")
        history_len = record.get("env_step")
        if type(level) is int and type(history_len) is int:
            starts.append((level, history_len))

    no_progress = 0
    end = (snapshot.level, snapshot.history_len)
    for start in reversed(starts):
        if end[0] > start[0] or end[1] > start[1]:
            break
        no_progress += 1
        end = start
    return no_progress


def run_live(args: argparse.Namespace) -> int:
    with _live_run_lock():
        return _run_live(args)


def _run_live(args: argparse.Namespace) -> int:
    if args.provider == "codex":
        if os.environ.get("ONLY_RESET_LEVELS") != "true":
            raise RuntimeError("Codex live runs require ONLY_RESET_LEVELS=true")
        if args.effort not in _CODEX_REASONING_EFFORTS:
            raise ValueError(f"unsupported Codex reasoning effort: {args.effort!r}")
        _validate_codex_workdir(args.workdir)
    workdir, snapshot = initialize_workdir(
        args.workdir,
        game=args.game,
        provider=args.provider,
        model=args.model,
        max_actions=args.max_actions,
        effort=args.effort,
        system_prompt_file=args.system_prompt_file,
        experimental_tooling=args.experimental_tooling,
    )
    token: str | None = None
    codex_home: Path | None = None
    codex_catalog: Path | None = None
    if args.provider == "claude":
        token = oauth_token()
        if not token:
            raise RuntimeError(
                "Claude Code OAuth token was not found in macOS Keychain "
                "(Claude Code-credentials)"
            )
        saved_session = load_driver_session(
            workdir, provider=args.provider, model=args.model
        )
        session_id, resume = saved_session or (str(uuid.uuid4()), False)
    else:
        codex_home = _prepare_codex_home(workdir)
        codex_catalog, catalog_digest, cli_version = _prepare_codex_catalog(
            workdir,
            codex_home=codex_home,
            model=args.model,
            effort=args.effort,
        )
        _record_codex_metadata(
            workdir,
            catalog_digest=catalog_digest,
            cli_version=cli_version,
            max_turns=args.max_turns,
            turn_timeout=args.turn_timeout,
            turn_token_cap=args.turn_token_cap,
            run_token_cap=args.run_token_cap,
            no_progress_turns=args.no_progress_turns,
            only_reset_levels="true",
            model=args.model,
            effort=args.effort,
            experimental_tooling=args.experimental_tooling,
        )
        saved_session = load_driver_session(
            workdir, provider=args.provider, model=args.model
        )
        session_id, resume = saved_session or ("", False)
        if resume and not _codex_session_available(codex_home, session_id):
            print("Codex session state unavailable; starting a fresh driver session")
            session_id, resume = "", False

    if args.provider == "codex":
        (
            costs,
            total_tokens,
            historical_turns,
            codex_session_usage,
        ) = _historical_driver_totals(workdir)
        no_progress = _historical_no_progress(workdir, snapshot)
    else:
        costs, total_tokens, historical_turns = [], 0, 0
        codex_session_usage = {}
        no_progress = 0
    total_cost = sum(costs)
    if historical_turns >= args.max_turns:
        print(f"stopping: trajectory turn cap {args.max_turns} already reached")
        return 0
    if args.run_token_cap and total_tokens >= args.run_token_cap:
        print(f"stopping: run token cap {args.run_token_cap:,} already reached")
        return 0
    if costs and total_cost >= args.run_cost_cap:
        print(f"stopping: run cost cap ${args.run_cost_cap:.2f} already reached")
        return 0
    if no_progress >= args.no_progress_turns:
        print(f"stopping: no progress for {no_progress} prior turns")
        return 0
    remaining_turns = args.max_turns - historical_turns
    start_history_len = _run_started(
        workdir,
        snapshot,
        provider=args.provider,
        model=args.model,
        max_actions=args.max_actions,
    )
    previous_level = snapshot.level
    previous_history = snapshot.history_len
    driver_failed = False
    first_turn = next_turn_number(workdir)
    previous = load_previous_committed_turn(workdir, first_turn)
    surprise = previous.result.surprise if previous is not None else ""
    prompt_digest = (
        str(json.loads((workdir / "run.json").read_text(encoding="utf-8"))[
            "system_prompt_sha256"
        ])
        if args.system_prompt_file
        else None
    )

    for turn_offset in range(remaining_turns):
        turn = first_turn + turn_offset
        snapshot = load_snapshot(workdir)
        if snapshot.state == "WIN" or snapshot.history_len >= args.max_actions:
            break
        turn_id = f"turn-{turn:06d}"
        _turn_started(workdir, snapshot, turn=turn, surprise=surprise)
        model_path = live_model_path(workdir)
        message = build_turn_message(
            snapshot,
            workdir,
            notes=(workdir / "notes.md").read_text(encoding="utf-8"),
            previous=previous,
            has_world_model=model_path is not None,
            model_filename=model_path.name if model_path else "world_model_v5.py",
            session_start=not resume,
        )
        mcp_config = write_mcp_config(
            workdir,
            game=args.game,
            turn=turn,
            turn_id=turn_id,
            max_actions=args.max_actions,
            experimental_tooling=args.experimental_tooling,
        )
        method_prompt = _verified_method_prompt(workdir, prompt_digest)
        if args.provider == "claude":
            result = run_claude_turn(
                message,
                session_id=session_id,
                resume=resume,
                cwd=workdir,
                config_dir=workdir / "config" / "claude",
                locus_log=workdir / "runtime" / "locus.jsonl",
                mcp_cfg=mcp_config,
                model=args.model,
                token=token,
                effort=args.effort,
                timeout=args.turn_timeout,
                system_prompt_file=method_prompt,
                allowed_tools=CLAUDE_LOCUS_TOOLS,
            )
        else:
            assert codex_home is not None and codex_catalog is not None
            result = run_codex_turn(
                message,
                workdir=workdir,
                codex_home=codex_home,
                model_catalog=codex_catalog,
                catalog_digest=catalog_digest,
                game=args.game,
                turn=turn,
                turn_id=turn_id,
                max_actions=args.max_actions,
                model=args.model,
                effort=args.effort,
                session_id=session_id,
                resume=resume,
                timeout=args.turn_timeout,
                system_prompt_file=method_prompt,
                experimental_tooling=args.experimental_tooling,
            )
        if not isinstance(result, dict):
            raise RuntimeError(f"{args.provider} driver did not return structured output")
        usage_violation = None
        if args.provider == "codex":
            usage_violation = _codex_usage_violation(result.get("usage"))
            if usage_violation is not None:
                violations = result.get("violations")
                violation_list = (
                    [str(item) for item in violations]
                    if isinstance(violations, list)
                    else []
                )
                violation_list.append(usage_violation)
                result["violations"] = list(dict.fromkeys(violation_list))
                result["usage"] = {"cost_available": False}
                result["is_error"] = True
                result["result"] = (
                    "Codex driver rejected the turn: malformed token usage. "
                    "See private driver logs."
                )
        reported_session_id = str(result.get("session_id") or "")
        turn_tokens = _aggregate_usage_tokens(result.get("usage"))
        if (
            args.provider == "codex"
            and usage_violation is None
            and _codex_usage_has_counters(result.get("usage"))
        ):
            try:
                turn_tokens, usage_snapshot = _codex_usage_delta_tokens(
                    result.get("usage"),
                    codex_session_usage.get(reported_session_id),
                )
            except ValueError as exc:
                violations = result.get("violations")
                violation_list = (
                    [str(item) for item in violations]
                    if isinstance(violations, list)
                    else []
                )
                violation_list.append(str(exc))
                result["violations"] = list(dict.fromkeys(violation_list))
                result["is_error"] = True
                result["result"] = (
                    "Codex driver rejected the turn: cumulative token usage regressed. "
                    "See private driver logs."
                )
                turn_tokens = 0
            else:
                if reported_session_id:
                    codex_session_usage[reported_session_id] = usage_snapshot
        result_is_error = bool(result.get("is_error"))
        if not result_is_error and reported_session_id:
            session_id = reported_session_id
        cost_available = result.get("cost_available") is not False
        turn_cost_value = float(result.get("total_cost_usd") or 0.0)
        expected_average = (
            (sum(costs) + turn_cost_value) / (len(costs) + 1)
            if cost_available
            else 0.0
        )
        projected = (
            total_cost + expected_average * (remaining_turns - turn_offset)
            if cost_available
            else 0.0
        )
        turn_cost = _record_driver_result(
            workdir,
            turn=turn,
            result=result,
            projected=projected,
        )
        if cost_available:
            costs.append(turn_cost)
            total_cost += turn_cost
        total_tokens += turn_tokens
        if cost_available:
            print(
                f"turn {turn}: cost=${turn_cost:.4f}; total=${total_cost:.4f}; "
                f"projected=${projected:.4f}; tokens={turn_tokens:,}"
            )
        else:
            print(
                f"turn {turn}: subscription USD unavailable; "
                f"tokens={turn_tokens:,}; cumulative tokens={total_tokens:,}"
            )

        committed = load_committed_turn(workdir, turn_id)
        if committed is None:
            fallback_reason = (
                f"{args.provider} turn timed out after {args.turn_timeout}s — no committed action"
                if bool(result.get("timed_out"))
                else FALLBACK_REASON
            )
            with EventLog(workdir / "events.jsonl", clock=time.time) as event_log:
                event_log.append(TurnFallback(turn=turn, reason=fallback_reason))
            previous = None
            surprise = fallback_reason
        else:
            previous = committed
            surprise = committed.result.surprise

        next_snapshot = load_snapshot(workdir)
        progressed = (
            next_snapshot.level > previous_level
            or next_snapshot.history_len > previous_history
        )
        no_progress = 0 if progressed else no_progress + 1
        previous_level = next_snapshot.level
        previous_history = next_snapshot.history_len

        if args.provider == "codex" and not result_is_error:
            resume = bool(session_id)
        elif (
            args.provider == "claude"
            and not result_is_error
            and _usage_tokens(result.get("usage")) >= args.context_rollover_tokens
        ):
            session_id = str(uuid.uuid4())
            resume = False
        elif args.provider == "claude" and not result_is_error:
            resume = True

        if session_id and not result_is_error:
            sessions = workdir / "sessions" / "sessions.json"
            _atomic_json(
                sessions,
                {
                    "cwd": str(workdir),
                    "provider": args.provider,
                    "model": args.model,
                    "sid": session_id,
                    "resume": resume,
                },
            )

        if result_is_error:
            if args.provider == "codex":
                _atomic_json(
                    workdir / "sessions" / "sessions.json",
                    {
                        "cwd": str(workdir),
                        "provider": args.provider,
                        "model": args.model,
                        "sid": "",
                        "resume": False,
                        "invalidated": True,
                        "turn": turn,
                    },
                )
            violations = result.get("violations")
            detail = (
                ": " + "; ".join(str(item) for item in violations)
                if isinstance(violations, list) and violations
                else ""
            )
            print(f"stopping: {args.provider} driver returned an error{detail}")
            driver_failed = True
            break
        if cost_available and turn_cost > args.turn_cost_cap:
            print(f"stopping: per-turn cost cap ${args.turn_cost_cap:.2f} exceeded")
            break
        if cost_available and total_cost >= args.run_cost_cap:
            print(f"stopping: run cost cap ${args.run_cost_cap:.2f} reached")
            break
        if (
            args.provider == "codex"
            and args.turn_token_cap
            and turn_tokens > args.turn_token_cap
        ):
            print(f"stopping: per-turn token cap {args.turn_token_cap:,} exceeded")
            break
        if (
            args.provider == "codex"
            and args.run_token_cap
            and total_tokens >= args.run_token_cap
        ):
            print(f"stopping: run token cap {args.run_token_cap:,} reached")
            break
        if no_progress >= args.no_progress_turns:
            print(f"stopping: no progress for {no_progress} turns")
            break
        if next_snapshot.state == "WIN" or next_snapshot.history_len >= args.max_actions:
            break

    final_snapshot = load_snapshot(workdir)
    _finish_run(workdir, final_snapshot, start_history_len=start_history_len)
    return 1 if driver_failed else 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", default=DEFAULT_GAME)
    parser.add_argument("--provider", choices=("codex", "claude"), default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", default=None, help="provider reasoning effort, e.g. max")
    parser.add_argument("--turn-timeout", type=int, default=1200,
                        help="per-turn wall-clock seconds before the driver process group is killed")
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--max-actions", type=int, default=3000)
    parser.add_argument(
        "--max-turns",
        type=int,
        default=300,
        help="total trajectory turns for Codex; turns in this invocation for Claude",
    )
    parser.add_argument("--turn-cost-cap", type=float, default=2.0)
    parser.add_argument("--run-cost-cap", type=float, default=50.0)
    parser.add_argument("--turn-token-cap", type=int, default=1_000_000)
    parser.add_argument(
        "--run-token-cap",
        type=int,
        default=60_000_000,
        help="cumulative Codex trajectory-token cap; 0 disables",
    )
    parser.add_argument("--no-progress-turns", type=int, default=5)
    parser.add_argument("--context-rollover-tokens", type=int, default=150_000)
    parser.add_argument(
        "--system-prompt-file",
        type=Path,
        default=DEFAULT_SYSTEM_PROMPT,
        help="standing method prompt injected through the selected provider",
    )
    parser.add_argument(
        "--no-system-prompt",
        action="store_true",
        help="disable the standing method prompt for an ablation run",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--experimental-tooling",
        action="store_true",
        help="enable unproven inspector appendices and commit gates",
    )
    args = parser.parse_args(argv)
    args.game = _canonical_game_id(args.game)
    if args.provider is None:
        args.provider = (
            "claude"
            if isinstance(args.model, str) and args.model.startswith("claude-")
            else DEFAULT_PROVIDER
        )
    if args.model is None:
        args.model = (
            DEFAULT_CODEX_MODEL if args.provider == "codex" else DEFAULT_CLAUDE_MODEL
        )
    if args.effort is None and args.provider == "codex":
        args.effort = DEFAULT_CODEX_EFFORT
    if args.no_system_prompt:
        args.system_prompt_file = None
    elif not args.system_prompt_file.is_file():
        parser.error(f"system prompt file not found: {args.system_prompt_file}")
    if args.workdir is None:
        if args.dry_run:
            args.workdir = Path(tempfile.mkdtemp(prefix="schema-bp35-dry-"))
        else:
            try:
                game_label = short_game_id(args.game)
            except ValueError as exc:
                parser.error(str(exc))
            args.workdir = Path.home() / f"agent-{game_label}"
    if args.max_actions < 1 or args.max_turns < 1:
        parser.error("--max-actions and --max-turns must be positive")
    if args.turn_timeout < 1:
        parser.error("--turn-timeout must be positive")
    if args.turn_cost_cap < 0 or args.run_cost_cap < 0:
        parser.error("cost caps must be non-negative")
    if args.turn_token_cap < 0 or args.run_token_cap < 0:
        parser.error("token caps must be non-negative")
    if args.no_progress_turns < 1:
        parser.error("--no-progress-turns must be positive")
    if args.context_rollover_tokens < 1:
        parser.error("--context-rollover-tokens must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return run_dry(args) if args.dry_run else run_live(args)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CommittedTurn",
    "DEFAULT_GAME",
    "DEFAULT_MODEL",
    "DEFAULT_SYSTEM_PROMPT",
    "NOTES_TEMPLATE",
    "ScriptedAgent",
    "build_mid_session_message",
    "build_session_start_message",
    "build_turn_message",
    "encode_grid",
    "initialize_workdir",
    "live_model_path",
    "load_committed_turn",
    "load_driver_session",
    "load_previous_committed_turn",
    "load_snapshot",
    "main",
    "next_turn_number",
    "parse_args",
    "run_dry",
    "run_live",
    "write_mcp_config",
]
