"""Randomised rate experimentation.

The engine learns its price response by comparing nights of the same type
that happened to be priced differently.  The trouble is who did the pricing.
Every rate in the incumbent's history was set by looking at how full the night
already was, so rate and demand were decided together and the comparison is
contaminated.  No amount of care in the regression fixes that, because the
variation needed to answer the question was never created.

So create it.  Each stay date is assigned, once and at random, a small
multiplier on whatever rate the optimizer would otherwise publish.  The
assignment depends on nothing but the date, which makes it independent of
demand by construction.  Fitting the price response on that variation gives
an answer that means something.

Three details that matter more than they look.

The assignment is a hash of the date, not a random draw.  Python salts its
built-in hash per process, so a stored experiment would silently re-randomise
on the next run; blake2b does not.  Any past date's arm can be recomputed
from the date alone, so nothing has to be persisted and nothing can drift.

A date keeps one arm for its whole booking window.  The date is the unit of
randomisation.  Switching arms mid-window would contaminate the very thing
the experiment exists to measure.

Compliance is imperfect on purpose.  The perturbation is skipped when the
night is forecast to sell out, and it is clipped when it would push the rate
below the bid price floor.  Both are refusals to spend real money on an
experiment that would not answer anything.  The estimator handles it by
treating the assigned arm as an instrument for the rate actually charged,
which is the standard answer to partial compliance.
"""

import datetime as dt
import hashlib
import math
from collections import defaultdict
from statistics import fmean
from typing import Dict, List, Optional, Sequence, Tuple

from .calendar import demand_class
from .config import SEGMENTS, Hotel

# Twelve control slots, eight treated.  Forty percent of nights carry a
# perturbation, which is enough to identify the curve inside a season without
# putting a large share of the year on a rate nobody chose.
DEFAULT_WHEEL: Tuple[float, ...] = (
    (0.00,) * 10 + (-0.16, -0.16, -0.08, -0.08, 0.08, 0.08, 0.16, 0.16)
)

# Below this, the assigned arms have not moved the rate actually charged by
# enough to identify anything, and the estimate they produce is worse than the
# biased one it would replace.  Refusing is the correct answer.
MIN_INSTRUMENT_SPREAD = 0.045


class RateExperiment:
    def __init__(self, salt: str = "pace/rate-response/v1",
                 wheel: Sequence[float] = DEFAULT_WHEEL,
                 min_lead: int = 3, max_lead: int = 120,
                 first: Optional[dt.date] = None, last: Optional[dt.date] = None):
        self.salt = salt
        self.wheel = tuple(wheel)
        self.min_lead = min_lead
        self.max_lead = max_lead
        self.first = first
        self.last = last
        self.applied = 0
        self.skipped_sellout = 0
        self.clipped_by_floor = 0

    # ------------------------------------------------------------ assignment

    def arm(self, stay_date: dt.date) -> float:
        """The multiplier this date was assigned.  Depends only on the date."""
        key = "%s|%d" % (self.salt, stay_date.toordinal())
        digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
        return self.wheel[int.from_bytes(digest, "big") % len(self.wheel)]

    def covers(self, stay_date: dt.date) -> bool:
        if self.first and stay_date < self.first:
            return False
        if self.last and stay_date > self.last:
            return False
        return True

    def eligible(self, stay_date: dt.date, lead: int, sellout: bool) -> bool:
        if not self.covers(stay_date):
            return False
        if lead < self.min_lead or lead > self.max_lead:
            return False
        if sellout:
            self.skipped_sellout += 1
            return False
        return True

    def apply(self, rate: float, stay_date: dt.date, lead: int, sellout: bool,
              floor: float, ladder: Sequence[float]) -> Tuple[float, float]:
        """Return (published rate, arm actually delivered)."""
        arm = self.arm(stay_date)
        if arm == 0.0 or not self.eligible(stay_date, lead, sellout):
            return rate, 0.0
        target = rate * (1.0 + arm)
        if target < floor:
            target = floor
            self.clipped_by_floor += 1
        published = min(ladder, key=lambda r: abs(r - target))
        self.applied += 1
        return published, arm

    def summary(self) -> dict:
        treated = sum(1 for x in self.wheel if x != 0.0)
        return {
            "salt": self.salt,
            "arms": sorted(set(self.wheel)),
            "treated_share": round(treated / len(self.wheel), 3),
            "lead_window": [self.min_lead, self.max_lead],
            "nights_perturbed": self.applied,
            "skipped_because_forecast_full": self.skipped_sellout,
            "clipped_by_bid_price_floor": self.clipped_by_floor,
        }


