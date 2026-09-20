import collections
import contextlib
import csv
import datetime as dt
import hashlib
import io
import json
import os
import random
import tempfile
import unittest
from collections import defaultdict

from pace import baselines
from pace import hotelconfig as HC
from pace import ingest
from pace import pilot
from pace import score as S
from pace.elasticity import acceptance as _price_acceptance
from pace.ledger import Ledger
from pace.policy import PaceEngine

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
                     arrival=d.isoformat(), nights=nights, rate=rate)
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
        cls.payload = pilot.run_one(cls.csv_path, FIXTURE_HOTEL, cls.settings_path, cls.out_dir,
                                    score_first=SCORE_FIRST, score_last=SCORE_LAST,
                                    warmup_end=SCORE_FIRST - dt.timedelta(days=1))
        cls.written = os.path.join(cls.out_dir, "pilot-pilot.json")
        with open(cls.written, "rb") as fh:
            cls.on_disk = fh.read()
        # A second run of the same hotel, scoring three weeks instead of three
        # months, standing in for `--quick`.
        cls.quick = pilot.run_one(cls.csv_path, FIXTURE_HOTEL, cls.settings_path, cls.out_dir,
                                  score_first=SCORE_FIRST,
                                  score_last=SCORE_FIRST + dt.timedelta(days=cls.QUICK_NIGHTS - 1),
                                  warmup_end=SCORE_FIRST - dt.timedelta(days=1),
                                  label="quick")
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

    def test_tables_two_and_three_are_none_until_they_are_run(self):
        self.assertIsNone(self.back["table2"])
        self.assertIsNone(self.back["table3"])

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
