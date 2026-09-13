import unittest

from digikam_nextcloud.geometry import displayed_dimensions, parse_tag_region


class DisplayedDimensionsTests(unittest.TestCase):
    def test_quarter_turn_orientations_swap_dimensions(self):
        for orientation in (5, 6, 7, 8):
            with self.subTest(orientation=orientation):
                self.assertEqual(
                    displayed_dimensions(5712, 4284, orientation),
                    (4284, 5712),
                )

    def test_other_orientations_keep_dimensions(self):
        for orientation in (0, 1, 2, 3, 4):
            with self.subTest(orientation=orientation):
                self.assertEqual(
                    displayed_dimensions(5712, 4284, orientation),
                    (5712, 4284),
                )

    def test_pixel_region_uses_displayed_dimensions(self):
        region = parse_tag_region(
            '<rect x="1617" y="844" width="1152" height="1509"/>',
            width=5712,
            height=4284,
            orientation=6,
        )

        self.assertIsNotNone(region)
        self.assertAlmostEqual(region.x, 1617 / 4284)
        self.assertAlmostEqual(region.y, 844 / 5712)
        self.assertAlmostEqual(region.w, 1152 / 4284)
        self.assertAlmostEqual(region.h, 1509 / 5712)

    def test_normalized_region_is_unchanged(self):
        region = parse_tag_region(
            '<rect x="0.1" y="0.2" width="0.3" height="0.4"/>',
            width=5712,
            height=4284,
            orientation=6,
        )

        self.assertIsNotNone(region)
        self.assertEqual(region.as_tuple(), (0.1, 0.2, 0.3, 0.4))


if __name__ == '__main__':
    unittest.main()
