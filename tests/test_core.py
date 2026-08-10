"""Checks for the parts where a quiet mistake would be expensive."""

import datetime as dt
import math
import random
import unittest
from statistics import fmean

from pace import plugins
from pace.calendar import default_calendar
from pace.config import DEFAULT_HOTEL, SEGMENTS
from pace.controls import authorized_capacity, closed_segments, min_length_of_stay
from pace.elasticity import PriceResponse, acceptance, local_elasticity, prior_k
from pace.ledger import Ledger
from pace.optimize import bid_price, choose_rate, value_function
from pace.otb import build_pace_curves
from pace.simulate import (Request, generate_requests, poisson, quoted_rate,
                           reference_rate, replay)
from pace.unconstrain import expected_above, project_detruncate

HOTEL = DEFAULT_HOTEL
CAL = default_calendar(range(2023, 2026))


class TestRandom(unittest.TestCase):
    def test_poisson_mean(self):
        rng = random.Random(1)
        for lam in (0.4, 3.0, 12.0, 40.0):
            draws = [poisson(rng, lam) for _ in range(4000)]
            self.assertAlmostEqual(fmean(draws), lam, delta=max(0.2, lam * 0.12))

    def test_poisson_zero(self):
        self.assertEqual(poisson(random.Random(0), 0.0), 0)


class TestPriceResponse(unittest.TestCase):
    def test_shape(self):
        self.assertAlmostEqual(acceptance(4.0, 1.0), 1.0, places=9)
        self.assertLess(acceptance(4.0, 1.4), acceptance(4.0, 1.1))
        self.assertLess(acceptance(4.0, 3.0), 0.02)
        self.assertGreater(acceptance(4.0, 0.6), 1.0)

    def test_k_is_twice_elasticity_at_reference(self):
        for k in (0.9, 3.8, 6.2):
            self.assertAlmostEqual(local_elasticity(k, 1.0), k / 2.0, places=9)

    def test_revenue_has_an_interior_maximum_even_when_inelastic(self):
        """The failure this model exists to prevent."""
        for k in (0.4, 0.9, 1.6, 4.0):
            rates = [50 + i for i in range(900)]
            best = max(rates, key=lambda r: r * acceptance(k, r / 189.0))
            self.assertLess(best, rates[-1], "k=%s walked the rate into the ceiling" % k)

    def test_contracted_segments_do_not_respond(self):
        resp = PriceResponse({c: {"k": prior_k(c), "observations": 0, "prior_k": prior_k(c)}
                              for c in SEGMENTS})
        self.assertEqual(resp.accept("CORP", 400.0, 155.0), 1.0)
        self.assertLess(resp.accept("RETAIL", 400.0, 189.0), 0.2)

    def test_quoted_rate_ignores_bar_for_contracted(self):
        self.assertEqual(quoted_rate("CORP", 400.0, 155.0), 155.0)
        self.assertAlmostEqual(quoted_rate("RETAIL", 400.0, 189.0), 400.0)


class TestUnconstrain(unittest.TestCase):
    def test_conditional_expectation_above_cut(self):
        self.assertGreater(expected_above(100.0, 20.0, 100.0), 100.0)
        self.assertGreater(expected_above(100.0, 20.0, 130.0), 130.0)

    def test_recovers_a_truncated_mean_better_than_the_naive_average(self):
        rng = random.Random(7)
        truth_mu, truth_sd, cap = 140.0, 22.0, 150.0
        truth = [rng.gauss(truth_mu, truth_sd) for _ in range(600)]
        obs = [(min(v, cap), v >= cap) for v in truth]
        naive = fmean([v for v, _ in obs])
        mu, _sd, _imp = project_detruncate(obs)
        self.assertLess(abs(mu - truth_mu), abs(naive - truth_mu))
        self.assertLess(abs(mu - truth_mu), 4.0)

    def test_uncensored_sample_is_left_alone(self):
        values = [(100.0 + i, False) for i in range(40)]
        mu, _sd, _ = project_detruncate(values)
        self.assertAlmostEqual(mu, fmean([v for v, _ in values]), places=6)


