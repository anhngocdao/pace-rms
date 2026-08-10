"""Checks for the multi room type network layer.

The single resource engine is already measured.  What these tests defend is
the promise that adding room types cannot quietly change what that engine
does, and that when it does change something the change is substitution and
not arithmetic drift.
"""

import random
import unittest

from pace.choice import SubstitutionModel, ratios_from_rates
from pace.config import DEFAULT_HOTEL
from pace.elasticity import acceptance
from pace.network import (NetworkInstance, Product, allocate,
                          decomposed_bid_prices, dual_prices,
                          independent_bid_prices, stay_bid)
from pace.optimize import bid_price_static, demand_sd
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


def one_night(capacity, classes, room="STD"):
    """A one cell instance, which is the single resource problem restated."""
    inst = NetworkInstance({(room, 0): float(capacity)})
    for i, (demand, value) in enumerate(classes):
        inst.add(Product("S%d" % i, room, 0, 1, value, demand, ((room, 0),)))
    return inst


def uniform_nights(nights, capacity, room="STD"):
    return NetworkInstance({(room, n): float(capacity) for n in range(nights)})


class TestDualPrices(unittest.TestCase):
    def test_a_room_nobody_is_competing_for_is_free(self):
        sol = dual_prices(one_night(100, [(40, 150.0), (20, 120.0)]))
        self.assertAlmostEqual(sol.prices[("STD", 0)], 0.0, places=12)

    def test_the_last_room_in_the_house_is_worth_the_best_request(self):
        sol = dual_prices(one_night(0, [(40, 150.0), (20, 120.0)]))
        self.assertAlmostEqual(sol.prices[("STD", 0)], 150.0, places=9)

    def test_the_price_is_the_request_that_filled_the_night(self):
        sol = dual_prices(one_night(100, [(140, 150.0), (30, 120.0)]))
        self.assertAlmostEqual(sol.prices[("STD", 0)], 150.0, places=9)

    def test_a_slack_night_carries_none_of_the_stay(self):
        inst = uniform_nights(2, 100)
        inst.add(Product("R", "STD", 0, 1, 150.0, 105.0, (("STD", 0),)))
        inst.add(Product("R", "STD", 1, 1, 150.0, 40.0, (("STD", 1),)))
        inst.add(Product("R", "STD", 0, 2, 300.0, 20.0, (("STD", 0), ("STD", 1))))
        sol = dual_prices(inst)
        self.assertGreater(sol.prices[("STD", 0)], 100.0)
        self.assertAlmostEqual(sol.prices[("STD", 1)], 0.0, places=12)
        self.assertTrue(sol.accepts(inst.products[2]))

    def test_the_solver_reports_how_far_from_optimal_it_is(self):
        """A gap that is asserted rather than measured is a gap nobody knows."""
        rng = random.Random(4)
        cap = {("T%d" % t, n): float(rng.randint(20, 90))
               for t in range(3) for n in range(14)}
        inst = NetworkInstance(cap)
        for _ in range(400):
            t = "T%d" % rng.randint(0, 2)
            a = rng.randint(0, 13)
            cells = tuple((t, n) for n in range(a, min(14, a + rng.randint(1, 5))))
            inst.add(Product("S", t, a, len(cells),
                             rng.uniform(90, 240) * len(cells),
                             rng.uniform(0.5, 6.0), cells))
        sol = dual_prices(inst)
        self.assertGreaterEqual(sol.dual_objective, sol.primal_value)
        self.assertLess(sol.gap, 0.02)
        self.assertLess(sol.passes, 60)

    def test_a_night_that_is_charged_for_is_a_night_that_fills(self):
        """Complementary slackness, which is the whole meaning of a dual price.

        Charging for a room the hotel is not going to run out of would be a
        straightforward error, and it is the error a bid price is defined to
        avoid.

        Counted weakly, because the request that sets the price clears it by
        exactly nothing.  That is not a rounding concession: it is where the
        price comes from.  The dual sits at the value of the marginal request,
        which the linear program then serves fractionally, so a strict
        comparison excludes precisely the request that fills the night.
        """
        rng = random.Random(11)
        inst = uniform_nights(6, 60)
        for _ in range(120):
            a = rng.randint(0, 5)
            cells = tuple(("STD", n) for n in range(a, min(6, a + rng.randint(1, 4))))
            inst.add(Product("S", "STD", a, len(cells),
                             rng.uniform(100, 260) * len(cells),
                             rng.uniform(0.5, 8.0), cells))
        sol = dual_prices(inst)
        for cell, price in sol.prices.items():
            if price <= 1e-6:
                continue
            clearing = sum(p.demand for p in inst.products
                           if cell in p.cells and p.value >= sol.stay_bid(p) - 1e-6)
            self.assertGreaterEqual(clearing, inst.capacity[cell] - 1e-6,
                                    "%s is charged %.2f but does not fill" % (cell, price))


