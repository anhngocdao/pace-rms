# tests/test_calendar_config.py
import datetime as dt
import unittest

from pace import calendar as C


class SeasonalitySeam(unittest.TestCase):
    def tearDown(self):
        C.reset_seasonality()

    def test_default_is_the_toronto_table(self):
        self.assertEqual(C.month_factor(dt.date(2024, 7, 1)), 1.22)
        self.assertEqual(C.season_band(dt.date(2024, 7, 1)), "peak")
        self.assertEqual(C.season_band(dt.date(2024, 1, 1)), "trough")
        self.assertEqual(C.season_band(dt.date(2024, 4, 1)), "shoulder")

    def test_toronto_bands_are_four_three_five(self):
        s = C.toronto_seasonality()
        bands = list(s.demand_season_band.values())
        self.assertEqual((bands.count("peak"), bands.count("shoulder"), bands.count("trough")), (4, 3, 5))

    def test_price_and_demand_are_independent(self):
        flat_price = {m: 1.0 for m in range(1, 13)}
        bands = {m: ("peak" if m in (7, 8) else "trough") for m in range(1, 13)}
        C.set_seasonality(C.Seasonality(flat_price, bands))
        self.assertEqual(C.month_factor(dt.date(2024, 7, 1)), 1.0)
        self.assertEqual(C.season_band(dt.date(2024, 7, 1)), "peak")
        self.assertEqual(C.season_band(dt.date(2024, 4, 1)), "trough")
        self.assertEqual(C.demand_class(dt.date(2024, 4, 1)), ("trough", 0))

    def test_reset_restores_toronto(self):
        C.set_seasonality(C.Seasonality({m: 2.0 for m in range(1, 13)}, {m: "peak" for m in range(1, 13)}))
        C.reset_seasonality()
        self.assertEqual(C.month_factor(dt.date(2024, 1, 1)), 0.72)

    def test_rejects_incomplete_tables(self):
        with self.assertRaises(ValueError):
            C.Seasonality({1: 1.0}, {m: "peak" for m in range(1, 13)}).validate()
        with self.assertRaises(ValueError):
            C.Seasonality({m: 1.0 for m in range(1, 13)}, {m: "high" for m in range(1, 13)}).validate()
