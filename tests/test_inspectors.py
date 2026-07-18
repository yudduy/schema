from __future__ import annotations

import numpy as np

from schema_harness.inspectors import (
    describe_actor_affordances,
    describe_grid_diff,
    discover_click_targets,
    pending_actor_affordance_hint,
)


def _affordance_frame(actor_col: int, *, runway: bool = True) -> list[list[int]]:
    grid = np.full((16, 22), 8, dtype=int)
    grid[8:10, 2:13] = 9
    if runway:
        grid[2:10, 11:13] = 9
    grid[8:10, actor_col:actor_col + 2] = np.array([[2, 3], [4, 5]])
    return grid.tolist()


def test_click_targets_use_an_actual_irregular_component_cell_and_xy_order():
    grid = np.zeros((9, 9), dtype=int)
    grid[1:4, 1:4] = 2
    grid[2, 2] = 0

    targets = discover_click_targets(grid)

    assert targets[0] == [2, 1]
    x, y = targets[0]
    assert grid[y, x] == 2


def test_click_targets_keep_large_fields_as_a_uniform_fallback():
    assert discover_click_targets(np.full((64, 64), 7, dtype=int)) == [[31, 31]]


def test_click_targets_are_bounded_unique_and_spatially_distributed():
    rows, cols = np.indices((64, 64))
    checkerboard = (rows + cols) % 2

    targets = discover_click_targets(checkerboard)

    assert len(targets) == 32
    assert len({tuple(target) for target in targets}) == 32
    assert {int(checkerboard[y, x]) for x, y in targets} == {0, 1}
    assert min(x for x, _ in targets) <= 1 and max(x for x, _ in targets) >= 62
    assert min(y for _, y in targets) <= 1 and max(y for _, y in targets) >= 62


def test_click_targets_rank_compact_objects_before_large_fields():
    grid = np.zeros((16, 16), dtype=int)
    grid[2:5, 2:5] = 3
    grid[3, 3] = 0

    targets = discover_click_targets(grid)

    assert targets[0] == [3, 2]
    assert targets[-1] == [8, 8]


def test_actor_affordance_finds_novel_full_footprint_runway():
    output = describe_actor_affordances(
        _affordance_frame(2),
        [(4, _affordance_frame(5)), (3, _affordance_frame(2))],
    )

    assert output == (
        "Translated-footprint topology (heuristic; current level):\n"
        "  evidence=2 translated moves; footprint=2x2; step=3 columns; "
        "current anchor=(row=8,col=2)\n"
        "  novel extrapolated context via observed moves: action 4 ×3 -> "
        "anchor=(row=8,col=11)\n"
        "  upward clearance there=2 step(s) (observed anchors max=0); "
        "anchors=[(row=5,col=11),(row=2,col=11)]"
    )


def test_actor_affordance_suppresses_no_runway_and_one_way_evidence():
    assert describe_actor_affordances(
        _affordance_frame(2, runway=False),
        [
            (4, _affordance_frame(5, runway=False)),
            (3, _affordance_frame(2, runway=False)),
        ],
    ) is None
    assert describe_actor_affordances(
        _affordance_frame(2),
        [(4, _affordance_frame(5))],
    ) is None


def test_pending_actor_affordance_requests_a_paired_observation():
    expected = (
        "Cross-transition inspector: one localized translation direction is observed "
        "but unconfirmed. After a paired/opposite movement probe, call "
        'read_history(detail="full") again to check for a novel structural context.'
    )

    assert pending_actor_affordance_hint(
        _affordance_frame(2),
        [(4, _affordance_frame(5))],
    ) == expected
    assert pending_actor_affordance_hint(
        _affordance_frame(2),
        [(4, _affordance_frame(5)), (4, _affordance_frame(8))],
    ) == expected


def test_pending_actor_affordance_stops_after_paired_or_ambiguous_evidence():
    assert pending_actor_affordance_hint(
        _affordance_frame(2),
        [(4, _affordance_frame(5)), (3, _affordance_frame(2))],
    ) is None
    assert pending_actor_affordance_hint(
        _affordance_frame(2),
        [(6, _affordance_frame(2))],
    ) is None

    def duplicate_frame(left_col: int, right_col: int) -> list[list[int]]:
        grid = np.full((16, 22), 9, dtype=int)
        actor = np.array([[2, 3], [4, 5]])
        grid[3:5, left_col:left_col + 2] = actor
        grid[11:13, right_col:right_col + 2] = actor
        return grid.tolist()

    assert pending_actor_affordance_hint(
        duplicate_frame(2, 14),
        [(4, duplicate_frame(5, 11))],
    ) is None


