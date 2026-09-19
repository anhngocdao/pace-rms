import csv
import datetime as dt
import io
import os
import tempfile
import unittest

from pace import hotelconfig as HC
from pace import ingest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEADER = ["booking_id", "booked_on", "arrival", "nights", "rooms", "rate", "currency", "segment",
          "rate_code", "source", "room_type", "company", "status", "status_date", "updated_on"]


def _csv(rows, header=HEADER):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(header); w.writerows(rows)
    return path


def _cfg(**over):
    cfg = HC.load_hotel_json(os.path.join(ROOT, "data", "sample-hotel.json"))
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def _row(**kw):
    base = dict(booking_id="B1", booked_on="2025-01-01", arrival="2025-02-01", nights="2", rooms="1",
                rate="120", currency="EUR", segment="WEB", rate_code="", source="", room_type="STD",
                company="", status="stayed", status_date="", updated_on="")
    base.update(kw)
    return [base[h] for h in HEADER]


class ReadBookings(unittest.TestCase):
    def test_reads_a_clean_row(self):
        rows, rep = ingest.read_bookings(_csv([_row()]), _cfg())
        self.assertEqual(rep.errors, [])
        self.assertEqual(rows[0].nights, 2)
        self.assertEqual(rows[0].arrival, dt.date(2025, 2, 1))

    def test_departure_gives_nights(self):
        header = [h for h in HEADER if h != "nights"] + ["departure"]
        r = _row(); r = [v for h, v in zip(HEADER, r) if h != "nights"] + ["2025-02-04"]
        rows, rep = ingest.read_bookings(_csv([r], header), _cfg())
        self.assertEqual(rows[0].nights, 3)

    def test_total_revenue_gives_rate(self):
        header = [h for h in HEADER if h != "rate"] + ["total_revenue"]
        r = [v for h, v in zip(HEADER, _row()) if h != "rate"] + ["300"]
        rows, _ = ingest.read_bookings(_csv([r], header), _cfg())
        self.assertEqual(rows[0].rate, 150.0)

    def test_multi_room_rows_expand(self):
        rows, _ = ingest.read_bookings(_csv([_row(rooms="3")]), _cfg())
        self.assertEqual(len(rows), 3)
        self.assertEqual([b.booking_id for b in rows], ["B1#1", "B1#2", "B1#3"])
        self.assertTrue(all(b.rooms == 1 for b in rows))

    def test_day_use_is_allowed(self):
        rows, rep = ingest.read_bookings(_csv([_row(nights="0")]), _cfg())
        self.assertEqual(rep.errors, []); self.assertEqual(rows[0].nights, 0)

    def test_errors_are_collected_up_to_fifty(self):
        bad = [_row(booking_id="B%d" % i, arrival="not-a-date") for i in range(60)]
        _, rep = ingest.read_bookings(_csv(bad), _cfg())
        self.assertEqual(len(rep.errors), 50)
        self.assertEqual(rep.errors[0][1], "arrival")
        with self.assertRaises(ingest.IngestError):
            rep.fail_if_errors()

    def test_rate_outside_range_only_warns(self):
        _, rep = ingest.read_bookings(_csv([_row(rate="0"), _row(booking_id="B2", rate="900")]), _cfg())
        self.assertEqual(rep.errors, [])
        self.assertEqual(rep.warnings["rate_nonpositive"], 1)
        self.assertEqual(rep.warnings["rate_out_of_range"], 1)

    def test_mixed_currency_needs_fx(self):
        rows, rep = ingest.read_bookings(_csv([_row(), _row(booking_id="B2", currency="USD", rate="100")]), _cfg())
        self.assertEqual(rep.errors, []); self.assertEqual(rows[1].rate, 92.0)
        _, rep2 = ingest.read_bookings(_csv([_row(currency="GBP")]), _cfg(fx={}))
        self.assertEqual(rep2.errors[0][1], "currency")

    def test_cancel_dates_are_clamped_and_counted(self):
        rows, rep = ingest.read_bookings(_csv([
            _row(status="cancelled", status_date="2025-02-10"),
            _row(booking_id="B2", status="cancelled", status_date="2024-12-01"),
            _row(booking_id="B3", status="cancelled"),
        ]), _cfg())
        self.assertEqual(rows[0].status_date, dt.date(2025, 2, 1))
        self.assertEqual(rows[1].status_date, dt.date(2025, 1, 1))
        self.assertIsNone(rows[2].status_date)
        self.assertEqual(rep.warnings["cancel_after_arrival"], 1)
        self.assertEqual(rep.warnings["cancel_before_booking"], 1)
        self.assertEqual(rep.warnings["cancelled_without_date"], 1)

    def test_unknown_status_is_an_error(self):
        _, rep = ingest.read_bookings(_csv([_row(status="checked")]), _cfg())
        self.assertEqual(rep.errors[0][1], "status")

    def test_blank_lines_do_not_shift_row_numbers(self):
        rows = [_row(), [], _row(booking_id="B2", arrival="not-a-date")]
        _, rep = ingest.read_bookings(_csv(rows), _cfg())
        self.assertEqual(rep.errors[0][0], 4)
        self.assertEqual(rep.errors[0][1], "arrival")

    def test_duplicate_booking_id_is_an_error(self):
        rows, rep = ingest.read_bookings(_csv([_row(), _row(arrival="2025-03-01")]), _cfg())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rep.errors[0], (3, "booking_id", "duplicate booking_id"))

    def test_expanded_id_colliding_with_a_real_id_is_an_error(self):
        rows, rep = ingest.read_bookings(_csv([
            _row(booking_id="B1#2"),
            _row(booking_id="B1", rooms="2"),
        ]), _cfg())
        ids = [b.booking_id for b in rows]
        self.assertEqual(ids.count("B1#2"), 1)
        self.assertIn("B1#1", ids)
        self.assertTrue(any(e[1] == "booking_id" for e in rep.errors))

    def test_nights_and_departure_must_agree_when_both_given(self):
        header = HEADER + ["departure"]
        agree = _row() + ["2025-02-03"]
        disagree = _row(booking_id="B2") + ["2025-02-05"]
        rows, rep = ingest.read_bookings(_csv([agree, disagree], header), _cfg())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].booking_id, "B1")
        self.assertEqual(rep.errors[0][1], "nights")

    def test_rate_and_total_revenue_must_agree_when_both_given(self):
        header = HEADER + ["total_revenue"]
        agree = _row() + ["240"]
        disagree = _row(booking_id="B2") + ["999"]
        rows, rep = ingest.read_bookings(_csv([agree, disagree], header), _cfg())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].booking_id, "B1")
        self.assertEqual(rep.errors[0][1], "rate")


