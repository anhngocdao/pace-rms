"""Price response.

The engine cannot see the guests who looked at the rate and left, so it
cannot observe a demand curve directly.  What it can do is compare nights of
the same type that happened to be priced differently, and read the slope.

Functional form matters more than the fit here.  A constant elasticity power
law is the obvious choice and it is a trap: whenever the fitted elasticity
comes out below one, revenue rises without bound as the rate rises, and the
optimizer walks the rate straight into the ceiling.  Negotiated corporate
business fits below one almost every time.

So the model is a logistic choice curve instead, which is also what
commercial systems use:

    acceptance(r) = 2 / (1 + exp(k * (r / reference - 1)))

Normalised so that acceptance equals one at the reference rate.  It decays to
zero as the rate climbs, which gives revenue a genuine interior maximum for
every segment, however insensitive.  The single parameter k is twice the
local elasticity at the reference rate, so it stays readable: k = 3.8 means
an elasticity of 1.9 where the hotel normally prices.

Estimation is a grid search on k with class fixed effects removed, shrunk
toward the configured prior.  Sold-out nights are dropped, because on those
nights the number sold measures the building, not the guests.

Two sources of variation, and they are not equal.  Observational history is
contaminated: the incumbent set every rate by looking at how full the night
already was, so rate and demand were decided together.  Randomised rate
variation is clean by construction.  When enough of the second exists the
engine uses it and ignores the first.  See pace/experiment.py.
"""

import datetime as dt
import math
from collections import defaultdict
from statistics import fmean
from typing import Dict, Iterable, List, Tuple

from .calendar import EventCalendar, demand_class
from .config import SEGMENTS, Hotel
from .ledger import Ledger
from .simulate import reference_rate

K_GRID = [round(0.4 + 0.1 * i, 2) for i in range(0, 106)]     # 0.4 .. 10.9
PRIOR_WEIGHT = 40.0        # pseudo observations behind the configured prior
MIN_ROWS = 12


def acceptance(k: float, ratio: float) -> float:
    """Share of reference-rate demand that still books at this price ratio."""
    u = k * (ratio - 1.0)
    if u > 60:
        return 0.0
    if u < -60:
        return 2.0
    return 2.0 / (1.0 + math.exp(u))


def local_elasticity(k: float, ratio: float = 1.0) -> float:
    """Percentage fall in demand per percentage rise in rate, at this ratio."""
    u = k * (ratio - 1.0)
    if u > 60:
        return k * ratio
    e = math.exp(u)
    return k * ratio * e / (1.0 + e)


def prior_k(code: str) -> float:
    return 2.0 * SEGMENTS[code].prior_elasticity


def fit_segment(ledger: Ledger, hotel: Hotel, cal: EventCalendar,
                completed: Iterable[dt.date], code: str,
                sellout_threshold: float = 0.97) -> Tuple[float, int]:
    if not SEGMENTS[code].floats_with_bar:
        # Contracted business is quoted a rate the engine does not set, so
        # there is no price variation to read a response from.  Its
        # acceptance is flat by construction.
        return prior_k(code), 0
    rows: List[Tuple[tuple, float, float]] = []
    for d in completed:
        if ledger.rooms_on(d) >= hotel.rooms * sellout_threshold:
            continue
        sold = ledger.seg_rooms.get(d, {}).get(code, 0)
        if sold <= 0:
            continue
        revenue = ledger.seg_revenue.get(d, {}).get(code, 0.0)
        rate = revenue / sold
        ref = reference_rate(hotel, cal, d, code)
        if rate <= 0 or ref <= 0:
            continue
        rows.append((demand_class(d), rate / ref, math.log(sold)))

    k0 = prior_k(code)
    if len(rows) < MIN_ROWS:
        return k0, len(rows)

    by_class: Dict[tuple, List[Tuple[float, float]]] = defaultdict(list)
    for key, x, y in rows:
        by_class[key].append((x, y))
    groups = [pairs for pairs in by_class.values() if len(pairs) >= 3]
    used = sum(len(g) for g in groups)
    if used < MIN_ROWS:
        return k0, used

    best_k, best_sse = k0, None
    for k in K_GRID:
        sse = 0.0
        for pairs in groups:
            preds = [math.log(max(1e-6, acceptance(k, x))) for x, _ in pairs]
            resid = [y - p for (_, y), p in zip(pairs, preds)]
            m = fmean(resid)
            for r in resid:
                sse += (r - m) ** 2
        if best_sse is None or sse < best_sse:
            best_sse, best_k = sse, k

    # Shrink toward the prior so the first months in service are not spent
    # making confident rate moves on twelve observations.
    k_hat = (used * best_k + PRIOR_WEIGHT * k0) / (used + PRIOR_WEIGHT)
    return max(K_GRID[0], min(K_GRID[-1], k_hat)), used


class PriceResponse:
    """Fitted price response for every segment."""

    def __init__(self, params: Dict[str, dict]):
        self.params = params

    def k(self, code: str) -> float:
        return self.params[code]["k"]

    def accept(self, code: str, rate: float, ref: float) -> float:
        if ref <= 0 or rate <= 0 or not SEGMENTS[code].floats_with_bar:
            return 1.0
        return acceptance(self.k(code), rate / ref)

    def elasticity(self, code: str, ratio: float = 1.0) -> float:
        return local_elasticity(self.k(code), ratio)

    def mix_acceptance(self, mix: Dict[str, float], bar: float,
                       refs: Dict[str, float]) -> float:
        total = sum(mix.values()) or 1.0
        out = 0.0
        for code, share in mix.items():
            if code not in self.params:
                continue
            quoted = bar * SEGMENTS[code].rate_multiplier
            out += (share / total) * self.accept(code, quoted, refs[code])
        return max(1e-3, out)

    def as_dict(self) -> Dict[str, dict]:
        return {c: {"k": round(v["k"], 3),
                    "elasticity_at_reference": (round(local_elasticity(v["k"]), 3)
                                                if SEGMENTS[c].floats_with_bar else 0.0),
                    "prior_k": round(v["prior_k"], 3),
                    "observations": v["observations"],
                    "method": v.get("method", "observational"),
                    "contracted": not SEGMENTS[c].floats_with_bar}
                for c, v in self.params.items()}


def fit_all(ledger: Ledger, hotel: Hotel, cal: EventCalendar,
            completed: Iterable[dt.date], experiment=None) -> PriceResponse:
    from . import experiment as experiment_module

    completed = list(completed)
    params: Dict[str, dict] = {}
    for code in SEGMENTS:
        if not SEGMENTS[code].floats_with_bar:
            params[code] = {"k": prior_k(code), "observations": 0,
                            "prior_k": prior_k(code), "method": "contracted"}
            continue
        if experiment is not None:
            k, n, method = experiment_module.fit_segment(
                ledger, hotel, cal, completed, code, experiment,
                prior_k(code), K_GRID, PRIOR_WEIGHT)
            if method == "experiment":
                params[code] = {"k": k, "observations": n,
                                "prior_k": prior_k(code), "method": "experiment"}
                continue
        k, n = fit_segment(ledger, hotel, cal, completed, code)
        params[code] = {"k": k, "observations": n,
                        "prior_k": prior_k(code), "method": "observational"}
    return PriceResponse(params)
