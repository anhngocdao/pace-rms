"""The booking ledger: the only thing the engine is allowed to read.

This is the stand-in for a property management system.  It knows what was
booked, at what rate, by which segment, and what the house looked like on
every past day.  It does not know what demand was turned away on price,
because no real PMS knows that either.  That asymmetry is the whole reason
the unconstraining step exists.
"""

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional

from .config import SEGMENTS, Hotel

# Denial reasons the hotel can actually observe and log.  A guest who looked
# at the rate and quietly closed the tab is not one of them.
OBSERVABLE_DENIALS = ("capacity", "mlos", "cta", "segment_closed")


@dataclass
class Controls:
    """What the revenue manager has decided, per stay date."""

    rate: Dict[dt.date, float] = field(default_factory=dict)
    authorized: Dict[dt.date, int] = field(default_factory=dict)
    mlos: Dict[dt.date, int] = field(default_factory=dict)
    cta: Dict[dt.date, bool] = field(default_factory=dict)
    closed: Dict[dt.date, FrozenSet] = field(default_factory=dict)


@dataclass
class Hold:
    rid: int
    segment: str
    rooms: int
    rate: float          # gross rate per room night
    arrival: dt.date
    los: int
    booked_on: dt.date


class Ledger:
    def __init__(self, hotel: Hotel, first_stay: dt.date, last_stay: dt.date):
        self.hotel = hotel
        self.first_stay = first_stay
        self.last_stay = last_stay
        self.occ: Dict[dt.date, int] = defaultdict(int)
        self.holds: Dict[dt.date, List[Hold]] = defaultdict(list)
        self.seg_rooms: Dict[dt.date, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.seg_revenue: Dict[dt.date, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.snapshots: Dict[dt.date, Dict[int, int]] = defaultdict(dict)
        self.first_booked: Dict[dt.date, Dict[int, int]] = defaultdict(dict)
        self.denials: List[dict] = []
        self.settled: Dict[dt.date, dict] = {}
        self.arrivals_on: Dict[dt.date, List[Hold]] = defaultdict(list)
        self.n_bookings = 0
        self.n_cancels = 0

    # ---------------------------------------------------------------- reads

    def rooms_on(self, d: dt.date) -> int:
        return self.occ.get(d, 0)

    def segment_mix(self, d: dt.date) -> Dict[str, int]:
        return dict(self.seg_rooms.get(d, {}))

    def adr_on(self, d: dt.date) -> float:
        rooms = self.rooms_on(d)
        if rooms <= 0:
            return 0.0
        rev = sum(self.seg_revenue.get(d, {}).values())
        return rev / rooms

    def otb_at(self, stay_date: dt.date, lead: int) -> Optional[int]:
        return self.snapshots.get(stay_date, {}).get(lead)

    def observable_denials(self, stay_date: dt.date) -> int:
        return sum(
            d["rooms"] for d in self.denials
            if d["arrival"] == stay_date and d["reason"] in OBSERVABLE_DENIALS
        )

    # --------------------------------------------------------------- writes

    def deny(self, asof: dt.date, req, reason: str) -> None:
        self.denials.append({
            "asof": asof, "arrival": req.arrival, "segment": req.segment,
            "rooms": req.rooms, "los": req.los, "reason": reason,
        })

    def book(self, asof: dt.date, req, rate: float) -> Hold:
        hold = Hold(req.rid, req.segment, req.rooms, rate, req.arrival, req.los, asof)
        for i in range(req.los):
            n = req.arrival + dt.timedelta(days=i)
            self.holds[n].append(hold)
            self.occ[n] += req.rooms
            self.seg_rooms[n][req.segment] += req.rooms
            self.seg_revenue[n][req.segment] += rate * req.rooms
        self.arrivals_on[req.arrival].append(hold)
        self.n_bookings += 1
        return hold

    def release(self, hold: Hold, from_night: Optional[dt.date] = None) -> None:
        """Remove a hold from the house, optionally only from a night onward."""
        for i in range(hold.los):
            n = hold.arrival + dt.timedelta(days=i)
            if from_night is not None and n < from_night:
                continue
            bucket = self.holds.get(n)
            if not bucket or hold not in bucket:
                continue
            bucket.remove(hold)
            self.occ[n] -= hold.rooms
            self.seg_rooms[n][hold.segment] -= hold.rooms
            self.seg_revenue[n][hold.segment] -= hold.rate * hold.rooms

    def cancel(self, hold: Hold) -> None:
        self.release(hold)
        self.n_cancels += 1

    def snapshot(self, asof: dt.date, horizon: int) -> None:
        """Freeze the on-the-books picture for every future stay date."""
        for lead in range(0, horizon + 1):
            d = asof + dt.timedelta(days=lead)
            if d > self.last_stay:
                break
            self.snapshots[d][lead] = self.occ.get(d, 0)

    def settle(self, stay_date: dt.date) -> dict:
        """Close out one night: no-shows drop, overbooked guests get walked."""
        hotel = self.hotel
        for hold in list(self.arrivals_on.get(stay_date, [])):
            if getattr(hold, "no_show", False):
                self.release(hold, from_night=stay_date)

        rooms_held = self.occ.get(stay_date, 0)
        walked = max(0, rooms_held - hotel.rooms)
        if walked:
            # Walk the cheapest business first: it is the least costly to lose
            # and the most likely to be re-accommodated nearby.
            order = sorted(self.holds.get(stay_date, []), key=lambda h: h.rate)
            left = walked
            for hold in order:
                if left <= 0:
                    break
                take = min(left, hold.rooms)
                self.occ[stay_date] -= take
                self.seg_rooms[stay_date][hold.segment] -= take
                self.seg_revenue[stay_date][hold.segment] -= hold.rate * take
                left -= take

        sold = self.occ.get(stay_date, 0)
        revenue = sum(self.seg_revenue.get(stay_date, {}).values())
        result = {
            "date": stay_date,
            "rooms_sold": sold,
            "revenue": revenue,
            "walked": walked,
            "walk_cost": walked * hotel.walk_cost,
            "adr": revenue / sold if sold else 0.0,
            "occupancy": sold / hotel.rooms,
            "revpar": revenue / hotel.rooms,
            "mix": dict(self.seg_rooms.get(stay_date, {})),
            "denied_observable": self.observable_denials(stay_date),
        }
        self.settled[stay_date] = result
        return result

    # ------------------------------------------------------------------ kpi

    def kpi(self, first: dt.date, last: dt.date) -> dict:
        rows = [r for d, r in self.settled.items() if first <= d <= last]
        if not rows:
            return {}
        nights = len(rows)
        rooms = sum(r["rooms_sold"] for r in rows)
        revenue = sum(r["revenue"] for r in rows)
        walked = sum(r["walked"] for r in rows)
        walk_cost = sum(r["walk_cost"] for r in rows)
        capacity = nights * self.hotel.rooms
        var_cost = rooms * self.hotel.variable_cost
        return {
            "nights": nights,
            "rooms_sold": rooms,
            "capacity": capacity,
            "occupancy": rooms / capacity,
            "adr": revenue / rooms if rooms else 0.0,
            "revpar": revenue / capacity,
            "room_revenue": revenue,
            "walked": walked,
            "walk_cost": walk_cost,
            "net_contribution": revenue - var_cost - walk_cost,
            "goppar": (revenue - var_cost - walk_cost) / capacity,
            "sellouts": sum(1 for r in rows if r["rooms_sold"] >= self.hotel.rooms),
        }