class TestOptimizer(unittest.TestCase):
    def buckets(self, n=40, p=0.2, v=150.0):
        return [[(p, v), (p / 2, v * 0.6)] for _ in range(n)]

    def test_value_function_is_monotone_and_concave(self):
        V = value_function(60, self.buckets())
        for i in range(len(V) - 1):
            self.assertLessEqual(V[i], V[i + 1] + 1e-9)
        deltas = [V[i + 1] - V[i] for i in range(len(V) - 1)]
        for i in range(len(deltas) - 1):
            self.assertGreaterEqual(deltas[i], deltas[i + 1] - 1e-9)

    def test_bid_price_falls_as_capacity_grows(self):
        b = self.buckets()
        self.assertGreater(bid_price(4, b), bid_price(20, b))
        self.assertGreaterEqual(bid_price(20, b), bid_price(200, b))

    def test_bid_price_rises_with_demand(self):
        self.assertGreater(bid_price(10, self.buckets(n=80)), bid_price(10, self.buckets(n=20)))

    def test_bid_price_never_exceeds_the_best_fare(self):
        self.assertLessEqual(bid_price(1, self.buckets(n=400)), 150.0 + 1e-6)

    def test_no_capacity_is_infinitely_valuable(self):
        self.assertEqual(bid_price(0, self.buckets()), float("inf"))

    def test_rate_never_lands_below_the_bid_price_floor(self):
        resp = PriceResponse({c: {"k": prior_k(c), "observations": 0, "prior_k": prior_k(c)}
                              for c in SEGMENTS})
        refs = {c: reference_rate(HOTEL, CAL, dt.date(2025, 2, 14), c) for c in SEGMENTS}
        remaining = {"RETAIL": 40.0, "OTA": 30.0, "CORP": 20.0, "GROUP": 5.0}
        for bid in (0.0, 80.0, 200.0, 320.0):
            out = choose_rate(HOTEL, remaining, refs, resp, bid, 25)
            net = out["rate"] * (1 - SEGMENTS["RETAIL"].commission) - HOTEL.variable_cost
            self.assertTrue(net >= bid - 1e-6 or out["rate"] >= HOTEL.rate_ceiling - 1e-6,
                            "rate %s sold under a bid price of %s" % (out["rate"], bid))


class TestControls(unittest.TestCase):
    def test_overbooking_stays_inside_its_bounds(self):
        for cancel in (0.0, 0.05, 0.15, 0.30):
            a = authorized_capacity(HOTEL, 190.0, cancel, 0.03)
            self.assertGreaterEqual(a, HOTEL.rooms)
            self.assertLessEqual(a, int(HOTEL.rooms * (1 + HOTEL.max_overbook_pct)))

    def test_overbooking_grows_with_cancellation_risk(self):
        self.assertGreaterEqual(authorized_capacity(HOTEL, 190.0, 0.25, 0.03),
                                authorized_capacity(HOTEL, 190.0, 0.02, 0.03))

    def test_no_overbooking_when_nothing_is_handed_back(self):
        self.assertEqual(authorized_capacity(HOTEL, 190.0, 0.0, 0.0), HOTEL.rooms)

    def test_minimum_stay_is_one_when_a_single_night_pays(self):
        d = dt.date(2025, 2, 14)
        rates = {d + dt.timedelta(days=i): 260.0 for i in range(6)}
        bids = {d + dt.timedelta(days=i): 40.0 for i in range(6)}
        self.assertEqual(min_length_of_stay(HOTEL, d, rates, bids), 1)

    def test_minimum_stay_reaches_into_the_soft_nights(self):
        d = dt.date(2025, 2, 14)
        rates = {d + dt.timedelta(days=i): 260.0 for i in range(6)}
        bids = {d: 600.0}
        for i in range(1, 6):
            bids[d + dt.timedelta(days=i)] = 10.0
        self.assertGreater(min_length_of_stay(HOTEL, d, rates, bids), 1)

    def test_closures_follow_the_bid_price(self):
        refs = {c: reference_rate(HOTEL, CAL, dt.date(2025, 2, 14), c) for c in SEGMENTS}
        self.assertEqual(closed_segments(HOTEL, 200.0, 0.0, refs), frozenset())
        heavy = closed_segments(HOTEL, 200.0, 400.0, refs)
        self.assertIn("GROUP", heavy)
        self.assertLess(len(heavy), len(SEGMENTS), "the highest paying door must stay open")


class TestLedger(unittest.TestCase):
    def ledger(self):
        return Ledger(HOTEL, dt.date(2025, 1, 1), dt.date(2025, 1, 31))

    def req(self, rid=1, rooms=2, los=3, seg="RETAIL"):
        return Request(rid, seg, dt.date(2024, 12, 1), dt.date(2025, 1, 10),
                       los, rooms, 400.0, -1, False)

    def test_booking_then_cancelling_returns_the_house(self):
        led = self.ledger()
        hold = led.book(dt.date(2024, 12, 1), self.req(), 210.0)
        self.assertEqual(led.rooms_on(dt.date(2025, 1, 11)), 2)
        led.cancel(hold)
        for i in range(3):
            self.assertEqual(led.rooms_on(dt.date(2025, 1, 10) + dt.timedelta(days=i)), 0)

    def test_settlement_walks_the_cheapest_business_first(self):
        led = self.ledger()
        night = dt.date(2025, 1, 10)
        led.book(dt.date(2024, 12, 1), self.req(rid=1, rooms=HOTEL.rooms, los=1), 300.0)
        led.book(dt.date(2024, 12, 1), self.req(rid=2, rooms=4, los=1, seg="GROUP"), 120.0)
        result = led.settle(night)
        self.assertEqual(result["walked"], 4)
        self.assertEqual(result["rooms_sold"], HOTEL.rooms)
        self.assertAlmostEqual(result["adr"], 300.0, places=6)


