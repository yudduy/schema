"""Turn-by-turn Schema harness runner using the proven headless Claude driver."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "schema_harness"

from spikes.driver_probe import DISALLOWED, oauth_token, run_turn as run_claude_turn

from .events import (
    EventLog,
    RunFinished,
    RunStarted,
    TextDelta,
    TurnFallback,
    TurnStarted,
    TurnTelemetry,
)
from .gateway import ExecutionResult, GatewaySnapshot, PersistentGateway
from .locus import LocusService
from .narration import commit_result_narration, world_model_line


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GAME = "bp35-0a0ad940"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_SYSTEM_PROMPT = REPO_ROOT / "schema_harness" / "prompts" / "physicist_v1.md"
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


@dataclass(frozen=True, slots=True)
class CommittedTurn:
    plan: list[list[int | None]]
    reason: str
    result: ExecutionResult


def canonical_game_id(game: str) -> str:
    """Resolve a public-game shorthand to the scorer's versioned game id."""

    baseline = REPO_ROOT / "vendor" / "baseline_actions.csv"
    if "-" in game or not baseline.is_file():
        return game
    with baseline.open(encoding="utf-8", newline="") as handle:
        matches = [
            row["game_id"]
            for row in csv.DictReader(handle)
            if row.get("game") == game and row.get("game_id")
        ]
    return matches[0] if len(matches) == 1 else game


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
    system_prompt_file: str | os.PathLike[str] | None = None,
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
        if existing.get("system_prompt_sha256") != prompt_digest:
            raise ValueError("workdir was created with a different system prompt")

    notes = root / "notes.md"
    if not notes.exists():
        notes.write_text(NOTES_TEMPLATE, encoding="utf-8", newline="")
    _copy_framework(root)

    config = root / "config" / "claude"
    config.mkdir(parents=True, exist_ok=True)
    settings = config / "settings.json"
    if not settings.exists():
        _atomic_json(settings, {"autoCompactEnabled": False})
    account_marker = Path.home() / ".claude.json"
    isolated_marker = config / ".claude.json"
    if account_marker.exists() and not isolated_marker.exists():
        shutil.copy2(account_marker, isolated_marker)

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
        "max_actions": max_actions,
        "win_levels": snapshot.win_levels,
        "workdir": str(root),
        "started_at": time.time(),
        "system_prompt": "method_prompt.md" if prompt_digest else None,
        "system_prompt_sha256": prompt_digest,
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


def load_driver_session(workdir: str | os.PathLike[str]) -> tuple[str, bool] | None:
    """Load a durable Claude session checkpoint written after a completed turn."""

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
        for line in events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            turn = event.get("turn") if isinstance(event, dict) else None
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
) -> Path:
    """Write the strict one-server MCP config inherited by the headless driver."""

    python_path = os.environ.get("PYTHONPATH")
    combined_pythonpath = str(REPO_ROOT)
    if python_path:
        combined_pythonpath += os.pathsep + python_path
    environment = {
        "PYTHONPATH": combined_pythonpath,
        "LOCUS_WORKDIR": str(workdir),
        "LOCUS_GAME": game,
        "LOCUS_TURN": str(turn),
        "LOCUS_TURN_ID": turn_id,
        "LOCUS_MAX_ACTIONS": str(max_actions),
        "LOCUS_EVENTS": str(workdir / "events.jsonl"),
        "LOCUS_LOG": str(workdir / "runtime" / "locus.jsonl"),
        # Claude needs the OAuth token; the spawned MCP/tool subprocesses do not.
        "CLAUDE_CODE_OAUTH_TOKEN": "",
    }
    config = {
        "mcpServers": {
            "locus": {
                "command": sys.executable,
                "args": ["-m", "schema_harness.locus"],
                "env": environment,
            }
        }
    }
    path = workdir / "mcp.json"
    _atomic_json(path, config)
    return path


def _run_started(
    workdir: Path,
    snapshot: GatewaySnapshot,
    *,
    provider: str,
    model: str,
    max_actions: int,
) -> int:
    resumed = snapshot.history_len > 0
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


