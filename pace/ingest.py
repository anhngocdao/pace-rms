"""Real booking history into the engine's ledger (spec: docs/booking-log.md).

Nothing here changes the engine.  The ledger it builds is the same object the
simulator builds; the engine cannot tell the two apart, which is the point.
"""
import csv
import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .hotelconfig import HotelConfig

STATUSES = ("booked", "in_house", "stayed", "cancelled", "no_show")
TARGETS = ("RETAIL", "OTA", "CORP", "GROUP", "NONREV")
MAX_ERRORS = 50


class IngestError(ValueError):
    pass


@dataclass
class Booking:
    booking_id: str
    booked_on: dt.date
    arrival: dt.date
    nights: int
    rooms: int
    rate: float
    currency: str
    segment: str
    rate_code: str
    source: str
    room_type: str
    company: str
    status: str
    status_date: Optional[dt.date]
    updated_on: Optional[dt.date]
    row: int
    target: Optional[str] = None
    imputed_cancel: bool = False

    @property
    def occupies(self) -> bool:
        return self.status in ("stayed", "in_house")

    @property
    def departure(self) -> dt.date:
        return self.arrival + dt.timedelta(days=self.nights)


@dataclass
class Report:
    errors: List[Tuple[int, str, str]] = field(default_factory=list)
    unmapped: Counter = field(default_factory=Counter)
    warnings: Counter = field(default_factory=Counter)
    notes: List[str] = field(default_factory=list)

    def error(self, row: int, column: str, message: str) -> None:
        if len(self.errors) < MAX_ERRORS:
            self.errors.append((row, column, message))
        self.warnings["_errors_total"] += 1

    def fail_if_errors(self) -> None:
        if not self.errors and not self.unmapped:
            return
        lines = ["%d row errors (showing up to %d):" % (self.warnings["_errors_total"], MAX_ERRORS)]
        lines += ["  row %d, %s: %s" % e for e in self.errors]
        if self.unmapped:
            lines.append("unmapped segment values (value: rows):")
            lines += ["  %r: %d" % (v, n) for v, n in self.unmapped.most_common()]
        raise IngestError("\n".join(lines))


def _date(value: str) -> Optional[dt.date]:
    value = (value or "").strip()
    return dt.date.fromisoformat(value) if value else None


def read_bookings(path: str, cfg: HotelConfig) -> Tuple[List[Booking], Report]:
    rep = Report()
    out: List[Booking] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = set(reader.fieldnames or [])
        for need in ("booking_id", "booked_on", "arrival", "segment", "status"):
            if need not in cols:
                rep.error(1, need, "required column missing")
        if not ({"nights", "departure"} & cols):
            rep.error(1, "nights", "need nights or departure")
        if not ({"rate", "total_revenue"} & cols):
            rep.error(1, "rate", "need rate or total_revenue")
        if rep.errors:
            return out, rep
        for i, r in enumerate(reader, start=2):
            b = _parse_row(i, r, cfg, rep)
            if b is None:
                continue
            n = b.rooms
            if n == 1:
                out.append(b)
            else:
                for k in range(1, n + 1):
                    copy = Booking(**{**b.__dict__, "booking_id": "%s#%d" % (b.booking_id, k), "rooms": 1})
                    out.append(copy)
    return out, rep


def _parse_row(i: int, r: dict, cfg: HotelConfig, rep: Report) -> Optional[Booking]:
    ok = True

    def bad(col, msg):
        nonlocal ok
        ok = False
        rep.error(i, col, msg)

    dates = {}
    bad_dates = set()
    for col in ("booked_on", "arrival", "departure", "status_date", "updated_on"):
        try:
            dates[col] = _date(r.get(col, ""))
        except ValueError:
            bad(col, "not a date, want YYYY-MM-DD")
            bad_dates.add(col)
            dates[col] = None
    if dates["booked_on"] is None and "booked_on" not in bad_dates:
        bad("booked_on", "empty")
    if dates["arrival"] is None and "arrival" not in bad_dates:
        bad("arrival", "empty")

    nights = None
    if (r.get("nights") or "").strip():
        try:
            nights = int(r["nights"])
            if nights < 0:
                bad("nights", "negative")
        except ValueError:
            bad("nights", "not an integer")
    elif dates.get("departure") and dates["arrival"]:
        nights = (dates["departure"] - dates["arrival"]).days
        if nights < 0:
            bad("departure", "before arrival")
    else:
        bad("nights", "need nights or departure")

    rooms = 1
    if (r.get("rooms") or "").strip():
        try:
            rooms = int(r["rooms"])
            if rooms < 1:
                bad("rooms", "must be at least 1")
        except ValueError:
            bad("rooms", "not an integer")

    rate = None
    if (r.get("rate") or "").strip():
        try:
            rate = float(r["rate"])
        except ValueError:
            bad("rate", "not a number")
    elif (r.get("total_revenue") or "").strip():
        try:
            total = float(r["total_revenue"])
            rate = total / nights if nights else 0.0
        except ValueError:
            bad("total_revenue", "not a number")
    else:
        bad("rate", "need rate or total_revenue")

    currency = (r.get("currency") or cfg.currency).strip().upper()
    if rate is not None and currency != cfg.currency.upper():
        fx = cfg.fx.get(currency)
        if fx is None:
            bad("currency", "%s has no fx rate in hotel.json" % currency)
        else:
            rate = rate * fx

    status = (r.get("status") or "").strip().lower()
    if status not in STATUSES:
        bad("status", "unknown status %r; want one of %s" % (status, "/".join(STATUSES)))

    if not ok:
        return None

    status_date = dates["status_date"]
    if status == "cancelled":
        if status_date is None:
            rep.warnings["cancelled_without_date"] += 1
        elif status_date > dates["arrival"]:
            status_date = dates["arrival"]; rep.warnings["cancel_after_arrival"] += 1
        elif status_date < dates["booked_on"]:
            status_date = dates["booked_on"]; rep.warnings["cancel_before_booking"] += 1
    elif status == "no_show":
        status_date = dates["arrival"]
    if rate is not None:
        if rate <= 0:
            rep.warnings["rate_nonpositive"] += 1
        elif not (cfg.rate_floor <= rate <= cfg.rate_ceiling):
            rep.warnings["rate_out_of_range"] += 1

    return Booking(
        booking_id=(r.get("booking_id") or "").strip(), booked_on=dates["booked_on"], arrival=dates["arrival"],
        nights=nights, rooms=rooms, rate=float(rate), currency=cfg.currency.upper(),
        segment=(r.get("segment") or "").strip(), rate_code=(r.get("rate_code") or "").strip(),
        source=(r.get("source") or "").strip(), room_type=(r.get("room_type") or "").strip(),
        company=(r.get("company") or "").strip(), status=status, status_date=status_date,
        updated_on=dates["updated_on"], row=i,
    )
