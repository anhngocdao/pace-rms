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
import json
import os
from collections import defaultdict
from statistics import fmean
from typing import Dict, List, Optional, Tuple

from . import baselines
from . import hotelconfig as HC
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


_INDEX: Dict[int, Index] = {}


def index_for(res) -> Index:
    """One index per ingest result, built on first use. run_proof and
    run_forward clear it, so a process that runs twice never reads a stale
    index for a new log."""
    idx = _INDEX.get(id(res))
    if idx is None:
        idx = Index(res.bookings, res.nonrev)
        _INDEX[id(res)] = idx
    return idx


def excluded(night: dt.date, weeks) -> bool:
    """settings.json excluded_weeks, as (first, last) date pairs, inclusive."""
    return any(a <= night <= b for a, b in weeks)


def _actual_of(res, night: dt.date, quantity: str):
    return actuals(res.bookings, res.nonrev, res.ledger, night, index_for(res)).get(quantity)


def errors(rows: Dict[Tuple[dt.date, int], dict], res, nights,
           actual_of=_actual_of) -> Dict[Tuple[str, int], List[float]]:
    """forecast - actual, per (quantity, lead), over `nights`; a row or an
    actual that is None contributes nothing, never a zero."""
    out: Dict[Tuple[str, int], List[float]] = {}
    wanted = set(nights)
    for (night, lead), row in rows.items():
        if night not in wanted:
            continue
        for q in QUANTITIES:
            f = row.get(q)
            if f is None:
                continue
            a = actual_of(res, night, q)
            if a is None:
                continue
            out.setdefault((q, lead), []).append(float(f) - float(a))
    return out


def bands(errs: Dict[Tuple[str, int], List[float]]) -> Dict[Tuple[str, int], Optional[dict]]:
    """Empirical p10 / p50 / p90 of the error, or None below the floor."""
    out = {}
    for key, values in errs.items():
        if len(values) < MIN_BAND_NIGHTS:
            out[key] = None
            continue
        out[key] = {"n": len(values),
                    "p10": pilot.percentile(values, P_LO),
                    "p50": pilot.percentile(values, P_MID),
                    "p90": pilot.percentile(values, P_HI)}
    return out


def band_of(point: float, band: Optional[dict]) -> Optional[Tuple[float, float, float]]:
    """The printed band for a point forecast.

    Errors are forecast minus actual, so the actual sits at the forecast
    minus the error: the band's low end is point - p90 (where the forecast
    ran highest above the actual) and its high end point - p10. With a
    symmetric error the two forms agree; with a biased one only this form
    puts the band where the actual lands, and a test with every error at +5
    holds it there."""
    if band is None:
        return None
    return (point - band["p90"], point - band["p50"], point - band["p10"])


def coverage(rows, res, band_table, nights, actual_of=_actual_of) -> Dict[Tuple[str, int], dict]:
    """Share of scored nights whose actual fell inside [lo, hi]."""
    wanted = set(nights)
    counts: Dict[Tuple[str, int], List[int]] = {}
    for (night, lead), row in rows.items():
        if night not in wanted:
            continue
        for q in QUANTITIES:
            f = row.get(q)
            band = band_table.get((q, lead))
            if f is None or band is None:
                continue
            a = actual_of(res, night, q)
            if a is None:
                continue
            lo, _, hi = band_of(float(f), band)
            n_in = counts.setdefault((q, lead), [0, 0])
            n_in[0] += 1
            if lo <= float(a) <= hi:
                n_in[1] += 1
    return {key: {"n": n, "inside": k, "share": (k / n) if n else None}
            for key, (n, k) in counts.items()}


MIN_RATE_NIGHTS = 30


def rate(k: int, n: int) -> Optional[float]:
    """k of n, or None below the same 30-night floor that governs bands: a
    recall of 3 of 4 is a number about four nights, not about the hotel."""
    if n < MIN_RATE_NIGHTS:
        return None
    return k / n


def sellout_cut(res, night: dt.date, asof: Optional[dt.date] = None) -> float:
    """sellout_threshold x capacity_on, the pre-registered cut (spec 7.1)."""
    return res.hotel.sellout_threshold * pilot.capacity_on(res.hotel, res.nonrev, night, asof)


def sold_out(res, night: dt.date) -> bool:
    a = baselines.actual(res.ledger, night)
    return a is not None and a >= sellout_cut(res, night)


def notice_of(warned: Dict[int, bool]) -> Tuple[Optional[int], bool]:
    """The deepest lead L such that the warning was on at every lead from L
    down to 0; (None, False) when it was off at lead 0; (14, True) when it was
    on all the way, which is censored: the walk records nothing deeper."""
    if not warned.get(0):
        return None, False
    deepest = 0
    for lead in LEADS[1:]:
        if warned.get(lead):
            deepest = lead
        else:
            break
    return deepest, deepest == HORIZON


