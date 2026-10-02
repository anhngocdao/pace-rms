"""The pilot report.

Three opening lines before any number, because all three change how every
number below them should be read: the room count is inferred, the rates are of
unknown tax treatment and appear to include board, and the export holds only
each booking's final state.

Every number goes through one formatter. A win and a loss are printed by the
same code path and in the same words, so nothing can be rounded in the engine's
favour by accident.

Table 3 is rendered from the per-combination gate: a combination quotes an
estimate only when its own censored nights reach the gate, and the section
opens with the grid's summed, distinct and largest counts so a reader sees
that a sum over the grid is not a sample.
"""
import datetime as dt
import os
from typing import List, Optional

from . import baselines
from . import holdout
from . import score as scoring
from .pilot import PilotError

MISSING = "n/a"
UNDEFINED_BOARD = "board not recorded (Undefined in the export)"


def num(x, nd: int = 2) -> str:
    if x is None:
        return MISSING
    return ("%%.%df" % nd) % float(x)


def pct(x, nd: int = 1) -> str:
    if x is None:
        return MISSING
    return ("%%.%df%%%%" % nd) % (100.0 * float(x))


def board_label(meal: str) -> str:
    """The export writes the literal "Undefined" for the rows whose board was
    never recorded (1,169 at H1), and they reach table2.meals as a board of
    their own.  Printed under a name that says what it is."""
    return UNDEFINED_BOARD if meal == "Undefined" else meal


def check_full(payload: dict, path: str = "") -> None:
    """Refuse anything but a full run with both tables.

    The pair joins full runs only: a quick run scored a different window and
    a payload without table 2 or 3 is stale or partial, and either would sit
    beside a real run looking like one.
    """
    where = path or ("the %s payload" % payload.get("hotel", {}).get("code", "?"))
    label = payload.get("label")
    if label != "full":
        raise PilotError("%s is a %r run, not a full one, so pilot-report refuses it: a run "
                         "that did not score the pre-registered window cannot stand beside "
                         "one that did. Regenerate it with `python3 run.py pilot <csv> "
                         "<hotel.json>` and no --quick." % (where, label))
    for name in ("table2", "table3"):
        if not isinstance(payload.get(name), dict):
            raise PilotError("%s has no %s, so pilot-report refuses it: a full run writes both "
                             "tables, and a payload without one is stale or partial. "
                             "Regenerate it with `python3 run.py pilot <csv> <hotel.json>`."
                             % (where, name))


def audit_section(path: str, code: str) -> Optional[str]:
    """The block of data/antonio/audit.md headed `# Audit <code>`."""
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    blocks = text.split("\n# ")
    for i, block in enumerate(blocks):
        head = block if i == 0 else "# " + block
        first = head.splitlines()[0] if head.splitlines() else ""
        if first.strip().lower() == ("# audit %s" % code).lower():
            return head.strip()
    return None


def meal_check(audit_text: Optional[str]) -> Optional[str]:
    if not audit_text:
        return None
    for line in audit_text.splitlines():
        if "median adr HB" in line:
            return line.lstrip("- ").strip()
    return None


def opening_lines(payload: dict, audit_text: Optional[str]) -> List[str]:
    h = payload["hotel"]
    meal = meal_check(audit_text)
    # The inference block can carry None on a fixture too short to say which
    # night was busiest; the sentence then says n/a rather than failing to print.
    one = ("1. The room count is inferred, not stated: %d rooms, from the busiest observed "
           "night (%s), with %s nights within 2 percent of it and a second highest of %s; "
           "yearly maxima %s. An inferred count is biased low, so every night that reads as "
           "full reads that way against a number this project computed."
           % (h["rooms"], h["peak_night"] or MISSING, num(h["nights_within_2pct"], 0),
              num(h["second_highest"], 0), h["per_year_max"] or MISSING))
    if not h["rooms_inferred"]:
        one = "1. The room count was stated in hotel.json: %d rooms." % h["rooms"]
    two = ("2. The rates are of unknown tax treatment: hotel.json says rates_include_tax is "
           "%s, and nothing in the data speaks to tax. On board the data does speak: %s, a "
           "wide and steady gap, so the rate appears to include board."
           % (h["rates_include_tax"], meal or "the meal check is not in the audit file"))
    three = ("3. There is no booking change history in this export. Each booking appears in "
             "its final state, so a guest who moved dates is replayed onto the new dates from "
             "the original booking day and pickup reflects final state rather than the state "
             "on the day of booking. Group booked_on is often the rooming-list entry date and "
             "unpicked blocks are absent, so nothing about group pickup should be concluded "
             "from this source.")
    return [one, two, three]


