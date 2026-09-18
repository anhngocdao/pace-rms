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
        if r["customer_type"] != "Transient-party":
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
        return "OFFLINE_TO_CONTRACT" if ct == "Contract" else "OFFLINE_TO_TRANSIENT"
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
        elif seg == "Offline TA/TO" and ct == "Group":
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
