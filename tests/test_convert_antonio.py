import datetime as dt
import json
import os
import statistics
import tempfile
import unittest
from collections import Counter
from unittest import mock

from tools import convert_antonio as CA
from pace import hotelconfig as HC

SETTINGS = {"group_threshold_rooms": 10, "undefined_stop_share": 0.01, "seed": 20250115}


def _arow(**kw):
    base = dict(hotel="Resort Hotel", is_canceled="0", lead_time="30", arrival_date_year="2016",
                arrival_date_month="July", arrival_date_week_number="28", arrival_date_day_of_month="10",
                stays_in_weekend_nights="1", stays_in_week_nights="2", adults="2", children="0", babies="0",
                meal="BB", country="PRT", market_segment="Direct", distribution_channel="Direct",
                is_repeated_guest="0", previous_cancellations="0", previous_bookings_not_canceled="0",
                reserved_room_type="A", assigned_room_type="A", booking_changes="0", deposit_type="No Deposit",
                agent="NULL", company="NULL", days_in_waiting_list="0", customer_type="Transient", adr="120.0",
                required_car_parking_spaces="0", total_of_special_requests="0",
                reservation_status="Check-Out", reservation_status_date="2016-07-13")
    base.update(kw)
    return base


class RowBasics(unittest.TestCase):
    def test_arrival_and_booked_on(self):
        out, _, _ = CA.branch_rows([_arow()], SETTINGS)
        self.assertEqual(out[0]["arrival"], "2016-07-10")
        self.assertEqual(out[0]["booked_on"], "2016-06-10")
        self.assertEqual(out[0]["nights"], "3")
        self.assertEqual(out[0]["booking_id"], "H1-000001")

    def test_status_comes_from_reservation_status_not_is_canceled(self):
        self.assertEqual(CA.status_of(_arow(reservation_status="No-Show", is_canceled="1")), ("no_show", ""))
        self.assertEqual(CA.status_of(_arow(reservation_status="Canceled", is_canceled="1",
                                            reservation_status_date="2016-06-20")), ("cancelled", "2016-06-20"))
        self.assertEqual(CA.status_of(_arow()), ("stayed", ""))

    def test_day_use_kept(self):
        out, _, _ = CA.branch_rows([_arow(stays_in_weekend_nights="0", stays_in_week_nights="0")], SETTINGS)
        self.assertEqual(out[0]["nights"], "0")

    def test_city_hotel_code_and_duplicates_kept(self):
        out, _, _ = CA.branch_rows([_arow(hotel="City Hotel"), _arow(hotel="City Hotel")], SETTINGS)
        self.assertEqual([o["booking_id"] for o in out], ["H2-000001", "H2-000002"])