def _title(payload: dict) -> str:
    title = "# Pilot: %s" % payload["hotel"]["name"]
    if payload["label"] != "full":
        title += " (%s run)" % payload["label"]
    return title


def _warnings(d: dict) -> str:
    return ", ".join("%s %d" % (k, v) for k, v in sorted(d.items())) or "none"


def _method_table(block: dict) -> List[str]:
    lines = ["| method | n | MAE rooms | MAE of capacity | bias rooms | hit the clamp |",
             "|---|---|---|---|---|---|"]
    for method in scoring.METHODS:
        cell = block["methods"].get(method)
        if cell is None:
            continue
        name = "Pace engine" if method == "engine" else baselines.NAMES[method]
        lines.append("| %s | %d | %s | %s | %s | %s |"
                     % (name, cell["n"], num(cell["mae"]), pct(cell["mae_share"]),
                        num(cell["bias"]), pct(cell["clamp_share"])))
    return lines


def _headline_line(lead: str, head: dict) -> str:
    if head["engine_mae"] is None:
        return "- lead %s: no night was scored by every method." % lead
    gap = head["engine_better_by"]
    word = "better" if gap >= 0 else "worse"
    text = ("- lead %s: the engine is %s by %s rooms than the average of additive pickup and "
            "last year (%s against %s)."
            % (lead, word, num(abs(gap)), num(head["engine_mae"]), num(head["average_mae"])))
    if head["best_single"]:
        text += (" A single baseline beats the average here: %s at %s."
                 % (baselines.NAMES[head["best_single"]], num(head["best_single_mae"])))
    return text


def _combo_head(c: dict) -> str:
    return "%s / %s / %s" % (pct(c["threshold"], 0), pct(c["cap"], 0), c["rule_name"])


