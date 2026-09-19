"""The four baselines the engine has to beat.

None of them needs the forward walk.  They read Ledger.snapshots, the leak-free
as-of series the replay already froze, and Ledger.settled, what each night
actually sold.  At H1 that is 335,995 snapshot entries across 1,276 stay dates
after one ingest.load.

The one place a baseline can cheat is its reference window, so the rule is
written down here rather than at the call site: a reference night counts only
when it had already finished on the forecast day.  At lead 120 the ten trailing
same-weekday nights then sit between 126 and 189 days before the night being
forecast.

Each of these is arithmetic on the ledger, fixed before the run and not tuned
afterwards.  None of them prices a night, so beating one is not evidence of
revenue.
"""
import datetime as dt
from statistics import fmean
from typing import Dict, List, Optional

from .ledger import Ledger

WINDOW = 10          # settings.json baseline_window_weeks
STLY_BACK = 364      # a year, aligned by weekday
ORDER = ("pickup_add", "pickup_mult", "stly_add", "average")
NAMES = {
    "pickup_add": "Additive pickup",
    "pickup_mult": "Multiplicative pickup",
    "stly_add": "Same time last year, additive",
    "average": "Average of additive pickup and last year",
}

BASELINE_NOTE = (
    "The baselines are arithmetic on the same snapshots the engine reads, fixed "
    "before the run and not tuned afterwards. A reference night counts only when "
    "it had already finished on the forecast day, so at lead 120 the trailing ten "
    "weeks sit between 126 and 189 days before the night being forecast. None of "
    "these methods prices a night, so beating one of them is not evidence of "
    "revenue.")


def actual(ledger: Ledger, d: dt.date) -> Optional[float]:
    """Rooms that actually stayed, from the settlement rather than the lead-0
    snapshot: the replay snapshots a day before it settles, so lead 0 still
    holds the night's no-shows and any rooms the settlement walked. The two
    differ on 108 of 427 scoring nights at H1 and 220 of 427 at H2."""
    row = ledger.settled.get(d)
    return None if row is None else float(row["rooms_sold"])


def reference_nights(ledger: Ledger, d: dt.date, lead: int,
                     weeks: int = WINDOW) -> List[dt.date]:
    """The `weeks` most recent same-weekday nights already finished on the
    forecast day, each with a snapshot at this lead."""
    asof = d - dt.timedelta(days=lead)
    out: List[dt.date] = []
    n = d - dt.timedelta(days=7)
    while len(out) < weeks and n >= ledger.first_stay:
        if n < asof and d != n and actual(ledger, n) is not None \
                and ledger.otb_at(n, lead) is not None:
            out.append(n)
        n -= dt.timedelta(days=7)
    return out


def additive_pickup(ledger: Ledger, d: dt.date, lead: int,
                    refs: Optional[List[dt.date]] = None,
                    weeks: int = WINDOW) -> Optional[float]:
    """Rooms on the books now, plus the rooms that normally still come in."""
    otb = ledger.otb_at(d, lead)
    refs = reference_nights(ledger, d, lead, weeks) if refs is None else refs
    if otb is None or len(refs) < weeks:
        return None
    return otb + fmean([actual(ledger, n) - ledger.otb_at(n, lead) for n in refs])


def multiplicative_pickup(ledger: Ledger, d: dt.date, lead: int,
                          refs: Optional[List[dt.date]] = None,
                          weeks: int = WINDOW) -> Optional[float]:
    """Rooms on the books now, divided by the share normally sold by this lead.

    Expected to explode at long leads, where the denominator is small. Kept
    because it is the form most hotels use.
    """
    otb = ledger.otb_at(d, lead)
    refs = reference_nights(ledger, d, lead, weeks) if refs is None else refs
    if otb is None or len(refs) < weeks:
        return None
    shares = []
    for n in refs:
        final = actual(ledger, n)
        if final and final > 0:
            shares.append(ledger.otb_at(n, lead) / final)
    if len(shares) < weeks:
        return None
    share = fmean(shares)
    if share <= 0:
        return None
    return otb / share


def stly_additive(ledger: Ledger, d: dt.date, lead: int) -> Optional[float]:
    """Last year's final, aligned by weekday, plus this year's head start.

    The ratio form was rejected in the design because its denominator is tiny at
    long leads, which is the same failure as multiplicative pickup and twice.
    """
    n = d - dt.timedelta(days=STLY_BACK)
    otb = ledger.otb_at(d, lead)
    last_otb = ledger.otb_at(n, lead)
    last_final = actual(ledger, n)
    if otb is None or last_otb is None or last_final is None:
        return None
    return last_final + (otb - last_otb)


def average_pickup_and_stly(add: Optional[float], stly: Optional[float]) -> Optional[float]:
    """The forecast a good revenue manager builds in Excel, and the engine's
    real opponent."""
    if add is None or stly is None:
        return None
    return 0.5 * (add + stly)


def all_baselines(ledger: Ledger, d: dt.date, lead: int,
                  weeks: int = WINDOW) -> Dict[str, Optional[float]]:
    refs = reference_nights(ledger, d, lead, weeks)
    add = additive_pickup(ledger, d, lead, refs, weeks)
    return {
        "pickup_add": add,
        "pickup_mult": multiplicative_pickup(ledger, d, lead, refs, weeks),
        "stly_add": stly_additive(ledger, d, lead),
        "average": average_pickup_and_stly(add, stly_additive(ledger, d, lead)),
    }
