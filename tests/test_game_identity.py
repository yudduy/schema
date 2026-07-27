from __future__ import annotations

import csv
from pathlib import Path

import pytest

from schema_harness import game_identity
from schema_harness.game_identity import canonical_game_id, short_game_id
from schema_harness.runner import parse_args


REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_ACTIONS = REPO_ROOT / "vendor" / "baseline_actions.csv"


def test_public_game_identity_round_trips_every_baseline_row(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with BASELINE_ACTIONS.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 25
    assert len({row["game"] for row in rows}) == len(rows)
    assert len({row["game_id"] for row in rows}) == len(rows)

    for row in rows:
        short_id = row["game"]
        full_id = row["game_id"]

        from_short = parse_args(["--game", short_id])
        from_full = parse_args(["--game", full_id])

        assert from_short.game == from_full.game == full_id
        assert from_short.workdir == from_full.workdir == tmp_path / f"agent-{short_id}"

        assert canonical_game_id(short_id) == full_id
        assert short_game_id(full_id) == short_id


def test_unknown_public_game_identity_is_rejected_at_inverse_boundary():
    assert canonical_game_id("unknown") == "unknown"
    with pytest.raises(ValueError, match="unknown public game_id"):
        short_game_id("unknown-deadbeef")


@pytest.mark.parametrize(
    "rows",
    [
        [("same", "same-11111111"), ("same", "same-22222222")],
        [("one", "same-11111111"), ("two", "same-11111111")],
    ],
)
def test_ambiguous_public_game_identity_is_rejected(rows, tmp_path, monkeypatch):
    baseline = tmp_path / "baseline_actions.csv"
    baseline.write_text(
        "game,game_id\n"
        + "".join(f"{short_id},{full_id}\n" for short_id, full_id in rows),
        encoding="utf-8",
    )
    monkeypatch.setattr(game_identity, "BASELINE_ACTIONS", baseline)

    with pytest.raises(ValueError, match="ambiguous game identity"):
        canonical_game_id(rows[0][0])