class RuleOrder(unittest.TestCase):
    def _branches(self, rows, settings=SETTINGS):
        out, counts, _ = CA.branch_rows(rows, settings)
        return [o["_branch"] for o in out], counts

    def test_complementary_is_comp_even_with_positive_adr(self):
        b, _ = self._branches([_arow(market_segment="Complementary", adr="50")])
        self.assertEqual(b, ["COMP"])

    def test_groups_and_offline_group_are_group_branches(self):
        b, _ = self._branches([_arow(market_segment="Groups"), _arow(market_segment="Offline TA/TO", customer_type="Group")])
        self.assertEqual(b, ["GROUPS", "OFFLINE_TO_GROUP"])

    def test_adr_zero_on_group_member_keeps_group(self):
        b, _ = self._branches([_arow(market_segment="Groups", adr="0")])
        self.assertEqual(b, ["GROUPS"])

    def test_adr_zero_on_stayed_direct_is_adr0_but_not_on_cancelled(self):
        b, _ = self._branches([_arow(adr="0"), _arow(adr="0", reservation_status="Canceled", is_canceled="1")])
        self.assertEqual(b, ["ADR0", "DIRECT"])

    def test_adr_zero_day_use_is_not_adr0(self):
        b, _ = self._branches([_arow(adr="0", stays_in_weekend_nights="0", stays_in_week_nights="0")])
        self.assertEqual(b, ["DIRECT"])

    def test_adr_zero_on_offline_to_group_member_keeps_group(self):
        b, _ = self._branches([_arow(market_segment="Offline TA/TO", customer_type="Group", adr="0")])
        self.assertEqual(b, ["OFFLINE_TO_GROUP"])

    def test_adr_zero_no_show_is_adr0(self):
        b, _ = self._branches([_arow(adr="0", reservation_status="No-Show", is_canceled="1")])
        self.assertEqual(b, ["ADR0"])

    def test_table_branches(self):
        rows = [_arow(market_segment="Online TA"), _arow(market_segment="Corporate"), _arow(market_segment="Aviation"),
                _arow(market_segment="Offline TA/TO", customer_type="Contract"),
                _arow(market_segment="Offline TA/TO", customer_type="Transient")]
        b, _ = self._branches(rows)
        self.assertEqual(b, ["ONLINE_TA", "CORPORATE", "AVIATION", "OFFLINE_TO_CONTRACT", "OFFLINE_TO_TRANSIENT"])

    def test_undefined_goes_by_channel(self):
        rows = [_arow(market_segment="Undefined", distribution_channel="TA/TO"),
                _arow(market_segment="Undefined", distribution_channel="Undefined")]
        # Both rows are "stayed" by default, and SETTINGS caps the undefined
        # share of stayed room nights at 1%: two Undefined rows out of two
        # stayed rows is 100% and would legitimately trip ConvertStop before
        # ever reaching the assertions below. This test is about branch
        # routing by channel, not the stop threshold, so it relaxes the share
        # limit for its own call instead of touching the expected branches.
        b, counts = self._branches(rows, settings=dict(SETTINGS, undefined_stop_share=1.0))
        self.assertEqual(b, ["UNDEFINED_CH_TATO", "UNDEFINED_FALLBACK"])
        self.assertEqual(counts["UNDEFINED_FALLBACK"], 1)

    def test_undefined_over_one_percent_of_room_nights_stops(self):
        rows = [_arow(market_segment="Undefined") for _ in range(5)] + [_arow() for _ in range(5)]
        with self.assertRaises(CA.ConvertStop):
            CA.branch_rows(rows, SETTINGS)


class TransientParty(unittest.TestCase):
    def _tp(self, n, **kw):
        return [_arow(customer_type="Transient-party", market_segment="Offline TA/TO", **kw) for _ in range(n)]

    def test_cluster_at_threshold_becomes_group(self):
        out, _, _ = CA.branch_rows(self._tp(10, agent="9", company="NULL"), SETTINGS)
        self.assertTrue(all(o["_branch"] == "TP_CLUSTER" for o in out))

    def test_below_threshold_is_treated_as_transient(self):
        out, _, _ = CA.branch_rows(self._tp(9, agent="9"), SETTINGS)
        self.assertTrue(all(o["_branch"] == "OFFLINE_TO_TRANSIENT" for o in out))

    def test_agent_present_company_empty_clusters(self):
        rows = self._tp(10, agent="9", company="NULL")
        self.assertEqual(len(CA.cluster_transient_party(rows, 10)), 10)

    def test_both_empty_never_clusters(self):
        rows = self._tp(10, agent="NULL", company="NULL")
        self.assertEqual(CA.cluster_transient_party(rows, 10), set())

    def test_different_lead_or_segment_splits_the_cluster(self):
        rows = self._tp(6, agent="9") + self._tp(6, agent="9", lead_time="31")
        self.assertEqual(CA.cluster_transient_party(rows, 10), set())

    def test_adr_zero_on_transient_party_cluster_member_keeps_group(self):
        # A real master folio charges the leader and not the members, so a
        # cluster mixes zero-rate and normal-rate rows and all of it must
        # still land in TP_CLUSTER, none of it in ADR0.
        rows = self._tp(5, agent="9", company="NULL", adr="0") + self._tp(5, agent="9", company="NULL")
        out, _, _ = CA.branch_rows(rows, SETTINGS)
        self.assertTrue(all(o["_branch"] == "TP_CLUSTER" for o in out))


