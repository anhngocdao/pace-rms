"""Antonio, de Almeida and Nunes (2019) hotel booking demand data into Pace's
booking log.  Two Portuguese hotels, arrivals 1 July 2015 to 31 August 2017,
CC BY 4.0.  The file itself is never committed.

Duplicated rows are kept on purpose: the data is anonymised, so an identical
row cannot be told apart from a distinct room.  The audit prints the room
count with and without them.
"""
import csv
import datetime as dt
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Tuple

from pace import ingest as PI

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}
HOTEL_CODE = {"Resort Hotel": "H1", "City Hotel": "H2"}
GROUP_BRANCHES = ("GROUPS", "OFFLINE_TO_GROUP", "TP_CLUSTER")
CHANNEL_BRANCH = {"Direct": "UNDEFINED_CH_DIRECT", "Corporate": "UNDEFINED_CH_CORPORATE",
                  "GDS": "UNDEFINED_CH_GDS", "TA/TO": "UNDEFINED_CH_TATO"}
OUT_COLUMNS = ["booking_id", "booked_on", "arrival", "nights", "rooms", "rate", "currency", "segment",
               "rate_code", "source", "room_type", "company", "status", "status_date", "updated_on"]


class ConvertStop(RuntimeError):
    """The data needs a human decision before conversion may continue."""


def _empty(v: str) -> bool:
    return v is None or v.strip() in ("", "NULL", "NA")


def _norm(v: str) -> str:
    """Case- and whitespace-insensitive key for a categorical value such as
    customer_type. A booking-log source at schema version 0 has not been
    checked against a real PMS export's own capitalisation, so a rule built
    against one file's spelling (Transient-party) must not go silently dead
    against another's (Transient-Party) or a stray leading space."""
    return (v or "").strip().lower()


def arrival_of(r: dict) -> dt.date:
    return dt.date(int(r["arrival_date_year"]), MONTHS[r["arrival_date_month"]], int(r["arrival_date_day_of_month"]))


def nights_of(r: dict) -> int:
    return int(r["stays_in_weekend_nights"]) + int(r["stays_in_week_nights"])


def status_of(r: dict) -> Tuple[str, str]:
    s = r["reservation_status"]
    if s == "Check-Out":
        return "stayed", ""
    if s == "No-Show":
        return "no_show", ""
    if s == "Canceled":
        return "cancelled", r["reservation_status_date"]
    raise ValueError("unknown reservation_status %r" % s)