class MapSegments(unittest.TestCase):
    def _mapped(self, *rows, **over):
        cfg = _cfg(**over)
        bookings, rep = ingest.read_bookings(_csv(list(rows)), cfg)
        ingest.map_segments(bookings, cfg, rep)
        return bookings, rep

    def test_first_key_wins_when_present(self):
        b, _ = self._mapped(_row(segment="BOOKING", rate_code="BAR"))
        self.assertEqual(b[0].target, "OTA")

    def test_default_value_falls_through_to_rate_code(self):
        b, _ = self._mapped(_row(segment="DEFAULT", rate_code="BAR"))
        self.assertEqual(b[0].target, "RETAIL")

    def test_prefix_needs_the_star(self):
        b, _ = self._mapped(_row(segment="", rate_code="CORP-ACME"))
        self.assertEqual(b[0].target, "CORP")
        b2, rep2 = self._mapped(_row(segment="", rate_code="BARX"))
        self.assertIsNone(b2[0].target); self.assertEqual(rep2.unmapped["rate_code=BARX"], 1)

    def test_source_is_the_last_key(self):
        b, _ = self._mapped(_row(segment="", rate_code="", source="Agoda"))
        self.assertEqual(b[0].target, "OTA")

    def test_unmapped_values_are_grouped_not_listed_per_row(self):
        rows = [_row(booking_id="B%d" % i, segment="NEWCODE") for i in range(120)]
        _, rep = self._mapped(*rows)
        self.assertEqual(rep.unmapped["segment=NEWCODE"], 120)
        self.assertEqual(rep.errors, [])
        with self.assertRaises(ingest.IngestError) as cm:
            rep.fail_if_errors()
        self.assertIn("NEWCODE", str(cm.exception))

    def test_order_is_configurable(self):
        b, _ = self._mapped(_row(segment="BOOKING", rate_code="BAR"), segment_map_order=["rate_code", "segment"])
        self.assertEqual(b[0].target, "RETAIL")


