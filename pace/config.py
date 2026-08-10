"""Property configuration.

Everything the engine knows about a specific hotel lives in this module:
physical capacity, cost structure, the rate ladder it is allowed to quote,
and the demand segments it sells into.  Pointing the engine at a different
property means editing this file (or loading the same shape from JSON).
No engine module contains a hard-coded hotel number.
"""

from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass(frozen=True)
class Segment:
    """One demand stream the hotel sells into.

    rate_multiplier   what this segment pays relative to the public BAR
    commission        share of room revenue paid away to the channel
    prior_elasticity  starting belief about price sensitivity.  The fitted
                      value is shrunk toward this, so the engine still
                      behaves sensibly on thin data.
    lead_time_mean    average days between booking and arrival
    lead_time_shape   gamma shape.  Low values give a long last-minute tail.
    los_weights       probability of a 1..5 night stay
    dow_weights       relative arrival volume, Monday first
    share             share of total house demand
    wtp_sigma         lognormal spread of willingness to pay
    wtp_premium       median willingness to pay, as a multiple of the rate
                      this segment is normally quoted
    block_size        rooms per request (groups book in blocks)
    floats_with_bar   True for business quoted off today's public rate.
                      False for contracted business: a negotiated corporate
                      rate and a signed group rate do not move when the BAR
                      moves, so raising the public rate neither earns more
                      from them nor drives them away.  The engine still has
                      to decide whether to let them have the room, which is
                      what the bid price and the segment closures are for.
    """

    code: str
    name: str
    rate_multiplier: float
    commission: float
    prior_elasticity: float
    lead_time_mean: float
    lead_time_shape: float
    los_weights: Tuple[float, ...]
    dow_weights: Tuple[float, ...]
    share: float
    cancel_rate: float
    no_show_rate: float
    wtp_sigma: float
    wtp_premium: float = 1.0
    block_size: Tuple[int, int] = (1, 1)
    floats_with_bar: bool = True


SEGMENTS: Dict[str, Segment] = {
    "RETAIL": Segment(
        code="RETAIL",
        name="Retail transient (direct BAR)",
        rate_multiplier=1.00,
        commission=0.02,
        prior_elasticity=1.90,
        lead_time_mean=18.0,
        lead_time_shape=1.5,
        los_weights=(0.42, 0.33, 0.16, 0.06, 0.03),
        dow_weights=(0.80, 0.78, 0.86, 0.98, 1.38, 1.55, 1.05),
        share=0.34,
        cancel_rate=0.14,
        no_show_rate=0.02,
        wtp_sigma=0.34,
        wtp_premium=1.05,
    ),
    "OTA": Segment(
        code="OTA",
        name="Online travel agency",
        rate_multiplier=1.00,
        commission=0.17,
        prior_elasticity=2.40,
        lead_time_mean=26.0,
        lead_time_shape=1.7,
        los_weights=(0.46, 0.31, 0.15, 0.05, 0.03),
        dow_weights=(0.82, 0.80, 0.88, 1.00, 1.40, 1.58, 1.08),
        share=0.28,
        cancel_rate=0.24,
        no_show_rate=0.03,
        wtp_sigma=0.30,
        wtp_premium=1.02,
    ),
    "CORP": Segment(
        code="CORP",
        name="Negotiated corporate (LNR)",
        rate_multiplier=0.82,
        commission=0.00,
        prior_elasticity=0.80,
        lead_time_mean=8.0,
        lead_time_shape=1.2,
        los_weights=(0.55, 0.30, 0.10, 0.04, 0.01),
        dow_weights=(1.45, 1.52, 1.46, 1.28, 0.58, 0.24, 0.52),
        share=0.26,
        cancel_rate=0.10,
        no_show_rate=0.05,
        wtp_sigma=0.22,
        wtp_premium=1.18,
        floats_with_bar=False,
    ),
    "GROUP": Segment(
        code="GROUP",
        name="Group and contract",
        rate_multiplier=0.70,
        commission=0.00,
        prior_elasticity=0.45,
        lead_time_mean=105.0,
        lead_time_shape=3.0,
        los_weights=(0.15, 0.35, 0.30, 0.15, 0.05),
        dow_weights=(1.20, 1.30, 1.25, 1.02, 0.62, 0.40, 0.70),
        share=0.12,
        cancel_rate=0.08,
        no_show_rate=0.01,
        wtp_sigma=0.18,
        wtp_premium=1.12,
        block_size=(8, 34),
        floats_with_bar=False,
    ),
}

# Order matters for the optimizer only in that higher value classes should be
# considered first when two requests tie.  Kept explicit so results are stable.
SEGMENT_ORDER = ("CORP", "GROUP", "RETAIL", "OTA")

# How strongly each segment reacts to a citywide demand event.  Groups are
# already contracted when the event is announced, so they barely move.
EVENT_SENSITIVITY = {"RETAIL": 1.00, "OTA": 1.10, "CORP": 0.35, "GROUP": 0.15}


@dataclass(frozen=True)
class Hotel:
    name: str = "Hotel Aurora"
    city: str = "Toronto"
    rooms: int = 150
    base_rate: float = 189.0
    rate_floor: float = 109.0
    rate_ceiling: float = 469.0
    rate_step: float = 4.0
    variable_cost: float = 31.0      # housekeeping, amenities, laundry per sold room
    walk_cost: float = 620.0         # relocation, transport, comp, goodwill
    currency: str = "CAD"
    base_daily_demand: float = 106.0  # requests per night at the reference rate
    max_los: int = 5
    max_lead: int = 180
    max_overbook_pct: float = 0.06

    def rate_ladder(self):
        """The discrete set of public rates the engine may quote."""
        rates, r = [], self.rate_floor
        while r <= self.rate_ceiling + 1e-9:
            rates.append(round(r, 2))
            r += self.rate_step
        return rates


DEFAULT_HOTEL = Hotel()
