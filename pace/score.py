"""Table 1: forecast accuracy, the engine against four baselines.

Three rules, each of them measured on the real files before this was written.

Actuals come from ledger.settled, never from the lead-0 snapshot: the replay
snapshots a day before it settles the night, so lead 0 still holds the night's
no-shows and any rooms the settlement walked, and the two differ on 108 of 427
scoring nights at H1 and 220 of 427 at H2.

Forecasts are clamped to capacity before scoring, because on a full night a
forecast above capacity is not wrong, and the share that hit the clamp is
printed beside the error, because at H1 lead 120 the engine's mean absolute
error is 37.2 rooms raw and 16.6 clamped, with 48.9 percent of its forecasts
above capacity. Those three figures are measured over the 415 nights every
method forecasts, the population this table reports; on the wider 427 nights
the engine alone forecasts they are 39.0, 16.3 and 50.4 percent. A method
rescued by the clamp has to be visible as one.

Lead 1 is on its own line and measures late cancellations and no-shows, which
is an overbooking question and not a demand question.
"""
import datetime as dt
from collections import defaultdict
from statistics import fmean
from typing import Dict, List

from . import baselines
from . import ingest
from . import pilot

SEASON_SPLIT = (4, 4, 4)
EVENT_WEEKS = (
    # The pre-registered excluded weeks are Easter week 2016 and Christmas to
    # New Year 2015, both in the warm-up. These are the same two shapes moved
    # one year forward into the scoring window, chosen after the data, and the
    # report says so beside the cut.
    ("Easter", dt.date(2017, 4, 10), dt.date(2017, 4, 16)),
    ("Christmas and New Year", dt.date(2016, 12, 24), dt.date(2017, 1, 1)),
)
METHODS = ("engine",) + baselines.ORDER

TABLE1_NOTE = (
    "These are forecast errors in rooms, not revenue. A method that forecasts a "
    "night better is not thereby shown to earn more: nothing in this table prices "
    "a night, and no rate other than the one that was charged was ever offered to "
    "a guest. Errors are in rooms and as a share of mean capacity, and capacity is "
    "sellable rooms minus the comp rooms already entered on the forecast day.")
LEAD1_NOTE = (
    "Lead 1 measures late cancellations and no-shows, an overbooking question and "
    "not a demand question. The engine floors every forecast at rooms on the books "
    "(forecast.py:91), so it cannot forecast a decline; at H1, 7.5 percent of "
    "scoring nights finish below their lead-1 on the books and the bias on this "
    "line is that floor, not an error better forecasting would remove.")
SEASON_NOTE = (
    "High, shoulder and low are terciles of monthly occupancy in the warm-up year "
    "alone, four months each. They are not hotel.json's demand_season_band, which "
    "ranks months by gross demand on a 4/3/5 split and is an engine input; cutting "
    "the table by that would group the nights by the engine's own opinion. Both "
    "labellings are printed.")
EVENT_NOTE = (
    "The Easter, Christmas and New Year windows were chosen after the data, as the "
    "pre-registered excluded weeks moved one year forward into the scoring window. "
    "They are flags on a cut, not an input to any method.")


def season_cut(ledger, first: dt.date, last: dt.date,
               split=SEASON_SPLIT) -> Dict[int, str]:
    """Months ranked by mean rooms sold in the warm-up window."""
    total: Dict[int, float] = defaultdict(float)
    nights: Dict[int, int] = defaultdict(int)
    for d, row in ledger.settled.items():
        if first <= d <= last:
            total[d.month] += row["rooms_sold"]
            nights[d.month] += 1
    mean = {m: (total[m] / nights[m]) if nights[m] else 0.0 for m in range(1, 13)}
    order = sorted(range(1, 13), key=lambda m: (-mean[m], m))
    high, shoulder, _low = split
    out = {}
    for i, m in enumerate(order):
        out[m] = "high" if i < high else ("shoulder" if i < high + shoulder else "low")
    return out


def event_of(d: dt.date) -> str:
    for name, lo, hi in EVENT_WEEKS:
        if lo <= d <= hi:
            return name
    return "none"