class DetectGroups(unittest.TestCase):
    def _run(self, rows, **over):
        cfg = _cfg(**over)
        bookings, rep = ingest.read_bookings(_csv(rows), cfg)
        ingest.map_segments(bookings, cfg, rep)
        n = ingest.detect_groups(bookings, cfg, rep)
        return bookings, n

    def test_same_company_day_arrival_nights_at_threshold_is_a_group(self):
        rows = [_row(booking_id="B%d" % i, segment="CORP", company="ACME") for i in range(5)]
        b, n = self._run(rows)
        self.assertEqual(n, 5); self.assertTrue(all(x.target == "GROUP" for x in b))

    def test_company_alone_is_not_a_group(self):
        rows = [_row(booking_id="B%d" % i, segment="CORP", company="ACME", arrival="2025-02-%02d" % (i + 1)) for i in range(5)]
        b, n = self._run(rows)
        self.assertEqual(n, 0); self.assertTrue(all(x.target == "CORP" for x in b))

    def test_different_booking_days_do_not_cluster(self):
        rows = [_row(booking_id="B%d" % i, segment="BOOKING", company="AGENT9", booked_on="2025-01-%02d" % (i + 1)) for i in range(6)]
        _, n = self._run(rows)
        self.assertEqual(n, 0)

    def test_nonrev_rows_stay_nonrev(self):
        rows = [_row(booking_id="B%d" % i, segment="COMP", company="ACME", rate="0") for i in range(5)]
        b, n = self._run(rows)
        self.assertEqual(n, 0); self.assertTrue(all(x.target == "NONREV" for x in b))

    def test_empty_company_never_matches(self):
        rows = [_row(booking_id="B%d" % i, segment="WEB", company="") for i in range(8)]
        _, n = self._run(rows)
        self.assertEqual(n, 0)

    def test_detect_groups_can_be_switched_off(self):
        rows = [_row(booking_id="B%d" % i, segment="CORP", company="ACME") for i in range(5)]
        _, n = self._run(rows, detect_groups=False)
        self.assertEqual(n, 0)


