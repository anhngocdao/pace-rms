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
import hashlib
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import hotelconfig as HC
from . import ingest
from .calendar import EventCalendar
from .config import Hotel
from .ingest import Booking, Report
from .ledger import Hold, Ledger
from .policy import PaceEngine

# The three windows below are pre-registered in data/antonio/settings.json,
# section 9 of the design, and written out here as dates because pace/ must not
# read data/: a hotel running the ingest has the package and not the pilot's
# fixtures.  A second copy is only safe if it cannot drift from the first, so
# tests/test_pilot.py's PreRegistration pins each one to the file, the way
# tests/test_convert_antonio.py pins the converter's own copies.  The
# pre-registered lead marks are deliberately not copied here at all: marks_for
# reads them out of the settings file, so that list has exactly one home.
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


def ledger_rows(bookings: List[Booking]) -> List[Booking]:
    """The rows that reach the ledger: revenue rows with at least one night.

    walk() and ingest.replay both filter exactly this way, so every window and
    every count taken from the log is taken from this population rather than
    from the raw file, which also carries comped rooms and zero-night day use.
    """
    return [b for b in bookings if b.target not in (None, "NONREV") and b.nights > 0]


def stay_window(bookings: List[Booking], last_cap: dt.date) -> Tuple[dt.date, dt.date]:
    """The stay window the ledger is built over (design, section 4).

    Guests who arrived before the export opened and were still in house are not
    in the file, so its first nights are short by an unknown number of rooms.
    Trim the front by the 99th percentile of length of stay: 14 nights at H1 and
    9 at H2 on the public dataset. The back is capped because the export stops.
    """
    rows = ledger_rows(bookings)
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


def trim_nights(bookings: List[Booking], first_stay: dt.date) -> int:
    """How many nights stay_window took off the front, measured against the
    rows it trimmed.

    Against the raw file instead, one comped room or one day-use row entered
    before the first real arrival is reported as a trim of weeks that never
    happened: the window the code used would be right and the number beside it
    describing that window would be wrong.
    """
    return (first_stay - min(b.arrival for b in ledger_rows(bookings))).days


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


PREREG_COMMIT = "d23024c2b135e0a4af3dd5d4723b7c3fe604d618"
PREREG_SHA256 = "f59937ba6b768fab3f4eb5707ce5579ef15f99c7ccf16d4d957e7255c8e94a2b"

NO_LIFT_NOTE = (
    "No number in this report is a revenue lift. History records only what sold "
    "at the rate that was charged, so nothing here can say what the hotel would "
    "have earned under the engine's rates. Where the engine forecasts better, it "
    "forecasts better; that is the claim, and it is the whole claim.")

_CLAIMED = {"hotel": None}


def claim_process(hotel_json_path: str) -> None:
    """One hotel per process, keyed on which file this hotel is.

    hotelconfig.apply rebinds the module-level seasonality and mutates the
    shared SEGMENTS dict in place, so loading a second hotel rewrites the first
    one's engine: H1 sets CORP 0.6275 and GROUP 0.9949, H2 sets 0.7391 and
    0.7653.  Refusing here is cheaper than a report whose H1 tables were built
    with H2's contract ratios, because that report would look fine.

    The key is os.path.realpath and not the file's name.  `hotel.json` beside a
    booking log is the shape CLAUDE.md documents for the ingest, so two hotels
    in two directories are both called hotel.json more often than not, and a
    guard keyed on the name would wave the second one through with nothing to
    say.  realpath answers the other direction too: the same hotel reached by
    two different relative paths, or through a symlink, is one hotel and is
    allowed.
    """
    key = os.path.realpath(hotel_json_path)
    held = _CLAIMED["hotel"]
    if held is not None and held != key:
        # The command that joins the two files is described rather than named:
        # `run.py pilot-report` does not exist until Task 10, and an error
        # message that sends a hotel to a command which prints a usage dump is
        # worse than one that says what to do.  Task 10 may name it here.
        raise PilotError(
            "this process already ran %s. hotelconfig.apply rebinds the engine's "
            "seasonality and rewrites its segment table in place, so %s has to run "
            "in a process of its own: run the same command again for it, and the "
            "report that stands two hotels side by side is built afterwards from "
            "the two JSON files the two runs wrote." % (held, key))
    _CLAIMED["hotel"] = key


def _release_for_tests() -> None:
    """Only the test suite calls this.  A real run is one hotel and then exits."""
    _CLAIMED["hotel"] = None


