"""What a guest does when the hotel offers more than one room.

The price response in elasticity.py answers one question: of the guests who
wanted this hotel on this night, how many still book at this rate.  With
several room types there are two questions, and conflating them is a
modelling error with money attached.

    1.  Does the guest book here at all, or go elsewhere.
    2.  Given that they book, which room do they take.

Guests are far more willing to change room type than to change hotel.  Put
forty dollars on the suite and most of that demand moves to the executive
room; put forty dollars on every rate in the house and it leaves.  So the two
questions carry different price sensitivities, and the second is the larger.
That is the standard nested logit structure: a purchase decision across the
nest, an allocation decision inside it, with the inside sensitivity equal to
the outside one divided by the dissimilarity parameter.

    u_i     = theta_i - (k / lambda) * (p_i / ref_i - 1)
    s_i     = exp(u_i) / sum over offered types
    rho     = sum_i s_i * (p_i / ref_i)          the price the guest faces
    demand  = arrivals * acceptance(k, rho) * s_i

Two properties of this construction matter more than its exact shape.

First, with one room type it *is* the model in elasticity.py.  s = 1, rho is
the price ratio, and the answer is acceptance(k, ratio) exactly.  Adding room
types to a property therefore cannot silently move the house demand curve.

Second, if every type moves by the same proportion, rho moves by that
proportion and total demand behaves exactly as the single resource model says
it does.  Substitution only appears when the *relative* prices change, which
is the honest statement: a hotel that prices its types on a fixed multiplier
ladder has four room types and one decision, and gains nothing from any of
this.  The network optimizer earns its keep precisely by breaking the ladder.
"""

import math
from typing import Dict, Iterable, Optional

from .elasticity import acceptance
from .roomtypes import Inventory

# Nested logit dissimilarity.  1.0 would mean a guest is exactly as willing to
# leave the hotel as to take a different room, which no hotel has ever
# observed.  Lower means stickier: they stay and take what is left.
DEFAULT_NEST_LAMBDA = 0.62


class SubstitutionModel:
    """Splits a segment's arrivals across the room types on offer."""

    def __init__(self, inventory: Inventory,
                 preferences: Dict[str, Dict[str, float]],
                 nest_lambda: float = DEFAULT_NEST_LAMBDA):
        if not 0.0 < nest_lambda <= 1.0:
            raise ValueError("nest lambda must sit in (0, 1]")
        self.inventory = inventory
        self.preferences = preferences
        self.nest_lambda = nest_lambda

    # ---------------------------------------------------------------- shares

    def shares(self, code: str, ratios: Dict[str, float], k: float,
               offered: Optional[Iterable[str]] = None) -> Dict[str, float]:
        """Share of this segment's bookings taken by each offered type.

        ratios maps a room type to its quoted rate over its own reference
        rate, so a value of 1.0 means "priced where this type normally sits"
        whatever the type is.  Anything not offered is dropped and its share
        redistributes across what is left, which is the substitution.
        """
        codes = list(offered) if offered is not None else list(self.inventory.codes)
        codes = [c for c in codes if c in self.inventory.by_code]
        if not codes:
            return {}
        theta = self.preferences.get(code, {})
        beta = k / self.nest_lambda
        utils = {c: theta.get(c, 0.0) - beta * (ratios.get(c, 1.0) - 1.0) for c in codes}
        top = max(utils.values())
        weights = {c: math.exp(max(-60.0, u - top)) for c, u in utils.items()}
        total = sum(weights.values())
        if total <= 0.0:
            even = 1.0 / len(codes)
            return {c: even for c in codes}
        return {c: w / total for c, w in weights.items()}

    # ---------------------------------------------------------------- demand

    def split(self, code: str, arrivals: float, k: float,
              ratios: Dict[str, float],
              offered: Optional[Iterable[str]] = None) -> Dict[str, float]:
        """Expected bookings per room type, from this segment, at these prices.

        Summing the result gives house level demand, and that sum is the
        single resource answer whenever the types are priced in step.
        """
        if arrivals <= 0.0:
            return {}
        s = self.shares(code, ratios, k, offered)
        if not s:
            return {}
        rho = sum(share * ratios.get(c, 1.0) for c, share in s.items())
        taken = arrivals * acceptance(k, rho)
        return {c: taken * share for c, share in s.items()}

    def house_acceptance(self, code: str, k: float, ratios: Dict[str, float],
                         offered: Optional[Iterable[str]] = None) -> float:
        """Share of reference demand that still books somewhere in the house."""
        s = self.shares(code, ratios, k, offered)
        if not s:
            return 0.0
        rho = sum(share * ratios.get(c, 1.0) for c, share in s.items())
        return acceptance(k, rho)


def ratios_from_rates(inventory: Inventory, quoted: Dict[str, float],
                      reference_bar: float) -> Dict[str, float]:
    """Turn a quoted rate per type into the price ratios the model wants.

    The reference for a type is the reference public rate carried up the
    multiplier ladder, so quoting every type on the ladder returns a ratio of
    exactly one everywhere, and quoting one type above its ladder position
    shows up as that type alone being expensive.
    """
    out: Dict[str, float] = {}
    for t in inventory:
        ref = reference_bar * t.rate_multiplier
        if ref <= 0:
            continue
        rate = quoted.get(t.code)
        if rate is None or rate <= 0:
            continue
        out[t.code] = rate / ref
    return out
