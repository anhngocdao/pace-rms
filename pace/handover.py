"""The handover: one forecast, three departments.

The engine forecasts rooms for a stay night. The front desk reads that
directly. The kitchen and housekeeping work in service days, and every
quantity they read is derived from the rooms forecast as booked plus pickup
(spec section 6.2), banded on its own error (6.1), and scored on nights the
band was never measured on (6.3).

Day convention (spec section 3), the only one in this module: breakfast on
morning D belongs to night D - 1, dinner on evening D to night D, departures
on day D are rooms whose last night was D - 1, stayovers on day D cover both
D - 1 and D. Every function here indexes by the NIGHT a quantity belongs to;
the page re-labels by service day.

This module reads and never prices.
"""
import datetime as dt
from collections import defaultdict
from statistics import fmean
from typing import Dict, List, Optional, Tuple

from . import baselines
from . import ingest
from . import pilot
from .ingest import Booking
from .ledger import Ledger

LEADS = tuple(range(0, 15))
HORIZON = 14
BREAKFAST_BOARDS = frozenset(("BB", "HB", "FB"))
DINNER_BOARDS = frozenset(("HB", "FB"))
NO_MEAL_BOARDS = frozenset(("SC", "Undefined", ""))
QUANTITIES = ("rooms", "breakfast", "dinner", "stayovers", "departures")
MIN_BAND_NIGHTS = 30       # spec 6.4: a p90 needs enough nights to be a p90
P_LO, P_MID, P_HI = 0.10, 0.50, 0.90
WINDOW_WEEKS = baselines.WINDOW   # settings.json baseline_window_weeks, pre-registered


def physical_rows(bookings: List[Booking], nonrev: List[Booking]) -> List[Booking]:
    """Rows that occupy a room: stayed or in house, at least one night, NONREV
    included, because a comped room is still stripped and made up."""
    return [b for b in list(bookings) + list(nonrev) if b.occupies and b.nights > 0]


def rows_on(rows: List[Booking], night: dt.date) -> List[Booking]:
    return [b for b in rows if b.arrival <= night < b.departure]


def covers(rows: List[Booking], boards) -> Optional[int]:
    """Guests on the rows whose board eats this meal. None when any row has no
    guests figure: a file without the column gets no covers, not a guess."""
    total = 0
    for b in rows:
        if b.guests is None:
            return None
        if (b.meal or "").strip() in boards:
            total += b.guests
    return total


def _stayovers(rows: List[Booking], night: dt.date) -> int:
    """Rooms on `rows` covering both night and night + 1: the stayovers of day night + 1."""
    nxt = night + dt.timedelta(days=1)
    return sum(b.rooms for b in rows if b.arrival <= night and nxt < b.departure)


def _departures(rows: List[Booking], day: dt.date) -> int:
    """Rooms whose last night was day - 1."""
    return sum(b.rooms for b in rows if b.departure == day and b.nights > 0)


def _arrivals(rows: List[Booking], day: dt.date) -> int:
    return sum(b.rooms for b in rows if b.arrival == day and b.nights > 0)


class Index:
    """The rows filed by night, built once per log.

    Every function below answers for one (night, lead) and a run asks for
    thousands of them; scanning forty thousand rows each time is what made
    the first draft take minutes on a synthetic year. The index changes no
    answer, only how it is found: a test holds each indexed function to its
    scanning twin.
    """

    def __init__(self, bookings: List[Booking], nonrev: List[Booking]):
        self.phys_all = physical_rows(bookings, nonrev)
        self.rev_all = pilot.ledger_rows(bookings)
        self.comps_all = [b for b in nonrev if b.occupies and b.nights > 0]
        self._phys: Dict[dt.date, List[Booking]] = defaultdict(list)
        self._rev: Dict[dt.date, List[Booking]] = defaultdict(list)
        self._comps: Dict[dt.date, List[Booking]] = defaultdict(list)
        self._dep: Dict[dt.date, int] = defaultdict(int)
        self._arr: Dict[dt.date, int] = defaultdict(int)
        for b in self.phys_all:
            for k in range(b.nights):
                self._phys[b.arrival + dt.timedelta(days=k)].append(b)
            self._dep[b.departure] += b.rooms
            self._arr[b.arrival] += b.rooms
        for b in self.rev_all:
            for k in range(b.nights):
                self._rev[b.arrival + dt.timedelta(days=k)].append(b)
        for b in self.comps_all:
            for k in range(b.nights):
                self._comps[b.arrival + dt.timedelta(days=k)].append(b)
        self._stats: Dict[dt.date, dict] = {}

    def physical_on(self, night: dt.date) -> List[Booking]:
        return self._phys.get(night, [])

    def revenue_rows_on(self, night: dt.date) -> List[Booking]:
        """Every revenue row covering the night, whatever its status; on_books
        applies the booked_on, cancellation and no-show rules on top."""
        return self._rev.get(night, [])

    def comps_on(self, night: dt.date) -> List[Booking]:
        return self._comps.get(night, [])

    def departures(self, day: dt.date) -> int:
        return self._dep.get(day, 0)

    def arrivals(self, day: dt.date) -> int:
        return self._arr.get(day, 0)

    def stayovers(self, night: dt.date) -> int:
        return _stayovers(self.physical_on(night), night)

    def night_stats(self, night: dt.date) -> dict:
        """The settled counts a ratio window reads, once per night."""
        st = self._stats.get(night)
        if st is None:
            phys = self.physical_on(night)
            rev = [b for b in phys if b.target != "NONREV"]
            known = all(b.guests is not None for b in rev)
            st = {
                "rev_rooms": sum(b.rooms for b in rev),
                "guests": sum(b.guests for b in rev) if known else None,
                "breakfast": covers(rev, BREAKFAST_BOARDS),
                "dinner": covers(rev, DINNER_BOARDS),
                "phys_rooms": sum(b.rooms for b in phys),
                "stayovers": _stayovers(phys, night),
            }
            self._stats[night] = st
        return st


