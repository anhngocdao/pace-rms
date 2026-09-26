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
from statistics import fmean
from typing import Dict, List, Optional, Tuple

from . import ingest
from . import pilot
from .calendar import demand_class
from .config import SEGMENT_ORDER, Hotel
from .ledger import Ledger
from .otb import GLOBAL_KEY
from .unconstrain import project_detruncate

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
                  rep=None, snapshot_horizon: Optional[int] = None) -> Tuple[Ledger, Hotel]:
    """Replay the cut history into a hotel that really has `cap_rooms` rooms.

    The second half of the cap.  `ingest.replay` reads its sell-out and its walk
    off the Hotel it is handed, and `unconstrain.class_demand` reads its
    censoring test off the same room count, so a capped history replayed into
    the full-size hotel is a history where no night is ever censored and there
    is nothing for table 3 to score.

    `snapshot_horizon` is how many days ahead the replay freezes the books each
    day.  Left out, it is the hotel's own max_lead, as in every other replay.
    Table 3 passes 0: it reads this ledger through the settled figures and the
    lead-0 snapshot and through nothing else, and the grid replays the history
    24 times, so a 298-lead snapshot on each pass at H1 is most of the cost for
    numbers nothing reads.  A test asserts that the settled rooms and the lead-0
    snapshots are identical at 0 and at the hotel's real max_lead.
    """
    changes = {"rooms": int(cap_rooms)}
    if snapshot_horizon is not None:
        changes["max_lead"] = int(snapshot_horizon)
    capped = dataclasses.replace(hotel, **changes)
    rep = ingest.Report() if rep is None else rep
    return ingest.replay(kept, capped, first, last, rep), capped


MIN_CLASS_OBS = 8        # the gate PaceCurves uses before it trusts a class
MIN_SCORED = 20
BUCKETS = ("under 5 percent", "5 to 15 percent", "over 15 percent")
THRESHOLDS = (0.80, 0.85, 0.90)    # settings.json holdout_clean_thresholds
CAPS = (0.60, 0.70, 0.80)          # settings.json holdout_caps

WHY_NOT_UPLIFT = (
    "censoring_uplift is not the quantity scored here. It divides a mean that has "
    "been both price restated and detruncated by a raw lead-0 count, so scoring it "
    "against a known answer would charge the price restatement to the "
    "unconstrainer, and closing the cheap channels first would make that worse by "
    "raising the realised rate and lowering modelled acceptance with no censoring "
    "involved. This table runs the observation loop of class_demand with the price "
    "term removed, flags a night censored exactly as class_demand does, off its "
    "lead-0 snapshot, and calls project_detruncate directly, so what is measured is "
    "the censoring correction on its own.")
UNCENSORED_NOTE = (
    "Only nights the capped history flags as censored enter the unconstrainer's "
    "error. On a scored night it does not flag, project_detruncate hands the "
    "observation back unchanged, so the estimate is exactly what the capped history "
    "sold and the error is exactly the rooms the cut took. Those nights measure the "
    "cut and say nothing about the unconstrainer, so they are counted apart, beside "
    "the rooms cut on them, and left out of every error, every bucket and every "
    "segment figure.")
EMPTY_BUCKET_NOTE = (
    "A bucket with no nights in it prints as zero nights and no estimate. Buckets "
    "are never merged and never widened to find a sample: an empty bucket is a "
    "result about this history, not a gap to be filled. A combination whose three "
    "buckets are all empty prints its clean-night count beside its cut-night "
    "count and its count of clean nights the capped history flags as censored, so "
    "a reader can see whether the cut missed the clean nights, reached them "
    "without censoring them, or censored them and took nothing. A cut night is a "
    "clean night that sold fewer rooms in the capped history than in the real "
    "one, after cancellations and no-shows, which is the scale the known answer is "
    "on; the room nights the cap refused before any of them cancelled are printed "
    "apart, under a name that says they are gross. Through capped_ledger the "
    "cut-night count and the scored count are one count under two names: a scored "
    "night is a cut night with a lead-0 snapshot, and the only settled nights the "
    "capped replay leaves without one lie before the first booking day, where "
    "nothing was sold in either history and nothing was cut. They part only for a "
    "ledger built another way, without lead-0 snapshots on nights the cut "
    "reached.")
