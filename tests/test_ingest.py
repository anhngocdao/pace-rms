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
