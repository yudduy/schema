"""Sweep protocol: metric-derived action caps and per-game fallback ordering.

RHAE squares the action-efficiency ratio, so a run far past the human baseline cannot
reach a useful score however it finishes. The sweep must stop such a primary and hand
the game to the <80 fallback pass — and it must do so per game, so one unrecoverable
game cannot block every other game's fallback.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "spikes"))

import sweep  # noqa: E402


def test_action_cap_is_two_times_human_baseline():
    assert sweep.BASELINE["sp80"] == 518
    assert sweep.BASELINE["tn36"] == 317
    assert sweep.action_cap("sp80") == 1036
    assert sweep.action_cap("tn36") == 634


def test_action_cap_falls_back_for_unknown_game():
    assert sweep.action_cap("zz00") == sweep.DEFAULT_ACTION_CAP


def test_action_cap_is_far_below_our_observed_thrash():
    # The exact runs that motivated the cap: sp80 spent 2897 actions without clearing
    # level 1; tn36 spent 2364 (7.5x its baseline) to score 71.93.
    assert sweep.action_cap("sp80") < 2897
    assert sweep.action_cap("tn36") < 2364


class _Harness:
    """Records run_game call order and returns scripted scores."""

    def __init__(self, monkeypatch, tmp_path, scores):
        self.calls: list[tuple[str, str]] = []
        self.scores = scores
        monkeypatch.setattr(sweep, "LEDGER", tmp_path / "ledger.json")
        monkeypatch.setattr(sweep, "PROGRESS", tmp_path / "progress.log")
        monkeypatch.setattr(sweep, "_assert_lock_free", lambda: None)
        monkeypatch.setattr(sweep, "log", lambda *_a, **_k: None)
        monkeypatch.setattr(sweep, "run_game", self._run_game)

    def _run_game(self, _phase, game, _model, _effort, _provider, tag):
        self.calls.append((game, tag))
        return {"rhae": self.scores[(game, tag)], "state": "WIN",
                "levels": "1/1", "workdir": f"/tmp/{game}-{tag}"}


def test_run_phase_runs_fallback_immediately_after_a_sub80_primary(monkeypatch, tmp_path):
    h = _Harness(monkeypatch, tmp_path, {
        ("a", "primary"): 50.0, ("a", "fallback"): 90.0,
        ("b", "primary"): 100.0,
    })
    sweep.run_phase("sol", ["a", "b"])
    # a's fallback must precede b's primary — the head-of-line block this fixes.
    assert h.calls == [("a", "primary"), ("a", "fallback"), ("b", "primary")]
    ledger = sweep.load_ledger()["sol"]
    assert ledger["a"]["final"]["rhae"] == 90.0      # higher of the two is retained
    assert ledger["b"]["final"]["rhae"] == 100.0


def test_run_phase_retains_primary_when_fallback_scores_lower(monkeypatch, tmp_path):
    h = _Harness(monkeypatch, tmp_path, {
        ("a", "primary"): 61.44, ("a", "fallback"): 20.0,
    })
    sweep.run_phase("sol", ["a"])
    assert h.calls == [("a", "primary"), ("a", "fallback")]
    assert sweep.load_ledger()["sol"]["a"]["final"]["rhae"] == 61.44


def test_run_phase_skips_fallback_when_primary_at_or_above_80(monkeypatch, tmp_path):
    h = _Harness(monkeypatch, tmp_path, {("a", "primary"): 80.0})
    sweep.run_phase("sol", ["a"])
    assert h.calls == [("a", "primary")]
    assert sweep.load_ledger()["sol"]["a"]["final"]["rhae"] == 80.0