def alarm_scores(rows, res, band_table, nights) -> dict:
    """Spec 7.1: precision, recall and notice of the sell-out warning, the
    event count, and the nights the log cannot speak for."""
    scored = sorted(set(nights))
    events = {d: sold_out(res, d) for d in scored}
    per_lead = {}
    warned_by_night: Dict[dt.date, Dict[int, bool]] = {d: {} for d in scored}
    for lead in LEADS:
        warned = hits = n_events = 0
        for d in scored:
            row = rows.get((d, lead))
            band = band_table.get(("rooms", lead))
            if row is None or band is None:
                continue
            _, _, hi = band_of(row["rooms"], band)
            on = hi >= sellout_cut(res, d, row["asof"])
            warned_by_night[d][lead] = on
            if events[d]:
                n_events += 1
            if on:
                warned += 1
                if events[d]:
                    hits += 1
        per_lead[lead] = {"warned": warned, "hits": hits, "events": n_events,
                          "precision": rate(hits, warned), "recall": rate(hits, n_events)}
    values, censored = [], 0
    for d in scored:
        if not events[d] or not warned_by_night[d]:
            continue
        n, cens = notice_of(warned_by_night[d])
        if n is None:
            continue
        values.append(n)
        censored += 1 if cens else 0
    gap = (pilot.full_night_gap(res.bookings, res.ledger, res.hotel, scored[0], scored[-1])
           if scored else {"full_but_invisible": 0})
    return {
        "threshold": res.hotel.sellout_threshold,
        "nights": len(scored),
        "events": sum(1 for d in scored if events[d]),
        "per_lead": per_lead,
        "notice": {"median": (pilot.percentile([float(v) for v in values], 0.5) if values else None),
                   "censored": censored, "scored": len(values), "values": values},
        "cannot_speak": gap["full_but_invisible"],
    }


def tier_scores(point: float, band: dict, actuals_list: List[float]) -> dict:
    """Both tiers scored as promises: short share, and mean over-prep on the
    days that were not short (spec 7.2)."""
    _, mid, hi = band_of(point, band)
    out = {}
    for name, tier in (("covering", hi), ("balanced", mid)):
        short = [a for a in actuals_list if a > tier]
        over = [tier - a for a in actuals_list if a <= tier]
        out[name] = {"tier": tier, "short": len(short), "n": len(actuals_list),
                     "short_share": rate(len(short), len(actuals_list)),
                     "over_mean": (fmean(over) if over else None)}
    return out


def kitchen_scores(rows, res, band_table, nights) -> dict:
    """Spec 7.2: MAE in covers per meal per lead, and both tiers as promises."""
    scored = sorted(set(nights))
    out = {}
    for meal in ("breakfast", "dinner"):
        out[meal] = {}
        for lead in LEADS:
            band = band_table.get((meal, lead))
            pairs = []
            for d in scored:
                row = rows.get((d, lead))
                if row is None or row.get(meal) is None or band is None:
                    continue
                a = _actual_of(res, d, meal)
                if a is None:
                    continue
                pairs.append((row[meal], float(a)))
            if not pairs:
                out[meal][lead] = None
                continue
            short_c = short_b = 0
            over_c = over_b = 0.0
            nc = nb = 0
            for f, a in pairs:
                _, mid, hi = band_of(f, band)
                if a > hi:
                    short_c += 1
                else:
                    over_c += hi - a
                    nc += 1
                if a > mid:
                    short_b += 1
                else:
                    over_b += mid - a
                    nb += 1
            n = len(pairs)
            out[meal][lead] = {
                "n": n, "mae": fmean(abs(f - a) for f, a in pairs),
                "covering": {"short": short_c, "short_share": rate(short_c, n),
                             "over_mean": (over_c / nc) if nc else None},
                "balanced": {"short": short_b, "short_share": rate(short_b, n),
                             "over_mean": (over_b / nb) if nb else None},
            }
    return out


def roster_short(point: float, band: dict, actuals_list: List[float]) -> Tuple[int, Optional[float]]:
    """Rostering at the bottom of the band: days short-staffed, and by how
    many rooms on those days."""
    lo, _, _ = band_of(point, band)
    short = [a - lo for a in actuals_list if a > lo]
    return len(short), (fmean(short) if short else None)


