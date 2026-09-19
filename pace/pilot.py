"""The pilot's forward walk: the engine asked about a night it cannot see.

`Forecaster.forecast` reads the ledger's current state.  It takes
`ledger.rooms_on(stay_date)` at forecast.py:67, and `adr_on`, `segment_mix`
and `seg_revenue` the same way; `asof` sets the lead and nothing else.  Handed
the finished ledger and swept backwards it answers a question about March with
the books of August, and it does not raise, warn or return None.  Measured on
H1 stay date 2016-08-13, which finished at 183 rooms: at lead 120 it was handed
183 when 131 were really on the books, and forecast 275.

So the pilot owns the day loop.  One Ledger is mutated forward through time and
the engine is called inside it.  Controls come first, on the books as they
stood at the end of yesterday, because that is when a hotel publishes a rate
and it is the order pace/simulate.py replay uses.  The three ledger-mutating
steps after it, book then cancel then snapshot, and the settlement after those,
are the ones pace/ingest.py replay uses, in the same order, so the ledger this
walk leaves behind is the ledger ingest.load would have built over the same
window.  Reconstructing an as-of ledger from a finished one is not an
alternative: ledger.holds keeps only the bookings that survived, so at lead 90
on one H1 night only 123 of the 154 rooms then on the books still exist.
"""
import datetime as dt
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import ingest
from .calendar import EventCalendar
from .config import Hotel
from .ingest import Booking, Report
from .ledger import Hold, Ledger
from .policy import PaceEngine

# Pre-registered in data/antonio/settings.json, section 9 of the design.
MARKS = (120, 90, 60, 30, 14, 7)
LATE_MARK = 1
SCORE_FIRST = dt.date(2016, 7, 1)
SCORE_LAST = dt.date(2017, 8, 31)
WARMUP_END = dt.date(2016, 6, 30)

# Days of published-rate history before the scoring window opens.  The damping
# at policy.py:171 reads yesterday's rate and caps the daily move, so a rate
# read at a lead mark is only the rate the engine would have published if the
# path that led to it was walked too.  180 is the engine's own horizon, which
# is when a stay date first enters controls(), and it covers the deepest lead
# mark (120) with 60 days to spare.
DRIVE_LEAD = 180


class PilotError(RuntimeError):
    pass


@dataclass
class WalkRecord:
    stay_date: dt.date
    lead: int
    asof: dt.date
    otb: Optional[int]
    forecast_otb: Optional[int] = None
    forecast: Optional[float] = None
    published_rate: Optional[float] = None
    solved_on: Optional[dt.date] = None
    engine_ready: bool = False
    # The day of the fit behind the forecaster that answered this record, read
    # from the engine's own fit log rather than from the walk's bookkeeping.
    # otb equality alone cannot catch a forecaster whose curves were learned
    # on data from after asof: rooms_on/otb never depend on which fit
    # produced the curves, only on which ledger and stay_date were asked
    # about.  This field is what lets the guard see that second leak.
    fit_asof: Optional[dt.date] = None


@dataclass
class WalkResult:
    ledger: Ledger
    engine: PaceEngine
    records: Dict[Tuple[dt.date, int], WalkRecord] = field(default_factory=dict)
    days: int = 0
    seconds: float = 0.0
    report: Optional[Report] = None


def _last_fit_asof(engine: PaceEngine) -> Optional[dt.date]:
    """The day of the fit behind the engine's current forecaster, read from
    the engine's own fit log so the guard checks the engine's state rather
    than trusting the walk's own idea of when it last fit."""
    if not engine.fit_log:
        return None
    return dt.date.fromisoformat(engine.fit_log[-1]["asof"])


def walk(bookings: List[Booking], hotel: Hotel, cal: EventCalendar,
         first_stay: dt.date, last_stay: dt.date,
         score_first: dt.date, score_last: dt.date,
         marks: Sequence[int], drive_from: dt.date,
         warmup_nights: int = 150, refit_every: int = 28,
         rep: Optional[Report] = None, progress: Optional[int] = None) -> WalkResult:
    """Replay the log day by day, driving the engine from `drive_from`."""
    if score_first > score_last:
        raise PilotError("score_first %s is after score_last %s; the scoring window is "
                         "empty or backwards" % (score_first, score_last))
    if drive_from > score_last:
        raise PilotError("drive_from %s is after the scoring window closes on %s; the "
                         "engine would never be driven while there is still anything to "
                         "score" % (drive_from, score_last))
    all_marks = tuple(sorted(set(tuple(marks) + (LATE_MARK,)), reverse=True))
    too_deep = [m for m in all_marks if m > hotel.max_lead]
    if too_deep:
        raise PilotError("lead mark(s) %s exceed the hotel's max_lead of %d; "
                         "ledger.snapshot only keeps that many days of history, so "
                         "otb_at would come back None instead of a number" % (too_deep, hotel.max_lead))

    rep = ingest.Report() if rep is None else rep
    ledger = Ledger(hotel, first_stay, last_stay)
    engine = PaceEngine(hotel, cal, refit_every=refit_every, warmup_nights=warmup_nights)

    by_day: Dict[dt.date, List[Booking]] = defaultdict(list)
    cancels: Dict[dt.date, List[Booking]] = defaultdict(list)
    for b in bookings:
        if b.target in (None, "NONREV") or b.nights == 0:
            continue
        by_day[b.booked_on].append(b)
        if b.status == "cancelled" and b.status_date is not None:
            cancels[b.status_date].append(b)

    holds: Dict[str, Hold] = {}
    records: Dict[Tuple[dt.date, int], WalkRecord] = {}
    day = min(by_day) if by_day else first_stay
    settled_upto = first_stay - dt.timedelta(days=1)
    rid = 0
    days = 0
    started = time.time()

    while day <= last_stay:
        driving = day >= drive_from
        if driving and engine.last_fit is None:
            # Fit once before the first controls() call, or the engine spends
            # its first driven day on the ladder fallback for no reason.
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

        if driving:
            for lead in all_marks:
                d = day + dt.timedelta(days=lead)
                if d > last_stay or not (score_first <= d <= score_last):
                    continue
                fc = None
                if engine.forecaster is not None:
                    fc = engine.forecaster.forecast(ledger, d, day)
                solved = engine.last_solved.get(d)
                records[(d, lead)] = WalkRecord(
                    stay_date=d, lead=lead, asof=day,
                    otb=ledger.otb_at(d, lead),
                    forecast_otb=(fc.otb if fc is not None else None),
                    forecast=(fc.expected_bookings if fc is not None else None),
                    published_rate=(ctrl.rate.get(d) if ctrl is not None else None),
                    solved_on=(solved[0] if solved else None),
                    engine_ready=engine.ready,
                    fit_asof=(_last_fit_asof(engine) if fc is not None else None),
                )

        while settled_upto < day and settled_upto < last_stay:
            night = settled_upto + dt.timedelta(days=1)
            if night >= first_stay:
                result = ledger.settle(night)
                if result["walked"]:
                    rep.warnings["over_capacity_nights"] += 1
                    rep.warnings["rooms_walked_off_the_actuals"] += result["walked"]
            settled_upto = night

        if driving:
            engine.observe(day, ledger)

        days += 1
        if progress and days % progress == 0:
            print("    %s  otb_today=%3d  records=%d" % (day, ledger.rooms_on(day), len(records)),
                  flush=True)
        day += dt.timedelta(days=1)

    return WalkResult(ledger=ledger, engine=engine, records=records, days=days,
                      seconds=time.time() - started, report=rep)