class ImputeCancelDates(unittest.TestCase):
    def _bookings(self):
        rows = [_row(booking_id="C%d" % i, segment="WEB", status="cancelled", booked_on="2025-01-01",
                     arrival="2025-01-21", status_date="2025-01-%02d" % (6 + i)) for i in range(5)]   # ratio 0.25..0.45
        rows.append(_row(booking_id="U1", segment="WEB", status="cancelled", booked_on="2025-03-01", arrival="2025-03-11"))
        rows.append(_row(booking_id="U2", segment="WEB", status="cancelled", booked_on="2025-03-01", arrival="2025-03-31", updated_on="2025-03-05"))
        rows.append(_row(booking_id="U3", segment="CORP", status="cancelled", booked_on="2025-03-01", arrival="2025-03-11"))
        cfg = _cfg(); b, rep = ingest.read_bookings(_csv(rows), cfg); ingest.map_segments(b, cfg, rep)
        return b, rep

    def test_ratio_is_applied_to_the_rows_own_lead(self):
        b, rep = self._bookings()
        n = ingest.impute_cancel_dates(b, rep, seed=1)
        u1 = next(x for x in b if x.booking_id == "U1")
        self.assertEqual(n, 3); self.assertTrue(u1.imputed_cancel)
        self.assertTrue(dt.date(2025, 3, 3) <= u1.status_date <= dt.date(2025, 3, 6))   # 10-day lead times 0.25..0.45

    def test_updated_on_is_the_upper_bound(self):
        b, rep = self._bookings()
        ingest.impute_cancel_dates(b, rep, seed=1)
        u2 = next(x for x in b if x.booking_id == "U2")
        self.assertLessEqual(u2.status_date, dt.date(2025, 3, 5))

    def test_segment_without_dated_cancellations_uses_low_bound(self):
        b, rep = self._bookings()
        ingest.impute_cancel_dates(b, rep, seed=1)
        u3 = next(x for x in b if x.booking_id == "U3")
        self.assertEqual(u3.status_date, dt.date(2025, 3, 1))
        self.assertTrue(any("CORP" in n for n in rep.notes))

    def test_same_seed_same_dates(self):
        a, ra = self._bookings(); ingest.impute_cancel_dates(a, ra, seed=5)
        c, rc = self._bookings(); ingest.impute_cancel_dates(c, rc, seed=5)
        self.assertEqual([x.status_date for x in a], [x.status_date for x in c])

    def test_bounds_put_imputed_rows_at_booking_day_and_arrival(self):
        b, rep = self._bookings(); ingest.impute_cancel_dates(b, rep, seed=1)
        low, high = ingest.cancel_bounds(b)
        u1_low = next(x for x in low if x.booking_id == "U1"); u1_high = next(x for x in high if x.booking_id == "U1")
        self.assertEqual(u1_low.status_date, dt.date(2025, 3, 1)); self.assertEqual(u1_high.status_date, dt.date(2025, 3, 11))

    def test_imputed_date_never_precedes_the_booking_day(self):
        # UB has a segment (WEB/RETAIL) with dated cancellations to learn from, so it
        # really goes through the draw, but its updated_on precedes its own booked_on.
        rows = [_row(booking_id="C%d" % i, segment="WEB", status="cancelled", booked_on="2025-01-01",
                     arrival="2025-01-21", status_date="2025-01-%02d" % (6 + i)) for i in range(5)]
        rows.append(_row(booking_id="UB", segment="WEB", status="cancelled", booked_on="2025-03-01",
                          arrival="2025-03-21", updated_on="2025-02-15"))
        cfg = _cfg(); b, rep = ingest.read_bookings(_csv(rows), cfg); ingest.map_segments(b, cfg, rep)
        ingest.impute_cancel_dates(b, rep, seed=1)
        ub = next(x for x in b if x.booking_id == "UB")
        self.assertGreaterEqual(ub.status_date, dt.date(2025, 3, 1))
        self.assertEqual(rep.warnings["updated_on_before_booking"], 1)

    def test_lead_of_zero_lands_on_the_booking_day(self):
        # booked_on == arrival: nothing may divide by the zero-day lead, and the
        # imputed date has nowhere to land but that single day.
        rows = [_row(booking_id="C%d" % i, segment="WEB", status="cancelled", booked_on="2025-01-01",
                     arrival="2025-01-21", status_date="2025-01-%02d" % (6 + i)) for i in range(5)]
        rows.append(_row(booking_id="Z1", segment="WEB", status="cancelled",
                          booked_on="2025-04-01", arrival="2025-04-01"))
        cfg = _cfg(); b, rep = ingest.read_bookings(_csv(rows), cfg); ingest.map_segments(b, cfg, rep)
        ingest.impute_cancel_dates(b, rep, seed=1)
        z1 = next(x for x in b if x.booking_id == "Z1")
        self.assertEqual(z1.status_date, dt.date(2025, 4, 1))

    def test_ratio_of_one_lands_on_the_upper_bound_without_overshoot(self):
        # A pool of a single dated cancellation whose delay equals its lead forces
        # ratio == 1.0; the imputed date must land on the upper bound, not past it.
        rows = [_row(booking_id="CD1", segment="CORP", status="cancelled",
                     booked_on="2025-02-01", arrival="2025-02-11", status_date="2025-02-11")]
        rows.append(_row(booking_id="R1", segment="CORP", status="cancelled",
                          booked_on="2025-05-01", arrival="2025-05-21"))
        cfg = _cfg(); b, rep = ingest.read_bookings(_csv(rows), cfg); ingest.map_segments(b, cfg, rep)
        ingest.impute_cancel_dates(b, rep, seed=1)
        r1 = next(x for x in b if x.booking_id == "R1")
        self.assertEqual(r1.status_date, dt.date(2025, 5, 21))


