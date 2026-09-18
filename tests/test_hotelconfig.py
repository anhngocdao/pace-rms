# tests/test_hotelconfig.py
import unittest
import datetime as dt
import json
import os
import tempfile

from pace import config
from pace import calendar as C
from pace import hotelconfig as HC


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


TORONTO = {
    "name": "Hotel Aurora", "currency": "CAD", "fx": {},
    "sellable_rooms": 150, "rates_include_tax": "unknown",
    "group_threshold_rooms": 10, "detect_groups": True,
    "rate_floor": 109.0, "rate_ceiling": 469.0, "rate_step": 4.0, "base_rate": 189.0,
    "variable_cost": 31.0, "max_lead": 180, "max_los": 5, "sellout_threshold": 0.97,
    "price_month_factor": {str(m): f for m, f in C.MONTH_FACTOR.items()},
    "demand_season_band": {str(m): b for m, b in C.toronto_seasonality().demand_season_band.items()},
    "segment_rate_ratio": {"CORP": 0.82, "GROUP": 0.70},
    "segment_commission": {"RETAIL": 0.02, "OTA": 0.17, "CORP": 0.0, "GROUP": 0.0},
    "events": [],
    "segment_map_order": ["segment"],
    "segment_map": {"segment": {"RETAIL": "RETAIL"}},
}


def _write(d):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(d, fh)
    return path


class LoadHotelJson(unittest.TestCase):
    def tearDown(self):
        C.reset_seasonality()
        config.reset_segments()

    def test_strict_load_needs_every_field(self):
        d = dict(TORONTO); del d["price_month_factor"]
        with self.assertRaises(HC.ConfigError) as cm:
            HC.load_hotel_json(_write(d))
        self.assertIn("price_month_factor", str(cm.exception))

    def test_apply_returns_a_hotel_and_sets_the_seams(self):
        cfg = HC.load_hotel_json(_write(TORONTO))
        hotel = HC.apply(cfg)
        self.assertEqual(hotel.rooms, 150)
        self.assertEqual(hotel.sellout_threshold, 0.97)
        self.assertEqual(C.month_factor(dt.date(2024, 7, 1)), 1.22)
        self.assertEqual(config.SEGMENTS["OTA"].commission, 0.17)

    def test_month_keys_are_integers_after_load(self):
        cfg = HC.load_hotel_json(_write(TORONTO))
        self.assertEqual(set(cfg.price_month_factor), set(range(1, 13)))

    def test_events_build_a_calendar(self):
        d = dict(TORONTO); d["events"] = [{"name": "Fair", "start": "2024-08-16", "end": "2024-08-25", "multiplier": 1.2, "note": ""}]
        cal = HC.event_calendar(HC.load_hotel_json(_write(d)))
        self.assertAlmostEqual(cal.multiplier(dt.date(2024, 8, 20)), 1.2)
        self.assertAlmostEqual(cal.multiplier(dt.date(2024, 9, 1)), 1.0)


if __name__ == "__main__":
    unittest.main()