# ------------------------------------------------------------------ estimator


def experimental_rows(ledger, hotel: Hotel, cal, completed, code: str,
                      experiment: RateExperiment,
                      sellout_threshold: Optional[float] = None) -> List[Tuple[tuple, float, float, float]]:
    """(class, assigned arm, realised rate ratio, log rooms) per usable night."""
    from .simulate import reference_rate

    sellout_threshold = hotel.sellout_threshold if sellout_threshold is None else sellout_threshold
    rows = []
    for d in completed:
        if not experiment.covers(d):
            continue
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
        rows.append((demand_class(d), experiment.arm(d), rate / ref, math.log(sold)))
    return rows


def first_stage(rows) -> Dict[tuple, float]:
    """Mean realised rate ratio per (class, arm), with a pooled fallback.

    This is the fitted value of the first stage regression.  Substituting it
    for the realised ratio is two stage least squares with a saturated first
    stage, written out longhand so the mechanism stays visible.
    """
    cell: Dict[tuple, List[float]] = defaultdict(list)
    by_arm: Dict[float, List[float]] = defaultdict(list)
    for key, arm, ratio, _ in rows:
        cell[(key, arm)].append(ratio)
        by_arm[arm].append(ratio)
    fitted = {}
    for (key, arm), values in cell.items():
        if len(values) >= 4:
            fitted[(key, arm)] = fmean(values)
        else:
            fitted[(key, arm)] = fmean(by_arm[arm])
    return fitted


def instrument_spread(rows) -> float:
    """How much of the assigned variation survived into the rate charged.

    The first stage, stated as one number.  An experiment whose arms differ by
    thirty two percent on paper and by one percent in the ledger has not run.
    """
    by_arm: Dict[float, List[float]] = defaultdict(list)
    for _, arm, ratio, _ in rows:
        by_arm[arm].append(ratio)
    means = [fmean(v) for v in by_arm.values() if len(v) >= 5]
    return (max(means) - min(means)) if len(means) >= 2 else 0.0


def fit_segment(ledger, hotel: Hotel, cal, completed, code: str,
                experiment: RateExperiment, prior_k: float,
                k_grid: Sequence[float], prior_weight: float = 40.0,
                min_rows: int = 120, min_arms: int = 3) -> Tuple[float, int, str]:
    """Price response from experimental variation only.

    Returns (k, observations used, method).  The method is what to report
    when somebody asks where the number came from, which for a price
    elasticity is the first question worth asking.
    """
    from .elasticity import acceptance

    if not SEGMENTS[code].floats_with_bar:
        return prior_k, 0, "contracted"

    rows = experimental_rows(ledger, hotel, cal, completed, code, experiment)
    arms_seen = {arm for _, arm, _, _ in rows}
    if len(rows) < min_rows or len(arms_seen) < min_arms:
        return prior_k, len(rows), "insufficient"

    spread = instrument_spread(rows)
    if spread < MIN_INSTRUMENT_SPREAD:
        return prior_k, len(rows), "weak instrument (%.3f)" % spread

    fitted = first_stage(rows)
    by_class: Dict[tuple, List[Tuple[float, float]]] = defaultdict(list)
    for key, arm, _ratio, log_rooms in rows:
        by_class[key].append((fitted[(key, arm)], log_rooms))
    groups = [pairs for pairs in by_class.values() if len(pairs) >= 3]
    used = sum(len(g) for g in groups)
    if used < min_rows:
        return prior_k, used, "insufficient"

    best_k, best_sse = prior_k, None
    for k in k_grid:
        sse = 0.0
        for pairs in groups:
            resid = [y - math.log(max(1e-6, acceptance(k, x))) for x, y in pairs]
            m = fmean(resid)
            for r in resid:
                sse += (r - m) ** 2
        if best_sse is None or sse < best_sse:
            best_sse, best_k = sse, k

    k_hat = (used * best_k + prior_weight * prior_k) / (used + prior_weight)
    return max(k_grid[0], min(k_grid[-1], k_hat)), used, "experiment"


def true_local_elasticity(code: str) -> float:
    """The answer, read straight off the market that generated the data.

    Only knowable because this market is synthetic.  It exists so the
    experiment can be scored against something, and for no other purpose:
    nothing in the engine may read it.
    """
    from .unconstrain import norm_cdf, norm_pdf

    seg = SEGMENTS[code]
    if not seg.floats_with_bar:
        return 0.0
    z = -math.log(seg.wtp_premium) / seg.wtp_sigma
    return norm_pdf(z) / (1.0 - norm_cdf(z)) / seg.wtp_sigma
