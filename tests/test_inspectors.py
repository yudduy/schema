from __future__ import annotations

import numpy as np

from schema_harness.inspectors import describe_grid_diff, discover_click_targets


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
