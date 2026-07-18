"""In-process loading and normalized calls for Schema world models."""

from __future__ import annotations

import inspect
import itertools
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

import numpy as np

from .events import Grid


InterfaceKind = Literal["stateful", "stateless"]
Entrypoint = Literal["predict", "step"]
TERMINAL_FLAGS = ("level_up", "dead", "win")
_MODULE_IDS = itertools.count()


@dataclass(frozen=True, slots=True)
class ModelInterface:
    """The callable shape discovered in a loaded world model."""

    kind: InterfaceKind
    entrypoint: Entrypoint
    has_is_goal: bool

    @property
    def stateful(self) -> bool:
        return self.kind == "stateful"

    @property
    def shape(self) -> InterfaceKind:
        return self.kind

    @property
    def name(self) -> Entrypoint:
        return self.entrypoint

    @property
    def description(self) -> str:
        return f"{self.kind} ({self.entrypoint})"


class ModelInterfaceError(TypeError):
    """Raised when a Python file does not implement a supported model shape."""


def load_model(path: str | Path) -> ModuleType:
    """Execute *path* in a fresh module that is never registered in ``sys.modules``."""

    source_path = Path(path)
    source = source_path.read_bytes()
    stem = re.sub(r"\W+", "_", source_path.stem).strip("_") or "world_model"
    module = ModuleType(f"_schema_{stem}_{next(_MODULE_IDS)}")
    module.__file__ = str(source_path)
    module.__package__ = None
    # Released models intentionally refer to this injected global without defining it.
    module.CURRENT_LEVEL = 0
    exec(compile(source, str(source_path), "exec"), module.__dict__)
    detect_interface(module)
    return module


load_world_model = load_model


def detect_interface(model: ModuleType) -> ModelInterface:
    """Validate and describe the model's stateful or stateless entrypoint."""

    if callable(getattr(model, "predict", None)):
        if not callable(getattr(model, "init_state", None)):
            raise ModelInterfaceError("stateful predict models must define init_state(entry_grid)")
        kind: InterfaceKind = "stateful"
        entrypoint: Entrypoint = "predict"
    elif callable(getattr(model, "step", None)):
        kind = "stateless"
        entrypoint = "step"
    else:
        raise ModelInterfaceError("world model must define predict(...) or step(...)")
    return ModelInterface(
        kind=kind,
        entrypoint=entrypoint,
        has_is_goal=callable(getattr(model, "is_goal", None)),
    )


get_interface = detect_interface


def set_current_level(model: ModuleType, level: int) -> None:
    """Inject the current zero-based level into a loaded model."""

    if type(level) is not int or level < 0:
        raise ValueError("level must be a non-negative integer")
    model.CURRENT_LEVEL = level


def call_init_state(model: ModuleType, entry_grid: Grid) -> Any:
    """Initialize latent state, or return ``None`` for a stateless model."""

    interface = detect_interface(model)
    if not interface.stateful:
        return None
    return model.init_state(entry_grid)


init_state = call_init_state


def _call_step(function: Any, grid: Grid, action: int, x: int | None, y: int | None) -> Any:
    """Call the two-argument step form while supporting its optional click coordinates."""

    signature = inspect.signature(function)
    parameters = signature.parameters
    accepts_keywords = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    kwargs: dict[str, int | None] = {}
    if accepts_keywords or "x" in parameters:
        kwargs["x"] = x
    if accepts_keywords or "y" in parameters:
        kwargs["y"] = y
    return function(grid, action, **kwargs)


def _normalized_info(raw: Mapping[str, Any]) -> dict[str, bool]:
    info: dict[str, bool] = {}
    for name in TERMINAL_FLAGS:
        value = raw.get(name, False)
        if not isinstance(value, (bool, np.bool_)):
            raise TypeError(f"world-model info flag {name!r} must be bool")
        info[name] = bool(value)
    return info


def _normalized_grid(raw: Any) -> Grid:
    array = np.asarray(raw)
    if array.ndim != 2:
        raise TypeError("world-model predicted grid must be two-dimensional")
    if not np.issubdtype(array.dtype, np.integer):
        raise TypeError("world-model predicted grid must contain integers")
    return array.astype(int, copy=False).tolist()


def _normalize_prediction(raw: Any, fallback_state: Any) -> tuple[Grid, dict[str, bool], Any]:
    if (
        isinstance(raw, tuple)
        and len(raw) == 3
        and isinstance(raw[1], Mapping)
    ):
        grid, info, next_state = raw
    elif (
        isinstance(raw, tuple)
        and len(raw) == 2
        and isinstance(raw[1], Mapping)
    ):
        grid, info = raw
        next_state = fallback_state
    elif isinstance(raw, Mapping) and "grid" in raw:
        grid = raw["grid"]
        info = raw
        next_state = raw.get("state", fallback_state)
    else:
        grid = raw
        info = {}
        next_state = fallback_state
    return _normalized_grid(grid), _normalized_info(info), next_state


def call_predict(
    model: ModuleType,
    state: Any,
    grid: Grid,
    action: int,
    x: int | None = None,
    y: int | None = None,
) -> tuple[Grid, dict[str, bool], Any]:
    """Call either model shape and normalize its grid, flags, and next state."""

    interface = detect_interface(model)
    if interface.stateful:
        raw = model.predict(state, grid, action, x=x, y=y)
    else:
        raw = _call_step(model.step, grid, action, x, y)
    return _normalize_prediction(raw, state)


predict = call_predict


__all__ = [
    "Entrypoint",
    "InterfaceKind",
    "ModelInterface",
    "ModelInterfaceError",
    "TERMINAL_FLAGS",
    "call_init_state",
    "call_predict",
    "detect_interface",
    "get_interface",
    "init_state",
    "load_model",
    "load_world_model",
    "predict",
    "set_current_level",
]
