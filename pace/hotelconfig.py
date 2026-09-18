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
    "name", "currency", "sellable_rooms", "rates_include_tax", "group_threshold_rooms",
    "detect_groups", "rate_floor", "rate_ceiling", "rate_step", "base_rate", "variable_cost",
    "max_lead", "max_los", "sellout_threshold", "price_month_factor", "demand_season_band",
    "segment_rate_ratio", "segment_commission", "events", "segment_map_order", "segment_map",
)


class ConfigError(ValueError):
    pass


@dataclass
class HotelConfig:
    name: str
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
    max_lead: int
    max_los: int
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


def load_hotel_json(path: str) -> HotelConfig:
    """Load and validate a hotel.json. Every field in REQUIRED is mandatory:
    a real hotel must not run on Toronto's seasons, events, contract ratios
    or threshold by falling through to a default (ADR 0007)."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        raise ConfigError("hotel.json is missing: %s" % ", ".join(missing))
    raw["price_month_factor"] = _months(raw["price_month_factor"], "price_month_factor")
    raw["demand_season_band"] = _months(raw["demand_season_band"], "demand_season_band")
    for code in list(raw["segment_rate_ratio"]) + list(raw["segment_commission"]):
        if code not in config.DEFAULT_SEGMENTS:
            raise ConfigError("unknown segment %r in hotel.json" % code)
    known = {f for f in HotelConfig.__dataclass_fields__}
    extra = [k for k in raw if k not in known]
    if extra:
        raise ConfigError("unknown keys in hotel.json: %s" % ", ".join(extra))
    return HotelConfig(**raw)


def apply(cfg: HotelConfig) -> Hotel:
    """Point the engine at this hotel.  Call once per process before any fit."""
    C.set_seasonality(C.Seasonality(cfg.price_month_factor, cfg.demand_season_band))
    config.reset_segments()
    config.configure_segments(cfg.segment_rate_ratio, cfg.segment_commission)
    if cfg.sellable_rooms is None:
        raise ConfigError("sellable_rooms is still null; run the ingest inference first")
    return Hotel(
        name=cfg.name, currency=cfg.currency, rooms=int(cfg.sellable_rooms),
        base_rate=float(cfg.base_rate), rate_floor=float(cfg.rate_floor),
        rate_ceiling=float(cfg.rate_ceiling), rate_step=float(cfg.rate_step),
        variable_cost=float(cfg.variable_cost), max_lead=int(cfg.max_lead),
        max_los=int(cfg.max_los), sellout_threshold=float(cfg.sellout_threshold),
    )


def event_calendar(cfg: HotelConfig) -> EventCalendar:
    cal = EventCalendar()
    for e in cfg.events:
        cal.add(Event(e["name"], dt.date.fromisoformat(e["start"]), dt.date.fromisoformat(e["end"]),
                      float(e["multiplier"]), e.get("note", "")))
    return cal