def cluster_transient_party(rows: List[dict], threshold: int) -> set:
    groups: Dict[tuple, List[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        if _norm(r["customer_type"]) != "transient-party":
            continue
        agent = "" if _empty(r["agent"]) else r["agent"].strip()
        company = "" if _empty(r["company"]) else r["company"].strip()
        if not agent and not company:
            continue
        key = (r["hotel"], r["market_segment"], arrival_of(r), nights_of(r), int(r["lead_time"]), agent, company)
        groups[key].append(i)
    return {i for members in groups.values() if len(members) >= threshold for i in members}


def _table_branch(r: dict) -> str:
    seg, ct = r["market_segment"], r["customer_type"]
    if seg == "Direct":
        return "DIRECT"
    if seg == "Online TA":
        return "ONLINE_TA"
    if seg == "Corporate":
        return "CORPORATE"
    if seg == "Aviation":
        return "AVIATION"
    if seg == "Offline TA/TO":
        return "OFFLINE_TO_CONTRACT" if _norm(ct) == "contract" else "OFFLINE_TO_TRANSIENT"
    if seg == "Undefined":
        return CHANNEL_BRANCH.get(r["distribution_channel"], "UNDEFINED_FALLBACK")
    raise ValueError("unknown market_segment %r" % seg)


def branch_rows(rows: List[dict], settings: dict) -> Tuple[List[dict], Counter, List[str]]:
    """Apply the rules in the spec's order and write one booking-log row per input row."""
    clustered = cluster_transient_party(rows, int(settings["group_threshold_rooms"]))
    out: List[dict] = []
    counts: Counter = Counter()
    notes: List[str] = []
    per_hotel = Counter()
    undefined_nights = Counter()
    total_nights = Counter()
    for i, r in enumerate(rows):
        seg, ct = r["market_segment"], r["customer_type"]
        status, status_date = status_of(r)
        nights = nights_of(r)
        adr = float(r["adr"])
        if seg == "Complementary":
            branch = "COMP"
        elif seg == "Groups":
            branch = "GROUPS"
        elif seg == "Offline TA/TO" and _norm(ct) == "group":
            branch = "OFFLINE_TO_GROUP"
        elif i in clustered:
            branch = "TP_CLUSTER"
        elif adr == 0 and nights > 0 and status in ("stayed", "no_show"):
            branch = "ADR0"
        else:
            branch = _table_branch(r)
        counts[branch] += 1
        code = HOTEL_CODE[r["hotel"]]
        per_hotel[code] += 1
        if status == "stayed":
            total_nights[code] += nights
            if seg == "Undefined":
                undefined_nights[code] += nights
        arrival = arrival_of(r)
        agent = "" if _empty(r["agent"]) else r["agent"].strip()
        company = "" if _empty(r["company"]) else r["company"].strip()
        out.append({
            "booking_id": "%s-%06d" % (code, per_hotel[code]),
            "booked_on": (arrival - dt.timedelta(days=int(r["lead_time"]))).isoformat(),
            "arrival": arrival.isoformat(), "nights": str(nights), "rooms": "1",
            "rate": r["adr"], "currency": "EUR", "segment": branch, "rate_code": "",
            "source": r["distribution_channel"], "room_type": r["reserved_room_type"],
            "company": company or agent, "status": status, "status_date": status_date, "updated_on": "",
            "_branch": branch, "_raw": r,
        })
    for code in total_nights:
        share = undefined_nights[code] / total_nights[code] if total_nights[code] else 0.0
        if share > float(settings["undefined_stop_share"]):
            raise ConvertStop("%s: Undefined market segment is %.1f%% of stayed room nights; decide the mapping by hand"
                              % (code, 100 * share))
        notes.append("%s: Undefined share of stayed room nights %.3f%%" % (code, 100 * share))
    if counts["UNDEFINED_FALLBACK"]:
        notes.append("%d rows had Undefined segment and Undefined channel; mapped to RETAIL with a warning" % counts["UNDEFINED_FALLBACK"])
    return out, counts, notes


WARMUP = (dt.date(2015, 7, 1), dt.date(2016, 6, 30))
EXCLUDED_WEEKS = [(dt.date(2016, 3, 21), dt.date(2016, 3, 27)),   # Easter week 2016
                  (dt.date(2015, 12, 24), dt.date(2016, 1, 1))]
BAR_BRANCHES = {"DIRECT", "ONLINE_TA"}
CORP_BRANCHES = {"CORPORATE", "AVIATION", "OFFLINE_TO_CONTRACT", "OFFLINE_TO_TRANSIENT"}
TARGET_OF = {"COMP": "NONREV", "ADR0": "NONREV", "GROUPS": "GROUP", "OFFLINE_TO_GROUP": "GROUP",
             "TP_CLUSTER": "GROUP", "CORPORATE": "CORP", "AVIATION": "CORP", "OFFLINE_TO_CONTRACT": "CORP",
             "DIRECT": "RETAIL", "ONLINE_TA": "OTA", "UNDEFINED_FALLBACK": "RETAIL"}


def _d(o: dict) -> dt.date:
    return dt.date.fromisoformat(o["arrival"])


def _in_window(o: dict, window) -> bool:
    return window[0] <= _d(o) <= window[1]


def _excluded(o: dict) -> bool:
    d = _d(o)
    return any(a <= d <= b for a, b in EXCLUDED_WEEKS)


def _percentile(values: List[float], p: float) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def basket(out_rows, branches: set, window=WARMUP, settings=None, min_rows=None, exclude_weeks=True):
    cand = [o for o in out_rows if o["_branch"] in branches and o["status"] == "stayed"
            and float(o["rate"]) > 0 and _in_window(o, window) and not (exclude_weeks and _excluded(o))]
    if not cand:
        return [], False
    rates = [float(o["rate"]) for o in cand]
    lo, hi = _percentile(rates, 0.01), _percentile(rates, 0.99)
    cand = [o for o in cand if lo <= float(o["rate"]) <= hi]
    room = Counter(o["room_type"] for o in cand).most_common(1)[0][0]
    meal = Counter(o["_raw"]["meal"] for o in cand).most_common(1)[0][0]
    cand = [o for o in cand if o["room_type"] == room and o["_raw"]["meal"] == meal
            and o["_raw"]["children"] in ("0", "0.0", "") and o["_raw"]["babies"] == "0"]
    strict = [o for o in cand if o["_raw"]["adults"] == "2"]
    need = min_rows if min_rows is not None else int((settings or {}).get("min_basket_rows", 30))
    if len(strict) >= need:
        return strict, False
    return cand, True


def monthly_median(rows) -> Dict[int, float]:
    by_m: Dict[int, List[float]] = defaultdict(list)
    for o in rows:
        by_m[_d(o).month].append(float(o["rate"]))
    return {m: statistics.median(v) for m, v in by_m.items() if v}


def _monthly_basket_medians(out_rows, settings) -> Tuple[Dict[int, float], List[int]]:
    """The twelve per-month BAR basket medians, widening any month whose
    two-adult pool is thinner than min_basket_rows and filling any month
    with no BAR rows at all from the cross-month median of the months that
    do have one. price_month_factor rescales these twelve numbers to a mean
    of one; base_rate divides these same twelve numbers by that same
    rescaling. Both read this one function so a month that price_month_factor
    widens cannot silently disappear from base_rate's own per-month view,
    which is what happened when base_rate had its own separate, whole-window
    basket() call instead."""
    need = int(settings.get("min_basket_rows", 30))
    strict, _ = basket(out_rows, BAR_BRANCHES, settings=settings, min_rows=0)
    wide, _ = basket(out_rows, BAR_BRANCHES, settings=settings, min_rows=10 ** 9)
    med: Dict[int, float] = {}
    widened: List[int] = []
    for m in range(1, 13):
        s = [o for o in strict if _d(o).month == m]
        if len(s) >= need:
            med[m] = statistics.median(float(o["rate"]) for o in s)
        else:
            w = [o for o in wide if _d(o).month == m]
            widened.append(m)
            med[m] = statistics.median(float(o["rate"]) for o in w) if w else float("nan")
    known = [v for v in med.values() if v == v]
    if not known:
        raise ConvertStop("no BAR (DIRECT/ONLINE_TA) rows in the warm-up window; nothing to build a monthly basket on")
    fill = statistics.median(known)
    return {m: (v if v == v else fill) for m, v in med.items()}, widened


def price_month_factor(out_rows, settings) -> Tuple[Dict[int, float], List[int]]:
    med, widened = _monthly_basket_medians(out_rows, settings)
    mean = sum(med.values()) / 12
    if mean <= 0:
        raise ConvertStop("price_month_factor's monthly medians average to zero or less; check the adr column")
    return {m: v / mean for m, v in med.items()}, widened


def base_rate(out_rows, factors, settings) -> float:
    med, _ = _monthly_basket_medians(out_rows, settings)
    return statistics.median(med[m] / factors[m] for m in med)


def floor_ceiling(out_rows, settings) -> Tuple[float, float]:
    rows, _ = basket(out_rows, BAR_BRANCHES, settings=settings)
    rates = [float(o["rate"]) for o in rows]
    widen = float(settings.get("floor_ceiling_widen", 0.10))
    return round(_percentile(rates, 0.02) * (1 - widen), 2), round(_percentile(rates, 0.98) * (1 + widen), 2)


def rate_step(floor: float, ceiling: float) -> float:
    raw = (ceiling - floor) / 90.0
    for step in (1.0, 2.0, 5.0):
        if raw <= step * 1.5:
            return step
    return 5.0


def _gross_nights(out_rows, window=WARMUP, drop_duplicates=False) -> Dict[int, float]:
    seen = set()
    gross: Dict[int, float] = defaultdict(float)
    for o in out_rows:
        if not _in_window(o, window) or o["_branch"] == "COMP":
            continue
        if o["_raw"]["deposit_type"] == "Non Refund":
            continue
        if drop_duplicates:
            key = tuple(sorted((k, v) for k, v in o["_raw"].items()))
            if key in seen:
                continue
            seen.add(key)
        nights = int(o["nights"])
        for k in range(nights):
            gross[(_d(o) + dt.timedelta(days=k)).month] += 1
    return gross


def _rank_bands(by_month: Dict[int, float]) -> Dict[int, str]:
    order = sorted(range(1, 13), key=lambda m: (-by_month.get(m, 0.0), m))
    return {**{m: "peak" for m in order[:4]}, **{m: "shoulder" for m in order[4:7]}, **{m: "trough" for m in order[7:]}}


def demand_season_band(out_rows, window=WARMUP):
    gross = _gross_nights(out_rows, window)
    occ: Dict[int, float] = defaultdict(float)
    for o in out_rows:
        if o["status"] == "stayed" and _in_window(o, window):
            for k in range(int(o["nights"])):
                occ[(_d(o) + dt.timedelta(days=k)).month] += 1
    return _rank_bands(gross), _rank_bands(occ), {m: gross.get(m, 0.0) for m in range(1, 13)}


def _weighted_median(pairs: List[Tuple[float, float]]) -> float:
    """Median of values weighted by weight. When the cumulative weight lands
    on exactly half the total, this is the boundary between two values with
    equal standing, so the result averages them, the same way a plain median
    averages the two middle values of an even-length list; that is the case
    an equal-weight pair reduces to. Otherwise it is the first value whose
    cumulative weight passes half."""
    pairs = sorted(pairs)
    if not pairs:
        return float("nan")
    total = sum(w for _, w in pairs)
    acc = 0.0
    for i, (v, w) in enumerate(pairs):
        acc += w
        if total and acc == total / 2 and i + 1 < len(pairs):
            return (v + pairs[i + 1][0]) / 2
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def segment_rate_ratio(out_rows, settings):
    need = int(settings.get("min_ratio_rows", 30))
    # The public-rate side reads the same per-month decision price_month_factor
    # and base_rate read, so a month thin enough to widen still gets a real
    # median here (and votes below) instead of silently missing a key the
    # way a plain basket() plus monthly_median() would drop it. The row-count
    # gate below still needs a row-level pool, so it reads the wide (party
    # size not filtered) basket for the same month: that is the pool that
    # actually produced bar_med[m] whether or not the month widened, since a
    # non-widened month's wide pool is a superset of the exact rows its
    # median came from and a widened month's wide pool is exactly those rows.
    bar_med, _ = _monthly_basket_medians(out_rows, settings)
    bar_rows, _ = basket(out_rows, BAR_BRANCHES, settings=settings, min_rows=10 ** 9)
    per_origin: Dict[str, float] = {}
    weights: Dict[str, float] = {}
    by_band: Dict[str, Dict[str, float]] = defaultdict(dict)
    bands, _, _ = demand_season_band(out_rows)
    for origin in sorted(CORP_BRANCHES | set(GROUP_BRANCHES)):
        rows, _ = basket(out_rows, {origin}, settings=settings, min_rows=0)
        by_m: Dict[int, List[float]] = defaultdict(list)
        for o in rows:
            by_m[_d(o).month].append(float(o["rate"]))
        monthly = [statistics.median(v) / bar_med[m] for m, v in by_m.items()
                   if len(v) >= need and sum(1 for o in bar_rows if _d(o).month == m) >= need]
        if monthly:
            per_origin[origin] = statistics.median(monthly)
            weights[origin] = float(sum(int(o["nights"]) for o in rows))
            for band in ("peak", "shoulder", "trough"):
                sel = [statistics.median(v) / bar_med[m] for m, v in by_m.items() if bands[m] == band and len(v) >= need]
                if sel:
                    by_band[origin][band] = statistics.median(sel)
    ratios = {}
    for target, origins in (("CORP", CORP_BRANCHES), ("GROUP", set(GROUP_BRANCHES))):
        pairs = [(per_origin[o], weights[o]) for o in origins if o in per_origin]
        if pairs:
            ratios[target] = round(_weighted_median(pairs), 4)
    return ratios, per_origin, dict(by_band)


def _corr(xs: List[float], ys: List[float]) -> float:
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs); syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sxx * syy) ** 0.5


