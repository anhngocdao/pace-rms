"""Real booking history into the engine's ledger (spec: docs/booking-log.md).

Nothing here changes the engine.  The ledger it builds is the same object the
simulator builds; the engine cannot tell the two apart, which is the point.
"""
import csv
import datetime as dt
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

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
    """Read a booking log, returning the rows that parsed and a Report of the rest.

    Row numbers are the file's own physical line numbers (csv's line_num), not
    a count of yielded records, so a blank line or a quoted multi-line field
    never desyncs every error after it from the line a human would count.
    """
    rep = Report()
    parsed: List[Booking] = []
    seen_ids = set()
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
            return [], rep
        for r in reader:
            i = reader.line_num
            b = _parse_row(i, r, cfg, rep)
            if b is None:
                continue
            if b.booking_id in seen_ids:
                rep.error(i, "booking_id", "duplicate booking_id")
                continue
            seen_ids.add(b.booking_id)
            parsed.append(b)

    # Expansion happens after every literal id in the file is known, so a
    # synthesised "#N" id can be checked against ids the file already uses.
    out: List[Booking] = []
    for b in parsed:
        if b.rooms == 1:
            out.append(b)
            continue
        for k in range(1, b.rooms + 1):
            new_id = "%s#%d" % (b.booking_id, k)
            if new_id in seen_ids:
                rep.error(b.row, "booking_id",
                          "expanded id %r collides with an existing booking_id" % new_id)
                continue
            seen_ids.add(new_id)
            out.append(Booking(**{**b.__dict__, "booking_id": new_id, "rooms": 1}))
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

    has_nights = bool((r.get("nights") or "").strip())
    has_departure = dates.get("departure") is not None

    nights = None
    if has_nights:
        try:
            nights = int(r["nights"])
            if nights < 0:
                bad("nights", "negative")
        except ValueError:
            bad("nights", "not an integer")

    if has_departure and dates["arrival"] is not None:
        from_departure = (dates["departure"] - dates["arrival"]).days
        if from_departure < 0:
            bad("departure", "before arrival")
        elif has_nights:
            if nights is not None and nights != from_departure:
                bad("nights", "nights=%s disagrees with departure, which implies %d nights"
                    % (r["nights"], from_departure))
        else:
            nights = from_departure

    if not has_nights and not has_departure:
        bad("nights", "need nights or departure")

    rooms = 1
    if (r.get("rooms") or "").strip():
        try:
            rooms = int(r["rooms"])
            if rooms < 1:
                bad("rooms", "must be at least 1")
        except ValueError:
            bad("rooms", "not an integer")

    has_rate = bool((r.get("rate") or "").strip())
    has_total_revenue = bool((r.get("total_revenue") or "").strip())

    rate = None
    if has_rate:
        try:
            rate = float(r["rate"])
        except ValueError:
            bad("rate", "not a number")

    if has_total_revenue:
        try:
            total_revenue = float(r["total_revenue"])
        except ValueError:
            bad("total_revenue", "not a number")
            total_revenue = None
        if total_revenue is not None:
            if has_rate:
                # A zero-night day-use row has no per-night total to check
                # rate against, so both being present just keeps rate as given.
                if rate is not None and nights and nights >= 1:
                    implied = total_revenue / nights
                    if abs(rate - implied) > 0.01:
                        bad("rate", "rate=%s disagrees with total_revenue/nights=%.2f"
                            % (r["rate"], implied))
            else:
                rate = total_revenue / nights if nights else 0.0

    if not has_rate and not has_total_revenue:
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


def _lookup(table: Dict[str, str], value: str) -> Optional[str]:
    if value in table:
        return table[value]
    for key, target in table.items():
        if key.endswith("*") and value.startswith(key[:-1]):
            return target
    return None


def map_segments(bookings: List[Booking], cfg: HotelConfig, rep: Report) -> None:
    """segment_map_order decides which columns are tried; a row falls through to
    the next key only when its value is empty or absent from that key's table.
    No match anywhere is an error, grouped by value."""
    for b in bookings:
        target = None
        tried = []
        for key in cfg.segment_map_order:
            value = getattr(b, key, "")
            if not value:
                continue
            tried.append("%s=%s" % (key, value))
            target = _lookup(cfg.segment_map.get(key, {}), value)
            if target is not None:
                break
        if target is None:
            rep.unmapped[tried[0] if tried else "(all mapping columns empty)"] += 1
            continue
        if target not in TARGETS:
            raise IngestError("hotel.json maps to unknown target %r" % target)
        b.target = target