class InferSellableRooms(unittest.TestCase):
    def test_counts_stayed_and_in_house_including_nonrev_not_no_show_or_day_use(self):
        rows = [_row(booking_id="S%d" % i, segment="WEB", arrival="2025-02-01", nights="1") for i in range(10)]
        rows += [_row(booking_id="N1", segment="COMP", rate="0", arrival="2025-02-01", nights="1")]
        rows += [_row(booking_id="I1", segment="WEB", arrival="2025-02-01", nights="1", status="in_house")]
        rows += [_row(booking_id="X1", segment="WEB", arrival="2025-02-01", nights="1", status="no_show")]
        rows += [_row(booking_id="D1", segment="WEB", arrival="2025-02-01", nights="0")]
        rows += [_row(booking_id="T%d" % i, segment="WEB", arrival="2026-02-01", nights="1") for i in range(12)]
        cfg = _cfg(); b, rep = ingest.read_bookings(_csv(rows), cfg); ingest.map_segments(b, cfg, rep)
        inf = ingest.infer_sellable_rooms(b)
        self.assertEqual(inf.rooms, 12)
        self.assertEqual(inf.second_highest, 12)   # 2025-02-01 has 10 + 1 comp + 1 in_house
        self.assertEqual(inf.per_year_max, {2025: 12, 2026: 12})
        self.assertEqual(inf.nights_within_2pct, 2)


