# tests/test_hotelconfig.py
import unittest

from pace import config


class SegmentSeam(unittest.TestCase):
    def tearDown(self):
        config.reset_segments()

    def test_hotel_carries_the_sellout_threshold(self):
        self.assertEqual(config.Hotel().sellout_threshold, 0.97)
        self.assertEqual(config.Hotel(sellout_threshold=0.9).sellout_threshold, 0.9)

    def test_configure_segments_replaces_ratios_in_place(self):
        seg_dict = config.SEGMENTS
        config.configure_segments({"CORP": 0.75, "GROUP": 0.6}, {"OTA": 0.15})
        self.assertIs(config.SEGMENTS, seg_dict)
        self.assertEqual(config.SEGMENTS["CORP"].rate_multiplier, 0.75)
        self.assertEqual(config.SEGMENTS["GROUP"].rate_multiplier, 0.6)
        self.assertEqual(config.SEGMENTS["OTA"].commission, 0.15)
        self.assertEqual(config.SEGMENTS["RETAIL"].rate_multiplier, 1.0)

    def test_reset_restores_defaults(self):
        config.configure_segments({"CORP": 0.5}, {})
        config.reset_segments()
        self.assertEqual(config.SEGMENTS["CORP"].rate_multiplier, 0.82)
        self.assertEqual(config.SEGMENTS["GROUP"].rate_multiplier, 0.70)

    def test_unknown_segment_is_an_error(self):
        with self.assertRaises(KeyError):
            config.configure_segments({"NONREV": 0.0}, {})
