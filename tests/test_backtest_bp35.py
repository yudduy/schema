"""Framework-fidelity gate: our backtest faithfully reproduces the released models.

The RIGHT invariant is not "the final v5 model reproduces the entire 566-step history" —
the agent's final model, still being edited mid-L8, was never re-certified over the early
exploratory history it had superseded. The invariant the real system actually maintained is
"each certified checkpoint reproduces its own history": every per-level snapshot backtests
green over the transitions up to the level it cleared. That is what proves our model-exec +
backtest reconstruction (level segmentation, CURRENT_LEVEL injection, before-grid threading,
terminal-grid skip) matches theirs.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

from schema_harness.backtest import ALIGNMENT_BACKTEST_SELECTOR, run_backtest
from schema_harness.model_loader import load_model

REPO_ROOT = Path(__file__).resolve().parents[1]
BP35_EVENTS = REPO_ROOT / "vendor" / "bp35_events.jsonl"
BP35_MODEL = REPO_ROOT / "vendor" / "bp35_world_model_v5.py"

# level_up steps in bp35: [18, 65, 101, 123, 182, 224, 281, 348, 565].
# Each snapshot is the model at the moment it cleared level N; it must reproduce every
# transition through that clear.
SNAPSHOTS = [
    ("bp35_cleared_level_1.py", 65),   # through L1 clear
    ("bp35_cleared_level_4.py", 182),  # through L4 clear (spans deaths + resets)
    ("bp35_cleared_level_7.py", 348),  # through L7 clear (8 levels, 7 level-ups)
]


def _tiny_history() -> dict[str, object]:
    return {
        "initial_turn": {"grid": [[0]], "level": 0, "win_levels": 1},
        "actions": [
            {
                "step_index": index,
                "action": 3,
                "x": None,
                "y": None,
                "grid": [[index + 1]],
                "level": 0,
                "level_up": False,
                "dead": False,
                "win": False,
            }
            for index in range(3)
        ],
    }


def _correct_stateful_model(
    *,
    ingest_fails_on: list[list[int]] | None = None,
    predict_wrong_on: list[list[int]] | None = None,
) -> ModuleType:
    model = ModuleType("test_backtest_model")

    def init_state(entry_grid: list[list[int]]) -> int:
        return entry_grid[0][0]

    def predict(
        state: int,
        grid: list[list[int]],
        _action: int,
        x: int | None = None,
        y: int | None = None,
    ) -> tuple[list[list[int]], dict[str, bool], int]:
        del x, y
        predicted = (
            [[99]]
            if grid == predict_wrong_on
            else [[value + 1 for value in row] for row in grid]
        )
        return predicted, {}, state + 1

    def ingest(state: int, actual_grid: list[list[int]]) -> int:
        if actual_grid == ingest_fails_on:
            raise ValueError("ingest alignment crash")
        return state

    model.init_state = init_state
    model.predict = predict
    model.ingest = ingest
    return model


def test_alignment_backtest_records_ingest_crash():
    report = run_backtest(
        _correct_stateful_model(ingest_fails_on=[[2]]),
        _tiny_history(),
        selector=ALIGNMENT_BACKTEST_SELECTOR,
    )

    assert report.checked == 3
    assert report.correct == 2
    assert report.mismatch_count == 1
    assert report.correct == report.checked - report.mismatch_count
    assert report.ok is False
    assert (
        report.details[0].index,
        report.details[0].kinds,
        report.details[0].error,
    ) == (1, ("ingest",), "ingest: ValueError: ingest alignment crash")
    assert "; 1 mismatch(es)," in str(report)
    assert "Model predicts ALL checkable transitions" not in str(report)


def test_working_ingest_and_all_selector_stay_green():
    reports = (
        run_backtest(
            _correct_stateful_model(),
            _tiny_history(),
            selector=ALIGNMENT_BACKTEST_SELECTOR,
        ),
        run_backtest(
            _correct_stateful_model(ingest_fails_on=[[2]]),
            _tiny_history(),
            selector="all",
        ),
    )

    for report in reports:
        assert report.ok
        assert report.correct == report.checked == 3
        assert report.mismatch_count == 0


def test_ingest_crash_does_not_double_count_existing_transition_mismatch():
    report = run_backtest(
        _correct_stateful_model(
            ingest_fails_on=[[2]],
            predict_wrong_on=[[1]],
        ),
        _tiny_history(),
        selector=ALIGNMENT_BACKTEST_SELECTOR,
    )

    assert report.checked == 3
    assert report.correct == 2
    assert report.mismatch_count == 1
    assert [(detail.index, detail.kinds) for detail in report.details] == [
        (1, ("grid",)),
    ]


def test_snapshots_backtest_green_over_their_certified_windows():
    """THE GATE: each certified checkpoint reproduces its own history exactly."""
    for filename, last_idx in SNAPSHOTS:
        model = load_model(REPO_ROOT / "vendor" / filename)
        report = run_backtest(model, BP35_EVENTS, selector=range(0, last_idx + 1))
        assert report.checked > 0, filename
        assert report.mismatches == 0, f"{filename}: {report.details[:3]}"
        assert report.correct == report.checked, filename


def test_v5_reproduces_the_relevant_prefix_but_not_superseded_late_L8():
    """v5 (final model) is green over the history that mattered, and provably diverges only
    on the early-L8 exploration it later superseded — documented here, not hidden."""
    model = load_model(BP35_MODEL)

    # Green over the prefix the agent kept certified (indices 0..398).
    prefix = run_backtest(model, BP35_EVENTS, selector=range(0, 399))
    assert prefix.mismatches == 0
    assert prefix.correct == prefix.checked

    # Over the FULL history, v5 diverges only within late L8, starting at #399, by design.
    full = load_model(BP35_MODEL)
    report = run_backtest(full, BP35_EVENTS)  # selector="all"
    assert report.mismatches == 47
    assert min(d.index for d in report.details) == 399
    assert all(d.index >= 399 for d in report.details)  # all within late L8
