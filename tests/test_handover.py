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


class ForecastRow(unittest.TestCase):
    def tearDown(self):
        _reset_config()

    def _rows(self):
        res, out = walked()
        return res, out, handover.forecast_rows(res, out, res.hotel)

    def test_booked_plus_pickup_equals_the_engines_rooms_forecast_on_every_row(self):
        res, out, rows = self._rows()
        self.assertGreater(len(rows), 1000)
        for key, r in rows.items():
            self.assertAlmostEqual(r["booked_stay"] + r["pickup"], r["rooms"], places=9, msg=key)

    def test_the_pickup_is_never_negative(self):
        res, out, rows = self._rows()
        self.assertTrue(all(r["pickup"] >= 0.0 for r in rows.values()))

    def test_every_lead_from_zero_to_fourteen_is_recorded(self):
        res, out, rows = self._rows()
        leads = sorted({lead for _, lead in rows})
        self.assertEqual(leads, list(handover.LEADS))

    def test_departures_are_the_identity_against_the_physical_forecast(self):
        res, out, rows = self._rows()
        for key, r in rows.items():
            self.assertAlmostEqual(r["departures"] + r["stayovers"], r["physical"], places=9, msg=key)

    def test_covers_are_the_booked_rows_board_times_s_plus_the_pickup_times_the_share(self):
        """The formula, checked on the row with the smallest pickup so the
        booked term dominates and on one with a large pickup so the share
        term does."""
        res, out, rows = self._rows()
        with_dinner = [(k, r) for k, r in rows.items() if r["dinner"] is not None]
        self.assertTrue(with_dinner)
        for key, r in (min(with_dinner, key=lambda kr: kr[1]["pickup"]),
                       max(with_dinner, key=lambda kr: kr[1]["pickup"])):
            parts = handover.booked_parts(res.bookings, res.nonrev, key[0], r["asof"])
            rat = r["ratios"]
            want = parts["dinner"] * r["survival"] + r["pickup"] * rat["dinner_share"] * rat["guests_per_room"]
            self.assertAlmostEqual(r["dinner"], want, places=9, msg=key)

    def test_a_row_whose_window_cannot_supply_a_ratio_is_absent_not_defaulted(self):
        res, out = walked()
        early = next(k for k in sorted(out.records) if out.records[k].forecast is not None)
        rec = out.records[early]
        if handover.ratios(res.bookings, res.nonrev, res.ledger, rec.stay_date, rec.lead) is None:
            self.assertIsNone(handover.forecast_row(rec, res.bookings, res.nonrev, res.ledger, res.hotel))


class IndexedTwins(unittest.TestCase):
    """The index changes how an answer is found, never the answer: each
    indexed function equals its scanning twin on real records."""

    def tearDown(self):
        _reset_config()

    def test_actuals_booked_parts_and_ratios_agree_with_and_without_the_index(self):
        res, out = walked()
        idx = handover.Index(res.bookings, res.nonrev)
        keys = sorted(out.records)[::97][:40]
        for key in keys:
            rec = out.records[key]
            self.assertEqual(handover.actuals(res.bookings, res.nonrev, res.ledger, key[0]),
                             handover.actuals(res.bookings, res.nonrev, res.ledger, key[0], idx), key)
            self.assertEqual(handover.booked_parts(res.bookings, res.nonrev, key[0], rec.asof),
                             handover.booked_parts(res.bookings, res.nonrev, key[0], rec.asof, idx), key)
            self.assertEqual(handover.ratios(res.bookings, res.nonrev, res.ledger, key[0], key[1]),
                             handover.ratios(res.bookings, res.nonrev, res.ledger, key[0], key[1], idx), key)