def housekeeping_scores(rows, res, band_table, nights) -> dict:
    """Spec 7.3: departures and stayovers scored apart, and the p10 roster
    line on departures, the expensive half."""
    scored = sorted(set(nights))
    out = {}
    for lead in LEADS:
        cell = {}
        for q in ("departures", "stayovers"):
            band = band_table.get((q, lead))
            pairs = []
            for d in scored:
                row = rows.get((d, lead))
                if row is None or row.get(q) is None:
                    continue
                a = _actual_of(res, d, q)
                if a is None:
                    continue
                pairs.append((row[q], float(a)))
            cell[q] = {"n": len(pairs), "mae": (fmean(abs(f - a) for f, a in pairs) if pairs else None)}
            if q == "departures" and band is not None and pairs:
                gaps = [a - band_of(f, band)[0] for f, a in pairs if a > band_of(f, band)[0]]
                cell["roster"] = {"n": len(pairs), "short": len(gaps),
                                  "short_share": rate(len(gaps), len(pairs)),
                                  "short_rooms_mean": (fmean(gaps) if gaps else None)}
        out[lead] = cell
    return out


DAY_CONVENTION = (
    "A stay night N is the night beginning on calendar day N. Breakfast on morning D is "
    "served to the guests who stayed night D - 1. Dinner on evening D is served to the "
    "half-board and full-board guests staying night D. Departures on day D are rooms whose "
    "last stay night was D - 1; stayovers on day D are rooms covering both night D - 1 and "
    "night D; arrivals on day D have first stay night D. Rooms occupied on night D - 1 equal "
    "departures on day D plus stayovers on day D, and a test asserts it on every day.")
NO_LIFT = ("No number on this page is a revenue lift. The handover reads a forecast and "
           "translates it into operational language; it never prices, never authorises and "
           "never sends.")
SURVIVAL_ASSUMPTION = (
    "One survival rate serves every board: a booking cancels whole, so the rate is exact for "
    "rooms and assumes that cancelling bookings carry the same board mix and the same guests "
    "per room as the ones that stay. Survival by board code has not been measured; survival by "
    "segment has, and its spread at lead 7 is under one point at H1 and under two at H2.")
COVERS_POPULATION = (
    "Covers are counted on revenue rows: the engine forecasts revenue rooms, so a comped room's "
    "guests are not in the forecast and are not in the actual either. The physical room count on "
    "the housekeeping tab does include them.")
ROSTER_LIMIT = ("Pace does not know how many minutes a room takes. It reports rooms by kind; "
                "converting to people needs the hotel's own minutes per room.")
FORWARD_NOTE = (
    "Nothing on this page is checkable yet: these nights have not happened. Run live, a "
    "front-desk warning changes the outcome it predicts, and a warning that worked scores as a "
    "false alarm, so forward warnings are not scored at all. The proof run beside this page "
    "was measured on nights nobody interfered with.")


def _dates(pair):
    return dt.date.fromisoformat(pair[0]), dt.date.fromisoformat(pair[1])


def _band(point, band):
    """band_of, or None when either the point or the band is missing."""
    if point is None or band is None:
        return None
    return band_of(point, band)


def undefined_board_share(bookings: List[Booking]) -> dict:
    """spec 4.2: Undefined as a share of stayed revenue room nights."""
    rows = [b for b in pilot.ledger_rows(bookings) if b.occupies]
    total = sum(b.rooms * b.nights for b in rows)
    und = sum(b.rooms * b.nights for b in rows if (b.meal or "").strip() == "Undefined")
    return {"room_nights": und, "of": total, "share": (und / total) if total else None}


def _band_table_json(table) -> dict:
    out = {q: {} for q in QUANTITIES}
    for (q, lead), cell in table.items():
        out[q][str(lead)] = cell
    for q in QUANTITIES:
        for lead in LEADS:
            out[q].setdefault(str(lead), None)
    return out


def _coverage_json(cov) -> dict:
    out = {q: {} for q in QUANTITIES}
    for (q, lead), cell in cov.items():
        out[q][str(lead)] = cell
    return out


def _day_rows(rows, res, band_table, nights, lead=7) -> Tuple[list, list, list]:
    """The scoring-window rows at one lead, the lead a department plans a week
    at, shaped for the three tabs. Every other lead's bands and scores are in
    the payload's own blocks."""
    front, kitchen, housekeeping = [], [], []
    for d in sorted(nights):
        row = rows.get((d, lead))
        if row is None:
            continue
        band_rooms = _band(row["rooms"], band_table.get(("rooms", lead)))
        cut = sellout_cut(res, d, row["asof"])
        front.append({"night": d.isoformat(), "lead": lead, "otb": row["otb"], "rooms": row["rooms"],
                      "band": band_rooms, "cut": cut,
                      "authorised": int(round(res.hotel.rooms * (1 + res.hotel.max_overbook_pct))),
                      "warned": (band_rooms is not None and band_rooms[2] >= cut),
                      "sold_out": sold_out(res, d)})
        day = (d + dt.timedelta(days=1)).isoformat()
        kitchen.append({"day": day, "night": d.isoformat(), "lead": lead,
                        "breakfast": {"point": row["breakfast"],
                                      "band": _band(row["breakfast"], band_table.get(("breakfast", lead)))},
                        "dinner": {"evening": d.isoformat(), "point": row["dinner"],
                                   "band": _band(row["dinner"], band_table.get(("dinner", lead)))}})
        housekeeping.append({"day": day, "night": d.isoformat(), "lead": lead,
                             "departures": {"point": row["departures"],
                                            "band": _band(row["departures"], band_table.get(("departures", lead)))},
                             "stayovers": {"point": row["stayovers"],
                                           "band": _band(row["stayovers"], band_table.get(("stayovers", lead)))},
                             "arrivals_booked": row["arrivals_booked"]})
    return front, kitchen, housekeeping