def test_actor_affordance_rejects_ambiguous_duplicate_movers():
    def duplicate_frame(left_col: int, right_col: int) -> list[list[int]]:
        grid = np.full((16, 22), 9, dtype=int)
        actor = np.array([[2, 3], [4, 5]])
        grid[3:5, left_col:left_col + 2] = actor
        grid[11:13, right_col:right_col + 2] = actor
        return grid.tolist()

    assert describe_actor_affordances(
        duplicate_frame(2, 14),
        [
            (4, duplicate_frame(5, 11)),
            (3, duplicate_frame(2, 14)),
        ],
    ) is None


def test_actor_affordance_rejects_globally_inconsistent_action_vectors():
    def two_actor_frame(actor_col: int, second_row: int) -> list[list[int]]:
        grid = np.asarray(_affordance_frame(actor_col))
        grid[second_row:second_row + 2, 18:20] = np.array([[6, 7], [7, 6]])
        return grid.tolist()

    assert describe_actor_affordances(
        two_actor_frame(2, 2),
        [
            (4, two_actor_frame(5, 2)),
            (3, two_actor_frame(2, 2)),
            (4, two_actor_frame(2, 5)),
        ],
    ) is None


def test_actor_affordance_does_not_merge_same_histogram_movers():
    first = np.array([[2, 3], [4, 5]])
    second = np.array([[2, 4], [3, 5]])

    def two_mover_frame(first_col: int, second_col: int) -> list[list[int]]:
        grid = np.full((16, 22), 8, dtype=int)
        grid[8:10, 2:13] = 9
        grid[2:10, 11:13] = 9
        grid[12:14, 14:19] = 9
        grid[12:14, first_col:first_col + 2] = first
        grid[8:10, second_col:second_col + 2] = second
        return grid.tolist()

    assert describe_actor_affordances(
        two_mover_frame(14, 5),
        [
            (4, two_mover_frame(17, 5)),
            (3, two_mover_frame(17, 2)),
        ],
    ) is None


def test_actor_affordance_keeps_a_contiguous_bounded_movement_window():
    observations = [
        (
            4 if index % 2 == 0 else 3,
            _affordance_frame(5 if index % 2 == 0 else 2),
        )
        for index in range(65)
    ]

    output = describe_actor_affordances(_affordance_frame(2), observations)

    assert output is not None
    assert "evidence=64 translated moves" in output
    assert "action 4 ×2 -> anchor=(row=8,col=11)" in output


def test_grid_diff_describes_disconnected_motion_and_status_change():
    before = np.zeros((8, 8), dtype=int)
    before[1:3, 1:3] = 5
    after = np.zeros((8, 8), dtype=int)
    after[1:3, 5:7] = 5
    after[7, 0] = 9

    assert describe_grid_diff(before, after) == (
        "diff: shape=8x8; changed=9/64; bbox=[r1..7,c0..6]; regions=3\n"
        "  4@[r1..2,c1..2] from={5:4} to={0:4}\n"
        "    patch before=[[5,5],[5,5]] after=[[0,0],[0,0]]\n"
        "  4@[r1..2,c5..6] from={0:4} to={5:4}\n"
        "    patch before=[[0,0],[0,0]] after=[[5,5],[5,5]]\n"
        "  1@[r7..7,c0..0] from={0:1} to={9:1}\n"
        "    patch before=[[0]] after=[[9]]\n"
        "value_pairs={0->5:4,5->0:4,0->9:1}"
    )


def test_grid_diff_handles_noop_and_shape_change():
    grid = np.array([[1, 2], [3, 4]])
    assert describe_grid_diff(grid, grid) == (
        "diff: shape=2x2; changed=0/4; bbox=none; regions=0; value_pairs={}"
    )
    assert "shape=1x2->2x2" in describe_grid_diff([[1, 2]], [[1, 2], [3, 4]])
    assert "∅->3:1" in describe_grid_diff([[1, 2]], [[1, 2], [3, 4]])


def test_grid_diff_reports_bounded_omissions():
    before = np.zeros((1, 30), dtype=int)
    after = before.copy()
    after[0, ::2] = np.arange(1, 16)
    output = describe_grid_diff(before, after, max_regions=2, max_pairs=3)
    assert "…+13 regions omitted" in output
    assert "…+12 pairs" in output


def test_grid_diff_bounds_exact_patches_by_count_and_area():
    before = np.zeros((10, 20), dtype=int)
    after = before.copy()
    after[0, 0] = 1
    after[0, 2] = 2
    after[0, 4] = 3
    after[2:10, 10:20] = 4
    output = describe_grid_diff(before, after, max_patches=2, max_patch_area=64)
    assert output.count("patch before=") == 2
    assert "patch before=[[0,0,0,0,0,0,0,0,0,0]" not in output
