"""Workdir-jailed ``locus`` FastMCP server for one Schema harness turn.

The server owns a fresh in-process gateway. Gateway state survives per-turn MCP
processes through the durable workdir replay timeline and turn ledger.

``run_python`` and ``run_shell`` run in a deny-by-default OS sandbox in addition to
the service's path checks. Unsupported hosts fail closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from typing import Any, TypeVar

import numpy as np
from mcp.server.fastmcp import FastMCP

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "schema_harness"

from .backtest import ALIGNMENT_BACKTEST_SELECTOR
from .events import EventLog, ToolFinished, ToolStarted, TurnCommitted
from .guard import sandbox_exec_argv, shell_command_safe, wrap_python
from .gateway import (
    ExecutionResult,
    PersistentGateway,
    QueuedAction,
    Transition,
    WorldModelPrediction,
)
from .inspectors import (
    describe_actor_affordances,
    describe_grid_diff,
    discover_click_targets,
    pending_actor_affordance_hint,
)
from .model_loader import ModelInterface


_REPO_ROOT = str(Path(__file__).resolve().parents[1])
_PACKAGE_ROOT = Path(__file__).resolve().parent
_REPO_OUTPUT_PATTERN = re.compile(
    re.escape(_REPO_ROOT) + r"(?![A-Za-z0-9._-])"
)
_HARNESS_MANAGED_FILES = frozenset(
    {"events.jsonl", "mcp.json", "method_prompt.md", "run.json"}
)
_HARNESS_MANAGED_DIRS = frozenset({"config", "framework", "runtime", "sessions"})
_HARNESS_PRIVATE_DIRS = frozenset({"config", "sessions"})
_HARNESS_PRIVATE_FILES = _HARNESS_MANAGED_FILES
_HARNESS_PRIVATE_RUNTIME_FILES = frozenset(
    {
        PersistentGateway.TIMELINE_NAME,
        PersistentGateway.LEDGER_NAME,
        PersistentGateway.MODEL_NAME,
        "locus.jsonl",
    }
)
LOCK_MESSAGE = "Already committed this turn — end your turn now."
COMMIT_MESSAGE = "Committed {count} action(s). Stop now — end your turn, do not call more tools."
CROSS_TRANSITION_GATE_MESSAGE = (
    "Cross-transition gate: a newly supported structural context is available. "
    'Call read_history(detail="full") before committing; no action was taken, and '
    "you may retry this turn."
)
MODEL_REPAIR_GATE_MESSAGE = (
    "Model-repair gate: the latest non-RESET transition surprised the live world "
    "model, so another non-RESET action requires a green full-history backtest. "
    "No action was taken; repair the model and retry this turn. Pure RESET queues "
    "remain available."
)
MODEL_VALIDATE_GATE_MESSAGE = (
    "Validate-before-plan gate: multi-action commits require the installed world "
    "model to pass a green full-history backtest at the current history. No action "
    "was taken; repair the model (or commit a single action) and retry this turn. "
    "Pure RESET queues remain available."
)
MODEL_REQUIRED_GATE_MESSAGE = (
    "Validate-before-plan gate: no world model is installed, so commits are limited "
    "to a single action. No action was taken; commit one probe action, or install a "
    "world model and retry."
)
RESET_BOUNDARY_GATE_MESSAGE = (
    "Reset-boundary gate: while a world model is active, RESET must end the queue "
    "because full-history alignment reinitializes model state at RESET. No action "
    "was taken; commit RESET separately, then retry real actions next turn."
)
_MODEL_PATTERN = re.compile(r"world_model_v\d+\.py")
_T = TypeVar("_T")
_STDOUT_REDIRECT_LOCK = threading.RLock()


@contextmanager
def _tool_output_to_stderr():
    """Keep runtime/model logging off stdout, which is the MCP wire."""

    with _STDOUT_REDIRECT_LOCK:
        try:
            stdout_fd = sys.__stdout__.fileno()
            stderr_fd = sys.__stderr__.fileno()
            sys.__stdout__.flush()
            saved_stdout = os.dup(stdout_fd)
        except (AttributeError, OSError, ValueError):
            with redirect_stdout(sys.stderr):
                yield
            return
        try:
            os.dup2(stderr_fd, stdout_fd)
            with redirect_stdout(sys.stderr):
                yield
        finally:
            sys.stderr.flush()
            os.dup2(saved_stdout, stdout_fd)
            os.close(saved_stdout)


class _ModelWorkerTimeout(TimeoutError):
    """A sandboxed model worker exceeded its tool-specific deadline."""


class _FailingWorldModel:
    """Convert worker startup failure into the gateway's nondeterministic-model path."""

    def __init__(self, error: str) -> None:
        self.error = error

    def __call__(
        self,
        _grid: list[list[int]],
        _action: int,
        _x: int | None = None,
        _y: int | None = None,
    ) -> WorldModelPrediction:
        raise RuntimeError(self.error)


