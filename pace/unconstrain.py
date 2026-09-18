"""Unconstrained demand.

The ledger only records what the hotel accepted.  On a night that sold out,
or a night where a minimum stay rule turned away a two night guest, observed
demand is right censored: the true number is somewhere above what was booked,
and nobody wrote it down.

Feeding censored history straight into a forecast is the single most common
way a revenue system quietly teaches itself to under-price a peak.  The
forecast says "we sold 150", the system concludes "demand was 150", and next
year it opens the same night at the same rate.

This module applies projection detruncation (Weatherford and Bodily): assume
final demand within a class is normal, replace each censored observation with
its conditional expectation above the censoring point, re-estimate, repeat.
"""

import datetime as dt
import math
from collections import defaultdict
from statistics import fmean, pstdev
from typing import Dict, Iterable, List, Optional, Tuple

from .calendar import EventCalendar, demand_class
from .config import SEGMENT_ORDER, Hotel
from .ledger import Ledger
from .otb import GLOBAL_KEY
from .simulate import reference_rate


def norm_pdf(z: float) -> float:
    return math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)


def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def expected_above(mu: float, sigma: float, cut: float) -> float:
    """E[X | X > cut] for X ~ Normal(mu, sigma)."""
    if sigma <= 1e-9:
        return max(mu, cut)
    z = (cut - mu) / sigma
    tail = 1.0 - norm_cdf(z)
    if tail < 1e-6:
        return cut + 0.25 * sigma
    return mu + sigma * norm_pdf(z) / tail


def project_detruncate(observations: List[Tuple[float, bool]],
                       max_iter: int = 80, tol: float = 1e-4) -> Tuple[float, float, List[float]]:
    """Return (mu, sigma, imputed values) for a censored sample.

    observations: (observed value, was it censored)
    """
    if not observations:
        return 0.0, 0.0, []
    values = [v for v, _ in observations]
    open_values = [v for v, c in observations if not c]
    mu = fmean(open_values) if len(open_values) >= 2 else fmean(values)
    sigma = pstdev(open_values) if len(open_values) >= 2 else max(1.0, pstdev(values) if len(values) > 1 else 1.0)
    sigma = max(sigma, 1e-3)

    imputed = list(values)
    for _ in range(max_iter):
        imputed = [expected_above(mu, sigma, v) if c else v for v, c in observations]
        new_mu = fmean(imputed)
        new_sigma = max(1e-3, pstdev(imputed) if len(imputed) > 1 else sigma)
        if abs(new_mu - mu) < tol and abs(new_sigma - sigma) < tol:
            mu, sigma = new_mu, new_sigma
            break
        mu, sigma = new_mu, new_sigma
    return mu, sigma, imputed


class ClassDemand:
    """Unconstrained final demand, by demand class."""

    def __init__(self):
        self.mu: Dict[tuple, float] = {}
        self.sigma: Dict[tuple, float] = {}
        self.n: Dict[tuple, int] = {}
        self.censored: Dict[tuple, int] = {}
        self.uplift: Dict[tuple, float] = {}
        self.booked: Dict[tuple, float] = {}

    def mean(self, key: tuple) -> float:
        return self.mu.get(key, self.mu.get(GLOBAL_KEY, 0.0))

    def sd(self, key: tuple) -> float:
        return self.sigma.get(key, self.sigma.get(GLOBAL_KEY, 1.0))

    def booked_mean(self, key: tuple) -> float:
        """Average rooms actually sold in this class, before any uncensoring."""
        return self.booked.get(key, self.booked.get(GLOBAL_KEY, 0.0))

    def censoring_uplift(self, key: tuple) -> float:
        """How much larger true demand runs than booked demand, in this class."""
        return self.uplift.get(key, self.uplift.get(GLOBAL_KEY, 1.0))


MIN_ACCEPTANCE = 0.25       # keeps the price normalisation well conditioned


def class_demand(ledger: Ledger, completed: Iterable[dt.date], hotel: Hotel,
                 cal: EventCalendar, response,
                 sellout_threshold: Optional[float] = None) -> ClassDemand:
    """Unconstrained demand per class, restated at the reference rate.

    Two corrections, applied in this order and for different reasons.

    Price.  A night that was sold at eighty percent of the reference rate
    sold more rooms than the same night would at the reference rate.  Each
    segment is divided by its own acceptance at the rate it was actually
    charged, so dates priced differently become comparable.  Contracted
    segments divide by one, because their rate never moved.

    Censoring.  Nights that sold out, or turned business away on a
    restriction, are right censored.  Those are replaced by their conditional
    expectation above the censoring point and the whole class is re-estimated
    until it settles.
    """
    sellout_threshold = hotel.sellout_threshold if sellout_threshold is None else sellout_threshold
    raw: Dict[tuple, List[Tuple[float, bool]]] = defaultdict(list)
    booked_raw: Dict[tuple, List[float]] = defaultdict(list)
    rooms = hotel.rooms

    for d in completed:
        snaps = ledger.snapshots.get(d)
        if not snaps:
            continue
        final = snaps.get(0)
        if final is None:
            continue
        at_reference = 0.0
        for code in SEGMENT_ORDER:
            sold = ledger.seg_rooms.get(d, {}).get(code, 0)
            if sold <= 0:
                continue
            revenue = ledger.seg_revenue.get(d, {}).get(code, 0.0)
            rate = revenue / sold
            ref = reference_rate(hotel, cal, d, code)
            taken = max(MIN_ACCEPTANCE, response.accept(code, rate, ref))
            at_reference += sold / taken
        regrets = ledger.observable_denials(d)
        observed = at_reference + regrets    # denials we could log are demand we saw
        censored = (final >= rooms * sellout_threshold) or regrets > 0
        for key in (demand_class(d), GLOBAL_KEY):
            raw[key].append((float(observed), censored))
            booked_raw[key].append(float(final))

    out = ClassDemand()
    for key, obs in raw.items():
        mu, sigma, _ = project_detruncate(obs)
        booked = fmean(booked_raw[key]) if booked_raw.get(key) else 0.0
        out.mu[key] = mu
        out.sigma[key] = sigma
        out.n[key] = len(obs)
        out.censored[key] = sum(1 for _, c in obs if c)
        out.booked[key] = booked
        out.uplift[key] = (mu / booked) if booked > 0 else 1.0
    return out
