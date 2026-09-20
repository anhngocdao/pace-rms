#!/usr/bin/env python3
"""Pace command line.

  python3 run.py build       simulate, replay, score, price, export
  python3 run.py build --quick    short run, for checking a change
  python3 run.py dashboard   rebuild out/dashboard.html from out/run.json
  python3 run.py show 2025-02-14  print the reasoning for one night
  python3 run.py robustness  re-run the comparison on four independent markets
  python3 run.py experiment  score the randomised rate experiment against the truth
  python3 run.py network     score the network bid price against the nightly one
  python3 run.py bench       where the time goes, and how it scales
  python3 run.py ingest      replay a real booking log into the ledger
  python3 run.py pilot data/antonio/h1-bookings.csv data/antonio/h1-hotel.json
                             score the engine on a real booking log
  python3 run.py test        run the checks
"""

import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "build"
    args = argv[2:]

    if cmd == "build":
        from pace.pipeline import run
        payload = run(root=HERE, quick="--quick" in args)
        from pace.dashboard import build as build_dash
        from pace.dashboard import build_artifact
        path = build_dash(HERE, payload)
        print("   wrote %s" % path)
        print("   wrote %s" % build_artifact(HERE, payload))
        _summary(payload)
        return 0

    if cmd == "bench":
        from pace.pipeline import benchmark
        benchmark(root=HERE)
        return 0

    if cmd == "experiment":
        from pace.pipeline import experiment_report
        print("scoring the rate experiment ...")
        experiment_report(root=HERE)
        return 0

    if cmd == "network":
        from pace.pipeline import network_report
        print("scoring the network bid price ...")
        network_report(root=HERE, trials=4 if "--quick" in args else 12)
        return 0

    if cmd == "robustness":
        from pace.pipeline import robustness
        print("re-running on independent markets ...")
        robustness(root=HERE)
        return 0

    if cmd == "dashboard":
        from pace.dashboard import build as build_dash
        with open(os.path.join(HERE, "out", "run.json"), encoding="utf-8") as fh:
            payload = json.load(fh)
        print("wrote %s" % build_dash(HERE, payload))
        return 0

    if cmd == "show":
        with open(os.path.join(HERE, "out", "run.json"), encoding="utf-8") as fh:
            payload = json.load(fh)
        target = args[0] if args else payload["recommendations"][0]["date"]
        for row in payload["recommendations"]:
            if row["date"] == target:
                print("\n%s  %s" % (row["date"], row["dow"]))
                print("-" * 72)
                print(row["headline"])
                print()
                for d in row["drivers"]:
                    print("  - %s" % d)
                print("\n%s\n" % row["narrative"])
                return 0
        print("no recommendation for %s" % target)
        return 1

    if cmd == "ingest":
        if len(argv) < 4:
            print("usage: python3 run.py ingest <bookings.csv> <hotel.json>")
            return 1
        from pace import ingest
        from pace.hotelconfig import ConfigError, event_calendar
        from pace.policy import PaceEngine
        try:
            res = ingest.load(argv[2], argv[3])
        except (ingest.IngestError, ConfigError) as exc:
            print(exc)
            return 1
        rep = res.report
        print("rows %d, ledger bookings %d, cancels %d, nights %s to %s"
              % (len(res.bookings), res.ledger.n_bookings, res.ledger.n_cancels, res.first_stay, res.last_stay))
        print("rooms %d (%s)" % (res.hotel.rooms, "inferred" if res.inference else "from hotel.json"))
        for k, v in sorted(rep.warnings.items()):
            if not k.startswith("_"):
                print("warning %-32s %d" % (k, v))
        for n in rep.notes:
            print("note", n)
        engine = PaceEngine(res.hotel, event_calendar(res.cfg))
        engine.observe(res.last_stay + dt.timedelta(days=1), res.ledger)
        print("engine fitted:", engine.ready)
        return 0

    if cmd == "pilot":
        if len(args) < 2:
            print("usage: python3 run.py pilot <bookings.csv> <hotel.json> "
                  "[--out out/] [--settings data/antonio/settings.json] [--quick]")
            return 1
        from pace import ingest
        from pace import pilot
        from pace.hotelconfig import ConfigError
        out_dir = _opt(args, "--out", os.path.join(HERE, "out"))
        settings = _opt(args, "--settings", os.path.join(HERE, "data", "antonio", "settings.json"))
        first, last, label = _pilot_window(args)
        try:
            payload = pilot.run_one(args[0], args[1], settings, out_dir,
                                    score_first=first, score_last=last, progress=90,
                                    label=label)
        except (ingest.IngestError, ConfigError, pilot.PilotError) as exc:
            print(exc)
            return 1
        h = payload["hotel"]
        w = payload["walk"]
        print("\n%s (%s), %d rooms%s" % (h["name"], h["code"], h["rooms"],
                                         ", inferred" if h["rooms_inferred"] else ""))
        print("walked %d days in %.1f s, %d fits, %d forecasts recorded"
              % (w["days"], w["seconds"], w["fits"], w["records"]))
        gap = payload["full_night_gap"]
        # Both counts say which population they are a share of: the invisible
        # nights are a share of the full nights, never of the window.
        print("nights physically full %d of %d in the window, of those %d invisible to the "
              "ledger (%.0f%% of the full nights)"
              % (gap["physically_full"], gap["nights"], gap["full_but_invisible"],
                 100 * gap["share_of_full_nights"]))
        for lead in payload["table1"]["leads"] + [payload["table1"]["late_lead"]]:
            block = payload["table1"]["overall"][lead]
            m = block["methods"]
            if m["engine"]["mae"] is None:
                print("  lead %-4s no night scored" % lead)
                continue
            print("  lead %-4s n=%-4d engine MAE %6.2f (clamped %4.0f%%), average %6.2f"
                  % (lead, block["n"], m["engine"]["mae"], 100 * m["engine"]["clamp_share"],
                     m["average"]["mae"]))
        print("\n   wrote %s" % payload["_json_path"])
        return 0

    if cmd == "test":
        import unittest
        loader = unittest.TestLoader()
        suite = loader.discover(os.path.join(HERE, "tests"), top_level_dir=HERE)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1

    print(__doc__)
    return 1