class _SandboxedModelSession:
    """Synchronous JSON-RPC facade over one persistent sandboxed model process."""

    def __init__(self, process: subprocess.Popen[str], timeout: float) -> None:
        self.process = process
        self.timeout = timeout

    def _receive(self) -> Mapping[str, Any]:
        stdout = self.process.stdout
        if stdout is None:
            raise RuntimeError("sandboxed model worker has no stdout pipe")
        ready, _, _ = select.select([stdout], [], [], self.timeout)
        if not ready:
            self.close()
            raise _ModelWorkerTimeout
        line = stdout.readline()
        if not line:
            raise RuntimeError("sandboxed model worker exited without a response")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("sandboxed model worker returned invalid JSON") from exc
        if not isinstance(response, Mapping):
            raise RuntimeError("sandboxed model worker response must be an object")
        return response

    def initialize(self, request: Mapping[str, Any]) -> None:
        self._send(request)
        response = self._receive()
        if response.get("ok") is not True or response.get("ready") is not True:
            raise RuntimeError(str(response.get("error", "model worker failed to initialize")))

    def _send(self, request: Mapping[str, Any]) -> None:
        stdin = self.process.stdin
        if stdin is None:
            raise RuntimeError("sandboxed model worker has no stdin pipe")
        try:
            stdin.write(json.dumps(request, ensure_ascii=False, allow_nan=False) + "\n")
            stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RuntimeError("sandboxed model worker pipe closed") from exc

    def __call__(
        self,
        grid: list[list[int]],
        action: int,
        x: int | None = None,
        y: int | None = None,
    ) -> WorldModelPrediction:
        self._send({"grid": grid, "action": action, "x": x, "y": y})
        response = self._receive()
        if response.get("ok") is not True:
            raise RuntimeError(str(response.get("error", "model prediction failed")))
        prediction = response.get("result")
        if not isinstance(prediction, Mapping):
            raise RuntimeError("sandboxed model returned invalid prediction data")
        return WorldModelPrediction(
            grid=prediction["grid"],
            level_up=prediction["level_up"],
            dead=prediction["dead"],
            win=prediction["win"],
        )

    def close(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def _seconds_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


class LocusService:
    """Testable implementation behind the fourteen MCP tool functions."""

    def __init__(
        self,
        workdir: str | os.PathLike[str],
        game: str,
        turn_id: str,
        *,
        turn: int = 0,
        max_actions: int = 3000,
        events_path: str | os.PathLike[str] | None = None,
        event_log: EventLog | None = None,
        gateway: PersistentGateway | None = None,
        arcade: Any | None = None,
        process_timeout: float = 30,
        bfs_timeout: float = 600,
        backtest_timeout: float = 120,
        clock: Callable[[], float] = time.time,
        debug_log: str | os.PathLike[str] | None = None,
        experimental_tooling: bool | None = None,
    ) -> None:
        self.workdir = Path(workdir).resolve()
        if Path(_REPO_ROOT).is_relative_to(self.workdir):
            raise ValueError("workdir must not contain the Schema harness repository")
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.game = game
        self.turn_id = turn_id
        self.turn = turn
        self.process_timeout = process_timeout
        self.bfs_timeout = bfs_timeout
        self.backtest_timeout = backtest_timeout
        self.debug_log = Path(debug_log) if debug_log else None
        if experimental_tooling is None:
            configured = os.environ.get("SCHEMA_EXPERIMENTAL_TOOLING", "false")
            if configured not in {"true", "false"}:
                raise ValueError(
                    "SCHEMA_EXPERIMENTAL_TOOLING must be exactly 'true' or 'false'"
                )
            experimental_tooling = configured == "true"
        self.experimental_tooling = experimental_tooling
        self._owns_event_log = event_log is None and events_path is not None
        self.event_log = event_log
        if self.event_log is None and events_path is not None:
            self.event_log = EventLog(events_path, clock=clock)
        if gateway is None:
            # stdout is the MCP transport. The ARC runtime and arbitrary world
            # models may log while loading, so route that output to stderr.
            with _tool_output_to_stderr():
                self.gateway = PersistentGateway(
                    game,
                    self.workdir,
                    self.event_log,
                    arcade=arcade,
                    max_actions=max_actions,
                )
        else:
            self.gateway = gateway
        self._committed = self.gateway.is_turn_complete(turn_id)
        self._full_history_read = False
        self._backtest_cache: dict[tuple[str, int], str | None] = {}
        self.last_result: ExecutionResult | None = None

    def close(self) -> None:
        if self._owns_event_log and self.event_log is not None:
            self.event_log.close()

    def __enter__(self) -> "LocusService":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _debug(self, name: str, args: Mapping[str, Any], output: str, rejected: bool) -> None:
        if self.debug_log is None:
            return
        self.debug_log.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.time(),
            "tool": name,
            "args": args,
            "output": output,
            "rejected": rejected,
        }
        with self.debug_log.open("a", encoding="utf-8", newline="") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()

    def _started(self, name: str, args: Mapping[str, Any]) -> str:
        call_id = f"id-{uuid.uuid4().hex[:16]}"
        if self.event_log is not None:
            self.event_log.append(
                ToolStarted(turn=self.turn, call_id=call_id, name=name, args=dict(args))
            )
        return call_id

    def _finished(
        self,
        call_id: str,
        name: str,
        args: Mapping[str, Any],
        output: str,
        *,
        is_error: bool = False,
        rejected: bool = False,
    ) -> str:
        if self.event_log is not None:
            self.event_log.append(
                ToolFinished(
                    turn=self.turn,
                    call_id=call_id,
                    name=name,
                    output=output,
                    is_error=is_error,
                )
            )
        self._debug(name, args, output, rejected)
        return output

    def _invoke(
        self,
        name: str,
        args: Mapping[str, Any],
        operation: Callable[[], _T],
    ) -> _T | str:
        call_id = self._started(name, args)
        if self._committed:
            return self._finished(
                call_id,
                name,
                args,
                LOCK_MESSAGE,
                rejected=True,
            )
        try:
            with _tool_output_to_stderr():
                output = operation()
        except Exception as exc:
            self._finished(
                call_id,
                name,
                args,
                f"ERROR: {type(exc).__name__}: {exc}",
                is_error=True,
            )
            raise
        return self._finished(call_id, name, args, str(output))

    def _resolve(self, raw_path: str | os.PathLike[str], *, allow_root: bool = False) -> Path:
        if not isinstance(raw_path, (str, os.PathLike)):
            raise TypeError("path must be a string")
        supplied = Path(raw_path).expanduser()
        candidate = supplied if supplied.is_absolute() else self.workdir / supplied
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.workdir):
            raise ValueError(f"path escapes workdir: {raw_path}")
        if resolved == self.workdir and not allow_root:
            raise ValueError("the workdir root is not a file target")
        return resolved

    def _display(self, path: Path) -> str:
        return path.relative_to(self.workdir).as_posix() or "."

    def _require_agent_writable(self, path: Path) -> None:
        relative = path.relative_to(self.workdir)
        managed = (
            relative.as_posix() in _HARNESS_MANAGED_FILES
            or bool(relative.parts and relative.parts[0] in _HARNESS_MANAGED_DIRS)
        )
        if not managed and path.exists():
            managed = any(
                protected.exists() and path.samefile(protected)
                for protected in self._managed_write_files()
            )
        if managed:
            raise ValueError(
                f"harness-managed path is read-only: {relative.as_posix()}"
            )

    def _require_agent_readable(self, path: Path) -> None:
        relative = path.relative_to(self.workdir)
        private = (
            relative.as_posix() in _HARNESS_PRIVATE_FILES
            or bool(relative.parts and relative.parts[0] in _HARNESS_PRIVATE_DIRS)
            or bool(
                len(relative.parts) == 2
                and relative.parts[0] == "runtime"
                and relative.parts[1] in _HARNESS_PRIVATE_RUNTIME_FILES
            )
        )
        if not private and path.exists():
            private = any(
                protected.exists() and path.samefile(protected)
                for protected in self._private_read_files()
            )
        if private:
            raise ValueError(f"harness-private path is not readable: {relative.as_posix()}")

    def _dynamic_log_files(self) -> set[Path]:
        files: set[Path] = set()
        if self.event_log is not None:
            files.add(self.event_log.path.resolve())
        if self.debug_log is not None:
            files.add(
                (
                    self.debug_log
                    if self.debug_log.is_absolute()
                    else self.workdir / self.debug_log
                ).resolve()
            )
        return files

    def _files_below(self, directory_names: Sequence[str]) -> set[Path]:
        files: set[Path] = set()
        for name in directory_names:
            directory = self.workdir / name
            if directory.is_dir():
                files.update(
                    candidate
                    for candidate in directory.rglob("*")
                    if candidate.is_file()
                )
        return files

    def _hardlink_aliases(self, protected_files: set[Path]) -> set[Path]:
        """Find workdir names sharing a protected inode on resumed runs."""

        protected_inodes: set[tuple[int, int]] = set()
        for protected in protected_files:
            try:
                stat = protected.stat()
            except OSError:
                continue
            if stat.st_nlink > 1:
                protected_inodes.add((stat.st_dev, stat.st_ino))
        if not protected_inodes:
            return set()

        aliases: set[Path] = set()
        for candidate in self.workdir.rglob("*"):
            try:
                stat = candidate.stat()
            except OSError:
                continue
            if candidate.is_file() and (stat.st_dev, stat.st_ino) in protected_inodes:
                resolved = candidate.resolve()
                if resolved not in protected_files:
                    aliases.add(resolved)
        return aliases

    def _managed_write_files(self) -> tuple[Path, ...]:
        files = {self.workdir / name for name in _HARNESS_MANAGED_FILES}
        files.update(self._dynamic_log_files())
        protected = files | self._files_below(tuple(_HARNESS_MANAGED_DIRS))
        files.update(self._hardlink_aliases(protected))
        return tuple(sorted(files))

    def _managed_write_denials(self) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
        directories = tuple(self.workdir / name for name in _HARNESS_MANAGED_DIRS)
        return directories, self._managed_write_files()

    def _private_read_files(self) -> tuple[Path, ...]:
        files = {self.workdir / name for name in _HARNESS_PRIVATE_FILES}
        files.update(
            self.workdir / "runtime" / name
            for name in _HARNESS_PRIVATE_RUNTIME_FILES
        )
        files.update(self._dynamic_log_files())
        protected = files | self._files_below(tuple(_HARNESS_PRIVATE_DIRS))
        files.update(self._hardlink_aliases(protected))
        return tuple(sorted(files))

    def _managed_read_denials(self) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
        directories = tuple(self.workdir / name for name in _HARNESS_PRIVATE_DIRS)
        return directories, self._private_read_files()

    @staticmethod
    def _install_suffix(interface: ModelInterface) -> str:
        bfs = (
            "is_goal available (BFS enabled)"
            if interface.has_is_goal
            else "no is_goal (BFS disabled)"
        )
        return (
            f" Installed as the live world model [{interface.description}]; {bfs}. "
            "Run run_backtest to check it against history."
        )

    def _history_payload(self) -> dict[str, Any]:
        history = self.gateway.history()
        return {
            "initial_turn": history["initial_turn"],
            "actions": [transition._asdict() for transition in self.gateway.timeline],
        }

    def _model_worker_command(
        self,
        *arguments: str,
    ) -> tuple[list[str], dict[str, str]]:
        scratch = self.workdir / "runtime" / "model_scratch"
        process_tmp = scratch / "tmp"
        matplotlib = scratch / "matplotlib"
        process_tmp.mkdir(parents=True, exist_ok=True)
        matplotlib.mkdir(parents=True, exist_ok=True)
        denied_read_directories, denied_read_files = self._managed_read_denials()
        command, reason = sandbox_exec_argv(
            [sys.executable, "-m", "schema_harness.model_worker", *arguments],
            workdir=self.workdir,
            read_paths=(sys.prefix, sys.base_prefix, _PACKAGE_ROOT),
            read_literals=(_REPO_ROOT,),
            deny_read_paths=denied_read_directories,
            deny_read_literals=denied_read_files,
            write_paths=(scratch,),
            allow_subprocesses=False,
            allow_read_metadata=True,
        )
        if command is None:
            raise RuntimeError(reason)
        environment = self._subprocess_environment()
        environment.update(
            HOME=str(scratch),
            TMPDIR=str(process_tmp),
            MPLCONFIGDIR=str(matplotlib),
            PYTHONPATH=_REPO_ROOT,
        )
        return command, environment

    def _run_model_worker(
        self,
        operation: str,
        model_path: Path,
        payload: Mapping[str, Any] | None = None,
        *,
        timeout: float,
    ) -> Any:
        request = dict(payload or {})
        request.update(operation=operation, model_path=str(model_path))
        command, environment = self._model_worker_command()
        try:
            completed = subprocess.run(
                command,
                cwd=self.workdir,
                input=json.dumps(request, ensure_ascii=False, allow_nan=False),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise _ModelWorkerTimeout from exc
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            detail = (completed.stderr or completed.stdout)[-2000:].strip()
            raise RuntimeError(
                "sandboxed model worker returned an invalid response"
                + (f": {detail}" if detail else "")
            ) from exc
        if not isinstance(response, Mapping) or response.get("ok") is not True:
            error = response.get("error", "unknown model worker failure")
            raise RuntimeError(f"sandboxed model worker failed: {error}")
        if completed.returncode != 0:
            raise RuntimeError(
                f"sandboxed model worker exited with status {completed.returncode}"
            )
        return response.get("result")

    def _probe_model(self, path: Path) -> ModelInterface:
        result = self._run_model_worker(
            "probe",
            path,
            timeout=self.process_timeout,
        )
        if not isinstance(result, Mapping):
            raise RuntimeError("sandboxed model probe returned invalid interface data")
        return ModelInterface(
            kind=result["kind"],
            entrypoint=result["entrypoint"],
            has_is_goal=result["has_is_goal"],
        )

    def _atomic_write(self, path: Path, content: str) -> ModelInterface | None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8", newline="")
            interface: ModelInterface | None = None
            if path.parent == self.workdir and _MODEL_PATTERN.fullmatch(path.name):
                interface = self._probe_model(temporary)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        if interface is not None:
            self.gateway.set_live_model(path)
        return interface

    def commit_actions(
        self,
        actions: list[dict[str, int]],
        reason: str,
        suggestion: str | None = None,
    ) -> str:
        """Commit an action queue once and end the current agent turn."""

        args = {"actions": actions, "reason": reason}
        if suggestion is not None:
            args["suggestion"] = suggestion
        call_id = self._started("commit_actions", args)
        if self._committed:
            return self._finished(
                call_id,
                "commit_actions",
                args,
                LOCK_MESSAGE,
                rejected=True,
            )
        queue = tuple(QueuedAction.parse(action) for action in actions)
        has_non_reset = any(action.action != 0 for action in queue)
        gate_messages: list[str] = []
        repair_gate_appended = False
        if self.experimental_tooling:
            if queue and queue[0].action != 0 and not self._full_history_read:
                try:
                    new_topology = self._new_affordance_topology()
                except Exception:
                    # Inspector failure must never make real actions impossible.
                    new_topology = None
                if new_topology is not None:
                    gate_messages.append(CROSS_TRANSITION_GATE_MESSAGE)
            needs_model_repair = (
                has_non_reset
                and self.gateway.latest_completed_turn_needs_model_repair()
            )
            if needs_model_repair:
                repair_report = self._model_repair_report()
                if repair_report is not None:
                    gate_messages.append(MODEL_REPAIR_GATE_MESSAGE + "\n" + repair_report)
                    repair_gate_appended = True
            if self.gateway.live_model_path() is not None:
                reset_seen = False
                for action in queue:
                    reset_seen = reset_seen or action.action == 0
                    if reset_seen and action.action != 0:
                        gate_messages.append(RESET_BOUNDARY_GATE_MESSAGE)
                        break
        if len(queue) > 1 and has_non_reset:
            if self.gateway.live_model_path() is None:
                gate_messages.append(MODEL_REQUIRED_GATE_MESSAGE)
            elif not repair_gate_appended:
                validation_report = self._validated_model_report()
                if validation_report is not None:
                    gate_messages.append(
                        MODEL_VALIDATE_GATE_MESSAGE + "\n" + validation_report
                    )
        if gate_messages:
            return self._finished(
                call_id,
                "commit_actions",
                args,
                "\n".join(gate_messages),
                rejected=True,
            )
        output = COMMIT_MESSAGE.format(count=len(queue))
        self._committed = True
        # Match the released ordering: the tool call finishes before the durable
        # committed plan and resulting action events are emitted.
        self._finished(call_id, "commit_actions", args, output)
        if self.event_log is not None:
            self.event_log.append(
                TurnCommitted(
                    turn=self.turn,
                    plan=[action.released() for action in queue],
                    reason=reason,
                )
            )
        with _tool_output_to_stderr():
            live_model = self._model_session()
            try:
                self.last_result = self.gateway.commit(
                    self.turn_id,
                    queue,
                    reason,
                    suggestion,
                    live_model=live_model,
                    turn=self.turn,
                )
            finally:
                if isinstance(live_model, _SandboxedModelSession):
                    live_model.close()
        return output

    def _model_path(self) -> Path:
        path = self.gateway.live_model_path()
        if path is None:
            raise RuntimeError("no world model installed")
        return path

    def _backtest_output(
        self,
        selector: Any,
        model_path: Path | None = None,
    ) -> str:
        result = self._run_model_worker(
            "backtest",
            self._model_path() if model_path is None else model_path,
            {"history": self._history_payload(), "selector": selector},
            timeout=self.backtest_timeout,
        )
        if not isinstance(result, str):
            raise RuntimeError("sandboxed backtest returned invalid output")
        return result

    def _backtest_failure_report(self, exc: Exception) -> str:
        if isinstance(exc, _ModelWorkerTimeout):
            return (
                "ERROR: required full-history backtest timed out after "
                f"{_seconds_text(self.backtest_timeout)}s."
            )
        return (
            "ERROR: required full-history backtest failed: "
            f"{type(exc).__name__}: {exc}"
        )

    def _full_history_backtest_report(
        self,
        model_path: Path,
    ) -> tuple[str | None, bool]:
        try:
            with _tool_output_to_stderr():
                for selector, label in (
                    ("all", "[all transitions]"),
                    (ALIGNMENT_BACKTEST_SELECTOR, "[full-history alignment]"),
                ):
                    output = self._backtest_output(selector, model_path)
                    green = (
                        output.startswith(f"backtest {label}: ")
                        and "; 0 mismatch(es), " in output
                        and "Model predicts ALL checkable transitions" in output
                    )
                    if not green:
                        return output, True
        except Exception as exc:
            return self._backtest_failure_report(exc), False
        return None, True

    def _model_repair_report(self) -> str | None:
        try:
            model_path = self._model_path()
        except Exception as exc:
            return self._backtest_failure_report(exc)
        report, _ = self._full_history_backtest_report(model_path)
        return report

    def _validated_model_report(self) -> str | None:
        model_path = self.gateway.live_model_path()
        if model_path is None:
            raise RuntimeError("no world model installed")
        try:
            model_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()
        except Exception as exc:
            return self._backtest_failure_report(exc)
        key = (model_hash, len(self.gateway.timeline))
        if key in self._backtest_cache:
            return self._backtest_cache[key]

        report, definitive = self._full_history_backtest_report(model_path)
        if definitive:
            self._backtest_cache[key] = report
        return report

    def _model_session(
        self,
    ) -> _SandboxedModelSession | _FailingWorldModel | None:
        path = self.gateway.live_model_path()
        if path is None:
            return None
        session: _SandboxedModelSession | None = None
        try:
            command, environment = self._model_worker_command("serve")
            process = subprocess.Popen(
                command,
                cwd=self.workdir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=environment,
            )
            session = _SandboxedModelSession(process, self.process_timeout)
            session.initialize(
                {"model_path": str(path), "history": self._history_payload()}
            )
            return session
        except Exception as exc:
            if session is not None:
                session.close()
            return _FailingWorldModel(f"sandboxed model worker failed: {exc}")

    def run_backtest(
        self,
        start: int | None = None,
        indices: list[int] | None = None,
        max_details: int | None = None,
    ) -> str:
        """Backtest the installed model over all, ranged, or selected history."""

        args = {key: value for key, value in {
            "start": start,
            "indices": indices,
            "max_details": max_details,
        }.items() if value is not None}

        def operation() -> str:
            timeline = self.gateway.timeline
            selector: Any = "all"
            if indices is not None:
                selector = indices
            elif start is not None:
                normalized = start if start >= 0 else len(timeline) + start
                if not 0 <= normalized <= len(timeline):
                    raise ValueError("start is out of range")
                selector = (
                    []
                    if normalized == len(timeline)
                    else f"[range #{normalized}..#{len(timeline) - 1}]"
                )
            if max_details is not None and (type(max_details) is not int or max_details < 0):
                raise ValueError("max_details must be a non-negative integer")
            output = self._backtest_output(selector)
            artifact = self.workdir / "runtime" / "artifact"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(output + "\n", encoding="utf-8", newline="")
            return output

        return str(self._invoke("run_backtest", args, operation))

    def run_bfs(
        self,
        target: str,
        clicks: list[list[int]] = [],
        *,
        max_depth: int,
        max_nodes: int | None = None,
    ) -> str:
        """Run bounded model BFS with caller-supplied click targets."""

        args: dict[str, Any] = {
            "target": target,
            "clicks": clicks,
            "max_depth": max_depth,
        }
        if max_nodes is not None:
            args["max_nodes"] = max_nodes

        def operation() -> str:
            model_path = self._model_path()
            validation_report = self._validated_model_report()
            if validation_report is not None:
                return MODEL_VALIDATE_GATE_MESSAGE + "\n" + validation_report
            goals = {
                "advance": ("level_up", "win"),
                "level_up": ("level_up",),
                "win": ("win",),
                "death": ("dead",),
                "dead": ("dead",),
            }.get(target)
            if goals is None:
                raise ValueError("target must be one of: advance, level_up, win, dead")
            legal = tuple(
                action for action in self.gateway.gateway.legal_actions if action not in (0, 6)
            )
            nodes = 1_000_000 if max_nodes is None else max_nodes
            try:
                result = self._run_model_worker(
                    "bfs",
                    model_path,
                    {
                        "history": self._history_payload(),
                        "actions": legal,
                        "click_targets": clicks,
                        "max_nodes": nodes,
                        "max_depth": max_depth,
                        "goal": goals,
                    },
                    timeout=self.bfs_timeout,
                )
            except _ModelWorkerTimeout:
                return (
                    "ERROR: run_bfs timed out after "
                    f"{_seconds_text(self.bfs_timeout)}s."
                )
            if not isinstance(result, Mapping):
                raise RuntimeError("sandboxed BFS returned invalid output")
            if result.get("error") == "no_is_goal":
                raise RuntimeError("installed world model has no is_goal (BFS disabled)")
            output = result.get("output")
            if not isinstance(output, str):
                raise RuntimeError("sandboxed BFS returned invalid output")
            return output

        return str(self._invoke("run_bfs", args, operation))

    def _affordance_context(
        self,
    ) -> tuple[list[list[int]], list[tuple[int, list[list[int]]]], int]:
        timeline = self.gateway.timeline
        level_initial = self.gateway.gateway.initial_grid
        level_start = 0
        for position, item in enumerate(timeline):
            if item.action == 0 or item.level_up:
                level_initial = item.grid
                level_start = position + 1
        observations = [(item.action, item.grid) for item in timeline[level_start:]]
        return level_initial, observations, level_start

    def _affordance_advisory(self) -> str | None:
        level_initial, observations, _ = self._affordance_context()
        topology = describe_actor_affordances(level_initial, observations)
        if topology is not None:
            return topology
        return pending_actor_affordance_hint(level_initial, observations)

    def _new_affordance_topology(self) -> str | None:
        level_initial, observations, level_start = self._affordance_context()
        turn_start = self.gateway.latest_completed_turn_start()
        if not observations or turn_start is None:
            return None
        topology = describe_actor_affordances(level_initial, observations)
        if topology is None:
            return None
        prior_length = max(0, turn_start - level_start)
        prior = describe_actor_affordances(
            level_initial,
            observations[:prior_length],
        )
        return topology if prior is None else None

    def read_history(
        self,
        indices: list[int] | None = None,
        detail: str = "full",
    ) -> str:
        """Read history; full detail includes grid diffs and current click targets."""

        args: dict[str, Any] = {"detail": detail}
        if indices is not None:
            args["indices"] = indices

        def operation() -> str:
            timeline = self.gateway.timeline
            if detail not in {"full", "summary"}:
                raise ValueError("detail must be 'full' or 'summary'")
            selected: list[Transition]
            if indices is None:
                selected = list(timeline)
            else:
                normalized: list[int] = []
                for index in indices:
                    if type(index) is not int:
                        raise ValueError("history indices must be integers")
                    value = index if index >= 0 else len(timeline) + index
                    if not 0 <= value < len(timeline):
                        raise ValueError(f"history index {index} is out of range")
                    normalized.append(value)
                selected = [timeline[index] for index in normalized]
            counts = Counter(transition.action for transition in timeline)
            by_action = "{" + ", ".join(
                f"{action}: {counts[action]}" for action in sorted(counts)
            ) + "}"
            summary = (
                f"{len(timeline)} transitions total. Summary: "
                f"level_ups={sum(item.level_up for item in timeline)} "
                f"deaths={sum(item.dead for item in timeline)} "
                f"wins={sum(item.win for item in timeline)} "
                f"resets(action0)={counts[0]} clicks(action6)={counts[6]}; "
                f"by-action={by_action}; "
                f"max_level={max((item.level for item in timeline), default=0)}; "
            )
            shown = (
                f"[{selected[0].step_index}, {selected[-1].step_index}]"
                if selected
                else "[]"
            )
            summary += f"showing indices {shown} -> {len(selected)} steps"
            if detail == "summary":
                return summary + "."
            details: list[str] = []
            initial = self.gateway.gateway.initial_grid
            for item in selected:
                before = initial if item.step_index == 0 else timeline[item.step_index - 1].grid
                changed = int(
                    np.count_nonzero(
                        np.asarray(before, dtype=int) != np.asarray(item.grid, dtype=int)
                    )
                )
                action = (
                    f"6({item.x},{item.y})" if item.action == 6 else str(item.action)
                )
                details.append(
                    f"#{item.step_index} action={action}; {changed} cells changed; "
                    f"state={item.state}; level={item.level}; "
                    f"level_up={item.level_up} dead={item.dead} win={item.win}"
                )
            result = summary + "; detail=full: " + (" | ".join(details) or "(none)")
            if not self.experimental_tooling:
                return result
            inspected = selected[-8:]
            inspector_records: list[str] = []
            for item in inspected:
                before = initial if item.step_index == 0 else timeline[item.step_index - 1].grid
                inspector_records.append(
                    f"#{item.step_index} " + describe_grid_diff(before, item.grid)
                )
            appendix = (
                f"Inspector: showing {len(inspected)}/{len(selected)} selected transition(s)"
            )
            if inspector_records:
                appendix += "\n" + "\n".join(inspector_records)
            affordance_advisory = self._affordance_advisory()
            if affordance_advisory is not None:
                appendix += "\n" + affordance_advisory
            if timeline and self.gateway.live_model_path() is None:
                unit = "transition" if len(timeline) == 1 else "transitions"
                appendix += (
                    f"\nModel gate: NONE after {len(timeline)} {unit}. Write a minimal "
                    "world_model_v1.py now and run run_backtest; without an installed model, "
                    "commit_actions can execute only one probe. Model unknown actions "
                    "conservatively instead of waiting to solve every control."
                )
            if 6 in self.gateway.gateway.legal_actions:
                targets = discover_click_targets(self.gateway.gateway.grid)
                appendix += (
                    f"\nCurrent-grid click target proposals ({len(targets)}; "
                    "component-based, unverified; pass selected coordinates as "
                    "run_bfs clicks): "
                    + json.dumps(targets, separators=(",", ":"))
                )
            return result + "\n" + appendix

        output = self._invoke("read_history", args, operation)
        if detail == "full" and output != LOCK_MESSAGE:
            self._full_history_read = True
        return str(output)

    def _run_process(
        self,
        command: Sequence[str],
        *,
        display: str,
        timeout: float,
        environment: Mapping[str, str] | None = None,
    ) -> str:
        def redact(text: str) -> str:
            return _REPO_OUTPUT_PATTERN.sub("<harness-repo>", text)

        started = time.monotonic()
        try:
            completed = subprocess.run(
                list(command),
                cwd=self.workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=dict(environment) if environment is not None else None,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            partial = ""
            if exc.stdout:
                partial += exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout
            if exc.stderr:
                partial += exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr
            return (
                f"ERROR: timed out after {_seconds_text(timeout)}s — process killed. "
                f"Partial output below.\n{redact(partial)}"
            )
        elapsed = time.monotonic() - started
        output = (
            f"$ {display}\nexit={completed.returncode} in {elapsed:.2f}s"
            f"\n--- stdout ---\n{redact(completed.stdout)}"
        )
        if completed.stderr:
            output += f"\n--- stderr ---\n{redact(completed.stderr)}"
        return output

    def _subprocess_environment(self) -> dict[str, str]:
        """Return a minimal environment whose writable caches stay in the workdir."""

        process_tmp = self.workdir / ".agent_scratch" / "process_tmp"
        matplotlib = self.workdir / ".agent_scratch" / "matplotlib"
        process_home = self.workdir / ".agent_scratch" / "home"
        process_tmp.mkdir(parents=True, exist_ok=True)
        matplotlib.mkdir(parents=True, exist_ok=True)
        if not process_home.resolve(strict=False).is_relative_to(self.workdir):
            raise RuntimeError("agent process HOME escapes workdir")
        process_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        if process_home.is_symlink():
            raise RuntimeError("agent process HOME must not be a symlink")
        process_home.chmod(0o700)
        return {
            "HOME": str(process_home),
            "PATH": "/usr/bin:/bin",
            "TMPDIR": str(process_tmp),
            "LANG": "C",
            "LC_ALL": "C",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "MPLCONFIGDIR": str(matplotlib),
        }

    def run_python(self, code: str, timeout: float | None = None) -> str:
        """Run inline Python inside the workdir and deny external reads and network."""

        args: dict[str, Any] = {"code": code}
        if timeout is not None:
            args["timeout"] = timeout
        duration = self.process_timeout if timeout is None else timeout
        guarded = wrap_python(code, self.workdir)
        denied_directories, denied_files = self._managed_write_denials()
        denied_read_directories, denied_read_files = self._managed_read_denials()
        command, reason = sandbox_exec_argv(
            [sys.executable, "-c", guarded],
            workdir=self.workdir,
            read_paths=(sys.prefix, sys.base_prefix),
            deny_read_paths=denied_read_directories,
            deny_read_literals=denied_read_files,
            deny_write_paths=denied_directories,
            deny_write_literals=denied_files,
            allow_read_metadata=True,
        )
        if command is None:
            return str(self._invoke("run_python", args, lambda: f"ERROR: {reason}"))
        return str(self._invoke(
            "run_python",
            args,
            lambda: self._run_process(
                command,
                display="python -c <inline>",
                timeout=duration,
                environment=self._subprocess_environment(),
            ),
        ))

    def run_shell(
        self,
        command: str | None = None,
        cmd: str | None = None,
        timeout: float | None = None,
    ) -> str:
        """Run a shell command inside the workdir and deny external reads and network."""

        shell_command = command if command is not None else cmd
        if shell_command is None:
            raise ValueError("command is required")
        if command is not None and cmd is not None and command != cmd:
            raise ValueError("command and cmd disagree")
        args: dict[str, Any] = {"command": shell_command}
        if timeout is not None:
            args["timeout"] = timeout
        duration = self.process_timeout if timeout is None else timeout
        # Anti-cheat: reject commands referencing paths outside the workdir / game source.
        safe, reason = shell_command_safe(shell_command, self.workdir, _REPO_ROOT)
        if not safe:
            return self._invoke("run_shell", args, lambda: f"ERROR: {reason}")
        denied_directories, denied_files = self._managed_write_denials()
        denied_read_directories, denied_read_files = self._managed_read_denials()
        sandboxed, reason = sandbox_exec_argv(
            ["/bin/sh", "-c", shell_command],
            workdir=self.workdir,
            deny_read_paths=denied_read_directories,
            deny_read_literals=denied_read_files,
            deny_write_paths=denied_directories,
            deny_write_literals=denied_files,
        )
        if sandboxed is None:
            return str(self._invoke("run_shell", args, lambda: f"ERROR: {reason}"))
        return str(self._invoke(
            "run_shell",
            args,
            lambda: self._run_process(
                sandboxed,
                display=shell_command,
                timeout=duration,
                environment=self._subprocess_environment(),
            ),
        ))

    def write_file(self, path: str, content: str) -> str:
        """Write a UTF-8 workdir file, installing world_model_v<N>.py files."""

        args = {"path": path, "content": content}

        def operation() -> str:
            target = self._resolve(path)
            self._require_agent_writable(target)
            if target.exists() and target.is_dir():
                raise IsADirectoryError(target)
            interface = self._atomic_write(target, content)
            result = f"OK: wrote {len(content.encode('utf-8'))} bytes to {self._display(target)}."
            return result + (self._install_suffix(interface) if interface else "")

        return str(self._invoke("write_file", args, operation))

    def edit_file(self, path: str, old_string: str, new_string: str) -> str:
        """Replace every exact occurrence in a UTF-8 workdir file."""

        args = {"path": path, "old_string": old_string, "new_string": new_string}

        def operation() -> str:
            target = self._resolve(path)
            self._require_agent_writable(target)
            self._require_agent_readable(target)
            content = target.read_text(encoding="utf-8")
            count = content.count(old_string)
            if count == 0:
                raise ValueError("old_string was not found")
            interface = self._atomic_write(target, content.replace(old_string, new_string))
            result = f"OK: replaced {count} occurrence(s) in {self._display(target)}."
            return result + (self._install_suffix(interface) if interface else "")

        return str(self._invoke("edit_file", args, operation))

    def read_file(
        self,
        path: str,
        offset: int | None = None,
        limit: int | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        """Read numbered UTF-8 lines from a workdir file."""

        args = {key: value for key, value in {
            "path": path,
            "offset": offset,
            "limit": limit,
            "start_line": start_line,
            "end_line": end_line,
        }.items() if value is not None}

        def operation() -> str:
            target = self._resolve(path)
            self._require_agent_readable(target)
            lines = target.read_text(encoding="utf-8").splitlines()
            if start_line is not None or end_line is not None:
                first = 1 if start_line is None else start_line
                last = len(lines) if end_line is None else end_line
            else:
                first = 1 + (0 if offset is None else offset)
                last = len(lines) if limit is None else first + limit - 1
            if first < 1 or last < first - 1:
                raise ValueError("invalid line range")
            selected = lines[first - 1:last]
            shown_last = first + len(selected) - 1
            header = (
                f"{self._display(target)} ({len(lines)} lines, "
                f"showing {first}-{max(first - 1, shown_last)}):"
            )
            body = "\n".join(
                f"{number}\t{text}"
                for number, text in enumerate(selected, start=first)
            )
            return header + ("\n" + body if body else "")

        return str(self._invoke("read_file", args, operation))

    def grep(self, pattern: str, path: str = ".") -> str:
        """Regex-search text files below a jailed workdir path."""

        args = {"pattern": pattern, "path": path}

        def operation() -> str:
            expression = re.compile(pattern)
            target = self._resolve(path, allow_root=True)
            discovered = [target] if target.is_file() else sorted(target.rglob("*"))
            files: list[Path] = []
            seen: set[Path] = set()
            for item in discovered:
                try:
                    candidate = self._resolve(item)
                    self._require_agent_readable(candidate)
                except ValueError:
                    continue
                if candidate.is_file() and candidate not in seen:
                    files.append(candidate)
                    seen.add(candidate)
            matches: list[str] = []
            for candidate in files:
                try:
                    lines = candidate.read_text(encoding="utf-8").splitlines()
                except (UnicodeDecodeError, OSError):
                    continue
                for number, line in enumerate(lines, start=1):
                    if expression.search(line):
                        matches.append(f"{self._display(candidate)}:{number}: {line}")
                        if len(matches) >= 500:
                            return "\n".join(matches)
            return "\n".join(matches) if matches else "No matches."

        return str(self._invoke("grep", args, operation))

    def find(self, pattern: str = "*", path: str = ".") -> str:
        """List workdir paths matching a glob pattern."""

        args = {"pattern": pattern, "path": path}

        def operation() -> str:
            pattern_path = Path(pattern)
            if pattern_path.is_absolute() or ".." in pattern_path.parts:
                raise ValueError("find pattern must stay below the selected workdir path")
            target = self._resolve(path, allow_root=True)
            self._require_agent_readable(target)
            if target.is_file():
                candidates = [target] if target.match(pattern) else []
            else:
                candidates = []
                for candidate in sorted(target.rglob(pattern)):
                    try:
                        self._require_agent_readable(candidate)
                    except ValueError:
                        continue
                    candidates.append(candidate)
            return "\n".join(self._display(item) for item in candidates) or "No matches."

        return str(self._invoke("find", args, operation))

    def cp(self, source: str, destination: str) -> str:
        """Copy one file within the workdir jail."""

        args = {"source": source, "destination": destination}

        def operation() -> str:
            src = self._resolve(source)
            dst = self._resolve(destination)
            self._require_agent_readable(src)
            self._require_agent_writable(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            return f"OK: copied {self._display(src)} to {self._display(dst)}."

        return str(self._invoke("cp", args, operation))

    def mv(self, source: str, destination: str) -> str:
        """Move one path within the workdir jail."""

        args = {"source": source, "destination": destination}

        def operation() -> str:
            src = self._resolve(source)
            dst = self._resolve(destination)
            self._require_agent_writable(src)
            self._require_agent_writable(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return f"OK: moved {self._display(src)} to {self._display(dst)}."

        return str(self._invoke("mv", args, operation))

    def rm(self, path: str) -> str:
        """Remove one file, symlink, or empty directory within the workdir jail."""

        args = {"path": path}

        def operation() -> str:
            target = self._resolve(path)
            self._require_agent_writable(target)
            display = self._display(target)
            if target.is_dir():
                target.rmdir()
            else:
                target.unlink()
            return f"OK: removed {display}."

        return str(self._invoke("rm", args, operation))


mcp = FastMCP("locus")
_SERVICE: LocusService | None = None


def _service() -> LocusService:
    global _SERVICE

    def _env_float(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    if _SERVICE is None:
        workdir = os.environ.get("LOCUS_WORKDIR")
        game = os.environ.get("LOCUS_GAME")
        turn_id = os.environ.get("LOCUS_TURN_ID")
        if not workdir or not game or not turn_id:
            raise RuntimeError("LOCUS_WORKDIR, LOCUS_GAME, and LOCUS_TURN_ID are required")
        _SERVICE = LocusService(
            workdir,
            game,
            turn_id,
            turn=int(os.environ.get("LOCUS_TURN", "0")),
            max_actions=int(os.environ.get("LOCUS_MAX_ACTIONS", "3000")),
            events_path=os.environ.get("LOCUS_EVENTS"),
            process_timeout=_env_float("LOCUS_PROCESS_TIMEOUT", 30.0),
            bfs_timeout=_env_float("LOCUS_BFS_TIMEOUT", 600.0),
            backtest_timeout=_env_float("LOCUS_BACKTEST_TIMEOUT", 120.0),
            debug_log=os.environ.get("LOCUS_LOG"),
        )
    return _SERVICE


@mcp.tool()
def commit_actions(
    actions: list[dict[str, int]],
    reason: str,
    suggestion: str | None = None,
) -> str:
    """Commit an action queue and end this turn."""

    return _service().commit_actions(actions, reason, suggestion)


@mcp.tool()
def run_backtest(
    start: int | None = None,
    indices: list[int] | None = None,
    max_details: int | None = None,
) -> str:
    """Backtest the live world model against persisted transition history."""

    return _service().run_backtest(start, indices, max_details)


@mcp.tool()
def run_bfs(
    target: str,
    clicks: list[list[int]] = [],
    *,
    max_depth: int,
    max_nodes: int | None = None,
) -> str:
    """Search the model using caller-supplied clicks, such as targets from full history."""

    return _service().run_bfs(target, clicks, max_depth=max_depth, max_nodes=max_nodes)


@mcp.tool()
def read_history(
    indices: list[int] | None = None,
    detail: str = "full",
) -> str:
    """Read transitions; full detail adds grid diffs and current geometric click targets."""

    return _service().read_history(indices, detail)


@mcp.tool()
def run_python(code: str, timeout: float | None = None) -> str:
    """Run inline Python in the workdir; OS sandboxing is deferred after Step 3."""

    return _service().run_python(code, timeout)


@mcp.tool()
def run_shell(
    command: str | None = None,
    cmd: str | None = None,
    timeout: float | None = None,
) -> str:
    """Run a shell command in the workdir; OS sandboxing is deferred after Step 3."""

    return _service().run_shell(command, cmd, timeout)


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Write a jailed workdir file and auto-install world models."""

    return _service().write_file(path, content)


@mcp.tool()
def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Edit a jailed workdir file and auto-install world models."""

    return _service().edit_file(path, old_string, new_string)


@mcp.tool()
def read_file(
    path: str,
    offset: int | None = None,
    limit: int | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Read numbered lines from a jailed workdir file."""

    return _service().read_file(path, offset, limit, start_line, end_line)


@mcp.tool()
def grep(pattern: str, path: str = ".") -> str:
    """Regex-search text below a jailed workdir path."""

    return _service().grep(pattern, path)


@mcp.tool()
def find(pattern: str = "*", path: str = ".") -> str:
    """Find paths below a jailed workdir path."""

    return _service().find(pattern, path)


@mcp.tool()
def cp(source: str, destination: str) -> str:
    """Copy a file inside the workdir jail."""

    return _service().cp(source, destination)


@mcp.tool()
def mv(source: str, destination: str) -> str:
    """Move a path inside the workdir jail."""

    return _service().mv(source, destination)


@mcp.tool()
def rm(path: str) -> str:
    """Remove one path inside the workdir jail."""

    return _service().rm(path)


def main() -> None:
    try:
        mcp.run(transport="stdio")
    finally:
        if _SERVICE is not None:
            _SERVICE.close()


if __name__ == "__main__":
    main()


__all__ = [
    "COMMIT_MESSAGE",
    "CROSS_TRANSITION_GATE_MESSAGE",
    "LOCK_MESSAGE",
    "MODEL_REPAIR_GATE_MESSAGE",
    "MODEL_REQUIRED_GATE_MESSAGE",
    "MODEL_VALIDATE_GATE_MESSAGE",
    "RESET_BOUNDARY_GATE_MESSAGE",
    "LocusService",
    "commit_actions",
    "cp",
    "edit_file",
    "find",
    "grep",
    "main",
    "mcp",
    "mv",
    "read_file",
    "read_history",
    "rm",
    "run_backtest",
    "run_bfs",
    "run_python",
    "run_shell",
    "write_file",
]
