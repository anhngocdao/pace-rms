"""The handover page: one self-contained HTML file, three tabs, one per
department, each carrying a plain-sentence explanation of its own numbers.
Reuses the dashboard's CSS so the two documents look like one hotel's."""
import html
import json
import os

from .dashboard import CSS

MISSING = "n/a"


def num(x, nd=1) -> str:
    return MISSING if x is None else ("%%.%df" % nd) % float(x)


def rate_text(k: int, n: int) -> str:
    """A rate beside its count, and no rate at all below the 30-night floor."""
    if n < 30:
        return "%d of %d (below the 30-night floor, no rate printed)" % (k, n)
    return "%.1f%% (%d of %d)" % (100.0 * k / n, k, n)


def notice_text(notice: dict) -> str:
    med = notice.get("median")
    if med is None:
        return "no warned sell-out night to measure notice on"
    text = "median notice %s nights" % ("14 or more" if med >= 14 else num(med, 0))
    return "%s, %d of %d censored at 14 (the walk records no deeper lead)" % (
        text, notice.get("censored", 0), notice.get("scored", 0))


def band_text(b) -> str:
    return MISSING if not b else "%s to %s" % (num(b[0]), num(b[2]))


def _tab_front(p: dict) -> str:
    a = p["front_desk"]["alarm"]
    rows = []
    for r in p["front_desk"]["nights"]:
        rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            r["night"], r["otb"], num(r["rooms"]), band_text(r["band"]), r["authorised"],
            "warning" if r["warned"] else "", "sold out" if r["sold_out"] else ""))
    per_lead = "".join(
        "<tr><td>%d</td><td>%d</td><td>%d</td><td>%s</td><td>%s</td></tr>" % (
            lead, c["warned"], c["events"],
            rate_text(c["hits"], c["warned"]), rate_text(c["hits"], c["events"]))
        for lead, c in sorted(((int(k), v) for k, v in a["per_lead"].items())))
    return (
        "<section id=\"front\"><h2>Front desk, indexed by stay night</h2>"
        "<p>This tab reads <b>revenue rooms</b>: %s.</p>"
        "<p>The warning says the night is going to fill: the top of the band reaches the "
        "sell-out cut of %.2f times the rooms for sale that day. It is not a claim that anyone "
        "will be relocated; on both pilot hotels the room count is inferred from the busiest "
        "night in the log, so a night above capacity cannot occur in the data at all.</p>"
        "<p>%d of %d scored nights reached the cut, an upper bound because the inferred room "
        "count is biased low. %d of them were full in the house while the ledger was short: "
        "those are the nights the log cannot say whether anyone was turned away, reported here "
        "and folded into no rate.</p>"
        "<table><tr><th>night</th><th>on the books</th><th>forecast</th><th>band p10 to p90</th>"
        "<th>authorised</th><th>warning</th><th>outcome</th></tr>%s</table>"
        "<h3>The alarm, scored per lead</h3><p>%s.</p>"
        "<table><tr><th>lead</th><th>warned</th><th>sell-outs</th><th>precision</th><th>recall</th></tr>%s</table>"
        "</section>" % (html.escape(p["front_desk"]["reads"]), a["threshold"], a["events"], a["nights"],
                        a["cannot_speak"], "".join(rows), notice_text(a["notice"]), per_lead))


def _tier_cell(c) -> str:
    if c is None:
        return "<td colspan=\"4\">no band at this lead</td>"
    return ("<td>%d</td><td>%s</td><td>%s / %s</td><td>%s / %s</td>" % (
        c["n"], num(c["mae"]),
        rate_text(c["covering"]["short"], c["n"]), num(c["covering"]["over_mean"]),
        rate_text(c["balanced"]["short"], c["n"]), num(c["balanced"]["over_mean"])))