def _table3_lines(t3: dict) -> List[str]:
    """The grid from its per-combination gate.

    Counts for every combination; means and buckets only for a scorable one;
    then every censored night once, with the number of combinations that
    censored it, so ten nights under different caps read as ten nights.
    """
    out = []
    for note in t3["notes"]:
        out += ["> %s" % note, ""]
    gate = t3["min_scored"]
    n_combos = len(t3["combos"])
    out += ["- neighbour window %d nights; %d censored nights summed over the grid, %d "
            "distinct; the largest single combination reached %d against a gate of %d; "
            "%d of %d combinations scorable."
            % (t3["window"], t3["censored_scored_total"], t3["censored_distinct_nights"],
               t3["best_combo_censored"], gate, t3["scorable_combos"], n_combos), "",
            "| clean | cap | rule | cap rooms | clean nights | cut nights | room nights cut, "
            "gross | scored | censored scored | uncensored scored (mean rooms cut) | "
            "censored clean | censorable band, all and clean | scorable |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in t3["combos"]:
        out.append("| %s | %s | %s | %d | %d | %d | %d | %d | %d | %d (%s) | %d | %d and %d | %s |"
                   % (pct(c["threshold"], 0), pct(c["cap"], 0), c["rule_name"], c["cap_rooms"],
                      c["clean_nights"], c["cut_nights"], c["cut_room_nights_gross"],
                      c["scored"], c["censored_scored"], c["uncensored_scored"],
                      num(c["uncensored"]["mean_rooms_cut"]), c["censored_clean"],
                      c["censorable_band_nights"], c["censorable_band_clean"],
                      "yes" if c["scorable"] else "no"))
    out += [""]
    for c in t3["combos"]:
        if not c["scorable"]:
            out.append("- %s: %d censored scored nights, below the gate of %d; %d clean "
                       "nights, %d of them cut, %d flagged censored; no estimate."
                       % (_combo_head(c), c["censored_scored"], gate, c["clean_nights"],
                          c["cut_nights"], c["censored_clean"]))
            continue
        o = c["overall"]
        parts = ["n %d, mean known %s, mean estimate %s, MAE %s, bias %s"
                 % (o["n"], num(o["known"]), num(o["estimate"]), num(o["mae"]), num(o["bias"]))]
        for name in holdout.BUCKETS:
            b = c["buckets"][name]
            parts.append("%s: n %d, MAE %s" % (name, b["n"], num(b["mae"])))
        out.append("- %s: %s." % (_combo_head(c), "; ".join(parts)))
    seen = {}
    for c in t3["combos"]:
        done = set()
        for r in c["nights"]:
            if not r["censored"] or r["date"] in done:
                continue
            done.add(r["date"])
            entry = seen.setdefault(r["date"], {"class": r["class"], "known": r["known"],
                                                "cells": []})
            entry["cells"].append((r["capped"], r["estimate"]))
    out += ["", "### The censored nights", "",
            "Every night the capped history flagged censored in any combination, once, with "
            "the number of combinations that censored it and the range of what they handed "
            "the unconstrainer and got back. The count of distinct nights above is the "
            "length of this list.", "",
            "| night | class | known rooms | censored in | capped rooms | estimate |",
            "|---|---|---|---|---|---|"]
    for date in sorted(seen):
        e = seen[date]
        capped = [x for x, _ in e["cells"]]
        est = [y for _, y in e["cells"]]
        out.append("| %s | %s | %s | %d of %d | %s to %s | %s to %s |"
                   % (date, e["class"], num(e["known"]), len(e["cells"]), n_combos,
                      num(min(capped)), num(max(capped)), num(min(est)), num(max(est))))
    if not seen:
        out.append("| none | | | | | |")
    out += [""]
    return out


def render(payload: dict, audit_text: Optional[str] = None) -> str:
    h = payload["hotel"]
    w = payload["windows"]
    out = [_title(payload), ""]
    if payload["label"] != "full":
        out += ["> This is a %s run: it did not score the pre-registered window, and "
                "`run.py pilot-report` refuses it." % payload["label"], ""]
    out += opening_lines(payload, audit_text)
    out += ["", payload["notes"][0], ""]

    gap = payload["full_night_gap"]
    out += ["## What the ledger could not see", "",
            "- %d of %d nights in the stay window were physically full. On %d of them the "
            "settled figure fell short of the sell-out threshold of %s rooms: %s of all %d "
            "nights, %s of the %d full ones. On %d the lead-0 snapshot fell short, which is "
            "the count the unconstrainer itself worked from."
            % (gap["physically_full"], gap["nights"], gap["full_but_invisible"],
               num(gap["threshold_rooms"]), pct(gap["share_of_nights"]), gap["nights"],
               pct(gap["share_of_full_nights"]), gap["physically_full"],
               gap["full_but_uncensored_by_snapshot"]),
            "- %s" % payload["notes"][1], ""]

    p = payload["prereg"]
    out += ["## Pre-registration", "",
            "- Settings committed before the data was downloaded: commit `%s`." % p["commit"],
            "- sha256 of `%s` at run time: `%s`%s."
            % (p["settings_path"], p["settings_sha256"],
               "" if p["sha256_matches_the_recorded_one"]
               else ", WHICH DOES NOT MATCH the hash recorded in pace/pilot.py"),
            ""]

    ing = payload["ingest"]
    wk = payload["walk"]
    out += ["## What ingest read", "",
            "- %d rows in the converted log; %d bookings and %d cancellations reached the "
            "ledger; %d non-revenue rows were kept out of it."
            % (ing["rows"], ing["bookings"], ing["cancels"], ing["nonrev_rows"]),
            "- converter and ingest warnings: %s." % _warnings(ing["warnings"])]
    out += ["- %s" % note for note in ing["notes"]]
    out += ["", "## Windows and the walk", "",
            "- stay window %s to %s, trimmed %d nights off the front; scoring %s to %s (%d nights); "
            "the engine was driven from %s."
            % (w["first_stay"], w["last_stay"], w["trim_nights"], w["score_first"],
               w["score_last"], w["scoring_nights"], w["drive_from"]),
            "- walked %d days in %s seconds of wall clock and %s of CPU, %d fits, %d forecasts "
            "recorded, %d solves; warnings the walk added beyond ingest's: %s."
            % (wk["days"], num(wk["seconds"], 1), num(wk["cpu_seconds"], 1), wk["fits"],
               wk["records"], wk["solves"], _warnings(wk["over_capacity"])),
            ""]

    t1 = payload["table1"]
    out += ["## Table 1, forecast accuracy", ""]
    for note in t1["notes"]:
        out += ["> %s" % note, ""]
    for lead in t1["leads"]:
        out += ["### Lead %s, n = %d" % (lead, t1["overall"][lead]["n"]), ""]
        out += _method_table(t1["overall"][lead]) + [""]
    late = t1["late_lead"]
    out += ["### Lead %s, late cancellations and no-shows, n = %d"
            % (late, t1["overall"][late]["n"]), ""]
    out += _method_table(t1["overall"][late]) + [""]
    out += ["### Headline", ""]
    for lead in t1["leads"] + [late]:
        out.append(_headline_line(lead, t1["headline"][lead]))
    out += ["", "The headline rests on the 14 months from %s to %s, one high season."
            % (w["score_first"], w["score_last"]), ""]
    for cut_name, cut in sorted(t1["cuts"].items()):
        out += ["### Cut by %s" % cut_name, ""]
        for label in sorted(cut):
            for lead in t1["leads"]:
                block = cut[label].get(lead)
                if block is None or block["n"] == 0:
                    continue
                out += ["**%s, lead %s, n = %d**" % (label, lead, block["n"]), ""]
                out += _method_table(block) + [""]

    out += ["## Table 2, recommended rate against realised rate", ""]
    if payload["table2"] is None:
        out += ["Table 2 was not run.", ""]
    else:
        t2 = payload["table2"]
        for note in t2["notes"]:
            out += ["> %s" % note, ""]
        out += ["- room type %s (%s of priced room nights), board split over %s."
                % (t2["room_type"], pct(t2["room_type_share"]),
                   ", ".join(board_label(m) for m in t2["meals"]) or "nothing"),
                ""]
        out += ["| mark | window | n | median published | median realised | median gap | p25 | p75 | on the top rung | solve age, median and worst |",
                "|---|---|---|---|---|---|---|---|---|---|"]
        for mark in t2["marks"]:
            c = t2["cells"][mark]["all"]
            pin = t2["pinned"][mark]
            age = t2["solve_age_days"][mark]
            out.append("| %s | %s to %s | %d | %s | %s | %s | %s | %s | %s (%d of %d) | %s and %s |"
                       % (mark, t2["windows"][mark][0], t2["windows"][mark][1], c["n"],
                          num(c["median_published"]), num(c["median_realised"]),
                          num(c["median_gap"]), num(c["p25"]), num(c["p75"]),
                          pct(pin["share"]), pin["n_pinned"], pin["n"],
                          num(age["median"], 0), num(age["max"], 0)))
        out += [""]
        for mark in t2["marks"]:
            for label, group in (("board", "by_meal"), ("season", "by_season")):
                rows = t2["cells"][mark][group]
                if not rows:
                    continue
                out += ["**Mark %s by %s**" % (mark, label), "",
                        "| %s | n | median gap | p25 | p75 |" % label, "|---|---|---|---|---|"]
                for key in sorted(rows):
                    c = rows[key]
                    out.append("| %s | %d | %s | %s | %s |"
                               % (board_label(key) if group == "by_meal" else key, c["n"],
                                  num(c["median_gap"]), num(c["p25"]), num(c["p75"])))
                out += [""]
            comp = t2["cells"][mark]["nonrev_nights"]
            clean = t2["cells"][mark]["clean_nights"]
            out += ["- mark %s, nights holding a comp room: n = %d, median gap %s; nights "
                    "without one: n = %d, median gap %s. The engine sees more rooms free than "
                    "there were on the first group, so it may recommend a lower BAR on exactly "
                    "the nights that were nearly full."
                    % (mark, comp["n"], num(comp["median_gap"]), clean["n"],
                       num(clean["median_gap"])), ""]

    out += ["## Table 3, unconstraining checked by holdout", ""]
    if payload["table3"] is None:
        out += ["Table 3 was not run.", ""]
    else:
        out += _table3_lines(payload["table3"])

    if audit_text:
        out += ["## The converter's audit", "", audit_text, ""]
    return "\n".join(out) + "\n"


def render_pair(payloads: List[dict]) -> str:
    for p in payloads:
        check_full(p, p.get("_json_path", ""))
    by_code = {p["hotel"]["code"].upper(): p for p in payloads}
    codes = ["H1", "H2"]
    out = ["# Pilot, both hotels side by side", "",
           "Generated %s. Each hotel was run in its own process, because "
           "hotelconfig.apply rebinds the engine's seasonality and rewrites its segment "
           "table in place." % dt.date.today().isoformat(), ""]
    for code in codes:
        if code not in by_code:
            out.append("- %s was not run, so its column is absent below." % code)
    out.append("")
    present = [c for c in codes if c in by_code]
    for code in present:
        out.append("- %s: %s, %d rooms, scoring %s to %s."
                   % (code, by_code[code]["hotel"]["name"], by_code[code]["hotel"]["rooms"],
                      by_code[code]["windows"]["score_first"],
                      by_code[code]["windows"]["score_last"]))
    out += ["", by_code[present[0]]["notes"][0], "", "## Table 1, mean absolute error in rooms", ""]
    header = "| lead | method | " + " | ".join(present) + " |"
    out += [header, "|---|---|" + "---|" * len(present)]
    # The two hotels do not share a lead list: H1's export reaches lead 120 and
    # H2's does not, so the rows are the union, longest lead first, and a hotel
    # that never scored a lead prints n/a there rather than failing to print.
    leads = sorted({l for code in present for l in by_code[code]["table1"]["leads"]},
                   key=int, reverse=True)
    leads.append(by_code[present[0]]["table1"]["late_lead"])
    for lead in leads:
        for method in scoring.METHODS:
            name = "Pace engine" if method == "engine" else baselines.NAMES[method]
            cells = []
            for code in present:
                block = by_code[code]["table1"]["overall"].get(lead)
                cell = block["methods"].get(method) if block else None
                cells.append(num(cell["mae"]) if cell else MISSING)
            out.append("| %s | %s | %s |" % (lead, name, " | ".join(cells)))
    out += ["", "## Headline, per hotel", ""]
    for code in present:
        out.append("**%s**" % code)
        for lead in leads:
            head = by_code[code]["table1"]["headline"].get(lead)
            if head is None:
                out.append("- lead %s: not scored at this hotel, whose export does not reach "
                           "that lead." % lead)
                continue
            out.append(_headline_line(lead, head))
        out.append("")
    out += ["## Table 3, per hotel", "",
            "One hotel can quote an estimate while the other cannot; the same code path "
            "prints both, from each combination's own gate.", ""]
    for code in present:
        t3 = by_code[code]["table3"]
        out.append("**%s**: %d censored nights summed over %d combinations, %d distinct, the "
                   "largest combination %d against a gate of %d; %d scorable."
                   % (code, t3["censored_scored_total"], len(t3["combos"]),
                      t3["censored_distinct_nights"], t3["best_combo_censored"],
                      t3["min_scored"], t3["scorable_combos"]))
        for c in t3["combos"]:
            if c["scorable"]:
                o = c["overall"]
                out.append("- %s: n %d, MAE %s, bias %s."
                           % (_combo_head(c), o["n"], num(o["mae"]), num(o["bias"])))
        if not t3["scorable"]:
            out.append("- %s: no combination reached the gate, so no estimate is quoted "
                       "from its table 3." % code)
        out.append("")
    out += ["## Table 2", "",
            "Each hotel's own report carries it in full, with the pinned share per mark and "
            "the band note: " + ", ".join("out/pilot-%s.md" % c.lower() for c in present) + ".",
            ""]
    return "\n".join(out) + "\n"


def write(out_dir: str, payload: dict, audit_path: Optional[str] = None) -> str:
    text = render(payload, audit_section(audit_path, payload["hotel"]["code"])
                  if audit_path else None)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "pilot-%s%s.md"
                        % (payload["hotel"]["code"].lower(),
                           "" if payload["label"] == "full" else "-" + payload["label"]))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def write_pair(out_dir: str, payloads: List[dict]) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "pilot.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_pair(payloads))
    return path
