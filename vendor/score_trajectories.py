#!/usr/bin/env python3
"""Recompute and print RHAE scores for the 50 released trajectories.

The script uses only the Python standard library.  It discovers trajectory
directories, streams their ``events.jsonl`` files, recomputes per-level action
counts and RHAE scores, and cross-checks the results against each dataset's
``evaluation_results.csv`` manifest.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


DATASET_NAMES = ("gpt_5_6_sol", "claude_fable_opus")
PER_LEVEL_SCORE_CAP = 115.0
GAME_SCORE_CAP = 100.0
EFFORT_PATTERN = re.compile(
    r"(?:^|_)(minimal|low|medium|high|xhigh|max)(?:_|$)", re.IGNORECASE
)


class ScoreError(RuntimeError):
    """Raised when a trajectory or its metadata is inconsistent."""


@dataclass(frozen=True)
class Baseline:
    task: str
    game_id: str
    actions: tuple[int, ...]


@dataclass(frozen=True)
class EventSummary:
    completed_actions: tuple[int, ...]
    incomplete_actions: int | None
    total_actions: int
    state: str


@dataclass(frozen=True)
class TrajectoryScore:
    dataset: str
    path: Path
    task: str
    game_id: str
    provider: str
    model: str
    effort: str
    state: str
    completed_actions: tuple[int, ...]
    incomplete_actions: int | None
    total_actions: int
    baseline_actions: tuple[int, ...]
    level_scores: tuple[float, ...]
    score: float

    @property
    def levels_cleared(self) -> int:
        return len(self.completed_actions)

    @property
    def total_levels(self) -> int:
        return len(self.baseline_actions)

    @property
    def baseline_total(self) -> int:
        return sum(self.baseline_actions)

    @property
    def level_actions_text(self) -> str:
        return "/".join(str(value) for value in self.completed_actions) or "-"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recompute RHAE scores from all GPT-5.6 Sol and Claude/Fable "
            "trajectory event logs and print one combined terminal table."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="release root containing the two dataset directories (default: script directory)",
    )
    parser.add_argument(
        "--expected",
        type=int,
        default=50,
        help="required total trajectory count; use 0 to disable the check (default: 50)",
    )
    parser.add_argument(
        "--no-manifest-check",
        action="store_true",
        help="skip comparison with evaluation_results.csv files",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="omit the per-level action sequence and trajectory directory columns",
    )
    return parser.parse_args()


def positive_int(value: str, *, context: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ScoreError(f"{context}: expected an integer, got {value!r}") from exc
    if parsed <= 0:
        raise ScoreError(f"{context}: expected a positive integer, got {parsed}")
    return parsed


def baseline_csv_path(root: Path) -> Path:
    candidates = [root / "baseline_actions.csv"]
    candidates.extend(root / name / "baseline_actions.csv" for name in DATASET_NAMES)
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        raise ScoreError(
            "baseline_actions.csv was not found at the release root or in either dataset directory"
        )

    first_bytes = existing[0].read_bytes()
    for candidate in existing[1:]:
        if candidate.read_bytes() != first_bytes:
            raise ScoreError(
                f"baseline files disagree: {existing[0]} and {candidate}"
            )
    return existing[0]


def load_baselines(path: Path) -> dict[str, Baseline]:
    baselines: dict[str, Baseline] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            task = (row.get("game") or "").strip().lower()
            game_id = (row.get("game_id") or "").strip()
            if not task or not game_id:
                raise ScoreError(f"{path}:{row_number}: missing game or game_id")
            if game_id in baselines:
                raise ScoreError(f"{path}:{row_number}: duplicate game_id {game_id!r}")

            n_levels = positive_int(
                row.get("n_levels", ""), context=f"{path}:{row_number}:n_levels"
            )
            actions = tuple(
                positive_int(
                    row.get(f"level{level}", ""),
                    context=f"{path}:{row_number}:level{level}",
                )
                for level in range(1, n_levels + 1)
            )
            declared_total = positive_int(
                row.get("total_baseline_actions", ""),
                context=f"{path}:{row_number}:total_baseline_actions",
            )
            if sum(actions) != declared_total:
                raise ScoreError(
                    f"{path}:{row_number}: baseline sum {sum(actions)} "
                    f"does not match declared total {declared_total}"
                )
            baselines[game_id] = Baseline(task=task, game_id=game_id, actions=actions)

    if not baselines:
        raise ScoreError(f"{path}: no baseline rows found")
    return baselines


def discover_trajectory_dirs(root: Path) -> list[tuple[str, Path]]:
    discovered: list[tuple[str, Path]] = []
    for dataset in DATASET_NAMES:
        dataset_dir = root / dataset
        if not dataset_dir.is_dir():
            raise ScoreError(f"missing dataset directory: {dataset_dir}")
        for child in sorted(dataset_dir.iterdir(), key=lambda path: path.name.lower()):
            if child.is_dir() and (child / "events.jsonl").is_file():
                discovered.append((dataset, child))
    return discovered


def load_run_metadata(path: Path) -> dict[str, object]:
    metadata_path = path / "run.json"
    if not metadata_path.is_file():
        raise ScoreError(f"{path}: missing run.json")
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScoreError(f"{metadata_path}: could not read valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ScoreError(f"{metadata_path}: expected a JSON object")
    return payload


def stream_event_summary(path: Path) -> EventSummary:
    completed: list[int] = []
    current_level_actions = 0
    total_actions = 0
    actions_since_run_start = 0
    previous_step_index: int | None = None
    final_state: str | None = None
    last_action_state: str | None = None

    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ScoreError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(event, dict):
                raise ScoreError(f"{path}:{line_number}: event must be a JSON object")

            kind = event.get("kind")
            if kind == "run_started":
                resumed_transitions = event.get("resumed_transitions")
                if (
                    isinstance(resumed_transitions, int)
                    and resumed_transitions != total_actions
                ):
                    raise ScoreError(
                        f"{path}:{line_number}: run_started reports "
                        f"{resumed_transitions} resumed transitions, but {total_actions} "
                        "action_taken events precede it"
                    )
                actions_since_run_start = 0
            elif kind == "action_taken":
                step_index = event.get("step_index")
                if not isinstance(step_index, int):
                    raise ScoreError(
                        f"{path}:{line_number}: action_taken event has no integer step_index"
                    )
                expected = 0 if previous_step_index is None else previous_step_index + 1
                if step_index != expected:
                    raise ScoreError(
                        f"{path}:{line_number}: non-contiguous step_index; "
                        f"expected {expected}, got {step_index}"
                    )
                previous_step_index = step_index
                total_actions += 1
                actions_since_run_start += 1
                current_level_actions += 1
                if event.get("state") is not None:
                    last_action_state = str(event["state"]).upper()
                if event.get("level_up") is True:
                    completed.append(current_level_actions)
                    current_level_actions = 0
            elif kind == "run_finished" and event.get("state") is not None:
                final_state = str(event["state"]).upper()
                declared_actions = event.get("actions")
                if (
                    isinstance(declared_actions, int)
                    and declared_actions != actions_since_run_start
                ):
                    raise ScoreError(
                        f"{path}:{line_number}: run_finished reports {declared_actions} actions, "
                        f"but {actions_since_run_start} action_taken events were read "
                        "since the preceding run_started event"
                    )

    if total_actions == 0:
        raise ScoreError(f"{path}: no action_taken events found")

    state = final_state or last_action_state or "UNKNOWN"
    if state == "NOT_FINISHED":
        state = "STOPPED"
    if state == "WIN" and current_level_actions:
        raise ScoreError(
            f"{path}: run ended in WIN with {current_level_actions} unassigned actions"
        )
    incomplete = current_level_actions if current_level_actions > 0 else None
    return EventSummary(
        completed_actions=tuple(completed),
        incomplete_actions=incomplete,
        total_actions=total_actions,
        state=state,
    )


def compute_scores(
    baseline_actions: Sequence[int], completed_actions: Sequence[int]
) -> tuple[tuple[float, ...], float]:
    if len(completed_actions) > len(baseline_actions):
        raise ScoreError(
            f"trajectory completed {len(completed_actions)} levels, "
            f"but the baseline defines only {len(baseline_actions)}"
        )

    scores: list[float] = []
    for index, baseline in enumerate(baseline_actions):
        if index < len(completed_actions):
            agent_actions = completed_actions[index]
            score = min(
                (baseline / agent_actions) ** 2 * 100.0,
                PER_LEVEL_SCORE_CAP,
            )
        else:
            score = 0.0
        scores.append(score)

    weights = list(range(1, len(baseline_actions) + 1))
    total_weight = sum(weights)
    raw_score = sum(score * weight for score, weight in zip(scores, weights)) / total_weight
    completed_weight = sum(range(1, len(completed_actions) + 1))
    completion_cap = completed_weight / total_weight * GAME_SCORE_CAP
    return tuple(scores), min(raw_score, completion_cap)


def infer_effort(directory_name: str) -> str:
    match = EFFORT_PATTERN.search(directory_name)
    return match.group(1).lower() if match else "-"


def score_trajectory(
    dataset: str,
    path: Path,
    baselines: dict[str, Baseline],
) -> TrajectoryScore:
    metadata = load_run_metadata(path)
    game_id = str(metadata.get("game_id") or "").strip()
    if game_id not in baselines:
        raise ScoreError(f"{path}: no human baseline for game_id {game_id!r}")
    baseline = baselines[game_id]
    event_summary = stream_event_summary(path / "events.jsonl")
    level_scores, game_score = compute_scores(
        baseline.actions, event_summary.completed_actions
    )

    if event_summary.state == "WIN" and len(event_summary.completed_actions) != len(
        baseline.actions
    ):
        raise ScoreError(
            f"{path}: WIN run completed {len(event_summary.completed_actions)}/"
            f"{len(baseline.actions)} levels"
        )

    return TrajectoryScore(
        dataset=dataset,
        path=path,
        task=baseline.task,
        game_id=game_id,
        provider=str(metadata.get("provider") or "-"),
        model=str(metadata.get("model") or "-"),
        effort=infer_effort(path.name),
        state=event_summary.state,
        completed_actions=event_summary.completed_actions,
        incomplete_actions=event_summary.incomplete_actions,
        total_actions=event_summary.total_actions,
        baseline_actions=baseline.actions,
        level_scores=level_scores,
        score=game_score,
    )


def parse_manifest_action(raw: str) -> int | None:
    value = raw.strip()
    if not value:
        return None
    match = re.fullmatch(r"\d+", value)
    if not match:
        raise ScoreError(
            f"invalid manifest action value {raw!r}; unfinished-level cells must be blank"
        )
    return int(value)


def check_manifests(root: Path, scores: Sequence[TrajectoryScore]) -> None:
    by_dataset: dict[str, list[TrajectoryScore]] = {name: [] for name in DATASET_NAMES}
    for score in scores:
        by_dataset[score.dataset].append(score)

    for dataset, dataset_scores in by_dataset.items():
        manifest_path = root / dataset / "evaluation_results.csv"
        if not manifest_path.is_file():
            raise ScoreError(f"missing evaluation manifest: {manifest_path}")
        with manifest_path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        rows_by_task = {(row.get("task") or "").strip().lower(): row for row in rows}
        if len(rows_by_task) != len(rows):
            raise ScoreError(f"{manifest_path}: duplicate or empty task values")

        expected_tasks = {score.task for score in dataset_scores}
        if set(rows_by_task) != expected_tasks:
            missing = sorted(expected_tasks - set(rows_by_task))
            extra = sorted(set(rows_by_task) - expected_tasks)
            raise ScoreError(
                f"{manifest_path}: task mismatch; missing={missing or '-'}, extra={extra or '-'}"
            )

        for score in dataset_scores:
            row = rows_by_task[score.task]
            recorded_score = float(row.get("rhae") or "nan")
            if not math.isfinite(recorded_score) or abs(recorded_score - score.score) > 0.011:
                raise ScoreError(
                    f"{manifest_path}:{score.task}: recorded RHAE {recorded_score:.2f} "
                    f"does not match recomputed {score.score:.2f}"
                )

            recorded_workdir = (row.get("workdir") or "").strip()
            if Path(recorded_workdir).name != score.path.name:
                raise ScoreError(
                    f"{manifest_path}:{score.task}: workdir {recorded_workdir!r} "
                    f"does not identify trajectory {score.path.name!r}"
                )

            try:
                recorded_levels = int(row.get("win_levels") or "")
            except ValueError as exc:
                raise ScoreError(
                    f"{manifest_path}:{score.task}: invalid win_levels value"
                ) from exc
            if recorded_levels != score.levels_cleared:
                raise ScoreError(
                    f"{manifest_path}:{score.task}: win_levels={recorded_levels} "
                    f"does not match {score.levels_cleared} completed levels"
                )

            recorded_actions = []
            for level in range(10):
                parsed = parse_manifest_action(row.get(f"level{level}", ""))
                if parsed is not None:
                    recorded_actions.append(parsed)
            expected_actions = list(score.completed_actions)
            if recorded_actions != expected_actions:
                raise ScoreError(
                    f"{manifest_path}:{score.task}: action sequence {recorded_actions} "
                    f"does not match events {expected_actions}"
                )


def render_table(
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
    *,
    right_aligned: set[int] | None = None,
) -> str:
    string_rows = [[str(value) for value in row] for row in rows]
    if any(len(row) != len(headers) for row in string_rows):
        raise ValueError("all table rows must have the same width as the header")
    widths = [len(header) for header in headers]
    for row in string_rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    right_aligned = right_aligned or set()

    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"

    def format_row(row: Sequence[str]) -> str:
        cells = []
        for index, (value, width) in enumerate(zip(row, widths)):
            cells.append(value.rjust(width) if index in right_aligned else value.ljust(width))
        return "| " + " | ".join(cells) + " |"

    lines = [border, format_row(headers), border]
    lines.extend(format_row(row) for row in string_rows)
    lines.append(border)
    return "\n".join(lines)


def print_scores(scores: Sequence[TrajectoryScore], *, compact: bool) -> None:
    headers = [
        "#",
        "Dataset",
        "Task",
        "Model",
        "Effort",
        "State",
        "Levels",
        "Human",
        "RHAE",
    ]
    if not compact:
        headers.extend(["Level actions", "Trajectory"])

    rows = []
    for index, score in enumerate(scores, start=1):
        row: list[object] = [
            index,
            score.dataset,
            score.task.upper(),
            score.model,
            score.effort,
            score.state,
            f"{score.levels_cleared}/{score.total_levels}",
            score.baseline_total,
            f"{score.score:.2f}%",
        ]
        if not compact:
            row.extend([score.level_actions_text, score.path.name])
        rows.append(row)

    print("TRAJECTORY SCORES")
    print(
        render_table(
            headers,
            rows,
            right_aligned={0, 7, 8},
        )
    )


def print_summary(scores: Sequence[TrajectoryScore]) -> None:
    summary_rows = []
    for dataset in (*DATASET_NAMES, "ALL"):
        selected = list(scores) if dataset == "ALL" else [s for s in scores if s.dataset == dataset]
        mean_score = sum(score.score for score in selected) / len(selected)
        summary_rows.append(
            [
                dataset,
                len(selected),
                sum(score.state == "WIN" for score in selected),
                f"{sum(score.levels_cleared for score in selected)}/"
                f"{sum(score.total_levels for score in selected)}",
                f"{mean_score:.2f}%",
            ]
        )

    print("\nSUMMARY")
    print(
        render_table(
            [
                "Dataset",
                "Trajectories",
                "Wins",
                "Levels",
                "Mean RHAE",
            ],
            summary_rows,
            right_aligned={1, 2, 4},
        )
    )


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    try:
        baselines = load_baselines(baseline_csv_path(root))
        trajectory_dirs = discover_trajectory_dirs(root)
        if args.expected and len(trajectory_dirs) != args.expected:
            raise ScoreError(
                f"expected {args.expected} trajectories, found {len(trajectory_dirs)}"
            )

        dataset_counts = {
            dataset: sum(found_dataset == dataset for found_dataset, _ in trajectory_dirs)
            for dataset in DATASET_NAMES
        }
        if args.expected == 50 and dataset_counts != {
            "gpt_5_6_sol": 25,
            "claude_fable_opus": 25,
        }:
            raise ScoreError(f"expected 25 trajectories per dataset, found {dataset_counts}")

        scores = [
            score_trajectory(dataset, path, baselines)
            for dataset, path in trajectory_dirs
        ]
        scores.sort(key=lambda item: (item.task, DATASET_NAMES.index(item.dataset)))
        if not args.no_manifest_check:
            check_manifests(root, scores)

        print_scores(scores, compact=args.compact)
        print_summary(scores)
        if not args.no_manifest_check:
            print("\nValidation: all event-derived scores match both evaluation manifests.")
        return 0
    except (OSError, ScoreError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