class Replay(unittest.TestCase):
    def _load(self, rows, **over):
        cfg_path = os.path.join(ROOT, "data", "sample-hotel.json")
        if over:
            import json
            with open(cfg_path) as fh: d = json.load(fh)
            d.update(over)
            fd, cfg_path = tempfile.mkstemp(suffix=".json")
            with os.fdopen(fd, "w") as fh: json.dump(d, fh)
        return ingest.load(_csv(rows), cfg_path, seed=1)

    def tearDown(self):
        from pace import calendar as C
        from pace import config
        C.reset_seasonality(); config.reset_segments()

    def test_paid_rooms_plus_nonrev_equals_stayed_rows_per_night(self):
        rows = [_row(booking_id="S%d" % i, segment="WEB", arrival="2025-02-01", nights="2") for i in range(6)]
        rows += [_row(booking_id="N%d" % i, segment="COMP", rate="0", arrival="2025-02-01", nights="1") for i in range(2)]
        res = self._load(rows)
        night = dt.date(2025, 2, 1)
        self.assertEqual(res.ledger.rooms_on(night), 6)
        self.assertEqual(ingest.nonrev_on(res.nonrev, night), 2)
        self.assertEqual(res.ledger.rooms_on(night) + ingest.nonrev_on(res.nonrev, night), 8)
        self.assertEqual(res.ledger.rooms_on(dt.date(2025, 2, 2)), 6)
        self.assertNotIn("NONREV", res.ledger.segment_mix(night))

    def test_snapshots_follow_booking_and_cancel_dates(self):
        rows = [_row(booking_id="A", segment="WEB", booked_on="2025-01-02", arrival="2025-02-01", nights="1"),
                _row(booking_id="B", segment="WEB", booked_on="2025-01-25", arrival="2025-02-01", nights="1"),
                _row(booking_id="C", segment="WEB", booked_on="2025-01-02", arrival="2025-02-01", nights="1",
                     status="cancelled", status_date="2025-01-20")]
        res = self._load(rows)
        night = dt.date(2025, 2, 1)
        self.assertEqual(res.ledger.otb_at(night, 30), 2)   # 2025-01-02: A and C
        self.assertEqual(res.ledger.otb_at(night, 7), 2)    # 2025-01-25: A and B, C cancelled on the 20th
        self.assertEqual(res.ledger.otb_at(night, 0), 2)

    def test_no_show_stays_on_the_books_until_arrival_then_drops(self):
        rows = [_row(booking_id="X", segment="WEB", booked_on="2025-01-02", arrival="2025-02-01", nights="1", status="no_show")]
        res = self._load(rows)
        night = dt.date(2025, 2, 1)
        self.assertEqual(res.ledger.otb_at(night, 7), 1)
        self.assertEqual(res.ledger.settled[night]["rooms_sold"], 0)

    def test_revenue_by_segment_uses_converted_rate(self):
        rows = [_row(booking_id="U", segment="BOOKING", currency="USD", rate="100", arrival="2025-02-01", nights="1")]
        res = self._load(rows)
        self.assertAlmostEqual(res.ledger.seg_revenue[dt.date(2025, 2, 1)]["OTA"], 92.0)

    def test_inference_fills_null_rooms_and_notes_the_ceiling(self):
        rows = [_row(booking_id="S%d" % i, segment="WEB", arrival="2025-02-01", nights="1") for i in range(7)]
        res = self._load(rows, sellable_rooms=None)
        self.assertEqual(res.hotel.rooms, 7); self.assertIsNotNone(res.inference)
        self.assertTrue(any("inferred" in n for n in res.report.notes))

    def test_over_capacity_is_a_warning_not_a_stop(self):
        rows = [_row(booking_id="S%d" % i, segment="WEB", arrival="2025-02-01", nights="1") for i in range(45)]
        res = self._load(rows)   # sample hotel has 40 rooms
        self.assertEqual(res.report.warnings["over_capacity_nights"], 1)

    def test_the_rooms_walked_out_of_the_actuals_are_counted_not_only_the_night(self):
        # settle() removes the excess rooms and their revenue from the night.
        # On a real booking log that edits the hotel's own history, so the size
        # of the edit has to be visible: five rooms left this night, and one
        # night over capacity does not say that on its own.
        rows = [_row(booking_id="S%d" % i, segment="WEB", arrival="2025-02-01", nights="1") for i in range(45)]
        res = self._load(rows)   # sample hotel has 40 rooms
        self.assertEqual(res.report.warnings["over_capacity_nights"], 1)
        self.assertEqual(res.report.warnings["rooms_walked_off_the_actuals"], 5)
        settled = res.ledger.settled[dt.date(2025, 2, 1)]
        self.assertEqual(settled["walked"], 5)
        self.assertEqual(settled["rooms_sold"], 40)

    def test_two_over_capacity_nights_add_their_rooms_together(self):
        rows = [_row(booking_id="A%d" % i, segment="WEB", arrival="2025-02-01", nights="1") for i in range(42)]
        rows += [_row(booking_id="B%d" % i, segment="WEB", arrival="2025-02-05", nights="1") for i in range(43)]
        res = self._load(rows)
        self.assertEqual(res.report.warnings["over_capacity_nights"], 2)
        self.assertEqual(res.report.warnings["rooms_walked_off_the_actuals"], 5)

    def test_a_night_inside_the_room_count_walks_nothing_and_says_nothing(self):
        rows = [_row(booking_id="S%d" % i, segment="WEB", arrival="2025-02-01", nights="1") for i in range(40)]
        res = self._load(rows)
        self.assertEqual(res.report.warnings["over_capacity_nights"], 0)
        self.assertEqual(res.report.warnings["rooms_walked_off_the_actuals"], 0)

    def test_no_shows_drop_before_the_walk_so_the_count_is_the_settlement_not_the_picture(self):
        # settle() releases the night's no-shows first and only then walks
        # whatever is still over the room count. Counted from rooms_on() a
        # moment earlier, the report charges the hotel for rooms that were
        # never in the house at midnight: 42 stayed plus 3 no-show on a
        # 40-room night reads as 5 rooms walked when the ledger walked 2.
        night = dt.date(2025, 2, 1)
        rows = [_row(booking_id="S%d" % i, segment="WEB", arrival="2025-02-01", nights="1") for i in range(42)]
        rows += [_row(booking_id="N%d" % i, segment="WEB", arrival="2025-02-01", nights="1",
                      status="no_show") for i in range(3)]
        res = self._load(rows)
        settled = res.ledger.settled[night]
        self.assertEqual(settled["walked"], 2)
        self.assertEqual(settled["rooms_sold"], 40)
        self.assertEqual(res.report.warnings["rooms_walked_off_the_actuals"], settled["walked"])
        self.assertEqual(res.report.warnings["over_capacity_nights"], 1)

    def test_no_shows_absorbing_the_whole_excess_leave_nothing_to_report(self):
        # 39 stayed plus 2 no-show on a 40-room night. Nothing is walked and
        # every room sold is kept, so a warning here would be telling a hotel
        # to reconcile an edit that never happened.
        night = dt.date(2025, 2, 1)
        rows = [_row(booking_id="S%d" % i, segment="WEB", arrival="2025-02-01", nights="1") for i in range(39)]
        rows += [_row(booking_id="N%d" % i, segment="WEB", arrival="2025-02-01", nights="1",
                      status="no_show") for i in range(2)]
        res = self._load(rows)
        settled = res.ledger.settled[night]
        self.assertEqual(settled["walked"], 0)
        self.assertEqual(settled["rooms_sold"], 39)
        self.assertEqual(res.report.warnings["over_capacity_nights"], 0)
        self.assertEqual(res.report.warnings["rooms_walked_off_the_actuals"], 0)

    def test_rows_beyond_max_lead_or_max_los_are_kept_whole(self):
        rows = [_row(booking_id="L", segment="WEB", booked_on="2024-01-01", arrival="2025-02-01", nights="12")]
        res = self._load(rows)   # max_lead 180, max_los 7
        self.assertEqual(res.ledger.rooms_on(dt.date(2025, 2, 12)), 1)

    def test_load_stops_on_errors_with_all_of_them_listed(self):
        rows = [_row(booking_id="B%d" % i, status="bogus") for i in range(3)] + [_row(booking_id="Z", segment="ZZZ")]
        with self.assertRaises(ingest.IngestError) as cm:
            self._load(rows)
        self.assertIn("3 row errors", str(cm.exception)); self.assertIn("ZZZ", str(cm.exception))

    def test_sample_file_loads(self):
        res = ingest.load(os.path.join(ROOT, "data", "sample-bookings.csv"), os.path.join(ROOT, "data", "sample-hotel.json"))
        self.assertGreater(res.ledger.n_bookings, 100)

    def test_a_valid_file_with_no_booking_rows_is_an_ingest_error_with_a_sentence(self):
        # A header this reader accepts and nothing under it used to reach
        # min() on an empty sequence, which is exactly the stack trace the
        # rest of this path exists to keep away from a hotel.
        with self.assertRaises(ingest.IngestError) as cm:
            self._load([])
        message = str(cm.exception)
        self.assertIn("no booking rows", message)
        self.assertIn("no history to replay", message)
        self.assertNotIn("min()", message)


