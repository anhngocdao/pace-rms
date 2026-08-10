"""Does the network formulation actually earn anything.

network.py argued that adding up independent nightly bid prices understates
what a long stay costs.  An argument is not a number.  This module settles it
the same way the rest of the project settles things: one arrival stream,
several controls, and whatever the difference turns out to be.

What is held fixed, so the comparison means something:

    the requests            same guests, same stays, same order, every run
    the published rates     every type quoted on its ladder position
    the forecast            each control is handed the true remaining demand

The last one is deliberate and it is not a favour to the network.  Forecast
error is measured elsewhere in this project; mixing it in here would leave any
difference impossible to attribute.  Every control gets a correct forecast, so
what is being compared is the control and nothing else.

What differs is the accept or refuse decision, and the room the guest ends up
in.  A guest refused the executive room is offered what is left and takes it
or leaves according to the same choice model that produced the preference in
the first place.  That is the substitution the single resource engine cannot
see, and it is half of what is being measured.

Four controls:

    independent   what Pace does today.  Each night valued on its own, from
                  per night values, and the stay charged the sum.
    dlp           the deterministic network dual.
    decomposed    the network dual, prorated, then solved per night with
                  demand uncertain.
    hindsight     an optimistic ceiling, not a policy.  It sees the whole
                  realised stream, may put any guest in any room, and assumes
                  they accept it.  No online control can beat it, and the gap
                  to it is the honest measure of how much is left on the table.
"""

import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

from .choice import SubstitutionModel
from .config import SEGMENT_ORDER, SEGMENTS, Hotel
from .network import (Cell, NetworkInstance, Product, decomposed_bid_prices,
                      dual_prices, independent_bid_prices)
from .roomtypes import Inventory, preferences_for

CONTROLS = ("independent", "dlp", "decomposed")


@dataclass
class Arrival:
    """One request, as it reaches the front desk."""

    rid: int
    segment: str
    first_night: int
    los: int
    rooms: int
    lead: float                 # larger means booked earlier
    taste: float                # the guest's own draw, reused across controls
    wanted: str = ""            # room type actually taken, filled in per run

    @property
    def nights(self) -> Tuple[int, ...]:
        return tuple(range(self.first_night, self.first_night + self.los))


@dataclass
class Window:
    """The stretch of nights being priced, and the market that wants it."""

    hotel: Hotel
    inventory: Inventory
    nights: int
    bar: Dict[int, float]                              # published rate per night
    arrivals_per_night: Dict[Tuple[str, int], float]   # (segment, night) -> requests
    model: SubstitutionModel = field(init=False)

    def __post_init__(self):
        self.model = SubstitutionModel(self.inventory, preferences_for(self.inventory))

    def capacity(self) -> Dict[Cell, float]:
        return {(t.code, n): float(t.rooms)
                for t in self.inventory for n in range(self.nights)}

    def net_value(self, segment: str, room_type: str, first_night: int,
                  los: int) -> float:
        """Net value of the whole stay, per room: rate less commission and cost."""
        seg = SEGMENTS[segment]
        mult = self.inventory.by_code[room_type].rate_multiplier
        total = 0.0
        for n in range(first_night, first_night + los):
            rate = self.bar.get(n, self.hotel.base_rate) * seg.rate_multiplier * mult
            total += rate * (1.0 - seg.commission) - self.hotel.variable_cost
        return total


# ----------------------------------------------------------------- the market


def default_window(hotel: Hotel, inventory: Inventory, nights: int = 21,
                   start_weekday: int = 0, demand_scale: float = 1.0) -> Window:
    """A three week block with the weekday shape the property really has.

    The rate is the base rate on the ladder throughout.  Pricing is not what is
    being tested here: a control that closes the right rooms at a fixed rate is
    being compared against one that closes the wrong ones.
    """
    bar = {n: hotel.base_rate for n in range(nights)}
    arrivals: Dict[Tuple[str, int], float] = {}
    for n in range(nights):
        dow = (start_weekday + n) % 7
        for code in SEGMENT_ORDER:
            seg = SEGMENTS[code]
            arrivals[(code, n)] = (hotel.base_daily_demand * seg.share
                                   * seg.dow_weights[dow] * demand_scale)
    return Window(hotel=hotel, inventory=inventory, nights=nights,
                  bar=bar, arrivals_per_night=arrivals)