SETTINGS_NOTE = (
    "Every combination of clean threshold, cap and cutting rule is printed, not a "
    "chosen one. If the estimate moves a lot across them, the settings are deciding "
    "the result and the table should be read as a sensitivity analysis rather than "
    "as a measurement.")
VANISH_NOTE = (
    "Refused guests are assumed to vanish. Some of them would have moved to "
    "another night of the same hotel, so the known answer is a lower bound on the "
    "demand the cut destroyed and the unconstrainer is being asked an easier "
    "question than the real one.")
# Filled by name from run_grid: the gate, the grid's counts, the neighbour
# window, and the largest combination's band counts with and without it.
_NO_SAMPLE_HEAD = (
    "No combination in this grid produced a scorable sample, so no estimate of "
    "the unconstrainer's error is quoted from this table. The gate is %(gate)d "
    "censored nights in one combination, because a combination is where an "
    "estimate is formed; of the %(combos)d run, the largest reached %(best)d. "
    "Their censored nights sum to %(summed)d across the grid, but that is "
    "%(distinct)d nights counted once per combination they appear in, not a "
    "sample of that size. In the largest combination, threshold %(threshold).2f "
    "and cap %(cap_rooms)d rooms under %(rule)s, %(band)d nights of the window "
    "sit in the band a cap of that size can censor and the clean threshold does "
    "not exclude, physical occupancy from %(lo).1f to under %(hi).1f rooms; with "
    "the neighbour window at 0 all %(band)d of them are clean, and with the "
    "window of %(window)d nights %(band_clean)d are. ")
NO_SAMPLE = _NO_SAMPLE_HEAD + (
    "What empties the sample is that window, not the cap: a stay spanning a busy "
    "night is refused for all of its nights, so a clean night may have no busy "
    "night within %(window)d nights on either side, and the moderately busy "
    "nights a cap censors sit inside busy weeks. The window is the pre-registered "
    "one, the 90th percentile of length of stay, and it is not moved here, so the "
    "empty sample is this history under that window and not a defect of this "
    "engine's unconstrainer, which this table therefore neither confirms nor "
    "refutes.")
# The same grid on a history where the window left the largest combination's
# band whole: then it is not the window that emptied the sample, and the
# sentence must not say it was.
NO_SAMPLE_WINDOW_TOOK_NOTHING = _NO_SAMPLE_HEAD + (
    "The window took nothing from that band, so it is not what emptied this "
    "sample: of those %(band)d clean nights the capped history flagged and "
    "scored %(best)d. That is not a defect of this engine's unconstrainer, "
    "which this table therefore neither confirms nor refutes.")


def observations(ledger, hotel: Hotel, nights) -> Dict[dt.date, Tuple[float, bool]]:
    """The observation and the censoring flag per night, as class_demand makes them.

    `unconstrain.class_demand` skips a night with no lead-0 snapshot, and flags
    a night censored when that snapshot reaches `rooms * sellout_threshold` or
    the ledger logged a denial for it.  It does not read the settled figure for
    the flag, and the two can fall either side of the line: a no-show stands in
    the lead-0 snapshot and leaves before settlement, so a full night with one
    in it is censored by the snapshot and would not be by the settled figure.
    The two figures differ on 108 of H1's 427 scoring nights (table 1 reads the
    settled one for that reason).  This flag is class_demand's.

    The observation is class_demand's with the price term taken out: the rooms
    the night sold, with nothing divided by an acceptance, plus any logged
    denial.  class_demand sums the settled segment mix, which is the settled
    room count; a replayed ledger logs no denials, so on this path the
    observation is the settled rooms sold.
    """
    out: Dict[dt.date, Tuple[float, bool]] = {}
    cut = hotel.rooms * hotel.sellout_threshold
    for d in nights:
        snaps = ledger.snapshots.get(d)
        if not snaps:
            continue
        final = snaps.get(0)
        if final is None:
            continue
        row = ledger.settled.get(d)
        if row is None:
            continue
        regrets = ledger.observable_denials(d)
        out[d] = (float(row["rooms_sold"] + regrets), (final >= cut) or regrets > 0)
    return out


