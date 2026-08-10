"""Extension points.

Two seams, both deliberately narrow:

  signal(name)  a function that looks at a stay date and returns a demand
                multiplier plus a sentence explaining it.  Competitor rates,
                weather, flight arrivals, a scraped event feed: all of these
                are signals.

  rule(name)    a function that receives a finished recommendation and may
                modify it.  Brand rate standards, parity constraints,
                a manual override table, a floor for a specific season: all
                of these are rules.  Rules run in priority order and every
                change one makes is recorded, so the dashboard can always
                show what the raw optimizer wanted before policy was applied.

Anything dropped into the plugins/ directory is loaded automatically.
"""

import importlib.util
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Signal:
    name: str
    multiplier: float
    reason: str = ""


_SIGNALS: List = []
_RULES: List = []


def signal(name: str):
    """Register a demand signal.  The function receives (stay_date, ctx)."""

    def deco(fn: Callable):
        _SIGNALS.append((name, fn))
        return fn

    return deco


def rule(name: str, priority: int = 100):
    """Register a post-optimizer rule.  Lower priority numbers run first."""

    def deco(fn: Callable):
        _RULES.append((priority, name, fn))
        _RULES.sort(key=lambda item: item[0])
        return fn

    return deco


def collect_signals(stay_date, ctx: Optional[Dict[str, Any]] = None) -> List[Signal]:
    ctx = ctx or {}
    out: List[Signal] = []
    for name, fn in _SIGNALS:
        result = fn(stay_date, ctx)
        if result is None:
            continue
        if isinstance(result, Signal):
            out.append(result)
        else:
            out.append(Signal(name, float(result), ""))
    return [s for s in out if abs(s.multiplier - 1.0) > 1e-9]


def signal_multiplier(stay_date, ctx: Optional[Dict[str, Any]] = None) -> float:
    m = 1.0
    for s in collect_signals(stay_date, ctx):
        m *= s.multiplier
    return m


def apply_rules(rec, ctx: Optional[Dict[str, Any]] = None):
    ctx = ctx or {}
    for _, name, fn in _RULES:
        before = rec.rate
        result = fn(rec, ctx)
        if result is not None:
            rec = result
        if abs(rec.rate - before) > 1e-9:
            rec.rule_trace.append("%s: %.0f -> %.0f" % (name, before, rec.rate))
    return rec


def registered() -> Dict[str, List[str]]:
    return {
        "signals": [n for n, _ in _SIGNALS],
        "rules": [n for _, n, _ in _RULES],
    }


def reset() -> None:
    """Used by the tests so plugin state does not leak between cases."""
    _SIGNALS.clear()
    _RULES.clear()


def load(directory: str) -> List[str]:
    """Import every .py file in a directory so its decorators run."""
    loaded: List[str] = []
    if not os.path.isdir(directory):
        return loaded
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        path = os.path.join(directory, fname)
        mod_name = "pace_plugin_" + fname[:-3]
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded.append(fname[:-3])
    return loaded
