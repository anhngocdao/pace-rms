"""The unit of output: one night, one decision, and the reasons for it."""

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Recommendation:
    stay_date: dt.date
    asof: dt.date
    lead: int

    otb: int
    authorized: int
    capacity_left: int

    rate: float
    prior_rate: float
    bid_price: float
    bound_by: str

    expected_final: float
    expected_occupancy: float
    expected_adr: float
    expected_revpar: float
    demand_at_reference: float
    pace_index: float
    censoring_uplift: float

    expected_sellout: bool = False
    experiment_arm: float = 0.0
    policy_rate: float = 0.0
    mlos: int = 1
    cta: bool = False
    closed: frozenset = frozenset()

    events: List[str] = field(default_factory=list)
    signals: List[Any] = field(default_factory=list)
    rule_trace: List[str] = field(default_factory=list)

    headline: str = ""
    drivers: List[str] = field(default_factory=list)
    narrative: str = ""
    confidence: str = "medium"
    forecast: Optional[Any] = None

    def to_row(self) -> Dict[str, Any]:
        return {
            "date": self.stay_date.isoformat(),
            "dow": self.stay_date.strftime("%a"),
            "lead": self.lead,
            "otb": self.otb,
            "authorized": self.authorized,
            "rate": round(self.rate, 2),
            "bid_price": round(self.bid_price, 2),
            "bound_by": self.bound_by,
            "forecast_rooms": round(self.expected_final, 1),
            "forecast_occ": round(self.expected_occupancy, 4),
            "sellout": self.expected_sellout,
            "experiment_arm": round(self.experiment_arm, 3),
            "policy_rate": round(self.policy_rate, 2),
            "forecast_adr": round(self.expected_adr, 2),
            "forecast_revpar": round(self.expected_revpar, 2),
            "demand_at_reference": round(self.demand_at_reference, 1),
            "pace_index": round(self.pace_index, 3),
            "mlos": self.mlos,
            "cta": self.cta,
            "closed": sorted(self.closed),
            "events": self.events,
            "signals": [{"name": s.name, "multiplier": round(s.multiplier, 3),
                         "reason": s.reason} for s in self.signals],
            "rules": self.rule_trace,
            "headline": self.headline,
            "drivers": self.drivers,
            "narrative": self.narrative,
            "confidence": self.confidence,
        }
