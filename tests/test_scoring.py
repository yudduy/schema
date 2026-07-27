from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from schema_harness.scoring import (
    VendoredScorerError,
    score_workdir,
)
from vendor.score_trajectories import stream_event_summary


REPO_ROOT = Path(__file__).resolve().parents[1]
BP35_EVENTS = REPO_ROOT / "vendor" / "bp35_events.jsonl"
EXPECTED_LEVEL_ACTIONS = (19, 47, 36, 22, 59, 42, 57, 67, 217)


def _write_workdir(tmp_path: Path, mutation: str | None = None) -> Path:
    workdir = tmp_path / f"bp35-{mutation or 'golden'}"
    workdir.mkdir()
    events = [
        json.loads(line)
        for line in BP35_EVENTS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    actions = [event for event in events if event.get("kind") == "action_taken"]
    if mutation == "step-gap":
        actions[len(actions) // 2]["step_index"] += 1
    elif mutation == "extra-level-up":
        next(event for event in actions if not event.get("level_up"))["level_up"] = True

    (workdir / "events.jsonl").write_text(
        "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events),
        encoding="utf-8",
    )
    (workdir / "run.json").write_text(
        '{"game_id":"bp35-0a0ad940"}\n',
        encoding="utf-8",
    )
    return workdir


def test_all_scorer_adapters_match_bp35_golden(tmp_path):
    summary = stream_event_summary(BP35_EVENTS)
    assert summary.state == "WIN"
    assert summary.completed_actions == EXPECTED_LEVEL_ACTIONS
    assert summary.total_actions == 566

    workdir = _write_workdir(tmp_path)
    score = score_workdir(workdir)

    assert (
        score.state,
        score.levels,
        score.rhae,
    ) == ("WIN", "9/9", 93.51)
    assert score.stdout.count("BP35") == 2


@pytest.mark.parametrize("mutation", ["step-gap", "extra-level-up"])
def test_all_scorer_adapters_reject_invalid_evidence(mutation, tmp_path):
    workdir = _write_workdir(tmp_path, mutation)

    with pytest.raises(VendoredScorerError, match="vendored scorer failed"):
        score_workdir(workdir)


def test_nonzero_scorer_exit_rejects_valid_looking_output(tmp_path):
    workdir = _write_workdir(tmp_path)
    fake_scorer = tmp_path / "fake_scorer.py"
    fake_scorer.write_text(
        "print('| 1 | gpt_5_6_sol | BP35 | - | max | WIN | 9/9 | 651 | 93.51% |')\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )

    with pytest.raises(VendoredScorerError) as raised:
        score_workdir(workdir, scorer_path=fake_scorer)

    assert raised.value.returncode == 2
    assert "93.51%" in raised.value.stdout


def test_importing_intake_does_not_create_sweep_directory(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    environment = os.environ.copy()
    environment["HOME"] = str(home)

    completed = subprocess.run(
        [sys.executable, "-c", "import spikes.intake"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert not (home / "schema-sweep").exists()
