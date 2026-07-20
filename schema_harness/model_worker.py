"""Sandboxed execution worker for agent-authored world models.

The trusted locus process sends JSON requests on stdin and receives one JSON response
on stdout. Latent model state never crosses the process boundary.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

from .backtest import run_backtest
from .bfs import run_bfs
from .model_loader import (
    call_init_state,
    call_predict,
    detect_interface,
    load_model,
    set_current_level,
)


@contextmanager
def _silence_model_output():
    """Keep arbitrary model logging away from the JSON protocol."""

    sys.stdout.flush()
    sys.stderr.flush()
    saved_stdout = os.dup(sys.stdout.fileno())
    saved_stderr = os.dup(sys.stderr.fileno())
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, sys.stdout.fileno())
        os.dup2(null, sys.stderr.fileno())
        yield
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os.dup2(saved_stdout, sys.stdout.fileno())
        os.dup2(saved_stderr, sys.stderr.fileno())
        os.close(null)
        os.close(saved_stdout)
        os.close(saved_stderr)


def _transition_value(transition: Mapping[str, Any], name: str) -> Any:
    if name not in transition:
        raise ValueError(f"history transition has no {name!r}")
    return transition[name]


def _align_model(
    model: ModuleType,
    history: Mapping[str, Any],
) -> tuple[Any, int, list[list[int]]]:
    initial = history["initial_turn"]
    if not isinstance(initial, Mapping):
        raise ValueError("history initial_turn must be an object")
    grid = initial["grid"]
    level = int(initial["level"])
    set_current_level(model, level)
    state = call_init_state(model, grid)

    actions = history["actions"]
    if not isinstance(actions, Sequence):
        raise ValueError("history actions must be a sequence")
    for raw in actions:
        if not isinstance(raw, Mapping):
            raise ValueError("history transitions must be objects")
        action = int(_transition_value(raw, "action"))
        actual_grid = _transition_value(raw, "grid")
        if action == 0:
            level = int(_transition_value(raw, "level"))
            set_current_level(model, level)
            state = call_init_state(model, actual_grid)
            grid = actual_grid
            continue
        set_current_level(model, level)
        _, _, state = call_predict(
            model,
            state,
            grid,
            action,
            raw.get("x"),
            raw.get("y"),
        )
        ingest = getattr(model, "ingest", None)
        if callable(ingest):
            ingested = ingest(state, actual_grid)
            if ingested is not None:
                state = ingested
        grid = actual_grid
        if bool(_transition_value(raw, "level_up")):
            level = int(_transition_value(raw, "level"))
            set_current_level(model, level)
            state = call_init_state(model, grid)

    set_current_level(model, level)
    return state, level, grid


def _probe(model: ModuleType, _request: Mapping[str, Any]) -> dict[str, Any]:
    interface = detect_interface(model)
    return {
        "kind": interface.kind,
        "entrypoint": interface.entrypoint,
        "has_is_goal": interface.has_is_goal,
    }


def _backtest(model: ModuleType, request: Mapping[str, Any]) -> str:
    return str(run_backtest(model, request["history"], selector=request["selector"]))


def _bfs(model: ModuleType, request: Mapping[str, Any]) -> dict[str, str]:
    if not detect_interface(model).has_is_goal:
        return {"error": "no_is_goal"}
    state, _, grid = _align_model(model, request["history"])
    return {
        "output": str(
            run_bfs(
                model,
                state,
                grid,
                actions=request["actions"],
                click_targets=request["click_targets"],
                max_nodes=request["max_nodes"],
                max_depth=request["max_depth"],
                goal=request["goal"],
            )
        )
    }


_OPERATIONS = {
    "probe": _probe,
    "backtest": _backtest,
    "bfs": _bfs,
}


def _write_response(response: Mapping[str, Any]) -> None:
    json.dump(response, sys.stdout, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    sys.stdout.write("\n")
    sys.stdout.flush()


def serve() -> int:
    """Keep one aligned model alive for commit-time request/response prediction."""

    try:
        request = json.loads(sys.stdin.readline())
        if not isinstance(request, dict):
            raise ValueError("worker request must be an object")
        with _silence_model_output():
            model = load_model(Path(request["model_path"]))
            state, level, _ = _align_model(model, request["history"])
        _write_response({"ok": True, "ready": True})
    except BaseException as exc:
        _write_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1

    for line in sys.stdin:
        try:
            request = json.loads(line)
            action = int(request["action"])
            x = request.get("x")
            y = request.get("y")
            with _silence_model_output():
                set_current_level(model, level)
                grid, flags, state = call_predict(
                    model,
                    state,
                    request["grid"],
                    action,
                    x,
                    y,
                )
            _write_response(
                {
                    "ok": True,
                    "result": {
                        "grid": grid,
                        "level_up": flags["level_up"],
                        "dead": flags["dead"],
                        "win": flags["win"],
                    },
                }
            )
        except BaseException as exc:
            _write_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return 1
    return 0


def main() -> int:
    if sys.argv[1:] == ["serve"]:
        return serve()
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ValueError("worker request must be an object")
        operation = request.get("operation")
        handler = _OPERATIONS.get(operation)
        if handler is None:
            raise ValueError(f"unsupported worker operation: {operation!r}")
        model_path = Path(request["model_path"])
        with _silence_model_output():
            model = load_model(model_path)
            result = handler(model, request)
        response = {"ok": True, "result": result}
    except BaseException as exc:
        response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    _write_response(response)
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