def settings_digest(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _read_settings(path: str) -> dict:
    """The pre-registered settings file, or a sentence saying what is wrong.

    A hotel is owed a sentence rather than a traceback (hotelconfig.py's house
    rule), and every one of these is a path typed on a command line.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            settings = json.load(fh)
    except OSError as exc:
        raise PilotError("cannot read the settings file at %s: %s. Point --settings at the "
                         "pre-registered file, or leave it off to take "
                         "data/antonio/settings.json." % (path, exc))
    except ValueError as exc:
        # json.JSONDecodeError is a ValueError; a half-edited settings file is
        # the ordinary way this happens.
        raise PilotError("the settings file at %s is not valid JSON: %s" % (path, exc))
    if not isinstance(settings, dict):
        raise PilotError("the settings file at %s holds a %s, not an object of settings"
                         % (path, type(settings).__name__))
    if "lead_marks" not in settings:
        raise PilotError("the settings file at %s has no lead_marks, so there is no lead to "
                         "score table 1 at; it should carry the pre-registered list from "
                         "data/antonio/settings.json." % path)
    return settings


def _prepare_out_dir(out_dir: str, path: str) -> None:
    """Make --out and prove the payload can be written into it, before the walk.

    The walk is minutes on a real hotel.  Discovering a read-only directory
    afterwards throws the whole run away, and it is the one failure that costs
    everything it had already done, so it is bought out here for a file the
    size of nothing.
    """
    try:
        os.makedirs(out_dir, exist_ok=True)
        probe = os.path.join(out_dir, ".pilot-write-probe-%d" % os.getpid())
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("")
        os.remove(probe)
    except OSError as exc:
        raise PilotError("cannot write into the output directory %s: %s. This is checked "
                         "before the walk rather than after it, because the walk is "
                         "minutes and the payload is the whole point of it." % (out_dir, exc))
    if os.path.exists(path) and not os.access(path, os.W_OK):
        raise PilotError("the payload this run would write, %s, already exists and is not "
                         "writable. This is checked before the walk rather than after it, "
                         "because the walk is minutes and the payload is the whole point "
                         "of it." % path)


def run_one(csv_path: str, hotel_json_path: str, settings_path: str, out_dir: str,
            score_first: dt.date = SCORE_FIRST, score_last: dt.date = SCORE_LAST,
            warmup_end: dt.date = WARMUP_END, progress: Optional[int] = None,
            label: str = "", with_holdout: bool = True) -> dict:
    """Ingest, walk, score tables 1 to 3, write the payload.  One hotel, one process.

    `label` marks a run that did not score the whole pre-registered window:
    it is written into the file name and into the payload, so a partial run
    can neither overwrite a real one nor be read back as if it were one.

    `with_holdout` False leaves table 3 as None.  `--quick` passes it, because
    the holdout is 24 cut replays of the whole history and a wiring check has
    no use for them.

    Everything that can be checked cheaply is checked before the process is
    claimed and before the walk starts.  A hotel gets one run; a typo in a
    path should cost a sentence, not the run, and a hotel.json that never
    loaded must not be the name every later attempt is refused against.
    """
    # Imported here rather than at the top: pace.score imports pace.pilot for
    # capacity_on and LATE_MARK, so the two modules are a cycle in the import
    # graph whichever way round the import is written.  It is a benign one --
    # score's import of pilot completes before either module uses a name from
    # the other, and a top-level `from . import score` here passes in every
    # import order -- so this deferral is a choice that keeps the orchestrator
    # out of its own parts' imports, not a necessity.
    from . import holdout
    from . import ratecheck
    from . import score as scoring

    settings = _read_settings(settings_path)
    digest = settings_digest(settings_path)
    if not os.path.isfile(csv_path):
        raise PilotError("no booking log to read at %s: %s. The first argument is the CSV "
                         "export and the second is the hotel.json that describes it."
                         % (csv_path, "that path is a directory"
                            if os.path.isdir(csv_path) else "there is no such file"))
    # Read and validated before the claim, and before ingest.load reads it
    # again: load_hotel_json touches no global (only hotelconfig.apply does),
    # so a hotel.json with a typo in its path or its JSON fails here with a
    # sentence and leaves the process free for the file that was meant.
    cfg = HC.load_hotel_json(hotel_json_path)
    code = hotel_code(cfg)
    path = os.path.join(out_dir, "pilot-%s%s.json"
                        % (code.lower(), ("-" + label) if label else ""))
    _prepare_out_dir(out_dir, path)

    claim_process(hotel_json_path)

    res = ingest.load(csv_path, hotel_json_path,
                      seed=int(settings.get("seed", ingest.DEFAULT_SEED)))
    first_stay, last_stay = stay_window(res.bookings, score_last)
    if first_stay > score_first:
        raise PilotError("the stay window starts at %s, after the scoring window opens at %s; "
                         "there is no warm-up to fit on" % (first_stay, score_first))
    marks = marks_for(code, settings, res.hotel)
    drive_from = score_first - dt.timedelta(days=DRIVE_LEAD)
    cal = HC.event_calendar(res.cfg)

    # The walk writes into the hotel's own ingest report, so the audit block
    # below carries what ingest actually found instead of an empty report
    # standing in for it.  Both replays count over_capacity_nights, over
    # windows that overlap, so the sum of the two is a count of nothing:
    # ingest's findings are read here, before the walk adds to them, and what
    # the walk added is published beside them as the walk's own.
    ingest_warnings = {k: v for k, v in res.report.warnings.items() if not k.startswith("_")}
    out = walk(res.bookings, res.hotel, cal, first_stay, last_stay,
               score_first, score_last, marks, drive_from, rep=res.report, progress=progress)
    walk_warnings = {}
    for k, v in res.report.warnings.items():
        if not k.startswith("_") and v - ingest_warnings.get(k, 0) > 0:
            walk_warnings[k] = v - ingest_warnings.get(k, 0)
    gap = full_night_gap(res.bookings, out.ledger, res.hotel, first_stay, last_stay)
    table1 = scoring.score(out, res.hotel, res.bookings, res.nonrev, marks,
                           score_first, score_last, first_stay, warmup_end)

    inf = res.inference
    payload = {
        # A run that scored only part of the window is a different file with a
        # different name and says which it is in its own right.  The name alone
        # would not survive being renamed; the field alone would still let a
        # 62-night run overwrite a 427-night one on disk.
        "label": label or "full",
        "hotel": {
            "code": code, "name": res.cfg.name, "city": res.cfg.city,
            "currency": res.cfg.currency, "rooms": res.hotel.rooms,
            "rooms_inferred": inf is not None,
            "peak_night": inf.peak_night.isoformat() if inf and inf.peak_night else None,
            "nights_within_2pct": inf.nights_within_2pct if inf else None,
            "second_highest": inf.second_highest if inf else None,
            "per_year_max": {str(k): v for k, v in (inf.per_year_max if inf else {}).items()},
            "rates_include_tax": res.cfg.rates_include_tax,
            "base_rate": res.hotel.base_rate, "rate_floor": res.hotel.rate_floor,
            "rate_ceiling": res.hotel.rate_ceiling, "rate_step": res.hotel.rate_step,
            "top_rung": res.hotel.rate_ladder()[-1],
            "max_lead": res.hotel.max_lead, "max_los": res.hotel.max_los,
            "sellout_threshold": res.hotel.sellout_threshold,
            "variable_cost": res.hotel.variable_cost, "walk_cost": res.hotel.walk_cost,
            "demand_season_band": {str(k): v for k, v in res.cfg.demand_season_band.items()},
            "segment_rate_ratio": dict(res.cfg.segment_rate_ratio),
        },
        "windows": {
            "first_stay": first_stay.isoformat(), "last_stay": last_stay.isoformat(),
            "warmup_end": warmup_end.isoformat(),
            "score_first": score_first.isoformat(), "score_last": score_last.isoformat(),
            "drive_from": drive_from.isoformat(),
            "trim_nights": trim_nights(res.bookings, first_stay),
            "scoring_nights": (score_last - score_first).days + 1,
        },
        "prereg": {"commit": PREREG_COMMIT, "settings_sha256": digest,
                   "settings_path": settings_path,
                   "sha256_matches_the_recorded_one": digest == PREREG_SHA256},
        "walk": {"days": out.days, "seconds": round(out.seconds, 1),
                 "fits": len(out.engine.fit_log), "records": len(out.records),
                 "marks": [int(m) for m in marks], "solves": out.engine.solves,
                 "over_capacity": walk_warnings},
        "ingest": {
            "rows": len(res.bookings), "bookings": out.ledger.n_bookings,
            "cancels": out.ledger.n_cancels, "nonrev_rows": len(res.nonrev),
            "warnings": ingest_warnings,
            "notes": list(res.report.notes),
        },
        "full_night_gap": gap,
        "table1": table1,
        # Table 3 is written by the holdout.  A None here means it was not run,
        # which is what --quick leaves behind.
        "table3": None,
        "notes": [NO_LIFT_NOTE, FULL_NIGHT_NOTE],
    }
    payload["table2"] = ratecheck.table2(
        out, res.bookings, res.nonrev, res.hotel, code,
        {int(k): v for k, v in table1["season_of_month"].items()},
        score_first, score_last)
    if with_holdout:
        # The neighbour window is the 90th percentile of length of stay: 8
        # nights at H1 and 5 at H2.  A stay spanning a full night is refused for
        # all of its nights, so a clean night needs clean neighbours.
        window = int(percentile([float(b.nights) for b in ledger_rows(res.bookings)], 0.90))
        if progress:
            print("    table 3: %d cut replays of the history, neighbour window %d"
                  % (8 * len(holdout.RULES), window), flush=True)
        payload["table3"] = holdout.run_grid(res.bookings, res.hotel, out.ledger,
                                             first_stay, last_stay, window)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    payload["_json_path"] = path
    return payload
