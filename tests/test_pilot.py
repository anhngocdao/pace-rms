import csv
import datetime as dt
import os
import random
import tempfile
import unittest
from collections import defaultdict

from pace import hotelconfig as HC
from pace import ingest
from pace import pilot
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
        # to call the night full.  19 sold plus one comp is full in the house.
        rows = [_row(booking_id="S%d" % i, arrival="2024-02-01", nights="1") for i in range(19)]
        rows += [_row(booking_id="C1", segment="COMP", rate="0", arrival="2024-02-01", nights="1")]
        rows += [_row(booking_id="T%d" % i, arrival="2024-02-02", nights="1") for i in range(4)]
        res = self._bookings(rows)
        gap = pilot.full_night_gap(res.bookings, res.ledger, res.hotel,
                                   dt.date(2024, 2, 1), dt.date(2024, 2, 2))
        self.assertEqual(gap["physically_full"], 1)
        self.assertEqual(gap["full_but_invisible"], 1)
        self.assertEqual(gap["nights"], 2)

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

    def test_a_mark_deeper_than_max_lead_is_refused(self):
        settings = {"lead_marks": [120, 90], "lead_marks_h1_only": []}
        hotel = HC.apply(HC.load_hotel_json(FIXTURE_HOTEL))   # max_lead 40
        with self.assertRaises(pilot.PilotError):
            pilot.marks_for("H1", settings, hotel)
