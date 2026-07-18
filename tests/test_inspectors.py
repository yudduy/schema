from __future__ import annotations

import numpy as np

from schema_harness.inspectors import describe_grid_diff


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
