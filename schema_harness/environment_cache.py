"""Resolution policy for the local ARC environment cache."""

from __future__ import annotations

import os
from pathlib import Path


def resolve_environments_dir() -> Path:
    """Return the configured, repository, or CWD-relative environment cache."""
    configured = os.environ.get("SCHEMA_ENVIRONMENTS_DIR")
    if configured:
        return Path(configured).expanduser()
    repository_cache = Path(__file__).resolve().parents[1] / "environment_files"
    return repository_cache if repository_cache.is_dir() else Path("environment_files")
