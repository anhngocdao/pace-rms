import collections
import contextlib
import csv
import dataclasses
import datetime as dt
import hashlib
import io
import json
import math
import os
import random
import tempfile
import unittest
from collections import defaultdict

from pace import baselines
from pace import holdout
from pace import hotelconfig as HC
from pace import ingest
from pace import pilot
from pace import ratecheck
from pace import score as S
from pace.elasticity import acceptance as _price_acceptance
from pace.ledger import Ledger
from pace.policy import PaceEngine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_HOTEL = os.path.join(ROOT, "tests", "fixtures", "pilot-hotel.json")
HEADER = ["booking_id", "booked_on", "arrival", "nights", "rooms", "rate", "currency",
          "segment", "rate_code", "source", "room_type", "meal", "company", "status",
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
                source="", room_type="STD", meal="BB", company="", status="stayed",
                status_date="", updated_on="")
    base.update(kw)
    return [base[h] for h in HEADER]


def _keep_probability(ratio, k=2.2):
    """Share of a night's price-elastic demand that still books at `ratio`
    times the reference rate, using the same logistic choice curve
    `pace/elasticity.py` fits.  Used to give the synthetic rate a genuine
    negative relationship with volume: without it every row's rate is noise
    uncorrelated with how many rooms sold, `fit_segment` shrinks every
    segment's k to the grid floor, and the engine has no reason ever to
    publish anything but the top ladder rung.
    """
    return min(1.0, _price_acceptance(k, ratio) / 2.0)