def actuals(bookings: List[Booking], nonrev: List[Booking], ledger: Ledger,
            night: dt.date, index: Optional[Index] = None) -> Dict[str, Optional[float]]:
    """What happened on `night`, every quantity in its own unit.

    rooms is the settled revenue figure the pilot scores against
    (baselines.actual). physical counts the rows, NONREV included. The three
    housekeeping counts are for the service day night + 1 and satisfy
    physical == departures + stayovers by construction.
    """
    nxt = night + dt.timedelta(days=1)
    if index is None:
        phys_all = physical_rows(bookings, nonrev)
        phys = rows_on(phys_all, night)
        rev = [b for b in phys if b.target != "NONREV"]
        return {
            "rooms": baselines.actual(ledger, night),
            "physical": sum(b.rooms for b in phys),
            "breakfast": covers(rev, BREAKFAST_BOARDS),
            "dinner": covers(rev, DINNER_BOARDS),
            "stayovers": _stayovers(phys_all, night),
            "departures": _departures(phys_all, nxt),
            "arrivals": _arrivals(phys_all, nxt),
        }
    st = index.night_stats(night)
    return {
        "rooms": baselines.actual(ledger, night),
        "physical": st["phys_rooms"],
        "breakfast": st["breakfast"],
        "dinner": st["dinner"],
        "stayovers": st["stayovers"],
        "departures": index.departures(nxt),
        "arrivals": index.arrivals(nxt),
    }


def on_books(rows: List[Booking], night: dt.date, asof: dt.date) -> List[Booking]:
    """Revenue rows on the books for `night` at the end of day `asof`.

    The walk books a row on its booked_on day, cancels it on its status_date
    (only when that date is known; an undated cancellation is never cancelled
    in the ledger either), snapshots after both, and releases a no-show only
    when it settles the arrival night the next day. So: entered on or before
    asof, covering the night, and not cancelled by a status_date on or before
    asof. A no-show is released from every night of its stay when the walk
    settles its arrival night, which happens at the end of the arrival day
    after that day's snapshot: so it is on the books through asof == arrival
    and off from the day after. The test ties the room count of this list to
    Ledger.otb_at on every record of a walk, and found this rule.
    """
    out = []
    for b in rows:
        if b.booked_on > asof or not (b.arrival <= night < b.departure):
            continue
        if b.status == "cancelled" and b.status_date is not None and b.status_date <= asof:
            continue
        if b.status == "no_show" and asof > b.arrival:
            continue
        out.append(b)
    return out


def booked_parts(rows: List[Booking], nonrev: List[Booking], night: dt.date,
                 asof: dt.date, index: Optional[Index] = None) -> Dict[str, Optional[float]]:
    """The booked part of each quantity for `night`, gross, counted from the
    rows' own board codes, guest counts and departure dates."""
    if index is None:
        rev = on_books(pilot.ledger_rows(rows), night, asof)
        comps = [b for b in nonrev if b.occupies and b.nights > 0 and b.booked_on <= asof
                 and b.arrival <= night < b.departure]
    else:
        rev = on_books(index.revenue_rows_on(night), night, asof)
        comps = [b for b in index.comps_on(night) if b.booked_on <= asof]
    return {
        "rooms": sum(b.rooms for b in rev),
        "breakfast": covers(rev, BREAKFAST_BOARDS),
        "dinner": covers(rev, DINNER_BOARDS),
        "stayovers": _stayovers(rev, night),
        "nonrev_stayovers": _stayovers(comps, night),
        "nonrev_rooms": sum(b.rooms for b in comps),
    }