def per_night_demand(ledger, hotel: Hotel, nights) -> Dict[dt.date, float]:
    """Unconstrained demand per night, censoring correction only.

    `observations` gives each night's value and flag, and project_detruncate
    returns one imputed value per observation, so keeping the dates in the
    order they went in gives a per-night estimate rather than a class mean.  A
    night the flag leaves open comes back as its own observation.  A class
    thinner than MIN_CLASS_OBS falls back to the house series, which is the
    same gate PaceCurves uses.  A night with no lead-0 snapshot is not in the
    sample and not in the answer, as in class_demand.
    """
    order: Dict[tuple, List[dt.date]] = defaultdict(list)
    obs: Dict[tuple, List[Tuple[float, bool]]] = defaultdict(list)
    seen = observations(ledger, hotel, nights)
    for d in nights:
        if d not in seen:
            continue
        for key in (demand_class(d), GLOBAL_KEY):
            obs[key].append(seen[d])
            order[key].append(d)
    est: Dict[tuple, Dict[dt.date, float]] = {}
    for key, rows in obs.items():
        _mu, _sigma, imputed = project_detruncate(rows)
        est[key] = dict(zip(order[key], imputed))
    house = est.get(GLOBAL_KEY, {})
    out: Dict[dt.date, float] = {}
    for d in nights:
        key = demand_class(d)
        if len(obs.get(key, ())) >= MIN_CLASS_OBS and d in est.get(key, {}):
            out[d] = est[key][d]
        elif d in house:
            out[d] = house[d]
    return out


def bucket_of(share: float) -> str:
    if share < 0.05:
        return BUCKETS[0]
    if share <= 0.15:
        return BUCKETS[1]
    return BUCKETS[2]


def _agg(rows) -> dict:
    if not rows:
        return {"n": 0, "known": None, "estimate": None, "mae": None, "bias": None}
    errs = [r["estimate"] - r["known"] for r in rows]
    return {"n": len(rows),
            "known": fmean([r["known"] for r in rows]),
            "estimate": fmean([r["estimate"] for r in rows]),
            "mae": fmean([abs(e) for e in errs]),
            "bias": fmean(errs)}


def _agg_segments(rows) -> dict:
    """The unconstrainer's error by segment, over the nights the segment traded.

    The estimate is split by the capped mix, so a segment with no rooms in the
    capped history has an estimate of exactly zero, and a segment with no rooms
    in the real history has a known of zero.  A pair that is zero on both sides
    is a night the segment did no business on in either history, and its error
    of zero is not an observation of the unconstrainer: counted, it reports a
    perfect score for a segment on nights it did not trade, six GROUP
    night-segments at H1 and seven at H2.  Such pairs are left out of the
    segment's n, its mae and its bias, and a segment with no pair left prints as
    n 0 and no estimate, like an empty bucket.  A pair with rooms on one side
    only is kept: that is a segment the cut emptied, or one the unconstrainer
    invented, and either is an error worth printing.
    """
    out = {}
    for code in SEGMENT_ORDER:
        pairs = [(r["segments"][code]["estimate"], r["segments"][code]["known"]) for r in rows]
        pairs = [(e, k) for e, k in pairs if not (e == 0 and k == 0)]
        if not pairs:
            out[code] = {"n": 0, "mae": None, "bias": None}
            continue
        errs = [e - k for e, k in pairs]
        out[code] = {"n": len(pairs), "mae": fmean([abs(x) for x in errs]),
                     "bias": fmean(errs)}
    return out


