"""Side-effect-free adapter for the frozen vendored trajectory scorer."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


SCORER_COLLECTIONS = ("gpt_5_6_sol", "claude_fable_opus")
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCORER = REPO_ROOT / "vendor" / "score_trajectories.py"
DEFAULT_BASELINE = REPO_ROOT / "vendor" / "baseline_actions.csv"
_LEVELS_PATTERN = re.compile(r"(\d+)/(\d+)")


@dataclass(frozen=True, slots=True)
class VendoredScore:
    state: str
    levels_cleared: int
    total_levels: int
    rhae: float
    stdout: str

    @property
    def levels(self) -> str:
        return f"{self.levels_cleared}/{self.total_levels}"


class VendoredScorerError(RuntimeError):
    """A scorer invocation or structured-output validation failure."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _score_rows(stdout: str) -> list[tuple[str, str, int, int, float]]:
    rows: list[tuple[str, str, int, int, float]] = []
    for line in stdout.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 9 or not cells[0].isdigit():
            continue
        levels_match = _LEVELS_PATTERN.fullmatch(cells[6])
        if levels_match is None:
            raise VendoredScorerError(
                f"vendored scorer returned invalid levels: {cells[6]!r}",
                stdout=stdout,
            )
        try:
            rhae = float(cells[8].removesuffix("%"))
        except ValueError as exc:
            raise VendoredScorerError(
                f"vendored scorer returned invalid RHAE: {cells[8]!r}",
                stdout=stdout,
            ) from exc
        rows.append(
            (
                cells[1],
                cells[5],
                int(levels_match.group(1)),
                int(levels_match.group(2)),
                rhae,
            )
        )
    return rows


def score_workdir(
    workdir: str | Path,
    *,
    scorer_path: str | Path | None = None,
    baseline_path: str | Path | None = None,
    compact: bool = False,
    trajectory_name: str | None = None,
) -> VendoredScore:
    """Score one workdir through the unchanged two-collection vendor interface."""
    source = Path(workdir)
    events_path = source / "events.jsonl"
    run_path = source / "run.json"
    missing = [path.name for path in (events_path, run_path) if not path.is_file()]
    if missing:
        raise VendoredScorerError(
            f"{source}: missing {', '.join(missing)}"
        )

    scorer = Path(scorer_path) if scorer_path is not None else DEFAULT_SCORER
    baseline = Path(baseline_path) if baseline_path is not None else DEFAULT_BASELINE
    name = trajectory_name or source.name
    try:
        with tempfile.TemporaryDirectory(prefix="schema-score-") as temporary:
            root = Path(temporary)
            shutil.copyfile(baseline, root / "baseline_actions.csv")
            for collection in SCORER_COLLECTIONS:
                trajectory = root / collection / name
                trajectory.mkdir(parents=True)
                shutil.copyfile(events_path, trajectory / "events.jsonl")
                shutil.copyfile(run_path, trajectory / "run.json")
            command = [
                sys.executable,
                str(scorer),
                "--root",
                str(root),
                "--expected",
                "0",
                "--no-manifest-check",
            ]
            if compact:
                command.append("--compact")
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
    except OSError as exc:
        raise VendoredScorerError(
            f"could not invoke vendored scorer: {exc}"
        ) from exc

    if completed.returncode != 0:
        raise VendoredScorerError(
            f"vendored scorer failed with exit {completed.returncode}: "
            f"{completed.stderr.strip()}",
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    rows = _score_rows(completed.stdout)
    if len(rows) != len(SCORER_COLLECTIONS):
        raise VendoredScorerError(
            f"vendored scorer returned {len(rows)} trajectory rows; "
            f"expected {len(SCORER_COLLECTIONS)}",
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    if {row[0] for row in rows} != set(SCORER_COLLECTIONS):
        raise VendoredScorerError(
            "vendored scorer returned unexpected collections",
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    values = {(state, cleared, total, rhae) for _, state, cleared, total, rhae in rows}
    if len(values) != 1:
        raise VendoredScorerError(
            "vendored scorer collections disagree",
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    state, levels_cleared, total_levels, rhae = values.pop()
    return VendoredScore(
        state=state,
        levels_cleared=levels_cleared,
        total_levels=total_levels,
        rhae=rhae,
        stdout=completed.stdout,
    )
