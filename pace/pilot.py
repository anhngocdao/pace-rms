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


FULL_NIGHT_NOTE = (
    "Two counts of a night the ledger could not see as full, and only one "
    "supports a claim about the unconstrainer. full_but_invisible reads the "
    "settled figure: the honest count of nights that were full in the "
    "house while the rooms actually sold, after no-shows and any walk, "
    "fell short of the sell-out threshold. full_but_uncensored_by_snapshot "
    "reads the lead-0 snapshot instead, the same figure "
    "pace/unconstrain.py's own censoring test reads, so it is the count of "
    "nights the unconstrainer itself treated as an uncensored observation "
    "and so learned a demand that is too low from. The two disagree on "
    "nights whose no-shows or walked rooms are resolved between the "
    "snapshot and the settlement. Neither count is a correction to the "
    "engine's forecast, and the room count both are measured against is "
    "itself inferred from the busiest observed night.")


def percentile(values: Sequence[float], p: float) -> float:
    """Linear interpolation between order statistics.

    The same formula as tools/convert_antonio.py `_percentile`, repeated rather
    than imported because pace/ must not depend on tools/: a hotel running the
    ingest has the package and not the converter.
    """
    s = sorted(values)
    if not s:
        return 0.0
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def hotel_code(cfg) -> str:
    """H1 out of "H1 Resort Hotel, Algarve", for file names and the 120 mark."""
    head = (getattr(cfg, "name", "") or "").split()
    return head[0].upper() if head else "HOTEL"


def stay_window(bookings: List[Booking], last_cap: dt.date) -> Tuple[dt.date, dt.date]:
    """The stay window the ledger is built over (design, section 4).

    Guests who arrived before the export opened and were still in house are not
    in the file, so its first nights are short by an unknown number of rooms.
    Trim the front by the 99th percentile of length of stay: 14 nights at H1 and
    9 at H2 on the public dataset. The back is capped because the export stops.
    """
    rows = [b for b in bookings if b.target not in (None, "NONREV") and b.nights > 0]
    if not rows:
        raise PilotError("no booking row reaches the ledger, so there is no stay window to take; "
                         "check the segment map and that the export covers real stays")
    trim = int(percentile([float(b.nights) for b in rows], 0.99))
    first = min(b.arrival for b in rows) + dt.timedelta(days=trim)
    last = min(max(b.departure for b in rows) - dt.timedelta(days=1), last_cap)
    if last <= first:
        raise PilotError("the stay window is empty after trimming %d nights off the front: "
                         "first stay %s, last stay %s" % (trim, first, last))
    return first, last


def capacity_on(hotel: Hotel, nonrev: List[Booking], night: dt.date,
                asof: Optional[dt.date] = None) -> int:
    """Rooms that could still be sold on `night`, as known on `asof`.

    Table 1 clamps to this before scoring, because on a full night a forecast
    above capacity is not wrong. The comp rooms come off because they are not
    for sale, and only the ones entered on or before `asof`, or every method is
    handed a room count that was not knowable on the day it forecast.
    """
    return max(0, hotel.rooms - ingest.nonrev_on(nonrev, night, asof))


def full_night_gap(bookings: List[Booking], ledger: Ledger, hotel: Hotel,
                   first: dt.date, last: dt.date) -> dict:
    """Nights that were physically full while the ledger cannot see them as full.

    `[first, last]` is a window over settled nights; the caller decides what
    it is (the trimmed stay window, or a narrower scoring window inside it),
    and the two counts below are shares of whichever one was passed, never
    an unlabelled "share" that leaves the denominator to be guessed.

    Two counts of "cannot see", because two ledger reads disagree about
    which nights that is. `full_but_invisible` compares the settled figure,
    `ledger.settled[d]["rooms_sold"]`, the honest record of what was
    actually sold, against the sell-out cut; it is the primary count.
    `full_but_uncensored_by_snapshot` compares the lead-0 snapshot instead,
    `ledger.snapshots[d][0]`, which is the figure pace/unconstrain.py's own
    censoring test reads (`final = snaps.get(0)`), so it is the count that
    actually corresponds to what the unconstrainer treats as uncensored.
    The two differ on nights whose no-shows or walked rooms are resolved
    between the snapshot (taken before that night's settlement) and the
    settlement itself; a night with no lead-0 snapshot at all is left out of
    the second count, the same way class_demand() skips it entirely rather
    than guessing which side of the cut it belongs on.
    """
    occ = ingest.physical_occupancy(bookings)
    cut = hotel.rooms * hotel.sellout_threshold
    nights = [d for d in ledger.settled if first <= d <= last]
    physically_full = 0
    invisible_settled = 0
    invisible_snapshot = 0
    for d in nights:
        if occ.get(d, 0) < cut:
            continue
        physically_full += 1
        if ledger.settled[d]["rooms_sold"] < cut:
            invisible_settled += 1
        snap0 = ledger.snapshots.get(d, {}).get(0)
        if snap0 is not None and snap0 < cut:
            invisible_snapshot += 1
    return {"nights": len(nights), "physically_full": physically_full,
            "full_but_invisible": invisible_settled,
            "full_but_uncensored_by_snapshot": invisible_snapshot,
            "share_of_nights": (invisible_settled / len(nights)) if nights else 0.0,
            "share_of_full_nights": (invisible_settled / physically_full) if physically_full else 0.0,
            "threshold_rooms": round(cut, 2), "rooms": hotel.rooms}


def marks_for(code: str, settings: dict, hotel: Hotel) -> Tuple[int, ...]:
    """Lead marks for this hotel, from the pre-registered list."""
    only_first = set(int(m) for m in settings.get("lead_marks_h1_only", ()))
    marks = [int(m) for m in settings["lead_marks"]]
    out = [m for m in marks if m not in only_first or code.upper() == "H1"]
    deep = [m for m in out if m > hotel.max_lead]
    if deep:
        raise PilotError("lead marks %s are deeper than the hotel's max_lead of %d, so no "
                         "snapshot exists at them" % (deep, hotel.max_lead))
    return tuple(sorted(out, reverse=True))