def run_proof(csv_path: str, hotel_json_path: str, settings_path: str, out_dir: str,
              progress: Optional[int] = None) -> dict:
    """Proof mode: replay the history, measure the bands on the warm-up window,
    answer and score on the scoring window, write the payload and the page."""
    from . import handoverpage
    settings = pilot._read_settings(settings_path)
    digest = pilot.settings_digest(settings_path)
    cfg = HC.load_hotel_json(hotel_json_path)
    code = pilot.hotel_code(cfg)
    pilot.claim_process(hotel_json_path)
    res = ingest.load(csv_path, hotel_json_path, seed=int(settings.get("seed", ingest.DEFAULT_SEED)))
    cal = HC.event_calendar(res.cfg)
    warm_first, warm_last = _dates(settings["warmup_window"])
    score_first, score_last = _dates(settings["scoring_window"])
    weeks = [_dates(p) for p in settings.get("excluded_weeks", [])]
    score_last = min(score_last, res.last_stay)
    out = walk_handover(res, cal, score_last, progress=progress)
    rows = forecast_rows(res, out, res.hotel)
    all_nights = sorted({k[0] for k in rows})
    band_nights = [d for d in all_nights if warm_first <= d <= warm_last and d < score_first
                   and not excluded(d, weeks)]
    score_nights = [d for d in all_nights if score_first <= d <= score_last]
    table = bands(errors(rows, res, band_nights))
    cov = coverage(rows, res, table, score_nights)
    front, kitchen, housekeeping = _day_rows(rows, res, table, score_nights)
    rep = res.report
    payload = {
        "mode": "proof", "label": "full",
        "hotel": {"code": code, "name": res.cfg.name, "rooms": res.hotel.rooms,
                  "rooms_inferred": res.inference is not None,
                  "sellout_threshold": res.hotel.sellout_threshold,
                  "max_overbook_pct": res.hotel.max_overbook_pct},
        "prereg": {"settings_path": settings_path, "settings_sha256": digest,
                   "sha256_matches_the_recorded_one": digest == pilot.PREREG_SHA256},
        "windows": {"first_stay": res.first_stay.isoformat(), "last_stay": res.last_stay.isoformat(),
                    "band_first": (band_nights[0].isoformat() if band_nights else None),
                    "band_last": (band_nights[-1].isoformat() if band_nights else None),
                    "band_nights": len(band_nights),
                    "score_first": score_first.isoformat(), "score_last": score_last.isoformat(),
                    "excluded_weeks": settings.get("excluded_weeks", [])},
        "day_convention": DAY_CONVENTION,
        "guests": {"column_present": any(b.guests is not None for b in res.bookings),
                   "guests_zero": int(rep.warnings.get("guests_zero", 0)),
                   "children_missing_note": next((n for n in rep.notes if "children NA" in n), None)},
        "undefined_board_share": undefined_board_share(res.bookings),
        "bands": _band_table_json(table),
        "coverage": _coverage_json(cov),
        "front_desk": {"nights": front, "band_nights": [d.isoformat() for d in band_nights],
                       "alarm": alarm_scores(rows, res, table, score_nights),
                       "reads": "revenue rooms: physical occupancy less the NONREV rows, the quantity "
                                "capacity_on nets against on both sides of the alarm"},
        "kitchen": {"days": kitchen, "scores": kitchen_scores(rows, res, table, score_nights),
                    "survival_assumption": SURVIVAL_ASSUMPTION, "covers_population": COVERS_POPULATION},
        "housekeeping": {"days": housekeeping, "scores": housekeeping_scores(rows, res, table, score_nights),
                         "reads": "physical rooms, NONREV included: a comped room is still stripped and made up",
                         "limit": ROSTER_LIMIT},
        "walk": {"days": out.days, "seconds": round(out.seconds, 1), "cpu_seconds": round(out.cpu_seconds, 1),
                 "records": len(out.records), "rows": len(rows)},
        "notes": [NO_LIFT],
    }
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "handover-%s.json" % code.lower())
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True, default=str)
    payload["_json_path"] = path
    payload["_html_path"] = handoverpage.write(out_dir, payload)
    return payload
