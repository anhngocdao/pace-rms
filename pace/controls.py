"""Inventory controls: overbooking, minimum stay, and segment closures.

These are the levers that exist because rate alone cannot express the
decision.  Rate is a blunt instrument: raising it to protect a sold-out
Saturday also drives away the Thursday guest who would have paid full price.
Length of stay controls and segment closures cut finer.
"""

import datetime as dt
import math
from typing import Dict, Iterable, Optional, Sequence

from .config import SEGMENTS, Hotel
from .simulate import quoted_rate
from .unconstrain import norm_cdf


def remaining_cancel_probability(ledger, stay_date: dt.date, asof: dt.date) -> float:
    """Blended chance that a room now on the books gives itself back.

    Scaled by how far out we are: almost nobody cancels the night before, and
    a booking made six months out has most of its cancellation risk ahead of
    it.  Thirty days is used as the point where the full segment rate applies.
    """
    mix = ledger.segment_mix(stay_date)
    total = sum(max(0, v) for v in mix.values())
    if total <= 0:
        return 0.0
    lead = max(0, (stay_date - asof).days)
    decay = min(1.0, lead / 30.0)
    blended = sum(SEGMENTS[c].cancel_rate * max(0, n) for c, n in mix.items() if c in SEGMENTS)
    return (blended / total) * decay


def blended_no_show(ledger, stay_date: dt.date) -> float:
    mix = ledger.segment_mix(stay_date)
    total = sum(max(0, v) for v in mix.values())
    if total <= 0:
        return 0.02
    return sum(SEGMENTS[c].no_show_rate * max(0, n) for c, n in mix.items() if c in SEGMENTS) / total


def authorized_capacity(hotel: Hotel, adr: float, cancel_prob: float,
                        no_show_prob: float) -> int:
    """How many rooms to sell, given that some of them will hand themselves back.

    Newsvendor logic.  Selling one more room earns the contribution on it if
    someone hands a room back, and costs a walk if nobody does.  Authorise up
    to the point where the chance of walking anyone equals the critical
    ratio, and never past the configured hard cap.
    """
    rooms = hotel.rooms
    give_back = max(0.0, min(0.6, cancel_prob + no_show_prob))
    if give_back <= 1e-4:
        return rooms
    shows = 1.0 - give_back
    under = max(1.0, adr - hotel.variable_cost)
    critical = under / (under + hotel.walk_cost)

    hard_cap = int(rooms * (1.0 + hotel.max_overbook_pct))
    authorized = rooms
    for candidate in range(rooms + 1, hard_cap + 1):
        mean = candidate * shows
        sd = math.sqrt(max(1e-6, candidate * shows * (1.0 - shows)))
        p_walk = 1.0 - norm_cdf((rooms + 0.5 - mean) / sd)
        if p_walk > critical:
            break
        authorized = candidate
    return authorized


def min_length_of_stay(hotel: Hotel, arrival: dt.date, rates: Dict[dt.date, float],
                       bids: Dict[dt.date, float], max_los: Optional[int] = None) -> int:
    """The shortest stay that pays for the rooms it consumes.

    A stay is worth taking when the total net rate over its nights clears the
    total bid price over those nights.  On a compressed night whose rate has
    already hit the ceiling, a one night stay fails that test while a stay
    that reaches into the soft nights either side passes it.  That is the
    whole idea behind a minimum stay restriction, stated as arithmetic.
    """
    max_los = max_los or hotel.max_los
    retail = SEGMENTS["RETAIL"]
    rate_sum = 0.0
    bid_sum = 0.0
    for n in range(max_los):
        night = arrival + dt.timedelta(days=n)
        bar = rates.get(night)
        if bar is None:
            break
        rate_sum += bar * (1.0 - retail.commission) - hotel.variable_cost
        bid_sum += bids.get(night, 0.0)
        if rate_sum >= bid_sum:
            return n + 1
    return max_los + 1          # nothing clears: close to arrival


def closed_segments(hotel: Hotel, bar: float, bid: float,
                    refs: Dict[str, float]) -> frozenset:
    """Segments whose net rate no longer covers the value of the room.

    This is displacement analysis stated as one comparison.  A contracted
    corporate room at 155 net is worth taking when the last room is worth 90
    and worth refusing when it is worth 210, and the only thing that changed
    is the demand still to come.
    """
    closed = []
    for code, seg in SEGMENTS.items():
        net = quoted_rate(code, bar, refs[code]) * (1.0 - seg.commission) - hotel.variable_cost
        if net < bid:
            closed.append(code)
    # Never close every door: the highest paying segment always stays open.
    if len(closed) == len(SEGMENTS):
        best = max(SEGMENTS, key=lambda c: SEGMENTS[c].rate_multiplier * (1 - SEGMENTS[c].commission))
        closed = [c for c in closed if c != best]
    return frozenset(closed)
