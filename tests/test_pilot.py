import csv
import datetime as dt
import os
import random
import tempfile
import unittest

from pace import hotelconfig as HC
from pace import ingest
from pace import pilot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_HOTEL = os.path.join(ROOT, "tests", "fixtures", "pilot-hotel.json")
HEADER = ["booking_id", "booked_on", "arrival", "nights", "rooms", "rate", "currency",
          "segment", "rate_code", "source", "room_type", "company", "status",
          "status_date", "updated_on"]


def _csv(rows, header=HEADER):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


def _row(**kw):
    base = dict(booking_id="B1", booked_on="2024-01-01", arrival="2024-02-01", nights="1",
                rooms="1", rate="120", currency="EUR", segment="WEB", rate_code="",
                source="", room_type="STD", company="", status="stayed",
                status_date="", updated_on="")
    base.update(kw)
    return [base[h] for h in HEADER]


def history_rows(first_arrival, nights_count, seed=3):
    """A synthetic year with a real pace curve behind it.

    Friday and Saturday run fuller, July and August run fuller again, and lead
    times spread from 35 days to 1, so the on the books at lead 30 is a long
    way below the final and a leak is visible rather than plausible.
    """
    rng = random.Random(seed)
    rows = []
    n = 0
    for k in range(nights_count):
        d = first_arrival + dt.timedelta(days=k)
        target = 9 + (4 if d.weekday() in (4, 5) else 0) + (3 if d.month in (7, 8) else 0)
        target = max(3, min(19, target + rng.randint(-2, 2)))
        for _ in range(target):
            n += 1
            lead = rng.choice([1, 2, 3, 5, 7, 10, 14, 18, 21, 25, 30, 33, 35])
            rows.append(_row(booking_id="S%05d" % n,
                             segment=rng.choice(["WEB", "WEB", "BOOKING", "CORP"]),
                             booked_on=(d - dt.timedelta(days=lead)).isoformat(),
                             arrival=d.isoformat(), nights="1",
                             rate="%.2f" % (100.0 + rng.randint(0, 70))))
    return rows


FIRST_ARRIVAL = dt.date(2024, 1, 1)
# Long enough that the same-time-last-year baseline exists inside the scoring
# window: it reads the night 364 days back, so a history of one year scores
# nothing at all and every table comes back empty.
NIGHTS = 500
SCORE_FIRST = dt.date(2025, 2, 1)
SCORE_LAST = dt.date(2025, 4, 30)
TEST_MARKS = (30, 14, 7)
_WALK = {}


def walked():
    """One walk, built once and shared: it costs about a second."""
    if "res" not in _WALK:
        path = _csv(history_rows(FIRST_ARRIVAL, NIGHTS))
        res = ingest.load(path, FIXTURE_HOTEL, seed=1)
        cal = HC.event_calendar(res.cfg)
        out = pilot.walk(res.bookings, res.hotel, cal,
                         first_stay=FIRST_ARRIVAL,
                         last_stay=FIRST_ARRIVAL + dt.timedelta(days=NIGHTS - 1),
                         score_first=SCORE_FIRST, score_last=SCORE_LAST,
                         marks=TEST_MARKS,
                         drive_from=SCORE_FIRST - dt.timedelta(days=45),
                         warmup_nights=60)
        _WALK["res"] = res
        _WALK["walk"] = out
    else:
        # Every test class resets seasonality and segments in tearDown, so put
        # the fixture hotel back before handing the cached walk to the next one.
        HC.apply(_WALK["res"].cfg)
    return _WALK["res"], _WALK["walk"]


class LeakGuard(unittest.TestCase):
    """A forecast asked about the past must not be answered with the present."""

    def tearDown(self):
        from pace import calendar as C
        from pace import config
        C.reset_seasonality()
        config.reset_segments()

    def test_every_forecast_saw_the_snapshot_of_its_own_day(self):
        _res, out = walked()
        checked = 0
        for (d, lead), rec in out.records.items():
            if rec.forecast is None:
                continue
            self.assertEqual(rec.forecast_otb, rec.otb,
                             "forecast for %s at lead %d saw %s on the books, snapshot says %s"
                             % (d, lead, rec.forecast_otb, rec.otb))
            self.assertEqual(rec.otb, out.ledger.otb_at(d, lead))
            checked += 1
        self.assertGreater(checked, 100)

    def test_the_guard_is_not_vacuous(self):
        """If every night finished where it stood, the guard would pass on a leak."""
        _res, out = walked()
        gaps = [out.ledger.settled[d]["rooms_sold"] - rec.otb
                for (d, lead), rec in out.records.items() if lead == 30]
        self.assertGreaterEqual(max(gaps), 4)

    def test_the_naive_call_answers_with_the_present(self):
        """The trap itself, kept in the suite so it cannot be reintroduced quietly."""
        _res, out = walked()
        d, lead = max((k for k in out.records if k[1] == 30),
                      key=lambda k: out.ledger.settled[k[0]]["rooms_sold"] - out.records[k].otb)
        naive = out.engine.forecaster.forecast(out.ledger, d, d - dt.timedelta(days=lead))
        self.assertEqual(naive.otb, out.ledger.rooms_on(d))
        self.assertGreater(naive.otb, out.records[(d, lead)].otb)

    def test_the_guard_rejects_records_built_the_naive_way(self):
        """The guard is run against the naive pilot, the one that forecasts
        after the walk has finished, and it has to reject it.  This is what
        makes the assertion in the first test load bearing rather than true by
        construction."""
        res, out = walked()
        naive = {}
        for (d, lead) in out.records:
            asof = d - dt.timedelta(days=lead)
            fc = out.engine.forecaster.forecast(res.ledger, d, asof)
            naive[(d, lead)] = pilot.WalkRecord(
                stay_date=d, lead=lead, asof=asof, otb=res.ledger.otb_at(d, lead),
                forecast_otb=fc.otb, forecast=fc.expected_bookings)
        rejected = [k for k, rec in naive.items() if rec.forecast_otb != rec.otb]
        self.assertGreater(len(rejected), 100,
                           "the naive records passed the guard, so the guard is not a guard")

    def test_the_walk_leaves_the_ledger_ingest_load_would_have_built(self):
        """The day order is the ingest's day order, so the snapshots agree exactly."""
        res, out = walked()
        self.assertEqual(res.ledger.n_bookings, out.ledger.n_bookings)
        self.assertEqual(res.ledger.n_cancels, out.ledger.n_cancels)
        for d in sorted(res.ledger.snapshots):
            self.assertEqual(res.ledger.snapshots[d], out.ledger.snapshots[d], str(d))
        for d in sorted(res.ledger.settled):
            self.assertEqual(res.ledger.settled[d]["rooms_sold"],
                             out.ledger.settled[d]["rooms_sold"], str(d))