def detect_groups(bookings: List[Booking], cfg: HotelConfig, rep: Report) -> int:
    """Same company, booked on the same day, same arrival and nights, reaching
    the threshold: one decision, many rooms.  Runs after mapping and never
    touches NONREV rows.  Off when the converter already decided groups."""
    if not cfg.detect_groups:
        return 0
    clusters: Dict[tuple, List[Booking]] = defaultdict(list)
    for b in bookings:
        if b.target in (None, "NONREV", "GROUP") or not b.company:
            continue
        clusters[(b.company, b.booked_on, b.arrival, b.nights)].append(b)
    changed = 0
    for members in clusters.values():
        if len(members) >= cfg.group_threshold_rooms:
            for b in members:
                b.target = "GROUP"
                changed += 1
    if changed:
        rep.notes.append("%d rows recognised as group rooms by company, booking day, arrival and nights" % changed)
    return changed


def impute_cancel_dates(bookings: List[Booking], rep: Report, seed: int) -> int:
    """Draw the ratio (days to cancel / lead) from dated cancellations of the same
    segment and apply it to this row's own lead, so short leads never get long
    delays that then clamp to arrival.  updated_on caps the result if present."""
    rng = random.Random(seed)
    ratios: Dict[str, List[float]] = defaultdict(list)
    for b in bookings:
        if b.status == "cancelled" and b.status_date is not None and b.target:
            lead = (b.arrival - b.booked_on).days
            if lead > 0:
                ratios[b.target].append(min(1.0, max(0.0, (b.status_date - b.booked_on).days / lead)))
    done = 0
    missing_segments = set()
    for b in bookings:
        if b.status != "cancelled" or b.status_date is not None:
            continue
        lead = (b.arrival - b.booked_on).days
        pool = ratios.get(b.target or "", [])
        if pool:
            when = b.booked_on + dt.timedelta(days=round(rng.choice(pool) * lead))
        else:
            when = b.booked_on
            missing_segments.add(b.target or "(unmapped)")
        upper = b.updated_on if b.updated_on is not None else b.arrival
        b.status_date = min(max(when, b.booked_on), upper)
        b.imputed_cancel = True
        done += 1
    if done:
        rep.notes.append("%d cancellations had no date; dates imputed from same-segment ratios, seed %d" % (done, seed))
    for seg in sorted(missing_segments):
        rep.notes.append("segment %s had no dated cancellations to learn from; its undated rows use the low bound (booking day)" % seg)
    return done


def cancel_bounds(bookings: List[Booking]) -> Tuple[List[Booking], List[Booking]]:
    """Two copies for the report: imputed rows at their low bound and at their high bound."""
    low, high = [], []
    for b in bookings:
        if b.imputed_cancel:
            low.append(Booking(**{**b.__dict__, "status_date": b.booked_on}))
            high.append(Booking(**{**b.__dict__, "status_date": b.arrival}))
        else:
            low.append(b); high.append(b)
    return low, high


@dataclass
class RoomInference:
    rooms: int
    peak_night: Optional[dt.date]
    nights_within_2pct: int
    second_highest: int
    per_year_max: Dict[int, int]


def physical_occupancy(bookings: List[Booking]) -> Dict[dt.date, int]:
    """Rooms physically occupied per night: stayed and in_house rows, NONREV
    included, no_show and day use excluded.  Reads the records, not the ledger,
    so NONREV rooms are counted."""
    occ: Dict[dt.date, int] = defaultdict(int)
    for b in bookings:
        if not b.occupies or b.nights == 0:
            continue
        for k in range(b.nights):
            occ[b.arrival + dt.timedelta(days=k)] += 1
    return occ


def infer_sellable_rooms(bookings: List[Booking]) -> RoomInference:
    occ = physical_occupancy(bookings)
    if not occ:
        return RoomInference(0, None, 0, 0, {})
    peak_night = max(occ, key=lambda d: (occ[d], d))
    rooms = occ[peak_night]
    counts = sorted(occ.values(), reverse=True)
    within = sum(1 for c in counts if c >= rooms * 0.98)
    second = counts[1] if len(counts) > 1 else rooms
    per_year: Dict[int, int] = {}
    for d, c in occ.items():
        per_year[d.year] = max(per_year.get(d.year, 0), c)
    return RoomInference(rooms, peak_night, within, second, per_year)
