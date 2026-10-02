"""Table 2: the rate the engine would have published against the rate that was paid.

This is a comparison of two prices and nothing more.  Demand at any other rate
was never observed, so a gap in either direction is not revenue forgone or
revenue won; it is a difference between a number the engine computed and a
number the hotel charged.

Two structural facts are printed beside every row rather than left for a reader
to discover.  The rate band was frozen on the first twelve months and the ladder
steps up from the floor, so the top rung is below the ceiling and a night at the
seasonal peak has nowhere left to go: at H1 the top rung is 188.02 against a
ceiling of 189.09.  And controls() re-solves on a cadence, so the rate read at a
mark is the rate that was published then, while the solve behind it may be
several days older.
"""
import datetime as dt
from collections import Counter, defaultdict
from statistics import median
from typing import Dict, List, Optional, Tuple

from . import ingest
from . import pilot

# Pre-registered in data/antonio/settings.json as rate_windows and
# basket_trim_percentiles, and written out here because pace/ must not read
# data/: a hotel running the ingest has the package and not the pilot's
# fixtures.  The same rule pilot.py's own window constants follow, and the same
# guard: tests/test_pilot.py's PreRegistration pins both of these back to the
# file they were pre-registered in, so the second copy cannot drift from it.
RATE_WINDOWS = {"60": (75, 45), "30": (40, 21), "14": (21, 7), "7": (10, 4)}
PRICED_SEGMENTS = ("RETAIL", "OTA")
TRIM = (0.01, 0.99)          # settings.json basket_trim_percentiles

TABLE2_NOTE = (
    "This is a comparison of two prices, not evidence of revenue. Demand at any "
    "rate other than the one charged was never observed, so a gap in either "
    "direction says nothing about what the hotel would have earned.")
OTA_NOTE = (
    "If the Online TA adr in this dataset is a net rate rather than a sell rate, "
    "every OTA row in the basket is low by the commission and the OTA side of the "
    "gap is biased by a constant this data cannot measure.")
BAND_NOTE_FMT = (
    "The rate band was frozen on the first twelve months and the ladder steps up "
    "from the floor, so its top rung is %.2f against a ceiling of %.2f and the "
    "ceiling test can never fire. %d of %d scored nights are pinned on the top "
    "rung. Where the gap is largest at the seasonal peak, that is an artefact of "
    "a frozen band, not a property of the engine.")
LEAD_LABEL_NOTE = (
    "controls() re-solves on a cadence, 14 days beyond lead 90 and 1 day inside "
    "lead 14, so the rate at a mark is the rate that was published on that day "
    "while the solve behind it can be older. The median and worst age of that "
    "solve are printed per mark.")
MEAL_NOTE = (
    "Rows are split by board because the audit found half board steadily above bed "
    "and breakfast at both hotels, 94.25 against 66.00 at H1 and 114.40 against "
    "100.00 at H2, so the rate appears to include board. A comparison that does "
    "not hold board constant compares two different products.")
MARK60_NOTE = (
    "Section 5 names the 60 mark for H1. It is shown for H2 as well because the "
    "pre-registered rate windows are not hotel specific and H2's median lead is 74 "
    "days against H1's 57.")


def top_room_type(bookings, first: dt.date, last: dt.date) -> Tuple[str, float]:
    """The room type most of the priced business occupied, and its share."""
    counts = Counter()
    for b in bookings:
        if b.target not in PRICED_SEGMENTS or not b.occupies or b.nights == 0:
            continue
        for k in range(b.nights):
            night = b.arrival + dt.timedelta(days=k)
            if first <= night <= last:
                counts[b.room_type] += 1
    total = sum(counts.values())
    if not total:
        return "", 0.0
    code, n = counts.most_common(1)[0]
    return code, n / float(total)


def basket(bookings, night: dt.date, lo: int, hi: int, room_type: str,
           meal: Optional[str] = None) -> List[float]:
    """Rates paid for `night` by guests who booked between `lo` and `hi` days out.

    The lead is measured to the night being priced, not to the arrival, because
    the rate the engine published at that lead was a rate for that night; for a
    one-night stay the two are the same number. Only rows that occupied the
    night count, so the basket is what was realised rather than what was quoted
    and later cancelled. Trimmed at the 1st and 99th percentile: across the two
    hotels' files exactly one rate is negative, -6.38 at H1, and exactly one is
    5,400, at H2. The negative one is already out on the `rate <= 0` line
    above, so the trim is there for the other end.
    """
    out = []
    for b in bookings:
        if b.target not in PRICED_SEGMENTS or not b.occupies or b.nights == 0:
            continue
        if b.rate <= 0 or b.room_type != room_type:
            continue
        if meal is not None and b.meal != meal:
            continue
        if not (b.arrival <= night < b.departure):
            continue
        lead = (night - b.booked_on).days
        if hi <= lead <= lo:
            out.append(float(b.rate))
    if len(out) < 3:
        return out
    low = pilot.percentile(out, TRIM[0])
    high = pilot.percentile(out, TRIM[1])
    return [v for v in out if low <= v <= high]


