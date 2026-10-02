import datetime as dt
import os
import tempfile
import unittest

from pace import baselines
from pace import handover
from pace import hotelconfig as HC
from pace import ingest
from pace import pilot
from tests.test_pilot import (_csv, _row, history_rows, FIXTURE_HOTEL, FIRST_ARRIVAL,
                              NIGHTS, SCORE_FIRST, SCORE_LAST, _reset_config)

D = dt.date


def _b(**kw):
    """One ingest.Booking with the fields the handover reads, nothing else."""
    base = dict(booking_id="X", booked_on=D(2024, 1, 1), arrival=D(2024, 2, 1), nights=1,
                rooms=1, rate=100.0, currency="EUR", segment="RETAIL", rate_code="", source="",
                room_type="A", company="", status="stayed", status_date=None, updated_on=None,
                row=1, meal="BB", guests=2, target="RETAIL")
    base.update(kw)
    return ingest.Booking(**base)


class DayConvention(unittest.TestCase):
    """Spec section 3, one assertion per sentence of it."""

    def setUp(self):
        n = D(2024, 2, 1)
        self.rows = [
            _b(booking_id="A", arrival=n, nights=2, meal="HB", guests=2),          # nights 1 and 2
            _b(booking_id="B", arrival=n - dt.timedelta(days=1), nights=2, meal="BB", guests=3),  # 31 Jan and 1 Feb
            _b(booking_id="C", arrival=n, nights=1, meal="SC", guests=1, rooms=1),  # night 1 only
            _b(booking_id="N", arrival=n, nights=1, meal="BB", guests=2, target="NONREV", rate=0.0),
            _b(booking_id="G", arrival=n, nights=1, meal="BB", guests=0, rate=50.0),  # a room, no cover
            _b(booking_id="X", arrival=n, nights=1, status="cancelled", status_date=D(2024, 1, 20)),
        ]
        self.night = n

    def test_breakfast_counts_bb_hb_fb_guests_on_revenue_rows_and_nothing_else(self):
        rev = [r for r in self.rows if r.target != "NONREV"]
        self.assertEqual(handover.covers(handover.rows_on(handover.physical_rows(rev, []), self.night),
                                         handover.BREAKFAST_BOARDS), 2 + 3 + 0)

    def test_dinner_counts_hb_and_fb_only(self):
        rev = [r for r in self.rows if r.target != "NONREV"]
        self.assertEqual(handover.covers(handover.rows_on(handover.physical_rows(rev, []), self.night),
                                         handover.DINNER_BOARDS), 2)

    def test_covers_is_none_when_any_row_has_no_guests_column(self):
        rows = [_b(guests=None), _b(booking_id="Y", guests=2)]
        self.assertIsNone(handover.covers(rows, handover.BREAKFAST_BOARDS))

    def test_physical_rooms_include_nonrev_and_exclude_cancelled(self):
        phys = handover.rows_on(handover.physical_rows(
            [r for r in self.rows if r.target != "NONREV"],
            [r for r in self.rows if r.target == "NONREV"]), self.night)
        self.assertEqual(sorted(r.booking_id for r in phys), ["A", "B", "C", "G", "N"])

    def test_the_identity_rooms_equals_departures_plus_stayovers(self):
        """rooms occupied on night D - 1 = departures on day D + stayovers on day D,
        in rooms, on every night of the synthetic year."""
        res = ingest.load(_csv(history_rows(FIRST_ARRIVAL, NIGHTS)), FIXTURE_HOTEL, seed=1)
        self.addCleanup(_reset_config)
        d = res.first_stay
        checked = 0
        while d <= res.last_stay:
            a = handover.actuals(res.bookings, res.nonrev, res.ledger, d)
            self.assertEqual(a["physical"], a["departures"] + a["stayovers"], d)
            checked += 1
            d += dt.timedelta(days=1)
        self.assertGreater(checked, 400)

    def test_units_are_rooms_not_bookings(self):
        three = _b(booking_id="T", rooms=3, nights=2, guests=6)
        a_rows = [three]
        night = three.arrival
        self.assertEqual(sum(r.rooms for r in handover.rows_on(handover.physical_rows(a_rows, []), night)), 3)
        self.assertEqual(handover._stayovers(a_rows, night), 3)
        # Two nights from `night`: the last night is night + 1, so the three
        # rooms depart on the morning of night + 2 and on no other day.
        self.assertEqual(handover._departures(a_rows, night + dt.timedelta(days=1)), 0)
        self.assertEqual(handover._departures(a_rows, night + dt.timedelta(days=2)), 3)
