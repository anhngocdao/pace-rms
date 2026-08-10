"""Policies: the engine, and the two baselines it has to beat.

StaticBAR      one rate all year.  The honest floor for any comparison.
SeasonalLadder what a well run independent hotel actually does: a seasonal
               base rate nudged up and down by how full the night looks.
               No forecast, no opportunity cost, no restrictions.
PaceEngine     forecast, unconstrain, price against the bid price, and set
               restrictions when rate runs out of room.

The engine is deliberately allowed to fall back to the ladder while it is
cold.  A revenue system with no history should behave like a sensible manager,
not like a confident one.
"""

import datetime as dt
from typing import Dict, List, Optional

from .calendar import EventCalendar, month_factor
from .config import SEGMENTS, Hotel
from .controls import (authorized_capacity, blended_no_show, closed_segments,
                       min_length_of_stay, remaining_cancel_probability)
from .elasticity import PriceResponse, fit_all
from .forecast import Forecaster
from .ledger import Controls, Ledger
from .optimize import expected_bookings_at, net_value, solve
from .otb import build_pace_curves
from .plugins import apply_rules, collect_signals
from .recommendation import Recommendation
from .simulate import Policy, quoted_rate, reference_rate
from .unconstrain import class_demand


def _snap(ladder: List[float], rate: float) -> float:
    return min(ladder, key=lambda r: abs(r - rate))


class StaticPolicy(Policy):
    name = "Static BAR"

    def __init__(self, hotel: Hotel, rate: Optional[float] = None):
        self.hotel = hotel
        self.rate = rate if rate else hotel.base_rate

    def controls(self, asof: dt.date, ledger: Ledger) -> Controls:
        c = Controls()
        for lead in range(0, self.hotel.max_lead + 1):
            d = asof + dt.timedelta(days=lead)
            if d > ledger.last_stay:
                break
            c.rate[d] = self.rate
            c.authorized[d] = self.hotel.rooms
        return c


class LadderPolicy(Policy):
    """Occupancy triggered pricing.  Reactive by construction: it can only
    respond to rooms already sold, never to the demand still coming."""

    name = "Seasonal ladder"
    STEPS = ((0.90, 1.35), (0.75, 1.20), (0.60, 1.08),
             (0.40, 1.00), (0.25, 0.94), (0.00, 0.88))

    def __init__(self, hotel: Hotel):
        self.hotel = hotel
        self.ladder = hotel.rate_ladder()

    def rate_for(self, ledger: Ledger, d: dt.date) -> float:
        base = self.hotel.base_rate * month_factor(d)
        occ = ledger.rooms_on(d) / self.hotel.rooms
        mult = self.STEPS[-1][1]
        for threshold, m in self.STEPS:
            if occ >= threshold:
                mult = m
                break
        return _snap(self.ladder, base * mult)

    def controls(self, asof: dt.date, ledger: Ledger) -> Controls:
        c = Controls()
        for lead in range(0, self.hotel.max_lead + 1):
            d = asof + dt.timedelta(days=lead)
            if d > ledger.last_stay:
                break
            c.rate[d] = self.rate_for(ledger, d)
            c.authorized[d] = self.hotel.rooms
        return c


class HandoverPolicy(Policy):
    """Run one policy up to a date, then hand the house to another.

    Both policies see every day, so the incoming engine has already learned
    from the outgoing manager's history on the day it takes over.  That is
    how an installation actually goes.
    """

    def __init__(self, warm: Policy, live: Policy, handover: dt.date):
        self.warm = warm
        self.live = live
        self.handover = handover
        self.name = getattr(live, "name", "policy")

    def controls(self, asof: dt.date, ledger: Ledger) -> Controls:
        active = self.live if asof >= self.handover else self.warm
        return active.controls(asof, ledger)

    def observe(self, asof: dt.date, ledger: Ledger) -> None:
        self.warm.observe(asof, ledger)
        self.live.observe(asof, ledger)