class ChannelMajorityWindow(unittest.TestCase):
    def test_majority_by_channel_reads_only_the_warm_up_window(self):
        # Inside the warm-up window, GDS-channel rows are mostly Corporate
        # (target CORP). Outside it (after 2016-06-30), the same channel is
        # mostly Direct (target RETAIL) instead. Task 11's rule is that a
        # derived fact is read from the first twelve settled months and then
        # frozen, so the derived mapping must follow the in-window majority
        # (CORP) and must not flip because of what happens after scoring
        # starts, even though the out-of-window rows outnumber the in-window
        # ones here.
        rows = [_arow(market_segment="Corporate", distribution_channel="GDS",
                       arrival_date_year="2016", arrival_date_month="January") for _ in range(5)]
        rows += [_arow(market_segment="Direct", distribution_channel="GDS",
                        arrival_date_year="2016", arrival_date_month="August") for _ in range(10)]
        out, _, _ = CA.branch_rows(rows, SETTINGS)
        majority = CA._majority_target_by_channel(out)
        self.assertEqual(majority["GDS"], "CORP")


class Basket(unittest.TestCase):
    def _rows(self):
        rows = []
        for m in range(7, 13):
            for k in range(40):
                rows.append(_arow(arrival_date_year="2015", arrival_date_month=list(CA.MONTHS)[m - 1],
                                  adr=str(100 + 10 * (m - 6) + (k % 3)), adults="2"))
        for m in range(1, 7):
            for k in range(40):
                rows.append(_arow(arrival_date_year="2016", arrival_date_month=list(CA.MONTHS)[m - 1],
                                  adr=str(100 + 10 * m + (k % 3)), adults="2"))
        # The three rows below must land inside the warm-up window (they
        # used to default to 2016-07-10, one day past WARMUP's end, which
        # made basket() drop them by date rather than by the filter each
        # comment names) and the family row must sit at an ordinary price
        # (500 was itself outside the p1..p99 band, so it was being trimmed
        # by the price filter, not the adults filter its comment names).
        rows.append(_arow(arrival_date_year="2016", arrival_date_month="June", adr="9999"))       # extreme, trimmed by p99
        rows.append(_arow(arrival_date_year="2016", arrival_date_month="June", adr="-5"))         # negative, dropped
        rows.append(_arow(arrival_date_year="2016", arrival_date_month="June", adults="4", adr="140"))  # family, out of basket
        out, _, _ = CA.branch_rows(rows, SETTINGS)
        return out

    def test_basket_filters(self):
        out = self._rows()
        rows, widened = CA.basket(out, {"DIRECT"}, min_rows=30)
        self.assertFalse(widened)
        self.assertTrue(all(float(r["rate"]) > 0 for r in rows))
        self.assertTrue(all(r["_raw"]["adults"] == "2" for r in rows))
        self.assertLess(max(float(r["rate"]) for r in rows), 9999)

    def test_factors_have_mean_one_and_base_rate_matches_by_construction(self):
        # This fixture deliberately widens February (its two-adult rows are
        # replaced with a smaller three-adult pool, the same trick as
        # test_thin_month_widens_adults_filter below). factors[m] is always
        # med[m] / mean by construction, so med[m] / factors[m] collapses to
        # exactly mean for any month whose med[m] agrees with the med used to
        # build factors, which every one of these twelve months' does once
        # both functions read _monthly_basket_medians: this checks that
        # identity holds over the full, all-twelve-months set, and that
        # price_month_factor's own widened list matches the shared helper's.
        # It would hold just as well for eleven months as for twelve, since
        # a median of otherwise-identical values does not move when one
        # entry is missing, so it cannot by itself prove base_rate reuses
        # the per-month decision rather than rebuilding its own; that is
        # what test_base_rate_reuses_price_month_factors_shared_helper below
        # is for.
        out = self._rows()
        out = [o for o in out if not (o["arrival"].startswith("2016-02") and o["_raw"]["adults"] == "2")]
        out += [dict(o, arrival="2016-02-10", _raw=dict(o["_raw"], adults="3")) for o in out[:35]]
        settings = dict(SETTINGS, min_basket_rows=30)
        factors, widened = CA.price_month_factor(out, settings)
        self.assertIn(2, widened)
        self.assertAlmostEqual(sum(factors.values()) / 12, 1.0, places=6)
        br = CA.base_rate(out, factors, settings)
        med, med_widened = CA._monthly_basket_medians(out, settings)
        self.assertEqual(sorted(med), list(range(1, 13)))
        self.assertEqual(med_widened, widened)
        self.assertAlmostEqual(br, statistics.median(med[m] / factors[m] for m in med), places=6)

    def test_base_rate_reuses_price_month_factors_shared_helper(self):
        # base_rate's own median-of-ratios collapses to the same constant
        # regardless of whether a widened month is missing or present (see
        # the note above), so no fixture built purely from _arow rows and
        # checked only by base_rate's return value can prove it reuses
        # price_month_factor's per-month widen decisions rather than
        # rebuilding them from a fresh whole-window basket() call: dropping
        # or mis-computing a minority of twelve near-identical ratios never
        # moves their median. What can be checked directly, since
        # _monthly_basket_medians is the one place either function may
        # compute a month's basket median, is that base_rate actually calls
        # it, the same way price_month_factor does, instead of calling
        # basket() itself for the whole window and losing a widened month's
        # entry in the process (which is exactly the bug: base_rate used to
        # call basket(out_rows, BAR_BRANCHES, settings=settings) directly).
        out = self._rows()
        settings = dict(SETTINGS, min_basket_rows=30)
        factors, _ = CA.price_month_factor(out, settings)
        with mock.patch("tools.convert_antonio._monthly_basket_medians",
                        wraps=CA._monthly_basket_medians) as spy:
            CA.base_rate(out, factors, settings)
        spy.assert_called_once_with(out, settings)

    def test_thin_month_widens_adults_filter(self):
        out = self._rows()
        out = [o for o in out if not (o["arrival"].startswith("2016-02") and o["_raw"]["adults"] == "2")]
        out += [dict(o, arrival="2016-02-10", _raw=dict(o["_raw"], adults="3")) for o in out[:35]]
        factors, widened = CA.price_month_factor(out, dict(SETTINGS, min_basket_rows=30))
        self.assertIn(2, widened)

    def test_rate_step_has_about_ninety_rungs_and_a_one_euro_floor(self):
        self.assertEqual(CA.rate_step(50.0, 500.0), 5.0)
        self.assertEqual(CA.rate_step(40.0, 100.0), 1.0)
        self.assertEqual(CA.rate_step(40.0, 220.0), 2.0)