class BookingIdRules(unittest.TestCase):
    def test_an_empty_booking_id_is_reported_as_empty_not_as_a_duplicate(self):
        rows = [_row(booking_id=""), _row(booking_id="", arrival="2025-03-01")]
        _, rep = ingest.read_bookings(_csv(rows), _cfg())
        self.assertEqual(len(rep.errors), 2)
        for _row_no, column, message in rep.errors:
            self.assertEqual(column, "booking_id")
            self.assertIn("empty", message)
            self.assertNotIn("duplicate", message)

    def test_a_real_duplicate_still_says_duplicate(self):
        rows = [_row(booking_id="B1"), _row(booking_id="B1", arrival="2025-03-01")]
        _, rep = ingest.read_bookings(_csv(rows), _cfg())
        self.assertEqual(len(rep.errors), 1)
        self.assertIn("duplicate", rep.errors[0][2])


class BookingLogDocExample(unittest.TestCase):
    """docs/booking-log.md is the file that goes to a hotel. Its hotel.json
    example is the first thing a hotel copies, so it has to be the whole file
    and it has to load: an example short of the required fields costs the
    hotel a round trip to be told about eleven missing keys."""

    def _json_blocks(self):
        with open(os.path.join(ROOT, "docs", "booking-log.md"), encoding="utf-8") as fh:
            text = fh.read()
        blocks, rest = [], text
        while "```json\n" in rest:
            rest = rest.split("```json\n", 1)[1]
            body, rest = rest.split("```", 1)
            blocks.append(body)
        return blocks

    def test_the_example_is_the_sample_hotel_file_itself(self):
        blocks = self._json_blocks()
        self.assertEqual(len(blocks), 1)
        with open(os.path.join(ROOT, "data", "sample-hotel.json"), encoding="utf-8") as fh:
            sample = fh.read()
        self.assertEqual(blocks[0].strip(), sample.strip())

    def test_the_example_loads_through_the_real_loader(self):
        import json as _json
        raw = _json.loads(self._json_blocks()[0])
        for key in HC.REQUIRED:
            self.assertIn(key, raw, "the documented example is missing %s" % key)
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            _json.dump(raw, fh)
        cfg = HC.load_hotel_json(path)
        self.assertEqual(len(cfg.price_month_factor), 12)
        self.assertIn("CORP", cfg.segment_rate_ratio)
        self.assertIn("GROUP", cfg.segment_rate_ratio)

    def test_the_document_says_what_happens_to_an_over_capacity_night(self):
        with open(os.path.join(ROOT, "docs", "booking-log.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("rooms_walked_off_the_actuals", text)
        self.assertIn("### Nights that go over the room count", text)
