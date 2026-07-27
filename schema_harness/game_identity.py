"""Public ARC game identity backed by the frozen human-baseline catalog."""

from __future__ import annotations

import csv
from pathlib import Path


BASELINE_ACTIONS = (
    Path(__file__).resolve().parents[1] / "vendor" / "baseline_actions.csv"
)


def _identity_maps() -> tuple[dict[str, str], dict[str, str]]:
    short_to_full: dict[str, str] = {}
    full_to_short: dict[str, str] = {}
    with BASELINE_ACTIONS.open(encoding="utf-8", newline="") as handle:
        for line_number, row in enumerate(csv.DictReader(handle), start=2):
            short_id = row.get("game") or ""
            full_id = row.get("game_id") or ""
            if not short_id or not full_id:
                raise ValueError(
                    f"{BASELINE_ACTIONS}:{line_number}: missing game identity"
                )
            if short_id in short_to_full or full_id in full_to_short:
                raise ValueError(
                    f"{BASELINE_ACTIONS}:{line_number}: ambiguous game identity"
                )
            short_to_full[short_id] = full_id
            full_to_short[full_id] = short_id
    return short_to_full, full_to_short


def canonical_game_id(game: str) -> str:
    """Resolve a public short selector to its full versioned game ID."""
    short_to_full, _ = _identity_maps()
    return short_to_full.get(game, game)


def short_game_id(game_id: str) -> str:
    """Return the public short ID for an exact full versioned game ID."""
    _, full_to_short = _identity_maps()
    try:
        return full_to_short[game_id]
    except KeyError:
        raise ValueError(f"unknown public game_id: {game_id!r}") from None
