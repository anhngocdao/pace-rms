"""Bid price and rate selection.

The bid price is the marginal value of the last available room: what the
hotel gives up by selling it now instead of holding it for whoever comes
later.  It is the number a revenue system actually decides with.  Rate,
minimum stay, segment closures and overbooking are all downstream of it.

Two implementations live here, and the reason there are two is worth stating.

The dynamic program is the honest one.  With c rooms left and one more
arrival period to go,

    V(t, c) = V(t-1, c) + sum_s p_s * max(0, r_s - [V(t-1,c) - V(t-1,c-1)])

Read plainly: in each period a request of class s shows up with probability
p_s, and it is worth taking only if its net rate clears the value of the room
it consumes.  The bid price is V(t,c) - V(t,c-1).  It has one hard
requirement that is very easy to get wrong: the recursion consumes at most
one room per period, so the number of periods must exceed the capacity being
valued, or every room past the period count is silently valued at zero.

The closed form is the fast one, and it is what runs in production here.
For a single resource with nested classes, expected revenue on c rooms is

    R(c) = sum_j (v_j - v_{j+1}) * E[min(D_j, c)]

where classes are ordered by value and D_j is cumulative demand down to
class j.  Differentiating gives the marginal value of the last room directly:

    bid(c) = sum_j (v_j - v_{j+1}) * P(D_j > c)

which is the foundation the EMSR heuristics are built on.  It costs four
normal tail probabilities instead of a quarter of a million operations, and
the tests check the two against each other.

Once the bid price is known, the rate is the point on the estimated demand
curve that maximises expected contribution above it.  Rate and bid price are
solved twice, because the bid price depends on the rates the remaining
demand will be quoted, and those rates depend on the bid price.
"""

import math
from typing import Dict, List, Optional, Sequence, Tuple

from .config import SEGMENT_ORDER, SEGMENTS, Hotel
from .elasticity import PriceResponse
from .simulate import quoted_rate
from .unconstrain import norm_cdf

Bucket = List[Tuple[float, float]]      # [(probability, net value), ...]


def value_function(capacity: int, buckets: Sequence[Bucket]) -> List[float]:
    """Expected revenue with c rooms and all of these arrival periods left."""
    capacity = max(0, int(capacity))
    V = [0.0] * (capacity + 1)
    if capacity == 0:
        return V
    for bucket in reversed(buckets):
        positive = [(p, v) for p, v in bucket if v > 0.0]
        take_all = sum(p * v for p, v in positive)
        new = [0.0] * (capacity + 1)
        tail_from: Optional[int] = None
        for c in range(1, capacity + 1):
            delta = V[c] - V[c - 1]
            if delta <= 1e-9:
                # V is concave in c, so once the marginal room is worthless
                # every larger c accepts everything.  Fill the tail in one go.
                tail_from = c
                break
            gain = 0.0
            for p, v in positive:
                if v > delta:
                    gain += p * (v - delta)
            new[c] = V[c] + gain
        if tail_from is not None:
            for c in range(tail_from, capacity + 1):
                new[c] = V[c] + take_all
        V = new
    return V


def bid_price(capacity: int, buckets: Sequence[Bucket]) -> float:
    if capacity <= 0:
        return float("inf")
    V = value_function(capacity, buckets)
    return max(0.0, V[capacity] - V[capacity - 1])


def build_buckets(remaining: Dict[str, float], net_values: Dict[str, float],
                  capacity: int = 0, max_prob: float = 0.25) -> List[Bucket]:
    """Split remaining demand into periods with at most one arrival each.

    Rates are held flat across the remaining window, so the periods are
    exchangeable and equal sized splitting loses nothing.  The period count
    must clear the capacity as well as the arrival rate: the recursion sells
    at most one room per period, so with fewer periods than rooms the tail of
    the capacity is valued at nothing at all.
    """
    peak = max(remaining.values()) if remaining else 0.0
    if peak <= 0:
        return []
    n = int(max(8, math.ceil(peak / max_prob), capacity + 8))
    bucket: Bucket = []
    for code in SEGMENT_ORDER:
        lam = remaining.get(code, 0.0)
        if lam <= 0:
            continue
        bucket.append((min(0.95, lam / n), net_values.get(code, 0.0)))
    return [list(bucket) for _ in range(n)]


def demand_sd(mean: float, forecast_cv: float = 0.25) -> float:
    """Spread of remaining demand: arrival randomness plus forecast error.

    The Poisson part is what the arrivals do on their own.  The second term
    is the admission that the forecast itself is an estimate, which matters
    far more at sixty days out than the arrival noise does.
    """
    mean = max(0.0, mean)
    return math.sqrt(mean + (forecast_cv * mean) ** 2)