class TestPaceCurves(unittest.TestCase):
    def test_curve_closes_at_one_and_pickup_closes_at_zero(self):
        hotel = HOTEL
        reqs = generate_requests(hotel, CAL, dt.date(2023, 1, 1), dt.date(2023, 4, 30), seed=3)
        from pace.policy import LadderPolicy
        led = replay(hotel, reqs, LadderPolicy(hotel),
                     dt.date(2023, 1, 1) - dt.timedelta(days=180), dt.date(2023, 4, 30),
                     dt.date(2023, 1, 1), dt.date(2023, 4, 30), seed=7)
        done = [d for d in led.settled]
        curves = build_pace_curves(led, done, max_lead=90)
        self.assertTrue(curves.is_ready())
        for key in curves.ratio:
            self.assertAlmostEqual(curves.ratio[key][0], 1.0, places=6)
            self.assertAlmostEqual(curves.pickup[key][0], 0.0, places=6)
        self.assertGreater(curves.ratio_at(("__house__", -1), 0),
                           curves.ratio_at(("__house__", -1), 60))

    def test_segments_book_on_different_clocks(self):
        hotel = HOTEL
        reqs = generate_requests(hotel, CAL, dt.date(2023, 1, 1), dt.date(2023, 6, 30), seed=3)
        from pace.policy import LadderPolicy
        led = replay(hotel, reqs, LadderPolicy(hotel),
                     dt.date(2023, 1, 1) - dt.timedelta(days=180), dt.date(2023, 6, 30),
                     dt.date(2023, 1, 1), dt.date(2023, 6, 30), seed=7)
        curves = build_pace_curves(led, list(led.settled), max_lead=120)
        # At three weeks out most group business is signed and most corporate is not.
        self.assertLess(curves.segment_still_to_come("GROUP", 21),
                        curves.segment_still_to_come("CORP", 21))


class TestPlugins(unittest.TestCase):
    def setUp(self):
        self._signals = list(plugins._SIGNALS)
        self._rules = list(plugins._RULES)
        plugins.reset()

    def tearDown(self):
        plugins.reset()
        plugins._SIGNALS.extend(self._signals)
        plugins._RULES.extend(self._rules)

    def test_signals_compose_and_neutral_ones_are_dropped(self):
        plugins.signal("a")(lambda d, ctx: 1.2)
        plugins.signal("b")(lambda d, ctx: 1.5)
        plugins.signal("quiet")(lambda d, ctx: 1.0)
        plugins.signal("absent")(lambda d, ctx: None)
        got = plugins.collect_signals(dt.date(2025, 2, 1), {})
        self.assertEqual(len(got), 2)
        self.assertAlmostEqual(plugins.signal_multiplier(dt.date(2025, 2, 1), {}), 1.8)

    def test_rules_run_in_priority_order_and_leave_a_trace(self):
        from pace.recommendation import Recommendation
        plugins.rule("second", priority=50)(lambda rec, ctx: setattr(rec, "rate", rec.rate + 5) or rec)
        plugins.rule("first", priority=10)(lambda rec, ctx: setattr(rec, "rate", 200.0) or rec)
        rec = Recommendation(dt.date(2025, 2, 1), dt.date(2025, 1, 1), 31, 0, 150, 150,
                             100.0, 100.0, 0.0, "demand", 0, 0, 0, 0, 0, 1.0, 1.0)
        rec = plugins.apply_rules(rec, {})
        self.assertEqual(rec.rate, 205.0)
        self.assertEqual(len(rec.rule_trace), 2)
        self.assertTrue(rec.rule_trace[0].startswith("first"))


