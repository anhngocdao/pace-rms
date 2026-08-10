"""The network bid price: what a room is worth when a stay spans several nights.

The closed form in optimize.py values one resource: one room type, one night.
Real demand does not arrive in that shape.  A guest asks for a deluxe king
from Friday to Monday, which consumes three separate pieces of inventory at
once, and the hotel has to decide whether that stay is worth more than the
three rooms it takes.  Pace answers this today by adding up three independent
nightly bid prices.  That approximation is standard, and it is wrong in a
specific and expensive direction.

    A Friday priced for a sellout has a high bid price.  A Sunday with forty
    rooms open has a bid price of nothing.  Summed, the three night stay is
    charged almost the full Friday scarcity and gets refused.  But accepting
    it fills two nights that were never going to fill, and the Friday room it
    takes would have gone to a one night guest paying a similar rate.  The
    independent sum cannot see that trade, because each night was valued as
    though the other two did not exist.

The fix is the one named at the end of METHOD.md: value the whole network at
once.  Products are stays.  Resources are (room type, night) cells.  A product
consumes one unit of every cell it touches.  Then

    max  sum_j v_j x_j     subject to   sum_j a_rj x_j <= c_r,  0 <= x_j <= d_j

and the dual price of each capacity constraint is that cell's bid price.  A
stay is worth taking when its total value clears the sum of the duals of every
cell it consumes: the same decision rule as before, computed against prices
that know about each other.

Two solvers live here and the difference between them is the point.

`dual_prices` solves the deterministic linear program above by exact
coordinate descent on its dual.  Minimising one coordinate has a closed form
and a plain reading: the bid price of a cell is the value of the marginal
request that fills it, after paying for the other cells that request also
consumes.  It is fast, and it treats demand as known, which it is not.

`decomposed_bid_prices` repairs that.  The duals say where the scarcity is; a
stay's value is prorated onto its nights in that proportion; then each night
is solved on its own with the stochastic closed form from optimize.py.  The
deterministic program prices a cell at zero until expected demand passes
capacity, while the stochastic form charges as soon as running out is
possible, which is earlier and correct.

Proration rather than displacement adjustment, and the difference matters.
Subtracting the other cells' duals is what virtual nesting does, and it is
right when the decision is taken one resource at a time.  A hotel decides on
the whole stay, against the sum of its nights, so subtracting the neighbours
inside every night makes a five night stay pay for its neighbours four times
over.  Proration keeps the parts summing to the whole and leaves the sum a
statement about the stay instead of about its length.

Neither solver is trusted blindly.  `dual_prices` also builds a feasible
primal solution and reports the duality gap, so a caller can see how far from
optimal the answer is instead of assuming it is close.
"""

import math
from dataclasses import dataclass, field
from typing import Dict, Hashable, Iterable, List, Optional, Tuple

from .optimize import bid_price_static, demand_sd

Cell = Tuple[str, Hashable]        # (room type, night)


@dataclass(frozen=True)
class Product:
    """One stay a guest might ask for, as the network sees it.

    value    total net value of the whole stay: rate over every night, less
             commission, less the variable cost of servicing the rooms.  Not
             per night.  The comparison the engine makes is stay against stay,
             so the units have to be stay sized.
    demand   expected number of such requests over the window being priced.
    cells    every (room type, night) this stay consumes, one unit each.
    """

    segment: str
    room_type: str
    arrival: Hashable
    los: int
    value: float
    demand: float
    cells: Tuple[Cell, ...]

    @property
    def key(self) -> tuple:
        return (self.segment, self.room_type, self.arrival, self.los)


@dataclass
class NetworkInstance:
    """Capacity, and the demand competing for it."""

    capacity: Dict[Cell, float]
    products: List[Product] = field(default_factory=list)

    def add(self, product: Product) -> None:
        self.products.append(product)

    def cells(self) -> Tuple[Cell, ...]:
        return tuple(self.capacity)

    def users(self) -> Dict[Cell, List[int]]:
        """Index from a cell to the products that consume it."""
        out: Dict[Cell, List[int]] = {c: [] for c in self.capacity}
        for i, p in enumerate(self.products):
            for c in p.cells:
                if c in out:
                    out[c].append(i)
        return out

    def total_demand_on(self, cell: Cell) -> float:
        return sum(p.demand for p in self.products if cell in p.cells)