class TestDecompositionReducesToTheOldModel(unittest.TestCase):
    def test_one_night_one_type_is_the_closed_form_exactly(self):
        classes = [(140.0, 150.0), (30.0, 120.0), (12.0, 96.0)]
        inst = one_night(100, classes)
        want = bid_price_static(100, [(d, demand_sd(d), v) for d, v in classes])
        got = decomposed_bid_prices(inst)[("STD", 0)]
        self.assertAlmostEqual(got, want, places=12)
        self.assertAlmostEqual(independent_bid_prices(inst)[("STD", 0)], want, places=12)


class TestAllocation(unittest.TestCase):
    def test_the_parts_sum_to_the_whole(self):
        """The property that keeps a long stay from paying for itself twice."""
        rng = random.Random(2)
        for _ in range(200):
            los = rng.randint(1, 7)
            cells = tuple(("STD", n) for n in range(los))
            duals = {c: (rng.uniform(0.0, 200.0) if rng.random() < 0.7 else 0.0)
                     for c in cells}
            value = rng.uniform(80.0, 1400.0)
            parts = allocate(value, los, cells, duals)
            self.assertAlmostEqual(sum(parts.values()), value, places=9)

    def test_value_lands_on_the_night_that_is_scarce(self):
        cells = (("STD", 0), ("STD", 1))
        parts = allocate(300.0, 2, cells, {("STD", 0): 150.0, ("STD", 1): 0.0})
        self.assertAlmostEqual(parts[("STD", 0)], 300.0, places=9)
        self.assertAlmostEqual(parts[("STD", 1)], 0.0, places=9)

    def test_equally_scarce_nights_prorate_evenly(self):
        cells = tuple(("STD", n) for n in range(4))
        even = allocate(400.0, 4, cells, {c: 90.0 for c in cells})
        none = allocate(400.0, 4, cells, {c: 0.0 for c in cells})
        for c in cells:
            self.assertAlmostEqual(even[c], 100.0, places=9)
            self.assertAlmostEqual(none[c], 100.0, places=9)


class TestWhereTheAdditiveApproximationBreaks(unittest.TestCase):
    """The defect this whole module exists to expose.

    Five nights, every one of them wanted by more one night guests than there
    are rooms.  A five night stay worth 620 displaces five one night guests
    worth 750, so it should be refused.  The deterministic network says so.
    Both additive forms accept it, because each night charges only its own
    expected shortage and a stay that needs all five nights at once is
    charged as though the five shortages were separate events.
    """

    def setUp(self):
        self.cells = tuple(("STD", n) for n in range(5))
        self.inst = uniform_nights(5, 100)
        for n in range(5):
            self.inst.add(Product("R", "STD", n, 1, 150.0, 105.0, (("STD", n),)))
        self.inst.add(Product("R", "STD", 0, 5, 620.0, 10.0, self.cells))
        self.long = self.inst.products[-1]
        self.duals = dual_prices(self.inst)

    def test_the_deterministic_network_refuses_the_long_stay(self):
        self.assertAlmostEqual(self.duals.prices[("STD", 0)], 150.0, places=9)
        self.assertAlmostEqual(self.duals.stay_bid(self.long), 750.0, places=6)
        self.assertFalse(self.duals.accepts(self.long))

    def test_both_additive_forms_accept_it(self):
        for prices in (decomposed_bid_prices(self.inst, self.duals),
                       independent_bid_prices(self.inst)):
            self.assertLess(stay_bid(prices, self.cells), self.long.value)

    def test_prorating_evenly_is_what_the_engine_already_does(self):
        """With every night equally scarce the two additive forms coincide.

        That is the design working, not a coincidence.  The network can only
        differ from the single resource engine where the nights differ, so a
        flat week has to reproduce the existing answer exactly.
        """
        dec = decomposed_bid_prices(self.inst, self.duals)
        ind = independent_bid_prices(self.inst)
        for cell in self.cells:
            self.assertAlmostEqual(dec[cell], ind[cell], places=9)


if __name__ == "__main__":
    unittest.main()