def history_rows(first_arrival, nights_count, seed=3):
    """A synthetic year and a bit with a real pace curve behind it, and every
    hazard the walk has to survive on real history.

    Friday and Saturday run fuller, July and August run fuller again, and lead
    times spread from 35 days to 1, so the on the books at lead 30 is a long
    way below the final and a leak is visible rather than plausible.

    Each night draws its own price regime for the price-elastic segments
    (RETAIL and OTA, booked here as WEB and BOOKING): a night priced above
    reference keeps fewer of its candidate rows, a night priced below keeps
    more, and the row's own rate is set from that same regime.  That is what
    gives `pace/elasticity.py`'s fit something real to recover, which is what
    lets the published rate move off the ladder's top rung instead of pinning
    there for lack of any signal that higher rates cost volume.

    A meaningful share of the demand cancels, roughly half of that on the same
    day it was booked, which is exactly the booking a cancel-before-book day
    order keeps by mistake.  A smaller share no-shows, walking out of the
    ledger only at settlement.  A few rows are non-revenue (comped) and a few
    more are zero-night day use; both are filtered out of the ledger entirely,
    the same way `pace/ingest.py` filters them.  About a fifth of the
    survivors stay more than one night.  One night, well clear of the scoring
    window and its drive-in, gets a deliberate extra push of stayed bookings
    so the busiest night in the house goes over the fixture hotel's twenty
    rooms and `Ledger.settle`'s walk branch actually runs.
    """
    rng = random.Random(seed)
    # Board and room type are drawn from a stream of their own.  Neither
    # reaches the ledger (`ingest._Req` carries a target, a room count and
    # dates, and nothing else), so the walk is the same walk with or without
    # them; drawing them from `rng` would nonetheless shift every later date,
    # rate and status in this history and move numbers in tests that have
    # nothing to do with board.
    mix = random.Random(seed + 101)
    rows = []
    n = 0
    for k in range(nights_count):
        d = first_arrival + dt.timedelta(days=k)
        target = 6 + (5 if d.weekday() in (4, 5) else 0) + (4 if d.month in (7, 8) else 0)
        target = max(2, min(19, target + rng.randint(-3, 3)))
        # Reference for RETAIL/OTA is base_rate (120) times a flat month
        # factor of 1.0 everywhere in this fixture, so 120 is the ratio's
        # own denominator here.
        ratio_d = rng.choice([0.75, 0.85, 1.0, 1.0, 1.15, 1.3])
        for _ in range(target):
            lead = rng.choice([1, 2, 3, 5, 7, 10, 14, 18, 21, 25, 30, 33, 35])
            booked_on = d - dt.timedelta(days=lead)
            segment = rng.choice(["WEB", "WEB", "BOOKING", "CORP"])
            if segment in ("WEB", "BOOKING"):
                if rng.random() > _keep_probability(ratio_d):
                    continue                                     # priced out at this night's regime
                rate = "%.2f" % max(1.0, ratio_d * 120.0 + rng.uniform(-6, 6))
            else:
                rate = "%.2f" % (96.0 + rng.uniform(-6, 6))       # CORP: its own contracted ratio, 0.8x
            n += 1
            nights = "1"
            if k < nights_count - 3 and rng.random() < 0.18:
                nights = str(rng.choice([2, 3]))
            roll = rng.random()
            kw = dict(booking_id="S%05d" % n, segment=segment, booked_on=booked_on.isoformat(),
                     arrival=d.isoformat(), nights=nights, rate=rate,
                     meal=mix.choice(["BB", "BB", "HB"]),
                     room_type=mix.choice(["STD", "STD", "STD", "SUP"]))
            if roll < 0.03:
                kw.update(segment="COMP")                       # non-revenue: never reaches the ledger
            elif roll < 0.06:
                kw.update(nights="0")                            # day use: never reaches the ledger
            elif roll < 0.10:
                kw.update(status="no_show")
            elif roll < 0.22:
                kw.update(status="cancelled", status_date=booked_on.isoformat())     # same-day cancel
            elif roll < 0.32:
                gap = rng.randint(1, max(1, lead - 1))
                kw.update(status="cancelled", status_date=(booked_on + dt.timedelta(days=gap)).isoformat())
            rows.append(_row(**kw))

    # One deliberate spike, far from the scoring window, so the busiest night
    # of the whole history overbooks the house.  Unfiltered by the price
    # regime above: guaranteed stayed business, deliberately larger than any
    # plausible baseline night so the over-capacity margin survives whatever
    # the random background demand does on this particular night.
    spike_day = first_arrival + dt.timedelta(days=220)
    for _ in range(26):
        n += 1
        lead = rng.choice([1, 2, 3, 5])
        rows.append(_row(booking_id="S%05d" % n, segment=rng.choice(["WEB", "BOOKING"]),
                         booked_on=(spike_day - dt.timedelta(days=lead)).isoformat(),
                         arrival=spike_day.isoformat(), nights="1",
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
    """One walk, built once and shared: it costs about a second.

    `pace/pipeline.py` loads the real plugins/ directory (a late-announcement
    signal, brand-floor and charm-pricing rules) into the module-level
    registry in `pace/plugins.py` and never resets it.  Run this file inside
    the full suite, after `test_golden.py` has run `pipeline.run()`, and
    those plugins are still registered when this walk's engine calls
    recommend(); run this file alone and the registry is empty.  Either is a
    legitimate way to run one test file, but this walk's numbers cannot
    depend on which one happened to run first, so plugins are reset to empty
    before this walk is ever built.
    """
    if "res" not in _WALK:
        from pace import plugins as _plugins
        _plugins.reset()
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


def _replay_controls_at(res, stop_asof, drive_from, warmup_nights=60, refit_every=28):
    """An independent day loop over the same rows `walked()` used, stopped at
    one asof, with its own fresh Ledger and PaceEngine.

    This mirrors pilot.walk()'s own mechanics exactly (fit once before the
    first controls() call, controls before that day's rows arrive, book then
    cancel then snapshot then settle, observe at the end of the day) so its
    engine reaches the same state pilot.walk()'s engine reaches at the same
    asof.  That is what makes it useful as a check: it is the only thing that
    does not share pilot.walk()'s own filing.  Reading published_rate or
    solved_on back off the walk's own output and asserting something about
    it proves nothing about which stay_date it was filed under; a rate and a
    solve day computed here, independently, by a loop that never touches a
    WalkRecord, is something a wrong key in the walk's filing has no reason
    to agree with by accident.
    """
    hotel = res.hotel
    cal = HC.event_calendar(res.cfg)
    ledger = Ledger(hotel, FIRST_ARRIVAL, FIRST_ARRIVAL + dt.timedelta(days=NIGHTS - 1))
    engine = PaceEngine(hotel, cal, refit_every=refit_every, warmup_nights=warmup_nights)
    by_day = defaultdict(list)
    cancels = defaultdict(list)
    for b in res.bookings:
        if b.target in (None, "NONREV") or b.nights == 0:
            continue
        by_day[b.booked_on].append(b)
        if b.status == "cancelled" and b.status_date is not None:
            cancels[b.status_date].append(b)
    holds = {}
    day = min(by_day)
    settled_upto = FIRST_ARRIVAL - dt.timedelta(days=1)
    rid = 0
    ctrl = None
    while day <= stop_asof:
        driving = day >= drive_from
        if driving and engine.last_fit is None:
            engine.observe(day, ledger)
        ctrl = engine.controls(day, ledger) if driving else None
        for b in by_day.get(day, ()):
            rid += 1
            hold = ledger.book(day, ingest._Req(rid, b.target, 1, b.arrival, b.nights), b.rate)
            if b.status == "no_show":
                hold.no_show = True
            holds[b.booking_id] = hold
        for b in cancels.get(day, ()):
            hold = holds.pop(b.booking_id, None)
            if hold is not None:
                ledger.cancel(hold)
        ledger.snapshot(day, hotel.max_lead)
        while settled_upto < day and settled_upto < ledger.last_stay:
            night = settled_upto + dt.timedelta(days=1)
            if night >= FIRST_ARRIVAL:
                ledger.settle(night)
            settled_upto = night
        if driving:
            engine.observe(day, ledger)
        day += dt.timedelta(days=1)
    return ctrl, engine


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
            # otb equality alone cannot see a forecaster whose curves were
            # learned on data from after asof: rooms_on/otb never depend on
            # which fit produced them.  fit_asof does: it is read from the
            # engine's own fit log, not from the walk's bookkeeping, so it
            # has to have happened on or before the day it is answering for.
            self.assertIsNotNone(rec.fit_asof,
                                 "forecast for %s at lead %d has no fit behind it at all"
                                 % (d, lead))
            self.assertLessEqual(rec.fit_asof, rec.asof,
                                 "forecast for %s at lead %d was answered by a fit taken on %s, "
                                 "after its own asof of %s" % (d, lead, rec.fit_asof, rec.asof))
            if rec.solved_on is not None:
                self.assertLessEqual(rec.solved_on, rec.asof,
                                     "the rate for %s was filed as solved on %s, after its own "
                                     "asof of %s" % (d, rec.solved_on, rec.asof))
            checked += 1
        self.assertGreater(checked, 100)

    def test_the_published_rate_moves(self):
        """Every record publishing the same rate would hide a bug in which
        date's rate gets filed onto a record: a constant can't distinguish a
        swapped date from a correct one."""
        _res, out = walked()
        rates = set(rec.published_rate for rec in out.records.values()
                   if rec.published_rate is not None)
        self.assertGreater(len(rates), 1, "every record published the same rate: %r" % rates)

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

    def test_the_fit_recency_assertion_would_reject_a_forecaster_fit_on_the_whole_history(self):
        """This test is about the shape of the assertion `fit_asof <= asof`,
        not about pilot.walk()'s own wiring: it hand-builds WalkRecords and
        asserts on values it just set, so it would stay green even if the
        line that fills fit_asof inside walk() were deleted.  The wiring
        itself, i.e. that walk() actually populates fit_asof correctly from
        the real engine as it runs, is what
        test_every_forecast_saw_the_snapshot_of_its_own_day exercises, since
        it reads fit_asof off real WalkRecords that only pilot.walk()
        produced.

        What this test shows instead: otb equality alone cannot catch a
        forecaster whose curves were learned on data from after its own
        asof, because rooms_on(d) never depends on which fit produced the
        curves, only on which ledger and stay_date were asked about.  Build a
        forecaster fit on the whole walked history (the way the reviewer
        demonstrated: a walk whose ledger is honest but whose
        engine.forecaster was fit at the very end, after every record) and
        show the otb check is blind to it while the fit-recency check is not.
        The shared engine is never touched: a separate Forecaster is built
        from the same building blocks PaceEngine._fit uses, so this test
        cannot corrupt the cached walk for the tests that run after it."""
        res, out = walked()
        from pace.otb import build_pace_curves
        from pace.elasticity import fit_all
        from pace.unconstrain import class_demand
        from pace.forecast import Forecaster

        final_asof = out.ledger.last_stay + dt.timedelta(days=1)
        completed = [d for d in out.ledger.settled if d < final_asof]
        curves = build_pace_curves(out.ledger, completed, max_lead=res.hotel.max_lead)
        response = fit_all(out.ledger, res.hotel, out.engine.cal, completed, None)
        demand = class_demand(out.ledger, completed, res.hotel, out.engine.cal, response)
        future_forecaster = Forecaster(res.hotel, out.engine.cal, curves, demand, response)

        naive = {}
        for (d, lead), rec in out.records.items():
            if rec.forecast is None:
                continue
            fc = future_forecaster.forecast(out.ledger, d, rec.asof)
            naive[(d, lead)] = pilot.WalkRecord(
                stay_date=d, lead=lead, asof=rec.asof, otb=rec.otb,
                forecast_otb=fc.otb, forecast=fc.expected_bookings, fit_asof=final_asof)

        otb_blind = sum(1 for rec in naive.values() if rec.forecast_otb == rec.otb)
        self.assertGreater(otb_blind, 100,
                           "otb should still agree: rooms_on never depends on which fit "
                           "produced the curves, so the otb check alone cannot see this leak")

        rejected = [k for k, rec in naive.items() if rec.fit_asof > rec.asof]
        self.assertGreater(len(rejected), 100,
                           "the fit-recency guard did not reject records answered by a "
                           "forecaster fit on the whole history, so it is not a guard")

    def test_published_rate_and_solved_on_are_filed_under_the_correct_stay_date(self):
        """ctrl.rate.get(d) and engine.last_solved.get(d) are both plain dict
        reads keyed by stay_date.  A wrong key, a neighbouring date read by
        mistake, still returns a real rate and a real solve day, so nothing
        the walk's own output says about itself can catch it.  An
        independently replayed day loop (`_replay_controls_at`) can: it
        recomputes ctrl fresh, keyed correctly by construction, so it has no
        reason to share a mistake in the walk's own filing.

        The three stay dates below were picked (from `python3 -c` against
        `walked()`) because each has a calendar neighbour whose rate genuinely
        differs at this asof: 2025-03-03 published 152.0 against a neighbour
        at 150.0; 2025-03-09 published 154.0 against a neighbour at 152.0;
        2025-03-16 published 154.0 against a neighbour at 152.0.  Filing any
        of them under d + 1 day instead of d would read a different number,
        which is what makes this check able to fail rather than pass on a
        wrong key by coincidence.  A self-check below re-confirms the pair
        still disagrees before trusting it.
        """
        res, out = walked()
        stop_asof = dt.date(2025, 3, 2)
        drive_from = SCORE_FIRST - dt.timedelta(days=45)
        ctrl, engine = _replay_controls_at(res, stop_asof, drive_from, warmup_nights=60)

        cases = [
            (dt.date(2025, 3, 3), 1, 152.0, 150.0),
            (dt.date(2025, 3, 9), 7, 154.0, 152.0),
            (dt.date(2025, 3, 16), 14, 154.0, 152.0),
        ]
        checked = 0
        for stay_date, lead, expected_rate, neighbour_rate in cases:
            rec = out.records[(stay_date, lead)]
            self.assertEqual(rec.asof, stop_asof)

            oracle_rate = ctrl.rate[stay_date]
            oracle_neighbour = ctrl.rate[stay_date + dt.timedelta(days=1)]
            self.assertNotEqual(oracle_rate, oracle_neighbour,
                               "%s and its neighbour no longer publish different rates; this "
                               "pair can no longer catch a wrong key" % stay_date)
            self.assertEqual(oracle_rate, expected_rate)
            self.assertEqual(oracle_neighbour, neighbour_rate)

            self.assertEqual(rec.published_rate, oracle_rate,
                             "the rate filed for %s at lead %d was %s; the independently "
                             "replayed controls() says it should have been %s"
                             % (stay_date, lead, rec.published_rate, oracle_rate))

            oracle_solved = engine.last_solved.get(stay_date)
            oracle_solved_day = oracle_solved[0] if oracle_solved else None
            self.assertEqual(rec.solved_on, oracle_solved_day,
                             "the solve day filed for %s at lead %d was %s; the independently "
                             "replayed engine says it should have been %s"
                             % (stay_date, lead, rec.solved_on, oracle_solved_day))
            checked += 1
        self.assertEqual(checked, len(cases))

    def test_engine_ready_matches_the_engine_s_own_fit_log(self):
        """engine_ready is supposed to mean not ready before the engine's own
        first fit and ready from then on.  Check it against
        engine.fit_log[0], which nothing in a record's own construction
        controls, rather than assume a constant.

        Every scored record in this fixture's driven window falls on or
        after the engine's first fit: pilot.walk() forces that fit before the
        first controls() call of the driven window (its own comment says so),
        and by the time driving starts here roughly 350 nights have already
        settled, far more than warmup_nights=60.  So every record below is
        expected True, and there is no reachable False example in this
        fixture to also check; a constant True would coincidentally match
        every one of them too.  What this test adds over a constant is that
        the expectation is read from engine.fit_log rather than asserted
        outright, so a hard-wired False (the review's own injected bug) is
        still caught: it disagrees with what the fit log says every record
        should be.
        """
        res, out = walked()
        self.assertGreater(len(out.engine.fit_log), 0)
        first_fit = dt.date.fromisoformat(out.engine.fit_log[0]["asof"])
        mismatches = 0
        checked = 0
        for (d, lead), rec in out.records.items():
            expected = rec.asof >= first_fit
            if rec.engine_ready != expected:
                mismatches += 1
            checked += 1
        self.assertEqual(mismatches, 0,
                         "%d of %d records disagreed with the engine's own fit log about "
                         "whether it was ready yet" % (mismatches, checked))
        self.assertGreater(checked, 100)


class WalkValidation(unittest.TestCase):
    """walk() refuses a configuration it cannot answer honestly instead of
    quietly handing back a record built on a None or a rate that predates
    anything the caller asked for."""

    def setUp(self):
        cfg = HC.load_hotel_json(FIXTURE_HOTEL)
        self.hotel = HC.apply(cfg)
        self.cal = HC.event_calendar(cfg)

    def tearDown(self):
        from pace import calendar as C
        from pace import config
        C.reset_seasonality()
        config.reset_segments()

    def test_a_lead_mark_deeper_than_max_lead_is_refused(self):
        """A mark past max_lead leaves ledger.snapshot with nothing to answer
        with: otb_at would come back None instead of a number, and the guard
        would fail on a comparison against None instead of a sentence."""
        with self.assertRaises(pilot.PilotError):
            pilot.walk([], self.hotel, self.cal,
                       first_stay=FIRST_ARRIVAL, last_stay=FIRST_ARRIVAL + dt.timedelta(days=10),
                       score_first=FIRST_ARRIVAL, score_last=FIRST_ARRIVAL + dt.timedelta(days=10),
                       marks=(self.hotel.max_lead + 1,), drive_from=FIRST_ARRIVAL)

    def test_a_drive_date_after_the_scoring_window_is_refused(self):
        with self.assertRaises(pilot.PilotError):
            pilot.walk([], self.hotel, self.cal,
                       first_stay=FIRST_ARRIVAL, last_stay=FIRST_ARRIVAL + dt.timedelta(days=10),
                       score_first=FIRST_ARRIVAL, score_last=FIRST_ARRIVAL + dt.timedelta(days=5),
                       marks=(1,), drive_from=FIRST_ARRIVAL + dt.timedelta(days=6))

    def test_a_backwards_scoring_window_is_refused(self):
        with self.assertRaises(pilot.PilotError):
            pilot.walk([], self.hotel, self.cal,
                       first_stay=FIRST_ARRIVAL, last_stay=FIRST_ARRIVAL + dt.timedelta(days=10),
                       score_first=FIRST_ARRIVAL + dt.timedelta(days=5), score_last=FIRST_ARRIVAL,
                       marks=(1,), drive_from=FIRST_ARRIVAL)


class Windows(unittest.TestCase):
    def tearDown(self):
        from pace import calendar as C
        from pace import config
        C.reset_seasonality()
        config.reset_segments()

    def _bookings(self, rows):
        res = ingest.load(_csv(rows), FIXTURE_HOTEL, seed=1)
        return res

    def test_the_front_is_trimmed_by_the_longest_stays(self):
        # Fixed from the brief: with `range(99)` background rows the single
        # 6-night outlier was swamped (100 values, 99 of them 1s), so the
        # 99th percentile landed at 1.05 (int 1) instead of pulling the trim
        # toward the outlier, and `first` came out one day off arrival, not
        # five.  Nine background rows plus the outlier (ten total) gives
        # percentile 5.55 (int 5), matching the trim asserted below; checked
        # against pace.pilot.percentile directly before changing this.  A
        # late, unrelated one-night booking (arrival in June) is added so the
        # back of the window, taken from the longest real departure, lands
        # far past the trimmed front instead of colliding with it the way it
        # did when every row arrived on the same day as the outlier.
        rows = [_row(booking_id="A%d" % i, arrival="2024-01-01", nights="1") for i in range(9)]
        rows += [_row(booking_id="L", arrival="2024-01-01", nights="6")]
        rows += [_row(booking_id="Z", arrival="2024-06-01", nights="1")]
        res = self._bookings(rows)
        first, _last = pilot.stay_window(res.bookings, dt.date(2024, 12, 31))
        self.assertEqual(first, dt.date(2024, 1, 1) + dt.timedelta(days=5))

    def test_the_back_is_capped_by_the_export(self):
        # Fixed from the brief: ten bookings all arriving on 2024-01-10 gave a
        # natural last of 2024-01-10 (departure minus one day), which is
        # before the trimmed first of 2024-01-11 (the 99th percentile of ten
        # equal 1-night stays still trims 1 day off the single arrival day),
        # so stay_window raised PilotError on an empty window before the cap
        # could be observed at all.  Spreading the ten arrivals over ten
        # different days keeps the natural last well past the trimmed front,
        # so last_cap is what actually decides `last`.
        rows = [_row(booking_id="A%d" % i,
                     arrival=(dt.date(2024, 1, 1) + dt.timedelta(days=i)).isoformat(),
                     nights="1") for i in range(10)]
        res = self._bookings(rows)
        _first, last = pilot.stay_window(res.bookings, dt.date(2024, 1, 5))
        self.assertEqual(last, dt.date(2024, 1, 5))

    def test_a_log_with_nothing_in_the_ledger_is_refused_in_a_sentence(self):
        rows = [_row(booking_id="C%d" % i, segment="COMP", rate="0") for i in range(3)]
        res = self._bookings(rows)
        with self.assertRaises(pilot.PilotError):
            pilot.stay_window(res.bookings, dt.date(2024, 12, 31))

    def test_capacity_ignores_a_comp_room_entered_after_the_forecast_day(self):
        rows = [_row(booking_id="S%d" % i, arrival="2024-02-01", nights="1") for i in range(5)]
        rows += [_row(booking_id="C1", segment="COMP", rate="0", booked_on="2024-01-05",
                      arrival="2024-02-01", nights="1"),
                 _row(booking_id="C2", segment="COMP", rate="0", booked_on="2024-01-31",
                      arrival="2024-02-01", nights="1")]
        res = self._bookings(rows)
        night = dt.date(2024, 2, 1)
        self.assertEqual(pilot.capacity_on(res.hotel, res.nonrev, night, dt.date(2024, 1, 10)), 19)
        self.assertEqual(pilot.capacity_on(res.hotel, res.nonrev, night, dt.date(2024, 2, 1)), 18)
        self.assertEqual(pilot.capacity_on(res.hotel, res.nonrev, night), 18)

    def test_a_night_full_only_thanks_to_a_comp_room_is_invisible_to_the_ledger(self):
        # 20 rooms, sell-out threshold 0.97, so the ledger needs 20 rooms sold
        # to call the night full.  19 sold plus one comp is full in the house
        # but reads short in the ledger.  02-03 is a third night, full and
        # sold out on its own with no comp room at all, so it must count
        # toward physically_full without also counting toward
        # full_but_invisible: without this night, a gap() that always set
        # full_but_invisible equal to physically_full (an unconditional
        # "true" in place of the settled comparison) would still pass, since
        # the only physically full night in the original fixture happened to
        # also be the only invisible one.  On the real data the split is 153
        # physically full against 49 invisible, so this comparison is the
        # function's central discrimination.
        rows = [_row(booking_id="S%d" % i, arrival="2024-02-01", nights="1") for i in range(19)]
        rows += [_row(booking_id="C1", segment="COMP", rate="0", arrival="2024-02-01", nights="1")]
        rows += [_row(booking_id="T%d" % i, arrival="2024-02-02", nights="1") for i in range(4)]
        rows += [_row(booking_id="V%d" % i, arrival="2024-02-03", nights="1") for i in range(20)]
        res = self._bookings(rows)
        gap = pilot.full_night_gap(res.bookings, res.ledger, res.hotel,
                                   dt.date(2024, 2, 1), dt.date(2024, 2, 3))
        self.assertEqual(gap["nights"], 3)
        self.assertEqual(gap["physically_full"], 2)
        self.assertEqual(gap["full_but_invisible"], 1)
        # No no-shows and no walk in this fixture, so the lead-0 snapshot and
        # the settled figure agree on both full nights: the snapshot-based
        # count lands on the same night as the settled-based one here, even
        # though the two reads can disagree on real data.
        self.assertEqual(gap["full_but_uncensored_by_snapshot"], 1)

    def test_the_snapshot_count_reads_the_snapshot_and_not_the_settled_figure(self):
        """The two counts read two figures, and a fixture where the figures
        agree cannot tell a function that reads the settled figure twice from
        one that reads each once.  Here they part: 02-01 has nineteen stayed
        singles, one no-show and one comp room, so the house is physically
        full at twenty, the lead-0 snapshot holds twenty because the no-show
        is still on the books when it is taken, and the settlement, which
        releases the no-show, records nineteen.  Full but invisible by the
        settled figure; censored, and so not in the second count, by the
        snapshot.  02-02 is nineteen stayed and one comp room, short of the
        cut in both reads, and 02-03 is twenty stayed, full in both.  So the
        settled count is two nights and the snapshot count is one; a second
        count that re-read the settled figure would say two."""
        rows = [_row(booking_id="S%d" % i, arrival="2024-02-01", nights="1") for i in range(19)]
        rows += [_row(booking_id="N1", arrival="2024-02-01", nights="1", status="no_show"),
                 _row(booking_id="C1", segment="COMP", rate="0", arrival="2024-02-01", nights="1")]
        rows += [_row(booking_id="T%d" % i, arrival="2024-02-02", nights="1") for i in range(19)]
        rows += [_row(booking_id="C2", segment="COMP", rate="0", arrival="2024-02-02", nights="1")]
        rows += [_row(booking_id="V%d" % i, arrival="2024-02-03", nights="1") for i in range(20)]
        res = self._bookings(rows)
        night = dt.date(2024, 2, 1)
        self.assertEqual(ingest.physical_occupancy(res.bookings)[night], 20)
        self.assertEqual(res.ledger.snapshots[night][0], 20)
        self.assertEqual(res.ledger.settled[night]["rooms_sold"], 19)
        gap = pilot.full_night_gap(res.bookings, res.ledger, res.hotel,
                                   dt.date(2024, 2, 1), dt.date(2024, 2, 3))
        self.assertEqual(gap["nights"], 3)
        self.assertEqual(gap["physically_full"], 3)
        self.assertEqual(gap["full_but_invisible"], 2)
        self.assertEqual(gap["full_but_uncensored_by_snapshot"], 1)

    def test_the_trim_is_measured_against_the_rows_that_reach_the_ledger(self):
        """windows.trim_nights describes the trim stay_window took, so it has
        to be measured against the population stay_window trimmed.

        One comped room entered a month before the first revenue arrival is
        not in that population: it never reaches the ledger, it never moved
        the window, and a trim measured from the raw file would report 33
        nights of trimming where 3 happened.  It does not bite on H1 or H2
        today, where the earliest arrival is the same in both populations,
        which is exactly why it needs a test rather than a reader.
        """
        rows = [_row(booking_id="C1", segment="COMP", rate="0",
                     booked_on="2023-12-01", arrival="2023-12-02", nights="1")]
        rows += [_row(booking_id="A%d" % i, arrival="2024-01-01", nights="1") for i in range(9)]
        rows += [_row(booking_id="L", arrival="2024-01-01", nights="4")]
        rows += [_row(booking_id="Z", arrival="2024-06-01", nights="1")]
        res = self._bookings(rows)
        first, _last = pilot.stay_window(res.bookings, dt.date(2024, 12, 31))
        # Ten rows reach the ledger, nine of one night and one of four, so the
        # 99th percentile of their lengths is 3.73 and the trim is 3 nights
        # off the first revenue arrival, 2024-01-01.
        self.assertEqual(first, dt.date(2024, 1, 4))
        self.assertEqual(pilot.trim_nights(res.bookings, first), 3)
        # The comp row is 30 days earlier, so a trim taken from the raw file
        # would say 33.
        self.assertEqual(min(b.arrival for b in res.bookings), dt.date(2023, 12, 2))

    def test_the_deep_mark_is_h1_only(self):
        # Fixed from the brief: the fixture hotel's max_lead is 40, so 90 and
        # 60 from the original [120, 90, 60, 30, 14, 7] were both too deep for
        # H2 regardless of the h1-only filter, and marks_for("H2", ...) raised
        # PilotError before the h1-only exclusion itself could be observed.
        # Dropping 90 and 60 isolates the behaviour this test names: 120 is
        # missing from H2's marks because it is h1-only, not because it is
        # too deep.
        settings = {"lead_marks": [120, 30, 14, 7], "lead_marks_h1_only": [120]}
        hotel = HC.apply(HC.load_hotel_json(FIXTURE_HOTEL))
        self.assertNotIn(120, pilot.marks_for("H2", settings, hotel))
        self.assertIn(30, pilot.marks_for("H2", settings, hotel))

    def test_the_h1_only_mark_survives_for_h1(self):
        # test_the_deep_mark_is_h1_only only checks that an h1-only mark is
        # absent for H2; nothing asserted that H1 itself still gets it.  A
        # filter with its boolean flipped (`and` in place of `or`) would
        # leave that test green while silently dropping the h1-only mark
        # from H1 too, which would drop table 1's deepest mark on the
        # flagship hotel without any test noticing.  40 is used here instead
        # of 120 because it sits exactly at the fixture hotel's max_lead (a
        # mark is refused only strictly past it), so it survives for H1
        # without tripping that guard, where 120 would.
        settings = {"lead_marks": [40, 30, 14, 7], "lead_marks_h1_only": [40]}
        hotel = HC.apply(HC.load_hotel_json(FIXTURE_HOTEL))
        self.assertIn(40, pilot.marks_for("H1", settings, hotel))
        self.assertNotIn(40, pilot.marks_for("H2", settings, hotel))

    def test_a_mark_deeper_than_max_lead_is_refused(self):
        settings = {"lead_marks": [120, 90], "lead_marks_h1_only": []}
        hotel = HC.apply(HC.load_hotel_json(FIXTURE_HOTEL))   # max_lead 40
        with self.assertRaises(pilot.PilotError):
            pilot.marks_for("H1", settings, hotel)


def _settled_row(d, rooms):
    return {"date": d, "rooms_sold": rooms, "revenue": 0.0, "walked": 0, "walk_cost": 0.0,
            "adr": 0.0, "occupancy": 0.0, "revpar": 0.0, "mix": {}, "denied_observable": 0}


def _seed(led, d, per_lead, final):
    for lead, otb in per_lead.items():
        led.snapshots[d][lead] = otb
    led.settled[d] = _settled_row(d, final)


def _bare_walk(ledger, records):
    """A `WalkResult`-alike carrying only what `score()` reads (`.ledger` and
    `.records`), so a scoring scenario can be hand-built precisely instead of
    driving a real `pilot.walk()`."""
    class _Walk:
        pass
    w = _Walk()
    w.ledger = ledger
    w.records = records
    return w


class Baselines(unittest.TestCase):
    """Hand-built snapshots with answers worked out by hand."""

    NIGHT = dt.date(2025, 6, 4)          # a Wednesday
    LEAD = 30

    def _ledger(self):
        hotel = HC.apply(HC.load_hotel_json(FIXTURE_HOTEL))
        # first_stay is set to the tenth trailing reference's own date (d-98),
        # not an arbitrary early date. Because STLY_BACK (364) is a multiple
        # of 7, the same-time-last-year night seeded below is always
        # weekday-aligned with NIGHT, so a looser first_stay (e.g. 2024-01-01)
        # would leave it reachable by reference_nights' backward search and it
        # would silently stand in for a deleted trailing week, which is
        # exactly the "thinner estimator" outcome
        # test_a_short_window_gives_none_rather_than_a_thinner_estimator
        # exists to rule out. Bounding the ledger here means removing one of
        # the ten trailing weeks leaves genuinely nothing earlier to search.
        led = Ledger(hotel, self.NIGHT - dt.timedelta(days=98), dt.date(2025, 12, 31))
        for k in range(5, 15):           # the ten Wednesdays from d-35 back to d-98
            n = self.NIGHT - dt.timedelta(days=7 * k)
            _seed(led, n, {self.LEAD: 10}, 16)
        stly_night = self.NIGHT - dt.timedelta(days=baselines.STLY_BACK)
        # Pin the design's actual reason for choosing 364 over 365: it is a
        # multiple of seven, so the same-time-last-year night always falls on
        # the same weekday as the night being forecast. Without this, changing
        # STLY_BACK to 365 would leave every test in this class green while
        # quietly breaking the alignment the design relies on.
        self.assertEqual(stly_night.weekday(), self.NIGHT.weekday(),
                         "STLY_BACK no longer lands on the same weekday as the forecast "
                         "night; that alignment is the whole reason 364 was chosen over 365")
        _seed(led, stly_night, {self.LEAD: 10}, 16)
        led.snapshots[self.NIGHT][self.LEAD] = 12
        led.settled[self.NIGHT] = _settled_row(self.NIGHT, 20)
        return led

    def tearDown(self):
        from pace import calendar as C
        from pace import config
        C.reset_seasonality()
        config.reset_segments()

    def test_reference_nights_are_ten_same_weekday_nights_already_finished(self):
        led = self._ledger()
        refs = baselines.reference_nights(led, self.NIGHT, self.LEAD)
        self.assertEqual(len(refs), 10)
        self.assertEqual(refs[0], self.NIGHT - dt.timedelta(days=35))
        self.assertEqual(refs[-1], self.NIGHT - dt.timedelta(days=98))
        self.assertTrue(all(n.weekday() == self.NIGHT.weekday() for n in refs))

    def test_a_night_that_had_not_finished_on_the_forecast_day_is_not_used(self):
        led = self._ledger()
        leaky = self.NIGHT - dt.timedelta(days=28)      # two days after asof
        _seed(led, leaky, {self.LEAD: 10}, 16)
        refs = baselines.reference_nights(led, self.NIGHT, self.LEAD)
        self.assertNotIn(leaky, refs)
        self.assertEqual(len(refs), 10)

    def test_a_night_exactly_at_the_forecast_day_is_not_used(self):
        """References step in whole weeks, so a candidate lands exactly on
        asof (= d - lead) only when lead is itself a multiple of seven; two
        of the pre-registered lead marks, 7 and 14, are. asof is the day the
        snapshot at this lead was frozen for the night being forecast, one
        day before that night's own settlement, so a reference sitting
        exactly on asof has not finished on the forecast day and must be
        excluded by a strict `n < asof`, not `n <= asof`.
        `test_a_night_that_had_not_finished_on_the_forecast_day_is_not_used`
        cannot catch that boundary: its leaky night sits two days after
        asof, never on it, because its lead (30) is not a multiple of seven.
        On the real H1 file, mutating the comparison to `<=` moves additive
        pickup on 414 of 427 scoring nights at lead 7 and 417 of 427 at lead
        14 (see the fix report for both outputs).
        """
        lead = 28    # a multiple of seven, so asof itself lands on a weekly step
        led = self._ledger()
        for k in range(5, 15):      # the ten same-weekday nights already finished by asof
            n = self.NIGHT - dt.timedelta(days=7 * k)
            _seed(led, n, {lead: 10}, 16)
        boundary = self.NIGHT - dt.timedelta(days=lead)     # exactly asof
        _seed(led, boundary, {lead: 10}, 16)
        refs = baselines.reference_nights(led, self.NIGHT, lead)
        self.assertNotIn(boundary, refs)
        self.assertEqual(len(refs), 10)

    def test_additive_pickup(self):
        led = self._ledger()
        self.assertAlmostEqual(baselines.additive_pickup(led, self.NIGHT, self.LEAD), 18.0)

    def test_multiplicative_pickup(self):
        led = self._ledger()
        self.assertAlmostEqual(baselines.multiplicative_pickup(led, self.NIGHT, self.LEAD), 19.2)

    def test_same_time_last_year_additive(self):
        led = self._ledger()
        self.assertAlmostEqual(baselines.stly_additive(led, self.NIGHT, self.LEAD), 18.0)

    def test_the_average_is_of_the_two_additive_forms(self):
        led = self._ledger()
        out = baselines.all_baselines(led, self.NIGHT, self.LEAD)
        self.assertAlmostEqual(out["average"], 18.0)
        self.assertEqual(sorted(out), sorted(baselines.ORDER))

    def test_a_short_window_gives_none_rather_than_a_thinner_estimator(self):
        led = self._ledger()
        del led.settled[self.NIGHT - dt.timedelta(days=98)]
        out = baselines.all_baselines(led, self.NIGHT, self.LEAD)
        self.assertIsNone(out["pickup_add"])
        self.assertIsNone(out["pickup_mult"])
        self.assertIsNone(out["average"])
        self.assertIsNotNone(out["stly_add"])

    def test_no_last_year_gives_none(self):
        led = self._ledger()
        del led.settled[self.NIGHT - dt.timedelta(days=baselines.STLY_BACK)]
        out = baselines.all_baselines(led, self.NIGHT, self.LEAD)
        self.assertIsNone(out["stly_add"])
        self.assertIsNone(out["average"])
        self.assertIsNotNone(out["pickup_add"])


class Scoring(unittest.TestCase):
    def tearDown(self):
        from pace import calendar as C
        from pace import config
        C.reset_seasonality()
        config.reset_segments()

    def test_cell_statistics_on_hand_numbers(self):
        # capacity 20 throughout; two forecasts above it, both clamped to 20.
        raw = [25.0, 18.0, 30.0, 12.0]
        clamped = [20.0, 18.0, 20.0, 12.0]
        act = [20.0, 16.0, 19.0, 14.0]
        cap = [20.0, 20.0, 20.0, 20.0]
        out = S.cell(raw, clamped, act, cap)
        self.assertEqual(out["n"], 4)
        self.assertAlmostEqual(out["mae"], (0 + 2 + 1 + 2) / 4.0)
        self.assertAlmostEqual(out["bias"], (0 + 2 + 1 - 2) / 4.0)
        self.assertAlmostEqual(out["clamp_share"], 0.5)
        self.assertAlmostEqual(out["mae_share"], 1.25 / 20.0)

    def test_an_empty_cell_says_none_rather_than_zero(self):
        out = S.cell([], [], [], [])
        self.assertEqual(out["n"], 0)
        self.assertIsNone(out["mae"])
        self.assertIsNone(out["clamp_share"])

    def _resolvable_baseline_ledger(self, hotel, d, lead, ref_final=16.0, ref_otb=10.0):
        """A ledger where every baseline resolves for night `d` at `lead`:
        ten trailing same-weekday references and a same-time-last-year night,
        seeded generically (their own values are never asserted on by the
        clamp tests, which only care that the night is scored at all)."""
        led = Ledger(hotel, d - dt.timedelta(days=400), d + dt.timedelta(days=30))
        for k in range(1, 18):
            n = d - dt.timedelta(weeks=k)
            _seed(led, n, {lead: ref_otb}, ref_final)
        stly = d - dt.timedelta(days=baselines.STLY_BACK)
        self.assertEqual(stly.weekday(), d.weekday())
        _seed(led, stly, {lead: ref_otb}, ref_final)
        return led

    def test_the_forecast_is_clamped_before_scoring_not_the_raw_value(self):
        """Review finding 1 (clamp deletion survives): a night whose raw
        engine forecast exceeds capacity must be scored against the clamped
        value, and must still be scored at all (not silently dropped)."""
        hotel = HC.apply(HC.load_hotel_json(FIXTURE_HOTEL))     # 20 rooms -> capacity 20
        lead = 30
        d = dt.date(2025, 3, 5)
        led = self._resolvable_baseline_ledger(hotel, d, lead)
        _seed(led, d, {lead: 12}, 15.0)                          # settled actual 15, under capacity
        walk_result = _bare_walk(led, {
            (d, lead): pilot.WalkRecord(stay_date=d, lead=lead,
                                        asof=d - dt.timedelta(days=lead),
                                        otb=12, forecast=30.0),   # raw forecast well over capacity
        })
        table = S.score(walk_result, hotel, [], [], [lead], d, d,
                        d - dt.timedelta(days=365), d - dt.timedelta(days=1))
        engine = table["overall"][str(lead)]["methods"]["engine"]
        # Clamped forecast is min(30, 20) = 20; error against actual 15 is 5.
        # Scoring the raw 30 instead would give an error of 15; silently
        # dropping the night (because its raw forecast exceeded capacity)
        # would leave n at 0 and mae at None. Both survive the full suite
        # today, per the review; this pins the one correct answer instead.
        self.assertEqual(engine["n"], 1)
        self.assertAlmostEqual(engine["mae"], 5.0)
        self.assertAlmostEqual(engine["bias"], 5.0)
        self.assertAlmostEqual(engine["clamp_share"], 1.0)

    def test_the_actual_is_never_clamped_even_when_it_exceeds_capacity(self):
        """Review finding 1 (clamping the actual too survives): the clamp
        applies to a forecast, never to what actually happened. Built with a
        settled actual above capacity -- artificial (a real settled ledger
        never exceeds `hotel.rooms`, since `Ledger.settle` walks the excess
        off first), but the point of this test is to isolate the accumulator
        rule in `score()`'s `_add`, not to model a real night."""
        hotel = HC.apply(HC.load_hotel_json(FIXTURE_HOTEL))     # 20 rooms -> capacity 20
        lead = 30
        d = dt.date(2025, 3, 5)
        led = self._resolvable_baseline_ledger(hotel, d, lead)
        _seed(led, d, {lead: 14}, 25.0)                          # settled actual 25, ABOVE capacity
        walk_result = _bare_walk(led, {
            (d, lead): pilot.WalkRecord(stay_date=d, lead=lead,
                                        asof=d - dt.timedelta(days=lead),
                                        otb=14, forecast=18.0),   # forecast itself is under capacity
        })
        table = S.score(walk_result, hotel, [], [], [lead], d, d,
                        d - dt.timedelta(days=365), d - dt.timedelta(days=1))
        engine = table["overall"][str(lead)]["methods"]["engine"]
        # Forecast 18 is not clamped (under capacity 20); error against the
        # true actual 25 is -7. Clamping the actual too would give
        # min(25, 20) = 20 and an error of -2 instead.
        self.assertEqual(engine["n"], 1)
        self.assertAlmostEqual(engine["mae"], 7.0)
        self.assertAlmostEqual(engine["bias"], -7.0)
        self.assertAlmostEqual(engine["clamp_share"], 0.0)

    def test_season_cut_is_four_four_four_by_warm_up_occupancy(self):
        _res, out = walked()
        cut = S.season_cut(out.ledger, FIRST_ARRIVAL, SCORE_FIRST - dt.timedelta(days=1))
        self.assertEqual(sorted(cut), list(range(1, 13)))
        counts = collections.Counter(cut.values())
        self.assertEqual((counts["high"], counts["shoulder"], counts["low"]), (4, 4, 4))

    def test_season_cut_ignores_settled_data_outside_the_warm_up_window(self):
        """Review finding 4 (the window guard's removal survives): a night
        outside `[first, last]` must never move a month's ranking. Twelve
        in-window nights give month `m` a mean of exactly `m`; two outliers
        sit outside the window and would flip June (mean 6, "shoulder") and
        January (mean 1, "low") to the top of the ranking if the window
        check were ever dropped in favour of an unconditional true."""
        hotel = HC.apply(HC.load_hotel_json(FIXTURE_HOTEL))
        led = Ledger(hotel, dt.date(2023, 1, 1), dt.date(2025, 12, 31))
        first, last = dt.date(2024, 1, 1), dt.date(2024, 12, 31)
        for m in range(1, 13):
            d = dt.date(2024, m, 15)
            led.settled[d] = _settled_row(d, float(m))
        led.settled[dt.date(2023, 6, 15)] = _settled_row(dt.date(2023, 6, 15), 999.0)
        led.settled[dt.date(2025, 1, 15)] = _settled_row(dt.date(2025, 1, 15), 999.0)

        cut = S.season_cut(led, first, last)
        self.assertEqual(cut[6], "shoulder")
        self.assertEqual(cut[1], "low")
        counts = collections.Counter(cut.values())
        self.assertEqual((counts["high"], counts["shoulder"], counts["low"]), (4, 4, 4))

    def test_a_night_is_filed_under_its_own_season_not_its_forecast_days(self):
        """Review finding 4 (filing by the forecast day survives): the cut
        must key off the stay night's own month, never the asof (forecast)
        day's month. March is given an outlying high warm-up mean and
        February an outlying low one; a night on 2025-03-15 forecast 30 days
        out (asof 2025-02-13) can only land under "high" if it is filed by
        its own month rather than the day the forecast was made."""
        hotel = HC.apply(HC.load_hotel_json(FIXTURE_HOTEL))
        lead = 30
        d = dt.date(2025, 3, 15)
        led = self._resolvable_baseline_ledger(hotel, d, lead)
        _seed(led, d, {lead: 12}, 15.0)

        warm_first, warm_last = dt.date(2020, 1, 1), dt.date(2020, 12, 31)
        for m in range(1, 13):
            wd = dt.date(2020, m, 15)
            if m == 3:
                rooms = 1000.0                # March: an outlying high mean
            elif m == 2:
                rooms = 1.0                   # February: an outlying low mean
            else:
                rooms = float(10 + m)         # filler, strictly between the two
            led.settled[wd] = _settled_row(wd, rooms)

        walk_result = _bare_walk(led, {
            (d, lead): pilot.WalkRecord(stay_date=d, lead=lead,
                                        asof=d - dt.timedelta(days=lead),
                                        otb=12, forecast=15.0),
        })
        table = S.score(walk_result, hotel, [], [], [lead], d, d, warm_first, warm_last)

        self.assertEqual(table["season_of_month"]["3"], "high")
        self.assertEqual(table["season_of_month"]["2"], "low")
        self.assertIn("high", table["cuts"]["season"])
        self.assertNotIn("low", table["cuts"]["season"])
        self.assertEqual(table["cuts"]["season"]["high"][str(lead)]["n"], 1)

    def test_event_windows(self):
        self.assertEqual(S.event_of(dt.date(2017, 4, 14)), "Easter")
        self.assertEqual(S.event_of(dt.date(2016, 12, 31)), "Christmas and New Year")
        self.assertEqual(S.event_of(dt.date(2017, 5, 14)), "none")

    def test_a_night_one_method_cannot_forecast_is_dropped_from_every_method(self):
        """The design's rule: every table compares only nights on which every
        method produced a forecast, and prints that count."""
        res, out = walked()
        table = S.score(out, res.hotel, res.bookings, res.nonrev, TEST_MARKS,
                        SCORE_FIRST, SCORE_LAST, FIRST_ARRIVAL,
                        SCORE_FIRST - dt.timedelta(days=1))
        for lead in table["leads"] + [table["late_lead"]]:
            ns = set(c["n"] for c in table["overall"][lead]["methods"].values())
            self.assertEqual(len(ns), 1, "lead %s scored different methods on different nights" % lead)
            self.assertEqual(table["overall"][lead]["n"], ns.pop())

    def test_a_night_one_baseline_cannot_forecast_is_excluded_not_zero_filled(self):
        """Review finding 2: `cell()` takes its count from the actual list,
        the same list every method reads, so "every method agrees on n" is
        true no matter what gets put in that list -- it cannot fail even if
        a night with no real forecast is kept and a missing value is
        substituted with zero. This test instead checks the population
        against a hand-derived ground truth: two nights with every baseline
        resolving (n should be 2), plus a third whose same-time-last-year
        reference is deliberately left unseeded so `stly_add` (and therefore
        `average`) is `None` for it, with an engine forecast so large that
        including it under any substitution would be unmistakable in the
        aggregate.
        """
        hotel = HC.apply(HC.load_hotel_json(FIXTURE_HOTEL))     # 20 rooms -> capacity 20
        lead = 30
        good1 = dt.date(2025, 3, 5)
        good2 = good1 + dt.timedelta(days=7)
        bad = good2 + dt.timedelta(days=7)
        led = Ledger(hotel, good1 - dt.timedelta(days=400), bad + dt.timedelta(days=30))
        for k in range(1, 26):
            n = bad - dt.timedelta(weeks=k)
            _seed(led, n, {lead: 10.0}, 16.0)
        for night in (good1, good2):                # bad's own stly night is left unseeded on purpose
            stly = night - dt.timedelta(days=baselines.STLY_BACK)
            self.assertEqual(stly.weekday(), night.weekday())
            _seed(led, stly, {lead: 10.0}, 16.0)
        _seed(led, good1, {lead: 15}, 15.0)
        _seed(led, good2, {lead: 16}, 14.0)
        _seed(led, bad, {lead: 11}, 10.0)            # otb/actual exist; only stly_add is missing

        walk_result = _bare_walk(led, {
            (good1, lead): pilot.WalkRecord(stay_date=good1, lead=lead,
                                            asof=good1 - dt.timedelta(days=lead),
                                            otb=15, forecast=18.0),
            (good2, lead): pilot.WalkRecord(stay_date=good2, lead=lead,
                                            asof=good2 - dt.timedelta(days=lead),
                                            otb=16, forecast=12.0),
            (bad, lead): pilot.WalkRecord(stay_date=bad, lead=lead,
                                          asof=bad - dt.timedelta(days=lead),
                                          otb=11, forecast=100.0),
        })
        # Confirm the scenario is what it claims to be before trusting the
        # assertions below: `bad` really is missing exactly the baselines
        # that depend on the same-time-last-year night, and nothing else.
        bl = baselines.all_baselines(led, bad, lead)
        self.assertIsNone(bl["stly_add"])
        self.assertIsNone(bl["average"])
        self.assertIsNotNone(bl["pickup_add"])
        self.assertIsNotNone(bl["pickup_mult"])

        table = S.score(walk_result, hotel, [], [], [lead], good1, bad,
                        good1 - dt.timedelta(days=365), good1 - dt.timedelta(days=1))
        engine = table["overall"][str(lead)]["methods"]["engine"]
        # Correct: only good1 (engine error 18-15=3) and good2 (12-14=-2)
        # are scored. mae = mean(3, 2) = 2.5, bias = mean(3, -2) = 0.5. If
        # `bad` leaked in with its forecast merely clamped (min(100, 20) =
        # 20, error 10), the population would be 3 and the mean would jump
        # to (3 + 2 + 10) / 3 = 5.0 -- unmistakably different either way.
        self.assertEqual(table["overall"][str(lead)]["n"], 2)
        self.assertEqual(engine["n"], 2)
        self.assertAlmostEqual(engine["mae"], 2.5)
        self.assertAlmostEqual(engine["bias"], 0.5)

    def test_the_full_night_cut_counts_a_night_full_only_thanks_to_a_comp_room(self):
        """Fixed from the brief: as written this test called `walked()`, whose
        demand model caps each night's target at `min(19, base + noise)` with
        base at most 15 (weekday and month bonuses), and the scoring window
        (2025-02-01 to 2025-04-30) is neither Friday/Saturday-heavy nor July or
        August, so the fixture's own physical occupancy inside the scored
        window tops out at 8 rooms against a 20-room house (checked directly:
        `ingest.physical_occupancy(res.bookings)` over that window peaks at 8,
        the sellout cut is 20*0.97=19.4). The one deliberate overbooked night
        the fixture does carry sits at 2024-08-08, entirely outside both the
        warm-up and scoring windows. So `walked()` cannot produce a single
        night classified "full" in this table at any lead, and the original
        test could only ever fail on `assertIn("full", ...)` for a reason that
        has nothing to do with whether the cut reads physical occupancy or the
        settled figure -- the situation named in the test's own name never
        occurs in that fixture.

        Replaced with a hand-built ledger and booking list, the same technique
        the `Baselines` class already uses for its own hand-worked numbers,
        scoped to exactly the mechanism this test is named for: two nights,
        one made full only by a comp room (19 revenue-bearing stays plus one
        COMP stay, physical occupancy 20 against capacity 20, while the
        ledger's own settled `rooms_sold` -- the figure the table scores
        against -- is seeded at 19, under the cut on its own), the other left
        with no bookings at all so its physical occupancy is 0 and it sorts
        into not_full. Both nights carry the ten trailing same-weekday
        references and the same-time-last-year reference every baseline
        needs, seeded the same way `Baselines._ledger()` seeds them, or
        `score()`'s own rule (every method must produce a forecast) would
        drop the night from every cut before the full/not_full split is ever
        reached.
        """
        hotel = HC.apply(HC.load_hotel_json(FIXTURE_HOTEL))    # 20 rooms, sellout 0.97 -> full at 19.4
        lead = 30
        full_night = dt.date(2025, 3, 5)          # a Wednesday inside the scoring window
        plain_night = full_night + dt.timedelta(days=7)
        led = Ledger(hotel, dt.date(2023, 1, 1), dt.date(2025, 12, 31))
        for k in range(1, 17):                    # covers both nights' ten trailing weeks with room
            n = full_night - dt.timedelta(weeks=k)
            _seed(led, n, {lead: 10}, 16)
        for night in (full_night, plain_night):
            stly = night - dt.timedelta(days=baselines.STLY_BACK)
            self.assertEqual(stly.weekday(), night.weekday())
            _seed(led, stly, {lead: 10}, 16)
        _seed(led, full_night, {lead: 12}, 19)    # settled 19: under the 20-room cut on its own
        _seed(led, plain_night, {lead: 8}, 12)

        def _stay(bid, arrival, segment, target):
            return ingest.Booking(booking_id=bid, booked_on=arrival - dt.timedelta(days=60),
                                  arrival=arrival, nights=1, rooms=1, rate=100.0, currency="EUR",
                                  segment=segment, rate_code="", source="", room_type="STD",
                                  company="", status="stayed", status_date=None, updated_on=None,
                                  row=0, target=target)

        bookings = [_stay("R%d" % i, full_night, "WEB", "RETAIL") for i in range(19)]
        comp = _stay("C1", full_night, "COMP", "NONREV")
        bookings.append(comp)                     # 19 + 1 comp = 20 physical, only "full" with it
        nonrev = [comp]

        class _Walk:
            pass
        walk_result = _Walk()
        walk_result.ledger = led
        walk_result.records = {
            (full_night, lead): pilot.WalkRecord(stay_date=full_night, lead=lead,
                                                 asof=full_night - dt.timedelta(days=lead),
                                                 otb=12, forecast=18.0),
            (plain_night, lead): pilot.WalkRecord(stay_date=plain_night, lead=lead,
                                                  asof=plain_night - dt.timedelta(days=lead),
                                                  otb=8, forecast=11.0),
        }

        table = S.score(walk_result, hotel, bookings, nonrev, [lead],
                        full_night, plain_night,
                        full_night - dt.timedelta(days=365), full_night - dt.timedelta(days=1))
        self.assertIn("full", table["cuts"]["full"])
        self.assertIn("not_full", table["cuts"]["full"])
        self.assertEqual(table["cuts"]["full"]["full"][str(lead)]["n"], 1)
        self.assertEqual(table["cuts"]["full"]["not_full"][str(lead)]["n"], 1)

    def test_the_headline_names_a_single_baseline_when_one_beats_the_average(self):
        res, out = walked()
        table = S.score(out, res.hotel, res.bookings, res.nonrev, TEST_MARKS,
                        SCORE_FIRST, SCORE_LAST, FIRST_ARRIVAL,
                        SCORE_FIRST - dt.timedelta(days=1))
        for lead, head in table["headline"].items():
            if head["best_single"] is not None:
                self.assertLess(head["best_single_mae"], head["average_mae"])

    def test_the_headline_names_the_best_single_baseline_not_the_worst(self):
        """Review finding 3: the existing headline test only checks the
        positive case (when a name is given, it does beat the average), so
        naming the worst baseline instead of the best, or naming one that
        loses to the average, both survive undetected. Built so the three
        singles rank distinctly -- pickup_mult 0.2 off, pickup_add 1 off,
        stly_add 3 off -- and the best of them (pickup_mult) beats the
        average (1 off), so both a min/max flip and a missing beats-check
        would be caught: a max flip would name stly_add instead."""
        hotel = HC.apply(HC.load_hotel_json(FIXTURE_HOTEL))     # 20 rooms -> capacity 20
        lead = 30
        d = dt.date(2025, 3, 5)
        led = Ledger(hotel, d - dt.timedelta(days=400), d + dt.timedelta(days=30))
        for k in range(1, 18):
            n = d - dt.timedelta(weeks=k)
            _seed(led, n, {lead: 10.0}, 16.0)                    # otb 10, final 16 for every ref
        stly = d - dt.timedelta(days=baselines.STLY_BACK)
        self.assertEqual(stly.weekday(), d.weekday())
        _seed(led, stly, {lead: 10.0}, 20.0)                     # last year: otb 10, final 20
        _seed(led, d, {lead: 12}, 19.0)                          # this year: otb 12, actual 19

        # Hand-worked: pickup_add = 12 + (16-10) = 18 (err 1); pickup_mult =
        # 12 * 16/10 = 19.2 (err 0.2); stly_add = 20 + (12-10) = 22 (err 3);
        # average = mean(18, 22) = 20 (err 1). Confirm before trusting the
        # headline assertions below.
        bl = baselines.all_baselines(led, d, lead)
        self.assertAlmostEqual(bl["pickup_add"], 18.0)
        self.assertAlmostEqual(bl["pickup_mult"], 19.2)
        self.assertAlmostEqual(bl["stly_add"], 22.0)
        self.assertAlmostEqual(bl["average"], 20.0)

        walk_result = _bare_walk(led, {
            (d, lead): pilot.WalkRecord(stay_date=d, lead=lead,
                                        asof=d - dt.timedelta(days=lead),
                                        otb=12, forecast=25.0),  # clamped to 20, err 1
        })
        table = S.score(walk_result, hotel, [], [], [lead], d, d,
                        d - dt.timedelta(days=365), d - dt.timedelta(days=1))
        head = table["headline"][str(lead)]
        self.assertEqual(head["best_single"], "pickup_mult")
        self.assertAlmostEqual(head["best_single_mae"], 0.2)

    def test_the_headline_says_none_when_every_single_baseline_loses_to_the_average(self):
        """Review finding 3, the negative case the existing test never
        exercises: when every single baseline's own error is worse than the
        average's, `best_single` must be `None`, not the least-bad loser.
        Built so the average lands exactly on the actual (err 0) while every
        single is off by 5 or more."""
        hotel = HC.apply(HC.load_hotel_json(FIXTURE_HOTEL))     # 20 rooms -> capacity 20
        lead = 30
        d = dt.date(2025, 3, 5)
        led = Ledger(hotel, d - dt.timedelta(days=400), d + dt.timedelta(days=30))
        for k in range(1, 18):
            n = d - dt.timedelta(weeks=k)
            _seed(led, n, {lead: 10.0}, 14.0)                    # otb 10, final 14 for every ref
        stly = d - dt.timedelta(days=baselines.STLY_BACK)
        self.assertEqual(stly.weekday(), d.weekday())
        _seed(led, stly, {lead: 10.0}, 24.0)                     # last year: otb 10, final 24
        _seed(led, d, {lead: 6}, 15.0)                           # this year: otb 6, actual 15

        # Hand-worked: pickup_add = 6 + (14-10) = 10 (err 5); pickup_mult =
        # 6 * 14/10 = 8.4 (err 6.6); stly_add = 24 + (6-10) = 20 (err 5);
        # average = mean(10, 20) = 15 = the actual exactly (err 0).
        bl = baselines.all_baselines(led, d, lead)
        self.assertAlmostEqual(bl["pickup_add"], 10.0)
        self.assertAlmostEqual(bl["pickup_mult"], 8.4)
        self.assertAlmostEqual(bl["stly_add"], 20.0)
        self.assertAlmostEqual(bl["average"], 15.0)

        walk_result = _bare_walk(led, {
            (d, lead): pilot.WalkRecord(stay_date=d, lead=lead,
                                        asof=d - dt.timedelta(days=lead),
                                        otb=6, forecast=17.0),
        })
        table = S.score(walk_result, hotel, [], [], [lead], d, d,
                        d - dt.timedelta(days=365), d - dt.timedelta(days=1))
        head = table["headline"][str(lead)]
        self.assertIsNone(head["best_single"])
        self.assertIsNone(head["best_single_mae"])

    def test_lead_1_is_the_late_lead_not_folded_into_the_ordinary_leads(self):
        """Review finding 5: folding lead 1 into the ordinary leads (so it
        would print as a demand lead rather than the late-cancellation line
        the design's own rule requires) survives the whole suite today."""
        res, out = walked()
        table = S.score(out, res.hotel, res.bookings, res.nonrev, TEST_MARKS,
                        SCORE_FIRST, SCORE_LAST, FIRST_ARRIVAL,
                        SCORE_FIRST - dt.timedelta(days=1))
        self.assertEqual(table["late_lead"], "1")
        self.assertEqual(table["leads"], [str(m) for m in TEST_MARKS])
        self.assertNotIn(table["late_lead"], table["leads"])

    def test_every_note_is_carried(self):
        res, out = walked()
        table = S.score(out, res.hotel, res.bookings, res.nonrev, TEST_MARKS,
                        SCORE_FIRST, SCORE_LAST, FIRST_ARRIVAL,
                        SCORE_FIRST - dt.timedelta(days=1))
        text = " ".join(table["notes"])
        self.assertIn("not evidence of revenue", text)
        self.assertIn("cannot forecast a decline", text)
        # Review "two smaller things": table 1's own no-revenue sentence was
        # not asserted on by anything, so emptying TABLE1_NOTE entirely kept
        # the suite green. This phrase is unique to it (BASELINE_NOTE and
        # LEAD1_NOTE do not contain it).
        self.assertIn("nothing in this table prices a night", text)

    def test_the_lead_1_note_counts_the_nights_that_finished_below_their_books(self):
        """The note's number is computed from the walk, never typed in, so it
        cannot go stale; this test counts the same nights again from the
        walk's own lead-1 records and the ledger's settled rows, and pins the
        fixture's count as a literal beside that, the way RunOne pins its
        record counts.  On this history 20 of the 89 scoring nights settle
        below their lead-1 on the books and the other 69 settle exactly on it,
        which is what a count that took `at or below` would get wrong.

        H1's own figure is 41 of 427, 9.6 percent, measured on the real walk
        and quoted in the write-up; the fixture cannot reproduce it, so the
        template is formatted with it here to pin the sentence the write-up
        quotes to the arithmetic that produces it."""
        res, out = walked()
        table = S.score(out, res.hotel, res.bookings, res.nonrev, TEST_MARKS,
                        SCORE_FIRST, SCORE_LAST, FIRST_ARRIVAL,
                        SCORE_FIRST - dt.timedelta(days=1))
        below = nights = on_the_books = 0
        d = SCORE_FIRST
        while d <= SCORE_LAST:
            rec = out.records.get((d, 1))
            row = out.ledger.settled.get(d)
            if rec is not None and rec.otb is not None and row is not None:
                nights += 1
                below += 1 if row["rooms_sold"] < rec.otb else 0
                on_the_books += 1 if row["rooms_sold"] == rec.otb else 0
            d += dt.timedelta(days=1)
        self.assertEqual((nights, below, on_the_books), (89, 20, 69))
        self.assertEqual(table["below_lead1_books"], {"nights": 89, "below": 20, "share": 20 / 89.0})
        self.assertEqual(table["below_lead1_books"]["below"], below)
        self.assertEqual(table["below_lead1_books"]["nights"], nights)
        note = [n for n in table["notes"] if "cannot forecast a decline" in n]
        self.assertEqual(len(note), 1)
        self.assertIn("on this run 22.5 percent of scoring nights (20 of 89) finish below "
                      "their lead-1 on the books", note[0])
        self.assertIn("%.1f percent of scoring nights (%d of %d)" % (100.0 * below / nights,
                                                                      below, nights), note[0])
        self.assertIn("9.6 percent of scoring nights (41 of 427)",
                      S.LEAD1_NOTE % (100.0 * 41 / 427, 41, 427))
        self.assertNotIn("7.5 percent", S.LEAD1_NOTE)


def _payload_rows():
    """The whole-run fixture: the shared synthetic history, plus two rows the
    ingest is bound to flag.

    The two zero-rate rows are the payload's proof of which report its ingest
    block was filled from.  `pilot.walk` never looks at a rate, so only
    `ingest.read_bookings` can have counted them; a block filled from the
    walk's own report comes back without them however loudly it is labelled
    `ingest`.
    """
    rows = history_rows(FIRST_ARRIVAL, NIGHTS)
    rows += [_row(booking_id="Z1", booked_on="2024-03-01", arrival="2024-03-10",
                  nights="1", rate="0"),
             _row(booking_id="Z2", booked_on="2024-03-02", arrival="2024-03-11",
                  nights="1", rate="0")]
    return rows


def _as_dicts(rows):
    return [dict(zip(HEADER, r)) for r in rows]


def _rows_that_reach_the_ledger(dicts):
    """The rows the replay lets in, read off the CSV rather than off any
    ledger: the fixture's segment map sends COMP to NONREV and nothing else,
    and a zero-night row is day use."""
    return [r for r in dicts if r["segment"] != "COMP" and int(r["nights"]) > 0]


def _physically_full_nights(dicts, rooms, threshold, first, last):
    """Nights occupied at or above the sell-out cut, counted from the rows.

    A cancelled row and a no-show never sleep in the house; a comped room
    does.  That is `ingest.physical_occupancy`'s rule, applied here to the CSV
    so the payload's count has something to be wrong against.
    """
    occ = collections.Counter()
    for r in dicts:
        if r["status"] not in ("stayed", "in_house"):
            continue
        arrival = dt.date.fromisoformat(r["arrival"])
        for k in range(int(r["nights"])):
            occ[arrival + dt.timedelta(days=k)] += 1
    return sorted(d for d, c in occ.items()
                  if c >= rooms * threshold and first <= d <= last)


def _priced_room_nights(dicts, first, last):
    """Room nights the priced segments occupied inside a window, by room type,
    counted off the CSV.

    The fixture's segment map sends WEB to RETAIL and BOOKING to OTA, and
    those two are the priced segments; CORP is contracted and COMP is
    non-revenue, so neither belongs in a comparison of public rates.
    """
    counts = collections.Counter()
    for r in dicts:
        if r["segment"] not in ("WEB", "BOOKING") or r["status"] not in ("stayed", "in_house"):
            continue
        arrival = dt.date.fromisoformat(r["arrival"])
        for k in range(int(r["nights"])):
            night = arrival + dt.timedelta(days=k)
            if first <= night <= last:
                counts[r["room_type"]] += 1
    return dict(counts)


def _nights_with_a_basket(dicts, first, last, lo, hi, room_type):
    """Nights in the window with at least one priced row of `room_type` booked
    between `lo` and `hi` days before that night, counted off the CSV.

    The trim is left out on purpose: it drops at most one row from each end of
    a basket of three or more and never empties one, so it cannot change this
    count, and repeating it here would only repeat whatever the code under
    test does with it.
    """
    nights = set()
    for r in dicts:
        if r["segment"] not in ("WEB", "BOOKING") or r["status"] not in ("stayed", "in_house"):
            continue
        if r["room_type"] != room_type or float(r["rate"]) <= 0:
            continue
        arrival = dt.date.fromisoformat(r["arrival"])
        booked = dt.date.fromisoformat(r["booked_on"])
        for k in range(int(r["nights"])):
            night = arrival + dt.timedelta(days=k)
            if first <= night <= last and hi <= (night - booked).days <= lo:
                nights.add(night)
    return len(nights)


def _fixture_settings(path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"seed": 1, "lead_marks": [30, 14, 7], "lead_marks_h1_only": []}, fh)
    return path


class OneHotelPerProcess(unittest.TestCase):
    """hotelconfig.apply rewrites module-level state, so two hotels in one
    process means the second one's engine answers for the first one's."""

    def tearDown(self):
        from pace import calendar as C
        from pace import config
        C.reset_seasonality()
        config.reset_segments()
        pilot._release_for_tests()

    def _hotel_named(self, dirname, name="hotel.json"):
        """A copy of the fixture hotel at a path of its own."""
        d = tempfile.mkdtemp(prefix=dirname)
        path = os.path.join(d, name)
        with open(FIXTURE_HOTEL, encoding="utf-8") as fh:
            raw = fh.read()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(raw)
        return path

    def test_a_second_hotel_in_the_same_process_is_refused_in_a_sentence(self):
        pilot.claim_process("h1-hotel.json")
        pilot.claim_process("h1-hotel.json")          # the same one is fine
        with self.assertRaises(pilot.PilotError) as ctx:
            pilot.claim_process("h2-hotel.json")
        self.assertIn("process of its own", str(ctx.exception))

    def test_two_different_hotels_both_called_hotel_json_are_refused(self):
        """The guard is keyed on which file the hotel is, not on what it is
        called.  `hotel.json` beside a booking log is the shape CLAUDE.md
        documents for the ingest, so two hotels with that name is the normal
        case and not a corner of one: keyed on the basename, H2 would be
        waved through and would rewrite H1's contract ratios in place, which
        is the exact harm the guard exists to stop.
        """
        first = self._hotel_named("h1-")
        second = self._hotel_named("h2-")
        self.assertEqual(os.path.basename(first), os.path.basename(second))
        pilot.claim_process(first)
        with self.assertRaises(pilot.PilotError) as ctx:
            pilot.claim_process(second)
        self.assertIn("process of its own", str(ctx.exception))
        # The sentence names both files, or a hotel cannot tell which two.
        self.assertIn(os.path.realpath(first), str(ctx.exception))
        self.assertIn(os.path.realpath(second), str(ctx.exception))

    def test_the_same_hotel_reached_by_two_different_paths_is_allowed(self):
        """The other direction of the same rule: one hotel is one hotel
        however the path to it was spelled, so a guard that refused it would
        refuse a legitimate run for the sake of a string comparison."""
        path = self._hotel_named("h1-")
        detour = os.path.join(os.path.dirname(path), "sub", "..", os.path.basename(path))
        os.makedirs(os.path.join(os.path.dirname(path), "sub"), exist_ok=True)
        self.assertNotEqual(path, detour)
        pilot.claim_process(path)
        pilot.claim_process(detour)                   # no raise: the same file
        self.assertEqual(os.path.realpath(detour), os.path.realpath(path))

    def test_run_one_refuses_a_second_hotel_before_it_reads_the_log(self):
        """The headline of this task is that `run.py pilot` refuses the second
        hotel, not that a function nobody calls would refuse it.  Nothing else
        in the suite would notice `claim_process` being dropped out of
        `run_one` altogether.
        """
        already = self._hotel_named("h1-")
        second = self._hotel_named("h2-")
        pilot.claim_process(already)
        out_dir = tempfile.mkdtemp()
        rows = [_row(booking_id="A%d" % i,
                     arrival=(dt.date(2024, 1, 1) + dt.timedelta(days=i)).isoformat())
                for i in range(10)]
        with self.assertRaises(pilot.PilotError) as ctx:
            pilot.run_one(_csv(rows), second, _fixture_settings(
                os.path.join(out_dir, "settings.json")), out_dir,
                score_first=dt.date(2024, 1, 20), score_last=dt.date(2024, 1, 25),
                warmup_end=dt.date(2024, 1, 19))
        self.assertIn("process of its own", str(ctx.exception))
        self.assertIn(os.path.realpath(already), str(ctx.exception))
        # Nothing was written for the hotel that was refused.
        self.assertEqual([f for f in os.listdir(out_dir) if f.startswith("pilot-")], [])

    def test_run_one_claims_the_hotel_it_was_given_and_not_a_fixed_name(self):
        """The test above pre-claims the process by hand, so it still passes
        with run_one handing `claim_process` a constant: the held key and the
        constant differ, the second hotel is refused, and the guard looks
        fine while two real runs would both claim that one constant and both
        go through.  Only two run_one calls can tell the difference.
        """
        first = self._hotel_named("first-")
        second = self._hotel_named("second-")
        out_dir = tempfile.mkdtemp()
        settings = _fixture_settings(os.path.join(out_dir, "settings.json"))
        rows = [_row(booking_id="A%d" % i,
                     arrival=(dt.date(2024, 1, 1) + dt.timedelta(days=i)).isoformat())
                for i in range(10)]
        csv_path = _csv(rows)
        kw = dict(score_first=dt.date(2024, 1, 20), score_last=dt.date(2024, 1, 25),
                  warmup_end=dt.date(2024, 1, 19))
        # This run is allowed to fail on its data. What is being pinned is
        # that it got past the claim, which sits after the cheap checks and
        # before ingest.load, and claimed the file it was actually handed.
        try:
            pilot.run_one(csv_path, first, settings, out_dir, **kw)
        except Exception:
            pass
        self.assertEqual(pilot._CLAIMED["hotel"], os.path.realpath(first))
        with self.assertRaises(pilot.PilotError) as ctx:
            pilot.run_one(csv_path, second, settings, out_dir, **kw)
        self.assertIn("process of its own", str(ctx.exception))
        self.assertIn(os.path.realpath(first), str(ctx.exception))

    def test_a_hotel_json_that_cannot_be_read_does_not_claim_the_process(self):
        """A typo in the path is the ordinary way this happens.  Claiming the
        process for a hotel that never loaded turns one typo into a process
        that refuses the hotel it was meant to run, and says something false
        while it does it.  load_hotel_json touches no global, so validating
        before claiming costs nothing.
        """
        out_dir = tempfile.mkdtemp()
        rows = [_row(booking_id="A1")]
        with self.assertRaises(HC.ConfigError):
            pilot.run_one(_csv(rows), os.path.join(out_dir, "h1-hotel.jsn"),
                          _fixture_settings(os.path.join(out_dir, "settings.json")), out_dir)
        # The real file is still free to claim the process.
        pilot.claim_process(FIXTURE_HOTEL)


class PreRegistration(unittest.TestCase):
    """The pre-registered file is checkable, and so is the code's agreement
    with it.  A digest that proves the file has not moved proves nothing at
    all if the constants the run uses were never tied to what it says."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "data", "antonio", "settings.json"), encoding="utf-8") as fh:
            cls.settings = json.load(fh)

    def test_the_pre_registered_settings_file_has_not_moved(self):
        """The whole point of pre-registration is that it is checkable."""
        path = os.path.join(ROOT, "data", "antonio", "settings.json")
        self.assertEqual(pilot.settings_digest(path), pilot.PREREG_SHA256)

    def test_the_scoring_window_is_the_pre_registered_one(self):
        self.assertEqual([pilot.SCORE_FIRST.isoformat(), pilot.SCORE_LAST.isoformat()],
                         self.settings["scoring_window"])

    def test_the_warm_up_ends_where_the_pre_registered_window_ends(self):
        self.assertEqual(pilot.WARMUP_END.isoformat(), self.settings["warmup_window"][1])

    def test_the_scoring_window_opens_the_day_after_the_warm_up_closes(self):
        """A gap between them would leave nights in neither; an overlap would
        fit the engine on nights it is then scored over."""
        self.assertEqual(pilot.WARMUP_END + dt.timedelta(days=1), pilot.SCORE_FIRST)
        self.assertEqual(pilot.WARMUP_END + dt.timedelta(days=1),
                         dt.date.fromisoformat(self.settings["scoring_window"][0]))

    def test_the_lead_marks_live_in_the_settings_file_and_nowhere_else(self):
        """A module-level copy of the pre-registered marks that nothing reads
        is a second answer to the question `which marks were pre-registered`,
        free to disagree with the first.  marks_for reads the file, so the
        module keeps no copy at all.
        """
        self.assertFalse(hasattr(pilot, "MARKS"))
        hotel = HC.apply(HC.load_hotel_json(FIXTURE_HOTEL))
        try:
            shallow = {"lead_marks": [m for m in self.settings["lead_marks"]
                                      if m <= hotel.max_lead],
                       "lead_marks_h1_only": self.settings["lead_marks_h1_only"]}
            self.assertEqual(list(pilot.marks_for("H1", shallow, hotel)),
                             sorted(shallow["lead_marks"], reverse=True))
        finally:
            from pace import calendar as C
            from pace import config
            C.reset_seasonality()
            config.reset_segments()

    def test_the_rate_windows_are_the_pre_registered_ones(self):
        """`pace/ratecheck.py` keeps its own copy of the comparison windows,
        because pace/ must not read data/.  A second copy is only safe while
        it cannot drift from the first, so every bound is checked against the
        file it was pre-registered in, not merely against itself.
        """
        self.assertEqual({k: list(v) for k, v in ratecheck.RATE_WINDOWS.items()},
                         self.settings["rate_windows"])
        # Spelled out as well, so a settings file edited to agree with a
        # mutated constant still fails here: both sides would have to be
        # changed, and the digest above says the file has not been.
        self.assertEqual(ratecheck.RATE_WINDOWS,
                         {"60": (75, 45), "30": (40, 21), "14": (21, 7), "7": (10, 4)})
        for mark, (lo, hi) in ratecheck.RATE_WINDOWS.items():
            self.assertLess(hi, int(mark), "window %s opens after its own mark" % mark)
            self.assertLess(int(mark), lo, "window %s closes before its own mark" % mark)

    def test_the_cheap_first_mark_is_the_pre_registered_one(self):
        """`pace/holdout.py` keeps its own copy of the mark the cheap channels
        close at, for the reason the rate windows are copied: pace/ must not
        read data/.  Checked against the file and spelled out as well, so a
        settings file edited to agree with a mutated constant still fails."""
        self.assertEqual(holdout.MARK, self.settings["close_cheap_first_mark"])
        self.assertEqual(holdout.MARK, 0.90)

    def test_the_holdout_grid_is_the_pre_registered_one(self):
        """The clean thresholds and the caps of table 3, copied into
        pace/holdout.py for the same reason and checked the same way."""
        self.assertEqual(list(holdout.THRESHOLDS), self.settings["holdout_clean_thresholds"])
        self.assertEqual(list(holdout.CAPS), self.settings["holdout_caps"])
        self.assertEqual(holdout.THRESHOLDS, (0.80, 0.85, 0.90))
        self.assertEqual(holdout.CAPS, (0.60, 0.70, 0.80))

    def test_the_basket_trim_is_the_pre_registered_one(self):
        self.assertEqual(list(ratecheck.TRIM), self.settings["basket_trim_percentiles"])
        self.assertEqual(ratecheck.TRIM, (0.01, 0.99))

    def test_every_pre_registered_rate_window_has_a_lead_mark_to_read(self):
        """A window with no mark behind it is a column of the table nothing
        can fill: the published rate is read out of the walk at that lead."""
        self.assertEqual(sorted(int(m) for m in ratecheck.RATE_WINDOWS),
                         sorted(m for m in self.settings["lead_marks"]
                                if m in (60, 30, 14, 7)))
        for mark in ratecheck.RATE_WINDOWS:
            self.assertIn(int(mark), self.settings["lead_marks"])

    def test_the_driven_run_up_covers_the_deepest_pre_registered_mark(self):
        """DRIVE_LEAD is the rate path in front of the scoring window.  A mark
        deeper than it would be read at a rate the engine never walked to."""
        self.assertGreaterEqual(pilot.DRIVE_LEAD, max(self.settings["lead_marks"]))
        self.assertEqual(pilot.DRIVE_LEAD, 180)


class RunOne(unittest.TestCase):
    """One whole run, with every block of the payload checked against a value
    worked out ahead of it.

    Every expected number below comes from the CSV the fixture generated, from
    tests/fixtures/pilot-hotel.json, or from arithmetic on the window that was
    asked for.  None of them is read back out of the payload: a payload
    checked against itself passes with `code` mutated to WRONG, because both
    sides of the comparison move together.
    """

    SCORING_NIGHTS = 89          # 2025-02-01 to 2025-04-30 inclusive: 28 + 31 + 30
    DRIVEN_DAYS = 269            # 2024-08-05 to 2025-04-30 inclusive
    QUICK_NIGHTS = 21

    @classmethod
    def setUpClass(cls):
        # Same reason walked() does it: plugins.py keeps a module-level
        # registry that pipeline.run() fills and never clears, so this run's
        # numbers must not depend on whether test_golden.py ran first.
        from pace import plugins as _plugins
        _plugins.reset()
        cls.rows = _payload_rows()
        cls.dicts = _as_dicts(cls.rows)
        cls.csv_path = _csv(cls.rows)
        cls.out_dir = tempfile.mkdtemp()
        cls.settings_path = _fixture_settings(os.path.join(cls.out_dir, "settings.json"))
        # Run with a progress interval and its stdout kept, so the lines a
        # hotel watches during a run can be checked against what was run.
        cls.printed = io.StringIO()
        with contextlib.redirect_stdout(cls.printed):
            cls.payload = pilot.run_one(cls.csv_path, FIXTURE_HOTEL, cls.settings_path,
                                        cls.out_dir, score_first=SCORE_FIRST,
                                        score_last=SCORE_LAST,
                                        warmup_end=SCORE_FIRST - dt.timedelta(days=1),
                                        progress=90)
        cls.written = os.path.join(cls.out_dir, "pilot-pilot.json")
        with open(cls.written, "rb") as fh:
            cls.on_disk = fh.read()
        # A second run of the same hotel, scoring three weeks instead of three
        # months, standing in for `--quick`.
        cls.quick = pilot.run_one(cls.csv_path, FIXTURE_HOTEL, cls.settings_path, cls.out_dir,
                                  score_first=SCORE_FIRST,
                                  score_last=SCORE_FIRST + dt.timedelta(days=cls.QUICK_NIGHTS - 1),
                                  warmup_end=SCORE_FIRST - dt.timedelta(days=1),
                                  label="quick", with_holdout=False)
        with open(cls.written, "rb") as fh:
            cls.on_disk_after_quick = fh.read()
        with open(cls.written, encoding="utf-8") as fh:
            cls.back = json.load(fh)
        with open(FIXTURE_HOTEL, encoding="utf-8") as fh:
            cls.hotel_json = json.load(fh)

    @classmethod
    def tearDownClass(cls):
        from pace import calendar as C
        from pace import config
        C.reset_seasonality()
        config.reset_segments()
        pilot._release_for_tests()

    def test_the_payload_is_written_under_the_hotel_s_own_code(self):
        """tests/fixtures/pilot-hotel.json is named "Pilot Fixture Hotel", so
        the code is PILOT and the file is pilot-pilot.json.  Building the
        expected name out of payload["hotel"]["code"] would assert nothing:
        both sides would move together, and the file would still be found
        with the code mutated to WRONG.
        """
        self.assertEqual(self.payload["hotel"]["code"], "PILOT")
        self.assertTrue(os.path.exists(os.path.join(self.out_dir, "pilot-pilot.json")))
        self.assertEqual(self.payload["_json_path"], os.path.join(self.out_dir, "pilot-pilot.json"))

    def test_what_is_on_disk_is_the_payload_that_was_returned(self):
        """Minus the path itself, which is added after the write and is the
        one key the file cannot carry."""
        self.assertEqual(self.back,
                         {k: v for k, v in self.payload.items() if k != "_json_path"})

    def test_the_windows_are_the_ones_the_run_was_asked_for(self):
        w = self.back["windows"]
        ledger_rows = _rows_that_reach_the_ledger(self.dicts)
        first_arrival = min(dt.date.fromisoformat(r["arrival"]) for r in ledger_rows)
        self.assertEqual(first_arrival, dt.date(2024, 1, 1))
        # Stays in this fixture are 1, 2 or 3 nights, so the 99th percentile
        # of their lengths is 3 and the front of the window is trimmed by 3.
        self.assertEqual(w["trim_nights"], 3)
        self.assertEqual(w["first_stay"], "2024-01-04")
        # The history runs to 2025-05-14, well past the scoring window, so the
        # back of the stay window is the scoring cap and not the export's end.
        self.assertEqual(w["last_stay"], SCORE_LAST.isoformat())
        self.assertEqual(w["score_first"], "2025-02-01")
        self.assertEqual(w["score_last"], "2025-04-30")
        self.assertEqual(w["warmup_end"], "2025-01-31")
        # DRIVE_LEAD is 180 days of rate path in front of the window, so the
        # engine is driven from 2025-02-01 minus 180 days.
        self.assertEqual(w["drive_from"], "2024-08-05")
        self.assertEqual(w["scoring_nights"], self.SCORING_NIGHTS)

    def test_the_walk_reached_every_night_of_the_window_at_every_mark(self):
        w = self.back["walk"]
        ledger_rows = _rows_that_reach_the_ledger(self.dicts)
        first_day = min(dt.date.fromisoformat(r["booked_on"]) for r in ledger_rows)
        self.assertEqual(first_day, dt.date(2023, 11, 28))
        # The day loop opens on the earliest booking in the file and closes on
        # the last night of the stay window.
        self.assertEqual(w["days"], (SCORE_LAST - first_day).days + 1)
        self.assertEqual(w["days"], 520)
        self.assertEqual(w["marks"], [30, 14, 7])
        # Three marks plus the late mark, at every night of the window: the
        # walk is driven from 2024-08-05, which is in front of 2025-02-01 by
        # more than the deepest mark, so no night is short of a record.
        self.assertEqual(w["records"], 4 * self.SCORING_NIGHTS)
        self.assertEqual(w["records"], 356)
        # One fit before the first driven day, then one every 28 days of the
        # 269 driven days.
        self.assertEqual(w["fits"], 1 + (self.DRIVEN_DAYS - 1) // 28)
        self.assertEqual(w["fits"], 10)
        self.assertGreater(w["solves"], 0)
        self.assertGreater(w["seconds"], 0)

    def test_the_progress_line_counts_the_cut_replays_the_grid_runs(self):
        """The line a hotel reads while table 3 runs says how many cut replays
        are coming: the pre-registered grid's own count, eight pairs of
        threshold and cap under three rules, and the neighbour window read off
        the CSV, not a number typed into the line."""
        lines = [l for l in self.printed.getvalue().splitlines() if "table 3" in l]
        self.assertEqual(lines, ["    table 3: 24 cut replays of the history, neighbour window 2"])
        self.assertEqual(len(self.back["table3"]["combos"]), 24)
        self.assertEqual(len(holdout.grid_cells()), 24)

    def test_the_ingest_block_carries_what_the_ingest_found(self):
        """The block is labelled `ingest`, so it says what ingest found.

        Filled from the walk's own fresh Report it would publish "no
        warnings" over rows the reader was flagged for: at H1 that is
        rate_nonpositive 752 and rate_out_of_range 5,555 reported as nothing
        at all, beside a notes list that does come from the ingest.
        """
        block = self.back["ingest"]
        ledger_rows = _rows_that_reach_the_ledger(self.dicts)
        last_stay = SCORE_LAST
        self.assertEqual(block["rows"], len(self.dicts))
        self.assertEqual(block["rows"], 2474)
        self.assertEqual(block["nonrev_rows"],
                         sum(1 for r in self.dicts if r["segment"] == "COMP"))
        self.assertEqual(block["bookings"],
                         sum(1 for r in ledger_rows
                             if dt.date.fromisoformat(r["booked_on"]) <= last_stay))
        self.assertEqual(block["cancels"],
                         sum(1 for r in ledger_rows
                             if r["status"] == "cancelled" and r["status_date"]
                             and dt.date.fromisoformat(r["status_date"]) <= last_stay))
        # The two rows priced at zero. Only the reader can have counted these.
        self.assertEqual(block["warnings"]["rate_nonpositive"], 2)

    def test_the_two_replays_over_capacity_counts_are_not_added_together(self):
        """ingest.replay and pilot.walk both settle the same nights and both
        count over_capacity_nights into the report they are handed.  Sharing
        one report is what puts the ingest's warnings in the payload; adding
        the two counts together would publish a number that is a count of
        nothing, so what the walk added is published as the walk's own.
        """
        ingest_block = self.back["ingest"]["warnings"]
        walk_block = self.back["walk"]["over_capacity"]
        # The fixture pushes one deliberate spike over the twenty rooms, and
        # the ledger's rooms are a subset of the rooms in the house, so at
        # most the one physically full night can be over capacity.
        full = _physically_full_nights(self.dicts, 20, 0.97,
                                       dt.date(2024, 1, 4), SCORE_LAST)
        self.assertEqual(len(full), 1)
        self.assertEqual(ingest_block["over_capacity_nights"], 1)
        self.assertEqual(walk_block["over_capacity_nights"], 1)
        self.assertEqual(ingest_block["rooms_walked_off_the_actuals"],
                         walk_block["rooms_walked_off_the_actuals"])

    def test_the_hotel_block_is_the_hotel_json_it_was_given(self):
        h = self.back["hotel"]
        j = self.hotel_json
        self.assertEqual(h["name"], j["name"])
        self.assertEqual(h["city"], j["city"])
        self.assertEqual(h["currency"], j["currency"])
        self.assertEqual(h["rooms"], j["sellable_rooms"])
        self.assertEqual(h["rooms"], 20)
        self.assertEqual(h["base_rate"], j["base_rate"])
        self.assertEqual(h["rate_floor"], j["rate_floor"])
        self.assertEqual(h["rate_ceiling"], j["rate_ceiling"])
        self.assertEqual(h["rate_step"], j["rate_step"])
        self.assertEqual(h["max_lead"], j["max_lead"])
        self.assertEqual(h["max_los"], j["max_los"])
        self.assertEqual(h["sellout_threshold"], j["sellout_threshold"])
        self.assertEqual(h["variable_cost"], j["variable_cost"])
        self.assertEqual(h["walk_cost"], j["walk_cost"])
        self.assertEqual(h["demand_season_band"], j["demand_season_band"])
        self.assertEqual(h["segment_rate_ratio"], j["segment_rate_ratio"])
        self.assertEqual(h["rates_include_tax"], j["rates_include_tax"])
        # 80 to 201 in steps of 2 stops at 200: the ceiling is not a rung.
        self.assertEqual(h["top_rung"], 200.0)
        # sellable_rooms is set in the fixture, so nothing was inferred.
        self.assertFalse(h["rooms_inferred"])
        self.assertIsNone(h["peak_night"])
        self.assertEqual(h["per_year_max"], {})

    def test_the_prereg_block_records_the_settings_file_that_was_read(self):
        with open(self.settings_path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        p = self.back["prereg"]
        self.assertEqual(p["settings_sha256"], digest)
        self.assertEqual(p["settings_path"], self.settings_path)
        self.assertEqual(p["commit"], pilot.PREREG_COMMIT)
        # This run was given a settings file of its own, so the flag has to
        # say the digest is not the pre-registered one.  A flag that is always
        # true is worth less than no flag.
        self.assertNotEqual(digest, pilot.PREREG_SHA256)
        self.assertFalse(p["sha256_matches_the_recorded_one"])

    def test_table_one_is_scored_at_the_marks_that_were_asked_for(self):
        t = self.back["table1"]
        self.assertEqual(t["leads"], ["30", "14", "7"])
        self.assertEqual(t["late_lead"], "1")
        self.assertEqual(t["methods"], ["engine", "pickup_add", "pickup_mult",
                                        "stly_add", "average"])
        self.assertEqual(sorted(t["overall"]), sorted(["30", "14", "7", "1"]))
        for lead, block in t["overall"].items():
            # A night is scored once, and only when every method could answer
            # for it, so n can fall short of the window but never exceed it.
            self.assertLessEqual(block["n"], self.SCORING_NIGHTS)
            self.assertGreater(block["n"], 0)
            # Capacity is the room count less the comped rooms of the night,
            # so it sits just under 20; handed an empty non-revenue list it
            # would be exactly 20 on every night.
            self.assertLess(block["capacity_mean"], 20.0)
            self.assertGreater(block["capacity_mean"], 19.0)
            head = t["headline"][lead]
            self.assertEqual(head["engine_mae"], block["methods"]["engine"]["mae"])
            self.assertEqual(head["average_mae"], block["methods"]["average"]["mae"])

    def test_the_season_cut_is_taken_over_the_warm_up_window(self):
        """score() is handed the warm-up window as (first, last).  Swapped,
        it reads no settled night at all and falls back to ranking the months
        by their number, which makes January high season and July shoulder.

        The fixture books July and August fuller than any other month by
        construction, and the warm-up window covers a whole year of it.
        """
        seasons = self.back["table1"]["season_of_month"]
        self.assertEqual(seasons["7"], "high")
        self.assertEqual(seasons["8"], "high")
        self.assertEqual(collections.Counter(seasons.values()),
                         collections.Counter({"high": 4, "shoulder": 4, "low": 4}))

    def test_the_full_night_gap_counts_the_window_it_was_handed(self):
        gap = self.back["full_night_gap"]
        first_stay = dt.date(2024, 1, 4)
        self.assertEqual(gap["nights"], (SCORE_LAST - first_stay).days + 1)
        self.assertEqual(gap["nights"], 483)
        self.assertEqual(gap["rooms"], 20)
        self.assertEqual(gap["threshold_rooms"], 19.4)
        full = _physically_full_nights(self.dicts, 20, 0.97, first_stay, SCORE_LAST)
        self.assertEqual(gap["physically_full"], len(full))
        self.assertEqual(gap["physically_full"], 1)
        # Both shares say which population they are a share of, and each one
        # agrees with the counts beside it.
        self.assertAlmostEqual(gap["share_of_nights"],
                               gap["full_but_invisible"] / float(gap["nights"]))
        self.assertAlmostEqual(gap["share_of_full_nights"],
                               gap["full_but_invisible"] / float(gap["physically_full"]))

    def test_table_three_is_none_when_the_holdout_is_not_run(self):
        self.assertIsNone(self.quick["table3"])
        with open(os.path.join(self.out_dir, "pilot-pilot-quick.json"), encoding="utf-8") as fh:
            self.assertIsNone(json.load(fh)["table3"])

    def test_table_three_is_the_grid_over_this_run_s_stay_window(self):
        """The grid run again here, on a ledger ingest builds over the stay
        window the test works out for itself, with the neighbour window read
        off the CSV: the 90th percentile of the nights of every row that
        reaches the ledger.  The payload has to be that grid exactly."""
        stays = sorted(int(r["nights"]) for r in _rows_that_reach_the_ledger(self.dicts))
        window = int(pilot.percentile([float(n) for n in stays], 0.90))
        self.assertEqual(window, 2)
        self.assertEqual(self.back["table3"]["window"], window)
        first_stay = dt.date.fromisoformat(self.back["windows"]["first_stay"])
        self.assertEqual(first_stay, FIRST_ARRIVAL + dt.timedelta(days=int(pilot.percentile(
            [float(n) for n in stays], 0.99))))
        res = ingest.load(self.csv_path, FIXTURE_HOTEL, seed=1,
                          first_stay=first_stay, last_stay=SCORE_LAST)
        grid = holdout.run_grid(res.bookings, res.hotel, res.ledger, first_stay, SCORE_LAST,
                                window)
        self.assertEqual(self.back["table3"], json.loads(json.dumps(grid)))
        self.assertEqual(len(self.back["table3"]["combos"]), 24)

    def test_table_two_was_handed_this_run_s_own_window_and_hotel(self):
        """Every number here is worked out from the CSV or from
        tests/fixtures/pilot-hotel.json, never read back out of the payload.

        The share is the discriminating one: counted over the scoring window
        it is 126 STD room nights out of 164, and counted over the whole
        history it is a different number, so a table2 handed the stay window
        instead of the scoring window fails here rather than looking fine.
        """
        t2 = self.back["table2"]
        priced = _priced_room_nights(self.dicts, SCORE_FIRST, SCORE_LAST)
        self.assertEqual(priced, {"STD": 126, "SUP": 38})
        self.assertEqual(t2["room_type"], "STD")
        self.assertAlmostEqual(t2["room_type_share"], 126 / 164.0)
        self.assertEqual(t2["meals"], ["BB", "HB"])
        # The band, read off the hotel.json this run was pointed at: floor 80,
        # ceiling 201, step 2, so the ladder stops at 200 and the ceiling test
        # can never fire.
        self.assertEqual(t2["rate_floor"], 80.0)
        self.assertEqual(t2["rate_ceiling"], 201.0)
        self.assertEqual(t2["top_rung"], 200.0)
        self.assertLess(t2["top_rung"], t2["rate_ceiling"])

    def test_table_two_scored_the_nights_the_csv_says_it_could(self):
        """The count at each mark, derived from the CSV by a basket written
        here rather than by the one under test.

        This fixture drives the engine for 180 days before the window opens,
        so every night of it has a record with a rate on it; what decides
        whether a night is scored is whether anyone booked the top room type
        inside that mark's comparison window.  A mark reading the wrong lead,
        the wrong window bounds, the wrong room type or the wrong segments
        lands on a different count.  It says nothing about the scoring window
        being too wide, because the walk files no record outside it: the
        share above catches that, and the partial run below catches a window
        too narrow.
        """
        t2 = self.back["table2"]
        for mark, expect in (("30", 36), ("14", 39), ("7", 24)):
            lo, hi = ratecheck.RATE_WINDOWS[mark]
            counted = _nights_with_a_basket(self.dicts, SCORE_FIRST, SCORE_LAST, lo, hi, "STD")
            self.assertEqual(counted, expect, "mark %s" % mark)
            self.assertEqual(t2["cells"][mark]["all"]["n"], expect, "mark %s" % mark)
            self.assertEqual(t2["pinned"][mark]["n"], expect, "mark %s" % mark)
        # This fixture's marks are 30, 14 and 7 and its longest lead is 35
        # days, so the 60 row has neither a record to read nor anyone who
        # booked that far out.  Which of the two empties it is settled on the
        # hand-built walk in Table2, not here.
        self.assertEqual(t2["cells"]["60"]["all"]["n"], 0)
        self.assertEqual(_nights_with_a_basket(self.dicts, SCORE_FIRST, SCORE_LAST,
                                               75, 45, "STD"), 0)
        self.assertIsNone(t2["pinned"]["60"]["share"])
        self.assertEqual(t2["pinned_overall"]["n"], 36 + 39 + 24)

    def test_table_two_cuts_by_the_seasons_table_one_measured(self):
        """Not by hotel.json's demand_season_band, which is an engine input
        and labels its months peak, shoulder and trough."""
        t2 = self.back["table2"]
        months = set(range(SCORE_FIRST.month, SCORE_LAST.month + 1))
        expected = set(self.back["table1"]["season_of_month"][str(m)] for m in months)
        self.assertEqual(expected, {"low", "shoulder"})
        for mark in ("30", "14", "7"):
            self.assertEqual(set(t2["cells"][mark]["by_season"]), expected)
            self.assertEqual(sum(c["n"] for c in t2["cells"][mark]["by_season"].values()),
                             t2["cells"][mark]["all"]["n"])
            self.assertEqual(t2["cells"][mark]["nonrev_nights"]["n"]
                             + t2["cells"][mark]["clean_nights"]["n"],
                             t2["cells"][mark]["all"]["n"])

    def test_table_two_carries_the_mark_sixty_label_for_a_hotel_that_is_not_h1(self):
        """This fixture's code is PILOT, so it gets the label; H1 does not."""
        self.assertEqual(self.back["hotel"]["code"], "PILOT")
        self.assertEqual(self.back["table2"]["notes"][-1], ratecheck.MARK60_NOTE)
        self.assertEqual(len(self.back["table2"]["notes"]), 6)

    def test_a_partial_run_s_table_two_is_over_the_partial_window(self):
        quick_last = SCORE_FIRST + dt.timedelta(days=self.QUICK_NIGHTS - 1)
        self.assertEqual(self.quick["table2"]["room_type"], "STD")
        for mark in ("30", "14", "7"):
            lo, hi = ratecheck.RATE_WINDOWS[mark]
            self.assertEqual(self.quick["table2"]["cells"][mark]["all"]["n"],
                             _nights_with_a_basket(self.dicts, SCORE_FIRST, quick_last,
                                                   lo, hi, "STD"),
                             "mark %s" % mark)
        self.assertLess(self.quick["table2"]["pinned_overall"]["n"],
                        self.back["table2"]["pinned_overall"]["n"])

    def test_the_notes_the_payload_promises_are_in_it(self):
        notes = self.back["notes"]
        self.assertEqual(len(notes), 2)
        self.assertIn("no number in this report is a revenue lift", " ".join(notes).lower())
        # The second note is the one that keeps the two full-night counts
        # apart; dropping it leaves the numbers with nothing saying which is
        # which.
        self.assertIn("full_but_uncensored_by_snapshot", " ".join(notes))
        self.assertEqual(notes, [pilot.NO_LIFT_NOTE, pilot.FULL_NIGHT_NOTE])

    def test_a_partial_run_cannot_overwrite_or_be_mistaken_for_a_whole_one(self):
        """A run over part of the window is not the pilot's result, so it is
        a file of its own and says so in its own payload.

        The name alone would not survive the file being renamed or copied; the
        field alone would still let a three-week run land on top of a
        three-month one on disk.  Task 10 joins two hotels from two files, and
        the failure this stops is a 21-night H1 being reported beside a
        427-night H2 with nothing on either to say so.
        """
        quick_path = os.path.join(self.out_dir, "pilot-pilot-quick.json")
        self.assertEqual(self.quick["_json_path"], quick_path)
        self.assertTrue(os.path.exists(quick_path))
        self.assertEqual(self.quick["label"], "quick")
        self.assertEqual(self.payload["label"], "full")
        self.assertEqual(self.quick["windows"]["scoring_nights"], self.QUICK_NIGHTS)
        # The whole run's payload is untouched by the partial one.
        self.assertEqual(self.on_disk, self.on_disk_after_quick)
        self.assertEqual(sorted(f for f in os.listdir(self.out_dir) if f.startswith("pilot-")),
                         ["pilot-pilot-quick.json", "pilot-pilot.json"])


class RunOneErrors(unittest.TestCase):
    """Every one of these is a path or a file typed on a command line, so
    every one of them is owed a sentence rather than a traceback
    (pace/hotelconfig.py's house rule, and ADR 0007's reason for it)."""

    def setUp(self):
        self.out_dir = tempfile.mkdtemp()
        self.settings = _fixture_settings(os.path.join(self.out_dir, "settings.json"))
        self.csv = _csv([_row(booking_id="A1")])

    def tearDown(self):
        from pace import calendar as C
        from pace import config
        C.reset_seasonality()
        config.reset_segments()
        pilot._release_for_tests()

    def _run(self, csv_path=None, hotel=None, settings=None, out_dir=None, **kw):
        return pilot.run_one(csv_path or self.csv, hotel or FIXTURE_HOTEL,
                             settings or self.settings, out_dir or self.out_dir, **kw)

    def test_a_missing_booking_log_is_a_sentence(self):
        with self.assertRaises(pilot.PilotError) as ctx:
            self._run(csv_path=os.path.join(self.out_dir, "not-here.csv"))
        self.assertIn("no such file", str(ctx.exception))

    def test_a_booking_log_that_is_a_directory_is_a_sentence(self):
        with self.assertRaises(pilot.PilotError) as ctx:
            self._run(csv_path=self.out_dir)
        self.assertIn("directory", str(ctx.exception))

    def test_a_missing_settings_file_is_a_sentence(self):
        with self.assertRaises(pilot.PilotError) as ctx:
            self._run(settings=os.path.join(self.out_dir, "nothing.json"))
        self.assertIn("settings", str(ctx.exception))

    def test_a_settings_file_that_is_not_json_is_a_sentence(self):
        path = os.path.join(self.out_dir, "broken.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"seed": 1,')
        with self.assertRaises(pilot.PilotError) as ctx:
            self._run(settings=path)
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_settings_without_lead_marks_is_a_sentence(self):
        path = os.path.join(self.out_dir, "no-marks.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"seed": 1}, fh)
        with self.assertRaises(pilot.PilotError) as ctx:
            self._run(settings=path)
        self.assertIn("lead_marks", str(ctx.exception))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root writes into a read-only directory anyway")
    def test_an_unwritable_out_directory_fails_before_the_walk(self):
        """The walk is minutes on a real hotel.  Finding out that --out is
        read-only at the moment the payload is written throws away the whole
        run, which is the one failure that costs everything it had already
        done.

        `progress=1` makes the walk audible: it prints a line for every day it
        walks.  Nothing printed is the demonstration that nothing was walked.
        """
        locked = tempfile.mkdtemp()
        os.chmod(locked, 0o500)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                with self.assertRaises(pilot.PilotError) as ctx:
                    self._run(out_dir=locked, progress=1)
            self.assertIn("output directory", str(ctx.exception))
            self.assertEqual(out.getvalue(), "")
        finally:
            os.chmod(locked, 0o700)

    def test_a_stay_window_that_opens_after_the_scoring_window_is_a_sentence(self):
        """The guard that says there is no warm-up to fit on.  With it
        removed the run walks a window it has no history for and scores an
        empty table instead of saying why.
        """
        rows = [_row(booking_id="A%d" % i,
                     arrival=(dt.date(2024, 6, 1) + dt.timedelta(days=i)).isoformat())
                for i in range(10)]
        with self.assertRaises(pilot.PilotError) as ctx:
            self._run(csv_path=_csv(rows), score_first=dt.date(2024, 1, 1),
                      score_last=dt.date(2024, 6, 20), warmup_end=dt.date(2023, 12, 31))
        self.assertIn("no warm-up to fit on", str(ctx.exception))


class Switchboard(unittest.TestCase):
    """run.py's pilot branch: what it passes down, and what it prints back."""

    def setUp(self):
        import run as runpy_module
        self.run = runpy_module
        self.real_run_one = pilot.run_one

    def tearDown(self):
        pilot.run_one = self.real_run_one
        pilot._release_for_tests()

    def _main(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.run.main(["run.py"] + argv)
        return code, out.getvalue()

    def _payload(self):
        """A payload with numbers chosen so a line that prints the wrong field
        prints a number that is visibly not the one asked for."""
        methods = {name: {"mae": 4.0, "clamp_share": 0.25} for name in
                   ("engine", "pickup_add", "pickup_mult", "stly_add", "average")}
        methods["engine"] = {"mae": 3.5, "clamp_share": 0.25}
        methods["average"] = {"mae": 6.5, "clamp_share": 0.0}
        return {
            "label": "full",
            "hotel": {"name": "H1 Resort Hotel", "code": "H1", "rooms": 187,
                      "rooms_inferred": True},
            "walk": {"days": 898, "seconds": 16.1, "fits": 22, "records": 434},
            "full_night_gap": {"nights": 414, "physically_full": 86,
                               "full_but_invisible": 25, "share_of_full_nights": 25 / 86.0},
            "table1": {"leads": ["30"], "late_lead": "1",
                       "overall": {"30": {"n": 62, "methods": methods},
                                   "1": {"n": 0, "methods": {"engine": {"mae": None}}}}},
            "_json_path": "/tmp/out/pilot-h1.json",
        }

    def test_the_pilot_command_needs_both_paths(self):
        code, text = self._main(["pilot"])
        self.assertEqual(code, 1)
        self.assertIn("usage: python3 run.py pilot", text)

    def test_an_unknown_command_prints_the_doc_and_fails(self):
        code, text = self._main(["nonsense"])
        self.assertEqual(code, 1)
        self.assertIn("run.py pilot", text)

    def test_the_log_and_the_hotel_are_passed_in_the_order_they_were_typed(self):
        seen = {}

        def fake(csv_path, hotel_json_path, settings_path, out_dir, **kw):
            seen.update(csv=csv_path, hotel=hotel_json_path, settings=settings_path,
                        out=out_dir, kw=kw)
            return self._payload()

        pilot.run_one = fake
        code, _text = self._main(["pilot", "bookings.csv", "hotel.json"])
        self.assertEqual(code, 0)
        self.assertEqual(seen["csv"], "bookings.csv")
        self.assertEqual(seen["hotel"], "hotel.json")
        # The defaults, when neither flag is given.
        self.assertEqual(seen["out"], os.path.join(ROOT, "out"))
        self.assertEqual(seen["settings"],
                         os.path.join(ROOT, "data", "antonio", "settings.json"))
        self.assertEqual(seen["kw"]["score_first"], pilot.SCORE_FIRST)
        self.assertEqual(seen["kw"]["score_last"], pilot.SCORE_LAST)
        self.assertEqual(seen["kw"]["label"], "")
        self.assertIs(seen["kw"]["with_holdout"], True)

    def test_quick_moves_the_far_end_of_the_window_and_labels_the_run(self):
        """--quick is a wiring check, not a result.  It shortens the window
        from the far end: the near end is what the warm-up and the 180 driven
        days in front of it are cut to, so moving it would move the run-up
        as well and score nights the engine was never driven towards.
        """
        first, last, label = self.run._pilot_window(["--quick"])
        self.assertEqual(first, pilot.SCORE_FIRST)
        self.assertEqual(last, pilot.SCORE_FIRST + dt.timedelta(days=61))
        self.assertEqual(last, dt.date(2016, 8, 31))
        self.assertEqual(label, "quick")
        plain = self.run._pilot_window([])
        self.assertEqual(plain, (pilot.SCORE_FIRST, pilot.SCORE_LAST, ""))

    def test_the_flags_are_read_off_the_arguments(self):
        self.assertEqual(self.run._opt(["--out", "here"], "--out", "fallback"), "here")
        self.assertEqual(self.run._opt([], "--out", "fallback"), "fallback")
        # A flag with nothing after it takes the default rather than raising.
        self.assertEqual(self.run._opt(["--out"], "--out", "fallback"), "fallback")

    def test_the_flags_reach_run_one(self):
        seen = {}

        def fake(csv_path, hotel_json_path, settings_path, out_dir, **kw):
            seen.update(settings=settings_path, out=out_dir, kw=kw)
            return self._payload()

        pilot.run_one = fake
        code, _text = self._main(["pilot", "b.csv", "h.json", "--out", "/tmp/elsewhere",
                                  "--settings", "/tmp/s.json", "--quick"])
        self.assertEqual(code, 0)
        self.assertEqual(seen["out"], "/tmp/elsewhere")
        self.assertEqual(seen["settings"], "/tmp/s.json")
        self.assertEqual(seen["kw"]["label"], "quick")
        self.assertEqual(seen["kw"]["score_last"], pilot.SCORE_FIRST + dt.timedelta(days=61))
        # --quick is a wiring check and skips the 24 cut replays of table 3.
        self.assertIs(seen["kw"]["with_holdout"], False)

    def test_what_it_prints_says_which_population_each_share_is_of(self):
        """25 of 86 full nights is 29 percent of the full nights and 6 percent
        of the window.  Printed as "25 of 414" it reads as the second while
        being the first, and full_night_gap's own docstring is written against
        exactly that.
        """
        pilot.run_one = lambda *a, **kw: self._payload()
        code, text = self._main(["pilot", "b.csv", "h.json"])
        self.assertEqual(code, 0)
        self.assertIn("H1 Resort Hotel (H1), 187 rooms, inferred", text)
        self.assertIn("walked 898 days in 16.1 s, 22 fits, 434 forecasts recorded", text)
        self.assertIn("nights physically full 86 of 414 in the window", text)
        self.assertIn("25 invisible to the ledger (29% of the full nights)", text)
        self.assertIn("lead 30   n=62   engine MAE   3.50 (clamped   25%), average   6.50", text)
        # A lead with nothing scored says so rather than printing a None.
        self.assertIn("lead 1    no night scored", text)
        self.assertIn("wrote /tmp/out/pilot-h1.json", text)

    def test_a_bad_path_comes_back_as_one_sentence_and_exit_1(self):
        out_dir = tempfile.mkdtemp()
        code, text = self._main(["pilot", os.path.join(out_dir, "nope.csv"), FIXTURE_HOTEL,
                                 "--out", out_dir,
                                 "--settings", _fixture_settings(
                                     os.path.join(out_dir, "settings.json"))])
        self.assertEqual(code, 1)
        self.assertEqual(len(text.strip().splitlines()), 1)
        self.assertIn("no such file", text)
        self.assertNotIn("Traceback", text)


def _rows_with_a_deliberate_near_tie_at_the_top(rows):
    """Raise two nights to a near-tie at the top of physical occupancy.

    The synthetic history peaks on a lone night, so `nights_within_2pct` is
    genuinely 1 and `second_highest` sits far below the peak.  Both fields
    then pass with the trivial answer hard-wired, and no assertion written
    against that fixture can tell a real answer from a guessed one: a bound
    like `>= 1` or `<= rooms` is satisfied by the wrong value as readily as
    by the right one.

    The plateau is built at 100, 99, 99, 98 and 98 rooms, far above anything
    the synthetic history reaches, and that scale is the point: "within 2
    percent" of 100 is 98, so all five nights count and none of them ties the
    peak, and the true answers are 5 and 99 against a room count of 100.  Five
    rather than two, because a single-scenario assertion is passed by any
    constant that happens to equal the right answer, and 1 and 2 are the two
    constants someone would reach for.  At the fixture hotel's
    own scale the two fields cannot be separated at all, because 2 percent of
    twenty rooms is less than one room and "within 2 percent" collapses into
    "equal", which would force `second_highest` to equal `rooms` and put the
    two checks in contradiction.  A hundred-room plateau on a twenty-room
    fixture is artificial; isolating `infer_sellable_rooms`'s arithmetic is
    what the fixture is for, and the room count is the thing under test.
    """
    occ = collections.Counter()
    for r in _as_dicts(rows):
        if r["status"] not in ("stayed", "in_house") or int(r["nights"]) < 1:
            continue
        arrival = dt.date.fromisoformat(r["arrival"])
        for k in range(int(r["nights"])):
            occ[arrival + dt.timedelta(days=k)] += int(r["rooms"])
    extra = []
    for offset, target in ((200, 100), (201, 99), (202, 99), (203, 98), (204, 98)):
        night = FIRST_ARRIVAL + dt.timedelta(days=offset)
        assert occ[night] < target, (night, occ[night], target)
        for i in range(target - occ[night]):
            extra.append(_row(booking_id="TIE%d-%d" % (offset, i),
                              booked_on=(night - dt.timedelta(days=30)).isoformat(),
                              arrival=night.isoformat(), nights="1"))
    return rows + extra


def _hotel_without_a_room_count(out_dir):
    """The fixture hotel with sellable_rooms left null, which is what an
    export from a PMS that does not state the room count looks like."""
    with open(FIXTURE_HOTEL, encoding="utf-8") as fh:
        raw = json.load(fh)
    raw["sellable_rooms"] = None
    path = os.path.join(out_dir, "hotel.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(raw, fh)
    return path


class RunOneWhenTheRoomCountIsInferred(unittest.TestCase):
    """RunOne's fixture states sellable_rooms, so `rooms_inferred` is False
    there no matter what the code does: that class's assertion on the field
    passes with it hard-wired to False, and the three fields that describe
    the inference are all empty either way.  Only a hotel whose room count
    really was inferred can tell the two apart.
    """

    @classmethod
    def setUpClass(cls):
        from pace import plugins as _plugins
        _plugins.reset()
        cls.rows = _rows_with_a_deliberate_near_tie_at_the_top(_payload_rows())
        cls.out_dir = tempfile.mkdtemp()
        cls.payload = pilot.run_one(
            _csv(cls.rows), _hotel_without_a_room_count(cls.out_dir),
            _fixture_settings(os.path.join(cls.out_dir, "settings.json")), cls.out_dir,
            score_first=SCORE_FIRST, score_last=SCORE_FIRST + dt.timedelta(days=13),
            warmup_end=SCORE_FIRST - dt.timedelta(days=1))

    @classmethod
    def tearDownClass(cls):
        from pace import calendar as C
        from pace import config
        C.reset_seasonality()
        config.reset_segments()
        pilot._release_for_tests()

    def _occupancy(self):
        """Physical occupancy per night, counted here rather than read back out
        of the payload: `infer_sellable_rooms` derives every field in the
        inference block from this one map, so rebuilding it is the same
        arithmetic arrived at by a different road."""
        occ = collections.Counter()
        for r in _as_dicts(self.rows):
            if r["status"] not in ("stayed", "in_house") or int(r["nights"]) < 1:
                continue
            arrival = dt.date.fromisoformat(r["arrival"])
            for k in range(int(r["nights"])):
                occ[arrival + dt.timedelta(days=k)] += int(r["rooms"])
        return occ

    def test_the_payload_says_the_room_count_was_inferred_and_from_where(self):
        """Every field is checked against this fixture's own arithmetic.

        A bound rather than a value is not a check: `nights_within_2pct >= 1`
        passes with the field hard-wired to 1, and `second_highest <= rooms`
        passes with it set to `rooms` itself, which is the one wrong answer
        that matters, because the gap between the two is what says whether
        the peak was a lone spike or a real ceiling.
        """
        h = self.payload["hotel"]
        occ = self._occupancy()
        night, rooms = max(occ.items(), key=lambda kv: (kv[1], kv[0]))
        counts = sorted(occ.values(), reverse=True)
        self.assertTrue(h["rooms_inferred"])
        self.assertEqual(h["rooms"], rooms)
        self.assertEqual(h["peak_night"], night.isoformat())
        self.assertEqual(h["nights_within_2pct"],
                         sum(1 for c in counts if c >= rooms * 0.98))
        self.assertEqual(h["second_highest"], counts[1])
        # The fixture has to be able to tell a hard-wired answer from a real
        # one, so neither field may happen to equal the trivial value.
        self.assertGreater(h["nights_within_2pct"], 1)
        self.assertLess(h["second_highest"], h["rooms"])
        self.assertEqual(max(h["per_year_max"].values()), h["rooms"])
        self.assertEqual(sorted(h["per_year_max"]),
                         sorted({str(d.year) for d in occ}))


def _reset_config():
    from pace import calendar as C
    from pace import config
    C.reset_seasonality()
    config.reset_segments()


class RateCheck(unittest.TestCase):
    """The parts of table 2, each on a fixture whose right answer was worked
    out before the code ran and is not a number a wrong answer would reach."""

    def tearDown(self):
        _reset_config()

    def _bookings(self, rows):
        return ingest.load(_csv(rows), FIXTURE_HOTEL, seed=1).bookings

    def test_the_room_type_that_slept_the_most_nights_wins_not_the_one_with_the_most_rows(self):
        """Three three-night stays in a junior suite are nine room nights;
        five one-night stays in a standard are five.  Counting bookings makes
        STD the winner, counting room nights makes JS the winner, and the
        table compares rates for a night, so it is nights that decide.
        """
        first, last = dt.date(2024, 2, 1), dt.date(2024, 2, 3)
        rows = [_row(booking_id="J%d" % i, arrival="2024-02-01", nights="3", room_type="JS")
                for i in range(3)]
        rows += [_row(booking_id="S%d" % i, arrival="2024-02-02", nights="1", room_type="STD")
                 for i in range(5)]
        # Twelve suite nights outside the window: the winner is decided inside
        # it, so a table2 handed the wrong window answers SUP instead.
        rows += [_row(booking_id="O%d" % i, arrival="2024-03-01", nights="1", room_type="SUP")
                 for i in range(12)]
        rows += [
            _row(booking_id="COMP1", segment="COMP", arrival="2024-02-01", nights="3",
                 room_type="STD", rate="0"),
            _row(booking_id="CORP1", segment="CORP", arrival="2024-02-01", nights="3",
                 room_type="STD", rate="96"),
            _row(booking_id="CXL1", arrival="2024-02-01", nights="3", room_type="STD",
                 status="cancelled", status_date="2024-01-20"),
            _row(booking_id="DAY1", arrival="2024-02-01", nights="0", room_type="STD"),
        ]
        code, share = ratecheck.top_room_type(self._bookings(rows), first, last)
        self.assertEqual(code, "JS")
        # Nine junior-suite nights out of the fourteen priced room nights in
        # the window: the comp, the contract, the cancellation and the day
        # use are all outside the population, and each of them would move
        # this.
        self.assertAlmostEqual(share, 9 / 14.0)

    def test_the_room_type_share_is_none_of_the_shapes_an_empty_answer_takes(self):
        code, share = ratecheck.top_room_type([], dt.date(2024, 2, 1), dt.date(2024, 2, 3))
        self.assertEqual(code, "")
        self.assertEqual(share, 0.0)

    def test_the_basket_takes_both_ends_of_its_window_and_nothing_outside_them(self):
        night = dt.date(2024, 2, 1)
        rows = [_row(booking_id="L%d" % lead, rate="%d" % (100 + lead),
                     booked_on=(night - dt.timedelta(days=lead)).isoformat(),
                     arrival="2024-02-01", nights="1")
                for lead in (6, 7, 21, 22)]
        got = ratecheck.basket(self._bookings(rows), night, 21, 7, "STD")
        self.assertEqual(sorted(got), [107.0, 121.0])

    def test_the_basket_measures_the_lead_to_the_night_not_to_the_arrival(self):
        """A three-night stay booked fifteen days before it arrives was booked
        seventeen days before its third night, and it is the third night's
        rate that the seventeen-day mark published.  So a window of exactly
        seventeen days finds this stay on its last night and on neither of the
        other two.
        """
        rows = [
            _row(booking_id="LONG", booked_on="2024-01-17", arrival="2024-02-01",
                 nights="3", rate="150"),
            # A one-night stay whose own lead to 2024-02-01 is 17, so the
            # window has something to find on that night: the long stay is
            # missing from it because of its lead and not because the window
            # is empty of everything.
            _row(booking_id="SHORT", booked_on="2024-01-15", arrival="2024-02-01",
                 nights="1", rate="181"),
        ]
        bookings = self._bookings(rows)
        self.assertEqual(ratecheck.basket(bookings, dt.date(2024, 2, 1), 17, 17, "STD"),
                         [181.0])
        self.assertEqual(ratecheck.basket(bookings, dt.date(2024, 2, 2), 17, 17, "STD"), [])
        self.assertEqual(ratecheck.basket(bookings, dt.date(2024, 2, 3), 17, 17, "STD"),
                         [150.0])

    def test_the_basket_holds_lead_room_type_and_board_constant(self):
        night = dt.date(2024, 2, 1)
        rows = [
            _row(booking_id="IN", booked_on="2024-01-18", arrival="2024-02-01", nights="1",
                 room_type="STD", meal="BB", rate="150"),            # lead 14, in
            _row(booking_id="LATE", booked_on="2024-01-29", arrival="2024-02-01", nights="1",
                 room_type="STD", meal="BB", rate="90"),             # lead 3, out
            _row(booking_id="EARLY", booked_on="2024-01-02", arrival="2024-02-01", nights="1",
                 room_type="STD", meal="BB", rate="95"),             # lead 30, out
            _row(booking_id="OTHERROOM", booked_on="2024-01-18", arrival="2024-02-01", nights="1",
                 room_type="SUP", meal="BB", rate="300"),            # wrong room type
            _row(booking_id="OTHERMEAL", booked_on="2024-01-18", arrival="2024-02-01", nights="1",
                 room_type="STD", meal="HB", rate="400"),            # wrong board
            _row(booking_id="COMP", segment="COMP", booked_on="2024-01-18",
                 arrival="2024-02-01", nights="1", room_type="STD", meal="BB", rate="0"),
            _row(booking_id="CORP", segment="CORP", booked_on="2024-01-18",
                 arrival="2024-02-01", nights="1", room_type="STD", meal="BB", rate="96"),
            _row(booking_id="CXL", booked_on="2024-01-18", arrival="2024-02-01", nights="1",
                 room_type="STD", meal="BB", rate="999", status="cancelled",
                 status_date="2024-01-20"),
            _row(booking_id="FREE", booked_on="2024-01-18", arrival="2024-02-01", nights="1",
                 room_type="STD", meal="BB", rate="0"),
        ]
        got = ratecheck.basket(self._bookings(rows), night, 21, 7, "STD", "BB")
        self.assertEqual(got, [150.0])

    def test_the_basket_with_no_board_asked_for_takes_every_board(self):
        night = dt.date(2024, 2, 1)
        rows = [_row(booking_id="BB1", booked_on="2024-01-18", arrival="2024-02-01",
                     meal="BB", rate="100"),
                _row(booking_id="HB1", booked_on="2024-01-18", arrival="2024-02-01",
                     meal="HB", rate="200")]
        bookings = self._bookings(rows)
        self.assertEqual(sorted(ratecheck.basket(bookings, night, 21, 7, "STD")),
                         [100.0, 200.0])
        self.assertEqual(ratecheck.basket(bookings, night, 21, 7, "STD", "HB"), [200.0])

    def test_the_basket_trims_the_two_rates_that_are_not_prices(self):
        """One rate in the two real files is negative and one is 5,400.  Six
        rows here, and the first and ninety-ninth percentile take the
        outermost of them off each end.
        """
        night = dt.date(2024, 2, 1)
        rates = [100, 110, 120, 130, 140, 5400]
        rows = [_row(booking_id="R%d" % r, booked_on="2024-01-18", arrival="2024-02-01",
                     rate=str(r)) for r in rates]
        got = ratecheck.basket(self._bookings(rows), night, 21, 7, "STD")
        self.assertEqual(sorted(got), [110.0, 120.0, 130.0, 140.0])

    def test_a_basket_too_short_to_trim_is_returned_whole(self):
        night = dt.date(2024, 2, 1)
        rows = [_row(booking_id="R%d" % r, booked_on="2024-01-18", arrival="2024-02-01",
                     rate=str(r)) for r in (100, 5400)]
        got = ratecheck.basket(self._bookings(rows), night, 21, 7, "STD")
        self.assertEqual(sorted(got), [100.0, 5400.0])

    def test_the_nightly_index_files_a_stay_under_every_night_it_slept(self):
        rows = [
            _row(booking_id="LONG", booked_on="2024-01-15", arrival="2024-02-01",
                 nights="3", rate="150"),
            _row(booking_id="ONE", booked_on="2024-01-25", arrival="2024-02-02",
                 nights="1", rate="130"),
            _row(booking_id="CXL", booked_on="2024-01-15", arrival="2024-02-01",
                 nights="3", rate="140", status="cancelled", status_date="2024-01-20"),
            _row(booking_id="COMP", segment="COMP", booked_on="2024-01-15",
                 arrival="2024-02-01", nights="3", rate="0"),
        ]
        bookings = self._bookings(rows)
        index = ratecheck.nightly_index(bookings)
        self.assertEqual({d: sorted(b.booking_id for b in bs) for d, bs in index.items()},
                         {dt.date(2024, 2, 1): ["LONG"],
                          dt.date(2024, 2, 2): ["LONG", "ONE"],
                          dt.date(2024, 2, 3): ["LONG"]})
        # And slicing it is the same question as reading the whole log, which
        # is the only reason table2 is allowed to slice it.
        for day in (dt.date(2024, 2, 1), dt.date(2024, 2, 2), dt.date(2024, 2, 3)):
            self.assertEqual(ratecheck.basket(index.get(day, ()), day, 21, 7, "STD"),
                             ratecheck.basket(bookings, day, 21, 7, "STD"))

    def test_the_quartiles_are_the_quartiles_and_in_that_order(self):
        # Ten values, so the three answers are three different numbers: p25
        # interpolates between 12 and 14, the median between 18 and 20.
        values = [10, 12, 14, 15, 18, 20, 21, 26, 30, 44]
        p25, mid, p75 = ratecheck.quartiles(values)
        self.assertAlmostEqual(p25, 14.25)
        self.assertAlmostEqual(mid, 19.0)
        self.assertAlmostEqual(p75, 24.75)
        self.assertEqual(ratecheck.quartiles([]), (None, None, None))

    def test_a_cell_is_the_median_of_the_gaps_not_the_gap_of_the_medians(self):
        """The two are 40 and 20 on these five nights, and only one of them is
        what the table claims to print."""
        c = ratecheck._cell([(200.0, 150.0), (180.0, 120.0), (160.0, 155.0),
                             (150.0, 140.0), (140.0, 100.0)])
        self.assertEqual(c["n"], 5)
        self.assertAlmostEqual(c["median_gap"], 40.0)         # median of 50,60,5,10,40
        self.assertAlmostEqual(c["median_published"], 160.0)
        self.assertAlmostEqual(c["median_realised"], 140.0)   # so the gap of medians is 20
        self.assertAlmostEqual(c["p25"], 10.0)
        self.assertAlmostEqual(c["p75"], 50.0)

    def test_an_empty_cell_says_nothing_rather_than_zero(self):
        self.assertEqual(ratecheck._cell([]),
                         {"n": 0, "median_gap": None, "p25": None, "p75": None,
                          "median_published": None, "median_realised": None})


# ---------------------------------------------------------------------------
# A hand-built table 2.  Four nights, four marks, every rate chosen so that no
# answer the table prints is a number a wrong one would also reach: the median
# of the gaps differs from the gap of the medians on every line, the two board
# codes disagree with each other and with the pooled row, the two seasons
# disagree, the quartiles sit either side of the median, and the three marks
# that score anything score a different number of nights each.
T2_D1 = dt.date(2024, 2, 28)        # February, "high" below
T2_D2 = dt.date(2024, 2, 29)
T2_D3 = dt.date(2024, 3, 1)         # March, "low"
T2_D4 = dt.date(2024, 3, 2)
T2_NIGHTS = (T2_D1, T2_D2, T2_D3, T2_D4)
T2_SEASONS = {m: ("high" if m == 2 else "low" if m == 3 else "shoulder")
              for m in range(1, 13)}

# night -> lead -> (segment, room_type, meal, rate).  Leads 14, 5 and 30 are
# each inside exactly one pre-registered window (14 in 21-7, 5 in 10-4, 30 in
# 40-21), so no row is counted at two marks and every basket is readable by
# eye.  Nothing sits at leads 45 to 75, which is what leaves the 60 mark with
# nothing to score.
T2_ROWS = {
    T2_D1: {14: [("WEB", "STD", "BB", 96), ("BOOKING", "STD", "BB", 104),
                 ("WEB", "STD", "BB", 140), ("BOOKING", "STD", "HB", 150),
                 ("WEB", "STD", "HB", 180), ("WEB", "SUP", "BB", 210)],
             5: [("WEB", "STD", "BB", 110), ("BOOKING", "STD", "BB", 120),
                 ("WEB", "STD", "HB", 140), ("WEB", "SUP", "BB", 300)],
            30: [("WEB", "STD", "BB", 118), ("BOOKING", "STD", "HB", 150)]},
    T2_D2: {14: [("WEB", "STD", "BB", 90), ("BOOKING", "STD", "BB", 118),
                 ("WEB", "STD", "BB", 126), ("BOOKING", "STD", "HB", 160),
                 ("WEB", "STD", "HB", 176), ("WEB", "SUP", "BB", 212)],
             5: [("WEB", "STD", "BB", 130), ("BOOKING", "STD", "BB", 144),
                 ("WEB", "STD", "HB", 170), ("WEB", "SUP", "BB", 302)],
            30: [("WEB", "STD", "BB", 124), ("BOOKING", "STD", "HB", 156)]},
    T2_D3: {14: [("WEB", "STD", "BB", 80), ("BOOKING", "STD", "BB", 100),
                 ("WEB", "STD", "BB", 112), ("BOOKING", "STD", "HB", 130),
                 ("WEB", "STD", "HB", 148), ("WEB", "SUP", "BB", 214)],
             5: [("WEB", "STD", "BB", 90), ("BOOKING", "STD", "BB", 106),
                 ("WEB", "STD", "HB", 128)],
            30: [("WEB", "STD", "BB", 108), ("BOOKING", "STD", "HB", 136)]},
    T2_D4: {14: [("WEB", "STD", "BB", 70), ("BOOKING", "STD", "BB", 86),
                 ("WEB", "STD", "BB", 94), ("BOOKING", "STD", "HB", 120),
                 ("WEB", "STD", "HB", 134), ("WEB", "SUP", "BB", 216)],
             5: [("WEB", "STD", "BB", 60), ("BOOKING", "STD", "BB", 78),
                 ("WEB", "STD", "HB", 110)],
            30: [("WEB", "STD", "BB", 102), ("BOOKING", "STD", "HB", 130)]},
}

# lead -> night -> (published rate, days between the solve and the mark).  A
# rate of None is a mark the engine published nothing at; an age of None is a
# mark with no solve recorded behind it.  The three marks that score carry
# three different rates for the same night, so a table reading its rate at the
# wrong lead reads a different number rather than the same one.
T2_PUBLISHED = {
    60: {T2_D1: (200.0, 0), T2_D2: (200.0, 0), T2_D3: (200.0, 0), T2_D4: (200.0, 0)},
    30: {T2_D1: (120.0, 14), T2_D2: (122.0, 9), T2_D3: (124.0, 0), T2_D4: (126.0, 2)},
    14: {T2_D1: (200.0, 0), T2_D2: (200.0, 3), T2_D3: (164.0, 7), T2_D4: (152.0, 5)},
    7: {T2_D1: (None, 4), T2_D2: (200.0, 1), T2_D3: (140.0, 6), T2_D4: (120.0, None)},
}
# The one record that carries a rate and is still not scored, so the two ways
# a night drops out are one each rather than both the same way.
T2_NOT_READY = (30, T2_D4)


def _t2_rows():
    rows = []
    n = 0
    for night in T2_NIGHTS:
        for lead, spec in sorted(T2_ROWS[night].items()):
            for segment, room, meal, rate in spec:
                n += 1
                rows.append(_row(booking_id="T%03d" % n, segment=segment, room_type=room,
                                 meal=meal, rate=str(rate), nights="1",
                                 arrival=night.isoformat(),
                                 booked_on=(night - dt.timedelta(days=lead)).isoformat()))
    rows += [
        # A three-night stay booked sixteen days before it arrives: seventeen
        # and eighteen days before its second and third nights, so all three
        # of its nights land in the 14 mark's window and none in any other.
        _row(booking_id="TLONG", segment="WEB", room_type="STD", meal="HB", rate="136",
             nights="3", arrival=T2_D2.isoformat(),
             booked_on=(T2_D2 - dt.timedelta(days=16)).isoformat()),
        # Comped rooms.  The first is entered a month out and is on the books
        # at every mark; the second is entered ten days out, so the 7 mark
        # sees it and the 14 and 30 marks, reading the house as it stood on
        # their own day, do not.
        _row(booking_id="TCOMP1", segment="COMP", room_type="STD", meal="BB", rate="0",
             nights="1", arrival=T2_D2.isoformat(),
             booked_on=(T2_D1 - dt.timedelta(days=30)).isoformat()),
        _row(booking_id="TCOMP2", segment="COMP", room_type="STD", meal="BB", rate="0",
             nights="1", arrival=T2_D3.isoformat(),
             booked_on=(T2_D3 - dt.timedelta(days=10)).isoformat()),
        # Rows that must not reach a basket: a contract rate, a group rate, a
        # cancellation, a day use, and a zero-rate row that is a real stay and
        # so does count towards which room type the house mostly sold.
        _row(booking_id="TCORP", segment="CORP", room_type="STD", meal="BB", rate="500",
             nights="1", arrival=T2_D1.isoformat(),
             booked_on=(T2_D1 - dt.timedelta(days=14)).isoformat()),
        _row(booking_id="TGROUP", segment="GROUP", room_type="STD", meal="BB", rate="400",
             nights="1", arrival=T2_D1.isoformat(),
             booked_on=(T2_D1 - dt.timedelta(days=14)).isoformat()),
        _row(booking_id="TCXL", segment="WEB", room_type="STD", meal="BB", rate="999",
             nights="1", arrival=T2_D1.isoformat(), status="cancelled",
             booked_on=(T2_D1 - dt.timedelta(days=14)).isoformat(),
             status_date=(T2_D1 - dt.timedelta(days=10)).isoformat()),
        _row(booking_id="TDAY", segment="WEB", room_type="STD", meal="BB", rate="140",
             nights="0", arrival=T2_D1.isoformat(),
             booked_on=(T2_D1 - dt.timedelta(days=14)).isoformat()),
        _row(booking_id="TFREE", segment="WEB", room_type="STD", meal="BB", rate="0",
             nights="1", arrival=T2_D1.isoformat(),
             booked_on=(T2_D1 - dt.timedelta(days=14)).isoformat()),
        # A suite at a lead inside no window at all, with no board code on it:
        # it moves the room-type share and nothing else, and it is what the
        # list of board codes has to leave out.
        _row(booking_id="TSUP", segment="WEB", room_type="SUP", meal="", rate="250",
             nights="1", arrival=T2_D1.isoformat(),
             booked_on=(T2_D1 - dt.timedelta(days=100)).isoformat()),
    ]
    return rows


def _t2_records():
    records = {}
    for lead, per_night in T2_PUBLISHED.items():
        for night, (rate, age) in per_night.items():
            asof = night - dt.timedelta(days=lead)
            records[(night, lead)] = pilot.WalkRecord(
                stay_date=night, lead=lead, asof=asof, otb=0,
                published_rate=rate,
                solved_on=(None if age is None else asof - dt.timedelta(days=age)),
                engine_ready=((lead, night) != T2_NOT_READY))
    return records


class Table2(unittest.TestCase):
    """Table 2 over a hand-built walk, against numbers worked out from the
    fixture before the code was run."""

    @classmethod
    def setUpClass(cls):
        cls.res = ingest.load(_csv(_t2_rows()), FIXTURE_HOTEL, seed=1)
        cls.table = ratecheck.table2(
            _bare_walk(None, _t2_records()), cls.res.bookings, cls.res.nonrev,
            cls.res.hotel, "H2", T2_SEASONS, T2_D1, T2_D4)

    @classmethod
    def tearDownClass(cls):
        _reset_config()

    def cell(self, mark, *path):
        block = self.table["cells"][mark]
        for key in path:
            block = block[key]
        return block

    def assertCell(self, mark, path, n, gap, p25, p75, published, realised):
        got = self.cell(mark, *path)
        where = "mark %s %s" % (mark, "/".join(path))
        self.assertEqual(got["n"], n, where)
        for name, want in (("median_gap", gap), ("p25", p25), ("p75", p75),
                           ("median_published", published), ("median_realised", realised)):
            self.assertAlmostEqual(got[name], want, msg="%s %s" % (where, name))

    def test_the_marks_are_the_four_pre_registered_ones_deepest_first(self):
        self.assertEqual(self.table["marks"], ["60", "30", "14", "7"])
        self.assertEqual(self.table["windows"],
                         {"60": [75, 45], "30": [40, 21], "14": [21, 7], "7": [10, 4]})

    def test_the_top_room_type_is_the_one_the_priced_nights_slept_in(self):
        """Forty-four standard room nights against seven suite nights over the
        four scored nights: twenty at the 14 mark's lead, twelve at the 7
        mark's, eight at the 30 mark's, three from the long stay and one from
        the zero-rate row, against six suite nights in the baskets and the one
        suite at a lead no window reaches.  The comp, the contract, the group,
        the cancellation and the day use are all outside the population.
        """
        self.assertEqual(self.table["room_type"], "STD")
        self.assertAlmostEqual(self.table["room_type_share"], 44 / 51.0)
        self.assertEqual(self.table["meals"], ["BB", "HB"])

    def test_the_band_is_the_hotel_s_and_its_top_rung_is_under_its_ceiling(self):
        self.assertEqual(self.table["rate_floor"], 80.0)
        self.assertEqual(self.table["rate_ceiling"], 201.0)
        self.assertEqual(self.table["top_rung"], 200.0)

    def test_the_sixty_mark_has_a_rate_to_read_and_no_one_to_compare_it_with(self):
        """The 60 row is empty because nobody booked inside 75 to 45 days out,
        not because the engine published nothing then and not because the
        room type is wrong: the record carries 200.00 and the same nights
        have full baskets at shorter leads.
        """
        rec = _t2_records()[(T2_D2, 60)]
        self.assertEqual(rec.published_rate, 200.0)
        self.assertTrue(rec.engine_ready)
        self.assertEqual(ratecheck.basket(self.res.bookings, T2_D2, 75, 45, "STD"), [])
        self.assertNotEqual(ratecheck.basket(self.res.bookings, T2_D2, 75, 4, "STD"), [])
        self.assertCell("60", ("all",), 0, None, None, None, None, None)
        self.assertEqual(self.table["cells"]["60"]["by_meal"], {})
        self.assertEqual(self.table["cells"]["60"]["by_season"], {})
        self.assertEqual(self.table["pinned"]["60"],
                         {"n": 0, "n_pinned": 0, "share": None})
        self.assertEqual(self.table["solve_age_days"]["60"],
                         {"median": None, "max": None, "n": 0})

    def test_the_fourteen_mark_is_the_median_of_four_nights_of_gaps(self):
        """Baskets 140, 131, 121 and 107 against rates 200, 200, 164 and 152:
        gaps of 60, 69, 43 and 45.  The median of those is 52.5 and the gap
        between the median rate, 182, and the median basket, 126, is 56.
        """
        self.assertCell("14", ("all",), 4, 52.5, 44.5, 62.25, 182.0, 126.0)

    def test_the_fourteen_mark_splits_by_board_into_two_different_answers(self):
        """Bed and breakfast baskets are 104, 118, 100 and 86; half board
        ones are 165, 160, 136 and 134.  Neither is the pooled 52.5, and the
        long stay's half-board rate is what moves three of the four.
        """
        self.assertCell("14", ("by_meal", "BB"), 4, 74.0, 65.5, 85.5, 182.0, 102.0)
        self.assertCell("14", ("by_meal", "HB"), 4, 31.5, 25.5, 36.25, 182.0, 148.0)
        self.assertEqual(sorted(self.table["cells"]["14"]["by_meal"]), ["BB", "HB"])

    def test_the_fourteen_mark_splits_by_the_season_it_was_handed(self):
        self.assertCell("14", ("by_season", "high"), 2, 64.5, 62.25, 66.75, 200.0, 135.5)
        self.assertCell("14", ("by_season", "low"), 2, 44.0, 43.5, 44.5, 158.0, 114.0)

    def test_the_fourteen_mark_keeps_the_comped_night_apart(self):
        """One comp room was on the books a month out, so it is there at this
        mark; the other was entered ten days out and is not."""
        self.assertCell("14", ("nonrev_nights",), 1, 69.0, 69.0, 69.0, 200.0, 131.0)
        self.assertCell("14", ("clean_nights",), 3, 45.0, 44.0, 52.5, 164.0, 121.0)

    def test_the_seven_mark_drops_the_night_the_engine_published_nothing_for(self):
        """The first night has a basket at this lead and no rate, so it is
        the rate that drops it and not the basket."""
        self.assertNotEqual(ratecheck.basket(self.res.bookings, T2_D1, 10, 4, "STD"), [])
        self.assertIsNone(_t2_records()[(T2_D1, 7)].published_rate)
        self.assertCell("7", ("all",), 3, 42.0, 38.0, 49.0, 140.0, 106.0)
        self.assertCell("7", ("by_meal", "BB"), 3, 51.0, 46.5, 57.0, 140.0, 98.0)
        self.assertCell("7", ("by_meal", "HB"), 3, 12.0, 11.0, 21.0, 140.0, 128.0)

    def test_the_seven_mark_sees_the_comp_room_the_longer_marks_could_not(self):
        """The second comp room is entered ten days out.  At this mark the
        house is read seven days out and it is there; at the 14 and 30 marks
        it is not, and the same night sits on the other side of the line.
        """
        self.assertCell("7", ("nonrev_nights",), 2, 45.0, 39.5, 50.5, 170.0, 125.0)
        self.assertCell("7", ("clean_nights",), 1, 42.0, 42.0, 42.0, 120.0, 78.0)
        self.assertCell("7", ("by_season", "high"), 1, 56.0, 56.0, 56.0, 200.0, 144.0)
        self.assertCell("7", ("by_season", "low"), 2, 38.0, 36.0, 40.0, 130.0, 92.0)

    def test_the_thirty_mark_drops_the_night_the_engine_was_not_ready_for(self):
        """And its gaps are negative: the engine is under the market here, in
        the one direction a table that only ever prints overshoots would
        never show.
        """
        self.assertEqual(_t2_records()[(T2_D4, 30)].published_rate, 126.0)
        self.assertFalse(_t2_records()[(T2_D4, 30)].engine_ready)
        self.assertNotEqual(ratecheck.basket(self.res.bookings, T2_D4, 40, 21, "STD"), [])
        self.assertCell("30", ("all",), 3, -14.0, -16.0, -6.0, 122.0, 134.0)
        self.assertCell("30", ("by_meal", "BB"), 3, 2.0, 0.0, 9.0, 122.0, 118.0)
        self.assertCell("30", ("by_meal", "HB"), 3, -30.0, -32.0, -21.0, 122.0, 150.0)
        self.assertCell("30", ("by_season", "high"), 2, -16.0, -17.0, -15.0, 121.0, 137.0)
        self.assertCell("30", ("by_season", "low"), 1, 2.0, 2.0, 2.0, 124.0, 122.0)

    def test_the_pinned_share_counts_the_nights_sitting_on_the_top_rung(self):
        """Two of the four nights at the 14 mark are published at 200.00, one
        of the three at the 7 mark is, none of the three at the 30 mark is,
        and the 60 mark scores nothing, so the share is a different number on
        every line and the total is three nights of ten.
        """
        self.assertEqual(self.table["pinned"]["14"], {"n": 4, "n_pinned": 2, "share": 0.5})
        self.assertEqual(self.table["pinned"]["7"]["n"], 3)
        self.assertEqual(self.table["pinned"]["7"]["n_pinned"], 1)
        self.assertAlmostEqual(self.table["pinned"]["7"]["share"], 1 / 3.0)
        self.assertEqual(self.table["pinned"]["30"], {"n": 3, "n_pinned": 0, "share": 0.0})
        self.assertEqual(self.table["pinned_overall"]["n"], 10)
        self.assertEqual(self.table["pinned_overall"]["n_pinned"], 3)
        self.assertAlmostEqual(self.table["pinned_overall"]["share"], 0.3)

    def test_the_age_of_the_solve_behind_each_mark_is_published(self):
        """The 7 mark scores three nights and only two of them have a solve
        recorded, so the count of ages is not the count of nights."""
        self.assertEqual(self.table["solve_age_days"]["30"], {"median": 9, "max": 14, "n": 3})
        self.assertEqual(self.table["solve_age_days"]["14"], {"median": 4.0, "max": 7, "n": 4})
        self.assertEqual(self.table["solve_age_days"]["7"], {"median": 3.5, "max": 6, "n": 2})

    def test_the_band_note_carries_the_measured_count_and_the_verdict(self):
        note = self.table["band_note"]
        self.assertIn("top rung is 200.00 against a ceiling of 201.00", note)
        self.assertIn("3 of 10 scored nights are pinned on the top rung", note)
        self.assertIn("artefact of a frozen band", note)
        self.assertEqual(self.table["notes"][1], note)

    def test_the_notes_refuse_the_revenue_reading(self):
        text = " ".join(self.table["notes"])
        self.assertIn("not evidence of revenue", text)
        self.assertIn("net rate", text)
        self.assertIn("compares two different products", text)
        self.assertIn("re-solves on a cadence", text)

    def test_only_a_hotel_that_is_not_h1_is_told_why_it_has_a_sixty_row(self):
        self.assertEqual(self.table["notes"][-1], ratecheck.MARK60_NOTE)
        h1 = ratecheck.table2(_bare_walk(None, _t2_records()), self.res.bookings,
                              self.res.nonrev, self.res.hotel, "h1", T2_SEASONS,
                              T2_D1, T2_D4)
        self.assertNotIn(ratecheck.MARK60_NOTE, h1["notes"])
        self.assertEqual(len(h1["notes"]), 5)
        self.assertEqual(len(self.table["notes"]), 6)
        # Everything except the note is the same table, so the label is a
        # label and not a second way of scoring the hotel.
        self.assertEqual({k: v for k, v in h1.items() if k != "notes"},
                         {k: v for k, v in self.table.items() if k != "notes"})


# --------------------------------------------------------------------- table 3


def _feb(day):
    return dt.date(2024, 2, day)


def _a_rows(n, booked_on="2024-01-01", arrival="2024-02-01"):
    """`n` single nights on one date, one room each, entered on one day."""
    return [_row(booking_id="A%02d" % i, booked_on=booked_on, arrival=arrival, nights="1")
            for i in range(n)]


def _a_ids(n):
    return set("A%02d" % i for i in range(n))


class CutRules(unittest.TestCase):
    """A 20-room fixture hotel cut to 12 rooms, so the mark is 10.8.

    Every count below is read off the rows by hand before the code runs, and
    every fixture is built so the rule under test gives a different answer from
    the rule beside it: a cut that refused nothing, or a mark that never bound,
    would pass an assertion about rooms and prove nothing.
    """

    CAP = 12
    LAST = dt.date(2024, 2, 28)

    def tearDown(self):
        _reset_config()

    def _load(self, rows):
        return ingest.load(_csv(rows), FIXTURE_HOTEL, seed=1)

    def _cut(self, rows, rule, cap=None):
        """Note the mark is never passed: every test that comes through here
        exercises the module's own default, so moving MARK moves these
        answers.  The one test that hands a share in calls `cut_history`
        itself."""
        res = self._load(rows)
        kept, cut = holdout.cut_history(res.bookings, self.CAP if cap is None else cap,
                                        rule, self.LAST)
        return res, set(b.booking_id for b in kept), cut

    # ------------------------------------------------------------- the names

    def test_an_unknown_rule_is_named_and_the_three_real_ones_listed(self):
        res = self._load(_a_rows(1))
        with self.assertRaises(pilot.PilotError) as ctx:
            holdout.cut_history(res.bookings, self.CAP, "close_expensive_first", self.LAST)
        self.assertIn("close_expensive_first", str(ctx.exception))
        for rule in holdout.RULES:
            self.assertIn(rule, str(ctx.exception))

    def test_every_rule_has_a_name_and_no_name_is_without_a_rule(self):
        self.assertEqual(holdout.RULES,
                         ("sell_until_full", "close_cheap_first", "close_cheap_first_ota"))
        self.assertEqual(sorted(holdout.RULE_NAMES), sorted(holdout.RULES))

    def test_the_cheap_branches_are_the_converter_s_offline_ta_to_branches(self):
        """The origin has to be read off the branch code, because two of the
        three map to CORP and are indistinguishable from a corporate account
        once mapped.  Dropping one of them silently sells a tour operator
        allotment straight through the mark.

        This is the only guard on OFFLINE_TO_GROUP's membership.  That branch
        maps to the GROUP target, which closes at the mark anyway, so dropping
        it changes no answer this dataset can produce; it would change them at
        a hotel whose map sends its tour operator blocks somewhere else."""
        from tools import convert_antonio as CA
        self.assertEqual(set(holdout.CHEAP_BRANCHES),
                         set(b for b in CA.ALL_BRANCHES if b.startswith("OFFLINE_TO")))
        self.assertEqual(len(holdout.CHEAP_BRANCHES), 3)

    def test_the_note_says_which_part_of_the_rule_never_fires_here(self):
        """Section 5's rule closes promotional and non-refundable rate codes at
        the same mark.  Neither hotel's export carries a rate code on a single
        row, so that part of the rule is inert and the table says so rather
        than letting a reader assume it ran."""
        note = holdout.NO_RATE_CODES_NOTE
        self.assertIn("Promotional", note)
        self.assertIn("non-refundable", note)
        self.assertIn("no rate codes at all", note)
        for name in ("h1", "h2"):
            path = os.path.join(ROOT, "data", "antonio", "%s-bookings.csv" % name)
            with open(path, newline="", encoding="utf-8-sig") as fh:
                coded = sum(1 for r in csv.DictReader(fh) if (r["rate_code"] or "").strip())
            self.assertEqual(coded, 0, name)

    # -------------------------------------------------------- sell until full

    def test_sell_until_full_refuses_the_row_that_would_pass_the_cap(self):
        """Fourteen single nights on 1 February entered one a day into twelve
        rooms: the first twelve are taken in the order they arrived and the
        last two are refused."""
        rows = [_row(booking_id="A%02d" % i, booked_on="2024-01-%02d" % (i + 1),
                     arrival="2024-02-01", nights="1") for i in range(14)]
        _res, kept, cut = self._cut(rows, "sell_until_full")
        self.assertEqual(kept, _a_ids(12))
        self.assertEqual(cut, {_feb(1): 2})

    def test_a_row_losing_one_night_loses_all_of_them(self):
        """Twelve rooms are gone on the 2nd only.  The three-night stay that
        wants the 1st, the 2nd and the 3rd is refused for all three, because a
        guest turned away for the middle night does not take the ends."""
        rows = _a_rows(12, arrival="2024-02-02")
        rows += [_row(booking_id="SPAN", booked_on="2024-01-10",
                      arrival="2024-02-01", nights="3")]
        _res, kept, cut = self._cut(rows, "sell_until_full")
        self.assertEqual(kept, _a_ids(12))
        self.assertEqual(cut, {_feb(1): 1, _feb(2): 1, _feb(3): 1})

    # --------------------------------------------------------- cancellations

    def test_a_cancellation_of_an_accepted_row_frees_its_room(self):
        """Eleven rooms, then a twelfth that cancels on 20 January.  The row
        entered on the 25th finds the room back."""
        rows = _a_rows(11)
        rows += [_row(booking_id="C1", booked_on="2024-01-02", arrival="2024-02-01",
                      nights="1", status="cancelled", status_date="2024-01-20"),
                 _row(booking_id="LATE", booked_on="2024-01-25", arrival="2024-02-01",
                      nights="1")]
        _res, kept, cut = self._cut(rows, "sell_until_full")
        self.assertEqual(kept, _a_ids(11) | {"C1", "LATE"})
        self.assertEqual(cut, {})

    def test_a_room_given_back_this_morning_can_be_sold_this_afternoon(self):
        """Eleven rooms, then a twelfth, C1, that cancels on 10 January, and
        SAMEDAY entered on the 10th for the same night.  The day's
        cancellations are applied before its bookings, so SAMEDAY finds C1's
        room and the night loses nothing.  Given back only at the end of the
        10th, the room is still held when SAMEDAY asks and SAMEDAY is refused.
        The test above cannot tell the two apart, because its cancellation and
        its next booking fall on different days."""
        rows = _a_rows(11)
        rows += [_row(booking_id="C1", booked_on="2024-01-02", arrival="2024-02-01",
                      nights="1", status="cancelled", status_date="2024-01-10"),
                 _row(booking_id="SAMEDAY", booked_on="2024-01-10", arrival="2024-02-01",
                      nights="1")]
        _res, kept, cut = self._cut(rows, "sell_until_full")
        self.assertEqual(kept, _a_ids(11) | {"C1", "SAMEDAY"},
                         "the room C1 gave back on the 10th was not for sale on the 10th")
        self.assertEqual(cut, {})

    def test_a_refused_row_gives_nothing_back_when_its_cancel_date_arrives(self):
        """The cap was already full when C1 was entered, so C1 never held a
        room and its cancellation on 20 January frees none.  LATE is refused
        too, and the night lost two rooms rather than one."""
        rows = _a_rows(12)
        rows += [_row(booking_id="C1", booked_on="2024-01-01", arrival="2024-02-01",
                      nights="1", status="cancelled", status_date="2024-01-20"),
                 _row(booking_id="LATE", booked_on="2024-01-25", arrival="2024-02-01",
                      nights="1")]
        _res, kept, cut = self._cut(rows, "sell_until_full")
        self.assertEqual(kept, _a_ids(12))
        self.assertEqual(cut, {_feb(1): 2})

    def test_a_row_cancelled_on_the_day_it_was_booked_gives_its_room_back(self):
        """1,263 of H1's 10,831 dated cancellations fall on their own booking
        day.  Held for the rest of that day, where `ingest.replay` also holds
        them, and given back at the end of it: a room taken in January that is
        never given back would refuse every later row for nothing."""
        rows = _a_rows(11)
        rows += [_row(booking_id="SAME", booked_on="2024-01-02", arrival="2024-02-01",
                      nights="1", status="cancelled", status_date="2024-01-02"),
                 _row(booking_id="LATE", booked_on="2024-01-03", arrival="2024-02-01",
                      nights="1")]
        _res, kept, cut = self._cut(rows, "sell_until_full")
        self.assertEqual(kept, _a_ids(11) | {"SAME", "LATE"})
        self.assertEqual(cut, {})

    # ---------------------------------------------------------------- groups

    def test_a_group_is_refused_whole_at_the_mark_and_accepted_under_the_cap(self):
        """Five rooms are gone and a block of seven is asked for.  Under the
        cap it fits exactly and is taken.  At the mark of 10.8 it does not fit,
        and all seven rooms go, not the two that crossed the mark."""
        rows = _a_rows(5)
        rows += [_row(booking_id="G%d" % i, segment="GROUP", company="ACME",
                      booked_on="2024-01-02", arrival="2024-02-01", nights="1")
                 for i in range(7)]
        group = set("G%d" % i for i in range(7))
        _res, kept_full, cut_full = self._cut(rows, "sell_until_full")
        self.assertEqual(kept_full, _a_ids(5) | group)
        self.assertEqual(cut_full, {})
        _res, kept_cheap, cut_cheap = self._cut(rows, "close_cheap_first")
        self.assertEqual(kept_cheap, _a_ids(5))
        self.assertEqual(cut_cheap, {_feb(1): 7})

    def test_two_companies_on_the_same_day_are_two_decisions(self):
        """Five rooms gone, then three for ACME and three for BETA.  ACME fits
        under the mark and BETA does not.  One block of six would refuse both;
        six separate rooms would take ACME's three and two of BETA's."""
        rows = _a_rows(5)
        rows += [_row(booking_id="ACME%d" % i, segment="GROUP", company="ACME",
                      booked_on="2024-01-02", arrival="2024-02-01", nights="1")
                 for i in range(3)]
        rows += [_row(booking_id="BETA%d" % i, segment="GROUP", company="BETA",
                      booked_on="2024-01-02", arrival="2024-02-01", nights="1")
                 for i in range(3)]
        _res, kept, cut = self._cut(rows, "close_cheap_first")
        self.assertEqual(kept, _a_ids(5) | set("ACME%d" % i for i in range(3)))
        self.assertEqual(cut, {_feb(1): 3})

    def test_group_rows_with_no_company_are_decided_one_room_at_a_time(self):
        """Five rooms gone, then nine group rows with no company, all entered
        on 2 January for one night on the 1st.  Nothing in them says they are
        one booking, so each is its own decision: the first seven fill the cap
        of twelve and the last two are refused.  Merged on the date they share
        into one block of nine, they would be fourteen rooms against twelve
        and all nine would go."""
        rows = _a_rows(5)
        rows += [_row(booking_id="G%d" % i, segment="GROUP", company="",
                      booked_on="2024-01-02", arrival="2024-02-01", nights="1")
                 for i in range(9)]
        _res, kept, cut = self._cut(rows, "sell_until_full")
        self.assertEqual(kept, _a_ids(5) | set("G%d" % i for i in range(7)),
                         "group rows with no company were decided as one block")
        self.assertEqual(cut, {_feb(1): 2})

    def test_one_company_s_rooms_for_two_lengths_of_stay_are_two_decisions(self):
        """Five rooms gone on the 1st, then ACME asks on one day for three
        one-night rooms and three two-night rooms, all arriving on the 1st.
        At the mark of 10.8 the one-night block fits at eight rooms and the
        two-night block, eleven on the 1st, does not.  One block of six would
        be eleven on the 1st and would keep none of them."""
        rows = _a_rows(5)
        rows += [_row(booking_id="ONE%d" % i, segment="GROUP", company="ACME",
                      booked_on="2024-01-02", arrival="2024-02-01", nights="1")
                 for i in range(3)]
        rows += [_row(booking_id="TWO%d" % i, segment="GROUP", company="ACME",
                      booked_on="2024-01-02", arrival="2024-02-01", nights="2")
                 for i in range(3)]
        _res, kept, cut = self._cut(rows, "close_cheap_first")
        self.assertEqual(kept, _a_ids(5) | set("ONE%d" % i for i in range(3)),
                         "one company's one-night and two-night rooms were one decision")
        self.assertEqual(cut, {_feb(1): 3, _feb(2): 3})

    def test_one_company_s_rooms_for_two_arrivals_are_two_decisions(self):
        """Five rooms gone on the 1st and eight on the 2nd, then ACME asks on
        one day for three one-night rooms on the 1st and three on the 2nd.  At
        the mark of 10.8 the block for the 1st fits at eight rooms and the
        block for the 2nd, eleven, does not.  One block of six would be
        eleven on the 1st and would keep none of them."""
        rows = _a_rows(5)
        rows += [_row(booking_id="B%02d" % i, arrival="2024-02-02", nights="1")
                 for i in range(8)]
        rows += [_row(booking_id="FIRST%d" % i, segment="GROUP", company="ACME",
                      booked_on="2024-01-02", arrival="2024-02-01", nights="1")
                 for i in range(3)]
        rows += [_row(booking_id="SECOND%d" % i, segment="GROUP", company="ACME",
                      booked_on="2024-01-02", arrival="2024-02-02", nights="1")
                 for i in range(3)]
        _res, kept, cut = self._cut(rows, "close_cheap_first")
        self.assertEqual(kept, _a_ids(5) | set("B%02d" % i for i in range(8))
                         | set("FIRST%d" % i for i in range(3)),
                         "one company's rooms for the 1st and the 2nd were one decision")
        self.assertEqual(cut, {_feb(2): 3})

    # --------------------------------------------------------- close cheap first

    def _ten_then(self, extra_branch):
        """Ten rooms committed, then a row from `extra_branch`, an OTA row and
        a direct row, all entered on the same later day and in that order."""
        rows = _a_rows(10)
        rows += [_row(booking_id="TO", segment=extra_branch, booked_on="2024-01-05",
                      arrival="2024-02-01", nights="1"),
                 _row(booking_id="OTA1", segment="BOOKING", booked_on="2024-01-05",
                      arrival="2024-02-01", nights="1"),
                 _row(booking_id="WEB1", booked_on="2024-01-05", arrival="2024-02-01",
                      nights="1")]
        return rows

    def test_close_cheap_first_shuts_every_offline_branch_at_the_mark(self):
        """At ten committed rooms the eleventh crosses the mark of 10.8 and
        stays under the cap of twelve, so the two rules disagree about every
        row in the queue: the tour operator row goes and the direct row that
        would otherwise have been the thirteenth survives in its place."""
        # Spelled out rather than read off CHEAP_BRANCHES, which is the
        # constant under test: a loop over it tests only what it still
        # contains and passes the moment a branch is dropped from it.
        for branch in ("OFFLINE_TO_CONTRACT", "OFFLINE_TO_TRANSIENT", "OFFLINE_TO_GROUP"):
            rows = self._ten_then(branch)
            _res, kept, cut = self._cut(rows, "sell_until_full")
            self.assertEqual(kept, _a_ids(10) | {"TO", "OTA1"}, branch)
            self.assertEqual(cut, {_feb(1): 1}, branch)
            _res, kept, cut = self._cut(rows, "close_cheap_first")
            self.assertEqual(kept, _a_ids(10) | {"OTA1", "WEB1"}, branch)
            self.assertEqual(cut, {_feb(1): 1}, branch)

    def test_the_secondary_form_shuts_the_ota_rows_too_and_the_primary_does_not(self):
        rows = _a_rows(10)
        rows += [_row(booking_id="OTA1", segment="BOOKING", booked_on="2024-01-05",
                      arrival="2024-02-01", nights="1"),
                 _row(booking_id="WEB1", booked_on="2024-01-05", arrival="2024-02-01",
                      nights="1")]
        _res, kept, cut = self._cut(rows, "close_cheap_first")
        self.assertEqual(kept, _a_ids(10) | {"OTA1", "WEB1"})
        self.assertEqual(cut, {})
        _res, kept, cut = self._cut(rows, "close_cheap_first_ota")
        self.assertEqual(kept, _a_ids(10) | {"WEB1"})
        self.assertEqual(cut, {_feb(1): 1})

    # ------------------------------------------------------------- the mark

    def test_the_mark_is_nine_tenths_of_the_cap_and_not_a_smaller_share(self):
        """Seventeen rooms committed into a cap of twenty.  The mark is exactly
        eighteen rooms, the tour operator row is the eighteenth, and a row at
        the mark has not passed it.  At 0.85 the mark would be seventeen and
        this row would be refused."""
        rows = _a_rows(17) + [_row(booking_id="TO", segment="OFFLINE_TO_TRANSIENT",
                                   booked_on="2024-01-05", arrival="2024-02-01", nights="1")]
        _res, kept, cut = self._cut(rows, "close_cheap_first", cap=20)
        self.assertEqual(kept, _a_ids(17) | {"TO"})
        self.assertEqual(cut, {})

    def test_the_mark_is_nine_tenths_of_the_cap_and_not_a_larger_share(self):
        """One room further on, the same row is the nineteenth and is refused,
        although the cap of twenty has two rooms left.  At 0.95 the mark would
        be nineteen and this row would be taken."""
        rows = _a_rows(18) + [_row(booking_id="TO", segment="OFFLINE_TO_TRANSIENT",
                                   booked_on="2024-01-05", arrival="2024-02-01", nights="1")]
        _res, kept, cut = self._cut(rows, "close_cheap_first", cap=20)
        self.assertEqual(kept, _a_ids(18))
        self.assertEqual(cut, {_feb(1): 1})

    def test_a_share_handed_in_moves_the_mark_and_the_default_does_not_stand_in_for_it(self):
        """Every other test here runs the default mark.  Ten rooms gone, the
        tour operator row is the eleventh and crosses 10.8; handed a share of
        one, the mark is the cap itself and the same row is taken.  Five rooms
        gone, the row is the sixth and sits under 10.8; handed a share of 0.4,
        the mark is 4.8 and the row is refused."""
        tour = [_row(booking_id="TO", segment="OFFLINE_TO_TRANSIENT", booked_on="2024-01-05",
                     arrival="2024-02-01", nights="1")]

        def cut(committed, **share):
            res = self._load(_a_rows(committed) + tour)
            kept, lost = holdout.cut_history(res.bookings, self.CAP, "close_cheap_first",
                                             self.LAST, **share)
            return set(b.booking_id for b in kept), lost

        self.assertEqual(cut(10), (_a_ids(10), {_feb(1): 1}))
        self.assertEqual(cut(10, mark_share=1.0), (_a_ids(10) | {"TO"}, {}),
                         "a share of one did not move the mark up to the cap")
        self.assertEqual(cut(5), (_a_ids(5) | {"TO"}, {}))
        self.assertEqual(cut(5, mark_share=0.4), (_a_ids(5), {_feb(1): 1}),
                         "a share of 0.4 did not move the mark down to 4.8 rooms")

    # ----------------------------------------------- rows outside the cap

    def test_a_comp_room_entered_after_the_cap_overshoots_and_is_not_cut(self):
        """A comp room is not for sale, so it evicts nobody and nothing evicts
        it: the night ends on thirteen physical rooms in a house cut to
        twelve."""
        rows = _a_rows(12)
        rows += [_row(booking_id="COMP1", segment="COMP", rate="0", booked_on="2024-01-28",
                      arrival="2024-02-01", nights="1")]
        res = self._load(rows)
        kept, cut = holdout.cut_history(res.bookings, self.CAP, "sell_until_full", self.LAST)
        self.assertEqual(set(b.booking_id for b in kept), _a_ids(12) | {"COMP1"})
        self.assertEqual(cut, {})
        self.assertEqual(ingest.physical_occupancy(kept)[_feb(1)], 13)

    def test_a_day_use_row_is_kept_and_never_counted_against_the_cap(self):
        """Zero nights never reach the ledger, so they cannot fill a room, and
        the thirteenth real room is still the one that is refused."""
        rows = _a_rows(12)
        rows += [_row(booking_id="DAY", booked_on="2024-01-28", arrival="2024-02-01",
                      nights="0"),
                 _row(booking_id="LATE", booked_on="2024-01-29", arrival="2024-02-01",
                      nights="1")]
        _res, kept, cut = self._cut(rows, "sell_until_full")
        self.assertEqual(kept, _a_ids(12) | {"DAY"})
        self.assertEqual(cut, {_feb(1): 1})

    # ---------------------------------------------------------- the arguments

    def test_a_generator_is_cut_exactly_as_the_list_it_yields(self):
        """`cut_history` reads its rows more than once and finds the refusals
        by identity.  A version that did not take a generator into a list
        first would empty it on the first pass and hand back no rows at all,
        and the cut beside that empty answer would still look right."""
        rows = [_row(booking_id="A%02d" % i, booked_on="2024-01-%02d" % (i + 1),
                     arrival="2024-02-01", nights="1") for i in range(14)]
        res = self._load(rows)
        kept, cut = holdout.cut_history(res.bookings, self.CAP, "sell_until_full", self.LAST)
        gen_kept, gen_cut = holdout.cut_history((b for b in res.bookings), self.CAP,
                                                "sell_until_full", self.LAST)
        self.assertEqual([b.booking_id for b in kept], ["A%02d" % i for i in range(12)])
        self.assertEqual(cut, {_feb(1): 2})
        self.assertEqual([id(b) for b in gen_kept], [id(b) for b in kept],
                         "a generator came back with different rows from its list")
        self.assertEqual(gen_cut, {_feb(1): 2})

    def test_a_row_booked_after_the_last_stay_night_is_still_decided(self):
        """The loop runs to the last booking day when that is later than
        `last_stay`.  A row entered on 1 March for a night in March is past
        the 28 February handed in; stopping the loop there would leave it
        undecided and drop it from `kept`, and the caller would lose a row the
        cap never refused."""
        rows = _a_rows(3) + [_row(booking_id="MARCH", booked_on="2024-03-01",
                                  arrival="2024-03-05", nights="1")]
        _res, kept, cut = self._cut(rows, "sell_until_full")
        self.assertEqual(kept, _a_ids(3) | {"MARCH"},
                         "a row booked after last_stay was dropped without being refused")
        self.assertEqual(cut, {})


class NetCut(unittest.TestCase):
    """`net_cut` against the lead-0 snapshot it says it is on the scale of.

    The same twelve-room cap as above.  Twelve single nights on 1 February fill
    it, and every row after them is refused: a two-night stay, a one-night
    stay, a cancelled three-night stay, a one-night no-show, a three-night
    no-show arriving on the 1st, and a three-night no-show that arrived on 30
    January, before the window opens.

    The two multi-night no-shows are the point of the fixture.  The replay
    snapshots a night and then settles it, and settlement releases a no-show
    from its arrival night onward, so the one arriving on the 1st stands in the
    lead-0 snapshot of the 1st and of no later night.  The one arriving on the
    30th is never settled, because the replay settles only nights from the
    window's first, so it is never released and stands in the snapshot of the
    1st, the only one of its three nights the window holds.

    Each wrong version of the rule fails here for its own reason.  Counting
    every night of a no-show reports the 1st's arrival on the 2nd and 3rd too.
    Counting only a no-show's arrival night loses the early one from the 1st.
    Not clipping to the window reports the 30th and the 31st, nights no
    snapshot exists for.
    """

    CAP = 12
    FIRST = dt.date(2024, 2, 1)
    LAST = dt.date(2024, 2, 28)

    def setUp(self):
        rows = _a_rows(12)
        rows += [_row(booking_id="STAY2", booked_on="2024-01-05", arrival="2024-02-01",
                      nights="2"),
                 _row(booking_id="STAY1", booked_on="2024-01-05", arrival="2024-02-01",
                      nights="1"),
                 _row(booking_id="CANC3", booked_on="2024-01-05", arrival="2024-02-01",
                      nights="3", status="cancelled", status_date="2024-01-20"),
                 _row(booking_id="NOSHOW", booked_on="2024-01-05", arrival="2024-02-01",
                      nights="1", status="no_show"),
                 _row(booking_id="NOSHOW3", booked_on="2024-01-05", arrival="2024-02-01",
                      nights="3", status="no_show"),
                 _row(booking_id="EARLYNS", booked_on="2024-01-05", arrival="2024-01-30",
                      nights="3", status="no_show")]
        self.res = ingest.load(_csv(rows), FIXTURE_HOTEL, seed=1)
        self.kept, self.cut = holdout.cut_history(self.res.bookings, self.CAP,
                                                  "sell_until_full", self.LAST)

    def tearDown(self):
        _reset_config()

    def test_the_fixture_refuses_all_six(self):
        """Every row after the twelve is refused, on every night it covers,
        because each of them needs the 1st and the 1st is full."""
        self.assertEqual(set(b.booking_id for b in self.kept), _a_ids(12))
        self.assertEqual(self.cut, {dt.date(2024, 1, 30): 1, dt.date(2024, 1, 31): 1,
                                    _feb(1): 6, _feb(2): 3, _feb(3): 2})

    def test_the_net_cut_counts_each_refusal_where_the_snapshot_would_have(self):
        """The 1st: the two stays, the one-night no-show, the three-night
        no-show on its arrival night, and the early no-show that was never
        released, five.  The 2nd: the two-night stay, one.  The 3rd: nothing,
        because the three-night no-show was released on the 1st.  The cancelled
        stay is on no night at all, and nothing is reported before the 1st."""
        self.assertEqual(holdout.net_cut(self.res.bookings, self.kept, self.FIRST, self.LAST),
                         {_feb(1): 5, _feb(2): 1},
                         "a refused no-show or a refused cancellation was counted on the wrong nights")

    def test_the_net_cut_is_the_gap_between_the_two_lead_0_snapshots(self):
        """The whole history replayed into the real hotel, less the cut
        history replayed into the capped one, night by night at lead 0.  This
        is the oracle: it reads the replay itself rather than a restatement of
        the rule, so it holds `net_cut` to whatever the replay actually does."""
        full = ingest.replay(self.res.bookings, self.res.hotel, self.FIRST, self.LAST,
                             ingest.Report())
        capped, _hotel = holdout.capped_ledger(self.kept, self.res.hotel, self.CAP,
                                               self.FIRST, self.LAST)
        self.assertEqual(full.snapshots[self.FIRST][0], 17)
        self.assertEqual(capped.snapshots[self.FIRST][0], 12)
        gap = {}
        d = self.FIRST
        while d <= self.LAST:
            n = full.snapshots[d][0] - capped.snapshots[d][0]
            if n:
                gap[d] = n
            d += dt.timedelta(days=1)
        self.assertEqual(holdout.net_cut(self.res.bookings, self.kept, self.FIRST, self.LAST), gap)


class CappedReplay(unittest.TestCase):
    """Both halves of the cap.  Either one alone leaves table 3 a null."""

    CAP = 12
    NIGHT = dt.date(2024, 2, 1)
    LAST = dt.date(2024, 2, 28)

    def setUp(self):
        rows = [_row(booking_id="A%02d" % i, booked_on="2024-01-%02d" % (i + 1),
                     arrival="2024-02-01", nights="1") for i in range(14)]
        self.res = ingest.load(_csv(rows), FIXTURE_HOTEL, seed=1)
        self.kept, self.cut = holdout.cut_history(self.res.bookings, self.CAP,
                                                  "sell_until_full", self.LAST)

    def tearDown(self):
        _reset_config()

    def _capped(self, bookings, rep=None):
        return holdout.capped_ledger(bookings, self.res.hotel, self.CAP,
                                     self.NIGHT, self.LAST, rep)

    def test_the_cut_history_is_replayed_into_a_hotel_of_the_cap_s_size(self):
        led, capped = self._capped(self.kept)
        self.assertEqual(capped.rooms, self.CAP)
        self.assertEqual(self.res.hotel.rooms, 20)      # the real hotel is untouched
        self.assertEqual(capped.base_rate, self.res.hotel.base_rate)
        self.assertIs(led.hotel, capped)
        settled = led.settled[self.NIGHT]
        self.assertEqual(settled["rooms_sold"], 12)
        self.assertEqual(settled["occupancy"], 1.0)     # 0.6 in a house of twenty
        self.assertEqual(settled["walked"], 0)

    def test_a_capped_night_is_censored_only_because_the_hotel_is_the_cap(self):
        """`unconstrain.class_demand` marks a night censored when its lead-0
        snapshot reaches `rooms * sellout_threshold` of the Hotel it is handed,
        or when the ledger logged a denial for it, and a replay logs none.
        Twelve rooms on the books at lead 0 is past 0.97 of twelve and short of
        0.97 of twenty, so the one cut history is censored in the hotel of the
        cap's size and uncensored in the real one, where table 3 would have
        nothing to score.  The flag is read off `class_demand` itself, not
        restated here."""
        from pace.calendar import EventCalendar
        from pace.otb import GLOBAL_KEY
        from pace.unconstrain import class_demand

        class AtTheReferenceRate:
            """Acceptance of one: the price restatement moves the observed
            value and never the censoring flag, which is all this reads."""
            def accept(self, code, rate, ref):
                return 1.0

        led, capped = self._capped(self.kept)
        full = ingest.replay(self.kept, self.res.hotel, self.NIGHT, self.LAST,
                             ingest.Report())
        for ledger in (led, full):
            self.assertEqual(ledger.snapshots[self.NIGHT][0], 12)
            self.assertEqual(ledger.observable_denials(self.NIGHT), 0)
        in_the_cap = class_demand(led, [self.NIGHT], capped, EventCalendar(),
                                  AtTheReferenceRate())
        in_the_real_hotel = class_demand(full, [self.NIGHT], self.res.hotel, EventCalendar(),
                                         AtTheReferenceRate())
        self.assertEqual(in_the_cap.censored[GLOBAL_KEY], 1,
                         "the unconstrainer did not see a night at the cap as censored")
        self.assertEqual(in_the_real_hotel.censored[GLOBAL_KEY], 0)

    def test_without_the_cut_the_settlement_walks_the_excess_instead_of_refusing_it(self):
        """Lowering the room count imposes no cap on its own: the replay books
        every row and the settlement walks two guests off the actuals after the
        fact, which is an edit to the hotel's own history, not a refusal."""
        self.assertEqual(len(self.kept), 12)
        self.assertEqual(self.cut, {self.NIGHT: 2})
        rep = ingest.Report()
        led, _capped = self._capped(self.res.bookings, rep)
        self.assertEqual(led.settled[self.NIGHT]["walked"], 2)
        self.assertEqual(rep.warnings["over_capacity_nights"], 1)
        self.assertEqual(rep.warnings["rooms_walked_off_the_actuals"], 2)
        cut_rep = ingest.Report()
        cut_led, _capped = self._capped(self.kept, cut_rep)
        self.assertEqual(cut_led.settled[self.NIGHT]["walked"], 0)
        self.assertEqual(cut_rep.warnings["over_capacity_nights"], 0)


class CleanNights(unittest.TestCase):
    """Twenty rooms, so a threshold of 0.90 is eighteen of them."""

    def tearDown(self):
        _reset_config()

    def _filler(self, days, skip=()):
        return [_row(booking_id="Q%02d-%02d" % (day, i), arrival="2024-02-%02d" % day,
                     nights="1")
                for day in days if day not in skip for i in range(2)]

    def _load(self, rows):
        return ingest.load(_csv(rows), FIXTURE_HOTEL, seed=1)

    def test_the_window_takes_the_neighbours_of_a_full_night_out_with_it(self):
        """The 10th reaches nineteen rooms.  With a window of three the 7th to
        the 13th all go, because a stay spanning the 10th is refused for every
        night it covers and none of those nights has a known answer either."""
        rows = [_row(booking_id="F%02d" % i, arrival="2024-02-10", nights="1")
                for i in range(19)]
        rows += self._filler(range(1, 21), skip=(10,))
        res = self._load(rows)
        clean = holdout.clean_nights(res.bookings, res.hotel, _feb(1), _feb(20),
                                     threshold=0.90, window=3)
        self.assertEqual(clean, [_feb(d) for d in list(range(1, 7)) + list(range(14, 21))])

    def test_a_night_full_only_thanks_to_comp_rooms_is_not_clean(self):
        """Sixteen sold and three comped is nineteen rooms in the house and
        sixteen in the ledger.  Read off the ledger the night looks quiet, and
        the guest turned away on it still went elsewhere."""
        rows = [_row(booking_id="S%02d" % i, arrival="2024-02-10", nights="1")
                for i in range(16)]
        rows += [_row(booking_id="C%d" % i, segment="COMP", rate="0",
                      arrival="2024-02-10", nights="1") for i in range(3)]
        rows += self._filler(range(1, 21), skip=(10,))
        res = self._load(rows)
        clean = holdout.clean_nights(res.bookings, res.hotel, _feb(1), _feb(20),
                                     threshold=0.90, window=0)
        self.assertEqual(clean, [_feb(d) for d in range(1, 21) if d != 10])

    def test_a_full_night_past_the_last_one_offered_still_takes_its_neighbours(self):
        """The 21st is full and is outside the window being offered.  A window
        read only against the nights on offer would hand back the 19th and the
        20th, and a three-night stay over the 21st covers both."""
        rows = [_row(booking_id="F%02d" % i, arrival="2024-02-21", nights="1")
                for i in range(19)]
        rows += self._filler(range(1, 22), skip=(21,))
        res = self._load(rows)
        clean = holdout.clean_nights(res.bookings, res.hotel, _feb(1), _feb(20),
                                     threshold=0.90, window=2)
        self.assertEqual(clean, [_feb(d) for d in range(1, 19)])

    def test_the_threshold_is_a_share_of_the_room_count_and_a_night_at_it_is_busy(self):
        """Eighteen of twenty rooms is exactly nine tenths, so the night is
        busy at 0.90 and quiet at 0.95, and the whole month turns over on
        which of the two is asked for."""
        rows = [_row(booking_id="F%02d" % i, arrival="2024-02-10", nights="1")
                for i in range(18)]
        rows += self._filler(range(1, 21), skip=(10,))
        res = self._load(rows)
        at_90 = holdout.clean_nights(res.bookings, res.hotel, _feb(1), _feb(20),
                                     threshold=0.90, window=0)
        at_95 = holdout.clean_nights(res.bookings, res.hotel, _feb(1), _feb(20),
                                     threshold=0.95, window=0)
        self.assertEqual(at_90, [_feb(d) for d in range(1, 21) if d != 10])
        self.assertEqual(at_95, [_feb(d) for d in range(1, 21)])


# ------------------------------------------------------------------ table 3

T3_FIRST = dt.date(2024, 1, 29)
T3_LAST = dt.date(2024, 2, 14)
T3_EARLY = "2024-01-31"
T3_LATE = "2024-02-01"


def _t3_singles(day, n, prefix, booked_on=T3_EARLY, **kw):
    return [_row(booking_id="%s%02d" % (prefix, i), booked_on=booked_on,
                 arrival="2024-02-%02d" % day, nights="1", **kw) for i in range(n)]


def _t3_rows():
    """Seventeen nights in a twenty-room hotel, cut to twelve rooms.

    Every row is booked on 31 January or 1 February.  The replay opens on the
    first booking day, so 29 and 30 January are settled with nothing in them
    and have no lead-0 snapshot at all, while 31 January has one of nought.

    Night by night, with the capped history's settled figure and lead-0
    snapshot, and 11.64 rooms the capped hotel's censoring line:

    - 2 Feb, eight singles and a two-night stay over the 2nd and 3rd booked a
      day later.  The 3rd is full by then, so the stay is refused whole: the
      2nd sold 9 and keeps 8.  Cut, and not censored: its estimate is its
      observation and its error is the one room the cut took.
    - 3 Feb, three corporate and twelve retail singles.  Twelve are kept,
      three corporate and nine retail, and the refused stay was on it too: 16
      sold, 12 kept, censored.
    - 4 Feb, twelve singles, one of them a no-show, then a single and a
      cancelled two-night stay over the 4th and 5th booked a day later and
      both refused.  The real night settles at 12.  The capped night has 12 in
      its lead-0 snapshot and 11 once the no-show has gone: censored by the
      snapshot, which is class_demand's flag, and not by the settled figure.
    - 5 Feb, six singles.  The cancelled stay was refused on it, so the gross
      cut touches it, and nothing it would have sold was lost.
    - 9 Feb, nineteen singles: over 0.90 of twenty, so it and its neighbours
      inside a window of one, the 8th and the 10th, are not clean.  The 10th
      is fifteen singles cut to twelve, the same shape as the 3rd, and is
      scored only if the window is dropped.
    - 12 Feb, twelve singles: exactly the cap, so nothing is refused and the
      capped history sells what the real one sold, and its lead-0 snapshot of
      twelve is over the censoring line.  Clean, censored, and not scored,
      because the cut took nothing from it: the one night that tells
      censored_clean from censored_scored.
    - every other night a handful of singles, never cut.
    """
    rows = []
    rows += _t3_singles(1, 5, "D01-")
    rows += _t3_singles(2, 8, "D02-")
    rows += _t3_singles(3, 3, "D03C", segment="CORP", rate="96")
    rows += _t3_singles(3, 12, "D03-")
    rows += _t3_singles(4, 1, "D04N", status="no_show")
    rows += _t3_singles(4, 11, "D04-")
    for day, n in ((5, 6), (6, 7), (7, 4), (8, 10), (9, 19), (10, 15), (11, 6),
                   (12, 12), (13, 9), (14, 3)):
        rows += _t3_singles(day, n, "D%02d-" % day)
    rows += [_row(booking_id="SPAN2", booked_on=T3_LATE, arrival="2024-02-02", nights="2"),
             _row(booking_id="LATE4", booked_on=T3_LATE, arrival="2024-02-04", nights="1"),
             _row(booking_id="CANC4", booked_on=T3_LATE, arrival="2024-02-04", nights="2",
                  status="cancelled", status_date="2024-02-02")]
    return rows


# The capped history as class_demand sees it, worked out by hand from the
# rows above: (rooms sold, censored) for every night with a lead-0 snapshot,
# in date order.  The 4th sold 11 and is censored, because its snapshot held
# 12; the 12th sold 12, at the cap with nothing refused, and is censored too.
# Every night is here, clean or not, because the unconstrainer is handed the
# whole cut history.
T3_SAMPLE = [(0.0, False), (5.0, False), (8.0, False), (12.0, True), (11.0, True),
             (6.0, False), (7.0, False), (4.0, False), (10.0, False), (12.0, True),
             (12.0, True), (6.0, False), (12.0, True), (9.0, False), (3.0, False)]
T3_SAMPLE_NIGHTS = [dt.date(2024, 1, 31)] + [_feb(d) for d in range(1, 15)]


class PerNightDemand(unittest.TestCase):
    def tearDown(self):
        _reset_config()

    def _capped(self, rows, cap=12):
        res = ingest.load(_csv(rows), FIXTURE_HOTEL, seed=1)
        kept, _cut = holdout.cut_history(res.bookings, cap, "sell_until_full",
                                         dt.date(2024, 4, 30))
        led, capped = holdout.capped_ledger(kept, res.hotel, cap,
                                            dt.date(2024, 2, 1), dt.date(2024, 4, 30),
                                            snapshot_horizon=0)
        return res, led, capped

    def _rows(self, rate="120"):
        """Sixty nights from Thursday 1 February: every Monday and Tuesday
        fourteen rooms, every other night six.  Cut to twelve, so eight
        Mondays and eight Tuesdays sit at the cap and are censored, and every
        other night is left open."""
        rows = []
        for day in range(60):
            d = dt.date(2024, 2, 1) + dt.timedelta(days=day)
            sold = 14 if day % 7 in (4, 5) else 6
            for i in range(sold):
                rows.append(_row(booking_id="R%03d%02d" % (day, i),
                                 booked_on=(d - dt.timedelta(days=20)).isoformat(),
                                 arrival=d.isoformat(), nights="1", rate=rate))
        return rows

    def test_an_uncensored_night_is_left_alone(self):
        _res, led, capped = self._capped(self._rows())
        nights = sorted(led.settled)
        est = holdout.per_night_demand(led, capped, nights)
        quiet = [d for d in nights if d.month in (2, 3) and led.settled[d]["rooms_sold"] == 6]
        self.assertEqual(len(quiet), 44)
        for d in quiet:
            self.assertEqual(est[d], 6.0)

    def test_a_censored_night_is_lifted_by_its_own_class_once_the_class_has_eight(self):
        """Each of the two full weekdays is a class of exactly eight nights,
        all at twelve and all censored, which is MIN_CLASS_OBS: the class is
        trusted and the house series is not consulted.  With no open night in
        the class, project_detruncate starts from mean 12 and deviation 1, so
        its first pass lifts every night to the mean of a normal above its own
        mean, 12 + sqrt(2/pi), and the deviation of eight equal values is 0,
        which holds it there.  Read off the house series instead, the 44 open
        sixes and April's thirty empty nights would pull it somewhere else."""
        _res, led, capped = self._capped(self._rows())
        nights = sorted(led.settled)
        est = holdout.per_night_demand(led, capped, nights)
        full = [d for d in nights if led.settled[d]["rooms_sold"] == 12]
        self.assertEqual([d.weekday() for d in full], [0, 1] * 8)
        for d in full:
            self.assertEqual(led.snapshots[d][0], 12)
            self.assertAlmostEqual(est[d], 12.0 + math.sqrt(2.0 / math.pi), places=9)

    def test_the_estimate_does_not_move_when_the_rates_move(self):
        """The price term is out. class_demand divides each segment by its
        acceptance at the rate charged, which is exactly what must not be
        scored here."""
        _r1, led_cheap, capped = self._capped(self._rows(rate="90"))
        _r2, led_dear, _capped2 = self._capped(self._rows(rate="180"))
        nights = sorted(led_cheap.settled)
        cheap = holdout.per_night_demand(led_cheap, capped, nights)
        dear = holdout.per_night_demand(led_dear, capped, nights)
        self.assertEqual(sorted(cheap), sorted(dear))
        self.assertEqual(len(cheap), len(nights))
        for d in nights:
            self.assertEqual(cheap[d], dear[d])

    def test_the_capped_replay_settles_and_snapshots_lead_0_the_same_without_the_rest(self):
        """The flag reads the lead-0 snapshot, so a thin replay has to agree
        with a full one on that as well as on the settled figures.  The shared
        synthetic history has cancellations on the booking day and later,
        no-shows and multi-night stays, and 18 of its nights settle below
        their lead-0 snapshot, so the snapshot is not the settled figure under
        another name.  The replay snapshots every day from the first booking
        day, 28 November, so there are lead-0 snapshots for 34 days before the
        window opens as well as for its 120 nights."""
        res = ingest.load(_csv(history_rows(FIRST_ARRIVAL, 120)), FIXTURE_HOTEL, seed=1)
        last = FIRST_ARRIVAL + dt.timedelta(days=119)
        kept, _ = holdout.cut_history(res.bookings, 12, "sell_until_full", last)
        thin, thin_hotel = holdout.capped_ledger(kept, res.hotel, 12, FIRST_ARRIVAL, last,
                                                 snapshot_horizon=0)
        thick, thick_hotel = holdout.capped_ledger(kept, res.hotel, 12, FIRST_ARRIVAL, last,
                                                   snapshot_horizon=res.hotel.max_lead)
        self.assertEqual((thin_hotel.max_lead, thick_hotel.max_lead), (0, 40))
        self.assertEqual({d: r["rooms_sold"] for d, r in thin.settled.items()},
                         {d: r["rooms_sold"] for d, r in thick.settled.items()})
        lead0 = dict((d, s[0]) for d, s in thin.snapshots.items() if 0 in s)
        self.assertEqual(lead0, dict((d, s[0]) for d, s in thick.snapshots.items() if 0 in s))
        self.assertEqual((min(lead0), len(lead0)), (dt.date(2023, 11, 28), 154))
        self.assertEqual(set(len(s) for s in thin.snapshots.values()), {1})
        self.assertEqual(sum(1 for d, r in thin.settled.items() if r["rooms_sold"] < lead0[d]),
                         18)

    def test_left_out_the_horizon_is_the_hotel_s_own_as_it_was_before_table_3(self):
        res = ingest.load(_csv(self._rows()), FIXTURE_HOTEL, seed=1)
        kept, _ = holdout.cut_history(res.bookings, 12, "sell_until_full", dt.date(2024, 4, 30))
        led, capped = holdout.capped_ledger(kept, res.hotel, 12, dt.date(2024, 2, 1),
                                            dt.date(2024, 4, 30))
        self.assertEqual(capped.max_lead, 40)
        self.assertEqual(sorted(led.snapshots[dt.date(2024, 3, 1)]), list(range(0, 41)))


class Table3Night(unittest.TestCase):
    """One combination scored by hand: threshold 0.90, cap 0.60, sell until
    full, window 1, over the seventeen nights of `_t3_rows`."""

    def setUp(self):
        self.res = ingest.load(_csv(_t3_rows()), FIXTURE_HOTEL, seed=1,
                               first_stay=T3_FIRST, last_stay=T3_LAST)
        self.kept, self.cut = holdout.cut_history(self.res.bookings, 12, "sell_until_full",
                                                  T3_LAST)
        self.led, self.capped = holdout.capped_ledger(self.kept, self.res.hotel, 12,
                                                      T3_FIRST, T3_LAST, snapshot_horizon=0)
        from pace.unconstrain import project_detruncate
        _mu, _sigma, imputed = project_detruncate(T3_SAMPLE)
        self.expected = dict(zip(T3_SAMPLE_NIGHTS, imputed))

    def tearDown(self):
        _reset_config()

    def _combo(self, window=1):
        return holdout.score_combo(self.res.bookings, self.res.hotel, self.res.ledger,
                                   T3_FIRST, T3_LAST, 0.90, 0.60, "sell_until_full", window)

    def test_the_fixture_is_the_one_described(self):
        self.assertEqual(self.capped.rooms, 12)
        self.assertEqual({d: self.res.ledger.settled[d]["rooms_sold"]
                          for d in (_feb(2), _feb(3), _feb(4), _feb(5), _feb(9), _feb(10),
                                    _feb(12))},
                         {_feb(2): 9, _feb(3): 16, _feb(4): 12, _feb(5): 6, _feb(9): 19,
                          _feb(10): 15, _feb(12): 12})
        self.assertEqual(self.led.settled[_feb(4)]["rooms_sold"], 11)
        self.assertEqual(self.led.snapshots[_feb(4)][0], 12)
        self.assertEqual((self.led.settled[_feb(12)]["rooms_sold"], self.led.snapshots[_feb(12)][0]),
                         (12, 12))
        self.assertEqual(self.led.seg_rooms[_feb(3)]["CORP"], 3)
        self.assertEqual(self.led.seg_rooms[_feb(3)]["RETAIL"], 9)
        self.assertNotIn(dt.date(2024, 1, 30), self.led.snapshots)
        self.assertEqual(self.led.snapshots[dt.date(2024, 1, 31)], {0: 0})
        self.assertEqual(self.cut, {_feb(2): 1, _feb(3): 4, _feb(4): 2, _feb(5): 1,
                                    _feb(9): 7, _feb(10): 3})

    def test_the_observation_is_what_sold_and_the_flag_is_the_lead_0_snapshot_s(self):
        nights = sorted(self.led.settled)
        seen = holdout.observations(self.led, self.capped, nights)
        self.assertEqual([seen[d] for d in sorted(seen)], T3_SAMPLE)
        self.assertEqual(sorted(seen), T3_SAMPLE_NIGHTS)

    def test_a_logged_denial_is_demand_seen_and_censors_the_night(self):
        """class_demand adds the denials it could log to the observation and
        treats any of them as censoring.  A replay logs none, so this one is
        entered by hand on a night far below the line."""
        self.led.deny(_feb(1), ingest._Req(0, "RETAIL", 2, _feb(7), 1), "capacity")
        seen = holdout.observations(self.led, self.capped, [_feb(6), _feb(7)])
        self.assertEqual(seen, {_feb(6): (7.0, False), _feb(7): (6.0, True)})

    def test_every_night_of_the_cut_history_is_in_the_sample_and_none_without_a_snapshot(self):
        nights = sorted(self.led.settled)
        est = holdout.per_night_demand(self.led, self.capped, nights)
        self.assertEqual(sorted(est), T3_SAMPLE_NIGHTS)
        for d in T3_SAMPLE_NIGHTS:
            self.assertAlmostEqual(est[d], self.expected[d], places=9)
        self.assertEqual(est[_feb(2)], 8.0)
        self.assertEqual(est[dt.date(2024, 1, 31)], 0.0)

    def test_a_settled_night_the_snapshot_cannot_speak_for_is_left_out(self):
        """`class_demand` skips a night with no lead-0 snapshot rather than
        reading the settled figure in its place, and so does this.

        Both of its guards are unreachable through `capped_ledger`, because the
        replay writes a lead-0 snapshot on every day it walks, so this is the
        only way to reach them: one night is handed over with a snapshot that
        holds a deeper lead and no lead 0, another with no snapshot at all.
        They are kept because `observations` takes a ledger rather than a
        replay, and a caller that builds one another way would otherwise have a
        night scored off a figure the unconstrainer never reads.
        """
        nights = sorted(self.led.settled)
        before = holdout.observations(self.led, self.capped, nights)
        self.assertIn(_feb(3), before)
        self.assertIn(_feb(4), before)
        self.led.snapshots[_feb(3)] = {1: 12}      # snapshotted, but never at lead 0
        self.led.snapshots[_feb(4)] = {}           # never snapshotted at all
        after = holdout.observations(self.led, self.capped, nights)
        self.assertNotIn(_feb(3), after)
        self.assertNotIn(_feb(4), after)
        self.assertEqual(sorted(after),
                         [d for d in sorted(before) if d not in (_feb(3), _feb(4))])
        self.assertEqual(sorted(holdout.per_night_demand(self.led, self.capped, nights)),
                         [d for d in T3_SAMPLE_NIGHTS if d not in (_feb(3), _feb(4))])

    def test_every_scored_night_carries_the_capped_ledger_s_flag(self):
        combo = self._combo()
        got = [(r["date"], r["known"], r["capped"], r["censored"], r["cut_rooms"], r["bucket"])
               for r in combo["nights"]]
        self.assertEqual(got, [
            ("2024-02-02", 9.0, 8.0, False, 1.0, holdout.BUCKETS[1]),
            ("2024-02-03", 16.0, 12.0, True, 4.0, holdout.BUCKETS[2]),
            ("2024-02-04", 12.0, 11.0, True, 1.0, holdout.BUCKETS[1]),
        ])
        by_date = dict((r["date"], r) for r in combo["nights"])
        self.assertEqual(by_date["2024-02-02"]["estimate"], 8.0)
        self.assertAlmostEqual(by_date["2024-02-03"]["estimate"], self.expected[_feb(3)], places=9)
        self.assertAlmostEqual(by_date["2024-02-04"]["estimate"], self.expected[_feb(4)], places=9)

    def test_the_counts_beside_the_cell(self):
        """Fourteen clean nights: the 29th to the 7th and the 11th to the
        14th.  Three of them censored in the capped history, the 3rd, the 4th
        and the 12th, and only the first two scored: the 12th sold the cap
        exactly and lost nothing, so censored_clean is three against a
        censored_scored of two, and a count of clean censored nights taken
        from the scored rows would say two.  Three lost rooms on the settled
        figure, the 2nd to the 4th; the 5th was reached by the gross cut and
        lost nothing.  Eighteen room nights refused gross, cancellations and
        the busy nights included.  Four nights in the band the cap can
        censor, the 3rd, 4th, 10th and 12th, three of them clean at this
        window."""
        combo = self._combo()
        self.assertEqual(
            dict((k, combo[k]) for k in ("clean_nights", "censored_clean", "cut_nights",
                                         "cut_room_nights_gross", "scored",
                                         "censored_scored", "uncensored_scored", "cap_rooms",
                                         "censorable_band_nights", "censorable_band_clean")),
            {"clean_nights": 14, "censored_clean": 3, "cut_nights": 3,
             "cut_room_nights_gross": 18, "scored": 3, "censored_scored": 2,
             "uncensored_scored": 1, "cap_rooms": 12,
             "censorable_band_nights": 4, "censorable_band_clean": 3})
        self.assertEqual(combo["uncensored"], {"n": 1, "mean_rooms_cut": 1.0})
        self.assertNotIn("2024-02-12", [r["date"] for r in combo["nights"]])

    def test_the_cut_share_is_over_the_real_night_and_not_the_capped_one(self):
        """The 3rd sold sixteen and the cap left twelve, so the cut is a
        quarter of the night, 4 of 16, and not a third of what was left, 4 of
        12.  On H1 twelve night-rows change bucket between the two."""
        by_date = dict((r["date"], r) for r in self._combo()["nights"])
        third = by_date["2024-02-03"]
        self.assertEqual((third["known"], third["capped"], third["cut_rooms"]), (16.0, 12.0, 4.0))
        self.assertEqual(third["cut_share"], 4 / 16.0)
        self.assertNotEqual(third["cut_share"], 4 / 12.0)
        for row in by_date.values():
            self.assertEqual(row["cut_share"], row["cut_rooms"] / row["known"])
            self.assertEqual(row["bucket"], holdout.bucket_of(row["cut_rooms"] / row["known"]))

    def test_the_cap_is_rounded_to_the_nearest_room_and_not_truncated(self):
        """Every fixture hotel has twenty rooms and every pre-registered cap
        of it is a whole number, so truncation is invisible until the room
        count is one whose products are not: 21 rooms give 12.6 and 14.7,
        which round to 13 and 15 and truncate to 12 and 14.  At H1's 187
        rooms truncation moves the 0.70 cap from 131 to 130 and the 0.80 cap
        from 150 to 149, sixteen of the twenty-four combinations."""
        hotel = dataclasses.replace(self.res.hotel, rooms=21)
        caps = {}
        for cap in (0.60, 0.70):
            combo = holdout.score_combo(self.res.bookings, hotel, self.res.ledger,
                                        T3_FIRST, T3_LAST, 0.90, cap, "sell_until_full", 1)
            caps[cap] = combo["cap_rooms"]
        self.assertEqual(caps, {0.60: 13, 0.70: 15})
        self.assertEqual([int(round(c * 187)) for c in holdout.CAPS], [112, 131, 150])

    def test_the_band_the_cap_can_censor_is_counted_with_and_without_the_window(self):
        """The band is physical occupancy from the capped hotel's censoring
        line, 11.64 rooms, up to the clean threshold of 18: the 3rd at 16, the
        4th at 12, the 10th at 15 and the 12th at 12.  The 10th sits beside the
        9th's nineteen, so at a window of one it is in the band and not clean,
        and with the window dropped it is both.  A count that read the clean
        list for the band, or the band for the clean list, would give 3 and 3
        or 4 and 4 at window 1."""
        at_one = self._combo(window=1)
        at_zero = self._combo(window=0)
        self.assertEqual((at_one["censorable_band_nights"], at_one["censorable_band_clean"]),
                         (4, 3))
        self.assertEqual((at_zero["censorable_band_nights"], at_zero["censorable_band_clean"]),
                         (4, 4))
        clean_at_one = holdout.clean_nights(self.res.bookings, self.res.hotel, T3_FIRST, T3_LAST,
                                            0.90, 1)
        self.assertNotIn(_feb(10), clean_at_one)
        self.assertIn(_feb(10), holdout.clean_nights(self.res.bookings, self.res.hotel, T3_FIRST,
                                                     T3_LAST, 0.90, 0))
        occ = ingest.physical_occupancy(self.res.bookings)
        self.assertEqual([occ[d] for d in (_feb(3), _feb(4), _feb(10), _feb(12))], [16, 12, 15, 12])
        self.assertEqual([d for d in sorted(occ) if T3_FIRST <= d <= T3_LAST
                          and 12 * 0.97 <= occ[d] < 18],
                         [_feb(3), _feb(4), _feb(10), _feb(12)])

    def test_the_unconstrainer_s_error_is_over_the_censored_nights_only(self):
        e3, e4 = self.expected[_feb(3)], self.expected[_feb(4)]
        overall = self._combo()["overall"]
        self.assertEqual(overall["n"], 2)
        self.assertAlmostEqual(overall["known"], 14.0, places=9)
        self.assertAlmostEqual(overall["estimate"], (e3 + e4) / 2, places=9)
        self.assertAlmostEqual(overall["mae"], (abs(e3 - 16) + abs(e4 - 12)) / 2, places=9)
        self.assertAlmostEqual(overall["bias"], ((e3 - 16) + (e4 - 12)) / 2, places=9)

    def test_the_buckets_hold_censored_nights_and_an_empty_one_prints_nothing(self):
        e3, e4 = self.expected[_feb(3)], self.expected[_feb(4)]
        buckets = self._combo()["buckets"]
        self.assertEqual(sorted(buckets), sorted(holdout.BUCKETS))
        under, mid, over = (buckets[b] for b in holdout.BUCKETS)
        self.assertEqual((under["n"], under["mae"], under["known"], under["estimate"]),
                         (0, None, None, None))
        self.assertEqual(under["by_segment"]["RETAIL"], {"n": 0, "mae": None, "bias": None})
        self.assertEqual((mid["n"], over["n"]), (1, 1))
        self.assertAlmostEqual(mid["mae"], abs(e4 - 12), places=9)
        self.assertAlmostEqual(over["mae"], abs(e3 - 16), places=9)

    def test_the_segments_split_the_estimate_by_the_capped_mix(self):
        """The 3rd kept three corporate rooms of twelve, so a quarter of its
        estimate is corporate, against the three it really sold; the 4th is
        all retail, so it is one retail observation and no corporate one.
        The fixture has no OTA and no GROUP row, so those segments have no
        night at all: n 0 and no estimate, not two nights of a perfect zero
        error, which is what counting a pair of zeros would print.  On H1
        that was six GROUP night-segments scored perfectly on nights with no
        group business, on H2 seven."""
        e3, e4 = self.expected[_feb(3)], self.expected[_feb(4)]
        seg = self._combo()["by_segment"]
        self.assertEqual(seg["CORP"]["n"], 1)
        self.assertAlmostEqual(seg["CORP"]["bias"], e3 * 0.25 - 3, places=9)
        self.assertAlmostEqual(seg["CORP"]["mae"], abs(e3 * 0.25 - 3), places=9)
        self.assertEqual(seg["RETAIL"]["n"], 2)
        self.assertAlmostEqual(seg["RETAIL"]["bias"], ((e3 * 0.75 - 13) + (e4 - 12)) / 2,
                               places=9)
        self.assertEqual(seg["OTA"], {"n": 0, "mae": None, "bias": None})
        self.assertEqual(seg["GROUP"], {"n": 0, "mae": None, "bias": None})

    def test_a_segment_the_cut_emptied_is_still_an_observation(self):
        """A pair with rooms on one side only is kept.  One retail row on the
        3rd made into an OTA row: the real night sold one OTA room, the cut
        refused it, so the capped mix has no OTA and the estimate is zero
        against a known of one.  That is the cut emptying a segment, and an
        error the segment table has to show.  The 4th still has no OTA in
        either history and is still not an OTA observation, so n is one."""
        rows = _t3_rows()
        idx = [i for i, r in enumerate(rows) if r[0] == "D03-11"]
        self.assertEqual(len(idx), 1)
        rows[idx[0]] = _row(booking_id="D03-11", booked_on=T3_LATE, arrival="2024-02-03",
                            nights="1", segment="BOOKING")
        res = ingest.load(_csv(rows), FIXTURE_HOTEL, seed=1, first_stay=T3_FIRST, last_stay=T3_LAST)
        combo = holdout.score_combo(res.bookings, res.hotel, res.ledger, T3_FIRST, T3_LAST,
                                    0.90, 0.60, "sell_until_full", 1)
        third = dict((r["date"], r) for r in combo["nights"])["2024-02-03"]
        self.assertEqual(third["segments"]["OTA"], {"known": 1.0, "estimate": 0.0})
        self.assertEqual(combo["by_segment"]["OTA"], {"n": 1, "mae": 1.0, "bias": -1.0})

    def test_the_grid_replays_without_the_snapshots_it_never_reads(self):
        """The answer is the same at any horizon, which is what the test on
        the thin replay proves, so only a spy can see the horizon asked for."""
        seen = []
        real = holdout.capped_ledger

        def spy(*a, **kw):
            seen.append(kw.get("snapshot_horizon"))
            return real(*a, **kw)

        holdout.capped_ledger = spy
        try:
            self._combo()
        finally:
            holdout.capped_ledger = real
        self.assertEqual(seen, [0])

    def test_the_neighbour_window_is_the_one_handed_in(self):
        """Without it the 8th and the 10th come back clean and the 10th, cut
        from fifteen to twelve, is scored and censored."""
        combo = self._combo(window=0)
        self.assertEqual((combo["clean_nights"], combo["censored_clean"],
                          combo["censored_scored"]), (16, 4, 3))


def _t3_blocks(n):
    """`n` three-night blocks from 1 February: a night of eight singles, a
    night of fifteen, a night of five, and a two-night stay over the first two
    booked a day after everything else.  Cut to twelve, the second night is
    censored and loses four rooms, the first loses the refused stay and is not
    censored, and the third is untouched.  Every night is clean at 0.85 and
    at 0.90 with no window, so a grid of those two thresholds and one cap
    scores n censored and n uncensored nights per combination."""
    rows = []
    for i in range(n):
        d0 = dt.date(2024, 2, 1) + dt.timedelta(days=3 * i)
        for k, count in enumerate((8, 15, 5)):
            d = d0 + dt.timedelta(days=k)
            rows += [_row(booking_id="K%02d%d%02d" % (i, k, j), booked_on="2024-01-20",
                          arrival=d.isoformat(), nights="1") for j in range(count)]
        rows.append(_row(booking_id="K%02dspan" % i, booked_on="2024-01-21",
                         arrival=d0.isoformat(), nights="2"))
    return rows


class Grid(unittest.TestCase):
    def tearDown(self):
        _reset_config()

    def test_buckets_split_at_five_and_fifteen_percent(self):
        self.assertEqual(holdout.bucket_of(0.0), holdout.BUCKETS[0])
        self.assertEqual(holdout.bucket_of(0.0499), holdout.BUCKETS[0])
        self.assertEqual(holdout.bucket_of(0.05), holdout.BUCKETS[1])
        self.assertEqual(holdout.bucket_of(1 / 20.0), holdout.BUCKETS[1])
        self.assertEqual(holdout.bucket_of(0.1499), holdout.BUCKETS[1])
        self.assertEqual(holdout.bucket_of(0.15), holdout.BUCKETS[1])
        self.assertEqual(holdout.bucket_of(3 / 20.0), holdout.BUCKETS[1])
        self.assertEqual(holdout.bucket_of(0.1501), holdout.BUCKETS[2])
        self.assertEqual(holdout.bucket_of(1.0), holdout.BUCKETS[2])

    def _t3_grid(self, **kw):
        res = ingest.load(_csv(_t3_rows()), FIXTURE_HOTEL, seed=1,
                          first_stay=T3_FIRST, last_stay=T3_LAST)
        return holdout.run_grid(res.bookings, res.hotel, res.ledger, T3_FIRST, T3_LAST,
                                window=1, **kw)

    def test_only_caps_below_the_threshold_are_run(self):
        grid = self._t3_grid()
        seen = collections.Counter((c["threshold"], c["cap"]) for c in grid["combos"])
        self.assertEqual(seen, collections.Counter(dict(
            ((t, c), 3) for t, c in ((0.80, 0.60), (0.80, 0.70), (0.85, 0.60), (0.85, 0.70),
                                     (0.85, 0.80), (0.90, 0.60), (0.90, 0.70), (0.90, 0.80)))))
        self.assertEqual(len(grid["combos"]), 24)
        self.assertEqual([c["rule"] for c in grid["combos"][:3]], list(holdout.RULES))

    def test_an_empty_bucket_prints_zero_and_is_never_merged(self):
        grid = self._t3_grid()
        for combo in grid["combos"]:
            self.assertEqual(sorted(combo["buckets"]), sorted(holdout.BUCKETS))
            for name, block in combo["buckets"].items():
                if block["n"] == 0:
                    self.assertIsNone(block["mae"])
        text = " ".join(grid["notes"])
        self.assertIn("never merged", text)
        self.assertIn("one count under two names", text)
        self.assertIn("assumed to vanish", text)
        self.assertIn("censoring_uplift", text)
        self.assertIn("measure the cut and say nothing about the unconstrainer", text)

    def test_the_gate_is_twenty_censored_nights_in_one_combination(self):
        """Twenty blocks and one combination: twenty censored nights in the
        cell that would quote the estimate, so the estimate is quoted."""
        res = ingest.load(_csv(_t3_blocks(20)), FIXTURE_HOTEL, seed=1)
        grid = holdout.run_grid(res.bookings, res.hotel, res.ledger, dt.date(2024, 2, 1),
                                dt.date(2024, 4, 1), window=0, thresholds=(0.90,),
                                caps=(0.60,), rules=("sell_until_full",))
        self.assertEqual([(c["censored_scored"], c["scorable"]) for c in grid["combos"]],
                         [(20, True)])
        self.assertEqual(grid["scorable_combos"], 1)
        self.assertTrue(grid["scorable"])
        self.assertNotIn("No combination in this grid produced a scorable sample", " ".join(grid["notes"]))

    def test_two_combinations_of_ten_are_not_a_sample_of_twenty(self):
        """The same twenty nights, ten of them censored, run under two
        thresholds.  Their censored counts sum to twenty, and the sum is not a
        sample: it is ten nights counted once per combination they appear in,
        and neither cell has enough to quote a mean of its own.

        A grid-wide gate passes this, which is why the gate is per combination.
        """
        res = ingest.load(_csv(_t3_blocks(10)), FIXTURE_HOTEL, seed=1)
        grid = holdout.run_grid(res.bookings, res.hotel, res.ledger, dt.date(2024, 2, 1),
                                dt.date(2024, 3, 1), window=0, thresholds=(0.85, 0.90),
                                caps=(0.60,), rules=("sell_until_full",))
        self.assertEqual([(c["censored_scored"], c["scorable"]) for c in grid["combos"]],
                         [(10, False), (10, False)])
        self.assertEqual(grid["censored_scored_total"], 20)
        self.assertEqual(grid["censored_distinct_nights"], 10)
        self.assertEqual(grid["best_combo_censored"], 10)
        self.assertEqual(grid["scorable_combos"], 0)
        self.assertFalse(grid["scorable"])
        sentence = grid["notes"][-1]
        self.assertIn("the largest reached 10", sentence)
        self.assertIn("sum to 20 across the grid, but that is 10 nights", sentence)

    def test_uncensored_nights_do_not_count_towards_the_gate(self):
        """Nine blocks: thirty-six scored nights, far past twenty, and only
        eighteen of them censored, so the table has no sample."""
        res = ingest.load(_csv(_t3_blocks(9)), FIXTURE_HOTEL, seed=1)
        grid = holdout.run_grid(res.bookings, res.hotel, res.ledger, dt.date(2024, 2, 1),
                                dt.date(2024, 2, 27), window=0, thresholds=(0.85, 0.90),
                                caps=(0.60,), rules=("sell_until_full",))
        self.assertEqual((grid["censored_scored_total"], grid["scored_total"]), (18, 36))
        self.assertFalse(grid["scorable"])
        sentence = grid["notes"][-1]
        # Both combinations hold the same nine censored nights and the same
        # band, the nine second nights at sixteen rooms, so the first of them
        # is named.  With no window the band is left whole, so the sentence
        # must not blame the window.
        self.assertEqual(sentence, holdout.NO_SAMPLE_WINDOW_TOOK_NOTHING % dict(
            gate=20, combos=2, best=9, summed=18, distinct=9, window=0, threshold=0.85,
            cap_rooms=12, rule="sell until full", band=9, band_clean=9, lo=11.64, hi=17.0))
        for words in ("No combination in this grid produced a scorable sample", "the largest reached 9",
                      "sum to 18 across the grid, but that is 9 nights",
                      "9 nights of the window sit in the band",
                      "The window took nothing from that band",
                      "not a defect of this engine's unconstrainer",
                      "neither confirms nor refutes"):
            self.assertIn(words, sentence)
        self.assertNotIn("What empties the sample is that window", sentence)

    def test_the_no_sample_sentence_names_the_window_and_what_it_took_from_the_band(self):
        """The seventeen-night fixture at a window of one.  The largest
        combination is the first at cap twelve with two censored nights, the 3rd
        and the 4th, at threshold 0.85 (0.90 ties it on every count and comes
        later); its band holds the 3rd, the 4th, the 10th and the 12th, and the
        window takes the 10th, which sits beside the 9th's nineteen rooms.  The
        12th is in the band, clean and censored, and never scored, because at
        exactly the cap it lost nothing.  Summed over
        the grid: two censored nights in each of the six cells at cap twelve
        under 0.85 and 0.90, one (the 3rd) in each of the six at cap fourteen,
        none at cap sixteen where the 3rd is censored but loses nothing, and
        none under 0.80 where the 3rd is busy and takes the 2nd and 4th with
        it."""
        grid = self._t3_grid()
        self.assertEqual((grid["best_combo_censored"], grid["censored_scored_total"],
                          grid["censored_distinct_nights"]), (2, 18, 2))
        sentence = grid["notes"][-1]
        self.assertEqual(sentence, holdout.NO_SAMPLE % dict(
            gate=20, combos=24, best=2, summed=18, distinct=2, window=1, threshold=0.85,
            cap_rooms=12, rule="sell until full", band=4, band_clean=3, lo=11.64, hi=17.0))
        for words in ("neighbour window at 0 all 4 of them are clean",
                      "with the window of 1 nights 3 are",
                      "What empties the sample is that window, not the cap",
                      "no busy night within 1 nights on either side",
                      "The window is the pre-registered one",
                      "neither confirms nor refutes"):
            self.assertIn(words, sentence)
        self.assertNotIn("property of any holdout", sentence)

    def test_a_grid_with_no_cap_below_any_threshold_is_refused_in_a_sentence(self):
        res = ingest.load(_csv(_t3_rows()), FIXTURE_HOTEL, seed=1,
                          first_stay=T3_FIRST, last_stay=T3_LAST)
        with self.assertRaises(pilot.PilotError) as ctx:
            holdout.run_grid(res.bookings, res.hotel, res.ledger, T3_FIRST, T3_LAST, window=1,
                             thresholds=(0.60,), caps=(0.60, 0.70))
        self.assertIn("no combination in the grid has a cap below its threshold", str(ctx.exception))
        self.assertEqual(holdout.grid_cells(), [(t, c, r) for t in (0.80, 0.85, 0.90)
                                                for c in (0.60, 0.70, 0.80) if c < t
                                                for r in holdout.RULES])
        self.assertEqual(len(holdout.grid_cells()), 24)

    def test_a_grid_that_cuts_nothing_says_so_rather_than_quoting_a_number(self):
        """Five rooms of twenty every night: below every clean threshold, above
        no cap, so every night is clean and none of them is ever cut."""
        rows = []
        for day in range(40):
            d = FIRST_ARRIVAL + dt.timedelta(days=day)
            for i in range(5):
                rows.append(_row(booking_id="Q%03d%02d" % (day, i),
                                 booked_on=(d - dt.timedelta(days=10)).isoformat(),
                                 arrival=d.isoformat(), nights="1"))
        res = ingest.load(_csv(rows), FIXTURE_HOTEL, seed=1)
        grid = holdout.run_grid(res.bookings, res.hotel, res.ledger, FIRST_ARRIVAL,
                                FIRST_ARRIVAL + dt.timedelta(days=39), window=2)
        self.assertEqual((grid["scored_total"], grid["censored_scored_total"]), (0, 0))
        self.assertFalse(grid["scorable"])
        self.assertEqual(len(grid["combos"]), 24)
        # Every combination is alike, so the first is named: threshold 0.80
        # and cap twelve, whose band runs from 11.64 to under 16 rooms and
        # holds nothing at five.
        self.assertEqual(grid["notes"][-1], holdout.NO_SAMPLE_WINDOW_TOOK_NOTHING % dict(
            gate=20, combos=24, best=0, summed=0, distinct=0, window=2, threshold=0.80,
            cap_rooms=12, rule="sell until full", band=0, band_clean=0, lo=11.64, hi=16.0))
        for combo in grid["combos"]:
            self.assertEqual((combo["clean_nights"], combo["censored_clean"],
                              combo["cut_nights"], combo["cut_room_nights_gross"],
                              combo["censorable_band_nights"], combo["censorable_band_clean"]),
                             (40, 0, 0, 0, 0, 0))
            self.assertIsNone(combo["overall"]["mae"])
