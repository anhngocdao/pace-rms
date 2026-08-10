"""Forecast for one stay date, as of one day.

The chain, in order:

  on the books
      -> expected final bookings      (pace curves, ratio blended with pickup)
      -> unconstrained demand         (censoring uplift for this class)
      -> demand at the reference rate (divide out the price already charged)
      -> remaining demand by segment  (segment pace curves)

Only the last line feeds the optimizer.  Keeping the intermediate steps
addressable is deliberate: when a recommendation looks wrong, a revenue
manager needs to see which link broke, not just the answer.
"""

import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, Optional

from .calendar import EventCalendar, demand_class, month_factor
from .config import SEGMENT_ORDER, SEGMENTS, Hotel
from .ledger import Ledger
from .otb import PaceCurves
from .elasticity import PriceResponse
from .simulate import reference_rate
from .unconstrain import ClassDemand


@dataclass
class DateForecast:
    stay_date: dt.date
    asof: dt.date
    lead: int
    class_key: tuple
    otb: int
    otb_adr: float
    ratio_estimate: float
    pickup_estimate: float
    ratio_weight: float
    expected_bookings: float
    unconstrained_demand: float
    demand_at_reference: float
    remaining_by_segment: Dict[str, float] = field(default_factory=dict)
    censoring_uplift: float = 1.0
    class_observations: int = 0
    pace_index: float = 1.0        # OTB now versus the class norm at this lead

    @property
    def forecast_occupancy_pct(self) -> float:
        return self.expected_bookings


class Forecaster:
    def __init__(self, hotel: Hotel, cal: EventCalendar, curves: PaceCurves,
                 demand: ClassDemand, response: PriceResponse):
        self.hotel = hotel
        self.cal = cal
        self.curves = curves
        self.demand = demand
        self.response = response

    def forecast(self, ledger: Ledger, stay_date: dt.date, asof: dt.date,
                 current_bar: Optional[float] = None) -> DateForecast:
        hotel = self.hotel
        lead = (stay_date - asof).days
        key = demand_class(stay_date)
        otb = ledger.rooms_on(stay_date)
        otb_adr = ledger.adr_on(stay_date)

        ratio = self.curves.ratio_at(key, lead)
        ratio_sd = self.curves.ratio_sd(key, lead)
        pickup = self.curves.pickup_at(key, lead)
        pickup_sd = self.curves.pickup_sd(key, lead)

        f_ratio = otb / ratio if ratio > 0 else float(otb)
        f_pickup = otb + pickup

        # Variance of each estimate.  The ratio form inherits the variance of
        # the curve amplified by how little is on the books; early on that
        # blows up, which is exactly when it should be distrusted.
        var_ratio = ((otb / (ratio * ratio)) ** 2) * (ratio_sd ** 2) if otb > 0 else float("inf")
        var_pickup = pickup_sd ** 2
        w_ratio = 0.0 if var_ratio == float("inf") or var_ratio <= 0 else 1.0 / var_ratio
        w_pickup = 0.0 if var_pickup <= 0 else 1.0 / var_pickup
        if w_ratio + w_pickup <= 0:
            expected = float(otb)
            weight = 0.0
        else:
            weight = w_ratio / (w_ratio + w_pickup)
            expected = weight * f_ratio + (1.0 - weight) * f_pickup
        expected = max(float(otb), expected)

        uplift = self.demand.censoring_uplift(key)
        unconstrained = expected * uplift

        # Demand for this date, restated at the reference rate.
        #
        # Everything here has to add up: what is already booked, plus what is
        # still to come, must equal the total demand the engine believes in.
        # The share of demand normally booked by this lead is rebuilt from the
        # per segment pace curves rather than the house curve, because that is
        # the same decomposition the remaining demand is split by.  Using the
        # house curve for one and the segment curves for the other is how a
        # forecast quietly stops summing to itself.
        mix = self.curves.mix(key)
        refs = {c: reference_rate(hotel, self.cal, stay_date, c) for c in SEGMENT_ORDER}
        still = {c: self.curves.segment_still_to_come(c, lead) for c in SEGMENT_ORDER}
        booked_share = sum(mix.get(c, 0.0) * (1.0 - still[c]) for c in SEGMENT_ORDER)
        booked_share = max(0.02, min(0.999, booked_share))

        # Rooms already sold, divided by the acceptance at the rate they were
        # sold at, so that a night discounted into submission does not read as
        # a night with more demand than it had.
        otb_at_ref = 0.0
        for code, rooms in ledger.segment_mix(stay_date).items():
            if rooms <= 0 or code not in refs:
                continue
            revenue = ledger.seg_revenue.get(stay_date, {}).get(code, 0.0)
            taken = max(0.25, self.response.accept(code, revenue / rooms, refs[code]))
            otb_at_ref += rooms / taken

        class_level = max(1.0, self.demand.mean(key))
        date_estimate = otb_at_ref / booked_share
        # Weight the date's own signal by how much of its business is in.  At
        # four months out that is almost nothing and the class norm carries the
        # forecast; in the last fortnight the date speaks for itself.
        demand_at_ref = booked_share * date_estimate + (1.0 - booked_share) * class_level
        demand_at_ref = max(otb_at_ref, demand_at_ref)

        remaining = {c: max(0.0, demand_at_ref * mix.get(c, 0.0) * still[c])
                     for c in SEGMENT_ORDER}

        pace_index = date_estimate / class_level if class_level > 0 else 1.0
        pace_index = max(0.25, min(4.0, pace_index))

        return DateForecast(
            stay_date=stay_date, asof=asof, lead=lead, class_key=key,
            otb=otb, otb_adr=otb_adr,
            ratio_estimate=f_ratio, pickup_estimate=f_pickup, ratio_weight=weight,
            expected_bookings=expected, unconstrained_demand=unconstrained,
            demand_at_reference=demand_at_ref, remaining_by_segment=remaining,
            censoring_uplift=uplift, class_observations=self.curves.observations(key),
            pace_index=pace_index,
        )