class PaceEngine(Policy):
    name = "Pace engine"

    def __init__(self, hotel: Hotel, cal: EventCalendar, horizon: int = 180,
                 refit_every: int = 28, warmup_nights: int = 150,
                 max_daily_move: float = 0.12, min_move_steps: float = 1.5,
                 experiment=None, fallback: Optional[Policy] = None):
        self.hotel = hotel
        self.cal = cal
        self.horizon = min(horizon, hotel.max_lead)
        self.refit_every = refit_every
        self.warmup_nights = warmup_nights
        self.max_daily_move = max_daily_move
        self.min_move_steps = min_move_steps
        self.experiment = experiment
        self.fallback = fallback or LadderPolicy(hotel)
        self.ladder = hotel.rate_ladder()

        self.curves = None
        self.demand = None
        self.response: Optional[PriceResponse] = None
        self.forecaster: Optional[Forecaster] = None
        self.ready = False
        self.last_fit: Optional[dt.date] = None
        self.fit_log: List[dict] = []

        self.cache: Dict[dt.date, Recommendation] = {}
        self.last_solved: Dict[dt.date, tuple] = {}
        self.rates: Dict[dt.date, float] = {}
        self.bids: Dict[dt.date, float] = {}
        self.solves = 0

    # ------------------------------------------------------------- learning

    def observe(self, asof: dt.date, ledger: Ledger) -> None:
        completed = [d for d in ledger.settled if d < asof]
        if len(completed) < self.warmup_nights:
            return
        if self.last_fit is not None and (asof - self.last_fit).days < self.refit_every:
            return
        self._fit(asof, ledger, completed)

    def _fit(self, asof: dt.date, ledger: Ledger, completed: List[dt.date]) -> None:
        self.curves = build_pace_curves(ledger, completed, max_lead=self.hotel.max_lead)
        self.response = fit_all(ledger, self.hotel, self.cal, completed, self.experiment)
        self.demand = class_demand(ledger, completed, self.hotel, self.cal, self.response)
        self.forecaster = Forecaster(self.hotel, self.cal, self.curves,
                                     self.demand, self.response)
        self.ready = self.curves.is_ready()
        self.last_fit = asof
        self.fit_log.append({
            "asof": asof.isoformat(),
            "nights_of_history": len(completed),
            "price_response": self.response.as_dict(),
            "censoring_uplift_house": round(self.demand.censoring_uplift(("__house__", -1)), 4),
            "experiment": self.experiment.summary() if self.experiment else None,
        })

    # ------------------------------------------------------------ decisions

    def _damp(self, d: dt.date, target: float, expected_new: float, asof: dt.date,
              floor: float = 0.0) -> float:
        """Publish a rate a human would not argue with.

        Two guards, both standard practice.  A daily move limit, because a
        rate that swings forty percent overnight looks like a fault to a
        guest and to a channel manager alike.  And a dead band, because
        moving four dollars is not worth the churn it causes downstream.
        Both scale with how long it has been since this date was last priced.

        Neither guard may push the rate below the bid price floor.  Smoothing
        is a preference about how the hotel looks to the market; the floor is
        a statement that selling the room this cheap loses money, and a
        preference does not get to overrule that.
        """
        prior = self.rates.get(d)
        hard_floor = min(self.hotel.rate_ceiling,
                         max(self.hotel.rate_floor, _snap(self.ladder, floor)))
        if prior is None:
            return max(target, hard_floor)
        if expected_new < 0.5:
            return max(prior, hard_floor)
        last = self.last_solved.get(d)
        gap = max(1, (asof - last[0]).days) if last else 1
        allowed = max(self.hotel.rate_step, prior * self.max_daily_move * gap)
        bounded = max(prior - allowed, min(prior + allowed, target))
        snapped = max(_snap(self.ladder, bounded), hard_floor)
        if abs(snapped - prior) < self.hotel.rate_step * self.min_move_steps:
            return prior
        return snapped

    def _cadence(self, lead: int) -> int:
        if lead <= 14:
            return 1
        if lead <= 45:
            return 3
        if lead <= 90:
            return 7
        return 14

    def _needs_solve(self, d: dt.date, asof: dt.date, ledger: Ledger) -> bool:
        last = self.last_solved.get(d)
        if last is None:
            return True
        last_asof, last_otb = last
        if (asof - last_asof).days >= self._cadence((d - asof).days):
            return True
        return abs(ledger.rooms_on(d) - last_otb) >= 4

    def recommend(self, ledger: Ledger, d: dt.date, asof: dt.date) -> Recommendation:
        hotel = self.hotel
        prior = self.rates.get(d, _snap(self.ladder, hotel.base_rate * month_factor(d)))
        fc = self.forecaster.forecast(ledger, d, asof, current_bar=prior)

        ctx = {"hotel": hotel, "ledger": ledger, "asof": asof,
               "forecast": fc, "calendar": self.cal, "engine": self}
        signals = collect_signals(d, ctx)
        lift = 1.0
        for s in signals:
            lift *= s.multiplier
        remaining = {c: v * lift for c, v in fc.remaining_by_segment.items()}

        cancel_p = remaining_cancel_probability(ledger, d, asof)
        no_show_p = blended_no_show(ledger, d)
        adr_estimate = fc.otb_adr if fc.otb_adr > 0 else prior
        authorized = authorized_capacity(hotel, adr_estimate, cancel_p, no_show_p)
        otb = ledger.rooms_on(d)
        capacity_left = max(0, authorized - otb)

        refs = {c: reference_rate(hotel, self.cal, d, c) for c in SEGMENTS}

        if capacity_left <= 0:
            # Nothing left to sell.  Publishing a ceiling rate here would be
            # theatre: the date is closed, and the rate a closed date shows is
            # noise in every downstream channel.  Hold the last published rate
            # and let the closure carry the message.
            rate = prior
            bid = net_value(hotel, "RETAIL", hotel.rate_ceiling, refs["RETAIL"])
            bound = "sold out"
        else:
            sol = solve(hotel, remaining, refs, self.response, capacity_left,
                        self.ladder, start_rate=prior)
            bid = sol["bid_price"]
            bound = sol["bound_by"]
            rate = self._damp(d, sol["rate"], sum(sol["expected_bookings"].values()),
                              asof, floor=sol["implied_floor"])

        rec = Recommendation(
            stay_date=d, asof=asof, lead=(d - asof).days,
            otb=otb, authorized=authorized, capacity_left=capacity_left,
            rate=rate, prior_rate=prior, bid_price=bid, bound_by=bound,
            expected_final=float(otb), expected_occupancy=0.0, expected_sellout=False,
            expected_adr=0.0, expected_revpar=0.0,
            demand_at_reference=fc.demand_at_reference,
            pace_index=fc.pace_index, censoring_uplift=fc.censoring_uplift,
            events=[e.name for e in self.cal.active(d)],
            signals=signals, forecast=fc,
        )

        # Order matters here.  Rules are policy: a brand floor or a rounding
        # convention is part of the rate the hotel stands behind, so it is what
        # tomorrow's move limit measures against.  The experiment is a
        # deviation from policy, deliberately not remembered, or the move limit
        # would spend the next day undoing it.
        rec = apply_rules(rec, ctx)
        rec.policy_rate = rec.rate

        if self.experiment is not None and capacity_left > 0:
            probe = expected_bookings_at(hotel, remaining, refs, self.response, rec.rate)
            provisional = otb + min(capacity_left, sum(probe.values()))
            floor = min(hotel.rate_ceiling,
                        max(hotel.rate_floor, _snap(self.ladder, (bid + hotel.variable_cost)
                                                    / (1.0 - SEGMENTS["RETAIL"].commission))))
            published, arm = self.experiment.apply(
                rec.rate, d, rec.lead, provisional >= hotel.rooms * 0.995,
                floor, self.ladder)
            if arm:
                rec.rate = published
                rec.experiment_arm = arm
                rec.bound_by = "rate experiment"

        self._project(rec, hotel, remaining, refs, fc.otb_adr)
        self.solves += 1
        return rec

    def _project(self, rec: Recommendation, hotel: Hotel, remaining: Dict[str, float],
                 refs: Dict[str, float], otb_adr: float) -> None:
        """What this night is expected to end up doing, at the rate published.

        Run last, after rules and after any experimental deviation, so the
        numbers on the dashboard belong to the rate the hotel is actually
        quoting rather than to the one the optimizer proposed.
        """
        booked = ({} if rec.capacity_left <= 0
                  else expected_bookings_at(hotel, remaining, refs, self.response, rec.rate))
        total = sum(booked.values())
        new_rooms = min(rec.capacity_left, total)
        if total > 0 and new_rooms > 0:
            scale = new_rooms / total
            new_revenue = sum(n * scale * quoted_rate(c, rec.rate, refs[c])
                              for c, n in booked.items())
        else:
            new_revenue = 0.0
        expected_final = rec.otb + new_rooms
        revenue = rec.otb * otb_adr + new_revenue
        rec.expected_final = expected_final
        rec.expected_occupancy = min(1.0, expected_final / hotel.rooms)
        rec.expected_sellout = expected_final >= hotel.rooms * 0.995
        rec.expected_adr = (revenue / expected_final) if expected_final > 0 else 0.0
        rec.expected_revpar = revenue / hotel.rooms


    def controls(self, asof: dt.date, ledger: Ledger) -> Controls:
        if not self.ready or self.forecaster is None:
            return self.fallback.controls(asof, ledger)

        dates: List[dt.date] = []
        for lead in range(0, self.horizon + 1):
            d = asof + dt.timedelta(days=lead)
            if d > ledger.last_stay:
                break
            dates.append(d)

        for d in dates:
            if self._needs_solve(d, asof, ledger) or d not in self.cache:
                self.cache[d] = self.recommend(ledger, d, asof)
                self.last_solved[d] = (asof, ledger.rooms_on(d))
            rec = self.cache[d]
            self.rates[d] = rec.policy_rate
            self.bids[d] = rec.bid_price

        ctrl = Controls()
        for d in dates:
            rec = self.cache[d]
            mlos = min_length_of_stay(self.hotel, d, self.rates, self.bids)
            rec.cta = mlos > self.hotel.max_los
            rec.mlos = 1 if rec.cta else mlos
            refs_d = {c: reference_rate(self.hotel, self.cal, d, c) for c in SEGMENTS}
            rec.closed = closed_segments(self.hotel, rec.rate, rec.bid_price, refs_d)

            ctrl.rate[d] = rec.rate
            ctrl.authorized[d] = rec.authorized
            ctrl.mlos[d] = rec.mlos
            ctrl.cta[d] = rec.cta
            ctrl.closed[d] = rec.closed
        return ctrl
