import unittest

from mangrove_terrain.native_tiles import tile_bounds


class NativeTileBoundsTests(unittest.TestCase):
    def test_west_north_tile(self):
        self.assertEqual(tile_bounds("102W_012N"), (-102.0, 12.0, -96.0, 18.0))

    def test_east_south_tile(self):
        self.assertEqual(tile_bounds("132E_006S"), (132.0, -6.0, 138.0, 0.0))

    def test_rejects_invalid_id(self):
        with self.assertRaises(ValueError):
            tile_bounds("bad-tile")


if __name__ == "__main__":
    unittest.main()