def _tab_kitchen(p: dict) -> str:
    k = p["kitchen"]
    rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            r["day"], num(r["breakfast"]["point"]), band_text(r["breakfast"]["band"]),
            num(r["dinner"]["point"]), band_text(r["dinner"]["band"]))
        for r in k["days"])
    scores = ""
    for meal in ("breakfast", "dinner"):
        scores += "<h3>%s, scored per lead</h3><table><tr><th>lead</th><th>n</th><th>MAE covers</th>" \
                  "<th>covering tier: short / over-prep</th><th>balanced tier: short / over-prep</th></tr>" % meal
        for lead, c in sorted(((int(a), b) for a, b in k["scores"][meal].items())):
            scores += "<tr><td>%d</td>%s</tr>" % (lead, _tier_cell(c))
        scores += "</table>"
    und = p["undefined_board_share"]
    return (
        "<section id=\"kitchen\"><h2>Kitchen, indexed by service day</h2>"
        "<p>%s</p><p>Breakfast on a morning belongs to the night before and carries that "
        "night's lead; dinner belongs to the same night. Both tiers are promises and both are "
        "scored: the covering tier is the top of the band, the balanced tier is the median, "
        "the point at which a cover short and a cover wasted weigh the same. A hotel that knows "
        "a shortfall costs r times an over-prep reads the r / (1 + r) quantile; the band's "
        "quantiles are printed so it can.</p>"
        "<p>%s</p><p>%s</p>"
        "<p>SC and Undefined both count as no meal, the dataset's own documentation. Undefined "
        "is %s of %s stayed revenue room nights, %s.</p>"
        "<table><tr><th>day</th><th>breakfast</th><th>band</th><th>dinner</th><th>band</th></tr>%s</table>%s"
        "</section>" % (html.escape(p["day_convention"]), html.escape(k["survival_assumption"]),
                        html.escape(k["covers_population"]), und["room_nights"], und["of"],
                        (MISSING if und["share"] is None else "%.1f%%" % (100 * und["share"])), rows, scores))


def _tab_housekeeping(p: dict) -> str:
    h = p["housekeeping"]
    rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%d</td></tr>" % (
            r["day"], num(r["departures"]["point"]), band_text(r["departures"]["band"]),
            num(r["stayovers"]["point"]), band_text(r["stayovers"]["band"]), r["arrivals_booked"])
        for r in h["days"])
    scores = "<table><tr><th>lead</th><th>departures MAE (n)</th><th>stayovers MAE (n)</th>" \
             "<th>roster at p10: days short / rooms short on those days</th></tr>"
    for lead, c in sorted(((int(a), b) for a, b in h["scores"].items())):
        ros = c.get("roster")
        scores += "<tr><td>%d</td><td>%s (%d)</td><td>%s (%d)</td><td>%s</td></tr>" % (
            lead, num(c["departures"]["mae"]), c["departures"]["n"],
            num(c["stayovers"]["mae"]), c["stayovers"]["n"],
            ("no band" if ros is None else "%s / %s" % (rate_text(ros["short"], ros["n"]), num(ros["short_rooms_mean"]))))
    scores += "</table>"
    return (
        "<section id=\"housekeeping\"><h2>Housekeeping, indexed by service day</h2>"
        "<p>This tab reads <b>physical rooms</b>: %s.</p>"
        "<p>Departures need a deep clean and stayovers a light one, so they are counted apart. "
        "Arrivals are a priority line, not a workload line: they are mostly the same rooms as "
        "the departures and say which rooms must be finished before check-in. Roster at the "
        "bottom of the band; the top is printed beside it so the supervisor knows how many "
        "extra to call in.</p><p>%s</p>"
        "<table><tr><th>day</th><th>departures</th><th>band</th><th>stayovers</th><th>band</th>"
        "<th>arrivals on the books</th></tr>%s</table><h3>Scored per lead</h3>%s"
        "</section>" % (html.escape(h["reads"]), html.escape(h["limit"]), rows, scores))


def render(payload: dict) -> str:
    p = payload
    head = "<h1>%s, the handover%s</h1>" % (html.escape(p["hotel"]["name"]),
                                             "" if p["mode"] == "proof" else " (forward, not scored)")
    intro = "<p>%s</p>" % html.escape(p["notes"][0])
    if p["mode"] == "forward":
        intro += "<p>%s</p>" % html.escape(p["forward_note"])
    else:
        w = p["windows"]
        intro += ("<p>Bands measured on %d warm-up nights from %s to %s, excluded weeks removed; "
                  "answers and scores on %s to %s. The band never sees a night it is scored on.</p>"
                  % (w["band_nights"], w["band_first"], w["band_last"], w["score_first"], w["score_last"]))
    tabs = ("<nav><a href=\"#front\">Front desk</a> <a href=\"#kitchen\">Kitchen</a> "
            "<a href=\"#housekeeping\">Housekeeping</a></nav>")
    body = head + intro + tabs + _tab_front(p) + _tab_kitchen(p) + _tab_housekeeping(p)
    data = json.dumps({k: v for k, v in p.items() if not k.startswith("_")}, default=str)
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>%s, the handover</title><style>%s</style></head><body><div id=\"app\">%s</div>"
            "<script id=\"payload\" type=\"application/json\">%s</script></body></html>\n"
            % (html.escape(p["hotel"]["name"]), CSS, body, data.replace("</", "<\\/")))


def write(out_dir: str, payload: dict) -> str:
    os.makedirs(out_dir, exist_ok=True)
    suffix = "" if payload["mode"] == "proof" else "-forward"
    path = os.path.join(out_dir, "handover-%s%s.html" % (payload["hotel"]["code"].lower(), suffix))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render(payload))
    return path
