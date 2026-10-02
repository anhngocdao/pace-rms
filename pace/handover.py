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


def actuals(bookings: List[Booking], nonrev: List[Booking], ledger: Ledger,
            night: dt.date) -> Dict[str, Optional[float]]:
    """What happened on `night`, every quantity in its own unit.

    rooms is the settled revenue figure the pilot scores against
    (baselines.actual). physical counts the rows, NONREV included. The three
    housekeeping counts are for the service day night + 1 and satisfy
    physical == departures + stayovers by construction.
    """
    phys_all = physical_rows(bookings, nonrev)
    phys = rows_on(phys_all, night)
    rev = [b for b in phys if b.target != "NONREV"]
    nxt = night + dt.timedelta(days=1)
    return {
        "rooms": baselines.actual(ledger, night),
        "physical": sum(b.rooms for b in phys),
        "breakfast": covers(rev, BREAKFAST_BOARDS),
        "dinner": covers(rev, DINNER_BOARDS),
        "stayovers": _stayovers(phys_all, night),
        "departures": _departures(phys_all, nxt),
        "arrivals": _arrivals(phys_all, nxt),
    }
