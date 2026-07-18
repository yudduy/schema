from __future__ import annotations

import json
from pathlib import Path

from schema_harness.replay_verify import replay_and_verify


REPO_ROOT = Path(__file__).resolve().parents[1]
BP35_EVENTS = REPO_ROOT / "vendor" / "bp35_events.jsonl"


def _grid_bytes(grid):
    return json.dumps(grid, separators=(",", ":"), ensure_ascii=False).encode()


def test_released_bp35_replay_scores_and_byte_matches_grids(tmp_path):
    result = replay_and_verify(BP35_EVENTS, tmp_path)

    assert result.score == 93.51
    assert result.levels == 9
    assert result.action_count == 566
    assert "93.51%" in result.scorer_stdout
    assert "9/9" in result.scorer_stdout
    assert result.run_path.is_file()
    assert result.events_path.parent.name == "claude-fable-5_max_bp35_93.51"

    released = [json.loads(line) for line in BP35_EVENTS.read_text().splitlines()]
    replayed = [json.loads(line) for line in result.events_path.read_text().splitlines()]
    released_actions = [event for event in released if event["kind"] == "action_taken"]
    replayed_actions = [event for event in replayed if event["kind"] == "action_taken"]
    assert len(replayed_actions) == len(released_actions) == 566
    assert [event["step_index"] for event in replayed_actions] == list(range(566))
    assert all(
        _grid_bytes(ours["grid"]) == _grid_bytes(theirs["grid"])
        for ours, theirs in zip(replayed_actions, released_actions, strict=True)
    )
    assert replayed_actions[-1]["state"] == "WIN"
    assert replayed_actions[-1]["level_up"] is True
