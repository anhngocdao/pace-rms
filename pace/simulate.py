"""Synthetic market: demand generation and policy replay.

Two design choices make this more than a random number dump.

1.  The generator emits *requests*, not bookings.  Every request carries the
    guest willingness to pay, the length of stay, and the cancellation it is
    already fated to make.  The same request stream can therefore be replayed
    under different pricing policies and produce genuinely different revenue.
    That is what turns the backtest into a counterfactual instead of a
    re-scoring of fixed history.

2.  The engine never sees the request stream.  It reads the ledger, which
    only records what was accepted plus the denials a hotel could actually
    log.  Demand on sold-out or rate-fenced nights therefore arrives
    censored, exactly as it does in a real system.
"""

import datetime as dt
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional

from .calendar import EventCalendar, month_factor, span
from .config import EVENT_SENSITIVITY, SEGMENT_ORDER, SEGMENTS, Hotel
from .ledger import Controls, Ledger

# --------------------------------------------------------------------- random


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam < 25:
        target = math.exp(-lam)
        k, p = 0, 1.0
        while True:
            p *= rng.random()
            if p <= target or k > 500:
                return k
            k += 1
    return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))


def pick(rng: random.Random, weights) -> int:
    x = rng.random() * sum(weights)
    acc = 0.0
    for i, weight in enumerate(weights):
        acc += weight
        if x <= acc:
            return i
    return len(weights) - 1


# -------------------------------------------------------------------- demand


@dataclass
class Request:
    rid: int
    segment: str
    booked_on: dt.date
    arrival: dt.date
    los: int
    rooms: int
    wtp: float           # willingness to pay, per room night
    cancel_lead: int     # days before arrival this booking will cancel, -1 = never
    no_show: bool


def reference_rate(hotel: Hotel, cal: EventCalendar, d: dt.date, code: str) -> float:
    """What this segment would normally be quoted on this date.

    Used both by the generator (to centre willingness to pay) and by the
    engine (as the denominator of the price ratio in the elasticity model).
    The engine is allowed to know this because it is just seasonality times
    the segment discount, not a demand forecast.
    """
    seg = SEGMENTS[code]
    if not seg.floats_with_bar:
        # A contracted rate is agreed once, by season, and does not react to
        # what the city is doing that week.
        return hotel.base_rate * month_factor(d) * seg.rate_multiplier
    ev = cal.multiplier(d)
    lift = 1.0 + (ev - 1.0) * 0.45 * EVENT_SENSITIVITY[code]
    return hotel.base_rate * month_factor(d) * seg.rate_multiplier * lift


def quoted_rate(code: str, bar: float, ref: float) -> float:
    """What this segment is actually charged when the public rate is bar."""
    seg = SEGMENTS[code]
    return bar * seg.rate_multiplier if seg.floats_with_bar else ref


def generate_requests(hotel: Hotel, cal: EventCalendar, first_arrival: dt.date,
                      last_arrival: dt.date, seed: int = 20250115) -> List[Request]:
    rng = random.Random(seed)
    out: List[Request] = []
    rid = 0
    for a in span(first_arrival, last_arrival):
        mf = month_factor(a)
        ev = cal.multiplier(a)
        for code in SEGMENT_ORDER:
            seg = SEGMENTS[code]
            ev_lift = 1.0 + (ev - 1.0) * EVENT_SENSITIVITY[code]
            lam = hotel.base_daily_demand * seg.share * mf * seg.dow_weights[a.weekday()] * ev_lift
            lam *= math.exp(rng.gauss(0.0, 0.16))          # day level demand noise
            mean_block = (seg.block_size[0] + seg.block_size[1]) / 2.0
            lam /= mean_block                               # groups arrive in few, large requests
            for _ in range(poisson(rng, lam)):
                lead = int(rng.gammavariate(seg.lead_time_shape,
                                            seg.lead_time_mean / seg.lead_time_shape))
                lead = max(0, min(hotel.max_lead, lead))
                los = pick(rng, seg.los_weights) + 1
                rooms = (seg.block_size[0] if seg.block_size[0] == seg.block_size[1]
                         else rng.randint(seg.block_size[0], seg.block_size[1]))
                median = reference_rate(hotel, cal, a, code) * seg.wtp_premium
                wtp = rng.lognormvariate(math.log(median), seg.wtp_sigma)
                if rng.random() < seg.cancel_rate:
                    cancel_lead = int(rng.random() * max(1, lead))
                else:
                    cancel_lead = -1
                no_show = cancel_lead < 0 and rng.random() < seg.no_show_rate
                out.append(Request(rid, code, a - dt.timedelta(days=lead), a,
                                   los, rooms, wtp, cancel_lead, no_show))
                rid += 1
    out.sort(key=lambda r: (r.booked_on, r.rid))
    return out