class Bands(unittest.TestCase):
    def tearDown(self):
        _reset_config()

    def test_a_band_is_the_quantitys_own_error_quantiles(self):
        errs = {("rooms", 7): [float(x) for x in range(-20, 21)]}
        b = handover.bands(errs)[("rooms", 7)]
        self.assertEqual(b["n"], 41)
        self.assertAlmostEqual(b["p50"], 0.0)
        self.assertAlmostEqual(b["p10"], pilot.percentile(errs[("rooms", 7)], 0.10))
        self.assertAlmostEqual(b["p90"], pilot.percentile(errs[("rooms", 7)], 0.90))

    def test_below_thirty_nights_there_is_no_band(self):
        errs = {("rooms", 7): [1.0] * 29}
        self.assertIsNone(handover.bands(errs)[("rooms", 7)])
        self.assertIsNotNone(handover.bands({("rooms", 7): [1.0] * 30})[("rooms", 7)])

    def test_the_band_is_measured_on_nights_the_coverage_never_touches(self):
        res, out = walked()
        rows = handover.forecast_rows(res, out, res.hotel)
        warm = [d for d in sorted({k[0] for k in rows}) if d < SCORE_FIRST]
        score = [d for d in sorted({k[0] for k in rows}) if SCORE_FIRST <= d <= SCORE_LAST]
        self.assertFalse(set(warm) & set(score))
        table = handover.bands(handover.errors(rows, res, warm))
        cov = handover.coverage(rows, res, table, score)
        self.assertIn(("rooms", 7), cov)
        self.assertEqual(cov[("rooms", 7)]["n"], sum(1 for d in score if (d, 7) in rows))

    def test_coverage_is_the_share_of_scored_nights_inside_the_band(self):
        rows = {(D(2025, 2, 1), 7): {"rooms": 10.0}, (D(2025, 2, 2), 7): {"rooms": 10.0},
                (D(2025, 2, 3), 7): {"rooms": 10.0}, (D(2025, 2, 4), 7): {"rooms": 10.0}}
        table = {("rooms", 7): {"n": 40, "p10": -2.0, "p50": 0.0, "p90": 2.0}}

        class _Res:
            pass
        res = _Res()
        acts = {D(2025, 2, 1): 9.0, D(2025, 2, 2): 12.5, D(2025, 2, 3): 8.0, D(2025, 2, 4): 11.0}
        cov = handover.coverage(rows, res, table, sorted(acts), actual_of=lambda r, d, q: acts[d])
        self.assertEqual(cov[("rooms", 7)], {"n": 4, "inside": 3, "share": 0.75})

    def test_a_biased_forecast_is_banded_where_the_actual_lands(self):
        """Every error +5: the forecast always sat five above the actual, so
        the band must sit five below the point, not five above it."""
        band = handover.bands({("rooms", 7): [5.0] * 40})[("rooms", 7)]
        lo, mid, hi = handover.band_of(100.0, band)
        self.assertEqual((lo, mid, hi), (95.0, 95.0, 95.0))
        skew = handover.bands({("rooms", 7): [0.0] * 20 + [8.0] * 20})[("rooms", 7)]
        lo, mid, hi = handover.band_of(100.0, skew)
        self.assertLess(lo, hi)
        self.assertAlmostEqual(hi, 100.0 - skew["p10"])
        self.assertAlmostEqual(lo, 100.0 - skew["p90"])

    def test_excluded_weeks_are_not_measured_on(self):
        weeks = [(D(2024, 3, 25), D(2024, 3, 31))]
        self.assertTrue(handover.excluded(D(2024, 3, 27), weeks))
        self.assertFalse(handover.excluded(D(2024, 4, 1), weeks))