def draw_stream(window: Window, rng: random.Random) -> List[Arrival]:
    """One realisation of the demand, in the order it books.

    Sorted by lead time, so groups land first and the last minute corporate
    business arrives to find whatever is left.  That ordering is what makes
    this an online problem: a control that could see the whole stream would
    not need a bid price at all.
    """
    out: List[Arrival] = []
    rid = 0
    for (code, night), lam in window.arrivals_per_night.items():
        seg = SEGMENTS[code]
        mean_block = (seg.block_size[0] + seg.block_size[1]) / 2.0
        for _ in range(_poisson(rng, lam / mean_block)):
            los = _pick(rng, seg.los_weights) + 1
            if night + los > window.nights:
                los = max(1, window.nights - night)
            rooms = (seg.block_size[0] if seg.block_size[0] == seg.block_size[1]
                     else rng.randint(*seg.block_size))
            lead = rng.gammavariate(seg.lead_time_shape,
                                    seg.lead_time_mean / seg.lead_time_shape)
            out.append(Arrival(rid, code, night, los, rooms, lead, rng.random()))
            rid += 1
    out.sort(key=lambda a: (-a.lead, a.rid))
    return out


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam < 25:
        target = math.exp(-lam)
        k, p = 0, 1.0
        while True:
            p *= rng.random()
            if p <= target or k > 500:
                return k
            k += 1
    return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))


def _pick(rng: random.Random, weights: Sequence[float]) -> int:
    x = rng.random() * sum(weights)
    acc = 0.0
    for i, w in enumerate(weights):
        acc += w
        if x <= acc:
            return i
    return len(weights) - 1


# ---------------------------------------------------------- forward instances


def remaining_instance(window: Window, left: Dict[Cell, float],
                       still_to_come: float) -> NetworkInstance:
    """The optimisation problem as it stands part way through the booking window.

    still_to_come is the share of the stream not yet seen.  Expected demand is
    the true process scaled by it, which hands every control the same correct
    forecast and leaves the control itself as the only thing under test.
    """
    inst = NetworkInstance({c: max(0.0, v) for c, v in left.items()})
    if still_to_come <= 0.0:
        return inst
    flat = {t.code: 1.0 for t in window.inventory}
    for code in SEGMENT_ORDER:
        seg = SEGMENTS[code]
        shares = window.model.shares(code, flat, 2.0 * seg.prior_elasticity)
        for night in range(window.nights):
            lam = window.arrivals_per_night.get((code, night), 0.0) * still_to_come
            if lam <= 0.0:
                continue
            for los_index, los_weight in enumerate(seg.los_weights):
                los = los_index + 1
                if los_weight <= 0.0 or night + los > window.nights:
                    continue
                for type_code, share in shares.items():
                    demand = lam * los_weight * share
                    if demand <= 1e-6:
                        continue
                    cells = tuple((type_code, n) for n in range(night, night + los))
                    inst.add(Product(code, type_code, night, los,
                                     window.net_value(code, type_code, night, los),
                                     demand, cells))
    return inst


def prices_for(control: str, inst: NetworkInstance) -> Dict[Cell, float]:
    if control == "independent":
        return independent_bid_prices(inst)
    duals = dual_prices(inst)
    if control == "dlp":
        return dict(duals.prices)
    if control == "decomposed":
        return decomposed_bid_prices(inst, duals)
    raise ValueError("unknown control %r" % control)


# --------------------------------------------------------------------- replay


