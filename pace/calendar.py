"""Calendar helpers: seasonality, demand classes, and the event calendar.

A demand class is the (season band, day of week) bucket a stay date belongs
to.  Pace curves, unconstrained demand and elasticity are all estimated per
class, because a Saturday in July and a Tuesday in January book on completely
different clocks.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

MONTH_FACTOR = {
    1: 0.72, 2: 0.78, 3: 0.88, 4: 0.96, 5: 1.08, 6: 1.18,
    7: 1.22, 8: 1.20, 9: 1.14, 10: 1.02, 11: 0.90, 12: 0.82,
}

BANDS = ("peak", "shoulder", "trough")

DOW_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def daterange(start: dt.date, days: int) -> List[dt.date]:
    return [start + dt.timedelta(days=i) for i in range(days)]


def span(first: dt.date, last: dt.date) -> List[dt.date]:
    return daterange(first, (last - first).days + 1)


def _band_from_factor(f: float) -> str:
    if f >= 1.12:
        return "peak"
    if f >= 0.95:
        return "shoulder"
    return "trough"


@dataclass(frozen=True)
class Seasonality:
    """What the engine knows about a hotel's year.

    price_month_factor scales the reference rate; demand_season_band picks the
    estimation cell.  They are two fields because on a resort price swings far
    more than occupancy (ADR 0007).
    """
    price_month_factor: Dict[int, float]
    demand_season_band: Dict[int, str]

    def validate(self) -> "Seasonality":
        months = set(range(1, 13))
        if set(self.price_month_factor) != months or set(self.demand_season_band) != months:
            raise ValueError("seasonality needs all twelve months")
        bad = [b for b in self.demand_season_band.values() if b not in BANDS]
        if bad:
            raise ValueError("unknown season band %r; use %s" % (bad[0], "/".join(BANDS)))
        if any(f <= 0 for f in self.price_month_factor.values()):
            raise ValueError("price month factors must be positive")
        return self


def toronto_seasonality() -> Seasonality:
    return Seasonality(dict(MONTH_FACTOR), {m: _band_from_factor(f) for m, f in MONTH_FACTOR.items()})


_ACTIVE = toronto_seasonality()


def set_seasonality(s: Seasonality) -> None:
    global _ACTIVE
    _ACTIVE = s.validate()


def reset_seasonality() -> None:
    global _ACTIVE
    _ACTIVE = toronto_seasonality()


def active_seasonality() -> Seasonality:
    return _ACTIVE


def month_factor(d: dt.date) -> float:
    return _ACTIVE.price_month_factor[d.month]


def season_band(d: dt.date) -> str:
    return _ACTIVE.demand_season_band[d.month]


def demand_class(d: dt.date):
    """The bucket used for every statistical estimate in the engine."""
    return (season_band(d), d.weekday())


def class_label(key) -> str:
    return "%s %s" % (key[0], DOW_NAMES[key[1]])


@dataclass(frozen=True)
class Event:
    name: str
    start: dt.date
    end: dt.date
    multiplier: float
    note: str = ""

    def covers(self, d: dt.date) -> bool:
        return self.start <= d <= self.end


class EventCalendar:
    """Citywide demand events.  Plugins add to this; the engine only reads it."""

    def __init__(self, events: Optional[Iterable[Event]] = None):
        self.events: List[Event] = list(events or [])

    def add(self, event: Event) -> None:
        """Idempotent: a plugin file that runs twice must not double its event."""
        if event in self.events:
            return
        self.events.append(event)

    def active(self, d: dt.date) -> List[Event]:
        return [e for e in self.events if e.covers(d)]

    def multiplier(self, d: dt.date) -> float:
        m = 1.0
        for e in self.active(d):
            m *= e.multiplier
        return m


# (month, day, nights, name, multiplier, note)
_ANNUAL_EVENTS = [
    (1, 6, 20, "Post-holiday lull", 0.82, "City empties out after New Year"),
    (2, 10, 5, "Winter medical congress", 1.55, "Convention centre, 6k delegates"),
    (3, 14, 9, "March break", 1.25, "Family leisure demand"),
    (4, 22, 4, "Spring trade expo", 1.40, "Convention centre"),
    (5, 3, 3, "Marathon weekend", 1.35, "Downtown road closures"),
    (6, 20, 4, "Summer music festival", 1.60, "Waterfront, sells out the city"),
    (8, 16, 10, "Summer fair", 1.20, "Regional leisure draw"),
    (9, 5, 11, "Film festival", 1.75, "Highest compression of the year"),
    (10, 8, 3, "Technology summit", 1.45, "Corporate heavy"),
    (12, 5, 16, "Holiday market", 1.10, "Weekend leisure lift"),
    (12, 29, 4, "New Year", 1.30, "Leisure, long stays"),
]


def default_calendar(years: Iterable[int]) -> EventCalendar:
    cal = EventCalendar()
    for y in years:
        for month, day, nights, name, mult, note in _ANNUAL_EVENTS:
            start = dt.date(y, month, day)
            cal.add(Event(name, start, start + dt.timedelta(days=nights - 1), mult, note))
    return cal