class Scores(unittest.TestCase):
    def tearDown(self):
        _reset_config()

    def _scored(self):
        res, out = walked()
        rows = handover.forecast_rows(res, out, res.hotel)
        nights = [d for d in sorted({k[0] for k in rows}) if d < SCORE_FIRST]
        score = [d for d in sorted({k[0] for k in rows}) if SCORE_FIRST <= d <= SCORE_LAST]
        table = handover.bands(handover.errors(rows, res, nights))
        return res, out, rows, table, score

    def test_a_rate_is_not_printed_below_thirty_nights(self):
        self.assertIsNone(handover.rate(3, 4))
        self.assertAlmostEqual(handover.rate(24, 30), 0.8)

    def test_the_alarm_fires_on_the_top_of_the_band_against_the_same_cut_as_the_event(self):
        res, out, rows, table, score = self._scored()
        a = handover.alarm_scores(rows, res, table, score)
        self.assertEqual(a["nights"], len(score))
        self.assertEqual(sum(1 for d in score if handover.sold_out(res, d)), a["events"])
        for lead, cell in a["per_lead"].items():
            self.assertLessEqual(cell["hits"], cell["warned"])
            self.assertLessEqual(cell["hits"], cell["events"])

    def test_notice_is_censored_at_fourteen_and_counted(self):
        warned = {lead: True for lead in handover.LEADS}
        self.assertEqual(handover.notice_of(warned), (14, True))
        warned[9] = False
        self.assertEqual(handover.notice_of(warned), (8, False))
        warned[0] = False
        self.assertEqual(handover.notice_of(warned), (None, False))

    def test_the_kitchen_tiers_are_scored_as_promises(self):
        band = {"n": 40, "p10": -6.0, "p50": -1.0, "p90": 4.0}
        fc = 100.0        # band_of: lo 96, mid 101, hi 106
        acts = [95.0, 102.0, 107.0, 99.0]
        cov = handover.tier_scores(fc, band, acts)
        self.assertEqual(cov["covering"]["short"], 1)          # 107 > 106
        self.assertAlmostEqual(cov["covering"]["over_mean"], ((106 - 95) + (106 - 102) + (106 - 99)) / 3)
        self.assertEqual(cov["balanced"]["short"], 2)          # 102 and 107 > 101
        self.assertAlmostEqual(cov["balanced"]["over_mean"], ((101 - 95) + (101 - 99)) / 2)

    def test_housekeeping_errors_are_kept_apart_and_the_roster_reads_the_bottom(self):
        band = {"n": 40, "p10": -3.0, "p50": 0.0, "p90": 3.0}
        self.assertEqual(handover.roster_short(20.0, band, [19.0, 18.0, 16.0]), (2, 1.5))  # bottom 17: 19 and 18 are short by 2 and 1

    def test_the_count_the_log_cannot_speak_for_is_the_pilots_own(self):
        res, out, rows, table, score = self._scored()
        a = handover.alarm_scores(rows, res, table, score)
        gap = pilot.full_night_gap(res.bookings, res.ledger, res.hotel, score[0], score[-1])
        self.assertEqual(a["cannot_speak"], gap["full_but_invisible"])

    def test_kitchen_and_housekeeping_scores_cover_every_lead_with_a_band(self):
        res, out, rows, table, score = self._scored()
        k = handover.kitchen_scores(rows, res, table, score)
        h = handover.housekeeping_scores(rows, res, table, score)
        for lead in handover.LEADS:
            if table.get(("breakfast", lead)) is not None:
                self.assertIsNotNone(k["breakfast"][lead], lead)
                self.assertGreaterEqual(k["breakfast"][lead]["mae"], 0.0)
            self.assertIn("departures", h[lead])
            self.assertIn("stayovers", h[lead])


class ProofRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pace import plugins as _plugins
        _plugins.reset()
        pilot._release_for_tests()
        cls.out_dir = tempfile.mkdtemp()
        settings = os.path.join(cls.out_dir, "settings.json")
        with open(settings, "w", encoding="utf-8") as fh:
            fh.write('{"seed": 1, "lead_marks": [30, 14, 7], "lead_marks_h1_only": [], '
                     '"baseline_window_weeks": 10, "sellout_threshold": 0.97, '
                     '"excluded_weeks": [["2024-03-25", "2024-03-31"]], '
                     '"warmup_window": ["2024-01-01", "2025-01-31"], '
                     '"scoring_window": ["2025-02-01", "2025-04-30"]}')
        cls.payload = handover.run_proof(_csv(history_rows(FIRST_ARRIVAL, NIGHTS)), FIXTURE_HOTEL,
                                         settings, cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        _reset_config()
        pilot._release_for_tests()

    def test_the_payload_and_the_page_are_written_beside_each_other(self):
        self.assertTrue(self.payload["_json_path"].endswith("handover-pilot.json"))
        self.assertTrue(self.payload["_html_path"].endswith("handover-pilot.html"))
        self.assertTrue(os.path.exists(self.payload["_html_path"]))

    def test_the_bands_come_from_the_warm_up_window_and_the_coverage_from_the_scoring_window(self):
        w = self.payload["windows"]
        self.assertLess(w["band_last"], w["score_first"])
        self.assertEqual(w["score_first"], "2025-02-01")
        self.assertNotIn("2024-03-27", self.payload["front_desk"]["band_nights"])

    def test_every_lead_has_a_band_or_a_reason(self):
        for q in handover.QUANTITIES:
            for lead in handover.LEADS:
                cell = self.payload["bands"][q][str(lead)]
                self.assertTrue(cell is None or cell["n"] >= handover.MIN_BAND_NIGHTS, (q, lead))

    def test_the_page_names_which_rooms_each_tab_reads(self):
        with open(self.payload["_html_path"], encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("revenue rooms", html)
        self.assertIn("physical rooms", html)
        self.assertIn("Breakfast on morning D is served to the guests who stayed night D - 1", html)

    def test_a_rate_below_the_floor_prints_its_count_and_no_percentage(self):
        from pace import handoverpage
        text = handoverpage.rate_text(3, 4)
        self.assertEqual(text, "3 of 4 (below the 30-night floor, no rate printed)")
        self.assertEqual(handoverpage.rate_text(24, 30), "80.0% (24 of 30)")

    def test_notice_fourteen_is_printed_as_fourteen_or_more(self):
        from pace import handoverpage
        self.assertIn("14 or more", handoverpage.notice_text({"median": 14.0, "censored": 5, "scored": 9}))
        self.assertIn("5 of 9 censored", handoverpage.notice_text({"median": 14.0, "censored": 5, "scored": 9}))
        self.assertNotIn("or more", handoverpage.notice_text({"median": 6.0, "censored": 0, "scored": 9}))

    def test_the_no_lift_and_no_pricing_sentences_are_on_the_page(self):
        with open(self.payload["_html_path"], encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("never prices", html)
        self.assertIn("No number on this page is a revenue lift", html)

    def test_the_kitchen_tab_names_the_survival_assumption_and_the_undefined_share(self):
        k = self.payload["kitchen"]
        self.assertIn("same board mix", k["survival_assumption"])
        self.assertIn("revenue rows", k["covers_population"])
        self.assertIn("share", self.payload["undefined_board_share"])


class Switchboard(unittest.TestCase):
    """run.py handover hands the arguments through and prints the two paths."""

    def test_handover_is_dispatched_with_the_paths_in_order(self):
        import io, contextlib, importlib
        from unittest import mock
        run = importlib.import_module("run")
        seen = {}

        def fake(csv_path, hotel_json_path, settings_path, out_dir, progress=None):
            seen.update(csv=csv_path, hotel=hotel_json_path, settings=settings_path, out=out_dir)
            return {"hotel": {"name": "H1 Resort Hotel", "code": "H1", "rooms": 187},
                    "front_desk": {"alarm": {"events": 94, "nights": 427}},
                    "walk": {"days": 1, "seconds": 1.0, "cpu_seconds": 1.0},
                    "_json_path": "/tmp/out/handover-h1.json", "_html_path": "/tmp/out/handover-h1.html"}
        buf = io.StringIO()
        with mock.patch("pace.handover.run_proof", fake), contextlib.redirect_stdout(buf):
            code = run.main(["run.py", "handover", "b.csv", "h.json", "--out", "/tmp/elsewhere"])
        self.assertEqual(code, 0)
        self.assertEqual(seen["csv"], "b.csv")
        self.assertEqual(seen["out"], "/tmp/elsewhere")
        self.assertIn("wrote /tmp/out/handover-h1.html", buf.getvalue())
        self.assertIn("94 of 427 nights reached the sell-out cut", buf.getvalue())


class ForwardRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pace import plugins as _plugins
        _plugins.reset()
        pilot._release_for_tests()
        cls.out_dir = tempfile.mkdtemp()
        settings = os.path.join(cls.out_dir, "settings.json")
        with open(settings, "w", encoding="utf-8") as fh:
            fh.write('{"seed": 1, "lead_marks": [30, 14, 7], "lead_marks_h1_only": [], '
                     '"baseline_window_weeks": 10, "sellout_threshold": 0.97, "excluded_weeks": []}')
        # A log that stops: every row booked after the export date is dropped,
        # and nights after it are what the page answers for.
        rows = history_rows(FIRST_ARRIVAL, NIGHTS)
        export = (FIRST_ARRIVAL + dt.timedelta(days=NIGHTS - 40)).isoformat()
        kept = [r for r in rows if r[1] <= export]
        cls.export = dt.date.fromisoformat(export)
        cls.payload = handover.run_forward(_csv(kept), FIXTURE_HOTEL, settings, cls.out_dir)

    @classmethod
    def tearDownClass(cls):
        _reset_config()
        pilot._release_for_tests()

    def test_the_export_date_is_the_last_booking_day_and_the_horizon_is_fourteen_nights(self):
        self.assertEqual(self.payload["mode"], "forward")
        self.assertEqual(self.payload["export_date"], self.export.isoformat())
        nights = [r["night"] for r in self.payload["front_desk"]["nights"]]
        self.assertEqual(len(nights), 14)
        self.assertEqual(nights[0], (self.export + dt.timedelta(days=1)).isoformat())
        leads = [r["lead"] for r in self.payload["front_desk"]["nights"]]
        self.assertEqual(leads, list(range(1, 15)))

    def test_the_bands_come_from_settled_nights_before_the_export_only(self):
        w = self.payload["windows"]
        self.assertLess(w["band_last"], self.export.isoformat())
        self.assertGreaterEqual(w["band_nights"], 30)

    def test_nothing_is_scored_and_the_page_says_why(self):
        self.assertNotIn("alarm", self.payload["front_desk"])
        self.assertNotIn("scores", self.payload["kitchen"])
        with open(self.payload["_html_path"], encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("forward warnings are not scored", html.lower())
        self.assertTrue(self.payload["_html_path"].endswith("handover-pilot-forward.html"))