def _weekly_residuals(rows, block: str) -> Dict[int, float]:
    """Weekly median adr minus its month (or two-week block) mean, keyed by week number."""
    weekly: Dict[int, List[float]] = defaultdict(list)
    week_block: Dict[int, str] = {}
    for o in rows:
        w = int(o["_raw"]["arrival_date_week_number"])
        weekly[w].append(float(o["rate"]))
        d = _d(o)
        week_block[w] = ("%d-%02d" % (d.year, d.month)) if block == "month" else ("%d-%02d" % (d.year, w // 2))
    med = {w: statistics.median(v) for w, v in weekly.items()}
    blocks: Dict[str, List[float]] = defaultdict(list)
    for w, v in med.items():
        blocks[week_block[w]].append(v)
    bmean = {b: sum(v) / len(v) for b, v in blocks.items()}
    return {w: v - bmean[week_block[w]] for w, v in med.items()}


def bar_test(out_rows, settings) -> dict:
    thr = float(settings.get("bar_corr_threshold", 0.6))
    need = int(settings.get("min_basket_rows", 30))
    bar_rows, _ = basket(out_rows, BAR_BRANCHES, settings=settings, min_rows=need)
    to_rows, _ = basket(out_rows, {"OFFLINE_TO_TRANSIENT"}, settings=settings, min_rows=need)
    result = {"weeks": 0, "residual_corr_month": float("nan"), "residual_corr_2wk": float("nan"), "verdict": "CORP"}
    if not bar_rows or not to_rows:
        result["verdict"] = "inconclusive"
        return result
    corrs = []
    for block in ("month", "2wk"):
        a, b = _weekly_residuals(bar_rows, block), _weekly_residuals(to_rows, block)
        weeks = sorted(set(a) & set(b))
        result["weeks"] = len(weeks)
        c = _corr([a[w] for w in weeks], [b[w] for w in weeks])
        result["residual_corr_month" if block == "month" else "residual_corr_2wk"] = c
        corrs.append(c)
    votes = [c == c and c > thr for c in corrs]
    if all(votes):
        result["verdict"] = "OTA"
    elif any(votes):
        result["verdict"] = "inconclusive"
    return result


def _majority_target_by_channel(out_rows, window=WARMUP) -> Dict[str, str]:
    """What an Undefined-segment row of a given channel should map to, read
    only from the warm-up window: this is a derived fact (Task 11's rule is
    that history is read from the first twelve settled months and then
    frozen), so a row from the scoring period must not be able to shift it."""
    votes: Dict[str, Counter] = defaultdict(Counter)
    for o in out_rows:
        if o["_branch"].startswith("UNDEFINED") or not _in_window(o, window):
            continue
        votes[o["_raw"]["distribution_channel"]][TARGET_OF.get(o["_branch"], "CORP")] += 1
    return {ch: c.most_common(1)[0][0] for ch, c in votes.items()}


def derive_hotel_json(out_rows, hotel_code: str, settings: dict) -> dict:
    factors, widened = price_month_factor(out_rows, settings)
    br = base_rate(out_rows, factors, settings)
    floor, ceiling = floor_ceiling(out_rows, settings)
    bands_gross, bands_occ, gross = demand_season_band(out_rows)
    ratios, per_origin, by_band = segment_rate_ratio(out_rows, settings)
    verdict = bar_test(out_rows, settings)
    warm = [o for o in out_rows if _in_window(o, WARMUP)]
    leads = [int(o["_raw"]["lead_time"]) for o in warm]
    nights = [int(o["nights"]) for o in warm]
    max_lead = int(_percentile(leads, 0.99)) if leads else 180
    notes = []
    if hotel_code == "H1" and max_lead < 120:
        notes.append("max_lead raised from %d to 120 so the 120-day mark can run" % max_lead)
        max_lead = 120
    majority = _majority_target_by_channel(out_rows, window=WARMUP)
    seg_map = dict(TARGET_OF)
    seg_map["OFFLINE_TO_TRANSIENT"] = "OTA" if verdict["verdict"] == "OTA" else "CORP"
    for ch, branch in CHANNEL_BRANCH.items():
        seg_map[branch] = majority.get(ch, "RETAIL")
    return {
        "name": {"H1": "H1 Resort Hotel, Algarve", "H2": "H2 City Hotel, Lisbon"}[hotel_code],
        "currency": "EUR", "fx": {}, "sellable_rooms": None, "rates_include_tax": "unknown",
        "group_threshold_rooms": int(settings["group_threshold_rooms"]), "detect_groups": False,
        "rate_floor": floor, "rate_ceiling": ceiling, "rate_step": rate_step(floor, ceiling),
        "base_rate": round(br, 2),
        "variable_cost": round(br * float(settings["variable_cost_low_share"]), 2),
        "max_lead": max_lead, "max_los": max(1, int(_percentile(nights, 0.99))) if nights else 7,
        "sellout_threshold": float(settings["sellout_threshold"]),
        "price_month_factor": {str(m): round(f, 4) for m, f in factors.items()},
        "demand_season_band": {str(m): b for m, b in bands_gross.items()},
        "segment_rate_ratio": ratios,
        "segment_commission": {"RETAIL": 0.0, "OTA": float(settings["ota_commission_main"]), "CORP": 0.0, "GROUP": 0.0},
        "events": [],
        "segment_map_order": ["segment"],
        "segment_map": {"segment": seg_map},
        "_derivation": {"window": [WARMUP[0].isoformat(), WARMUP[1].isoformat()], "widened_months": widened,
                        "bands_from_occupancy": {str(m): b for m, b in bands_occ.items()},
                        "gross_by_month": {str(m): v for m, v in gross.items()},
                        "ratio_per_origin": per_origin, "ratio_by_band": by_band, "bar_test": verdict, "notes": notes},
    }


def _to_booking(o: dict, row_no: int) -> PI.Booking:
    """One booking-log row, already produced by branch_rows, as the same
    Booking dataclass pace.ingest.infer_sellable_rooms reads. This calls the
    real inference rather than a second copy of it, so the audit's duplicate
    room count and pace/ingest.py's own stay only ever disagree because the
    input differs (with duplicates kept versus dropped), never because the
    counting logic drifted apart."""
    def _pd(s):
        return dt.date.fromisoformat(s) if s else None
    return PI.Booking(
        booking_id=o["booking_id"], booked_on=dt.date.fromisoformat(o["booked_on"]),
        arrival=dt.date.fromisoformat(o["arrival"]), nights=int(o["nights"]), rooms=int(o["rooms"]),
        rate=float(o["rate"]), currency=o["currency"], segment=o["segment"], rate_code=o["rate_code"],
        source=o["source"], room_type=o["room_type"], company=o["company"], status=o["status"],
        status_date=_pd(o["status_date"]), updated_on=_pd(o["updated_on"]), row=row_no)


def audit(out_rows, hotel_code: str, settings: dict, derived: dict) -> str:
    raw = [o["_raw"] for o in out_rows]
    counts = Counter(o["_branch"] for o in out_rows)
    lines = ["# Audit %s" % hotel_code, "", "## Rows per rule branch", ""]
    lines += ["- %s: %d" % (b, n) for b, n in counts.most_common()]
    adr0 = Counter((o["_raw"]["market_segment"], o["status"]) for o in out_rows if float(o["rate"]) == 0)
    lines += ["", "## adr = 0 rows by market segment and status", ""] + ["- %s / %s: %d" % (k[0], k[1], n) for k, n in adr0.most_common()]
    cross: Dict[str, Counter] = defaultdict(Counter)
    for r in raw:
        cross[r["market_segment"]][r["distribution_channel"]] += 1
    lines += ["", "## market_segment by distribution_channel", ""] + ["- %s: %s" % (s, dict(c)) for s, c in cross.items()]
    av_nights = {o["arrival"] for o in out_rows if o["_branch"] == "AVIATION"}
    lines += ["", "## Aviation", "", "- rows: %d, nights with at least one Aviation room: %d" % (counts["AVIATION"], len(av_nights))]
    dup = Counter(tuple(sorted(r.items())) for r in raw)
    dups = [k for k, n in dup.items() if n > 1]
    dup_status = Counter(dict(k)["reservation_status"] for k in dups)
    extra_copies = sum(dup[k] - 1 for k in dups)
    shared = sum(1 for k in dups if not (_empty(dict(k)["agent"]) and _empty(dict(k)["company"])))
    seen = set()
    deduped = []
    for o in out_rows:
        key = tuple(sorted(o["_raw"].items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(o)
    with_dup = PI.infer_sellable_rooms([_to_booking(o, i) for i, o in enumerate(out_rows)])
    without_dup = PI.infer_sellable_rooms([_to_booking(o, i) for i, o in enumerate(deduped)])
    lines += ["", "## Duplicate rows", "",
              "- identical rows appearing more than once: %d, extra copies beyond the first: %d, by status %s"
              % (len(dups), extra_copies, dict(dup_status)),
              "- of those, %d share a non-empty agent or company with the other copy on the same arrival "
              "(a check on the group explanation: an identical row is more likely a second room of the "
              "same party than an anonymisation collision when it carries an agent or company code)" % shared,
              "- inferred sellable rooms with duplicates: %d (busiest night %s); without duplicates: %d (busiest night %s)"
              % (with_dup.rooms, with_dup.peak_night, without_dup.rooms, without_dup.peak_night)]
    hb = [float(o["rate"]) for o in out_rows if o["_raw"]["meal"] == "HB" and o["status"] == "stayed" and float(o["rate"]) > 0]
    bb = [float(o["rate"]) for o in out_rows if o["_raw"]["meal"] == "BB" and o["status"] == "stayed" and float(o["rate"]) > 0]
    if hb and bb:
        lines += ["", "## Meal check", "", "- median adr HB %.2f vs BB %.2f (same hotel, all room types); a steady large gap means adr includes meals"
                  % (statistics.median(hb), statistics.median(bb))]
    canc = [r for r in raw if r["reservation_status"] == "Canceled"]
    nr = [r for r in raw if r["deposit_type"] == "Non Refund"]
    lines += ["", "## Cancellations", "",
              "- cancellation rate all rows: %.1f%%" % (100 * len(canc) / max(1, len(raw))),
              "- cancellation rate excluding Non Refund: %.1f%%" % (100 * sum(1 for r in canc if r["deposit_type"] != "Non Refund") / max(1, len(raw) - len(nr))),
              "- Non Refund rows: %d, of which cancelled: %d" % (len(nr), sum(1 for r in nr if r["reservation_status"] == "Canceled"))]
    leads = sorted(int(r["lead_time"]) for r in raw)
    lines += ["", "## Lead time", "", "- median %d, p90 %d, p99 %d, max %d" % (leads[len(leads) // 2], leads[int(0.9 * (len(leads) - 1))], leads[int(0.99 * (len(leads) - 1))], leads[-1])]
    d = derived["_derivation"]
    lines += ["", "## Derived hotel.json (window %s to %s)" % tuple(d["window"]), "",
              "- base_rate %.2f, floor %.2f, ceiling %.2f, step %.1f" % (derived["base_rate"], derived["rate_floor"], derived["rate_ceiling"], derived["rate_step"]),
              "- price_month_factor %s" % derived["price_month_factor"],
              "- widened months %s" % d["widened_months"],
              "- demand bands from gross demand %s" % derived["demand_season_band"],
              "- demand bands from occupancy %s" % d["bands_from_occupancy"],
              "- segment_rate_ratio %s (simulated hotel: CORP 0.82, GROUP 0.70)" % derived["segment_rate_ratio"],
              "- ratio per origin %s" % d["ratio_per_origin"],
              "- ratio by band %s" % d["ratio_by_band"],
              "- BAR test %s" % d["bar_test"],
              "- notes %s" % d["notes"]]
    return "\n".join(lines) + "\n"


def convert(csv_path: str, out_dir: str, settings_path: str) -> None:
    with open(settings_path, encoding="utf-8") as fh:
        settings = json.load(fh)
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    os.makedirs(out_dir, exist_ok=True)
    audits = []
    for hotel_name, code in HOTEL_CODE.items():
        sub = [r for r in rows if r["hotel"] == hotel_name]
        out, counts, notes = branch_rows(sub, settings)
        derived = derive_hotel_json(out, code, settings)
        derived["_derivation"]["notes"] += notes
        text = audit(out, code, settings, derived)
        with open(os.path.join(out_dir, "%s-bookings.csv" % code.lower()), "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=OUT_COLUMNS, extrasaction="ignore")
            w.writeheader()
            w.writerows(out)
        clean = {k: v for k, v in derived.items() if k != "_derivation"}
        with open(os.path.join(out_dir, "%s-hotel.json" % code.lower()), "w", encoding="utf-8") as fh:
            json.dump(clean, fh, indent=2)
        audits.append(text)
        print(text)
    with open(os.path.join(out_dir, "audit.md"), "w", encoding="utf-8") as fh:
        fh.write("\n\n".join(audits))


def main(argv):
    if len(argv) != 4:
        print("usage: python3 -m tools.convert_antonio hotels.csv out_dir settings.json")
        return 1
    convert(argv[1], argv[2], argv[3])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
