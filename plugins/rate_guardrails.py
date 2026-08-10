"""Example extension: post-optimizer rules.

Rules run after the optimizer has finished and may change the published rate.
Every change is recorded on the recommendation, so the dashboard can always
show what the optimizer wanted before policy touched it.  That audit trail is
the point: a rule that silently overrides the model is how a revenue system
loses the trust of the people who have to defend its numbers.

Lower priority numbers run first.
"""

from pace.config import SEGMENTS
from pace.plugins import rule

# A brand or owner mandated floor, by month.  Below these the property is
# considered to be damaging its rate position regardless of demand.
BRAND_FLOOR = {1: 119, 2: 119, 3: 129, 4: 139, 5: 149, 6: 159,
               7: 159, 8: 159, 9: 159, 10: 145, 11: 129, 12: 125}


@rule("brand_floor", priority=10)
def brand_floor(rec, ctx):
    floor = BRAND_FLOOR.get(rec.stay_date.month)
    if floor and rec.rate < floor:
        rec.rate = float(floor)
    return rec


@rule("charm_pricing", priority=90)
def charm_pricing(rec, ctx):
    """Publish rates ending in nine, without ever crossing the bid price.

    Round to the nearest nine rather than always downward.  Always rounding
    down looks harmless and quietly costs several dollars on every night the
    hotel sells, which over a year is a rate position nobody decided to give
    away.  When the nearest nine falls under what the room is worth, take the
    one above instead: a presentation preference does not get to sell
    inventory below its opportunity cost.
    """
    hotel = ctx["hotel"]
    retail = SEGMENTS["RETAIL"]
    hard_floor = (rec.bid_price + hotel.variable_cost) / (1.0 - retail.commission)

    near = round((rec.rate - 9.0) / 10.0) * 10 + 9
    chosen = near if near >= hard_floor else near + 10
    chosen = max(hotel.rate_floor, min(hotel.rate_ceiling, chosen))
    if abs(chosen - rec.rate) > 0.001:
        rec.rate = float(chosen)
    return rec
