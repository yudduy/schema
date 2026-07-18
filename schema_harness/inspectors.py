"""Deterministic, bounded summaries of grid transitions."""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeAlias

import numpy as np


CellValue: TypeAlias = int | None


@dataclass(frozen=True, slots=True)
class _Translation:
    index: int
    action: int
    origin: tuple[int, int]
    destination: tuple[int, int]
    vector: tuple[int, int]
    footprint: tuple[int, int]
    underlay: np.ndarray
    appearance: np.ndarray
    actor_counts: tuple[tuple[int, int], ...]
    changed_cells: int


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


def _changed_regions(
    mask: np.ndarray,
    *,
    max_regions: int,
) -> list[list[tuple[int, int]]]:
    remaining = {
        (int(row), int(col))
        for row, col in np.argwhere(mask)
    }
    regions: list[list[tuple[int, int]]] = []
    while remaining:
        if len(regions) == max_regions:
            return []
        start = min(remaining)
        remaining.remove(start)
        queue = deque([start])
        region: list[tuple[int, int]] = []
        while queue:
            row, col = queue.popleft()
            region.append((row, col))
            for neighbor in (
                (row - 1, col),
                (row, col - 1),
                (row, col + 1),
                (row + 1, col),
            ):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    queue.append(neighbor)
        regions.append(sorted(region))
    return regions


def _region_bounds(
    region: Sequence[tuple[int, int]],
) -> tuple[int, int, int, int]:
    rows = [row for row, _ in region]
    cols = [col for _, col in region]
    return min(rows), min(cols), max(rows) + 1, max(cols) + 1


def _int_counts(array: np.ndarray) -> tuple[tuple[int, int], ...]:
    return tuple(sorted(Counter(int(value) for value in array.flat).items()))


def _translation_observation(
    before: np.ndarray,
    after: np.ndarray,
    *,
    index: int,
    action: int,
) -> _Translation | None:
    if before.shape != after.shape or before.ndim != 2:
        return None
    changed = before != after
    changed_count = int(np.count_nonzero(changed))
    if changed_count < 4 or changed_count > before.size // 2:
        return None
    regions = _changed_regions(changed, max_regions=24)
    if len(regions) < 2:
        return None

    value_counts = Counter(int(value) for value in before.flat)
    candidates: list[_Translation] = []
    for source_index, source in enumerate(regions):
        if len(source) < 2:
            continue
        source_bounds = _region_bounds(source)
        source_row, source_col, source_bottom, source_right = source_bounds
        footprint = (source_bottom - source_row, source_right - source_col)
        source_before = before[source_row:source_bottom, source_col:source_right]
        source_after = after[source_row:source_bottom, source_col:source_right]
        for destination_index, destination in enumerate(regions):
            if source_index == destination_index or len(source) != len(destination):
                continue
            destination_bounds = _region_bounds(destination)
            destination_row, destination_col, destination_bottom, destination_right = (
                destination_bounds
            )
            if footprint != (
                destination_bottom - destination_row,
                destination_right - destination_col,
            ):
                continue
            vector = (
                destination_row - source_row,
                destination_col - source_col,
            )
            if (vector[0] == 0) == (vector[1] == 0):
                continue
            if vector[0] == 0 and abs(vector[1]) < footprint[1]:
                continue
            if vector[1] == 0 and abs(vector[0]) < footprint[0]:
                continue

            destination_before = before[
                destination_row:destination_bottom,
                destination_col:destination_right,
            ]
            destination_after = after[
                destination_row:destination_bottom,
                destination_col:destination_right,
            ]
            if not np.array_equal(source_after, destination_before):
                continue
            if _int_counts(source_before) != _int_counts(destination_after):
                continue
            if not np.array_equal(
                source_before != source_after,
                destination_before != destination_after,
            ):
                continue

            underlay_prevalence = sum(
                value_counts[int(value)] for value in source_after.flat
            )
            actor_prevalence = sum(
                value_counts[int(value)] for value in source_before.flat
            )
            if underlay_prevalence <= actor_prevalence:
                continue
            candidates.append(
                _Translation(
                    index=index,
                    action=action,
                    origin=(source_row, source_col),
                    destination=(destination_row, destination_col),
                    vector=vector,
                    footprint=footprint,
                    underlay=source_after.copy(),
                    appearance=destination_after.copy(),
                    actor_counts=_int_counts(source_before),
                    changed_cells=len(source) + len(destination),
                )
            )

    if len(candidates) != 1:
        return None
    return candidates[0]


def _route_text(actions: Sequence[int]) -> str:
    return f"action {actions[0]} ×{len(actions)}"


def _anchors_text(anchors: Sequence[tuple[int, int]], *, limit: int = 6) -> str:
    body = ",".join(
        f"(row={row},col={col})" for row, col in anchors[:limit]
    )
    if len(anchors) > limit:
        body += f",…+{len(anchors) - limit}"
    return "[" + body + "]"