def cell(raw: List[float], clamped: List[float], actual: List[float],
         capacity: List[float]) -> dict:
    """Mean absolute error, the same as a share of mean capacity, signed error,
    and the share of forecasts that hit the clamp."""
    n = len(actual)
    if n == 0:
        return {"n": 0, "mae": None, "mae_share": None, "bias": None, "clamp_share": None}
    errs = [c - a for c, a in zip(clamped, actual)]
    mae = fmean([abs(e) for e in errs])
    cap_mean = fmean(capacity) if capacity else 0.0
    return {"n": n, "mae": mae,
            "mae_share": (mae / cap_mean) if cap_mean > 0 else None,
            "bias": fmean(errs),
            "clamp_share": sum(1 for r, c in zip(raw, capacity) if r > c) / float(n)}


def _blank():
    return {"raw": defaultdict(list), "clamped": defaultdict(list),
            "actual": [], "capacity": []}


def _add(bucket, forecasts, act, cap):
    for method, value in forecasts.items():
        bucket["raw"][method].append(value)
        bucket["clamped"][method].append(min(value, cap))
    bucket["actual"].append(act)
    bucket["capacity"].append(cap)


def _cells(bucket) -> dict:
    methods = {}
    for method in METHODS:
        methods[method] = cell(bucket["raw"][method], bucket["clamped"][method],
                               bucket["actual"], bucket["capacity"])
    return {"n": len(bucket["actual"]),
            "capacity_mean": fmean(bucket["capacity"]) if bucket["capacity"] else None,
            "methods": methods}


def score(walk_result, hotel, bookings, nonrev, marks, score_first: dt.date,
          score_last: dt.date, warm_first: dt.date, warm_last: dt.date) -> dict:
    ledger = walk_result.ledger
    seasons = season_cut(ledger, warm_first, warm_last)
    physical = ingest.physical_occupancy(bookings)
    full_at = hotel.rooms * hotel.sellout_threshold
    leads = [int(m) for m in marks]

    overall = {}
    cuts = {"season": {}, "event": {}, "full": {}}
    for lead in leads + [pilot.LATE_MARK]:
        key = str(lead)
        whole = _blank()
        by_season = defaultdict(_blank)
        by_event = defaultdict(_blank)
        by_full = defaultdict(_blank)
        d = score_first
        while d <= score_last:
            rec = walk_result.records.get((d, lead))
            act = baselines.actual(ledger, d)
            if rec is None or rec.forecast is None or act is None:
                d += dt.timedelta(days=1)
                continue
            forecasts = {"engine": float(rec.forecast)}
            forecasts.update(baselines.all_baselines(ledger, d, lead))
            if any(v is None for v in forecasts.values()):
                d += dt.timedelta(days=1)
                continue
            cap = float(pilot.capacity_on(hotel, nonrev, d, rec.asof))
            _add(whole, forecasts, act, cap)
            _add(by_season[seasons[d.month]], forecasts, act, cap)
            _add(by_event[event_of(d)], forecasts, act, cap)
            _add(by_full["full" if physical.get(d, 0) >= full_at else "not_full"],
                 forecasts, act, cap)
            d += dt.timedelta(days=1)
        overall[key] = _cells(whole)
        for label, bucket in by_season.items():
            cuts["season"].setdefault(label, {})[key] = _cells(bucket)
        for label, bucket in by_event.items():
            cuts["event"].setdefault(label, {})[key] = _cells(bucket)
        for label, bucket in by_full.items():
            cuts["full"].setdefault(label, {})[key] = _cells(bucket)

    head = {}
    for key, block in overall.items():
        m = block["methods"]
        if m["engine"]["mae"] is None or m["average"]["mae"] is None:
            head[key] = {"engine_mae": None, "average_mae": None, "engine_better_by": None,
                         "best_single": None, "best_single_mae": None}
            continue
        singles = {name: m[name]["mae"] for name in baselines.ORDER
                   if name != "average" and m[name]["mae"] is not None}
        best = min(singles, key=singles.get) if singles else None
        beats = best is not None and singles[best] < m["average"]["mae"]
        head[key] = {"engine_mae": m["engine"]["mae"], "average_mae": m["average"]["mae"],
                     "engine_better_by": m["average"]["mae"] - m["engine"]["mae"],
                     "best_single": best if beats else None,
                     "best_single_mae": singles[best] if beats else None}

    return {"leads": [str(m) for m in leads], "late_lead": str(pilot.LATE_MARK),
            "methods": list(METHODS),
            "season_of_month": {str(m): s for m, s in seasons.items()},
            "overall": overall,
            "cuts": cuts,
            "headline": head,
            "notes": [TABLE1_NOTE, LEAD1_NOTE, SEASON_NOTE, EVENT_NOTE,
                      baselines.BASELINE_NOTE]}
