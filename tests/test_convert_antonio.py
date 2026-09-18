import datetime as dt
import unittest
from collections import Counter

from tools import convert_antonio as CA

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