# -------------------------------------------------------------------- replay


class Policy:
    """Anything that can quote a rate and set controls for future nights."""

    name = "policy"

    def controls(self, asof: dt.date, ledger: Ledger) -> Controls:
        raise NotImplementedError

    def observe(self, asof: dt.date, ledger: Ledger) -> None:
        """Called once a day after the ledger has been updated."""


def replay(hotel: Hotel, requests: List[Request], policy: Policy,
           first_day: dt.date, last_day: dt.date,
           first_stay: dt.date, last_stay: dt.date,
           ledger: Optional[Ledger] = None, seed: int = 7,
           progress: Optional[int] = None) -> Ledger:
    """Run one policy against one request stream, day by day."""
    ledger = ledger or Ledger(hotel, first_stay, last_stay)
    by_day: Dict[dt.date, List[Request]] = defaultdict(list)
    for r in requests:
        by_day[r.booked_on].append(r)
    cancels_on: Dict[dt.date, List] = defaultdict(list)

    days = span(first_day, last_day)
    for i, asof in enumerate(days):
        for hold in cancels_on.pop(asof, []):
            ledger.cancel(hold)

        ctrl = policy.controls(asof, ledger)

        todays = list(by_day.get(asof, ()))
        random.Random(seed + asof.toordinal()).shuffle(todays)
        for req in todays:
            hold = _attempt(ledger, hotel, req, ctrl, asof)
            if hold is not None and req.cancel_lead >= 0:
                when = req.arrival - dt.timedelta(days=req.cancel_lead)
                if when > asof:
                    cancels_on[when].append(hold)
                else:
                    cancels_on[asof + dt.timedelta(days=1)].append(hold)

        ledger.snapshot(asof, hotel.max_lead)
        if first_stay <= asof <= last_stay:
            ledger.settle(asof)
        policy.observe(asof, ledger)

        if progress and i % progress == 0:
            print("    %s  occ_today=%3d  bookings=%6d"
                  % (asof, ledger.rooms_on(asof), ledger.n_bookings), flush=True)
    return ledger


def _contracted(hotel: Hotel, night: dt.date, seg) -> float:
    return hotel.base_rate * month_factor(night) * seg.rate_multiplier


def _attempt(ledger: Ledger, hotel: Hotel, req: Request, ctrl: Controls, asof: dt.date):
    nights = [req.arrival + dt.timedelta(days=i) for i in range(req.los)]
    if nights[-1] > ledger.last_stay or req.arrival < asof:
        return None

    if req.los < ctrl.mlos.get(req.arrival, 1):
        ledger.deny(asof, req, "mlos")
        return None
    if ctrl.cta.get(req.arrival, False):
        ledger.deny(asof, req, "cta")
        return None
    for n in nights:
        if req.segment in ctrl.closed.get(n, frozenset()):
            ledger.deny(asof, req, "segment_closed")
            return None
    for n in nights:
        cap = ctrl.authorized.get(n, hotel.rooms)
        if ledger.rooms_on(n) + req.rooms > cap:
            ledger.deny(asof, req, "capacity")
            return None

    seg = SEGMENTS[req.segment]
    if seg.floats_with_bar:
        bar = sum(ctrl.rate.get(n, hotel.base_rate) for n in nights) / len(nights)
        quoted = bar * seg.rate_multiplier
    else:
        quoted = sum(_contracted(hotel, n, seg) for n in nights) / len(nights)
    if req.wtp < quoted:
        ledger.deny(asof, req, "price")
        return None

    hold = ledger.book(asof, req, quoted)
    hold.no_show = req.no_show
    return hold
