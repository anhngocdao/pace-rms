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
    "name": "Hotel Aurora", "city": "Toronto", "currency": "CAD", "fx": {},
    "sellable_rooms": 150, "rates_include_tax": "unknown",
    "group_threshold_rooms": 10, "detect_groups": True,
    "rate_floor": 109.0, "rate_ceiling": 469.0, "rate_step": 4.0, "base_rate": 189.0,
    "variable_cost": 31.0, "walk_cost": 620.0, "max_lead": 180, "max_los": 5,
    "max_overbook_pct": 0.06, "sellout_threshold": 0.97,
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

    def test_unknown_key_is_rejected(self):
        d = dict(TORONTO); d["_derivation"] = {"window": "2015-07-01"}
        with self.assertRaises(HC.ConfigError) as cm:
            HC.load_hotel_json(_write(d))
        self.assertIn("_derivation", str(cm.exception))

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


class ApplyIsAllOrNothing(unittest.TestCase):
    """apply() either points the whole engine at this hotel or leaves it alone.
    Half a hotel is the worst of the three outcomes: a process running one
    hotel's seasons against another's contract ratios, with nothing said."""

    def tearDown(self):
        C.reset_seasonality()
        config.reset_segments()

    def _engine_state(self):
        s = C.active_seasonality()
        return (dict(s.price_month_factor), dict(s.demand_season_band),
                {code: (seg.rate_multiplier, seg.commission) for code, seg in config.SEGMENTS.items()})

    def _refused(self, **over):
        before = self._engine_state()
        cfg = HC.load_hotel_json(_write(dict(TORONTO, **over)))
        with self.assertRaises(HC.ConfigError):
            HC.apply(cfg)
        self.assertEqual(self._engine_state(), before)

    def test_null_rooms_leaves_seasonality_and_segments_untouched(self):
        self._refused(sellable_rooms=None,
                      price_month_factor={str(m): 2.0 for m in range(1, 13)},
                      demand_season_band={str(m): "peak" for m in range(1, 13)},
                      segment_rate_ratio={"CORP": 0.31, "GROUP": 0.29})

    def test_an_unusable_season_band_leaves_segments_untouched(self):
        self._refused(demand_season_band={str(m): "summer" for m in range(1, 13)},
                      segment_rate_ratio={"CORP": 0.31, "GROUP": 0.29})

    def test_an_unreadable_number_leaves_seasonality_untouched(self):
        self._refused(sellable_rooms="forty",
                      price_month_factor={str(m): 2.0 for m in range(1, 13)})

    def _refused_after_load(self, **attrs):
        """A config that reached apply() with a bad field set on it directly.
        pace/ingest.py writes sellable_rooms onto a loaded config, so a
        HotelConfig can differ from the file it came from by the time it gets
        here, and apply() is the last place to notice."""
        before = self._engine_state()
        cfg = HC.load_hotel_json(_write(dict(TORONTO, price_month_factor={str(m): 2.0 for m in range(1, 13)})))
        for k, v in attrs.items():
            setattr(cfg, k, v)
        with self.assertRaises(HC.ConfigError):
            HC.apply(cfg)
        self.assertEqual(self._engine_state(), before)

    def test_a_ratio_that_is_not_a_number_leaves_seasonality_untouched(self):
        # configure_segments is the last thing apply() does, so a value it
        # cannot read used to raise with this hotel's seasons already live and
        # the segment table half rebuilt, and with a bare ValueError that
        # run.py does not catch.
        self._refused_after_load(segment_rate_ratio={"CORP": "high", "GROUP": 0.70})

    def test_a_commission_that_is_not_a_number_leaves_seasonality_untouched(self):
        self._refused_after_load(segment_commission={"RETAIL": "two percent", "OTA": 0.17,
                                                     "CORP": 0.0, "GROUP": 0.0})

    def test_the_loader_refuses_a_ratio_that_is_not_a_number_before_apply_sees_it(self):
        d = dict(TORONTO, segment_rate_ratio={"CORP": "high", "GROUP": 0.70})
        with self.assertRaises(HC.ConfigError) as cm:
            HC.load_hotel_json(_write(d))
        self.assertIn("segment_rate_ratio", str(cm.exception))
        self.assertIn("not a number", str(cm.exception))

    def test_a_month_factor_that_is_not_a_number_is_a_config_error_not_a_traceback(self):
        d = dict(TORONTO, price_month_factor={str(m): ("x" if m == 3 else 2.0) for m in range(1, 13)})
        cfg = HC.load_hotel_json(_write(d))
        with self.assertRaises(HC.ConfigError) as cm:
            HC.apply(cfg)
        self.assertIn("seasonality", str(cm.exception))

    def test_the_active_seasonality_is_not_the_config_object(self):
        cfg = HC.load_hotel_json(_write(TORONTO))
        HC.apply(cfg)
        cfg.price_month_factor[7] = 9.9
        cfg.demand_season_band[7] = "trough"
        self.assertEqual(C.month_factor(dt.date(2024, 7, 1)), 1.22)
        self.assertEqual(C.active_seasonality().demand_season_band[7], "peak")


if __name__ == "__main__":
    unittest.main()
