"""Durable JSON replacement for harness-owned state."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def atomic_json(
    path: str | os.PathLike[str],
    payload: Mapping[str, Any],
    *,
    pretty: bool = False,
) -> None:
    """Flush and fsync complete JSON before atomically replacing its destination."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.tmp"
    )
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        if pretty:
            json.dump(payload, handle, indent=2, allow_nan=False)
        else:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