def score_combo(bookings, hotel: Hotel, full_ledger, first: dt.date, last: dt.date,
                threshold: float, cap_share: float, rule: str, window: int) -> dict:
    """One cut, one capped replay, and the unconstrainer scored on the clean nights.

    The unconstrainer is handed every night of the capped history, as the
    engine's own class_demand is handed every completed night, and only then
    are the clean nights picked out of its answer.  Handing it the clean nights
    alone would hand it the real history's verdict on which nights were quiet,
    which is the one thing the cut history is meant to hide from it.

    Every scored night carries the capped ledger's censoring flag, and the
    unconstrainer's figures, overall, by bucket and by segment, are over the
    censored ones only (UNCENSORED_NOTE says why).

    `cut_nights` and `scored` are the same count on this path.  A night is
    scored when it lost rooms and has an estimate, and per_night_demand has one
    for every night with a lead-0 snapshot; through capped_ledger the only
    settled nights without one lie before the first booking day, where both
    histories sold nothing and nothing was cut.  Both fields stay, because
    `observations` takes any ledger and a caller that builds one without lead-0
    snapshots would see them part: a cut night the snapshot cannot speak for is
    then counted as cut and not scored.  A mutation setting cut_nights to the
    row count survives every fixture built through capped_ledger, and this
    paragraph is why that is not a defect.

    `censorable_band_nights` counts the window's settled nights whose physical
    occupancy sits in [cap_rooms * sellout_threshold, threshold * rooms): high
    enough for a cap of this size to censor and below the clean threshold.
    `censorable_band_clean` is how many of them the neighbour window leaves
    clean.  With the window at 0 the two are equal by construction, so the pair
    is the sample this window left against the one no window would have.  At
    H1, threshold 0.90 and cap 112, the band holds 195 nights and the window of
    8 leaves 16 of them clean; that gap, not the cap, is why table 3 has no
    sample there.
    """
    cap_rooms = int(round(cap_share * hotel.rooms))
    kept, cut = cut_history(bookings, cap_rooms, rule, last)
    led, capped = capped_ledger(kept, hotel, cap_rooms, first, last, snapshot_horizon=0)
    clean = [d for d in clean_nights(bookings, hotel, first, last, threshold, window)
             if d in full_ledger.settled]
    history = sorted(d for d in led.settled if first <= d <= last)

    # The band a cap of this size can censor and the clean threshold does not
    # exclude: physical occupancy from the capped hotel's censoring line up to,
    # and not including, the clean threshold.  Every night in it is clean with
    # no neighbour window, so the two counts are the sample the window leaves
    # and the sample it would have left.  The band is read off physical
    # occupancy, as clean_nights reads the threshold, so a comp room counts.
    occ = ingest.physical_occupancy(bookings)
    lo, hi = cap_rooms * hotel.sellout_threshold, threshold * hotel.rooms
    band = [d for d in full_ledger.settled if first <= d <= last and lo <= occ.get(d, 0) < hi]
    clean_set = set(clean)
    band_clean = [d for d in band if d in clean_set]
    flags = dict((d, c) for d, (_v, c) in observations(led, capped, history).items())
    est = per_night_demand(led, capped, history)

    rows = []
    cut_nights = 0
    for d in clean:
        got_row = led.settled.get(d)
        if got_row is None:
            continue
        known = float(full_ledger.settled[d]["rooms_sold"])
        got = float(got_row["rooms_sold"])
        lost = known - got
        if lost > 0:
            cut_nights += 1
        if lost <= 0 or known <= 0 or d not in est:
            continue
        mix = led.seg_rooms.get(d, {})
        total = float(sum(max(0, v) for v in mix.values()))
        segments = {}
        for code in SEGMENT_ORDER:
            share = (max(0, mix.get(code, 0)) / total) if total > 0 else 0.0
            segments[code] = {
                "known": float(full_ledger.seg_rooms.get(d, {}).get(code, 0)),
                "estimate": est[d] * share,
            }
        rows.append({"date": d.isoformat(), "known": known, "capped": got,
                     "estimate": est[d], "censored": flags[d], "cut_rooms": lost,
                     "cut_share": lost / known, "bucket": bucket_of(lost / known),
                     "segments": segments})

    censored = [r for r in rows if r["censored"]]
    open_rows = [r for r in rows if not r["censored"]]
    scorable = len(censored) >= MIN_SCORED
    buckets = {}
    for name in BUCKETS:
        inside = [r for r in censored if r["bucket"] == name]
        buckets[name] = _agg(inside)
        buckets[name]["by_segment"] = _agg_segments(inside)
    return {
        "threshold": threshold, "cap": cap_share, "cap_rooms": cap_rooms, "rule": rule,
        "rule_name": RULE_NAMES[rule],
        "clean_nights": len(clean),
        "censored_clean": sum(1 for d in clean if flags.get(d)),
        "censorable_band_nights": len(band),
        "censorable_band_clean": len(band_clean),
        "cut_nights": cut_nights,
        "cut_room_nights_gross": sum(cut.values()),
        "scored": len(rows),
        "censored_scored": len(censored),
        "uncensored_scored": len(open_rows),
        "scorable": scorable,
        "overall": _agg(censored), "by_segment": _agg_segments(censored),
        "buckets": buckets,
        "uncensored": {"n": len(open_rows),
                       "mean_rooms_cut": (fmean([r["cut_rooms"] for r in open_rows])
                                          if open_rows else None)},
        "nights": rows,
    }


