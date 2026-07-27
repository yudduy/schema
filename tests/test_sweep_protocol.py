"""Sweep protocol: the <80 fallback must run per game, not in a second pass.

Two sequential passes (all primaries, then all fallbacks) let one non-terminating
primary block every other game's fallback — which is how sp80 stopped tn36 and sc25
from ever receiving their Sol-max rerun.

Note: a total-action cap was tried here and reverted. RHAE is scored per level, so a
run that pays a large one-time discovery cost and then beats the human on later levels
(tn36: 1501 actions on L1, then 43 and 48 vs human 55 and 62, winning 7/7) is the
method working — and a total-action cap kills exactly that run.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "spikes"))

import sweep  # noqa: E402


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
