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


def _component_representative(
    cells: Sequence[tuple[int, int]],
) -> tuple[int, int]:
    """Return the component cell nearest its centroid, with stable row/col ties."""

    count = len(cells)
    row_sum = sum(row for row, _ in cells)
    col_sum = sum(col for _, col in cells)
    return min(
        cells,
        key=lambda cell: (
            (cell[0] * count - row_sum) ** 2
            + (cell[1] * count - col_sum) ** 2,
            cell[0],
            cell[1],
        ),
    )


def _spread_components(
    components: Sequence[tuple[int, int, int]],
    *,
    height: int,
    width: int,
    limit: int,
) -> list[tuple[int, int, int]]:
    """Order equal-value components from small to spatially diverse."""

    remaining = list(components)
    ordered: list[tuple[int, int, int]] = []

    def center_distance(item: tuple[int, int, int]) -> int:
        return (2 * item[1] - height + 1) ** 2 + (2 * item[2] - width + 1) ** 2

    while remaining and len(ordered) < limit:
        smallest = min(size for size, _, _ in remaining)
        eligible = [item for item in remaining if item[0] == smallest]
        if ordered:
            def spread_key(item: tuple[int, int, int]) -> tuple[int, int, int, int]:
                separation = min(
                    (item[1] - row) ** 2 + (item[2] - col) ** 2
                    for _, row, col in ordered
                )
                return (-separation, center_distance(item), item[1], item[2])

            chosen = min(eligible, key=spread_key)
        else:
            chosen = min(
                eligible,
                key=lambda item: (center_distance(item), item[1], item[2]),
            )
        ordered.append(chosen)
        remaining.remove(chosen)
    return ordered


def discover_click_targets(
    grid: Sequence[Sequence[int]],
    *,
    max_targets: int = 32,
) -> list[list[int]]:
    """Propose bounded click targets from equal-value connected components.

    Compact multi-cell components are tried before isolated texture and large fields.
    Representatives are always real component cells. Value rarity only orders groups;
    no particular value is assigned semantics. Single-color components are a
    conservative first pass. Multi-color grouping is a possible upgrade if held-out
    diagnostics show fragmented targets.
    """

    if type(max_targets) is not int or max_targets < 0:
        raise ValueError("max_targets must be a non-negative integer")
    if max_targets == 0:
        return []
    array = np.asarray(grid, dtype=int)
    if array.ndim != 2 or not array.size:
        raise ValueError("click discovery requires a non-empty two-dimensional grid")
    height, width = array.shape
    if height > 64 or width > 64:
        raise ValueError("click discovery coordinates must fit in 0..63")

    value_counts = Counter(int(value) for value in array.flat)
    visited = np.zeros(array.shape, dtype=bool)
    # Each record is (tier, value, component size, representative row, col).
    records: list[tuple[int, int, int, int, int]] = []
    compact_area = max(1, array.size // 8)
    for start_row in range(height):
        for start_col in range(width):
            if visited[start_row, start_col]:
                continue
            value = int(array[start_row, start_col])
            visited[start_row, start_col] = True
            queue = deque([(start_row, start_col)])
            cells: list[tuple[int, int]] = []
            while queue:
                row, col = queue.popleft()
                cells.append((row, col))
                for neighbor_row, neighbor_col in (
                    (row - 1, col),
                    (row, col - 1),
                    (row, col + 1),
                    (row + 1, col),
                ):
                    if not (
                        0 <= neighbor_row < height
                        and 0 <= neighbor_col < width
                        and not visited[neighbor_row, neighbor_col]
                        and int(array[neighbor_row, neighbor_col]) == value
                    ):
                        continue
                    visited[neighbor_row, neighbor_col] = True
                    queue.append((neighbor_row, neighbor_col))

            rows = [row for row, _ in cells]
            cols = [col for _, col in cells]
            bbox_area = (max(rows) - min(rows) + 1) * (max(cols) - min(cols) + 1)
            spans_frame = (
                min(rows) == 0 and max(rows) == height - 1
            ) or (
                min(cols) == 0 and max(cols) == width - 1
            )
            if bbox_area > compact_area or spans_frame:
                tier = 2
            elif len(cells) == 1:
                tier = 1
            else:
                tier = 0
            row, col = _component_representative(cells)
            records.append((tier, value, len(cells), row, col))

    targets: list[list[int]] = []
    for tier in range(3):
        tier_records = [record for record in records if record[0] == tier]
        values = sorted(
            {record[1] for record in tier_records},
            key=lambda value: (value_counts[value], value),
        )
        groups = [
            _spread_components(
                [
                    (size, row, col)
                    for _, record_value, size, row, col in tier_records
                    if record_value == value
                ],
                height=height,
                width=width,
                limit=max_targets,
            )
            for value in values
        ]
        offset = 0
        while len(targets) < max_targets and any(offset < len(group) for group in groups):
            for group in groups:
                if offset >= len(group):
                    continue
                _, row, col = group[offset]
                targets.append([col, row])
                if len(targets) == max_targets:
                    return targets
            offset += 1
    return targets


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


__all__ = ["describe_grid_diff", "discover_click_targets"]
