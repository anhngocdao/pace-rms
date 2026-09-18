"""Golden numbers.

Every backtest is seeded, so the scores are exact. When one of these tests
goes red, the change either meant to move the number (then update CLAUDE.md,
this file, README.md and METHOD.md in the same commit) or it did not.

The quick backtest runs once per test process and is shared by the tests
below. The full backtest only runs with PACE_FULL=1 in the environment.
"""
import ast
import importlib.util
import os
import shutil
import sys
import sysconfig
import tempfile
import unittest

from pace import pipeline

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS = ["late_announcements", "rate_guardrails"]

QUICK_REVPAR = {"static": 64.50, "ladder": 78.80, "engine": 82.98}
FULL_REVPAR = {"static": 134.64, "ladder": 143.04, "engine": 152.25}
FULL_LIFT = 0.064
TOLERANCE = 0.01

_QUICK = {}


def _run(quick):
    """Run the pipeline with the real plugins but write outputs to a scratch dir,
    so a test run never overwrites out/ from a real build."""
    scratch = tempfile.mkdtemp(prefix="pace-golden-")
    try:
        os.symlink(os.path.join(ROOT, "plugins"), os.path.join(scratch, "plugins"))
        return pipeline.run(root=scratch, quick=quick, verbose=False)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def quick_payload():
    if "payload" not in _QUICK:
        _QUICK["payload"] = _run(quick=True)
    return _QUICK["payload"]


def _revpar(payload):
    return {k: round(v["revpar"], 2) for k, v in payload["backtest"]["scores"].items()}


class QuickGolden(unittest.TestCase):

    def test_quick_backtest_is_stable(self):
        payload = quick_payload()
        self.assertEqual(payload["plugins"]["loaded"], PLUGINS)
        scores = payload["backtest"]["scores"]
        for policy, want in QUICK_REVPAR.items():
            self.assertAlmostEqual(scores[policy]["revpar"], want, delta=TOLERANCE,
                                   msg="%s RevPAR moved from the golden %.2f" % (policy, want))

    def test_engine_beats_ladder_quick(self):
        scores = quick_payload()["backtest"]["scores"]
        self.assertGreater(scores["engine"]["revpar"], scores["ladder"]["revpar"])
        self.assertGreater(scores["engine"]["goppar"], scores["ladder"]["goppar"])
        self.assertGreater(scores["ladder"]["revpar"], scores["static"]["revpar"])

    def test_deterministic(self):
        first = quick_payload()
        second = _run(quick=True)
        self.assertEqual(first["backtest"]["scores"], second["backtest"]["scores"])
        self.assertEqual(first["backtest"]["denials_by_reason"],
                         second["backtest"]["denials_by_reason"])
        self.assertEqual(first["recommendations"], second["recommendations"])


class FullGolden(unittest.TestCase):

    @unittest.skipUnless(os.environ.get("PACE_FULL") == "1",
                         "set PACE_FULL=1 to run the full 184-night backtest")
    def test_full_backtest_golden(self):
        payload = _run(quick=False)
        scores = payload["backtest"]["scores"]
        for policy, want in FULL_REVPAR.items():
            self.assertAlmostEqual(scores[policy]["revpar"], want, delta=TOLERANCE,
                                   msg="%s RevPAR moved from the golden %.2f" % (policy, want))
        self.assertAlmostEqual(scores["engine"]["revpar_lift_vs_ladder"], FULL_LIFT, delta=0.002)


def _imported_modules(path):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0]


def _is_stdlib(name):
    if name in sys.builtin_module_names:
        return True
    names = getattr(sys, "stdlib_module_names", None)   # Python 3.10+
    if names is not None:
        return name in names
    spec = importlib.util.find_spec(name)              # Python 3.9 fallback
    if spec is None or not spec.origin:
        return False
    if spec.origin in ("built-in", "frozen"):
        return True
    stdlib = os.path.realpath(sysconfig.get_paths()["stdlib"])
    origin = os.path.realpath(spec.origin)
    return origin.startswith(stdlib + os.sep) and (os.sep + "site-packages" + os.sep) not in origin


class StdlibOnly(unittest.TestCase):

    def test_stdlib_only(self):
        local = {"pace", "plugins"}
        offenders = []
        for base in ("pace", "plugins"):
            for dirpath, _, files in os.walk(os.path.join(ROOT, base)):
                for name in files:
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, name)
                    for mod in _imported_modules(path):
                        if mod not in local and not _is_stdlib(mod):
                            offenders.append("%s imports %s" % (os.path.relpath(path, ROOT), mod))
        self.assertEqual(offenders, [], "third-party imports are not allowed:\n" + "\n".join(offenders))


class ExplicitTorontoConfig(unittest.TestCase):
    """ADR 0007 moves to Accepted only when the configuration path reproduces
    the golden numbers, not just the default path."""

    def tearDown(self):
        from pace import calendar as C
        from pace import config
        C.reset_seasonality()
        config.reset_segments()

    def test_spelled_out_toronto_matches_quick_golden(self):
        from pace import hotelconfig as HC
        cfg = HC.load_hotel_json(os.path.join(ROOT, "tests", "fixtures", "toronto-hotel.json"))
        HC.apply(cfg)
        scores = _run(quick=True)["backtest"]["scores"]
        for policy, want in QUICK_REVPAR.items():
            self.assertAlmostEqual(scores[policy]["revpar"], want, delta=TOLERANCE)


if __name__ == "__main__":
    unittest.main()
