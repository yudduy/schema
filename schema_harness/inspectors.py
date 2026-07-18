"""Deterministic, bounded summaries of grid transitions."""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Sequence
from typing import TypeAlias

import numpy as np


CellValue: TypeAlias = int | None


def _value_text(value: CellValue) -> str:
    return "∅" if value is None else str(value)


def _counts_text(counts: Counter[CellValue], limit: int = 12) -> str:
    ranked = sorted(counts.items(), key=lambda item: (-item[1], _value_text(item[0])))
    shown = ranked[:limit]
    body = ",".join(f"{_value_text(value)}:{count}" for value, count in shown)
    if len(ranked) > limit:
        body += f",…+{len(ranked) - limit} values"
    return "{" + body + "}"


def _bbox(cells: Sequence[tuple[int, int]]) -> str:
    rows = [row for row, _ in cells]
    cols = [col for _, col in cells]
    return f"[r{min(rows)}..{max(rows)},c{min(cols)}..{max(cols)}]"


def _patch_text(
    cells: dict[tuple[int, int], CellValue],
    region: Sequence[tuple[int, int]],
) -> str:
    rows = [row for row, _ in region]
    cols = [col for _, col in region]
    return "[" + ",".join(
        "[" + ",".join(
            _value_text(cells.get((row, col)))
            for col in range(min(cols), max(cols) + 1)
        ) + "]"
        for row in range(min(rows), max(rows) + 1)
    ) + "]"


def describe_grid_diff(
    before: Sequence[Sequence[int]],
    after: Sequence[Sequence[int]],
    *,
    max_regions: int = 6,
    max_pairs: int = 12,
    max_patches: int = 3,
    max_patch_area: int = 64,
) -> str:
    """Describe changed cells and their 4-connected regions without game semantics."""

    old = np.asarray(before, dtype=int)
    new = np.asarray(after, dtype=int)
    if old.ndim != 2 or new.ndim != 2:
        raise ValueError("grid diff requires two-dimensional grids")

    height = max(old.shape[0], new.shape[0])
    width = max(old.shape[1], new.shape[1])
    old_cells: dict[tuple[int, int], CellValue] = {
        (row, col): int(old[row, col])
        for row in range(old.shape[0])
        for col in range(old.shape[1])
    }
    new_cells: dict[tuple[int, int], CellValue] = {
        (row, col): int(new[row, col])
        for row in range(new.shape[0])
        for col in range(new.shape[1])
    }
    changed = {
        (row, col)
        for row in range(height)
        for col in range(width)
        if old_cells.get((row, col)) != new_cells.get((row, col))
    }
    shape = (
        f"{height}x{width}"
        if old.shape == new.shape
        else f"{old.shape[0]}x{old.shape[1]}->{new.shape[0]}x{new.shape[1]}"
    )
    if not changed:
        return (
            f"diff: shape={shape}; changed=0/{height * width}; bbox=none; "
            "regions=0; value_pairs={}"
        )

    remaining = set(changed)
    regions: list[list[tuple[int, int]]] = []
    while remaining:
        start = min(remaining)
        remaining.remove(start)
        queue = deque([start])
        region: list[tuple[int, int]] = []
        while queue:
            row, col = queue.popleft()
            region.append((row, col))
            for neighbor in ((row - 1, col), (row, col - 1), (row, col + 1), (row + 1, col)):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    queue.append(neighbor)
        regions.append(sorted(region))
    regions.sort(key=lambda region: (-len(region), region[0]))

    lines = [
        f"diff: shape={shape}; changed={len(changed)}/{height * width}; "
        f"bbox={_bbox(sorted(changed))}; regions={len(regions)}"
    ]
    patches = 0
    for region in regions[:max_regions]:
        from_counts = Counter(old_cells.get(cell) for cell in region)
        to_counts = Counter(new_cells.get(cell) for cell in region)
        lines.append(
            f"  {len(region)}@{_bbox(region)} "
            f"from={_counts_text(from_counts)} to={_counts_text(to_counts)}"
        )
        region_rows = [row for row, _ in region]
        region_cols = [col for _, col in region]
        area = (
            (max(region_rows) - min(region_rows) + 1)
            * (max(region_cols) - min(region_cols) + 1)
        )
        if patches < max_patches and area <= max_patch_area:
            lines.append(
                f"    patch before={_patch_text(old_cells, region)} "
                f"after={_patch_text(new_cells, region)}"
            )
            patches += 1
    if len(regions) > max_regions:
        lines.append(f"  …+{len(regions) - max_regions} regions omitted")

    pairs = Counter((old_cells.get(cell), new_cells.get(cell)) for cell in changed)
    ranked_pairs = sorted(
        pairs.items(),
        key=lambda item: (
            -item[1],
            _value_text(item[0][0]),
            _value_text(item[0][1]),
        ),
    )
    pair_body = ",".join(
        f"{_value_text(old_value)}->{_value_text(new_value)}:{count}"
        for (old_value, new_value), count in ranked_pairs[:max_pairs]
    )
    if len(ranked_pairs) > max_pairs:
        pair_body += f",…+{len(ranked_pairs) - max_pairs} pairs"
    lines.append("value_pairs={" + pair_body + "}")
    return "\n".join(lines)