class MaxLeadFloor(unittest.TestCase):
    def _rows(self):
        rows = []
        for m in range(7, 13):
            rows += [_arow(arrival_date_year="2015", arrival_date_month=list(CA.MONTHS)[m - 1],
                           adr=str(100 + m)) for _ in range(20)]
        for m in range(1, 7):
            rows += [_arow(arrival_date_year="2016", arrival_date_month=list(CA.MONTHS)[m - 1],
                           adr=str(100 + m)) for _ in range(20)]
        out, _, _ = CA.branch_rows(rows, SETTINGS)
        return out

    def test_h1_max_lead_is_floored_at_120_but_h2_is_not(self):
        # Every row in this fixture keeps _arow's default lead_time="30", so
        # the raw p99 lead time is 30 for both hotels. The 120-day mark the
        # pilot plan runs against only works if H1 is floored up to at least
        # 120; H2 has no such floor and should keep the raw, unfloored value.
        out = self._rows()
        settings = dict(SETTINGS, min_basket_rows=15, min_ratio_rows=10, floor_ceiling_widen=0.10,
                        sellout_threshold=0.95, ota_commission_main=0.15, variable_cost_low_share=0.35,
                        bar_corr_threshold=0.6)
        derived_h1 = CA.derive_hotel_json(out, "H1", settings)
        self.assertEqual(derived_h1["max_lead"], 120)
        self.assertTrue(any("120" in n for n in derived_h1["_derivation"]["notes"]))
        derived_h2 = CA.derive_hotel_json(out, "H2", settings)
        self.assertEqual(derived_h2["max_lead"], 30)


