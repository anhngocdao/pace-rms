"""End to end run: simulate, replay, score, recommend, export.

Everything downstream reads the JSON this writes, so the dashboard never
recomputes anything and the numbers on screen are provably the numbers the
engine produced.
"""

import csv
import datetime as dt
import json
import os
import time
from typing import Dict, List, Optional

from . import scenario as S
from .calendar import DOW_NAMES, class_label, demand_class, span
from .config import SEGMENTS, SEGMENT_ORDER, Hotel
from .explain import describe
from .ledger import Ledger
from .otb import GLOBAL_KEY
from .plugins import load as load_plugins
from .plugins import registered
from .elasticity import PriceResponse
from .policy import HandoverPolicy, LadderPolicy, PaceEngine, StaticPolicy
from .recommendation import Recommendation
from .simulate import generate_requests, replay


def benchmark(root: str = ".", verbose: bool = True) -> dict:
    """Where the time goes, and what happens when the property gets bigger.

    Two questions, measured rather than asserted.  How long does one pricing
    decision take, and does that change when the hotel has two thousand rooms
    instead of a hundred and fifty.
    """
    import time as _time

    from .elasticity import PriceResponse, prior_k
    from .optimize import solve
    from .simulate import reference_rate

    hotel, cal = S.HOTEL, S.CALENDAR
    response = PriceResponse({c: {"k": prior_k(c), "observations": 0, "prior_k": prior_k(c)}
                              for c in SEGMENTS})
    day = dt.date(2025, 6, 14)
    refs = {c: reference_rate(hotel, cal, day, c) for c in SEGMENTS}
    ladder = hotel.rate_ladder()

    rows = []
    for rooms in (50, 150, 500, 2000, 8000):
        remaining = {c: rooms * 0.9 * SEGMENTS[c].share for c in SEGMENTS}
        reps = 400
        t0 = _time.perf_counter()
        for _ in range(reps):
            solve(hotel, remaining, refs, response, rooms, ladder)
        per = (_time.perf_counter() - t0) / reps
        rows.append({"rooms": rooms, "ms_per_decision": round(per * 1000, 3),
                     "decisions_per_second": round(1.0 / per)})
        if verbose:
            print("   %5d rooms   %6.3f ms per pricing decision   %7d decisions/second"
                  % (rooms, per * 1000, 1.0 / per))

    if verbose:
        per_night = rows[1]["ms_per_decision"] / 1000.0
        for horizon, label in ((365, "one year"),):
            nightly = 365 * horizon * per_night
            print("\n   a %s horizon repriced every night for a year: %.0f seconds of optimizer time per property"
                  % (label, nightly))
            print("   one core, 8 hour overnight window: about %d properties"
                  % int(8 * 3600 / nightly))
    out = {"solve": rows}
    with open(os.path.join(_out_dir(root), "benchmark.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    return out


def experiment_report(root: str = ".", verbose: bool = True) -> dict:
    """Score the rate experiment against the market that generated the data.

    Two identical engines are run on the identical request stream.  One prices
    normally.  The other perturbs a randomly assigned forty percent of nights
    by up to ten percent and fits its price response on that variation alone.

    Because this market is synthetic the true price response is knowable, so
    the two estimates can be scored rather than merely compared.  The cost of
    the experiment is the revenue difference between the two engines, which is
    the number a general manager asks for first.
    """
    from .experiment import RateExperiment, true_local_elasticity

    hotel, cal = S.HOTEL, S.CALENDAR
    load_plugins(os.path.join(root, "plugins"))
    requests = generate_requests(hotel, cal, S.FIRST_ARRIVAL, S.LAST_ARRIVAL, seed=S.SEED)

    out = {}
    engines = {}
    for label, exp in (("observational", None),
                       ("experiment", RateExperiment(first=S.HANDOVER)),
                       ("oracle", None)):
        engine = PaceEngine(hotel, cal, experiment=exp)
        if label == "oracle":
            _make_oracle(engine)
        if verbose:
            print("   running %-14s ..." % label, end=" ", flush=True)
        ledger = replay(hotel, requests, HandoverPolicy(LadderPolicy(hotel), engine, S.HANDOVER),
                        S.FIRST_BOOKING_DAY, S.TODAY, S.FIRST_ARRIVAL, S.LAST_ARRIVAL, seed=7)
        engines[label] = (engine, ledger)
        if verbose:
            print("done", flush=True)
        out[label] = {"kpi": ledger.kpi(S.EVAL_FIRST, S.EVAL_LAST),
                      "response": engine.response.as_dict(),
                      "experiment": exp.summary() if exp else None}

    truth = {c: round(true_local_elasticity(c), 3) for c in SEGMENTS}
    out["truth"] = truth
    rows = []
    for code in SEGMENT_ORDER:
        if not SEGMENTS[code].floats_with_bar:
            continue
        t = truth[code]
        o = out["observational"]["response"][code]["elasticity_at_reference"]
        e = out["experiment"]["response"][code]["elasticity_at_reference"]
        rows.append({"segment": code, "true": t, "observational": o, "experimental": e,
                     "observational_error": round(abs(o - t) / t, 4) if t else None,
                     "experimental_error": round(abs(e - t) / t, 4) if t else None,
                     "method": out["experiment"]["response"][code]["method"]})
    out["accuracy"] = rows

    a, b, o = out["observational"]["kpi"], out["experiment"]["kpi"], out["oracle"]["kpi"]
    out["cost"] = {
        "revpar_observational": round(a["revpar"], 2),
        "revpar_experiment": round(b["revpar"], 2),
        "revpar_oracle": round(o["revpar"], 2),
        "revpar_change": round(b["revpar"] / a["revpar"] - 1, 4) if a["revpar"] else 0.0,
        "goppar_change": round(b["goppar"] / a["goppar"] - 1, 4) if a["goppar"] else 0.0,
        "oracle_headroom": round(o["revpar"] / a["revpar"] - 1, 4) if a["revpar"] else 0.0,
        "nights_scored": a.get("nights", 0),
    }

    with open(os.path.join(_out_dir(root), "experiment.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, default=str)

    if verbose:
        print("\n   price response at the reference rate")
        print("   %-8s %8s %15s %15s" % ("segment", "true", "observational", "experimental"))
        for r in rows:
            print("   %-8s %8.2f %8.2f %+5.0f%% %8.2f %+5.0f%%"
                  % (r["segment"], r["true"], r["observational"], (r["observational"] / r["true"] - 1) * 100,
                     r["experimental"], (r["experimental"] / r["true"] - 1) * 100))
        ex = out["experiment"]["experiment"]
        print("\n   %d nights perturbed, %d skipped because forecast full, %d clipped by the bid price floor"
              % (ex["nights_perturbed"], ex["skipped_because_forecast_full"], ex["clipped_by_bid_price_floor"]))
        c = out["cost"]
        print("\n   over %d settled nights" % c["nights_scored"])
        print("   price of running the experiment      RevPAR %+.2f%%  GOPPAR %+.2f%%"
              % (c["revpar_change"] * 100, c["goppar_change"] * 100))
        print("   value of knowing the answer exactly  RevPAR %+.2f%%   (engine handed the true response)"
              % (c["oracle_headroom"] * 100))
    return out


def _make_oracle(engine) -> None:
    """Hand an engine the true price response and stop it from ever refitting.

    Not a policy anyone can run.  It measures the ceiling: the most that
    better identification could possibly be worth, which is the number that
    decides whether the experiment is worth its price.
    """
    from .experiment import true_local_elasticity

    truth = {c: {"k": 2.0 * true_local_elasticity(c) if SEGMENTS[c].floats_with_bar
                 else 2.0 * SEGMENTS[c].prior_elasticity,
                 "observations": -1, "prior_k": 2.0 * SEGMENTS[c].prior_elasticity,
                 "method": "oracle"} for c in SEGMENTS}
    original = engine._fit

    def fit_then_override(asof, ledger, completed):
        original(asof, ledger, completed)
        engine.response = PriceResponse(truth)
        engine.forecaster.response = engine.response

    engine._fit = fit_then_override


def robustness(root: str = ".", seeds=(20250115, 4242, 991, 70707), verbose: bool = True) -> list:
    """Re-run the comparison on independent markets.

    A single simulated year can flatter any policy.  Changing the seed draws a
    different set of guests, arrival times and willingness to pay from the
    same generating process, which is the closest this project gets to asking
    whether the result is real.
    """
    hotel, cal = S.HOTEL, S.CALENDAR
    load_plugins(os.path.join(root, "plugins"))
    rows = []
    for seed in seeds:
        requests = generate_requests(hotel, cal, S.FIRST_ARRIVAL, S.LAST_ARRIVAL, seed=seed)
        engine = PaceEngine(hotel, cal)
        a = replay(hotel, requests, HandoverPolicy(LadderPolicy(hotel), engine, S.HANDOVER),
                   S.FIRST_BOOKING_DAY, S.TODAY, S.FIRST_ARRIVAL, S.LAST_ARRIVAL, seed=7)
        b = replay(hotel, requests, LadderPolicy(hotel), S.FIRST_BOOKING_DAY, S.TODAY,
                   S.FIRST_ARRIVAL, S.LAST_ARRIVAL, seed=7)
        ka, kb = a.kpi(S.EVAL_FIRST, S.EVAL_LAST), b.kpi(S.EVAL_FIRST, S.EVAL_LAST)
        row = {"seed": seed,
               "ladder_revpar": round(kb["revpar"], 2), "engine_revpar": round(ka["revpar"], 2),
               "revpar_lift": round(ka["revpar"] / kb["revpar"] - 1, 4),
               "goppar_lift": round(ka["goppar"] / kb["goppar"] - 1, 4),
               "restrictions_used": sum(1 for x in a.denials
                                        if S.EVAL_FIRST <= x["arrival"] <= S.EVAL_LAST
                                        and x["reason"] in ("mlos", "cta", "segment_closed"))}
        rows.append(row)
        if verbose:
            print("   seed %-9d ladder RevPAR %7.2f   engine %7.2f   RevPAR %+.1f%%   GOPPAR %+.1f%%"
                  % (seed, row["ladder_revpar"], row["engine_revpar"],
                     row["revpar_lift"] * 100, row["goppar_lift"] * 100), flush=True)
    with open(os.path.join(_out_dir(root), "robustness.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=1)
    return rows


def _out_dir(root: str) -> str:
    path = os.path.join(root, "out")
    os.makedirs(path, exist_ok=True)
    return path


def run(root: str = ".", quick: bool = False, verbose: bool = True,
        use_plugins: bool = True) -> dict:
    hotel: Hotel = S.HOTEL
    cal = S.CALENDAR
    today = S.TODAY if not quick else S.HANDOVER + dt.timedelta(days=60)
    eval_first, eval_last = S.EVAL_FIRST, S.EVAL_LAST
    if quick:
        eval_first = S.HANDOVER + dt.timedelta(days=20)
        eval_last = S.HANDOVER + dt.timedelta(days=50)

    plugin_names: List[str] = []
    if use_plugins:
        plugin_names = load_plugins(os.path.join(root, "plugins"))

    t0 = time.time()
    if verbose:
        print("1. generating demand ...", flush=True)
    requests = generate_requests(hotel, cal, S.FIRST_ARRIVAL, S.LAST_ARRIVAL, seed=S.SEED)
    if verbose:
        print("   %d requests, %d room nights of gross demand"
              % (len(requests), sum(r.rooms * r.los for r in requests)), flush=True)

    engine = PaceEngine(hotel, cal)
    policies = [
        ("static", StaticPolicy(hotel)),
        ("ladder", LadderPolicy(hotel)),
        ("engine", HandoverPolicy(LadderPolicy(hotel), engine, S.HANDOVER)),
    ]

    ledgers: Dict[str, Ledger] = {}
    for key, policy in policies:
        if verbose:
            print("2. replaying %-7s ..." % key, end=" ", flush=True)
        start = time.time()
        ledgers[key] = replay(hotel, requests, policy,
                              S.FIRST_BOOKING_DAY, today,
                              S.FIRST_ARRIVAL, S.LAST_ARRIVAL, seed=7)
        if verbose:
            print("%5.1fs" % (time.time() - start), flush=True)

    scores = {k: led.kpi(eval_first, eval_last) for k, led in ledgers.items()}
    base = scores["ladder"]
    for k, s in scores.items():
        if not s or not base:
            continue
        s["revpar_lift_vs_ladder"] = (s["revpar"] / base["revpar"] - 1.0) if base["revpar"] else 0.0
        s["goppar_lift_vs_ladder"] = (s["goppar"] / base["goppar"] - 1.0) if base["goppar"] else 0.0

    if verbose:
        print("3. pricing the next %d nights ..." % S.DASHBOARD_DAYS, flush=True)
    led = ledgers["engine"]
    first = today + dt.timedelta(days=1)
    last = min(S.LAST_ARRIVAL, first + dt.timedelta(days=S.DASHBOARD_DAYS - 1))
    recs: List[Recommendation] = []
    for d in span(first, last):
        rec = engine.cache.get(d)
        if rec is None or rec.asof != today:
            rec = engine.recommend(led, d, today)
        recs.append(describe(hotel, rec))

    payload = _payload(hotel, cal, engine, ledgers, scores, recs, today,
                       eval_first, eval_last, plugin_names, requests)
    payload["runtime_seconds"] = round(time.time() - t0, 1)

    out = _out_dir(root)
    with open(os.path.join(out, "run.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, default=str)
    _write_csvs(out, payload)
    if verbose:
        print("4. wrote %s" % os.path.join(out, "run.json"), flush=True)
    return payload


def _payload(hotel, cal, engine, ledgers, scores, recs, today,
             eval_first, eval_last, plugin_names, requests) -> dict:
    led = ledgers["engine"]

    history = []
    for d in sorted(led.settled):
        r = led.settled[d]
        history.append({
            "date": d.isoformat(), "rooms": r["rooms_sold"], "adr": round(r["adr"], 2),
            "revpar": round(r["revpar"], 2), "occ": round(r["occupancy"], 4),
            "walked": r["walked"],
        })

    # Pace curve, exported for the chart: the class norm next to the live dates.
    curves = engine.curves
    pace_export = {}
    if curves is not None:
        for key in sorted(curves.ratio, key=lambda k: str(k)):
            if curves.observations(key) < curves.min_obs and key != GLOBAL_KEY:
                continue
            pace_export[class_label(key) if key != GLOBAL_KEY else "House average"] = {
                "observations": curves.observations(key),
                "ratio": [round(v, 4) for v in curves.ratio[key][:121]],
                "pickup": [round(v, 2) for v in curves.pickup[key][:121]],
            }

    live_pace = []
    for rec in recs[:60]:
        snaps = led.snapshots.get(rec.stay_date, {})
        series = [snaps.get(l) for l in range(0, 121)]
        live_pace.append({
            "date": rec.stay_date.isoformat(),
            "class": class_label(demand_class(rec.stay_date)),
            "otb_by_lead": series,
            "forecast": round(rec.expected_final, 1),
        })

    seg_summary = {}
    for code in SEGMENT_ORDER:
        seg = SEGMENTS[code]
        rooms = sum(led.seg_rooms.get(d, {}).get(code, 0)
                    for d in led.settled if eval_first <= d <= eval_last)
        revenue = sum(led.seg_revenue.get(d, {}).get(code, 0.0)
                      for d in led.settled if eval_first <= d <= eval_last)
        seg_summary[code] = {
            "name": seg.name,
            "rooms": rooms,
            "revenue": round(revenue, 2),
            "adr": round(revenue / rooms, 2) if rooms else 0.0,
            "k": round(engine.response.k(code), 3),
            "elasticity_at_reference": round(engine.response.elasticity(code), 3),
            "elasticity_prior": seg.prior_elasticity,
            "elasticity_observations": engine.response.params[code]["observations"],
        }

    denial_mix: Dict[str, int] = {}
    for d in led.denials:
        if eval_first <= d["arrival"] <= eval_last:
            denial_mix[d["reason"]] = denial_mix.get(d["reason"], 0) + d["rooms"]

    return {
        "generated": today.isoformat(),
        "hotel": {"name": hotel.name, "city": hotel.city, "rooms": hotel.rooms,
                  "currency": hotel.currency, "base_rate": hotel.base_rate,
                  "rate_floor": hotel.rate_floor, "rate_ceiling": hotel.rate_ceiling,
                  "variable_cost": hotel.variable_cost, "walk_cost": hotel.walk_cost},
        "window": {"first": recs[0].stay_date.isoformat() if recs else None,
                   "last": recs[-1].stay_date.isoformat() if recs else None,
                   "days": len(recs)},
        "backtest": {"first": eval_first.isoformat(), "last": eval_last.isoformat(),
                     "handover": S.HANDOVER.isoformat(), "scores": scores,
                     "denials_by_reason": denial_mix},
        "recommendations": [r.to_row() for r in recs],
        "history": history,
        "pace_curves": pace_export,
        "live_pace": live_pace,
        "segments": seg_summary,
        "fit_log": engine.fit_log,
        "plugins": {"loaded": plugin_names, **registered()},
        "demand_total_requests": len(requests),
        "solves": engine.solves,
    }


def _write_csvs(out: str, payload: dict) -> None:
    rows = payload["recommendations"]
    if rows:
        cols = ["date", "dow", "lead", "otb", "authorized", "rate", "bid_price",
                "bound_by", "forecast_rooms", "forecast_occ", "forecast_adr",
                "forecast_revpar", "mlos", "cta", "confidence", "headline"]
        with open(os.path.join(out, "recommendations.csv"), "w", newline="", encoding="utf-8") as fh:
            wtr = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            wtr.writeheader()
            for r in rows:
                wtr.writerow(r)

    scores = payload["backtest"]["scores"]
    with open(os.path.join(out, "backtest.csv"), "w", newline="", encoding="utf-8") as fh:
        cols = ["policy", "nights", "rooms_sold", "occupancy", "adr", "revpar",
                "room_revenue", "walked", "walk_cost", "goppar", "sellouts",
                "revpar_lift_vs_ladder"]
        wtr = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        wtr.writeheader()
        for name, s in scores.items():
            row = dict(s)
            row["policy"] = name
            wtr.writerow(row)