@dataclass
class DualSolution:
    prices: Dict[Cell, float]
    dual_objective: float
    primal_value: float
    gap: float
    passes: int

    def price_of(self, cell: Cell) -> float:
        return self.prices.get(cell, 0.0)

    def stay_bid(self, product: Product) -> float:
        return sum(self.prices.get(c, 0.0) for c in product.cells)

    def accepts(self, product: Product) -> bool:
        return product.value > self.stay_bid(product)


# --------------------------------------------------------------- the dual LP


def _coordinate_minimum(threshold_demand: List[Tuple[float, float]],
                        capacity: float) -> float:
    """Exact minimiser of the dual in one coordinate.

    threshold_demand is [(t_j, d_j)] where t_j is what this cell may charge
    before product j stops clearing: its stay value less the duals of every
    other cell it consumes.  Sort those descending, take demand until the cell
    is full, and the price is the threshold of the request that filled it.  If
    nothing fills it the cell is free, which is the correct answer and the one
    a hotel finds hardest to believe in February.
    """
    if capacity <= 0:
        return max((t for t, _ in threshold_demand), default=0.0)
    ordered = sorted(threshold_demand, key=lambda td: -td[0])
    taken = 0.0
    for t, d in ordered:
        if t <= 0.0:
            break
        taken += d
        if taken > capacity:
            return t
    return 0.0


def dual_prices(instance: NetworkInstance, max_passes: int = 60,
                tol: float = 1e-7) -> DualSolution:
    """Bid price per cell, from the deterministic network linear program."""
    prices: Dict[Cell, float] = {c: 0.0 for c in instance.capacity}
    users = instance.users()
    products = instance.products

    passes = 0
    for passes in range(1, max_passes + 1):
        shift = 0.0
        for cell, idx in users.items():
            if not idx:
                shift = max(shift, abs(prices[cell]))
                prices[cell] = 0.0
                continue
            rows = []
            for i in idx:
                p = products[i]
                others = sum(prices.get(c, 0.0) for c in p.cells) - prices[cell]
                rows.append((p.value - others, p.demand))
            new = max(0.0, _coordinate_minimum(rows, instance.capacity.get(cell, 0.0)))
            shift = max(shift, abs(new - prices[cell]))
            prices[cell] = new
        if shift < tol:
            break

    dual_obj = _dual_objective(instance, prices)
    primal = _greedy_primal(instance, prices)
    gap = 0.0 if dual_obj <= 1e-9 else max(0.0, (dual_obj - primal) / dual_obj)
    return DualSolution(prices, dual_obj, primal, gap, passes)


def _dual_objective(instance: NetworkInstance, prices: Dict[Cell, float]) -> float:
    """sum_r c_r pi_r + sum_j d_j (v_j - sum_r pi_r)^+, an upper bound on the LP."""
    total = sum(instance.capacity[c] * prices.get(c, 0.0) for c in instance.capacity)
    for p in instance.products:
        surplus = p.value - sum(prices.get(c, 0.0) for c in p.cells)
        if surplus > 0.0:
            total += p.demand * surplus
    return total


def _greedy_primal(instance: NetworkInstance, prices: Dict[Cell, float]) -> float:
    """A feasible acceptance plan, so the gap above is measured and not assumed."""
    left = dict(instance.capacity)
    order = sorted(
        instance.products,
        key=lambda p: -(p.value - sum(prices.get(c, 0.0) for c in p.cells)))
    value = 0.0
    for p in order:
        if p.demand <= 0.0 or not p.cells:
            continue
        room = min((left.get(c, 0.0) for c in p.cells), default=0.0)
        take = min(p.demand, room)
        if take <= 0.0:
            continue
        value += take * p.value
        for c in p.cells:
            left[c] = left.get(c, 0.0) - take
    return value