class Bands(unittest.TestCase):
    def test_top_four_three_five_split(self):
        rows = []
        for m in range(1, 13):
            n = 5 + m   # more demand later in the year
            rows += [_arow(arrival_date_year="2016" if m <= 6 else "2015", arrival_date_month=list(CA.MONTHS)[m - 1]) for _ in range(n)]
        # January additionally carries a heavy load of ordinary
        # cancellations (not Non Refund, so _gross_nights still counts
        # them). Gross demand counts a cancelled request the same as a
        # stay; occupancy counts only rows that actually stayed. With every
        # row a plain stay, the two series would count exactly the same
        # room nights and could never disagree, so a mistaken implementation
        # that ranked bands from occupancy instead of gross demand (exactly
        # the mistake the design warns against) would pass unnoticed. This
        # pushes January to the top of gross demand while its occupancy
        # stays at the bottom, which is the situation the design says to
        # rank by gross for.
        rows += [_arow(arrival_date_year="2016", arrival_date_month="January",
                       reservation_status="Canceled", is_canceled="1") for _ in range(200)]
        out, _, _ = CA.branch_rows(rows, SETTINGS)
        gross, occ, _ = CA.demand_season_band(out)
        labels = Counter(gross.values())
        self.assertEqual((labels["peak"], labels["shoulder"], labels["trough"]), (4, 3, 5))
        self.assertEqual(gross[12], "peak")
        self.assertEqual(gross[1], "peak")     # the cancellations, not the stays, earn January this
        self.assertEqual(occ[1], "trough")     # occupancy only counts the few rows that actually stayed
        self.assertNotEqual(gross[1], occ[1])

    def test_non_refund_excluded_from_gross(self):
        rows = [_arow(arrival_date_year="2015", arrival_date_month="August") for _ in range(5)]
        rows += [_arow(arrival_date_year="2016", arrival_date_month="January", deposit_type="Non Refund",
                       reservation_status="Canceled", is_canceled="1") for _ in range(50)]
        out, _, _ = CA.branch_rows(rows, SETTINGS)
        _, _, gross_by_month = CA.demand_season_band(out)
        self.assertEqual(gross_by_month[1], 0)


class Ratios(unittest.TestCase):
    def test_corp_ratio_is_weighted_median_of_monthly_ratios(self):
        rows = []
        for m in ("July", "August", "September"):
            rows += [_arow(arrival_date_year="2015", arrival_date_month=m, adr="200") for _ in range(35)]
            rows += [_arow(arrival_date_year="2015", arrival_date_month=m, market_segment="Corporate", adr="160") for _ in range(35)]
            rows += [_arow(arrival_date_year="2015", arrival_date_month=m, market_segment="Offline TA/TO",
                           customer_type="Contract", adr="120") for _ in range(35)]
        out, _, _ = CA.branch_rows(rows, SETTINGS)
        ratios, per_origin, _ = CA.segment_rate_ratio(out, dict(SETTINGS, min_ratio_rows=30))
        self.assertAlmostEqual(per_origin["CORPORATE"], 0.8, places=3)
        self.assertAlmostEqual(per_origin["OFFLINE_TO_CONTRACT"], 0.6, places=3)
        self.assertAlmostEqual(ratios["CORP"], 0.7, places=3)   # equal room nights, weighted median of the two origins

    def test_corp_ratio_weights_by_room_nights_not_by_row_count(self):
        # Same per-origin ratios as above (0.8 and 0.6), but this time
        # CORPORATE is all one-night stays and OFFLINE_TO_CONTRACT is all
        # nine-night stays, so the two origins carry the same row count but
        # wildly different room-nights (105 vs 945). A weighted median that
        # actually weights by room-nights must land on OFFLINE_TO_CONTRACT's
        # own ratio, not on the row-count-weighted (here: equal-row-count)
        # plain average of 0.7 the tie-break case above exercises.
        rows = []
        for m in ("July", "August", "September"):
            rows += [_arow(arrival_date_year="2015", arrival_date_month=m, adr="200") for _ in range(35)]
            rows += [_arow(arrival_date_year="2015", arrival_date_month=m, market_segment="Corporate", adr="160",
                           stays_in_weekend_nights="0", stays_in_week_nights="1") for _ in range(35)]
            rows += [_arow(arrival_date_year="2015", arrival_date_month=m, market_segment="Offline TA/TO",
                           customer_type="Contract", adr="120",
                           stays_in_weekend_nights="3", stays_in_week_nights="6") for _ in range(35)]
        out, _, _ = CA.branch_rows(rows, SETTINGS)
        ratios, per_origin, _ = CA.segment_rate_ratio(out, dict(SETTINGS, min_ratio_rows=30))
        self.assertAlmostEqual(per_origin["CORPORATE"], 0.8, places=3)
        self.assertAlmostEqual(per_origin["OFFLINE_TO_CONTRACT"], 0.6, places=3)
        self.assertAlmostEqual(ratios["CORP"], 0.6, places=3)