def _collect_translations(
    initial_grid: Sequence[Sequence[int]],
    observations: Sequence[tuple[int, Sequence[Sequence[int]]]],
) -> tuple[np.ndarray, list[_Translation]]:
    initial = np.asarray(initial_grid, dtype=int)
    if initial.ndim != 2 or not initial.size:
        raise ValueError("affordance discovery requires a non-empty two-dimensional grid")

    discrete_indices = [
        index
        for index, (action, _) in enumerate(observations)
        if action not in (0, 6)
    ][-64:]
    translations: list[_Translation] = []
    for index in discrete_indices:
        before = initial if index == 0 else np.asarray(observations[index - 1][1], dtype=int)
        after = np.asarray(observations[index][1], dtype=int)
        observation = _translation_observation(
            before,
            after,
            index=index,
            action=observations[index][0],
        )
        if observation is not None:
            translations.append(observation)
    return initial, translations


def _translation_groups(
    translations: Sequence[_Translation],
) -> tuple[
    dict[int, set[tuple[int, int]]],
    list[list[_Translation]],
]:
    vectors_by_action: dict[int, set[tuple[int, int]]] = {}
    keyed: dict[
        tuple[tuple[int, int], int, int, tuple[int, ...], tuple[tuple[int, int], ...]],
        list[_Translation],
    ] = {}
    for item in translations:
        vectors_by_action.setdefault(item.action, set()).add(item.vector)
        axis = 0 if item.vector[0] else 1
        key = (
            item.footprint,
            axis,
            abs(item.vector[axis]),
            tuple(int(value) for value in item.underlay.flat),
            item.actor_counts,
        )
        keyed.setdefault(key, []).append(item)
    groups = [sorted(group, key=lambda item: item.index) for group in keyed.values()]
    return vectors_by_action, groups


def _has_unique_current_appearance(
    current: np.ndarray,
    item: _Translation,
) -> bool:
    height, width = item.footprint
    expected_row, expected_col = item.destination
    if not (
        current.ndim == 2
        and 0 <= expected_row <= current.shape[0] - height
        and 0 <= expected_col <= current.shape[1] - width
    ):
        return False
    matches = [
        (row, col)
        for row in range(current.shape[0] - height + 1)
        for col in range(current.shape[1] - width + 1)
        if np.array_equal(
            current[row:row + height, col:col + width],
            item.appearance,
        )
    ]
    return matches == [item.destination]


