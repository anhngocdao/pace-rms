"""Booking pace curves.

A pace curve answers one question: on a night like this one, how much of the
final business is normally on the books this many days out?  Every forecast
in the engine hangs off that curve, so it is estimated per demand class
(season band and day of week) and falls back to a house-wide curve whenever
a class is too thin to trust.

Two forms are kept, because they fail in opposite directions:

  ratio   final = on_the_books / typical_share_booked_by_now
          Sharp late, but violent early.  At 120 days out a single group can
          double the forecast.

  pickup  final = on_the_books + typical_rooms_still_to_come
          Stable early, but blind to a date that is genuinely running hot.

The forecaster blends them by inverse variance, so whichever form is more
reliable at that lead time carries the weight.  A third curve, per segment,
records how much of each segment is normally still to come.  The optimizer
needs it: at fourteen days out almost all group business is already on the
books, while most corporate business has not been booked yet.
"""

import datetime as dt
from collections import defaultdict
from statistics import fmean, pvariance
from typing import Dict, Iterable, List

from .calendar import demand_class
from .config import SEGMENT_ORDER
from .ledger import Ledger

GLOBAL_KEY = ("__house__", -1)


class PaceCurves:
    def __init__(self, max_lead: int = 180, min_obs: int = 8):
        self.max_lead = max_lead
        self.min_obs = min_obs
        self.ratio: Dict[tuple, List[float]] = {}
        self.ratio_var: Dict[tuple, List[float]] = {}
        self.pickup: Dict[tuple, List[float]] = {}
        self.pickup_var: Dict[tuple, List[float]] = {}
        self.counts: Dict[tuple, int] = {}
        self.segment_ratio: Dict[str, List[float]] = {}
        self.segment_share: Dict[tuple, Dict[str, float]] = {}

    def _resolve(self, key: tuple) -> tuple:
        """Use the class curve only when it has enough history behind it."""
        if self.counts.get(key, 0) >= self.min_obs:
            return key
        return GLOBAL_KEY

    def _clip(self, lead: int) -> int:
        return max(0, min(self.max_lead, lead))

    def ratio_at(self, key: tuple, lead: int) -> float:
        series = self.ratio.get(self._resolve(key))
        return max(0.02, series[self._clip(lead)]) if series else 1.0

    def ratio_sd(self, key: tuple, lead: int) -> float:
        series = self.ratio_var.get(self._resolve(key))
        return max(1e-4, series[self._clip(lead)] ** 0.5) if series else 0.5

    def pickup_at(self, key: tuple, lead: int) -> float:
        series = self.pickup.get(self._resolve(key))
        return max(0.0, series[self._clip(lead)]) if series else 0.0

    def pickup_sd(self, key: tuple, lead: int) -> float:
        series = self.pickup_var.get(self._resolve(key))
        return max(1e-4, series[self._clip(lead)] ** 0.5) if series else 10.0

    def segment_still_to_come(self, code: str, lead: int) -> float:
        """Share of this segment that is typically NOT yet booked at this lead."""
        series = self.segment_ratio.get(code)
        if not series:
            return 0.5
        return min(1.0, max(0.0, 1.0 - series[self._clip(lead)]))

    def mix(self, key: tuple) -> Dict[str, float]:
        share = self.segment_share.get(self._resolve(key)) or self.segment_share.get(GLOBAL_KEY)
        return share or {c: 1.0 / len(SEGMENT_ORDER) for c in SEGMENT_ORDER}

    def observations(self, key: tuple) -> int:
        return self.counts.get(key, 0)

    def is_ready(self) -> bool:
        return self.counts.get(GLOBAL_KEY, 0) >= self.min_obs


def build_pace_curves(ledger: Ledger, completed: Iterable[dt.date],
                      max_lead: int = 180, min_obs: int = 8) -> PaceCurves:
    completed = list(completed)
    curves = PaceCurves(max_lead=max_lead, min_obs=min_obs)

    ratio_obs: Dict[tuple, List[List[float]]] = defaultdict(
        lambda: [[] for _ in range(max_lead + 1)])
    pickup_obs: Dict[tuple, List[List[float]]] = defaultdict(
        lambda: [[] for _ in range(max_lead + 1)])
    mix_obs: Dict[tuple, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    seg_num = {c: [0.0] * (max_lead + 1) for c in SEGMENT_ORDER}
    seg_den = {c: [0.0] * (max_lead + 1) for c in SEGMENT_ORDER}

    for d in completed:
        snaps = ledger.snapshots.get(d)
        if not snaps:
            continue
        final = snaps.get(0)
        if final is None or final <= 0:
            continue
        keys = (demand_class(d), GLOBAL_KEY)
        for lead in range(0, max_lead + 1):
            otb = snaps.get(lead)
            if otb is None:
                continue
            for key in keys:
                ratio_obs[key][lead].append(otb / final)
                pickup_obs[key][lead].append(float(final - otb))
        for key in keys:
            for code, rooms in ledger.seg_rooms.get(d, {}).items():
                mix_obs[key][code] += max(0, rooms)

        # Per segment pace, read straight off the surviving bookings.  A hold
        # taken L days out is on the books at every lead from L down to zero.
        booked_at_lead = {c: [0.0] * (max_lead + 1) for c in SEGMENT_ORDER}
        for hold in ledger.holds.get(d, ()):  # noqa: B007
            lead = max(0, min(max_lead, (d - hold.booked_on).days))
            if hold.segment in booked_at_lead:
                booked_at_lead[hold.segment][lead] += hold.rooms
        for code in SEGMENT_ORDER:
            total = sum(booked_at_lead[code])
            if total <= 0:
                continue
            running = 0.0
            for lead in range(max_lead, -1, -1):
                running += booked_at_lead[code][lead]
                seg_num[code][lead] += running
                seg_den[code][lead] += total

    for key, per_lead in ratio_obs.items():
        curves.ratio[key] = [fmean(v) if v else 1.0 for v in per_lead]
        curves.ratio_var[key] = [pvariance(v) if len(v) > 1 else 0.25 for v in per_lead]
        curves.counts[key] = len(per_lead[0])
    for key, per_lead in pickup_obs.items():
        curves.pickup[key] = [fmean(v) if v else 0.0 for v in per_lead]
        curves.pickup_var[key] = [pvariance(v) if len(v) > 1 else 100.0 for v in per_lead]
    for key, mix in mix_obs.items():
        total = sum(mix.values())
        curves.segment_share[key] = (
            {c: mix.get(c, 0.0) / total for c in SEGMENT_ORDER} if total > 0
            else {c: 1.0 / len(SEGMENT_ORDER) for c in SEGMENT_ORDER})
    for code in SEGMENT_ORDER:
        if seg_den[code][0] > 0:
            curves.segment_ratio[code] = [
                (seg_num[code][i] / seg_den[code][i]) if seg_den[code][i] > 0 else 1.0
                for i in range(max_lead + 1)]
    return curves