class BarTest(unittest.TestCase):
    def _rows(self, contract_step):
        rows = []
        # Six months of four weeks each, July through December 2015: the
        # original range(27, 53) ran to 26 weeks, so the last iteration
        # computed month 13 and crashed list(CA.MONTHS)[12]. range(27, 51)
        # is the 24 weeks the day-of-month math and the comment both need.
        for week in range(27, 51):
            year, month = "2015", CA.MONTHS  # arrival built from week: use day-of-month cycling within July..December
            m = 7 + (week - 27) // 4
            d = 1 + ((week - 27) % 4) * 7
            bar = 150 + 20 * ((week % 4) - 1.5)          # moves within the month
            to = 120 + (10 if contract_step and week % 4 >= 2 else 0)   # steps mid-month when contract_step
            for _ in range(8):
                rows.append(_arow(arrival_date_year=year, arrival_date_month=list(CA.MONTHS)[m - 1],
                                  arrival_date_day_of_month=str(d), arrival_date_week_number=str(week), adr=str(bar)))
                rows.append(_arow(arrival_date_year=year, arrival_date_month=list(CA.MONTHS)[m - 1],
                                  arrival_date_day_of_month=str(d), arrival_date_week_number=str(week),
                                  market_segment="Offline TA/TO", customer_type="Transient", adr=str(to)))
        out, _, _ = CA.branch_rows(rows, SETTINGS)
        return out

    def test_flat_contract_maps_to_corp(self):
        res = CA.bar_test(self._rows(contract_step=False), dict(SETTINGS, bar_corr_threshold=0.6, min_basket_rows=5))
        self.assertEqual(res["verdict"], "CORP")

    def test_verdict_needs_both_demeanings_to_agree(self):
        res = CA.bar_test(self._rows(contract_step=True), dict(SETTINGS, bar_corr_threshold=0.6, min_basket_rows=5))
        self.assertIn(res["verdict"], ("CORP", "inconclusive"))

    def _rows_floating_with_bar(self):
        # Same week/month/day scaffolding as _rows above, but the Offline
        # TA/TO transient price is a fixed proportion of that week's public
        # rate rather than flat or stepped, so it genuinely floats with BAR:
        # a constant positive multiple of a series correlates perfectly with
        # it after either demeaning, which is the one case neither existing
        # test produces (the other fixture's Offline TA/TO series is either
        # flat or a mid-month step, never one that moves with BAR itself).
        rows = []
        for week in range(27, 51):
            m = 7 + (week - 27) // 4
            d = 1 + ((week - 27) % 4) * 7
            bar = 150 + 20 * ((week % 4) - 1.5)
            to = round(bar * 0.85, 2)
            for _ in range(8):
                rows.append(_arow(arrival_date_year="2015", arrival_date_month=list(CA.MONTHS)[m - 1],
                                  arrival_date_day_of_month=str(d), arrival_date_week_number=str(week), adr=str(bar)))
                rows.append(_arow(arrival_date_year="2015", arrival_date_month=list(CA.MONTHS)[m - 1],
                                  arrival_date_day_of_month=str(d), arrival_date_week_number=str(week),
                                  market_segment="Offline TA/TO", customer_type="Transient", adr=str(to)))
        out, _, _ = CA.branch_rows(rows, SETTINGS)
        return out

    def test_cell_that_floats_with_bar_maps_to_ota(self):
        out = self._rows_floating_with_bar()
        settings = dict(SETTINGS, bar_corr_threshold=0.6, min_basket_rows=5, min_ratio_rows=5,
                        floor_ceiling_widen=0.10, sellout_threshold=0.95, ota_commission_main=0.15,
                        variable_cost_low_share=0.35)
        res = CA.bar_test(out, settings)
        self.assertEqual(res["verdict"], "OTA")
        # And the wiring: derive_hotel_json must actually write that verdict
        # into segment_map, not just compute it and drop it.
        derived = CA.derive_hotel_json(out, "H2", settings)
        self.assertEqual(derived["segment_map"]["segment"]["OFFLINE_TO_TRANSIENT"], "OTA")


