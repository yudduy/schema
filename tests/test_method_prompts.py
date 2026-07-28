"""Hygiene checks on the production method prompt.

This file used to pin the literal wording of nine experimental prompt variants. Those
variants were the METHOD-lane experiment, it concluded, and only its winner ships (the
history lives under the `archive/goal1` tag). What is worth protecting is not the
prose but the invariants the prompt must keep carrying: the contamination boundary, and
the four steps of the loop the harness enforces in its tool surface.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT = REPO_ROOT / "schema_harness" / "prompts" / "physicist.md"

# Every public game id. None may appear in the prompt.
GAME_IDS = [
    "ar25", "bp35", "cd82", "cn04", "dc22", "ft09", "g50t", "ka59", "lf52",
    "lp85", "ls20", "m0r0", "r11l", "re86", "s5i5", "sb26", "sc25", "sk48",
    "sp80", "su15", "tn36", "tr87", "tu93", "vc33", "wa30",
]


def test_production_prompt_is_the_runner_default():
    from schema_harness import runner

    assert runner.DEFAULT_SYSTEM_PROMPT == PROMPT
    assert PROMPT.is_file()


def test_prompt_names_no_game_and_keeps_the_identifier_opaque():
    # The contamination boundary: the agent must derive every mechanism itself, so the
    # prompt may not mention a specific game or lean on memorised knowledge of one.
    text = PROMPT.read_text(encoding="utf-8").lower()
    named = [g for g in GAME_IDS if g in text]
    assert named == [], f"prompt names specific game(s): {named}"
    assert "opaque" in text
    assert "no external or memorized game-specific knowledge" in text


def test_prompt_carries_the_enforced_loop():
    # These four are not stylistic advice — each is gated in the tool surface, so a
    # prompt that stopped asking for them would silently fight the harness.
    text = PROMPT.read_text(encoding="utf-8")
    assert "world_model_v<N>.py" in text          # theory as an executable program
    assert "run_backtest" in text                 # certify against recorded history
    assert "run_bfs" in text                      # plan inside the certified model
    assert "commit_actions" in text               # the only channel to the environment


def test_prompt_requires_challenging_the_representation():
    # The Einstein-over-Lorentz move: when repair keeps failing, the state representation
    # is what must change. Losing this line is how a run degenerates into epicycles.
    text = PROMPT.read_text(encoding="utf-8")
    assert "challenge the representation instead of stacking patches" in text