# -------------------------------------------------------- stochastic overlay


def allocate(value: float, los: int, cells: Tuple[Cell, ...],
             duals: Dict[Cell, float]) -> Dict[Cell, float]:
    """Split a stay's value across the nights it occupies, in proportion to
    how scarce each of those nights is.

    The obvious alternative, subtracting the other cells' duals to get what
    the stay is worth to this one, is the displacement adjustment used in
    virtual nesting, and it must not be used here.  Virtual nesting decides on
    one resource at a time.  A hotel deciding on a stay compares the whole
    stay against the sum of its nights, and if every night has already
    subtracted the others, a five night stay pays its neighbours four times
    over and is refused for arithmetic reasons.

    Proportional allocation has the property that fixes it: the parts sum to
    the whole, exactly, so the sum of the per night bid prices remains a
    statement about this stay rather than a compounding of its length.  When
    every night is equally scarce it prorates evenly, which is the per night
    split the single resource engine already uses, so the network can only
    differ where the nights genuinely differ.  When nothing is scarce at all
    it prorates evenly too, and nothing is being protected anyway.
    """
    total = sum(duals.get(c, 0.0) for c in cells)
    if total <= 1e-9:
        even = value / max(1, los)
        return {c: even for c in cells}
    return {c: value * duals.get(c, 0.0) / total for c in cells}


def decomposed_bid_prices(instance: NetworkInstance,
                          duals: Optional[DualSolution] = None,
                          forecast_cv: float = 0.25) -> Dict[Cell, float]:
    """The network, with demand uncertain.

    Two steps.  The deterministic duals say where the scarcity is; the stay
    value is prorated onto its nights in that proportion; then each night is
    solved on its own with the stochastic closed form from optimize.py, which
    restores the uncertainty premium the linear program throws away.  A cell
    the deterministic program prices at zero because expected demand happens
    to sit just under capacity is priced here as soon as running out is
    possible, which is earlier and correct.
    """
    duals = duals or dual_prices(instance)
    shares: Dict[int, Dict[Cell, float]] = {}
    for i, p in enumerate(instance.products):
        shares[i] = allocate(p.value, p.los, p.cells, duals.prices)

    out: Dict[Cell, float] = {}
    for cell, idx in instance.users().items():
        classes: List[Tuple[float, float, float]] = []
        for i in idx:
            p = instance.products[i]
            v = shares[i].get(cell, 0.0)
            if v <= 0.0 or p.demand <= 0.0:
                continue
            classes.append((p.demand, demand_sd(p.demand, forecast_cv), v))
        bid = bid_price_static(int(math.floor(instance.capacity.get(cell, 0.0))), classes)
        out[cell] = 0.0 if bid == float("inf") else bid
    return out


def stay_bid(prices: Dict[Cell, float], cells: Iterable[Cell]) -> float:
    return sum(prices.get(c, 0.0) for c in cells)


# ---------------------------------------------------------------- comparison


def independent_bid_prices(instance: NetworkInstance,
                           forecast_cv: float = 0.25) -> Dict[Cell, float]:
    """What Pace does today, restated on this instance so it can be scored.

    Each cell is valued on its own, from the per night value of every product
    that touches it, with no knowledge that those products also consume other
    cells.  A three night stay is therefore counted at full strength in three
    separate valuations, and the stay is then charged the sum of all three.
    That double counting is exactly what the network formulation removes.
    """
    out: Dict[Cell, float] = {}
    for cell, idx in instance.users().items():
        classes = []
        for i in idx:
            p = instance.products[i]
            per_night = p.value / max(1, p.los)
            if per_night <= 0.0 or p.demand <= 0.0:
                continue
            classes.append((p.demand, demand_sd(p.demand, forecast_cv), per_night))
        bid = bid_price_static(int(math.floor(instance.capacity.get(cell, 0.0))), classes)
        out[cell] = 0.0 if bid == float("inf") else bid
    return out