def run_control(window: Window, stream: List[Arrival], control: str,
                checkpoints: int = 12) -> dict:
    """Walk the stream once, accepting or refusing against this control."""
    left: Dict[Cell, float] = dict(window.capacity())
    prices: Dict[Cell, float] = {c: 0.0 for c in left}
    total = len(stream)
    step = max(1, total // max(1, checkpoints))

    revenue = 0.0
    contribution = 0.0
    rooms_sold = 0
    refused = 0
    substituted = 0
    solves = 0

    for i, req in enumerate(stream):
        if i % step == 0:
            inst = remaining_instance(window, left,
                                      1.0 - i / total if total else 0.0)
            prices = prices_for(control, inst)
            solves += 1

        available = [t.code for t in window.inventory
                     if all(left.get((t.code, n), 0.0) >= req.rooms
                            for n in req.nights)]
        taken = None
        first_choice = True
        while available:
            code = _choose(window, req, available)
            value = window.net_value(req.segment, code, req.first_night, req.los)
            bid = sum(prices.get((code, n), 0.0) for n in req.nights)
            if value >= bid:
                taken = code
                break
            available.remove(code)
            first_choice = False
        if taken is None:
            refused += 1
            continue
        if not first_choice:
            substituted += 1

        req.wanted = taken
        seg = SEGMENTS[req.segment]
        mult = window.inventory.by_code[taken].rate_multiplier
        for n in req.nights:
            left[(taken, n)] -= req.rooms
            rate = window.bar[n] * seg.rate_multiplier * mult
            revenue += rate * req.rooms
            contribution += (rate * (1.0 - seg.commission)
                             - window.hotel.variable_cost) * req.rooms
        rooms_sold += req.rooms * req.los

    capacity = window.hotel.rooms * window.nights
    return {
        "control": control,
        "revenue": revenue,
        "contribution": contribution,
        "rooms_sold": rooms_sold,
        "occupancy": rooms_sold / capacity,
        "adr": revenue / rooms_sold if rooms_sold else 0.0,
        "revpar": revenue / capacity,
        "refused": refused,
        "substituted": substituted,
        "solves": solves,
    }


def _choose(window: Window, req: Arrival, available: Sequence[str]) -> str:
    """Which of the rooms on offer this guest asks for.

    The taste draw belongs to the guest and is reused across every control, so
    the same person makes the same choice whenever the same rooms are offered.
    Only the offer differs, which is the point.
    """
    flat = {c: 1.0 for c in available}
    k = 2.0 * SEGMENTS[req.segment].prior_elasticity
    shares = window.model.shares(req.segment, flat, k, available)
    acc = 0.0
    for code in available:
        acc += shares.get(code, 0.0)
        if req.taste <= acc:
            return code
    return available[-1]


# -------------------------------------------------------------------- ceiling


def hindsight_ceiling(window: Window, stream: List[Arrival]) -> dict:
    """An optimistic upper bound on any online control.

    Built from the realised stream, allowing every guest to be put in any room
    type and assuming they accept it.  Solved as the linear relaxation, whose
    dual objective bounds the optimum from above.  Loose on purpose: a bound
    that had to be tight would be a different project.
    """
    inst = NetworkInstance(window.capacity())
    grouped: Dict[tuple, float] = {}
    for req in stream:
        for t in window.inventory:
            key = (req.segment, t.code, req.first_night, req.los)
            grouped[key] = grouped.get(key, 0.0) + req.rooms
    for (segment, type_code, night, los), demand in grouped.items():
        cells = tuple((type_code, n) for n in range(night, night + los))
        inst.add(Product(segment, type_code, night, los,
                         window.net_value(segment, type_code, night, los),
                         demand, cells))
    sol = dual_prices(inst)
    capacity = window.hotel.rooms * window.nights
    return {"control": "hindsight", "contribution": sol.primal_value,
            "upper_bound": sol.dual_objective, "gap": sol.gap,
            "revpar": sol.primal_value / capacity}


# -------------------------------------------------------------------- compare


def compare(hotel: Hotel, inventory: Inventory, trials: int = 8,
            nights: int = 21, seed: int = 20250115,
            demand_scale: float = 1.0,
            controls: Sequence[str] = CONTROLS,
            checkpoints: int = 12,
            progress: bool = False) -> dict:
    """Run every control against the same streams and report the difference."""
    window = default_window(hotel, inventory, nights=nights,
                            demand_scale=demand_scale)
    totals: Dict[str, Dict[str, float]] = {c: {} for c in controls}
    ceiling: List[float] = []
    per_trial: List[dict] = []

    for t in range(trials):
        rng = random.Random(seed + 1000 * t)
        stream = draw_stream(window, rng)
        row: dict = {"trial": t, "requests": len(stream)}
        for control in controls:
            fresh = [Arrival(a.rid, a.segment, a.first_night, a.los, a.rooms,
                             a.lead, a.taste) for a in stream]
            result = run_control(window, fresh, control, checkpoints)
            row[control] = result
            for key, value in result.items():
                if isinstance(value, (int, float)):
                    totals[control][key] = totals[control].get(key, 0.0) + value
        top = hindsight_ceiling(window, stream)
        ceiling.append(top["contribution"])
        row["hindsight"] = top
        per_trial.append(row)
        if progress:
            print("   trial %d  %d requests  %s" % (
                t, len(stream),
                "  ".join("%s %.0f" % (c, row[c]["contribution"])
                          for c in controls)), flush=True)

    mean = {c: {k: v / trials for k, v in totals[c].items()} for c in controls}
    paired = _paired(per_trial, controls)
    mean_ceiling = sum(ceiling) / trials if ceiling else 0.0
    base = mean[controls[0]]["contribution"]
    for c in controls:
        mean[c]["lift_vs_%s" % controls[0]] = (
            mean[c]["contribution"] / base - 1.0 if base else 0.0)
        mean[c]["share_of_ceiling"] = (
            mean[c]["contribution"] / mean_ceiling if mean_ceiling else 0.0)
    return {"trials": trials, "nights": nights, "demand_scale": demand_scale,
            "rooms": hotel.rooms, "types": list(inventory.codes),
            "mean": mean, "paired": paired, "ceiling": mean_ceiling,
            "per_trial": per_trial}


def _paired(per_trial: List[dict], controls: Sequence[str]) -> Dict[str, dict]:
    """Difference against the baseline on the same stream, trial by trial.

    Every control saw identical guests in an identical order, so the paired
    difference removes the stream to stream variation entirely.  Comparing the
    two averages instead would bury a half percent effect under the noise of
    which weekend happened to draw a large group.
    """
    base_name = controls[0]
    out: Dict[str, dict] = {}
    for control in controls:
        diffs = []
        for row in per_trial:
            base = row[base_name]["contribution"]
            if base:
                diffs.append((row[control]["contribution"] - base) / base)
        if not diffs:
            continue
        n = len(diffs)
        mean = sum(diffs) / n
        if n > 1:
            var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
            se = math.sqrt(var / n)
        else:
            se = 0.0
        out[control] = {"mean": mean, "stderr": se, "trials": n,
                        "wins": sum(1 for d in diffs if d > 0)}
    return out


# --------------------------------------------------------------------- report


def interaction(hotel: Hotel, inventory: Inventory, trials: int = 12,
                nights: int = 21, demand_scale: float = 0.90,
                seed: int = 20250115) -> List[dict]:
    """Which of the two dimensions the network formulation actually needs.

    Four cells: room types on or off, stays spanning nights or not.  Turning a
    dimension off is done by collapsing it, never by changing the demand, so
    the same guests want the same nights in every cell and only the shape of
    the problem moves.
    """
    from .roomtypes import RoomType

    solo = Inventory([RoomType(inventory.entry.code, inventory.entry.name,
                               hotel.rooms, 1.00, 0)])
    rows: List[dict] = []
    for type_label, inv in (("many", inventory), ("one", solo)):
        for los_label, force in (("real", None), ("single", 1)):
            window = default_window(hotel, inv, nights=nights,
                                    demand_scale=demand_scale)
            scores: Dict[str, List[float]] = {c: [] for c in CONTROLS}
            for t in range(trials):
                stream = draw_stream(window, random.Random(seed + 1000 * t))
                if force:
                    stream = [Arrival(a.rid, a.segment, a.first_night, force,
                                      a.rooms, a.lead, a.taste) for a in stream]
                for control in CONTROLS:
                    fresh = [Arrival(a.rid, a.segment, a.first_night, a.los,
                                     a.rooms, a.lead, a.taste) for a in stream]
                    scores[control].append(
                        run_control(window, fresh, control)["contribution"])
            row = {"room_types": type_label, "length_of_stay": los_label,
                   "trials": trials}
            base = scores[CONTROLS[0]]
            for control in CONTROLS[1:]:
                diffs = [(x - b) / b for x, b in zip(scores[control], base) if b]
                mean = sum(diffs) / len(diffs)
                var = (sum((d - mean) ** 2 for d in diffs) / (len(diffs) - 1)
                       if len(diffs) > 1 else 0.0)
                row[control] = {"mean": mean,
                                "stderr": math.sqrt(var / len(diffs)),
                                "wins": sum(1 for d in diffs if d > 0)}
            rows.append(row)
    return rows