def _pilot_window(args):
    """The pilot's scoring window, and the label a partial run is filed under.

    --quick scores the first two months of the pre-registered window so the
    wiring can be checked in a minute.  It moves the far end and never the
    near one: the near end is what the warm-up and the 180 days of driven rate
    path in front of it are cut to.  A partial run is labelled, because its
    numbers are not the pilot's numbers and its payload must not be read, or
    overwritten, as though they were.
    """
    from pace import pilot
    first = pilot.SCORE_FIRST
    if "--quick" in args:
        return first, first + dt.timedelta(days=61), "quick"
    return first, pilot.SCORE_LAST, ""


def _opt(args, name, default):
    """--name value, or the default.  Kept tiny on purpose: run.py is a switchboard."""
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default


def _summary(payload):
    scores = payload["backtest"]["scores"]
    print("\n   backtest %s to %s" % (payload["backtest"]["first"], payload["backtest"]["last"]))
    print("   %-8s %8s %8s %8s %8s %8s" % ("policy", "occ", "ADR", "RevPAR", "GOPPAR", "vs ladder"))
    for name in ("static", "ladder", "engine"):
        s = scores.get(name) or {}
        if not s:
            continue
        print("   %-8s %7.1f%% %8.2f %8.2f %8.2f %8s"
              % (name, s["occupancy"] * 100, s["adr"], s["revpar"], s["goppar"],
                 ("%+.1f%%" % (s.get("revpar_lift_vs_ladder", 0) * 100))))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
