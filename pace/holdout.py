"""Table 3: unconstraining checked against a history whose answer is known.

The method is a holdout.  Take the real history, impose a capacity cap it never
had, and refuse the bookings that would have crossed it.  The rooms refused are
the known answer.  Then ask the unconstrainer, which sees only the cut history,
to say how much demand there was, and compare.

Two things have to be true at once or the table is a null by construction.  The
refusal must be a pre-pass over the booking rows, because lowering
sellable_rooms imposes no cap: the replay books every row and only the
settlement walks the excess, one night at a time, after the fact.  And the
capped history must be replayed into a Hotel whose rooms really are the cap, or
nothing is ever marked censored, because a history capped at 60 percent of 187
rooms never reaches 0.97 of 187.
"""
import dataclasses
import datetime as dt
from collections import defaultdict
from typing import Dict, List, Tuple

from . import ingest
from . import pilot
from .config import Hotel
from .ledger import Ledger

RULES = ("sell_until_full", "close_cheap_first", "close_cheap_first_ota")
RULE_NAMES = {
    "sell_until_full": "Sell until full",
    "close_cheap_first": "Close cheap first",
    "close_cheap_first_ota": "Close cheap first, OTA too",
}
# The converter writes its branch code into the segment column, which is the
# only place the origin survives: the mapped target cannot tell a tour operator
# allotment from a corporate account, because both are CORP.  These are every
# branch tools/convert_antonio.py raises on the Offline TA/TO market segment,
# contract, transient and group alike.
CHEAP_BRANCHES = ("OFFLINE_TO_CONTRACT", "OFFLINE_TO_TRANSIENT", "OFFLINE_TO_GROUP")
MARK = 0.90          # settings.json close_cheap_first_mark

NO_RATE_CODES_NOTE = (
    "Promotional and non-refundable rate codes would close at the same mark as "
    "the tour operator rows in a dataset that has them. This one has no rate "
    "codes at all, so that part of the rule never fires here.")


def clean_nights(bookings, hotel: Hotel, first: dt.date, last: dt.date,
                 threshold: float, window: int) -> List[dt.date]:
    """Nights below the clean threshold with no busy night in the neighbourhood.

    Occupancy is physical and counts the comp rooms, because a night full only
    thanks to comp rooms was full for the guest who was turned away.  The
    neighbourhood is the 90th percentile of length of stay, 8 nights at H1 and 5
    at H2, because a stay spanning a full night is refused for all its nights.
    A busy night just outside `[first, last]` disqualifies its neighbours inside
    it for the same reason, so the window is read against every night the log
    has, not only against the ones being offered.
    """
    occ = ingest.physical_occupancy(bookings)
    busy = set(d for d, n in occ.items() if n >= hotel.rooms * threshold)
    out = []
    d = first
    while d <= last:
        if not any((d + dt.timedelta(days=k)) in busy
                   for k in range(-window, window + 1)):
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def _limit(b, rule: str, cap: float, mark_rooms: float) -> float:
    """The room count this decision may not cross.

    Under the cheap-first rules the tour operator rows stop at the mark because
    their net rate is the lowest in the house, and a group stops there because a
    block is the one booking a hotel can see coming and decline.  OTA and
    corporate run to the cap: near full the hotel raises BAR and the OTA price
    follows it, and a corporate contract often carries last-room availability.
    The secondary form is for a hotel whose policy closes the OTA channel too.
    """
    if rule == "sell_until_full":
        return cap
    if b.segment in CHEAP_BRANCHES or b.target == "GROUP":
        return mark_rooms
    if rule == "close_cheap_first_ota" and b.target == "OTA":
        return mark_rooms
    return cap