def grid_cells(thresholds=THRESHOLDS, caps=CAPS, rules=RULES) -> List[Tuple[float, float, str]]:
    """The combinations the grid runs, in the order it runs them: every cap
    below its threshold, under every rule.  Eight pairs and 24 cells on the
    pre-registered values.  run_one reads the count off this rather than
    carrying its own copy of the arithmetic."""
    return [(threshold, cap, rule)
            for threshold in thresholds for cap in caps if cap < threshold
            for rule in rules]


def run_grid(bookings, hotel: Hotel, full_ledger, first: dt.date, last: dt.date,
             window: int, thresholds=THRESHOLDS, caps=CAPS, rules=RULES) -> dict:
    """Every pre-registered combination whose cap sits below its threshold.

    MIN_SCORED is applied to one combination at a time, because a combination is
    where an estimate is formed: the error, the buckets and the segment figures
    are all means over one combination's censored nights, and no number in this
    table is ever a mean over the grid.  Summing the censored nights of 24
    combinations would count one night up to 24 times and report a sample that
    does not exist; at H1 the sum is 81 and no single combination reaches the
    gate of 20; the largest reaches 9.  Both totals are returned, each saying
    what it counts.

    When no combination is scorable the sentence appended to the notes names
    the largest one, the combination with the most censored scored nights, and
    among equals the one with the most band nights left clean by the window and
    then the most band nights, and prints its censorable band with and without
    the neighbour window (score_combo says what the band is).  NO_SAMPLE says
    the window is what emptied the sample, and is used only when the window
    took something from that band; NO_SAMPLE_WINDOW_TOOK_NOTHING is the same
    sentence for a history where it did not, because a note must not name a
    cause the counts beside it refute.  A grid with no cell at all, every cap
    at or above every threshold, has nothing to say and is refused in a
    sentence.
    """
    cells = grid_cells(thresholds, caps, rules)
    if not cells:
        raise pilot.PilotError("no combination in the grid has a cap below its threshold "
                               "(thresholds %s, caps %s), so there is nothing to run"
                               % (list(thresholds), list(caps)))
    combos = [score_combo(bookings, hotel, full_ledger, first, last, threshold, cap, rule, window)
              for threshold, cap, rule in cells]
    scored_total = sum(c["scored"] for c in combos)
    censored_total = sum(c["censored_scored"] for c in combos)
    distinct = set()
    for c in combos:
        distinct.update(r["date"] for r in c["nights"] if r["censored"])
    largest = max(combos, key=lambda c: (c["censored_scored"], c["censorable_band_clean"],
                                         c["censorable_band_nights"]))
    best = largest["censored_scored"]
    scorable = [c for c in combos if c["scorable"]]
    notes = [WHY_NOT_UPLIFT, UNCENSORED_NOTE, VANISH_NOTE, SETTINGS_NOTE,
             EMPTY_BUCKET_NOTE, NO_RATE_CODES_NOTE]
    if not scorable:
        took = largest["censorable_band_clean"] < largest["censorable_band_nights"]
        notes.append((NO_SAMPLE if took else NO_SAMPLE_WINDOW_TOOK_NOTHING) % {
            "gate": MIN_SCORED, "combos": len(combos), "best": best,
            "summed": censored_total, "distinct": len(distinct), "window": window,
            "threshold": largest["threshold"], "cap_rooms": largest["cap_rooms"],
            "rule": largest["rule_name"].lower(),
            "band": largest["censorable_band_nights"],
            "band_clean": largest["censorable_band_clean"],
            "lo": largest["cap_rooms"] * hotel.sellout_threshold,
            "hi": largest["threshold"] * hotel.rooms})
    return {"window": window, "thresholds": list(thresholds), "caps": list(caps),
            "rules": list(rules), "combos": combos, "min_scored": MIN_SCORED,
            "scored_total": scored_total, "censored_scored_total": censored_total,
            "censored_distinct_nights": len(distinct),
            "best_combo_censored": best,
            "scorable_combos": len(scorable), "scorable": bool(scorable),
            "notes": notes}
