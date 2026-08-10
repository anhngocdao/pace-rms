"""Checks for the multi room type network layer.

The single resource engine is already measured.  What these tests defend is
the promise that adding room types cannot quietly change what that engine
does, and that when it does change something the change is substitution and
not arithmetic drift.
"""

import unittest

from pace.choice import SubstitutionModel, ratios_from_rates
from pace.config import DEFAULT_HOTEL
from pace.elasticity import acceptance
from pace.roomtypes import (DEFAULT_INVENTORY, Inventory, RoomType,
                            preferences_for)

INV = DEFAULT_INVENTORY
PREFS = preferences_for(INV)
SOLO = Inventory([RoomType("STD", "Only room", DEFAULT_HOTEL.rooms, 1.00, 0)])


class TestInventory(unittest.TestCase):
    def test_the_inventory_is_the_building_config_describes(self):
        INV.validate(DEFAULT_HOTEL.rooms)
        self.assertEqual(INV.total_rooms(), DEFAULT_HOTEL.rooms)

    def test_a_building_that_does_not_add_up_is_refused(self):
        wrong = Inventory([RoomType("STD", "Standard", 10, 1.00, 0)])
        with self.assertRaises(ValueError):
            wrong.validate(DEFAULT_HOTEL.rooms)

    def test_the_entry_type_carries_the_published_rate(self):
        bad = Inventory([RoomType("DLX", "Deluxe", 150, 1.18, 0)])
        with self.assertRaises(ValueError):
            bad.validate(150)
        self.assertEqual(INV.entry.code, "STD")
        self.assertAlmostEqual(INV.rate_of("STD", 200.0), 200.0, places=9)
        self.assertAlmostEqual(INV.rate_of("DLX", 200.0), 236.0, places=9)

    def test_types_come_back_in_quality_order(self):
        self.assertEqual(INV.codes, ("STD", "DLX", "EXE", "STE"))


class TestSubstitutionReducesToTheOldModel(unittest.TestCase):
    """The invariant that makes this safe to add to a measured system."""

    def test_one_room_type_is_the_single_resource_model_exactly(self):
        model = SubstitutionModel(SOLO, preferences_for(SOLO))
        for k in (0.9, 3.8, 4.8, 9.0):
            for ratio in (0.7, 0.9, 1.0, 1.15, 1.6, 2.4):
                got = model.split("RETAIL", 40.0, k, {"STD": ratio})
                self.assertAlmostEqual(got["STD"], 40.0 * acceptance(k, ratio),
                                       places=12)

    def test_pricing_every_type_in_step_cannot_move_house_demand(self):
        model = SubstitutionModel(INV, PREFS)
        for k in (1.6, 3.8, 4.8):
            for ratio in (0.8, 1.0, 1.25, 1.7):
                ratios = {c: ratio for c in INV.codes}
                total = sum(model.split("RETAIL", 60.0, k, ratios).values())
                self.assertAlmostEqual(total, 60.0 * acceptance(k, ratio), places=10)

    def test_shares_sum_to_one_whatever_is_offered(self):
        model = SubstitutionModel(INV, PREFS)
        ratios = {"STD": 1.0, "DLX": 1.1, "EXE": 0.95, "STE": 1.3}
        for offered in (("STD",), ("STD", "STE"), INV.codes):
            s = model.shares("OTA", ratios, 4.8, offered)
            self.assertAlmostEqual(sum(s.values()), 1.0, places=12)
            self.assertEqual(set(s), set(offered))


class TestSubstitutionBehaviour(unittest.TestCase):
    def setUp(self):
        self.model = SubstitutionModel(INV, PREFS)
        self.flat = {c: 1.0 for c in INV.codes}

    def test_taste_decides_the_mix_when_prices_sit_on_the_ladder(self):
        corp = self.model.shares("CORP", self.flat, 1.6)
        ota = self.model.shares("OTA", self.flat, 4.8)
        self.assertGreater(corp["EXE"], ota["EXE"])
        self.assertGreater(ota["STD"], corp["STD"])

    def test_a_premium_on_one_type_sends_guests_down_the_ladder(self):
        dear = dict(self.flat, STE=1.30)
        before = self.model.shares("RETAIL", self.flat, 3.8)
        after = self.model.shares("RETAIL", dear, 3.8)
        self.assertLess(after["STE"], before["STE"])
        for c in ("STD", "DLX", "EXE"):
            self.assertGreater(after[c], before[c])

    def test_the_house_loses_less_than_the_type_does(self):
        """The point of the whole module, as a number.

        Charging thirty percent more for the suite costs the suite most of its
        demand.  It costs the hotel almost nothing, because those guests take
        the executive room instead.  A single resource model cannot tell the
        difference, and prices the suite as though it could.
        """
        dear = dict(self.flat, STE=1.30)
        base = self.model.split("RETAIL", 100.0, 3.8, self.flat)
        after = self.model.split("RETAIL", 100.0, 3.8, dear)
        self.assertLess(after["STE"], base["STE"] * 0.5)
        house_loss = 1.0 - sum(after.values()) / sum(base.values())
        self.assertLess(house_loss, 0.05)

    def test_withdrawing_a_type_redistributes_its_guests(self):
        full = self.model.shares("RETAIL", self.flat, 3.8)
        without = self.model.shares("RETAIL", self.flat, 3.8,
                                    offered=("STD", "EXE", "STE"))
        self.assertNotIn("DLX", without)
        for c in ("STD", "EXE", "STE"):
            self.assertGreater(without[c], full[c])

    def test_guests_move_room_more_readily_than_they_move_hotel(self):
        """The nested structure, stated as a comparison.

        Ten percent on one room type must cost that type more share than ten
        percent on the whole house costs the house.  If it did not, the nest
        would be pointless and a single elasticity would do.
        """
        k = 3.8
        one_up = dict(self.flat, DLX=1.10)
        share_loss = 1.0 - (self.model.shares("RETAIL", one_up, k)["DLX"]
                            / self.model.shares("RETAIL", self.flat, k)["DLX"])
        all_up = {c: 1.10 for c in INV.codes}
        house_loss = 1.0 - self.model.house_acceptance("RETAIL", k, all_up)
        self.assertGreater(share_loss, house_loss)


class TestRatios(unittest.TestCase):
    def test_the_ladder_is_the_origin_of_the_ratio_scale(self):
        quoted = {t.code: 189.0 * t.rate_multiplier for t in INV}
        ratios = ratios_from_rates(INV, quoted, 189.0)
        for c in INV.codes:
            self.assertAlmostEqual(ratios[c], 1.0, places=12)

    def test_a_type_priced_off_the_ladder_shows_up_alone(self):
        quoted = {t.code: 189.0 * t.rate_multiplier for t in INV}
        quoted["EXE"] *= 1.2
        ratios = ratios_from_rates(INV, quoted, 189.0)
        self.assertAlmostEqual(ratios["EXE"], 1.2, places=12)
        self.assertAlmostEqual(ratios["STD"], 1.0, places=12)


if __name__ == "__main__":
    unittest.main()