def _blocks(rows) -> List[list]:
    """One decision per block.  A group is many rooms in one decision, so its
    rows are accepted or refused together; everything else stands alone.

    The key is `ingest.detect_groups`' own key, which is this project's
    definition of one decision, so rows the converter marked group business but
    left without a company or an agent fall together when they also share a
    booking day, an arrival and a length of stay.  That is 1,259 of H1's 6,425
    group rows.
    """
    out: List[list] = []
    groups: Dict[tuple, list] = {}
    for b in rows:
        if b.target == "GROUP":
            key = (b.company, b.booked_on, b.arrival, b.nights)
            if key not in groups:
                groups[key] = []
                out.append(groups[key])
            groups[key].append(b)
        else:
            out.append([b])
    return out


def cut_history(bookings, cap_rooms: int, rule: str, last_stay: dt.date,
                mark_share: float = MARK) -> Tuple[List, Dict[dt.date, int]]:
    """Which rows a hotel with `cap_rooms` rooms would have taken, and what it lost.

    A refused booking loses all of its nights, not the ones before the full one:
    a guest turned away for the Saturday of a three-night stay does not take the
    Friday and the Sunday.

    Cancellations return rooms on their cancel date, and are applied before that
    day's bookings so a room given back this morning can be sold this afternoon.
    A row cancelled on the day it was booked is given back at the end of that
    day instead, which is where `ingest.replay` puts it too; without that pass a
    same-day cancellation would hold its room for the rest of the history and
    the cut would refuse hundreds of rows on inventory nobody was ever in.
    Only an accepted row can cancel: a refusal frees nothing later.

    NONREV rows and zero-night day use are outside the cap entirely.  They never
    enter the ledger, they evict nothing, and a comp room entered after the cap
    was reached simply overshoots it.  They are handed back in `kept` unchanged,
    so the caller's population is the caller's population minus the refusals.
    """
    if rule not in RULES:
        raise pilot.PilotError("unknown cutting rule %r; want one of %s"
                               % (rule, ", ".join(RULES)))
    mark_rooms = mark_share * cap_rooms
    keep = set()
    live = []
    for b in bookings:
        if b.target not in (None, "NONREV") and b.nights > 0:
            live.append(b)
        else:
            keep.add(id(b))
    by_day: Dict[dt.date, list] = defaultdict(list)
    for b in live:
        by_day[b.booked_on].append(b)

    committed: Dict[dt.date, int] = defaultdict(int)
    cancels: Dict[dt.date, list] = defaultdict(list)
    cut: Dict[dt.date, int] = defaultdict(int)

    def give_back(when: dt.date) -> None:
        for b in cancels.pop(when, ()):
            for k in range(b.nights):
                committed[b.arrival + dt.timedelta(days=k)] -= 1

    day = min(by_day) if by_day else last_stay
    end = max([last_stay] + list(by_day)) if by_day else last_stay
    while day <= end:
        give_back(day)
        for block in _blocks(by_day.get(day, ())):
            head = block[0]
            size = len(block)
            nights = [head.arrival + dt.timedelta(days=k) for k in range(head.nights)]
            limit = _limit(head, rule, float(cap_rooms), mark_rooms)
            if all(committed[n] + size <= limit for n in nights):
                for b in block:
                    keep.add(id(b))
                    if b.status == "cancelled" and b.status_date is not None:
                        cancels[b.status_date].append(b)
                for n in nights:
                    committed[n] += size
            else:
                for n in nights:
                    cut[n] += size
        give_back(day)
        day += dt.timedelta(days=1)

    return [b for b in bookings if id(b) in keep], dict(cut)


def capped_ledger(kept, hotel: Hotel, cap_rooms: int, first: dt.date, last: dt.date,
                  rep=None) -> Tuple[Ledger, Hotel]:
    """Replay the cut history into a hotel that really has `cap_rooms` rooms.

    The second half of the cap.  `ingest.replay` reads its sell-out and its walk
    off the Hotel it is handed, and `unconstrain.class_demand` reads its
    censoring test off the same room count, so a capped history replayed into
    the full-size hotel is a history where no night is ever censored and there
    is nothing for table 3 to score.
    """
    capped = dataclasses.replace(hotel, rooms=int(cap_rooms))
    rep = ingest.Report() if rep is None else rep
    return ingest.replay(kept, capped, first, last, rep), capped
