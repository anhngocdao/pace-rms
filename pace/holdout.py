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

    Two group rows are one decision only when they carry the same company,
    because that code is the only thing in the file that says they are.  A group
    row with no company stands alone.  Both of this project's authorities on the
    question refuse to merge without an identifier: `ingest.detect_groups` skips
    a row with no company, and the converter's own `cluster_transient_party`
    skips a row carrying neither an agent nor a company.  The converter is the
    one that applies here, because H1 and H2 both set `detect_groups` false and
    arrive with their targets already decided, and it writes one room per row,
    so two rows sharing nothing but a date share nothing.  Merging on booking
    day, arrival and length of stay instead would gather H1's 1,247 company-less
    group rows, of the 6,394 group rows that reach the cut, into 104 multi-room
    blocks, the largest of them 95 rooms.  That is larger than the largest real
    company block in the file, and it would be refused whole on the evidence
    that its rows share a date: at cap 150 under sell until full it refuses 35
    rows and 130 room nights that nothing says belong together.

    `rows` is one booking day's rows, so the booking day is the same for every
    one of them and is not part of the key.
    """
    out: List[list] = []
    groups: Dict[tuple, list] = {}
    for b in rows:
        if b.target == "GROUP" and b.company:
            key = (b.company, b.arrival, b.nights)
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

    `cut` is a gross room count: every night of every refused row, whatever the
    guest went on to do.  At H1, cap 150, sell until full, the 4,498 refused
    rows carry 22,160 refused room nights, and 5,242 of those nights belong to
    rows that cancelled before arrival, so 23.7 percent of the gross answer is
    inventory the hotel would have had back.  Under the secondary rule it is
    28.5 percent.  The quantity table 3 scores against, `capped_ledger`'s lead-0
    snapshot, is net of cancellations, so the two are not the same scale and a
    caller must not subtract one from the other: `net_cut` below puts the
    refusals on the snapshot's scale.  Gross is what this function returns
    because gross is the answer to its own question, which is how many rooms the
    cap refused; the choice between the two is the caller's and is made visible
    here rather than made for it.

    A refused booking loses all of its nights, not the ones before the full one:
    a guest turned away for the Saturday of a three-night stay does not take the
    Friday and the Sunday.

    Cancellations return rooms on their cancel date, and are applied before that
    day's bookings so a room given back this morning can be sold this afternoon.
    A row cancelled on the day it was booked cannot be given back before it is
    taken, so it is held for the rest of that day and given back at the end of
    it; without that second pass a same-day cancellation would hold its room for
    the rest of the history and the cut would refuse hundreds of rows on
    inventory nobody was ever in.  1,263 of H1's 10,831 dated cancellations fall
    on their own booking day.  `ingest.replay` cancels after the day's bookings
    for every cancellation and not only for the same-day ones, so the two agree
    on the same-day case and differ on the ordinary one, on purpose: the replay
    books every row it is handed and the order only moves a snapshot, while this
    function has to decide whether to take a row, and a hotel holding a
    cancellation for tonight sells that room again today.  Only an accepted row
    can cancel: a refusal frees nothing later.

    NONREV rows and zero-night day use are outside the cap entirely.  They never
    enter the ledger, they evict nothing, and a comp room entered after the cap
    was reached simply overshoots it.  They are handed back in `kept` unchanged,
    so the caller's population is the caller's population minus the refusals.

    `bookings` is read three times and the refusals are found by identity, so it
    is taken into a list first: a generator would be emptied by the first pass
    and hand back an empty `kept` without failing.

    `last_stay` cannot change what comes back.  Every decision is made on a
    booking day and the loop runs to the last of those whatever it says, so a
    later date only walks empty days and an earlier one is ignored.  What
    matters is that it is the loop's floor and not its ceiling: ending the loop
    at `last_stay` would leave every row booked after the last stay night
    undecided and drop it from `kept` without a word.  It stays in the signature
    because that is the interface table 3 was planned against, and a signature
    that changes quietly mid-plan costs more than an inert argument; it can go
    when table 3 lands.
    """
    if rule not in RULES:
        raise pilot.PilotError("unknown cutting rule %r; want one of %s"
                               % (rule, ", ".join(RULES)))
    bookings = list(bookings)
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


def net_cut(bookings, kept, first: dt.date, last: dt.date) -> Dict[dt.date, int]:
    """The refused room nights that would still have been on the books at lead 0.

    `cut_history` counts every refused night.  The quantity table 3 scores
    against is `capped_ledger`'s lead-0 snapshot, and that one is net, so this
    counts each refused row on exactly the nights the full-size replay would
    have shown it at lead 0, and on no others:

    - A row that cancelled is on none of them.  `ingest.replay` applies a day's
      cancellations before it takes that day's snapshot, and `ingest` clamps a
      cancel date to the arrival, so a cancelled row is off the books on every
      night it would have covered.
    - A no-show is on its arrival night only.  The replay snapshots a night and
      then settles it, and `Ledger.settle` releases a no-show from its arrival
      night onward, so it stands in that night's lead-0 snapshot and in no later
      one.  Counting every night of a multi-night no-show over-counts: at H1,
      sell until full, by 68 room nights over 65 nights at cap 150 and by 198
      over 148 nights at cap 112.
    - Except a no-show that arrived before `first`.  The replay settles nights
      from `first` on, so it never settles that arrival night and never releases
      the row, and it stays on every night it covered.
    - A row that stayed is on every night it covered.

    Only nights from `first` to `last` are returned, because those are the only
    nights a lead-0 snapshot exists for; pass the same pair `capped_ledger` is
    given.  The result is then the gap between the two replays' lead-0
    snapshots exactly, night for night, which a test asserts.

    Measured at H1, sell until full, on the nights of the window: 16,624 room
    nights net against 21,908 gross at cap 150, and 38,616 against 52,364 at cap
    112.  Both sides are counted on the same nights; `cut_history`'s own totals
    run slightly higher because they include refused nights outside the window.

    Hand it the same list `cut_history` was handed and the same `kept` it gave
    back; the refusals are the difference between them, by identity, exactly as
    the cut found them.
    """
    keep = set(id(b) for b in kept)
    out: Dict[dt.date, int] = defaultdict(int)
    for b in bookings:
        if id(b) in keep or b.status == "cancelled":
            continue
        span = 1 if (b.status == "no_show" and b.arrival >= first) else b.nights
        for k in range(span):
            n = b.arrival + dt.timedelta(days=k)
            if first <= n <= last:
                out[n] += 1
    return dict(out)


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