def nightly_index(bookings) -> Dict[dt.date, List]:
    """The priced rows that occupied each night, filed under that night.

    `basket` re-reads the whole log for every night, every mark and every board
    code; on H1 that is forty thousand rows walked some ten thousand times.
    Every filter `basket` applies is a property of one row except the night
    test, so handing it the rows already filed under a night returns exactly
    the list it would have returned from the whole log, and `table2` builds
    this once and slices it.  The filters repeated here are the row-wise ones
    only: what is a basket row and what is not stays `basket`'s decision.
    """
    out: Dict[dt.date, List] = defaultdict(list)
    for b in bookings:
        if b.target not in PRICED_SEGMENTS or not b.occupies or b.nights == 0:
            continue
        for k in range(b.nights):
            out[b.arrival + dt.timedelta(days=k)].append(b)
    return out


def quartiles(values) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if not values:
        return None, None, None
    return (pilot.percentile(values, 0.25), median(values), pilot.percentile(values, 0.75))


def _cell(rows) -> dict:
    """rows: list of (published, realised)."""
    if not rows:
        return {"n": 0, "median_gap": None, "p25": None, "p75": None,
                "median_published": None, "median_realised": None}
    gaps = [p - r for p, r in rows]
    p25, mid, p75 = quartiles(gaps)
    return {"n": len(rows), "median_gap": mid, "p25": p25, "p75": p75,
            "median_published": median([p for p, _ in rows]),
            "median_realised": median([r for _, r in rows])}


def table2(walk_result, bookings, nonrev, hotel, code: str, seasons: Dict[int, str],
           score_first: dt.date, score_last: dt.date) -> dict:
    room_type, room_share = top_room_type(bookings, score_first, score_last)
    meals = sorted(set(b.meal for b in bookings
                       if b.target in PRICED_SEGMENTS and b.occupies and b.meal))
    top_rung = hotel.rate_ladder()[-1]
    by_night = nightly_index(bookings)

    cells: Dict[str, dict] = {}
    pinned: Dict[str, dict] = {}
    solve_age: Dict[str, dict] = {}
    pinned_total = 0
    scored_total = 0

    for mark in sorted(RATE_WINDOWS, key=lambda k: -int(k)):
        lo, hi = RATE_WINDOWS[mark]
        lead = int(mark)
        allrows, by_meal, by_season = [], defaultdict(list), defaultdict(list)
        with_comp, without_comp = [], []
        ages, on_top, scored = [], 0, 0
        d = score_first
        while d <= score_last:
            rec = walk_result.records.get((d, lead))
            d_next = d + dt.timedelta(days=1)
            if rec is None or rec.published_rate is None or not rec.engine_ready:
                d = d_next
                continue
            tonight = by_night.get(d, ())
            paid = basket(tonight, d, lo, hi, room_type)
            if not paid:
                d = d_next
                continue
            pair = (float(rec.published_rate), float(median(paid)))
            allrows.append(pair)
            by_season[seasons[d.month]].append(pair)
            scored += 1
            if abs(pair[0] - top_rung) < 1e-9:
                on_top += 1
            if rec.solved_on is not None:
                ages.append((rec.asof - rec.solved_on).days)
            if ingest.nonrev_on(nonrev, d, rec.asof) > 0:
                with_comp.append(pair)
            else:
                without_comp.append(pair)
            for meal in meals:
                one = basket(tonight, d, lo, hi, room_type, meal)
                if one:
                    by_meal[meal].append((pair[0], float(median(one))))
            d = d_next

        cells[mark] = {
            "all": _cell(allrows),
            "by_meal": {m: _cell(rows) for m, rows in sorted(by_meal.items())},
            "by_season": {s: _cell(rows) for s, rows in sorted(by_season.items())},
            "nonrev_nights": _cell(with_comp),
            "clean_nights": _cell(without_comp),
        }
        pinned[mark] = {"n": scored, "n_pinned": on_top,
                        "share": (on_top / float(scored)) if scored else None}
        solve_age[mark] = {"median": (median(ages) if ages else None),
                           "max": (max(ages) if ages else None), "n": len(ages)}
        pinned_total += on_top
        scored_total += scored

    band_note = BAND_NOTE_FMT % (top_rung, hotel.rate_ceiling, pinned_total, scored_total)
    notes = [TABLE2_NOTE, band_note, OTA_NOTE, MEAL_NOTE, LEAD_LABEL_NOTE]
    if code.upper() != "H1":
        notes.append(MARK60_NOTE)
    return {
        "marks": sorted(RATE_WINDOWS, key=lambda k: -int(k)),
        "windows": {k: list(v) for k, v in RATE_WINDOWS.items()},
        "room_type": room_type, "room_type_share": room_share, "meals": meals,
        "top_rung": top_rung, "rate_ceiling": hotel.rate_ceiling,
        "rate_floor": hotel.rate_floor,
        "cells": cells, "pinned": pinned,
        "pinned_overall": {"n": scored_total, "n_pinned": pinned_total,
                           "share": (pinned_total / float(scored_total)) if scored_total else None},
        "band_note": band_note, "solve_age_days": solve_age, "notes": notes,
    }