def ratios(bookings: List[Booking], nonrev: List[Booking], ledger: Ledger,
           night: dt.date, lead: int, index: Optional[Index] = None) -> Optional[Dict[str, object]]:
    """The five ratios for `night` at `lead`, from the ten trailing same-weekday
    nights already settled on the forecast day (spec 6.2).

    Every ratio is a ratio of sums over the window, not a mean of ratios, so a
    quiet reference night does not weigh as much as a full one. survival is
    rooms that stayed over rooms on the books at this lead. breakfast_share
    and dinner_share are covers over guests; guests_per_room is guests over
    revenue rooms; stayover_share is physical rooms that stayed on over
    physical rooms. Any denominator of zero makes that ratio None; fewer than
    WINDOW_WEEKS reference nights makes the whole thing None, the rule table 1
    applies to a night a baseline cannot forecast.
    """
    refs = baselines.reference_nights(ledger, night, lead, WINDOW_WEEKS)
    if len(refs) < WINDOW_WEEKS:
        return None
    if index is None:
        index = Index(bookings, nonrev)
    stayed = otb = 0.0
    rooms = guests = bf = dn = 0
    phys_rooms = stay_rooms = 0
    guests_known = True
    for n in refs:
        stayed += baselines.actual(ledger, n) or 0.0
        otb += ledger.otb_at(n, lead) or 0
        st = index.night_stats(n)
        rooms += st["rev_rooms"]
        if st["guests"] is None:
            guests_known = False
        else:
            guests += st["guests"]
            bf += st["breakfast"]
            dn += st["dinner"]
        phys_rooms += st["phys_rooms"]
        stay_rooms += st["stayovers"]

    def _div(a, b):
        return None if not b else a / b

    return {
        "refs": refs,
        "survival": _div(stayed, otb),
        "breakfast_share": _div(bf, guests) if guests_known else None,
        "dinner_share": _div(dn, guests) if guests_known else None,
        "guests_per_room": _div(guests, rooms) if guests_known else None,
        "stayover_share": _div(stay_rooms, phys_rooms),
    }


def forecast_row(rec, bookings: List[Booking], nonrev: List[Booking], ledger: Ledger,
                 hotel, index: Optional[Index] = None) -> Optional[Dict[str, object]]:
    """Booked plus pickup for one (night, lead) (spec 6.2, decision 18).

    booked_stay = rooms on the books x s
    pickup      = rooms forecast - booked_stay, floored at zero
    booked + pickup = rooms forecast, which a test asserts on every row.

    The four derived quantities count their booked part from the rows' own
    board codes, guest counts and departure dates, times s, and multiply only
    the pickup by a trailing-window ratio. Housekeeping's physical rooms add
    the NONREV rooms already entered on the forecast day, which are a count
    and never a forecast.
    """
    if rec.forecast is None or rec.otb is None:
        return None
    night, lead, asof = rec.stay_date, rec.lead, rec.asof
    if index is None:
        index = Index(bookings, nonrev)
    r = ratios(bookings, nonrev, ledger, night, lead, index)
    if r is None or r["survival"] is None:
        return None
    s = min(1.0, r["survival"])
    parts = booked_parts(bookings, nonrev, night, asof, index)
    rooms = float(rec.forecast)
    booked_stay = parts["rooms"] * s
    pickup = max(0.0, rooms - booked_stay)
    gpr = r["guests_per_room"]

    def _covers(booked, share):
        if booked is None or gpr is None or share is None:
            return None
        return booked * s + pickup * share * gpr

    stay_share = r["stayover_share"]
    stayovers = None
    if stay_share is not None:
        stayovers = parts["stayovers"] * s + parts["nonrev_stayovers"] + pickup * stay_share
    physical = rooms + parts["nonrev_rooms"]
    nxt = night + dt.timedelta(days=1)
    return {
        "night": night, "lead": lead, "asof": asof,
        "rooms": rooms, "otb": rec.otb, "survival": s,
        "booked_stay": booked_stay, "pickup": pickup, "physical": physical,
        "breakfast": _covers(parts["breakfast"], r["breakfast_share"]),
        "dinner": _covers(parts["dinner"], r["dinner_share"]),
        "stayovers": stayovers,
        "departures": None if stayovers is None else physical - stayovers,
        "arrivals_booked": _arrivals(on_books(index.revenue_rows_on(nxt), nxt, asof), nxt),
        "ratios": {k: v for k, v in r.items() if k != "refs"},
    }


def walk_handover(res, cal, score_last: dt.date, progress: Optional[int] = None):
    """The pilot's walk, recording every lead 0 to 14 on every night from the
    first stay night, driven from the first stay night (plan decision 1)."""
    return pilot.walk(res.bookings, res.hotel, cal,
                      first_stay=res.first_stay, last_stay=res.last_stay,
                      score_first=res.first_stay, score_last=score_last,
                      marks=LEADS, drive_from=res.first_stay, progress=progress)


def forecast_rows(res, out, hotel, index: Optional[Index] = None) -> Dict[Tuple[dt.date, int], dict]:
    index = Index(res.bookings, res.nonrev) if index is None else index
    rows = {}
    for key, rec in out.records.items():
        row = forecast_row(rec, res.bookings, res.nonrev, res.ledger, hotel, index)
        if row is not None:
            rows[key] = row
    return rows
