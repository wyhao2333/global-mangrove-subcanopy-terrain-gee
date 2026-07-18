import unittest

from mangrove_terrain.prepare_gmw import coverage_cells_for_bounds


class CoverageGridTests(unittest.TestCase):
    def test_single_cell_bounds_keep_one_cell(self):
        self.assertEqual(coverage_cells_for_bounds(100.1, 20.1, 100.9, 20.9), [(100, 20)])

    def test_crossing_cell_boundary_keeps_both_cells(self):
        self.assertEqual(
            coverage_cells_for_bounds(100.9, 20.2, 101.1, 20.8),
            [(100, 20), (101, 20)],
        )

    def test_touching_max_boundary_does_not_add_adjacent_cell(self):
        self.assertEqual(coverage_cells_for_bounds(100.2, 20.2, 101.0, 21.0), [(100, 20)])


if __name__ == "__main__":
    unittest.main()
