"""hotel.json: the facts about a hotel the engine must not assume (ADR 0007)."""
import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import calendar as C
from . import config
from .calendar import Event, EventCalendar
from .config import Hotel

REQUIRED = (
    "name", "city", "currency", "sellable_rooms", "rates_include_tax", "group_threshold_rooms",
    "detect_groups", "rate_floor", "rate_ceiling", "rate_step", "base_rate", "variable_cost",
    "walk_cost", "max_lead", "max_los", "max_overbook_pct", "sellout_threshold",
    "price_month_factor", "demand_season_band", "segment_rate_ratio", "segment_commission",
    "events", "segment_map_order", "segment_map",
)

# base_daily_demand is deliberately not here: it is read by the market
# generator alone and never by the engine, so an ingested hotel cannot be
# affected by the simulated hotel's value (ADR 0007).
CONTRACTED = ("CORP", "GROUP")


class ConfigError(ValueError):
    pass


@dataclass
class HotelConfig:
    name: str
    city: str
    currency: str
    sellable_rooms: Optional[int]
    rates_include_tax: str
    group_threshold_rooms: int
    detect_groups: bool
    rate_floor: float
    rate_ceiling: float
    rate_step: float
    base_rate: float
    variable_cost: float
    walk_cost: float
    max_lead: int
    max_los: int
    max_overbook_pct: float
    sellout_threshold: float
    price_month_factor: Dict[int, float]
    demand_season_band: Dict[int, str]
    segment_rate_ratio: Dict[str, float]
    segment_commission: Dict[str, float]
    events: List[dict]
    segment_map_order: List[str]
    segment_map: Dict[str, Dict[str, str]]
    fx: Dict[str, float] = field(default_factory=dict)


def _months(raw: dict, name: str) -> dict:
    try:
        out = {int(k): v for k, v in raw.items()}
    except (TypeError, ValueError, AttributeError):
        raise ConfigError("%s must map month numbers 1..12 to values" % name)
    if set(out) != set(range(1, 13)):
        raise ConfigError("%s must have all twelve months" % name)
    return out


def _check_segments(ratio, commission) -> None:
    """Both tables name segments the engine actually has, and every contracted
    segment states its own ratio: an absent CORP or GROUP would otherwise be
    priced at the simulated hotel's 0.82 and 0.70 without a word (ADR 0007)."""
    for table, name in ((ratio, "segment_rate_ratio"), (commission, "segment_commission")):
        if not isinstance(table, dict):
            raise ConfigError("%s must map segment codes to numbers" % name)
    for code in list(ratio) + list(commission):
        if code not in config.DEFAULT_SEGMENTS:
            raise ConfigError("unknown segment %r in hotel.json" % code)
    # The values too, and here rather than in configure_segments: that one
    # rebuilds SEGMENTS entry by entry, so a value it cannot read raises with
    # some of the table already replaced and the rest of the engine, including
    # seasonality, already pointed at this hotel.
    for table, name in ((ratio, "segment_rate_ratio"), (commission, "segment_commission")):
        for code, value in table.items():
            try:
                float(value)
            except (TypeError, ValueError):
                raise ConfigError("%s[%r] is %r, which is not a number" % (name, code, value))
    missing = [c for c in CONTRACTED if c not in ratio]
    if missing:
        raise ConfigError("segment_rate_ratio is missing %s; a contracted segment with no ratio of "
                          "its own would be priced at the simulated hotel's (ADR 0007)"
                          % ", ".join(missing))


def load_hotel_json(path: str) -> HotelConfig:
    """Load and validate a hotel.json. Every field in REQUIRED is mandatory:
    a real hotel must not run on Toronto's seasons, events, contract ratios
    or threshold by falling through to a default (ADR 0007)."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as exc:
        raise ConfigError("cannot read hotel.json at %s: %s" % (path, exc))
    except json.JSONDecodeError as exc:
        raise ConfigError("hotel.json at %s is not valid JSON: %s" % (path, exc))
    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        raise ConfigError("hotel.json is missing: %s" % ", ".join(missing))
    raw["price_month_factor"] = _months(raw["price_month_factor"], "price_month_factor")
    raw["demand_season_band"] = _months(raw["demand_season_band"], "demand_season_band")
    _check_segments(raw["segment_rate_ratio"], raw["segment_commission"])
    known = {f for f in HotelConfig.__dataclass_fields__}
    extra = [k for k in raw if k not in known]
    if extra:
        raise ConfigError("unknown keys in hotel.json: %s" % ", ".join(extra))
    return HotelConfig(**raw)


def apply(cfg: HotelConfig) -> Hotel:
    """Point the engine at this hotel.  Call once per process before any fit.

    Every field is checked and both objects are built before a single global
    is touched, so a config the engine refuses leaves seasonality and segments
    exactly as they were instead of half this hotel and half the simulated one.
    """
    if cfg.sellable_rooms is None:
        raise ConfigError("sellable_rooms is still null; run the ingest inference first")
    _check_segments(cfg.segment_rate_ratio, cfg.segment_commission)
    try:
        seasonality = C.Seasonality(cfg.price_month_factor, cfg.demand_season_band).validate()
    except (TypeError, ValueError) as exc:
        # TypeError as well: a month factor that is a string reaches validate()
        # as "x" <= 0, and a hotel is owed a sentence rather than a traceback.
        raise ConfigError("seasonality in hotel.json is unusable: %s" % exc)
    try:
        hotel = Hotel(
            name=cfg.name, city=cfg.city, currency=cfg.currency, rooms=int(cfg.sellable_rooms),
            base_rate=float(cfg.base_rate), rate_floor=float(cfg.rate_floor),
            rate_ceiling=float(cfg.rate_ceiling), rate_step=float(cfg.rate_step),
            variable_cost=float(cfg.variable_cost), walk_cost=float(cfg.walk_cost),
            max_lead=int(cfg.max_lead), max_los=int(cfg.max_los),
            max_overbook_pct=float(cfg.max_overbook_pct),
            sellout_threshold=float(cfg.sellout_threshold),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError("hotel.json has a field the engine cannot read: %s" % exc)
    C.set_seasonality(seasonality)
    config.reset_segments()
    config.configure_segments(cfg.segment_rate_ratio, cfg.segment_commission)
    return hotel


def event_calendar(cfg: HotelConfig) -> EventCalendar:
    cal = EventCalendar()
    for e in cfg.events:
        cal.add(Event(e["name"], dt.date.fromisoformat(e["start"]), dt.date.fromisoformat(e["end"]),
                      float(e["multiplier"]), e.get("note", "")))
    return cal