def run_live(args: argparse.Namespace) -> int:
    workdir, snapshot = initialize_workdir(
        args.workdir,
        game=args.game,
        provider="claude",
        model=args.model,
        max_actions=args.max_actions,
        system_prompt_file=args.system_prompt_file,
    )
    start_history_len = _run_started(
        workdir,
        snapshot,
        provider="claude",
        model=args.model,
        max_actions=args.max_actions,
    )
    token = oauth_token()
    if not token:
        raise RuntimeError(
            "Claude Code OAuth token was not found in macOS Keychain "
            "(Claude Code-credentials)"
        )

    saved_session = load_driver_session(workdir)
    session_id, resume = saved_session or (str(uuid.uuid4()), False)
    total_cost = 0.0
    costs: list[float] = []
    no_progress = 0
    previous_level = snapshot.level
    previous_history = snapshot.history_len
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

    for turn_offset in range(args.max_turns):
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
        )
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
            system_prompt_file=_verified_method_prompt(workdir, prompt_digest),
        )
        if result is None:
            # Turn timed out (killed mid-deliberation) — no commit this turn. Log a fallback
            # and continue; the next turn resumes the same session.
            with EventLog(workdir / "events.jsonl", clock=time.time) as event_log:
                event_log.append(TurnFallback(
                    turn=turn, reason=f"claude turn timed out after {args.turn_timeout}s — no action taken"
                ))
            resume = True
            sessions = workdir / "sessions" / "sessions.json"
            _atomic_json(
                sessions,
                {"cwd": str(workdir), "sid": session_id, "resume": resume},
            )
            next_snapshot = load_snapshot(workdir)
            no_progress = (no_progress + 1) if next_snapshot.history_len == previous_history else 0
            previous_level = next_snapshot.level
            previous_history = next_snapshot.history_len
            if no_progress >= args.no_progress_turns:
                print(f"stopping: no progress for {no_progress} turns (timeouts)")
                break
            continue
        if not isinstance(result, dict):
            raise RuntimeError("claude -p did not return JSON")
        session_id = str(result.get("session_id") or session_id)
        expected_average = (sum(costs) + float(result.get("total_cost_usd") or 0.0)) / (
            len(costs) + 1
        )
        projected = total_cost + expected_average * (args.max_turns - turn_offset)
        turn_cost = _record_driver_result(
            workdir,
            turn=turn,
            result=result,
            projected=projected,
        )
        costs.append(turn_cost)
        total_cost += turn_cost
        print(
            f"turn {turn}: cost=${turn_cost:.4f}; total=${total_cost:.4f}; "
            f"projected=${projected:.4f}"
        )

        committed = load_committed_turn(workdir, turn_id)
        if committed is None:
            with EventLog(workdir / "events.jsonl", clock=time.time) as event_log:
                event_log.append(TurnFallback(turn=turn, reason=FALLBACK_REASON))
            previous = None
            surprise = FALLBACK_REASON
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

        if _usage_tokens(result.get("usage")) >= args.context_rollover_tokens:
            session_id = str(uuid.uuid4())
            resume = False
        else:
            resume = True

        sessions = workdir / "sessions" / "sessions.json"
        _atomic_json(
            sessions,
            {"cwd": str(workdir), "sid": session_id, "resume": resume},
        )

        if turn_cost > args.turn_cost_cap:
            print(f"stopping: per-turn cost cap ${args.turn_cost_cap:.2f} exceeded")
            break
        if total_cost > args.run_cost_cap:
            print(f"stopping: run cost cap ${args.run_cost_cap:.2f} exceeded")
            break
        if no_progress >= args.no_progress_turns:
            print(f"stopping: no progress for {no_progress} turns")
            break
        if next_snapshot.state == "WIN" or next_snapshot.history_len >= args.max_actions:
            break

    final_snapshot = load_snapshot(workdir)
    _finish_run(workdir, final_snapshot, start_history_len=start_history_len)
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", default=DEFAULT_GAME)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default=None, help="claude effort level, e.g. max")
    parser.add_argument("--turn-timeout", type=int, default=1200,
                        help="per-turn wall-clock seconds before the claude subprocess is killed")
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--max-actions", type=int, default=3000)
    parser.add_argument("--max-turns", type=int, default=300)
    parser.add_argument("--turn-cost-cap", type=float, default=2.0)
    parser.add_argument("--run-cost-cap", type=float, default=50.0)
    parser.add_argument("--no-progress-turns", type=int, default=5)
    parser.add_argument("--context-rollover-tokens", type=int, default=150_000)
    parser.add_argument(
        "--system-prompt-file",
        type=Path,
        default=DEFAULT_SYSTEM_PROMPT,
        help="method prompt appended to Claude's default system prompt",
    )
    parser.add_argument(
        "--no-system-prompt",
        action="store_true",
        help="disable the standing method prompt for an ablation run",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    args.game = canonical_game_id(args.game)
    if args.no_system_prompt:
        args.system_prompt_file = None
    elif not args.system_prompt_file.is_file():
        parser.error(f"system prompt file not found: {args.system_prompt_file}")
    if args.workdir is None:
        if args.dry_run:
            args.workdir = Path(tempfile.mkdtemp(prefix="schema-bp35-dry-"))
        else:
            args.workdir = Path.home() / f"agent-{args.game.split('-', 1)[0]}"
    if args.max_actions < 1 or args.max_turns < 1:
        parser.error("--max-actions and --max-turns must be positive")
    if args.turn_cost_cap < 0 or args.run_cost_cap < 0:
        parser.error("cost caps must be non-negative")
    if args.no_progress_turns < 1:
        parser.error("--no-progress-turns must be positive")
    # Keep the exact built-in denial list visibly coupled to the proven probe.
    assert DISALLOWED == (
        "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Task,TodoWrite,"
        "NotebookEdit,MultiEdit,BashOutput"
    )
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