class TestRateExperiment(unittest.TestCase):
    def setUp(self):
        from pace.experiment import RateExperiment
        self.exp = RateExperiment(salt="test")

    def test_assignment_depends_on_the_date_and_nothing_else(self):
        from pace.experiment import RateExperiment
        d = dt.date(2025, 3, 4)
        self.assertEqual(self.exp.arm(d), RateExperiment(salt="test").arm(d))

    def test_assignment_is_balanced(self):
        seen = {}
        for i in range(3000):
            a = self.exp.arm(dt.date(2024, 1, 1) + dt.timedelta(days=i))
            seen[a] = seen.get(a, 0) + 1
        self.assertGreaterEqual(len(seen), 5)
        treated = sum(v for k, v in seen.items() if k != 0.0)
        self.assertGreater(treated / 3000, 0.25)
        self.assertLess(treated / 3000, 0.55)

    def test_a_night_forecast_full_is_left_alone(self):
        d = dt.date(2025, 3, 4)
        rate, arm = self.exp.apply(200.0, d, 30, True, 100.0, HOTEL.rate_ladder())
        self.assertEqual(arm, 0.0)
        self.assertEqual(rate, 200.0)

    def test_outside_the_lead_window_is_left_alone(self):
        d = dt.date(2025, 3, 4)
        self.assertEqual(self.exp.apply(200.0, d, 1, False, 100.0, HOTEL.rate_ladder())[1], 0.0)
        self.assertEqual(self.exp.apply(200.0, d, 900, False, 100.0, HOTEL.rate_ladder())[1], 0.0)

    def test_the_experiment_never_sells_below_the_bid_price_floor(self):
        ladder = HOTEL.rate_ladder()
        for i in range(400):
            d = dt.date(2024, 1, 1) + dt.timedelta(days=i)
            rate, _arm = self.exp.apply(200.0, d, 30, False, 197.0, ladder)
            self.assertGreaterEqual(rate, 197.0 - HOTEL.rate_step)

    def test_a_weak_instrument_is_refused_rather_than_used(self):
        from pace.experiment import MIN_INSTRUMENT_SPREAD, instrument_spread
        flat = [(("peak", 3), a, 0.90, 4.0) for a in (-0.16, 0.0, 0.16) for _ in range(10)]
        self.assertLess(instrument_spread(flat), MIN_INSTRUMENT_SPREAD)
        wide = ([(("peak", 3), -0.16, 0.80, 4.2)] * 10 + [(("peak", 3), 0.0, 0.90, 4.0)] * 10
                + [(("peak", 3), 0.16, 1.00, 3.7)] * 10)
        self.assertGreater(instrument_spread(wide), MIN_INSTRUMENT_SPREAD)

    def test_only_floating_segments_have_a_true_elasticity_to_recover(self):
        from pace.experiment import true_local_elasticity
        self.assertGreater(true_local_elasticity("RETAIL"), 1.0)
        self.assertGreater(true_local_elasticity("OTA"), 1.0)
        self.assertEqual(true_local_elasticity("CORP"), 0.0)


class TestRevenueSurface(unittest.TestCase):
    def test_the_surface_is_flat_near_its_own_optimum(self):
        """Why elasticity precision buys less than it looks like it should.

        On a single floating segment at a reference of 200, every rate inside
        a band of at least twelve dollars returns 99% of the best available
        contribution.  With the real segment mix, where nearly forty percent
        of the business is contracted and does not respond to the rate at all,
        the band is wider still.

        If this ever fails, the rate decision has become sharp and getting the
        price response right starts to matter a great deal more.
        """
        from pace.elasticity import acceptance
        ladder = HOTEL.rate_ladder()
        ref = 200.0

        def contribution(r, k):
            return acceptance(k, r / ref) * (r - HOTEL.variable_cost)

        best = max(ladder, key=lambda r: contribution(r, 4.2))
        peak = contribution(best, 4.2)
        within = [r for r in ladder if contribution(r, 4.2) >= 0.99 * peak]
        self.assertGreaterEqual(max(within) - min(within), 12.0)


class TestEndToEnd(unittest.TestCase):
    def test_the_engine_prices_a_short_horizon_without_falling_over(self):
        from pace.policy import HandoverPolicy, LadderPolicy, PaceEngine
        hotel = HOTEL
        handover = dt.date(2023, 10, 1)
        today = dt.date(2023, 11, 15)
        reqs = generate_requests(hotel, CAL, dt.date(2023, 1, 1), dt.date(2023, 12, 31), seed=11)
        eng = PaceEngine(hotel, CAL, warmup_nights=120)
        led = replay(hotel, reqs, HandoverPolicy(LadderPolicy(hotel), eng, handover),
                     dt.date(2023, 1, 1) - dt.timedelta(days=180), today,
                     dt.date(2023, 1, 1), dt.date(2023, 12, 31), seed=7)
        self.assertTrue(eng.ready)
        k = led.kpi(dt.date(2023, 10, 1), today)
        self.assertGreater(k["room_revenue"], 0)
        self.assertLessEqual(k["occupancy"], 1.0)
        rec = eng.recommend(led, today + dt.timedelta(days=30), today)
        self.assertGreaterEqual(rec.rate, hotel.rate_floor)
        self.assertLessEqual(rec.rate, hotel.rate_ceiling)
        self.assertGreaterEqual(rec.authorized, hotel.rooms)


if __name__ == "__main__":
    unittest.main()