def pending_actor_affordance_hint(
    initial_grid: Sequence[Sequence[int]],
    observations: Sequence[tuple[int, Sequence[Sequence[int]]]],
) -> str | None:
    """Prompt a later full-history read when only one movement direction is known."""

    initial, translations = _collect_translations(initial_grid, observations)
    if not translations or not observations:
        return None
    vectors_by_action, groups = _translation_groups(translations)
    if len(groups) != 1:
        return None
    group = groups[0]
    if not all(
        left.destination == right.origin
        for left, right in zip(group, group[1:])
    ):
        return None
    axis = 0 if group[0].vector[0] else 1
    signs = {item.vector[axis] // abs(item.vector[axis]) for item in group}
    if len(signs) != 1:
        return None
    if any(len(vectors_by_action[item.action]) != 1 for item in group):
        return None

    latest = group[-1]
    current = np.asarray(observations[-1][1], dtype=int)
    if current.shape != initial.shape or not _has_unique_current_appearance(current, latest):
        return None
    return (
        "Cross-transition inspector: one localized translation direction is observed "
        "but unconfirmed. After a paired/opposite movement probe, call "
        'read_history(detail="full") again to check for a novel structural context.'
    )


def describe_actor_affordances(
    initial_grid: Sequence[Sequence[int]],
    observations: Sequence[tuple[int, Sequence[Sequence[int]]]],
) -> str | None:
    """Report a conservative novel context for an observed translated footprint.

    This is advisory geometry, not a collision or dynamics model. It activates only
    after matching translations in both directions, a deterministic action/vector
    mapping, and an unchanged current footprint. Prospective anchors must exactly
    match the repeatedly observed underlay.
    """

    initial, translations = _collect_translations(initial_grid, observations)
    if len(translations) < 2:
        return None

    all_vectors_by_action, groups = _translation_groups(translations)

    supported: list[list[_Translation]] = []
    for group in groups:
        if len(group) < 2:
            continue
        pairs = list(zip(group, group[1:]))
        if not all(left.destination == right.origin for left, right in pairs):
            continue
        if not any(
            left.origin == right.destination and left.destination == right.origin
            for left, right in pairs
        ):
            continue
        axis = 0 if group[0].vector[0] else 1
        signs = {item.vector[axis] // abs(item.vector[axis]) for item in group}
        if signs != {-1, 1}:
            continue
        vectors_by_action: dict[int, set[tuple[int, int]]] = {}
        for item in group:
            vectors_by_action.setdefault(item.action, set()).add(item.vector)
        if any(
            len(vectors) != 1 or len(all_vectors_by_action[action]) != 1
            for action, vectors in vectors_by_action.items()
        ):
            continue
        supported.append(group)
    if not supported:
        return None

    group = min(
        supported,
        key=lambda items: (
            -len(items),
            -sum(item.changed_cells for item in items),
            items[0].footprint,
            items[0].origin,
        ),
    )
    latest = max(group, key=lambda item: item.index)
    height, width = latest.footprint
    current = np.asarray(observations[-1][1], dtype=int)
    if current.shape != initial.shape:
        return None
    current_row, current_col = latest.destination
    if not _has_unique_current_appearance(current, latest):
        return None

    underlay = latest.underlay
    base = current.copy()
    base[
        current_row:current_row + height,
        current_col:current_col + width,
    ] = underlay

    def valid_anchor(anchor: tuple[int, int]) -> bool:
        row, col = anchor
        return (
            0 <= row <= base.shape[0] - height
            and 0 <= col <= base.shape[1] - width
            and np.array_equal(base[row:row + height, col:col + width], underlay)
        )

    def valid_motion(
        anchor: tuple[int, int],
        vector: tuple[int, int],
    ) -> bool:
        distance = abs(vector[0] or vector[1])
        row_sign = 0 if vector[0] == 0 else vector[0] // abs(vector[0])
        col_sign = 0 if vector[1] == 0 else vector[1] // abs(vector[1])
        return all(
            valid_anchor((anchor[0] + row_sign * offset, anchor[1] + col_sign * offset))
            for offset in range(1, distance + 1)
        )

    action_counts = Counter((item.action, item.vector) for item in group)
    actions_by_vector: dict[tuple[int, int], list[int]] = {}
    for item in group:
        actions_by_vector.setdefault(item.vector, []).append(item.action)
    vector_actions = {
        vector: min(
            set(actions),
            key=lambda action: (-action_counts[(action, vector)], action),
        )
        for vector, actions in actions_by_vector.items()
    }

    # The supported vectors are opposite fixed-size steps on one axis, so their
    # configuration-space graph is a line rather than a general BFS frontier.
    routes: dict[tuple[int, int], tuple[int, ...]] = {latest.destination: ()}
    for vector, action in sorted(vector_actions.items()):
        anchor = latest.destination
        for distance in range(1, 13):
            if not valid_motion(anchor, vector):
                break
            anchor = (anchor[0] + vector[0], anchor[1] + vector[1])
            routes[anchor] = (action,) * distance

    axis = 0 if latest.vector[0] else 1
    step = abs(latest.vector[axis])
    if axis == 1:
        orthogonal = ((-step, 0, "upward"), (step, 0, "downward"))
        axis_name = "columns"
    else:
        orthogonal = ((0, -step, "leftward"), (0, step, "rightward"))
        axis_name = "rows"

    def clearance(
        anchor: tuple[int, int],
        vector: tuple[int, int],
    ) -> list[tuple[int, int]]:
        result: list[tuple[int, int]] = []
        candidate = (anchor[0] + vector[0], anchor[1] + vector[1])
        previous = anchor
        while len(result) < 16 and valid_motion(previous, vector):
            result.append(candidate)
            previous = candidate
            candidate = (candidate[0] + vector[0], candidate[1] + vector[1])
        return result

    observed_anchors = {
        anchor
        for item in group
        for anchor in (item.origin, item.destination)
        if valid_anchor(anchor)
    }
    observed_maxima = [
        max(
            (len(clearance(anchor, (row_step, col_step))) for anchor in observed_anchors),
            default=0,
        )
        for row_step, col_step, _ in orthogonal
    ]

    candidates: list[
        tuple[
            int,
            int,
            int,
            tuple[int, int],
            int,
            tuple[int, ...],
            list[tuple[int, int]],
        ]
    ] = []
    for anchor, plan in routes.items():
        if anchor in observed_anchors:
            continue
        for direction_index, (row_step, col_step, _) in enumerate(orthogonal):
            ray = clearance(anchor, (row_step, col_step))
            gain = len(ray) - observed_maxima[direction_index]
            if gain <= 0:
                continue
            candidates.append(
                (
                    -gain,
                    -len(ray),
                    len(plan),
                    anchor,
                    direction_index,
                    plan,
                    ray,
                )
            )
    if not candidates:
        return None

    _, _, _, anchor, direction_index, plan, ray = min(candidates)
    direction = orthogonal[direction_index][2]
    observed_maximum = observed_maxima[direction_index]
    return "\n".join(
        (
            "Translated-footprint topology (heuristic; current level):",
            f"  evidence={len(group)} translated moves; footprint={height}x{width}; "
            f"step={step} {axis_name}; current anchor=(row={current_row},col={current_col})",
            f"  novel extrapolated context via observed moves: {_route_text(plan)} -> "
            f"anchor=(row={anchor[0]},col={anchor[1]})",
            f"  {direction} clearance there={len(ray)} step(s) "
            f"(observed anchors max={observed_maximum}); anchors={_anchors_text(ray)}",
        )
    )


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


__all__ = [
    "describe_actor_affordances",
    "describe_grid_diff",
    "discover_click_targets",
    "pending_actor_affordance_hint",
]
