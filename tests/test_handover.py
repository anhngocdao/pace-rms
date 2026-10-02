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


_WALK = {}


def walked():
    """One handover walk, built once: every lead 0 to 14, driven from the
    first stay night, records on every night of the synthetic history."""
    if "res" not in _WALK:
        from pace import plugins as _plugins
        _plugins.reset()
        res = ingest.load(_csv(history_rows(FIRST_ARRIVAL, NIGHTS)), FIXTURE_HOTEL, seed=1)
        cal = HC.event_calendar(res.cfg)
        out = pilot.walk(res.bookings, res.hotel, cal,
                         first_stay=res.first_stay, last_stay=res.last_stay,
                         score_first=res.first_stay, score_last=res.last_stay,
                         marks=handover.LEADS, drive_from=res.first_stay,
                         warmup_nights=60)
        _WALK["res"] = res
        _WALK["walk"] = out
    else:
        HC.apply(_WALK["res"].cfg)
    return _WALK["res"], _WALK["walk"]


class OnTheBooks(unittest.TestCase):
    def tearDown(self):
        _reset_config()

    def test_the_rooms_on_the_books_equal_the_ledgers_snapshot_at_every_lead(self):
        """The leak guard for the booked part: what this module counts as on
        the books on day asof is exactly what the ledger froze that day."""
        res, out = walked()
        rows = pilot.ledger_rows(res.bookings)
        checked = 0
        for (night, lead), rec in out.records.items():
            if rec.otb is None:
                continue
            got = sum(b.rooms for b in handover.on_books(rows, night, rec.asof))
            self.assertEqual(got, rec.otb, (night, lead))
            checked += 1
        self.assertGreater(checked, 3000)

    def test_a_cancellation_dated_on_the_forecast_day_is_already_off_the_books(self):
        n = D(2024, 3, 1)
        rows = [_b(booking_id="C", arrival=n, nights=1, status="cancelled", status_date=D(2024, 2, 20)),
                _b(booking_id="K", arrival=n, nights=1, status="stayed")]
        self.assertEqual([b.booking_id for b in handover.on_books(rows, n, D(2024, 2, 20))], ["K"])
        self.assertEqual([b.booking_id for b in handover.on_books(rows, n, D(2024, 2, 19))], ["C", "K"])

    def test_a_booking_entered_after_the_forecast_day_is_not_on_the_books_yet(self):
        n = D(2024, 3, 1)
        rows = [_b(booking_id="L", booked_on=D(2024, 2, 25), arrival=n, nights=1)]
        self.assertEqual(handover.on_books(rows, n, D(2024, 2, 24)), [])
        self.assertEqual(len(handover.on_books(rows, n, D(2024, 2, 25))), 1)

    def test_a_no_show_stays_on_the_books_through_its_arrival_night(self):
        """The ledger releases a no-show when it settles the night, which is the
        day after; at lead 0 it is still on the books, and so it is here."""
        n = D(2024, 3, 1)
        rows = [_b(booking_id="S", arrival=n, nights=3, status="no_show")]
        self.assertEqual(len(handover.on_books(rows, n, n)), 1)
        # The walk settles the arrival night at the end of the arrival day and
        # releases the no-show from every night of its stay, so for the second
        # night it is on the books at lead 1 and gone at lead 0.
        n2 = n + dt.timedelta(days=1)
        self.assertEqual(len(handover.on_books(rows, n2, n)), 1)
        self.assertEqual(handover.on_books(rows, n2, n2), [])

    def test_booked_parts_use_the_rows_own_board_and_guests(self):
        n = D(2024, 3, 1)
        rows = [_b(booking_id="A", arrival=n, nights=2, meal="HB", guests=2),
                _b(booking_id="B", arrival=n, nights=1, meal="BB", guests=3),
                _b(booking_id="C", arrival=n, nights=1, meal="SC", guests=1)]
        nonrev = [_b(booking_id="N", arrival=n, nights=2, meal="BB", guests=2, target="NONREV", rate=0.0)]
        parts = handover.booked_parts(rows, nonrev, n, n - dt.timedelta(days=3))
        self.assertEqual(parts["rooms"], 3)
        self.assertEqual(parts["breakfast"], 5)
        self.assertEqual(parts["dinner"], 2)
        self.assertEqual(parts["stayovers"], 1)
        self.assertEqual(parts["nonrev_stayovers"], 1)
        self.assertEqual(parts["nonrev_rooms"], 1)


class Ratios(unittest.TestCase):
    def tearDown(self):
        _reset_config()

    def test_the_reference_nights_are_the_pilots_own_window(self):
        res, out = walked()
        night, lead = SCORE_FIRST + dt.timedelta(days=30), 7
        r = handover.ratios(res.bookings, res.nonrev, res.ledger, night, lead)
        self.assertIsNotNone(r)
        self.assertEqual(r["refs"], baselines.reference_nights(res.ledger, night, lead))
        self.assertEqual(len(r["refs"]), handover.WINDOW_WEEKS)

    def test_survival_is_rooms_that_stayed_over_rooms_on_the_books_summed_over_the_window(self):
        res, out = walked()
        night, lead = SCORE_FIRST + dt.timedelta(days=30), 7
        r = handover.ratios(res.bookings, res.nonrev, res.ledger, night, lead)
        stayed = sum(baselines.actual(res.ledger, n) for n in r["refs"])
        otb = sum(res.ledger.otb_at(n, lead) for n in r["refs"])
        self.assertAlmostEqual(r["survival"], stayed / otb)
        self.assertLessEqual(r["survival"], 1.0 + 1e-9)

    def test_no_ratio_leaks_a_night_at_or_after_the_forecast_day(self):
        res, out = walked()
        night, lead = SCORE_FIRST + dt.timedelta(days=30), 7
        r = handover.ratios(res.bookings, res.nonrev, res.ledger, night, lead)
        asof = night - dt.timedelta(days=lead)
        self.assertTrue(all(n < asof for n in r["refs"]))

    def test_a_short_window_gives_no_ratio_rather_than_a_default(self):
        res, out = walked()
        early = res.first_stay + dt.timedelta(days=20)
        self.assertIsNone(handover.ratios(res.bookings, res.nonrev, res.ledger, early, 7))

    def test_the_shares_are_over_the_reference_nights_stayed_revenue_rooms(self):
        res, out = walked()
        night, lead = SCORE_FIRST + dt.timedelta(days=30), 7
        r = handover.ratios(res.bookings, res.nonrev, res.ledger, night, lead)
        phys = handover.physical_rows(res.bookings, [])
        rooms = covers_b = guests = 0
        for n in r["refs"]:
            rev = [b for b in handover.rows_on(phys, n) if b.target != "NONREV"]
            rooms += sum(b.rooms for b in rev)
            covers_b += handover.covers(rev, handover.BREAKFAST_BOARDS)
            guests += sum(b.guests for b in rev)
        self.assertAlmostEqual(r["breakfast_share"], covers_b / guests)
        self.assertAlmostEqual(r["guests_per_room"], guests / rooms)