def bid_price_static(capacity: int, classes: Sequence[Tuple[float, float, float]]) -> float:
    """Marginal value of the last room, from the nested single resource model.

    classes: (mean demand, sd of demand, net value per room), any order.
    """
    if capacity <= 0:
        return float("inf")
    ordered = sorted([c for c in classes if c[2] > 0 and c[0] > 0], key=lambda t: -t[2])
    if not ordered:
        return 0.0
    cum_mean = 0.0
    cum_var = 0.0
    bid = 0.0
    for i, (mean, sd, value) in enumerate(ordered):
        cum_mean += mean
        cum_var += sd * sd
        lower = ordered[i + 1][2] if i + 1 < len(ordered) else 0.0
        if value <= lower:
            continue
        sigma = math.sqrt(cum_var) if cum_var > 1e-9 else 1e-6
        p_short = 1.0 - norm_cdf((capacity - cum_mean) / sigma)
        bid += (value - lower) * p_short
    return max(0.0, bid)


def net_value(hotel: Hotel, code: str, bar: float, ref: float) -> float:
    seg = SEGMENTS[code]
    return quoted_rate(code, bar, ref) * (1.0 - seg.commission) - hotel.variable_cost


def expected_bookings_at(hotel: Hotel, remaining: Dict[str, float],
                         refs: Dict[str, float], response: PriceResponse,
                         bar: float) -> Dict[str, float]:
    out = {}
    for code, lam in remaining.items():
        if lam <= 0:
            continue
        quoted = quoted_rate(code, bar, refs[code])
        out[code] = lam * response.accept(code, quoted, refs[code])
    return out


def choose_rate(hotel: Hotel, remaining: Dict[str, float], refs: Dict[str, float],
                response: PriceResponse, bid: float, capacity_left: int,
                ladder: Optional[Sequence[float]] = None) -> dict:
    """Pick the public rate that maximises contribution above the bid price."""
    ladder = list(ladder or hotel.rate_ladder())
    best = None
    curve = []
    for bar in ladder:
        booked = expected_bookings_at(hotel, remaining, refs, response, bar)
        total = sum(booked.values())
        scale = 1.0
        if capacity_left > 0 and total > capacity_left:
            scale = capacity_left / total          # cannot sell what does not exist
        contribution = 0.0
        revenue = 0.0
        for code, n in booked.items():
            n *= scale
            v = net_value(hotel, code, bar, refs[code])
            contribution += n * (v - bid)
            revenue += n * quoted_rate(code, bar, refs[code])
        curve.append({"rate": bar, "rooms": total * scale,
                      "revenue": revenue, "contribution": contribution})
        if best is None or contribution > best["contribution"]:
            best = curve[-1]

    # Never sell the marginal room below what it is worth.
    retail = SEGMENTS["RETAIL"]
    implied_floor = (bid + hotel.variable_cost) / max(1e-6, (1.0 - retail.commission))
    floor = max(hotel.rate_floor, min(hotel.rate_ceiling, implied_floor))
    chosen = best["rate"] if best else hotel.base_rate
    bound = "demand"
    if chosen < floor:
        chosen = min((r for r in ladder if r >= floor), default=hotel.rate_ceiling)
        bound = "bid price floor"
    if chosen >= hotel.rate_ceiling:
        bound = "rate ceiling"
    elif chosen <= hotel.rate_floor and bound == "demand":
        bound = "rate floor"

    return {"rate": chosen, "bound_by": bound, "implied_floor": implied_floor,
            "curve": curve, "unconstrained_best": best["rate"] if best else chosen}


def solve(hotel: Hotel, remaining: Dict[str, float], refs: Dict[str, float],
          response: PriceResponse, capacity_left: int, ladder: Optional[Sequence[float]] = None,
          passes: int = 3, start_rate: Optional[float] = None) -> dict:
    """Jointly settle the bid price and the rate.

    They depend on each other: the bid price is built from the demand that
    will actually arrive, which depends on the rate, which is chosen against
    the bid price.  A few passes settle it.
    """
    bar = start_rate or hotel.base_rate
    bid = 0.0
    result: dict = {}
    booked: Dict[str, float] = {}
    for _ in range(max(1, passes)):
        booked = expected_bookings_at(hotel, remaining, refs, response, bar)
        classes = [(n, demand_sd(n), net_value(hotel, code, bar, refs[code]))
                   for code, n in booked.items()]
        bid = bid_price_static(capacity_left, classes)
        if bid == float("inf"):
            bid = net_value(hotel, "RETAIL", hotel.rate_ceiling, refs["RETAIL"])
        result = choose_rate(hotel, remaining, refs, response, bid, capacity_left, ladder)
        if abs(result["rate"] - bar) < 1e-9:
            bar = result["rate"]
            break
        bar = result["rate"]
    result["bid_price"] = bid
    result["expected_bookings"] = expected_bookings_at(hotel, remaining, refs, response, bar)
    return result
