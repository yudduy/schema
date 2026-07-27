"""The submission gate: verify_events must re-execute a trajectory on the true
arc_agi engine and reject anything that does not reproduce byte-for-byte.

This is the trust boundary for helper-returned traces and the published release:
a genuine trace verifies green; a fabricated grid, a lied-about final level, or a
malformed file must all verify red.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "spikes"))

from replay_parity import verify_events  # noqa: E402

BP35 = REPO / "vendor" / "bp35_events.jsonl"


def _load(path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def _write(tmp_path, events) -> Path:
    p = tmp_path / "events.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return p


def test_genuine_bp35_verifies_green():
    v = verify_events(BP35, "bp35")
    assert v.green is True
    assert v.steps_replayed == v.total_actions == 566
    assert v.grid_mismatches == 0
    assert (v.final_levels, v.final_state) == (9, "WIN")
    assert v.error is None


def test_tampered_grid_is_red(tmp_path):
    events = _load(BP35)
    actions = [e for e in events if e.get("kind") == "action_taken"]
    target = actions[len(actions) // 2]
    target["grid"][0][0] = (int(target["grid"][0][0]) + 1) % 16  # flip one cell
    v = verify_events(_write(tmp_path, events), "bp35")
    assert v.green is False
    assert v.grid_mismatches >= 1


def test_forged_final_level_is_red(tmp_path):
    # Every settled grid is genuine, but the trace lies about the final level —
    # the exact forgery a grids-only check would miss.
    events = _load(BP35)
    actions = [e for e in events if e.get("kind") == "action_taken"]
    actions[-1]["level"] = 99
    v = verify_events(_write(tmp_path, events), "bp35")
    assert v.green is False
    assert v.level_divergences


def test_forged_level_up_partition_is_red(tmp_path):
    # Every grid is genuine, but a level_up flag is moved to a non-boundary step.
    # The vendored scorer partitions per-level action counts SOLELY by level_up, so
    # this repartitions actions across levels (inflating RHAE) — the gate must catch
    # it even though grids, level, and state all still match.
    events = _load(BP35)
    actions = [e for e in events if e.get("kind") == "action_taken"]
    victim = next(e for e in actions if not e.get("level_up"))
    victim["level_up"] = True
    v = verify_events(_write(tmp_path, events), "bp35")
    assert v.green is False
    assert v.level_up_divergences


def test_malformed_trace_is_red(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text("not json\n{}\n", encoding="utf-8")
    v = verify_events(p, "bp35")
    assert v.green is False
    assert v.error


def test_genuine_trace_on_wrong_game_engine_is_red():
    # The bp35 action sequence replayed on a different game's engine cannot match —
    # this is what binds the verified game to the scorer's baseline game.
    v = verify_events(BP35, "tu93")
    assert v.green is False


def test_intake_binds_verified_game_to_run_json_game_id(tmp_path):
    # The critical forgery: a genuine bp35 trace relabeled with a harder game's
    # game_id. intake must derive the verify game from game_id (not the workdir name),
    # so bp35 events replay on the tu93 engine and are REJECTED by the replay gate —
    # not scored against tu93's baseline.
    wd = tmp_path / "sol-primary-bp35"
    wd.mkdir()
    shutil.copy(BP35, wd / "events.jsonl")
    (wd / "run.json").write_text(json.dumps(
        {"game_id": "tu93-0768757b", "model": "gpt-5.6-sol", "effort": "max"}))
    r = subprocess.run(
        [sys.executable, str(REPO / "spikes" / "intake.py"), str(wd)],
        capture_output=True, text=True, cwd=str(REPO))
    assert r.returncode != 0
    assert "failed replay verification" in (r.stdout + r.stderr)