class DerivedHotelJsonIsLoadable(unittest.TestCase):
    """derive_hotel_json's whole point is to feed pace/hotelconfig.py, whose
    loader is strict and rejects unknown keys (including _derivation). This
    builds a several-hundred-row fixture across the warm-up year and proves
    the round trip: derive, strip _derivation, write, load for real."""

    NAME_OF = {v: k for k, v in CA.MONTHS.items()}

    def _make_row(self, year, month_num, day, **kw):
        d = dt.date(year, month_num, day)
        wk = d.isocalendar()[1]
        return _arow(arrival_date_year=str(year), arrival_date_month=self.NAME_OF[month_num],
                     arrival_date_day_of_month=str(day), arrival_date_week_number=str(wk), **kw)

    def _fixture_rows(self):
        months = [(2015, m) for m in range(7, 13)] + [(2016, m) for m in range(1, 7)]
        rows = []
        for year, mnum in months:
            base = 90 + 5 * mnum
            for day in (5, 12, 19, 26):
                for k in range(6):
                    rows.append(self._make_row(year, mnum, day, adr=str(base + (k % 3)), adults="2"))
                for k in range(2):
                    rows.append(self._make_row(year, mnum, day, market_segment="Online TA",
                                                distribution_channel="TA/TO", adr=str(base + 2 + (k % 3)), adults="2"))
                for k in range(3):
                    rows.append(self._make_row(year, mnum, day, market_segment="Corporate",
                                                distribution_channel="Corporate", adr=str(round(base * 0.8)), adults="2"))
                for k in range(2):
                    rows.append(self._make_row(year, mnum, day, market_segment="Offline TA/TO", customer_type="Contract",
                                                distribution_channel="TA/TO", adr=str(round(base * 0.6)), adults="2"))
                for k in range(2):
                    rows.append(self._make_row(year, mnum, day, market_segment="Offline TA/TO", customer_type="Transient",
                                                distribution_channel="TA/TO", adr=str(base + 1), adults="2"))
            rows.append(self._make_row(year, mnum, 15, market_segment="Groups", distribution_channel="TA/TO",
                                        adr=str(round(base * 0.55)), adults="2"))
            rows.append(self._make_row(year, mnum, 20, market_segment="Complementary",
                                        distribution_channel="Direct", adr="0"))
        return rows

    def test_derived_hotel_json_round_trips_through_the_real_loader(self):
        settings = dict(SETTINGS, bar_corr_threshold=0.6, min_basket_rows=20, min_ratio_rows=8,
                         floor_ceiling_widen=0.10, sellout_threshold=0.95, ota_commission_main=0.15,
                         variable_cost_low_share=0.35)
        rows = self._fixture_rows()
        self.assertGreater(len(rows), 200)
        out, counts, notes = CA.branch_rows(rows, settings)
        for code in ("H1", "H2"):
            derived = CA.derive_hotel_json(out, code, settings)
            self.assertIn("_derivation", derived)
            text = CA.audit(out, code, settings, derived)
            self.assertTrue(text.startswith("# Audit %s" % code))
            clean = {k: v for k, v in derived.items() if k != "_derivation"}
            self.assertNotIn("_derivation", clean)
            tmp_dir = tempfile.mkdtemp()
            path = os.path.join(tmp_dir, "%s-hotel.json" % code.lower())
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(clean, fh, indent=2)
            with open(path, encoding="utf-8") as fh:
                raw_text = fh.read()
            self.assertNotIn("NaN", raw_text)
            cfg = HC.load_hotel_json(path)
            self.assertEqual(len(cfg.price_month_factor), 12)
            self.assertEqual(len(cfg.demand_season_band), 12)
            self.assertIsNone(cfg.sellable_rooms)
            with self.assertRaises(HC.ConfigError):
                HC.apply(cfg)  # sellable_rooms is still null at this stage
