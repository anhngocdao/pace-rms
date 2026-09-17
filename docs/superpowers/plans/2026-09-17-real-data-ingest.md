# Real-data ingest (phase 5a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Pace read a real booking log into its `Ledger` through a documented CSV schema, convert the public Antonio dataset into that schema with a data audit, and open the four configuration seams the engine needs to run on a hotel that is not the simulated one, all without moving the golden numbers.

**Architecture:** Four seams in existing modules (`calendar.py` seasonality object, `Hotel.sellout_threshold`, `config.configure_segments`, events already injectable) read from a new `pace/hotelconfig.py` that loads `hotel.json`. A new `pace/ingest.py` validates a booking log, maps segments, imputes missing cancel dates, and replays into `Ledger`, returning the ledger plus a NONREV record list and inference notes. A new `tools/convert_antonio.py` maps the public dataset into that log and prints the audit. Settings in section 9 of the spec land in `data/antonio/settings.json` and are committed before the dataset is downloaded. The pilot command (spec section 5) is a second plan that consumes `ingest.load()`.

**Tech Stack:** Python 3.9 standard library only (`csv`, `json`, `datetime`, `statistics`, `random`, `dataclasses`, `unittest`). No numpy, no pandas.

**Spec:** `docs/superpowers/specs/2026-09-17-real-data-ingest-and-pilot-design.md`. Read sections 3, 4 and 9 before any task; they are the source of every rule below.

## Global Constraints

- Standard library only; `tests/test_golden.py::test_stdlib_only` scans `pace/` and `plugins/`. Put `tools/` code that imports only stdlib too.
- Python 3.9: no `match`, no `X | Y` unions, no `tomllib`, no `str.removeprefix` reliance.
- No engine algorithm changes. Only the four seams named above; every seam keeps the current constant as its default on the simulation path.
- After every task: `python3 run.py test` green, and `python3 run.py build --quick` prints static 64.50, ladder 78.80, engine 82.98.
- Commit voice: one sentence with a verb saying what is now true (see `git log --oneline`). End every commit message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- No em dashes or en dashes anywhere (code comments, docs, messages).
- `hotels.csv` is downloaded only in Task 13, only after Elle says yes in chat, and never committed (`.gitignore`).
- Work on branch `ingest`. Never commit to `main`.
- Run tests with `python3 -m unittest tests.test_x -v` for one file, `python3 run.py test` for all.

## File Structure

- `docs/adr/0007-hotel-facts-are-configuration.md` (new): Proposed ADR for seasonality, events, segment ratios, sell-out threshold as per-hotel input.
- `pace/calendar.py` (modify): `Seasonality` dataclass, module-level `_ACTIVE`, `set_seasonality()`, `reset_seasonality()`, `toronto_seasonality()`; `month_factor()` and `season_band()` read `_ACTIVE`.
- `pace/config.py` (modify): `Hotel.sellout_threshold: float = 0.97`; `configure_segments(ratios, commissions)` and `reset_segments()`.
- `pace/unconstrain.py`, `pace/elasticity.py`, `pace/experiment.py` (modify one line each): `sellout_threshold` defaults to `hotel.sellout_threshold` when the argument is None.
- `pace/hotelconfig.py` (new): `HotelConfig` dataclass, `load_hotel_json(path) -> HotelConfig`, `apply(cfg) -> Hotel` (sets seasonality, segments, returns `Hotel`), `event_calendar(cfg) -> EventCalendar`.
- `pace/ingest.py` (new): `read_bookings`, `map_segments`, `detect_groups`, `impute_cancel_dates`, `infer_sellable_rooms`, `replay`, `load` (the one-call entry point), `IngestError`, `IngestResult`.
- `tools/__init__.py` (new, empty) and `tools/convert_antonio.py` (new): `convert(path, out_dir, settings)`, audit functions, `main()`.
- `docs/booking-log.md` (new): schema v0 for hotels.
- `data/sample-bookings.csv`, `data/sample-hotel.json` (new): 200-row hand-made sample plus config.
- `data/antonio/settings.json` (new): section 9 values. `data/antonio/.gitignore` ignores `hotels.csv` and generated outputs.
- `tests/test_calendar_config.py`, `tests/test_hotelconfig.py`, `tests/test_ingest.py`, `tests/test_convert_antonio.py`, `tests/fixtures/antonio_40.csv` (new).
- `README.md`, `EXTENDING.md`, `CLAUDE.md` (modify): point at the schema and the converter; document the new commands.

---

### Task 1: ADR 0007, Proposed

**Files:**
- Create: `docs/adr/0007-hotel-facts-are-configuration.md`

**Interfaces:** none.

- [ ] **Step 1: Write the ADR (under 40 lines)**

```markdown
# ADR 0007: Hotel facts are configuration, not constants
Status: Proposed · Date: 2026-09-17

## Context
The engine reads four things that are facts about a hotel from module constants tuned for the simulated Toronto property: the monthly seasonality table `MONTH_FACTOR` in `pace/calendar.py`, which feeds both `reference_rate` (the denominator of every price ratio, and the ladder prior in `pace/policy.py`) and `season_band`, the estimation cell of pace curves, unconstraining and elasticity; the annual event list `_ANNUAL_EVENTS`, which enters `reference_rate` through the event multiplier; the per-segment `rate_multiplier` and `commission` in `pace/config.py` (CORP 0.82, GROUP 0.70); and the `sellout_threshold` of 0.97 that `unconstrain.py`, `elasticity.py` and `experiment.py` take as a function default to decide which nights were censored. Run unchanged on a resort in the Algarve, the engine would normalise Algarve prices against Toronto's seasons, pool nights into Toronto's demand classes, and price contracts at Toronto's ratios.

## Decision
Each of the four becomes a per-hotel input carried in `hotel.json` and applied by `pace/hotelconfig.py` before the engine runs. Seasonality splits into two fields, because on a resort price and demand seasonality do not move together: `price_month_factor` for `reference_rate` and the ladder prior, `demand_season_band` for `demand_class`. The simulation path keeps the current constants as defaults, so no golden number moves; the ingest path makes every field mandatory, so a real hotel cannot run on Toronto's seasons unnoticed. No algorithm changes.

## Consequences
The engine can be pointed at a real hotel with its own seasons, events, contract ratios and threshold. The contracted-segment rate in the engine is `reference_rate`, which still multiplies by the month factor, so CORP and GROUP follow the season like a tour-operator contract with seasonal steps and unlike a flat corporate rate; a fixed ratio is then too low in high season and too high in low season for flat contracts. This record moves to Accepted only when the golden test passes with a `hotel.json` that spells out the Toronto table explicitly, proving the configuration path and not only the default path.

## Evidence
`pace/calendar.py` `MONTH_FACTOR` (mean 0.99 across twelve months, bands at 1.12 and 0.95 giving 4, 3 and 5 months). `pace/simulate.py` `reference_rate` and `quoted_rate`. `pace/config.py` `SEGMENTS`. The 0.97 default in the three fit functions. Golden numbers in `CLAUDE.md`.
```

- [ ] **Step 2: Check length and dashes**

Run: `wc -l docs/adr/0007-hotel-facts-are-configuration.md && grep -c $'—\|–' docs/adr/0007-hotel-facts-are-configuration.md`
Expected: under 40 lines, count 0.

- [ ] **Step 3: Commit**

```bash
git add docs/adr/0007-hotel-facts-are-configuration.md
git commit -m "Four facts about a hotel are named as configuration before any of them moves out of a constant

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Seasonality seam in calendar.py

**Files:**
- Modify: `pace/calendar.py` (around lines 13 to 45)
- Test: `tests/test_calendar_config.py`

**Interfaces:**
- Produces: `Seasonality(price_month_factor: Dict[int, float], demand_season_band: Dict[int, str])`, `toronto_seasonality() -> Seasonality`, `set_seasonality(s: Seasonality) -> None`, `reset_seasonality() -> None`, `active_seasonality() -> Seasonality`. `month_factor(d)` and `season_band(d)` keep their signatures and read the active object.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_calendar_config.py
import datetime as dt
import unittest

from pace import calendar as C


class SeasonalitySeam(unittest.TestCase):
    def tearDown(self):
        C.reset_seasonality()

    def test_default_is_the_toronto_table(self):
        self.assertEqual(C.month_factor(dt.date(2024, 7, 1)), 1.22)
        self.assertEqual(C.season_band(dt.date(2024, 7, 1)), "peak")
        self.assertEqual(C.season_band(dt.date(2024, 1, 1)), "trough")
        self.assertEqual(C.season_band(dt.date(2024, 4, 1)), "shoulder")

    def test_toronto_bands_are_four_three_five(self):
        s = C.toronto_seasonality()
        bands = list(s.demand_season_band.values())
        self.assertEqual((bands.count("peak"), bands.count("shoulder"), bands.count("trough")), (4, 3, 5))

    def test_price_and_demand_are_independent(self):
        flat_price = {m: 1.0 for m in range(1, 13)}
        bands = {m: ("peak" if m in (7, 8) else "trough") for m in range(1, 13)}
        C.set_seasonality(C.Seasonality(flat_price, bands))
        self.assertEqual(C.month_factor(dt.date(2024, 7, 1)), 1.0)
        self.assertEqual(C.season_band(dt.date(2024, 7, 1)), "peak")
        self.assertEqual(C.season_band(dt.date(2024, 4, 1)), "trough")
        self.assertEqual(C.demand_class(dt.date(2024, 4, 1)), ("trough", 0))

    def test_reset_restores_toronto(self):
        C.set_seasonality(C.Seasonality({m: 2.0 for m in range(1, 13)}, {m: "peak" for m in range(1, 13)}))
        C.reset_seasonality()
        self.assertEqual(C.month_factor(dt.date(2024, 1, 1)), 0.72)

    def test_rejects_incomplete_tables(self):
        with self.assertRaises(ValueError):
            C.Seasonality({1: 1.0}, {m: "peak" for m in range(1, 13)}).validate()
        with self.assertRaises(ValueError):
            C.Seasonality({m: 1.0 for m in range(1, 13)}, {m: "high" for m in range(1, 13)}).validate()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_calendar_config -v`
Expected: FAIL, `AttributeError: module 'pace.calendar' has no attribute 'reset_seasonality'`.

- [ ] **Step 3: Implement the seam**

Replace the block from `MONTH_FACTOR = {` through `def season_band` in `pace/calendar.py` with:

```python
MONTH_FACTOR = {
    1: 0.72, 2: 0.78, 3: 0.88, 4: 0.96, 5: 1.08, 6: 1.18,
    7: 1.22, 8: 1.20, 9: 1.14, 10: 1.02, 11: 0.90, 12: 0.82,
}

BANDS = ("peak", "shoulder", "trough")


def _band_from_factor(f: float) -> str:
    if f >= 1.12:
        return "peak"
    if f >= 0.95:
        return "shoulder"
    return "trough"


@dataclass(frozen=True)
class Seasonality:
    """What the engine knows about a hotel's year.

    price_month_factor scales the reference rate; demand_season_band picks the
    estimation cell.  They are two fields because on a resort price swings far
    more than occupancy (ADR 0007).
    """
    price_month_factor: Dict[int, float]
    demand_season_band: Dict[int, str]

    def validate(self) -> "Seasonality":
        months = set(range(1, 13))
        if set(self.price_month_factor) != months or set(self.demand_season_band) != months:
            raise ValueError("seasonality needs all twelve months")
        bad = [b for b in self.demand_season_band.values() if b not in BANDS]
        if bad:
            raise ValueError("unknown season band %r; use %s" % (bad[0], "/".join(BANDS)))
        if any(f <= 0 for f in self.price_month_factor.values()):
            raise ValueError("price month factors must be positive")
        return self


def toronto_seasonality() -> Seasonality:
    return Seasonality(dict(MONTH_FACTOR), {m: _band_from_factor(f) for m, f in MONTH_FACTOR.items()})


_ACTIVE = toronto_seasonality()


def set_seasonality(s: Seasonality) -> None:
    global _ACTIVE
    _ACTIVE = s.validate()


def reset_seasonality() -> None:
    global _ACTIVE
    _ACTIVE = toronto_seasonality()


def active_seasonality() -> Seasonality:
    return _ACTIVE


def month_factor(d: dt.date) -> float:
    return _ACTIVE.price_month_factor[d.month]


def season_band(d: dt.date) -> str:
    return _ACTIVE.demand_season_band[d.month]
```

Add `Dict` to the `typing` import at the top of the file.

- [ ] **Step 4: Run the new tests, the whole suite, and the quick build**

Run: `python3 -m unittest tests.test_calendar_config -v && python3 run.py test 2>&1 | tail -3 && python3 run.py build --quick | tail -4`
Expected: new tests PASS; suite OK; RevPAR 64.50 / 78.80 / 82.98.

- [ ] **Step 5: Commit**

```bash
git add pace/calendar.py tests/test_calendar_config.py
git commit -m "Seasonality is an object the engine reads, with Toronto's table as the thing it reads by default

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: sellout_threshold on Hotel, segment ratios configurable

**Files:**
- Modify: `pace/config.py` (Hotel dataclass around line 139; after `SEGMENTS` dict)
- Modify: `pace/unconstrain.py:106`, `pace/elasticity.py:76`, `pace/experiment.py:130`
- Test: `tests/test_hotelconfig.py` (first half)

**Interfaces:**
- Produces: `Hotel.sellout_threshold: float = 0.97`; `configure_segments(rate_ratios: Dict[str, float], commissions: Dict[str, float]) -> None` (updates `SEGMENTS` in place with `dataclasses.replace`); `reset_segments() -> None`; `DEFAULT_SEGMENTS` (frozen copy of the originals). The three fit functions accept `sellout_threshold: Optional[float] = None` and use `hotel.sellout_threshold` when None.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_hotelconfig.py
import unittest

from pace import config


class SegmentSeam(unittest.TestCase):
    def tearDown(self):
        config.reset_segments()

    def test_hotel_carries_the_sellout_threshold(self):
        self.assertEqual(config.Hotel().sellout_threshold, 0.97)
        self.assertEqual(config.Hotel(sellout_threshold=0.9).sellout_threshold, 0.9)

    def test_configure_segments_replaces_ratios_in_place(self):
        seg_dict = config.SEGMENTS
        config.configure_segments({"CORP": 0.75, "GROUP": 0.6}, {"OTA": 0.15})
        self.assertIs(config.SEGMENTS, seg_dict)
        self.assertEqual(config.SEGMENTS["CORP"].rate_multiplier, 0.75)
        self.assertEqual(config.SEGMENTS["GROUP"].rate_multiplier, 0.6)
        self.assertEqual(config.SEGMENTS["OTA"].commission, 0.15)
        self.assertEqual(config.SEGMENTS["RETAIL"].rate_multiplier, 1.0)

    def test_reset_restores_defaults(self):
        config.configure_segments({"CORP": 0.5}, {})
        config.reset_segments()
        self.assertEqual(config.SEGMENTS["CORP"].rate_multiplier, 0.82)
        self.assertEqual(config.SEGMENTS["GROUP"].rate_multiplier, 0.70)

    def test_unknown_segment_is_an_error(self):
        with self.assertRaises(KeyError):
            config.configure_segments({"NONREV": 0.0}, {})
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_hotelconfig -v`
Expected: FAIL on `sellout_threshold` and `configure_segments`.

- [ ] **Step 3: Implement**

In `pace/config.py`, add to `Hotel` after `max_overbook_pct`:

```python
    sellout_threshold: float = 0.97  # share of rooms sold that marks a night censored
```

After the `SEGMENTS` dict definition (after `SEGMENT_ORDER` if it is defined right there, otherwise directly after the dict), add:

```python
DEFAULT_SEGMENTS: Dict[str, Segment] = dict(SEGMENTS)


def configure_segments(rate_ratios: Dict[str, float], commissions: Dict[str, float]) -> None:
    """Replace per-segment contract ratios and commissions in place (ADR 0007).

    SEGMENTS is imported by name in several modules, so it must stay the same
    dict object; entries are swapped, the dict is not rebound."""
    import dataclasses
    for code, ratio in rate_ratios.items():
        SEGMENTS[code] = dataclasses.replace(SEGMENTS[code], rate_multiplier=float(ratio))
    for code, com in commissions.items():
        SEGMENTS[code] = dataclasses.replace(SEGMENTS[code], commission=float(com))


def reset_segments() -> None:
    SEGMENTS.clear()
    SEGMENTS.update(DEFAULT_SEGMENTS)
```

`KeyError` for an unknown code comes for free from `SEGMENTS[code]`.

In `pace/unconstrain.py` change the `class_demand` signature to `sellout_threshold: Optional[float] = None` and, as the first line of the body, `sellout_threshold = hotel.sellout_threshold if sellout_threshold is None else sellout_threshold`. Add `Optional` to its typing import. Do the same in `fit_segment` (`pace/elasticity.py:74-76`) and `experimental_rows` (`pace/experiment.py:128-130`). If `fit_all` passes a threshold through to `fit_segment`, leave it; if it does not pass one, nothing changes.

- [ ] **Step 4: Run the tests, suite and quick build**

Run: `python3 -m unittest tests.test_hotelconfig -v && python3 run.py test 2>&1 | tail -3 && python3 run.py build --quick | tail -4`
Expected: PASS; OK; 64.50 / 78.80 / 82.98.

- [ ] **Step 5: Commit**

```bash
git add pace/config.py pace/unconstrain.py pace/elasticity.py pace/experiment.py tests/test_hotelconfig.py
git commit -m "The sell-out threshold belongs to the hotel and the contract ratios can be set for one

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: hotel.json loader and the explicit-Toronto golden test

**Files:**
- Create: `pace/hotelconfig.py`
- Test: `tests/test_hotelconfig.py` (second half), `tests/test_golden.py` (one added test)
- Create: `tests/fixtures/toronto-hotel.json`

**Interfaces:**
- Produces: `HotelConfig` dataclass with fields `name, currency, fx: Dict[str,float], sellable_rooms: Optional[int], rates_include_tax: str, group_threshold_rooms: int, detect_groups: bool, rate_floor, rate_ceiling, rate_step, base_rate, variable_cost, max_lead, max_los, sellout_threshold, price_month_factor: Dict[int,float], demand_season_band: Dict[int,str], segment_rate_ratio: Dict[str,float], segment_commission: Dict[str,float], events: List[dict], segment_map_order: List[str], segment_map: Dict[str, Dict[str,str]]`.
- `load_hotel_json(path: str, strict: bool = True) -> HotelConfig`: strict means every field mandatory (ingest path); `strict=False` fills defaults from the Toronto constants (used only by the golden test).
- `apply(cfg: HotelConfig) -> Hotel`: calls `set_seasonality`, `configure_segments`, returns `Hotel(rooms=cfg.sellable_rooms, base_rate=..., rate_floor=..., rate_ceiling=..., rate_step=..., variable_cost=..., max_lead=..., max_los=..., sellout_threshold=..., currency=..., name=...)`.
- `event_calendar(cfg: HotelConfig) -> EventCalendar` from `events` entries `{"name","start","end","multiplier","note"}`.
- `ConfigError(ValueError)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hotelconfig.py`:

```python
import json
import os
import tempfile

from pace import calendar as C
from pace import hotelconfig as HC

TORONTO = {
    "name": "Hotel Aurora", "currency": "CAD", "fx": {},
    "sellable_rooms": 150, "rates_include_tax": "unknown",
    "group_threshold_rooms": 10, "detect_groups": True,
    "rate_floor": 109.0, "rate_ceiling": 469.0, "rate_step": 4.0, "base_rate": 189.0,
    "variable_cost": 31.0, "max_lead": 180, "max_los": 5, "sellout_threshold": 0.97,
    "price_month_factor": {str(m): f for m, f in C.MONTH_FACTOR.items()},
    "demand_season_band": {str(m): b for m, b in C.toronto_seasonality().demand_season_band.items()},
    "segment_rate_ratio": {"CORP": 0.82, "GROUP": 0.70},
    "segment_commission": {"RETAIL": 0.02, "OTA": 0.18, "CORP": 0.0, "GROUP": 0.0},
    "events": [],
    "segment_map_order": ["segment"],
    "segment_map": {"segment": {"RETAIL": "RETAIL"}},
}


def _write(d):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(d, fh)
    return path


class LoadHotelJson(unittest.TestCase):
    def tearDown(self):
        C.reset_seasonality()
        config.reset_segments()

    def test_strict_load_needs_every_field(self):
        d = dict(TORONTO); del d["price_month_factor"]
        with self.assertRaises(HC.ConfigError) as cm:
            HC.load_hotel_json(_write(d))
        self.assertIn("price_month_factor", str(cm.exception))

    def test_apply_returns_a_hotel_and_sets_the_seams(self):
        cfg = HC.load_hotel_json(_write(TORONTO))
        hotel = HC.apply(cfg)
        self.assertEqual(hotel.rooms, 150)
        self.assertEqual(hotel.sellout_threshold, 0.97)
        self.assertEqual(C.month_factor(dt.date(2024, 7, 1)), 1.22)
        self.assertEqual(config.SEGMENTS["OTA"].commission, 0.18)

    def test_month_keys_are_integers_after_load(self):
        cfg = HC.load_hotel_json(_write(TORONTO))
        self.assertEqual(set(cfg.price_month_factor), set(range(1, 13)))

    def test_events_build_a_calendar(self):
        d = dict(TORONTO); d["events"] = [{"name": "Fair", "start": "2024-08-16", "end": "2024-08-25", "multiplier": 1.2, "note": ""}]
        cal = HC.event_calendar(HC.load_hotel_json(_write(d)))
        self.assertAlmostEqual(cal.multiplier(dt.date(2024, 8, 20)), 1.2)
        self.assertAlmostEqual(cal.multiplier(dt.date(2024, 9, 1)), 1.0)
```

Add `import datetime as dt` at the top of the test file. Save `TORONTO` also as `tests/fixtures/toronto-hotel.json` (write it with `json.dump(TORONTO, fh, indent=2)` once, then keep the file; the segment commission for OTA must match the value in `pace/config.py`, read it there and correct the fixture if 0.18 is not the actual number).

Add to `tests/test_golden.py`:

```python
class ExplicitTorontoConfig(unittest.TestCase):
    """ADR 0007 moves to Accepted only when the configuration path reproduces
    the golden numbers, not just the default path."""

    def tearDown(self):
        from pace import calendar as C
        from pace import config
        C.reset_seasonality()
        config.reset_segments()

    def test_spelled_out_toronto_matches_quick_golden(self):
        from pace import hotelconfig as HC
        cfg = HC.load_hotel_json(os.path.join(ROOT, "tests", "fixtures", "toronto-hotel.json"))
        HC.apply(cfg)
        scores = _run(quick=True)["backtest"]["scores"]
        for policy, want in QUICK_REVPAR.items():
            self.assertAlmostEqual(scores[policy]["revpar"], want, delta=TOLERANCE)
```

Note: `_run` uses `S.HOTEL` from the scenario, not the returned `Hotel`; the test proves the seasonality and segment seams, and the Hotel fields in the fixture equal the scenario's, so the result must match exactly.

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_hotelconfig -v`
Expected: `ModuleNotFoundError: No module named 'pace.hotelconfig'`.

- [ ] **Step 3: Implement pace/hotelconfig.py**

```python
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


def load_hotel_json(path: str, strict: bool = True) -> HotelConfig:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if strict:
        missing = [k for k in REQUIRED if k not in raw]
        if missing:
            raise ConfigError("hotel.json is missing: %s" % ", ".join(missing))
    else:
        tor = C.toronto_seasonality()
        defaults = {
            "price_month_factor": tor.price_month_factor,
            "demand_season_band": tor.demand_season_band,
            "segment_rate_ratio": {c: s.rate_multiplier for c, s in config.DEFAULT_SEGMENTS.items()},
            "segment_commission": {c: s.commission for c, s in config.DEFAULT_SEGMENTS.items()},
            "events": [], "fx": {}, "detect_groups": True, "rates_include_tax": "unknown",
            "group_threshold_rooms": 10, "sellout_threshold": 0.97,
        }
        for k, v in defaults.items():
            raw.setdefault(k, v)
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
```

Check `Hotel` for fields not listed (`walk_cost`, `base_daily_demand`, `max_overbook_pct`, `city`): they keep their defaults; `base_daily_demand` is generator-only.

- [ ] **Step 4: Run tests, suite, quick build**

Run: `python3 -m unittest tests.test_hotelconfig tests.test_golden.ExplicitTorontoConfig -v && python3 run.py test 2>&1 | tail -3 && python3 run.py build --quick | tail -4`
Expected: PASS (the explicit-Toronto test takes about 14 s); OK; 64.50 / 78.80 / 82.98.

- [ ] **Step 5: Flip ADR 0007 to Accepted and commit**

Edit line 2 of `docs/adr/0007-hotel-facts-are-configuration.md` to `Status: Accepted · Date: 2026-09-17`.

```bash
git add pace/hotelconfig.py tests/test_hotelconfig.py tests/test_golden.py tests/fixtures/toronto-hotel.json docs/adr/0007-hotel-facts-are-configuration.md
git commit -m "A hotel.json that spells out Toronto reproduces the golden numbers, so the configuration path is the real one

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Booking-log schema document and sample file

**Files:**
- Create: `docs/booking-log.md`, `data/sample-bookings.csv`, `data/sample-hotel.json`, `data/.gitignore`

**Interfaces:** none (documents used by Tasks 6 to 9 as fixtures).

- [ ] **Step 1: Write docs/booking-log.md**

Copy spec section 3 (the two column tables, the `hotel.json` example, the mapping rules, the NONREV paragraph, the validation rules, the undated-cancellation rule, the sellable-rooms rule) into `docs/booking-log.md` under the title `# Booking log, schema version 0`, with this opening paragraph:

```markdown
This is the file to send a hotel when asking for data, and the only format
`pace/ingest.py` reads. Version 0 means the column names have not yet been
checked against a real Opera, ezCloud or Smile export; the first two real
exports will fix them. One row per room. Dates are YYYY-MM-DD. Rates are per
room night before tax unless `rates_include_tax` in hotel.json says otherwise.
```

Then the tables and rules, verbatim from the spec. End with a "Converters" section that names `tools/convert_antonio.py` as the worked example.

- [ ] **Step 2: Write data/sample-hotel.json**

```json
{
  "name": "Sample Hotel", "currency": "EUR", "fx": {"USD": 0.92},
  "sellable_rooms": 40, "rates_include_tax": "no",
  "group_threshold_rooms": 5, "detect_groups": true,
  "rate_floor": 60, "rate_ceiling": 400, "rate_step": 4, "base_rate": 140,
  "variable_cost": 18, "max_lead": 180, "max_los": 7, "sellout_threshold": 0.97,
  "price_month_factor": {"1": 0.8, "2": 0.8, "3": 0.9, "4": 1.0, "5": 1.05, "6": 1.15,
                         "7": 1.25, "8": 1.25, "9": 1.1, "10": 1.0, "11": 0.85, "12": 0.85},
  "demand_season_band": {"1": "trough", "2": "trough", "3": "trough", "4": "shoulder", "5": "shoulder",
                         "6": "peak", "7": "peak", "8": "peak", "9": "peak", "10": "shoulder", "11": "trough", "12": "trough"},
  "segment_rate_ratio": {"CORP": 0.8, "GROUP": 0.7},
  "segment_commission": {"RETAIL": 0.0, "OTA": 0.15, "CORP": 0.0, "GROUP": 0.0},
  "events": [],
  "segment_map_order": ["segment", "rate_code", "source"],
  "segment_map": {
    "segment": {"WEB": "RETAIL", "PHONE": "RETAIL", "BOOKING": "OTA", "EXPEDIA": "OTA",
                "CORP": "CORP", "GROUP": "GROUP", "COMP": "NONREV", "HOUSE": "NONREV"},
    "rate_code": {"BAR": "RETAIL", "CORP-*": "CORP", "TO-*": "CORP"},
    "source": {"Walk-in": "RETAIL", "Agoda": "OTA"}
  }
}
```

- [ ] **Step 3: Generate data/sample-bookings.csv with a small seeded script (run once, keep the CSV, do not keep the script)**

```python
import csv, datetime as dt, random
rng = random.Random(7)
rows = []
segs = ["WEB", "PHONE", "BOOKING", "EXPEDIA", "CORP", "DEFAULT", "GROUP", "COMP"]
for i in range(200):
    arrival = dt.date(2025, 1, 1) + dt.timedelta(days=rng.randint(0, 120))
    lead = rng.randint(0, 90)
    seg = rng.choice(segs)
    status = rng.choices(["stayed", "cancelled", "no_show", "in_house", "booked"], [70, 15, 3, 2, 10])[0]
    nights = rng.choice([0, 1, 1, 2, 2, 3, 4])
    rate = 0 if seg == "COMP" else round(rng.uniform(70, 260), 2)
    status_date = ""
    if status == "cancelled" and rng.random() < 0.8:
        status_date = (arrival - dt.timedelta(days=rng.randint(0, lead))).isoformat()
    rows.append(["S-%04d" % i, (arrival - dt.timedelta(days=lead)).isoformat(), arrival.isoformat(),
                 nights, 1 if seg != "GROUP" else rng.randint(5, 8), rate, "EUR", seg,
                 "BAR" if seg == "DEFAULT" and i % 2 else "TO-SUMMER" if seg == "DEFAULT" else "",
                 rng.choice(["Web", "Agoda", "Walk-in", ""]), rng.choice(["STD", "DLX"]),
                 "ACME" if seg in ("CORP", "GROUP") else "", status, status_date, ""])
with open("data/sample-bookings.csv", "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["booking_id", "booked_on", "arrival", "nights", "rooms", "rate", "currency", "segment",
                "rate_code", "source", "room_type", "company", "status", "status_date", "updated_on"])
    w.writerows(rows)
```

- [ ] **Step 4: data/.gitignore**

```
antonio/hotels.csv
antonio/*-bookings.csv
antonio/*-hotel.json
antonio/audit.md
```

- [ ] **Step 5: Commit**

```bash
git add docs/booking-log.md data/sample-bookings.csv data/sample-hotel.json data/.gitignore
git commit -m "The booking log has a written shape and a sample a hotel can compare its export against

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: ingest, reading and validating rows

**Files:**
- Create: `pace/ingest.py`
- Test: `tests/test_ingest.py`

**Interfaces:**
- Produces:

```python
@dataclass
class Booking:
    booking_id: str; booked_on: dt.date; arrival: dt.date; nights: int; rooms: int
    rate: float; currency: str; segment: str; rate_code: str; source: str
    room_type: str; company: str; status: str; status_date: Optional[dt.date]
    updated_on: Optional[dt.date]; row: int
    target: Optional[str] = None          # RETAIL/OTA/CORP/GROUP/NONREV after mapping
    imputed_cancel: bool = False

class IngestError(ValueError): ...

@dataclass
class Report:
    errors: List[Tuple[int, str, str]]    # (row, column, message), capped at 50
    unmapped: Counter                     # value -> rows
    warnings: Counter                     # code -> count
    notes: List[str]
    def fail_if_errors(self) -> None      # raises IngestError listing errors and unmapped values

STATUSES = ("booked", "in_house", "stayed", "cancelled", "no_show")
def read_bookings(path: str, cfg: HotelConfig) -> Tuple[List[Booking], Report]
```

Rules implemented here: exactly one of `nights`/`departure`; exactly one of `rate`/`total_revenue`; `rooms` >= 1 expands to that many rows (booking_id suffixed `#1`, `#2`...); currency conversion via `cfg.fx` else error `mixed currencies without fx`; status in STATUSES (`in_house` kept as its own value; the replay treats it as stayed); warnings `rate_out_of_range`, `rate_nonpositive`, `cancelled_without_date`; clamps `status_date` (after arrival for cancelled -> arrival, warning `cancel_after_arrival`; before booked_on -> booked_on, warning `cancel_before_booking`); `nights` >= 0; day use allowed.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ingest.py
import csv
import datetime as dt
import io
import os
import tempfile
import unittest

from pace import hotelconfig as HC
from pace import ingest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEADER = ["booking_id", "booked_on", "arrival", "nights", "rooms", "rate", "currency", "segment",
          "rate_code", "source", "room_type", "company", "status", "status_date", "updated_on"]


def _csv(rows, header=HEADER):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(header); w.writerows(rows)
    return path


def _cfg(**over):
    cfg = HC.load_hotel_json(os.path.join(ROOT, "data", "sample-hotel.json"))
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def _row(**kw):
    base = dict(booking_id="B1", booked_on="2025-01-01", arrival="2025-02-01", nights="2", rooms="1",
                rate="120", currency="EUR", segment="WEB", rate_code="", source="", room_type="STD",
                company="", status="stayed", status_date="", updated_on="")
    base.update(kw)
    return [base[h] for h in HEADER]


class ReadBookings(unittest.TestCase):
    def test_reads_a_clean_row(self):
        rows, rep = ingest.read_bookings(_csv([_row()]), _cfg())
        self.assertEqual(rep.errors, [])
        self.assertEqual(rows[0].nights, 2)
        self.assertEqual(rows[0].arrival, dt.date(2025, 2, 1))

    def test_departure_gives_nights(self):
        header = [h for h in HEADER if h != "nights"] + ["departure"]
        r = _row(); r = [v for h, v in zip(HEADER, r) if h != "nights"] + ["2025-02-04"]
        rows, rep = ingest.read_bookings(_csv([r], header), _cfg())
        self.assertEqual(rows[0].nights, 3)

    def test_total_revenue_gives_rate(self):
        header = [h for h in HEADER if h != "rate"] + ["total_revenue"]
        r = [v for h, v in zip(HEADER, _row()) if h != "rate"] + ["300"]
        rows, _ = ingest.read_bookings(_csv([r], header), _cfg())
        self.assertEqual(rows[0].rate, 150.0)

    def test_multi_room_rows_expand(self):
        rows, _ = ingest.read_bookings(_csv([_row(rooms="3")]), _cfg())
        self.assertEqual(len(rows), 3)
        self.assertEqual([b.booking_id for b in rows], ["B1#1", "B1#2", "B1#3"])
        self.assertTrue(all(b.rooms == 1 for b in rows))

    def test_day_use_is_allowed(self):
        rows, rep = ingest.read_bookings(_csv([_row(nights="0")]), _cfg())
        self.assertEqual(rep.errors, []); self.assertEqual(rows[0].nights, 0)

    def test_errors_are_collected_up_to_fifty(self):
        bad = [_row(booking_id="B%d" % i, arrival="not-a-date") for i in range(60)]
        _, rep = ingest.read_bookings(_csv(bad), _cfg())
        self.assertEqual(len(rep.errors), 50)
        self.assertEqual(rep.errors[0][1], "arrival")
        with self.assertRaises(ingest.IngestError):
            rep.fail_if_errors()

    def test_rate_outside_range_only_warns(self):
        _, rep = ingest.read_bookings(_csv([_row(rate="0"), _row(booking_id="B2", rate="900")]), _cfg())
        self.assertEqual(rep.errors, [])
        self.assertEqual(rep.warnings["rate_nonpositive"], 1)
        self.assertEqual(rep.warnings["rate_out_of_range"], 1)

    def test_mixed_currency_needs_fx(self):
        rows, rep = ingest.read_bookings(_csv([_row(), _row(booking_id="B2", currency="USD", rate="100")]), _cfg())
        self.assertEqual(rep.errors, []); self.assertEqual(rows[1].rate, 92.0)
        _, rep2 = ingest.read_bookings(_csv([_row(currency="GBP")]), _cfg(fx={}))
        self.assertEqual(rep2.errors[0][1], "currency")

    def test_cancel_dates_are_clamped_and_counted(self):
        rows, rep = ingest.read_bookings(_csv([
            _row(status="cancelled", status_date="2025-02-10"),
            _row(booking_id="B2", status="cancelled", status_date="2024-12-01"),
            _row(booking_id="B3", status="cancelled"),
        ]), _cfg())
        self.assertEqual(rows[0].status_date, dt.date(2025, 2, 1))
        self.assertEqual(rows[1].status_date, dt.date(2025, 1, 1))
        self.assertIsNone(rows[2].status_date)
        self.assertEqual(rep.warnings["cancel_after_arrival"], 1)
        self.assertEqual(rep.warnings["cancel_before_booking"], 1)
        self.assertEqual(rep.warnings["cancelled_without_date"], 1)

    def test_unknown_status_is_an_error(self):
        _, rep = ingest.read_bookings(_csv([_row(status="checked")]), _cfg())
        self.assertEqual(rep.errors[0][1], "status")
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_ingest -v`
Expected: `ModuleNotFoundError: No module named 'pace.ingest'`.

- [ ] **Step 3: Implement the reader**

```python
# pace/ingest.py
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

from .config import Hotel
from .hotelconfig import HotelConfig
from .ledger import Hold, Ledger

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
    for col in ("booked_on", "arrival", "departure", "status_date", "updated_on"):
        try:
            dates[col] = _date(r.get(col, ""))
        except ValueError:
            bad(col, "not a date, want YYYY-MM-DD")
            dates[col] = None
    if dates["booked_on"] is None:
        bad("booked_on", "empty")
    if dates["arrival"] is None:
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
```

- [ ] **Step 4: Run tests and the suite**

Run: `python3 -m unittest tests.test_ingest -v && python3 run.py test 2>&1 | tail -3`
Expected: all PASS; suite OK (stdlib scan includes the new module).

- [ ] **Step 5: Commit**

```bash
git add pace/ingest.py tests/test_ingest.py
git commit -m "A booking log is read row by row, and every bad row is reported before anything stops

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: ingest, segment mapping and group detection

**Files:**
- Modify: `pace/ingest.py`
- Test: `tests/test_ingest.py`

**Interfaces:**
- Produces: `map_segments(bookings, cfg, rep) -> None` (sets `b.target`; unmapped values counted in `rep.unmapped`, target stays None); `detect_groups(bookings, cfg, rep) -> int` (returns rows turned into GROUP; skips NONREV; needs `cfg.detect_groups`).

- [ ] **Step 1: Write the failing tests**

```python
class MapSegments(unittest.TestCase):
    def _mapped(self, *rows, **over):
        bookings, rep = ingest.read_bookings(_csv(list(rows)), _cfg(**over))
        ingest.map_segments(bookings, _cfg(**over), rep)
        return bookings, rep

    def test_first_key_wins_when_present(self):
        b, _ = self._mapped(_row(segment="BOOKING", rate_code="BAR"))
        self.assertEqual(b[0].target, "OTA")

    def test_default_value_falls_through_to_rate_code(self):
        b, _ = self._mapped(_row(segment="DEFAULT", rate_code="BAR"))
        self.assertEqual(b[0].target, "RETAIL")

    def test_prefix_needs_the_star(self):
        b, _ = self._mapped(_row(segment="", rate_code="CORP-ACME"))
        self.assertEqual(b[0].target, "CORP")
        b2, rep2 = self._mapped(_row(segment="", rate_code="BARX"))
        self.assertIsNone(b2[0].target); self.assertEqual(rep2.unmapped["rate_code=BARX"], 1)

    def test_source_is_the_last_key(self):
        b, _ = self._mapped(_row(segment="", rate_code="", source="Agoda"))
        self.assertEqual(b[0].target, "OTA")

    def test_unmapped_values_are_grouped_not_listed_per_row(self):
        rows = [_row(booking_id="B%d" % i, segment="NEWCODE") for i in range(120)]
        _, rep = self._mapped(*rows)
        self.assertEqual(rep.unmapped["segment=NEWCODE"], 120)
        self.assertEqual(rep.errors, [])
        with self.assertRaises(ingest.IngestError) as cm:
            rep.fail_if_errors()
        self.assertIn("NEWCODE", str(cm.exception))

    def test_order_is_configurable(self):
        b, _ = self._mapped(_row(segment="BOOKING", rate_code="BAR"), segment_map_order=["rate_code", "segment"])
        self.assertEqual(b[0].target, "RETAIL")


class DetectGroups(unittest.TestCase):
    def _run(self, rows, **over):
        cfg = _cfg(**over)
        bookings, rep = ingest.read_bookings(_csv(rows), cfg)
        ingest.map_segments(bookings, cfg, rep)
        n = ingest.detect_groups(bookings, cfg, rep)
        return bookings, n

    def test_same_company_day_arrival_nights_at_threshold_is_a_group(self):
        rows = [_row(booking_id="B%d" % i, segment="CORP", company="ACME") for i in range(5)]
        b, n = self._run(rows)
        self.assertEqual(n, 5); self.assertTrue(all(x.target == "GROUP" for x in b))

    def test_company_alone_is_not_a_group(self):
        rows = [_row(booking_id="B%d" % i, segment="CORP", company="ACME", arrival="2025-02-%02d" % (i + 1)) for i in range(5)]
        b, n = self._run(rows)
        self.assertEqual(n, 0); self.assertTrue(all(x.target == "CORP" for x in b))

    def test_different_booking_days_do_not_cluster(self):
        rows = [_row(booking_id="B%d" % i, segment="BOOKING", company="AGENT9", booked_on="2025-01-%02d" % (i + 1)) for i in range(6)]
        _, n = self._run(rows)
        self.assertEqual(n, 0)

    def test_nonrev_rows_stay_nonrev(self):
        rows = [_row(booking_id="B%d" % i, segment="COMP", company="ACME", rate="0") for i in range(5)]
        b, n = self._run(rows)
        self.assertEqual(n, 0); self.assertTrue(all(x.target == "NONREV" for x in b))

    def test_empty_company_never_matches(self):
        rows = [_row(booking_id="B%d" % i, segment="WEB", company="") for i in range(8)]
        _, n = self._run(rows)
        self.assertEqual(n, 0)

    def test_detect_groups_can_be_switched_off(self):
        rows = [_row(booking_id="B%d" % i, segment="CORP", company="ACME") for i in range(5)]
        _, n = self._run(rows, detect_groups=False)
        self.assertEqual(n, 0)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_ingest.MapSegments tests.test_ingest.DetectGroups -v`
Expected: `AttributeError: module 'pace.ingest' has no attribute 'map_segments'`.

- [ ] **Step 3: Implement**

Append to `pace/ingest.py`:

```python
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
```

- [ ] **Step 4: Run tests and suite**

Run: `python3 -m unittest tests.test_ingest -v && python3 run.py test 2>&1 | tail -3`
Expected: PASS; OK.

- [ ] **Step 5: Commit**

```bash
git add pace/ingest.py tests/test_ingest.py
git commit -m "A hotel's own codes reach the four segments through an ordered table, and a group is one decision on one day

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: ingest, undated cancellations and sellable-room inference

**Files:**
- Modify: `pace/ingest.py`
- Test: `tests/test_ingest.py`

**Interfaces:**
- Produces: `impute_cancel_dates(bookings, rep, seed: int) -> int` (rows imputed; sets `status_date` and `imputed_cancel=True`; ratio drawn from dated cancellations of the same `target`; upper bound `updated_on` if present else `arrival`; segment with no dated cancellations uses the low bound, booked_on, and a note); `cancel_bounds(bookings) -> Tuple[List[Booking], List[Booking]]` returning two shallow-copied lists where imputed rows are set to booked_on (low) and arrival (high) for the report's sensitivity bounds; `infer_sellable_rooms(bookings) -> RoomInference` with fields `rooms: int, nights_within_2pct: int, second_highest: int, per_year_max: Dict[int,int], peak_night: dt.date`; physical occupancy counts `occupies` rows including NONREV, excludes `nights == 0`.

- [ ] **Step 1: Write the failing tests**

```python
class ImputeCancelDates(unittest.TestCase):
    def _bookings(self):
        rows = [_row(booking_id="C%d" % i, segment="WEB", status="cancelled", booked_on="2025-01-01",
                     arrival="2025-01-21", status_date="2025-01-%02d" % (6 + i)) for i in range(5)]   # ratio 0.25..0.45
        rows.append(_row(booking_id="U1", segment="WEB", status="cancelled", booked_on="2025-03-01", arrival="2025-03-11"))
        rows.append(_row(booking_id="U2", segment="WEB", status="cancelled", booked_on="2025-03-01", arrival="2025-03-31", updated_on="2025-03-05"))
        rows.append(_row(booking_id="U3", segment="CORP", status="cancelled", booked_on="2025-03-01", arrival="2025-03-11"))
        cfg = _cfg(); b, rep = ingest.read_bookings(_csv(rows), cfg); ingest.map_segments(b, cfg, rep)
        return b, rep

    def test_ratio_is_applied_to_the_rows_own_lead(self):
        b, rep = self._bookings()
        n = ingest.impute_cancel_dates(b, rep, seed=1)
        u1 = next(x for x in b if x.booking_id == "U1")
        self.assertEqual(n, 3); self.assertTrue(u1.imputed_cancel)
        self.assertTrue(dt.date(2025, 3, 3) <= u1.status_date <= dt.date(2025, 3, 6))   # 10-day lead times 0.25..0.45

    def test_updated_on_is_the_upper_bound(self):
        b, rep = self._bookings()
        ingest.impute_cancel_dates(b, rep, seed=1)
        u2 = next(x for x in b if x.booking_id == "U2")
        self.assertLessEqual(u2.status_date, dt.date(2025, 3, 5))

    def test_segment_without_dated_cancellations_uses_low_bound(self):
        b, rep = self._bookings()
        ingest.impute_cancel_dates(b, rep, seed=1)
        u3 = next(x for x in b if x.booking_id == "U3")
        self.assertEqual(u3.status_date, dt.date(2025, 3, 1))
        self.assertTrue(any("CORP" in n for n in rep.notes))

    def test_same_seed_same_dates(self):
        a, ra = self._bookings(); ingest.impute_cancel_dates(a, ra, seed=5)
        c, rc = self._bookings(); ingest.impute_cancel_dates(c, rc, seed=5)
        self.assertEqual([x.status_date for x in a], [x.status_date for x in c])

    def test_bounds_put_imputed_rows_at_booking_day_and_arrival(self):
        b, rep = self._bookings(); ingest.impute_cancel_dates(b, rep, seed=1)
        low, high = ingest.cancel_bounds(b)
        u1_low = next(x for x in low if x.booking_id == "U1"); u1_high = next(x for x in high if x.booking_id == "U1")
        self.assertEqual(u1_low.status_date, dt.date(2025, 3, 1)); self.assertEqual(u1_high.status_date, dt.date(2025, 3, 11))


class InferSellableRooms(unittest.TestCase):
    def test_counts_stayed_and_in_house_including_nonrev_not_no_show_or_day_use(self):
        rows = [_row(booking_id="S%d" % i, segment="WEB", arrival="2025-02-01", nights="1") for i in range(10)]
        rows += [_row(booking_id="N1", segment="COMP", rate="0", arrival="2025-02-01", nights="1")]
        rows += [_row(booking_id="I1", segment="WEB", arrival="2025-02-01", nights="1", status="in_house")]
        rows += [_row(booking_id="X1", segment="WEB", arrival="2025-02-01", nights="1", status="no_show")]
        rows += [_row(booking_id="D1", segment="WEB", arrival="2025-02-01", nights="0")]
        rows += [_row(booking_id="T%d" % i, segment="WEB", arrival="2026-02-01", nights="1") for i in range(12)]
        cfg = _cfg(); b, rep = ingest.read_bookings(_csv(rows), cfg); ingest.map_segments(b, cfg, rep)
        inf = ingest.infer_sellable_rooms(b)
        self.assertEqual(inf.rooms, 12)
        self.assertEqual(inf.second_highest, 12)   # 2025-02-01 has 10 + 1 comp + 1 in_house
        self.assertEqual(inf.per_year_max, {2025: 12, 2026: 12})
        self.assertEqual(inf.nights_within_2pct, 2)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_ingest.ImputeCancelDates tests.test_ingest.InferSellableRooms -v`
Expected: `AttributeError` on `impute_cancel_dates`.

- [ ] **Step 3: Implement**

Append to `pace/ingest.py`:

```python
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
```

- [ ] **Step 4: Run tests and suite**

Run: `python3 -m unittest tests.test_ingest -v && python3 run.py test 2>&1 | tail -3`
Expected: PASS; OK.

- [ ] **Step 5: Commit**

```bash
git add pace/ingest.py tests/test_ingest.py
git commit -m "A cancellation without a date gets one from its own segment's habits, and the room count is read off the busiest real night

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: ingest, replay into the ledger and the one-call entry point

**Files:**
- Modify: `pace/ingest.py`
- Test: `tests/test_ingest.py`

**Interfaces:**
- Produces:

```python
@dataclass
class IngestResult:
    ledger: Ledger
    hotel: Hotel
    cfg: HotelConfig
    bookings: List[Booking]          # all rows, mapped
    nonrev: List[Booking]            # NONREV rows, with their booked_on
    inference: Optional[RoomInference]   # None when sellable_rooms came from hotel.json
    report: Report
    first_stay: dt.date
    last_stay: dt.date

def nonrev_on(nonrev: List[Booking], night: dt.date, asof: Optional[dt.date] = None) -> int
    # NONREV rooms occupying `night`; with asof, only rows booked on or before asof
def replay(bookings, hotel, first_stay, last_stay, rep) -> Ledger
def load(csv_path: str, hotel_json_path: str, seed: int = 20250115,
         first_stay: Optional[dt.date] = None, last_stay: Optional[dt.date] = None) -> IngestResult
```

Replay rules: walk days from min(booked_on) to last_stay; book rows with `booked_on == day` and `target != NONREV` via `ledger.book(day, req, rate)` where `req` is a tiny object with `rid, segment, rooms, arrival, los`; for `no_show` rows set `hold.no_show = True` so `settle()` releases them on arrival (they stay on the books until then, as in a real PMS); cancel rows whose `status_date == day` (`cancelled` only) via `ledger.cancel(hold)`; `booked` rows (still open at export) are booked and never settled beyond `last_stay`; `in_house` treated as stayed. Before `settle(night)`, if `ledger.rooms_on(night) > hotel.rooms` count warning `over_capacity_nights` (settle will walk the excess; the report must know). Call `ledger.snapshot(day, hotel.max_lead)` every day and `settle` for every night `<= day` not yet settled and within `[first_stay, last_stay]`.

`load()` order: `load_hotel_json(strict=True)` then read, map, detect_groups, impute, `fail_if_errors`, infer rooms if `cfg.sellable_rooms is None` (set `cfg.sellable_rooms = inference.rooms`, add note with ceiling check), `apply(cfg)` for the `Hotel`, default `first_stay = min(arrival)`, `last_stay = max(departure) - 1 day`, replay, return.

- [ ] **Step 1: Write the failing tests**

```python
class Replay(unittest.TestCase):
    def _load(self, rows, **over):
        cfg_path = os.path.join(ROOT, "data", "sample-hotel.json")
        if over:
            import json
            with open(cfg_path) as fh: d = json.load(fh)
            d.update(over)
            fd, cfg_path = tempfile.mkstemp(suffix=".json")
            with os.fdopen(fd, "w") as fh: json.dump(d, fh)
        return ingest.load(_csv(rows), cfg_path, seed=1)

    def tearDown(self):
        from pace import calendar as C
        from pace import config
        C.reset_seasonality(); config.reset_segments()

    def test_paid_rooms_plus_nonrev_equals_stayed_rows_per_night(self):
        rows = [_row(booking_id="S%d" % i, segment="WEB", arrival="2025-02-01", nights="2") for i in range(6)]
        rows += [_row(booking_id="N%d" % i, segment="COMP", rate="0", arrival="2025-02-01", nights="1") for i in range(2)]
        res = self._load(rows)
        night = dt.date(2025, 2, 1)
        self.assertEqual(res.ledger.rooms_on(night), 6)
        self.assertEqual(ingest.nonrev_on(res.nonrev, night), 2)
        self.assertEqual(res.ledger.rooms_on(night) + ingest.nonrev_on(res.nonrev, night), 8)
        self.assertEqual(res.ledger.rooms_on(dt.date(2025, 2, 2)), 6)
        self.assertNotIn("NONREV", res.ledger.segment_mix(night))

    def test_snapshots_follow_booking_and_cancel_dates(self):
        rows = [_row(booking_id="A", segment="WEB", booked_on="2025-01-02", arrival="2025-02-01", nights="1"),
                _row(booking_id="B", segment="WEB", booked_on="2025-01-25", arrival="2025-02-01", nights="1"),
                _row(booking_id="C", segment="WEB", booked_on="2025-01-02", arrival="2025-02-01", nights="1",
                     status="cancelled", status_date="2025-01-20")]
        res = self._load(rows)
        night = dt.date(2025, 2, 1)
        self.assertEqual(res.ledger.otb_at(night, 30), 2)   # 2025-01-02: A and C
        self.assertEqual(res.ledger.otb_at(night, 7), 2)    # 2025-01-25: A and B, C cancelled on the 20th
        self.assertEqual(res.ledger.otb_at(night, 0), 2)

    def test_no_show_stays_on_the_books_until_arrival_then_drops(self):
        rows = [_row(booking_id="X", segment="WEB", booked_on="2025-01-02", arrival="2025-02-01", nights="1", status="no_show")]
        res = self._load(rows)
        night = dt.date(2025, 2, 1)
        self.assertEqual(res.ledger.otb_at(night, 7), 1)
        self.assertEqual(res.ledger.settled[night]["rooms_sold"], 0)

    def test_revenue_by_segment_uses_converted_rate(self):
        rows = [_row(booking_id="U", segment="BOOKING", currency="USD", rate="100", arrival="2025-02-01", nights="1")]
        res = self._load(rows)
        self.assertAlmostEqual(res.ledger.seg_revenue[dt.date(2025, 2, 1)]["OTA"], 92.0)

    def test_inference_fills_null_rooms_and_notes_the_ceiling(self):
        rows = [_row(booking_id="S%d" % i, segment="WEB", arrival="2025-02-01", nights="1") for i in range(7)]
        res = self._load(rows, sellable_rooms=None)
        self.assertEqual(res.hotel.rooms, 7); self.assertIsNotNone(res.inference)
        self.assertTrue(any("inferred" in n for n in res.report.notes))

    def test_over_capacity_is_a_warning_not_a_stop(self):
        rows = [_row(booking_id="S%d" % i, segment="WEB", arrival="2025-02-01", nights="1") for i in range(45)]
        res = self._load(rows)   # sample hotel has 40 rooms
        self.assertEqual(res.report.warnings["over_capacity_nights"], 1)

    def test_rows_beyond_max_lead_or_max_los_are_kept_whole(self):
        rows = [_row(booking_id="L", segment="WEB", booked_on="2024-01-01", arrival="2025-02-01", nights="12")]
        res = self._load(rows)   # max_lead 180, max_los 7
        self.assertEqual(res.ledger.rooms_on(dt.date(2025, 2, 12)), 1)

    def test_load_stops_on_errors_with_all_of_them_listed(self):
        rows = [_row(booking_id="B%d" % i, status="bogus") for i in range(3)] + [_row(booking_id="Z", segment="ZZZ")]
        with self.assertRaises(ingest.IngestError) as cm:
            self._load(rows)
        self.assertIn("3 row errors", str(cm.exception)); self.assertIn("ZZZ", str(cm.exception))

    def test_sample_file_loads(self):
        res = ingest.load(os.path.join(ROOT, "data", "sample-bookings.csv"), os.path.join(ROOT, "data", "sample-hotel.json"))
        self.assertGreater(res.ledger.n_bookings, 100)
```

If `test_sample_file_loads` fails because the generated sample contains an unmapped value or a rate outside the sample floor and ceiling, fix the sample generator in Task 5, regenerate, and amend that commit's file; the sample must load cleanly.

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_ingest.Replay -v`
Expected: `AttributeError` on `load`.

- [ ] **Step 3: Implement**

Append to `pace/ingest.py`:

```python
from . import hotelconfig as HC   # move to the import block at the top


class _Req:
    """The five fields Ledger.book reads off a request."""
    __slots__ = ("rid", "segment", "rooms", "arrival", "los")

    def __init__(self, rid, segment, rooms, arrival, los):
        self.rid, self.segment, self.rooms, self.arrival, self.los = rid, segment, rooms, arrival, los


def nonrev_on(nonrev: List[Booking], night: dt.date, asof: Optional[dt.date] = None) -> int:
    n = 0
    for b in nonrev:
        if not b.occupies or b.nights == 0:
            continue
        if asof is not None and b.booked_on > asof:
            continue
        if b.arrival <= night < b.departure:
            n += 1
    return n


def replay(bookings: List[Booking], hotel: Hotel, first_stay: dt.date, last_stay: dt.date,
           rep: Report) -> Ledger:
    ledger = Ledger(hotel, first_stay, last_stay)
    by_day: Dict[dt.date, List[Booking]] = defaultdict(list)
    cancels: Dict[dt.date, List[Booking]] = defaultdict(list)
    for b in bookings:
        if b.target in (None, "NONREV") or b.nights == 0:
            continue
        by_day[b.booked_on].append(b)
        if b.status == "cancelled" and b.status_date is not None:
            cancels[b.status_date].append(b)
    holds: Dict[str, Hold] = {}
    day = min(by_day) if by_day else first_stay
    settled_upto = first_stay - dt.timedelta(days=1)
    rid = 0
    while day <= last_stay:
        for b in by_day.get(day, ()):
            rid += 1
            hold = ledger.book(day, _Req(rid, b.target, 1, b.arrival, b.nights), b.rate)
            if b.status == "no_show":
                hold.no_show = True
            holds[b.booking_id] = hold
        for b in cancels.get(day, ()):
            hold = holds.pop(b.booking_id, None)
            if hold is not None:
                ledger.cancel(hold)
        ledger.snapshot(day, hotel.max_lead)
        while settled_upto < day and settled_upto < last_stay:
            night = settled_upto + dt.timedelta(days=1)
            if night >= first_stay:
                if ledger.rooms_on(night) > hotel.rooms:
                    rep.warnings["over_capacity_nights"] += 1
                ledger.settle(night)
            settled_upto = night
        day += dt.timedelta(days=1)
    return ledger


@dataclass
class IngestResult:
    ledger: Ledger
    hotel: Hotel
    cfg: HC.HotelConfig
    bookings: List[Booking]
    nonrev: List[Booking]
    inference: Optional[RoomInference]
    report: Report
    first_stay: dt.date
    last_stay: dt.date


def load(csv_path: str, hotel_json_path: str, seed: int = 20250115,
         first_stay: Optional[dt.date] = None, last_stay: Optional[dt.date] = None) -> IngestResult:
    cfg = HC.load_hotel_json(hotel_json_path, strict=True)
    bookings, rep = read_bookings(csv_path, cfg)
    map_segments(bookings, cfg, rep)
    detect_groups(bookings, cfg, rep)
    impute_cancel_dates(bookings, rep, seed)
    rep.fail_if_errors()
    inference = None
    if cfg.sellable_rooms is None:
        inference = infer_sellable_rooms(bookings)
        cfg.sellable_rooms = inference.rooms
        rep.notes.append(
            "sellable_rooms inferred as %d from the busiest night (%s); %d nights within 2%% of it, "
            "second highest %d; yearly maxima %s. Inferred counts are biased low."
            % (inference.rooms, inference.peak_night, inference.nights_within_2pct,
               inference.second_highest, inference.per_year_max))
    hotel = HC.apply(cfg)
    nonrev = [b for b in bookings if b.target == "NONREV"]
    if first_stay is None:
        first_stay = min(b.arrival for b in bookings)
    if last_stay is None:
        last_stay = max(b.departure for b in bookings) - dt.timedelta(days=1)
    ledger = replay(bookings, hotel, first_stay, last_stay, rep)
    return IngestResult(ledger, hotel, cfg, bookings, nonrev, inference, rep, first_stay, last_stay)
```

Note on `Hold`: it is a plain `@dataclass` (not frozen, no slots), so `hold.no_show = True` works; `settle()` already reads it with `getattr`.

- [ ] **Step 4: Run tests, suite and quick build**

Run: `python3 -m unittest tests.test_ingest -v && python3 run.py test 2>&1 | tail -3 && python3 run.py build --quick | tail -4`
Expected: PASS; OK; 64.50 / 78.80 / 82.98.

- [ ] **Step 5: Commit**

```bash
git add pace/ingest.py tests/test_ingest.py
git commit -m "A real booking log becomes the same ledger the simulator builds, with comp rooms counted beside it rather than inside it

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: converter, row mapping and rule order

**Files:**
- Create: `tools/__init__.py` (empty), `tools/convert_antonio.py`
- Test: `tests/test_convert_antonio.py`

**Interfaces:**
- Produces (all in `tools/convert_antonio.py`):

```python
MONTHS = {"January": 1, ..., "December": 12}
BRANCHES = ("COMP", "ADR0", "TP_CLUSTER", "GROUPS", "OFFLINE_TO_GROUP", "OFFLINE_TO_CONTRACT",
            "OFFLINE_TO_TRANSIENT", "DIRECT", "ONLINE_TA", "CORPORATE", "AVIATION",
            "UNDEFINED_CH_DIRECT", "UNDEFINED_CH_CORPORATE", "UNDEFINED_CH_GDS",
            "UNDEFINED_CH_TATO", "UNDEFINED_FALLBACK")
def arrival_of(r: dict) -> dt.date
def status_of(r: dict) -> Tuple[str, str]            # (status, status_date iso or "")
def branch_rows(rows: List[dict], settings: dict) -> Tuple[List[dict], Counter, List[str]]
    # each returned dict is one booking-log row (schema columns) plus "_branch" and "_raw" (the source row)
def cluster_transient_party(rows: List[dict], threshold: int) -> set   # indices that became TP_CLUSTER
```

Rule order inside `branch_rows` (spec section 4): 1 Complementary -> COMP; 2 groups: `market_segment == Groups` -> GROUPS, `Offline TA/TO` with `customer_type == Group` -> OFFLINE_TO_GROUP, Transient-party clusters at or above threshold -> TP_CLUSTER; 3 `adr == 0`, `nights > 0`, status in (stayed, no_show), branch not a group branch -> ADR0; 4 the table. Undefined: branch `UNDEFINED_CH_<channel>` where channel is `Direct`, `Corporate`, `GDS`, `TA/TO`; channel Undefined -> `UNDEFINED_FALLBACK` plus a warning count. Cluster key: (hotel, market_segment, arrival, nights, lead_time, agent, company) where `agent`/`company` "NULL" or "" count as empty; the pair may not be empty on both sides; empty matches empty when the other field is present and equal.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_convert_antonio.py
import datetime as dt
import unittest
from collections import Counter

from tools import convert_antonio as CA

SETTINGS = {"group_threshold_rooms": 10, "undefined_stop_share": 0.01, "seed": 20250115}


def _arow(**kw):
    base = dict(hotel="Resort Hotel", is_canceled="0", lead_time="30", arrival_date_year="2016",
                arrival_date_month="July", arrival_date_week_number="28", arrival_date_day_of_month="10",
                stays_in_weekend_nights="1", stays_in_week_nights="2", adults="2", children="0", babies="0",
                meal="BB", country="PRT", market_segment="Direct", distribution_channel="Direct",
                is_repeated_guest="0", previous_cancellations="0", previous_bookings_not_canceled="0",
                reserved_room_type="A", assigned_room_type="A", booking_changes="0", deposit_type="No Deposit",
                agent="NULL", company="NULL", days_in_waiting_list="0", customer_type="Transient", adr="120.0",
                required_car_parking_spaces="0", total_of_special_requests="0",
                reservation_status="Check-Out", reservation_status_date="2016-07-13")
    base.update(kw)
    return base


class RowBasics(unittest.TestCase):
    def test_arrival_and_booked_on(self):
        out, _, _ = CA.branch_rows([_arow()], SETTINGS)
        self.assertEqual(out[0]["arrival"], "2016-07-10")
        self.assertEqual(out[0]["booked_on"], "2016-06-10")
        self.assertEqual(out[0]["nights"], "3")
        self.assertEqual(out[0]["booking_id"], "H1-000001")

    def test_status_comes_from_reservation_status_not_is_canceled(self):
        self.assertEqual(CA.status_of(_arow(reservation_status="No-Show", is_canceled="1")), ("no_show", ""))
        self.assertEqual(CA.status_of(_arow(reservation_status="Canceled", is_canceled="1",
                                            reservation_status_date="2016-06-20")), ("cancelled", "2016-06-20"))
        self.assertEqual(CA.status_of(_arow()), ("stayed", ""))

    def test_day_use_kept(self):
        out, _, _ = CA.branch_rows([_arow(stays_in_weekend_nights="0", stays_in_week_nights="0")], SETTINGS)
        self.assertEqual(out[0]["nights"], "0")

    def test_city_hotel_code_and_duplicates_kept(self):
        out, _, _ = CA.branch_rows([_arow(hotel="City Hotel"), _arow(hotel="City Hotel")], SETTINGS)
        self.assertEqual([o["booking_id"] for o in out], ["H2-000001", "H2-000002"])


class RuleOrder(unittest.TestCase):
    def _branches(self, rows):
        out, counts, _ = CA.branch_rows(rows, SETTINGS)
        return [o["_branch"] for o in out], counts

    def test_complementary_is_comp_even_with_positive_adr(self):
        b, _ = self._branches([_arow(market_segment="Complementary", adr="50")])
        self.assertEqual(b, ["COMP"])

    def test_groups_and_offline_group_are_group_branches(self):
        b, _ = self._branches([_arow(market_segment="Groups"), _arow(market_segment="Offline TA/TO", customer_type="Group")])
        self.assertEqual(b, ["GROUPS", "OFFLINE_TO_GROUP"])

    def test_adr_zero_on_group_member_keeps_group(self):
        b, _ = self._branches([_arow(market_segment="Groups", adr="0")])
        self.assertEqual(b, ["GROUPS"])

    def test_adr_zero_on_stayed_direct_is_adr0_but_not_on_cancelled(self):
        b, _ = self._branches([_arow(adr="0"), _arow(adr="0", reservation_status="Canceled", is_canceled="1")])
        self.assertEqual(b, ["ADR0", "DIRECT"])

    def test_adr_zero_day_use_is_not_adr0(self):
        b, _ = self._branches([_arow(adr="0", stays_in_weekend_nights="0", stays_in_week_nights="0")])
        self.assertEqual(b, ["DIRECT"])

    def test_table_branches(self):
        rows = [_arow(market_segment="Online TA"), _arow(market_segment="Corporate"), _arow(market_segment="Aviation"),
                _arow(market_segment="Offline TA/TO", customer_type="Contract"),
                _arow(market_segment="Offline TA/TO", customer_type="Transient")]
        b, _ = self._branches(rows)
        self.assertEqual(b, ["ONLINE_TA", "CORPORATE", "AVIATION", "OFFLINE_TO_CONTRACT", "OFFLINE_TO_TRANSIENT"])

    def test_undefined_goes_by_channel(self):
        rows = [_arow(market_segment="Undefined", distribution_channel="TA/TO"),
                _arow(market_segment="Undefined", distribution_channel="Undefined")]
        b, counts = self._branches(rows)
        self.assertEqual(b, ["UNDEFINED_CH_TATO", "UNDEFINED_FALLBACK"])
        self.assertEqual(counts["UNDEFINED_FALLBACK"], 1)

    def test_undefined_over_one_percent_of_room_nights_stops(self):
        rows = [_arow(market_segment="Undefined") for _ in range(5)] + [_arow() for _ in range(5)]
        with self.assertRaises(CA.ConvertStop):
            CA.branch_rows(rows, SETTINGS)


class TransientParty(unittest.TestCase):
    def _tp(self, n, **kw):
        return [_arow(customer_type="Transient-party", market_segment="Offline TA/TO", **kw) for _ in range(n)]

    def test_cluster_at_threshold_becomes_group(self):
        out, _, _ = CA.branch_rows(self._tp(10, agent="9", company="NULL"), SETTINGS)
        self.assertTrue(all(o["_branch"] == "TP_CLUSTER" for o in out))

    def test_below_threshold_is_treated_as_transient(self):
        out, _, _ = CA.branch_rows(self._tp(9, agent="9"), SETTINGS)
        self.assertTrue(all(o["_branch"] == "OFFLINE_TO_TRANSIENT" for o in out))

    def test_agent_present_company_empty_clusters(self):
        rows = self._tp(10, agent="9", company="NULL")
        self.assertEqual(len(CA.cluster_transient_party(rows, 10)), 10)

    def test_both_empty_never_clusters(self):
        rows = self._tp(10, agent="NULL", company="NULL")
        self.assertEqual(CA.cluster_transient_party(rows, 10), set())

    def test_different_lead_or_segment_splits_the_cluster(self):
        rows = self._tp(6, agent="9") + self._tp(6, agent="9", lead_time="31")
        self.assertEqual(CA.cluster_transient_party(rows, 10), set())
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_convert_antonio -v`
Expected: `ModuleNotFoundError: No module named 'tools'` (create `tools/__init__.py` first if the error is about the package, then the error moves to `convert_antonio`).

- [ ] **Step 3: Implement**

```python
# tools/convert_antonio.py
"""Antonio, de Almeida and Nunes (2019) hotel booking demand data into Pace's
booking log.  Two Portuguese hotels, arrivals 1 July 2015 to 31 August 2017,
CC BY 4.0.  The file itself is never committed.

Duplicated rows are kept on purpose: the data is anonymised, so an identical
row cannot be told apart from a distinct room.  The audit prints the room
count with and without them.
"""
import csv
import datetime as dt
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Tuple

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}
HOTEL_CODE = {"Resort Hotel": "H1", "City Hotel": "H2"}
GROUP_BRANCHES = ("GROUPS", "OFFLINE_TO_GROUP", "TP_CLUSTER")
CHANNEL_BRANCH = {"Direct": "UNDEFINED_CH_DIRECT", "Corporate": "UNDEFINED_CH_CORPORATE",
                  "GDS": "UNDEFINED_CH_GDS", "TA/TO": "UNDEFINED_CH_TATO"}
OUT_COLUMNS = ["booking_id", "booked_on", "arrival", "nights", "rooms", "rate", "currency", "segment",
               "rate_code", "source", "room_type", "company", "status", "status_date", "updated_on"]


class ConvertStop(RuntimeError):
    """The data needs a human decision before conversion may continue."""


def _empty(v: str) -> bool:
    return v is None or v.strip() in ("", "NULL", "NA")


def arrival_of(r: dict) -> dt.date:
    return dt.date(int(r["arrival_date_year"]), MONTHS[r["arrival_date_month"]], int(r["arrival_date_day_of_month"]))


def nights_of(r: dict) -> int:
    return int(r["stays_in_weekend_nights"]) + int(r["stays_in_week_nights"])


def status_of(r: dict) -> Tuple[str, str]:
    s = r["reservation_status"]
    if s == "Check-Out":
        return "stayed", ""
    if s == "No-Show":
        return "no_show", ""
    if s == "Canceled":
        return "cancelled", r["reservation_status_date"]
    raise ValueError("unknown reservation_status %r" % s)


def cluster_transient_party(rows: List[dict], threshold: int) -> set:
    groups: Dict[tuple, List[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        if r["customer_type"] != "Transient-party":
            continue
        agent = "" if _empty(r["agent"]) else r["agent"].strip()
        company = "" if _empty(r["company"]) else r["company"].strip()
        if not agent and not company:
            continue
        key = (r["hotel"], r["market_segment"], arrival_of(r), nights_of(r), int(r["lead_time"]), agent, company)
        groups[key].append(i)
    return {i for members in groups.values() if len(members) >= threshold for i in members}


def _table_branch(r: dict) -> str:
    seg, ct = r["market_segment"], r["customer_type"]
    if seg == "Direct":
        return "DIRECT"
    if seg == "Online TA":
        return "ONLINE_TA"
    if seg == "Corporate":
        return "CORPORATE"
    if seg == "Aviation":
        return "AVIATION"
    if seg == "Offline TA/TO":
        return "OFFLINE_TO_CONTRACT" if ct == "Contract" else "OFFLINE_TO_TRANSIENT"
    if seg == "Undefined":
        return CHANNEL_BRANCH.get(r["distribution_channel"], "UNDEFINED_FALLBACK")
    raise ValueError("unknown market_segment %r" % seg)


def branch_rows(rows: List[dict], settings: dict) -> Tuple[List[dict], Counter, List[str]]:
    """Apply the rules in the spec's order and write one booking-log row per input row."""
    clustered = cluster_transient_party(rows, int(settings["group_threshold_rooms"]))
    out: List[dict] = []
    counts: Counter = Counter()
    notes: List[str] = []
    per_hotel = Counter()
    undefined_nights = Counter()
    total_nights = Counter()
    for i, r in enumerate(rows):
        seg, ct = r["market_segment"], r["customer_type"]
        status, status_date = status_of(r)
        nights = nights_of(r)
        adr = float(r["adr"])
        if seg == "Complementary":
            branch = "COMP"
        elif seg == "Groups":
            branch = "GROUPS"
        elif seg == "Offline TA/TO" and ct == "Group":
            branch = "OFFLINE_TO_GROUP"
        elif i in clustered:
            branch = "TP_CLUSTER"
        elif adr == 0 and nights > 0 and status in ("stayed", "no_show"):
            branch = "ADR0"
        else:
            branch = _table_branch(r)
        counts[branch] += 1
        code = HOTEL_CODE[r["hotel"]]
        per_hotel[code] += 1
        if status == "stayed":
            total_nights[code] += nights
            if seg == "Undefined":
                undefined_nights[code] += nights
        arrival = arrival_of(r)
        agent = "" if _empty(r["agent"]) else r["agent"].strip()
        company = "" if _empty(r["company"]) else r["company"].strip()
        out.append({
            "booking_id": "%s-%06d" % (code, per_hotel[code]),
            "booked_on": (arrival - dt.timedelta(days=int(r["lead_time"]))).isoformat(),
            "arrival": arrival.isoformat(), "nights": str(nights), "rooms": "1",
            "rate": r["adr"], "currency": "EUR", "segment": branch, "rate_code": "",
            "source": r["distribution_channel"], "room_type": r["reserved_room_type"],
            "company": company or agent, "status": status, "status_date": status_date, "updated_on": "",
            "_branch": branch, "_raw": r,
        })
    for code in total_nights:
        share = undefined_nights[code] / total_nights[code] if total_nights[code] else 0.0
        if share > float(settings["undefined_stop_share"]):
            raise ConvertStop("%s: Undefined market segment is %.1f%% of stayed room nights; decide the mapping by hand"
                              % (code, 100 * share))
        notes.append("%s: Undefined share of stayed room nights %.3f%%" % (code, 100 * share))
    if counts["UNDEFINED_FALLBACK"]:
        notes.append("%d rows had Undefined segment and Undefined channel; mapped to RETAIL with a warning" % counts["UNDEFINED_FALLBACK"])
    return out, counts, notes
```

Note: the `undefined_stop_share` check counts stayed room nights per hotel, so the five-Undefined test above trips it (50 percent). With 5 Undefined of 10 rows the test expects `ConvertStop`; keep the fixture rows all `Check-Out`.

- [ ] **Step 4: Run tests and suite**

Run: `python3 -m unittest tests.test_convert_antonio -v && python3 run.py test 2>&1 | tail -3`
Expected: PASS; OK. (`test_stdlib_only` only scans `pace/` and `plugins/`; `tools/` imports stdlib anyway.)

- [ ] **Step 5: Commit**

```bash
git add tools/__init__.py tools/convert_antonio.py tests/test_convert_antonio.py
git commit -m "Every row of the public dataset gets the name of the rule that placed it, in the order the rules were agreed

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: converter, price basket, derived hotel.json and audit

**Files:**
- Modify: `tools/convert_antonio.py`
- Test: `tests/test_convert_antonio.py`

**Interfaces:**
- Produces:

```python
WARMUP = (dt.date(2015, 7, 1), dt.date(2016, 6, 30))        # first twelve settled months, by arrival
EXCLUDED_WEEKS = [(dt.date(2016, 3, 21), dt.date(2016, 3, 27)), (dt.date(2015, 12, 24), dt.date(2016, 1, 1))]
def basket(out_rows, branches: set, window=WARMUP, settings=None, min_rows=None, exclude_weeks=True) -> Tuple[List[dict], bool]
    # rows: stayed, branch in branches, adr>0, adr inside p1..p99 of the candidate set, most common room type,
    # most common meal, adults == 2, children == 0 and babies == 0; returns (rows, widened) where widened
    # is True when the adults filter had to be dropped to reach min_rows
def monthly_median(rows) -> Dict[int, float]
def price_month_factor(out_rows, settings) -> Tuple[Dict[int, float], List[int]]   # factors (mean 1.0), widened months
def base_rate(out_rows, factors, settings) -> float
def floor_ceiling(out_rows, settings) -> Tuple[float, float]
def rate_step(floor, ceiling) -> float
def demand_season_band(out_rows) -> Tuple[Dict[int, str], Dict[int, str], Dict[int, float]]  # (bands from gross, bands from occupancy, gross by month)
def segment_rate_ratio(out_rows, settings) -> Tuple[Dict[str, float], Dict[str, float], Dict[str, Dict[str, float]]]
    # (ratios for CORP and GROUP, per-origin ratios, ratio by season band)
def bar_test(out_rows, settings) -> dict   # {"residual_corr_month":..., "residual_corr_2wk":..., "weeks":..., "verdict": "OTA"/"CORP"/"inconclusive"}
def derive_hotel_json(out_rows, hotel_code, settings) -> dict
def audit(out_rows, hotel_code, settings, derived) -> str    # markdown
def convert(csv_path, out_dir, settings_path) -> None       # writes h1/h2 bookings, hotel.json, audit.md
```

Branch sets: `BAR_BRANCHES = {"DIRECT", "ONLINE_TA"}`, `CORP_BRANCHES = {"CORPORATE", "AVIATION", "OFFLINE_TO_CONTRACT", "OFFLINE_TO_TRANSIENT"}`, `GROUP_BRANCHES` as above. Target map written into `hotel.json.segment_map.segment`: `COMP, ADR0 -> NONREV`; `GROUPS, OFFLINE_TO_GROUP, TP_CLUSTER -> GROUP`; `CORPORATE, AVIATION, OFFLINE_TO_CONTRACT -> CORP`; `OFFLINE_TO_TRANSIENT -> bar_test verdict (OTA if "OTA", else CORP)`; `DIRECT -> RETAIL`; `ONLINE_TA -> OTA`; `UNDEFINED_CH_* -> majority target of that channel among non-Undefined rows of the same hotel`; `UNDEFINED_FALLBACK -> RETAIL`. `detect_groups` false. `sellable_rooms` null. `rates_include_tax` "unknown". `sellout_threshold` from settings. `segment_commission` from settings (`ota_commission_main`). `variable_cost` = `variable_cost_low_share` times `base_rate` (the pilot plan reruns with the high value). `max_lead`, `max_los` p99 over the warm-up window, `max_lead` forced to at least 120 for H1 with a note if raised.

- [ ] **Step 1: Write the failing tests**

```python
class Basket(unittest.TestCase):
    def _rows(self):
        rows = []
        for m in range(7, 13):
            for k in range(40):
                rows.append(_arow(arrival_date_year="2015", arrival_date_month=list(CA.MONTHS)[m - 1],
                                  adr=str(100 + 10 * (m - 6) + (k % 3)), adults="2"))
        for m in range(1, 7):
            for k in range(40):
                rows.append(_arow(arrival_date_year="2016", arrival_date_month=list(CA.MONTHS)[m - 1],
                                  adr=str(100 + 10 * m + (k % 3)), adults="2"))
        rows.append(_arow(adr="9999"))       # extreme, trimmed by p99
        rows.append(_arow(adr="-5"))         # negative, dropped
        rows.append(_arow(adults="4", adr="500"))  # family, out of basket
        out, _, _ = CA.branch_rows(rows, SETTINGS)
        return out

    def test_basket_filters(self):
        out = self._rows()
        rows, widened = CA.basket(out, {"DIRECT"}, min_rows=30)
        self.assertFalse(widened)
        self.assertTrue(all(float(r["rate"]) > 0 for r in rows))
        self.assertTrue(all(r["_raw"]["adults"] == "2" for r in rows))
        self.assertLess(max(float(r["rate"]) for r in rows), 9999)

    def test_factors_have_mean_one_and_base_rate_matches_by_construction(self):
        out = self._rows()
        settings = dict(SETTINGS, min_basket_rows=30)
        factors, widened = CA.price_month_factor(out, settings)
        self.assertAlmostEqual(sum(factors.values()) / 12, 1.0, places=6)
        self.assertEqual(widened, [])
        br = CA.base_rate(out, factors, settings)
        med = CA.monthly_median(CA.basket(out, CA.BAR_BRANCHES, settings=settings)[0])
        self.assertAlmostEqual(br, statistics.median(med[m] / factors[m] for m in med), places=6)

    def test_thin_month_widens_adults_filter(self):
        out = self._rows()
        out = [o for o in out if not (o["arrival"].startswith("2016-02") and o["_raw"]["adults"] == "2")]
        out += [dict(o, arrival="2016-02-10", _raw=dict(o["_raw"], adults="3")) for o in out[:35]]
        factors, widened = CA.price_month_factor(out, dict(SETTINGS, min_basket_rows=30))
        self.assertIn(2, widened)

    def test_rate_step_has_about_ninety_rungs_and_a_one_euro_floor(self):
        self.assertEqual(CA.rate_step(50.0, 500.0), 5.0)
        self.assertEqual(CA.rate_step(40.0, 100.0), 1.0)
        self.assertEqual(CA.rate_step(40.0, 220.0), 2.0)


class Bands(unittest.TestCase):
    def test_top_four_three_five_split(self):
        rows = []
        for m in range(1, 13):
            n = 5 + m   # more demand later in the year
            rows += [_arow(arrival_date_year="2016" if m <= 6 else "2015", arrival_date_month=list(CA.MONTHS)[m - 1]) for _ in range(n)]
        out, _, _ = CA.branch_rows(rows, SETTINGS)
        gross, occ, _ = CA.demand_season_band(out)
        labels = Counter(gross.values())
        self.assertEqual((labels["peak"], labels["shoulder"], labels["trough"]), (4, 3, 5))
        self.assertEqual(gross[12], "peak"); self.assertEqual(gross[1], "trough")

    def test_non_refund_excluded_from_gross(self):
        rows = [_arow(arrival_date_year="2015", arrival_date_month="August") for _ in range(5)]
        rows += [_arow(arrival_date_year="2016", arrival_date_month="January", deposit_type="Non Refund",
                       reservation_status="Canceled", is_canceled="1") for _ in range(50)]
        out, _, _ = CA.branch_rows(rows, SETTINGS)
        _, _, gross_by_month = CA.demand_season_band(out)
        self.assertEqual(gross_by_month[1], 0)


class Ratios(unittest.TestCase):
    def test_corp_ratio_is_weighted_median_of_monthly_ratios(self):
        rows = []
        for m in ("July", "August", "September"):
            rows += [_arow(arrival_date_year="2015", arrival_date_month=m, adr="200") for _ in range(35)]
            rows += [_arow(arrival_date_year="2015", arrival_date_month=m, market_segment="Corporate", adr="160") for _ in range(35)]
            rows += [_arow(arrival_date_year="2015", arrival_date_month=m, market_segment="Offline TA/TO",
                           customer_type="Contract", adr="120") for _ in range(35)]
        out, _, _ = CA.branch_rows(rows, SETTINGS)
        ratios, per_origin, _ = CA.segment_rate_ratio(out, dict(SETTINGS, min_ratio_rows=30))
        self.assertAlmostEqual(per_origin["CORPORATE"], 0.8, places=3)
        self.assertAlmostEqual(per_origin["OFFLINE_TO_CONTRACT"], 0.6, places=3)
        self.assertAlmostEqual(ratios["CORP"], 0.7, places=3)   # equal room nights, weighted median of the two origins


class BarTest(unittest.TestCase):
    def _rows(self, contract_step):
        rows = []
        for week in range(27, 53):
            year, month = "2015", CA.MONTHS  # arrival built from week: use day-of-month cycling within July..December
            m = 7 + (week - 27) // 4
            d = 1 + ((week - 27) % 4) * 7
            bar = 150 + 20 * ((week % 4) - 1.5)          # moves within the month
            to = 120 + (10 if contract_step and week % 4 >= 2 else 0)   # steps mid-month when contract_step
            for _ in range(8):
                rows.append(_arow(arrival_date_year=year, arrival_date_month=list(CA.MONTHS)[m - 1],
                                  arrival_date_day_of_month=str(d), arrival_date_week_number=str(week), adr=str(bar)))
                rows.append(_arow(arrival_date_year=year, arrival_date_month=list(CA.MONTHS)[m - 1],
                                  arrival_date_day_of_month=str(d), arrival_date_week_number=str(week),
                                  market_segment="Offline TA/TO", customer_type="Transient", adr=str(to)))
        out, _, _ = CA.branch_rows(rows, SETTINGS)
        return out

    def test_flat_contract_maps_to_corp(self):
        res = CA.bar_test(self._rows(contract_step=False), dict(SETTINGS, bar_corr_threshold=0.6, min_basket_rows=5))
        self.assertEqual(res["verdict"], "CORP")

    def test_verdict_needs_both_demeanings_to_agree(self):
        res = CA.bar_test(self._rows(contract_step=True), dict(SETTINGS, bar_corr_threshold=0.6, min_basket_rows=5))
        self.assertIn(res["verdict"], ("CORP", "inconclusive"))
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_convert_antonio.Basket tests.test_convert_antonio.Bands tests.test_convert_antonio.Ratios tests.test_convert_antonio.BarTest -v`
Expected: `AttributeError` on `basket`.

- [ ] **Step 3: Implement**

Append to `tools/convert_antonio.py`:

```python
WARMUP = (dt.date(2015, 7, 1), dt.date(2016, 6, 30))
EXCLUDED_WEEKS = [(dt.date(2016, 3, 21), dt.date(2016, 3, 27)),   # Easter week 2016
                  (dt.date(2015, 12, 24), dt.date(2016, 1, 1))]
BAR_BRANCHES = {"DIRECT", "ONLINE_TA"}
CORP_BRANCHES = {"CORPORATE", "AVIATION", "OFFLINE_TO_CONTRACT", "OFFLINE_TO_TRANSIENT"}
TARGET_OF = {"COMP": "NONREV", "ADR0": "NONREV", "GROUPS": "GROUP", "OFFLINE_TO_GROUP": "GROUP",
             "TP_CLUSTER": "GROUP", "CORPORATE": "CORP", "AVIATION": "CORP", "OFFLINE_TO_CONTRACT": "CORP",
             "DIRECT": "RETAIL", "ONLINE_TA": "OTA", "UNDEFINED_FALLBACK": "RETAIL"}


def _d(o: dict) -> dt.date:
    return dt.date.fromisoformat(o["arrival"])


def _in_window(o: dict, window) -> bool:
    return window[0] <= _d(o) <= window[1]


def _excluded(o: dict) -> bool:
    d = _d(o)
    return any(a <= d <= b for a, b in EXCLUDED_WEEKS)


def _percentile(values: List[float], p: float) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def basket(out_rows, branches: set, window=WARMUP, settings=None, min_rows=None, exclude_weeks=True):
    cand = [o for o in out_rows if o["_branch"] in branches and o["status"] == "stayed"
            and float(o["rate"]) > 0 and _in_window(o, window) and not (exclude_weeks and _excluded(o))]
    if not cand:
        return [], False
    rates = [float(o["rate"]) for o in cand]
    lo, hi = _percentile(rates, 0.01), _percentile(rates, 0.99)
    cand = [o for o in cand if lo <= float(o["rate"]) <= hi]
    room = Counter(o["room_type"] for o in cand).most_common(1)[0][0]
    meal = Counter(o["_raw"]["meal"] for o in cand).most_common(1)[0][0]
    cand = [o for o in cand if o["room_type"] == room and o["_raw"]["meal"] == meal
            and o["_raw"]["children"] in ("0", "0.0", "") and o["_raw"]["babies"] == "0"]
    strict = [o for o in cand if o["_raw"]["adults"] == "2"]
    need = min_rows if min_rows is not None else int((settings or {}).get("min_basket_rows", 30))
    if len(strict) >= need:
        return strict, False
    return cand, True


def monthly_median(rows) -> Dict[int, float]:
    by_m: Dict[int, List[float]] = defaultdict(list)
    for o in rows:
        by_m[_d(o).month].append(float(o["rate"]))
    return {m: statistics.median(v) for m, v in by_m.items() if v}


def price_month_factor(out_rows, settings) -> Tuple[Dict[int, float], List[int]]:
    need = int(settings.get("min_basket_rows", 30))
    strict, _ = basket(out_rows, BAR_BRANCHES, settings=settings, min_rows=0)
    wide, _ = basket(out_rows, BAR_BRANCHES, settings=settings, min_rows=10 ** 9)
    med: Dict[int, float] = {}
    widened: List[int] = []
    for m in range(1, 13):
        s = [o for o in strict if _d(o).month == m]
        if len(s) >= need:
            med[m] = statistics.median(float(o["rate"]) for o in s)
        else:
            w = [o for o in wide if _d(o).month == m]
            widened.append(m)
            med[m] = statistics.median(float(o["rate"]) for o in w) if w else float("nan")
    known = [v for v in med.values() if v == v]
    fill = statistics.median(known)
    med = {m: (v if v == v else fill) for m, v in med.items()}
    mean = sum(med.values()) / 12
    return {m: v / mean for m, v in med.items()}, widened


def base_rate(out_rows, factors, settings) -> float:
    rows, _ = basket(out_rows, BAR_BRANCHES, settings=settings)
    med = monthly_median(rows)
    return statistics.median(med[m] / factors[m] for m in med)


def floor_ceiling(out_rows, settings) -> Tuple[float, float]:
    rows, _ = basket(out_rows, BAR_BRANCHES, settings=settings)
    rates = [float(o["rate"]) for o in rows]
    widen = float(settings.get("floor_ceiling_widen", 0.10))
    return round(_percentile(rates, 0.02) * (1 - widen), 2), round(_percentile(rates, 0.98) * (1 + widen), 2)


def rate_step(floor: float, ceiling: float) -> float:
    raw = (ceiling - floor) / 90.0
    for step in (1.0, 2.0, 5.0):
        if raw <= step * 1.5:
            return step
    return 5.0


def _gross_nights(out_rows, window=WARMUP, drop_duplicates=False) -> Dict[int, float]:
    seen = set()
    gross: Dict[int, float] = defaultdict(float)
    for o in out_rows:
        if not _in_window(o, window) or o["_branch"] == "COMP":
            continue
        if o["_raw"]["deposit_type"] == "Non Refund":
            continue
        if drop_duplicates:
            key = tuple(sorted((k, v) for k, v in o["_raw"].items()))
            if key in seen:
                continue
            seen.add(key)
        nights = int(o["nights"])
        for k in range(nights):
            gross[(_d(o) + dt.timedelta(days=k)).month] += 1
    return gross


def _rank_bands(by_month: Dict[int, float]) -> Dict[int, str]:
    order = sorted(range(1, 13), key=lambda m: (-by_month.get(m, 0.0), m))
    return {**{m: "peak" for m in order[:4]}, **{m: "shoulder" for m in order[4:7]}, **{m: "trough" for m in order[7:]}}


def demand_season_band(out_rows, window=WARMUP):
    gross = _gross_nights(out_rows, window)
    occ: Dict[int, float] = defaultdict(float)
    for o in out_rows:
        if o["status"] == "stayed" and _in_window(o, window):
            for k in range(int(o["nights"])):
                occ[(_d(o) + dt.timedelta(days=k)).month] += 1
    return _rank_bands(gross), _rank_bands(occ), {m: gross.get(m, 0.0) for m in range(1, 13)}


def _weighted_median(pairs: List[Tuple[float, float]]) -> float:
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0] if pairs else float("nan")


def segment_rate_ratio(out_rows, settings):
    need = int(settings.get("min_ratio_rows", 30))
    bar_rows, _ = basket(out_rows, BAR_BRANCHES, settings=settings)
    bar_med = monthly_median(bar_rows)
    per_origin: Dict[str, float] = {}
    weights: Dict[str, float] = {}
    by_band: Dict[str, Dict[str, float]] = defaultdict(dict)
    bands, _, _ = demand_season_band(out_rows)
    for origin in sorted(CORP_BRANCHES | set(GROUP_BRANCHES)):
        rows, _ = basket(out_rows, {origin}, settings=settings, min_rows=0)
        by_m: Dict[int, List[float]] = defaultdict(list)
        for o in rows:
            by_m[_d(o).month].append(float(o["rate"]))
        monthly = [statistics.median(v) / bar_med[m] for m, v in by_m.items()
                   if len(v) >= need and m in bar_med and sum(1 for o in bar_rows if _d(o).month == m) >= need]
        if monthly:
            per_origin[origin] = statistics.median(monthly)
            weights[origin] = float(sum(int(o["nights"]) for o in rows))
            for band in ("peak", "shoulder", "trough"):
                sel = [statistics.median(v) / bar_med[m] for m, v in by_m.items() if bands[m] == band and m in bar_med and len(v) >= need]
                if sel:
                    by_band[origin][band] = statistics.median(sel)
    ratios = {}
    for target, origins in (("CORP", CORP_BRANCHES), ("GROUP", set(GROUP_BRANCHES))):
        pairs = [(per_origin[o], weights[o]) for o in origins if o in per_origin]
        if pairs:
            ratios[target] = round(_weighted_median(pairs), 4)
    return ratios, per_origin, dict(by_band)


def _corr(xs: List[float], ys: List[float]) -> float:
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs); syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sxx * syy) ** 0.5


def _weekly_residuals(rows, block: str) -> Dict[int, float]:
    """Weekly median adr minus its month (or two-week block) mean, keyed by week number."""
    weekly: Dict[int, List[float]] = defaultdict(list)
    week_block: Dict[int, str] = {}
    for o in rows:
        w = int(o["_raw"]["arrival_date_week_number"])
        weekly[w].append(float(o["rate"]))
        d = _d(o)
        week_block[w] = ("%d-%02d" % (d.year, d.month)) if block == "month" else ("%d-%02d" % (d.year, w // 2))
    med = {w: statistics.median(v) for w, v in weekly.items()}
    blocks: Dict[str, List[float]] = defaultdict(list)
    for w, v in med.items():
        blocks[week_block[w]].append(v)
    bmean = {b: sum(v) / len(v) for b, v in blocks.items()}
    return {w: v - bmean[week_block[w]] for w, v in med.items()}


def bar_test(out_rows, settings) -> dict:
    thr = float(settings.get("bar_corr_threshold", 0.6))
    need = int(settings.get("min_basket_rows", 30))
    bar_rows, _ = basket(out_rows, BAR_BRANCHES, settings=settings, min_rows=need)
    to_rows, _ = basket(out_rows, {"OFFLINE_TO_TRANSIENT"}, settings=settings, min_rows=need)
    result = {"weeks": 0, "residual_corr_month": float("nan"), "residual_corr_2wk": float("nan"), "verdict": "CORP"}
    if not bar_rows or not to_rows:
        result["verdict"] = "inconclusive"
        return result
    corrs = []
    for block in ("month", "2wk"):
        a, b = _weekly_residuals(bar_rows, block), _weekly_residuals(to_rows, block)
        weeks = sorted(set(a) & set(b))
        result["weeks"] = len(weeks)
        c = _corr([a[w] for w in weeks], [b[w] for w in weeks])
        result["residual_corr_month" if block == "month" else "residual_corr_2wk"] = c
        corrs.append(c)
    votes = [c == c and c > thr for c in corrs]
    if all(votes):
        result["verdict"] = "OTA"
    elif any(votes):
        result["verdict"] = "inconclusive"
    return result
```

Then `derive_hotel_json`, `audit`, and `convert`:

```python
def _majority_target_by_channel(out_rows) -> Dict[str, str]:
    votes: Dict[str, Counter] = defaultdict(Counter)
    for o in out_rows:
        if o["_branch"].startswith("UNDEFINED"):
            continue
        votes[o["_raw"]["distribution_channel"]][TARGET_OF.get(o["_branch"], "CORP")] += 1
    return {ch: c.most_common(1)[0][0] for ch, c in votes.items()}


def derive_hotel_json(out_rows, hotel_code: str, settings: dict) -> dict:
    factors, widened = price_month_factor(out_rows, settings)
    br = base_rate(out_rows, factors, settings)
    floor, ceiling = floor_ceiling(out_rows, settings)
    bands_gross, bands_occ, gross = demand_season_band(out_rows)
    ratios, per_origin, by_band = segment_rate_ratio(out_rows, settings)
    verdict = bar_test(out_rows, settings)
    warm = [o for o in out_rows if _in_window(o, WARMUP)]
    leads = [int(o["_raw"]["lead_time"]) for o in warm]
    nights = [int(o["nights"]) for o in warm]
    max_lead = int(_percentile(leads, 0.99)) if leads else 180
    notes = []
    if hotel_code == "H1" and max_lead < 120:
        notes.append("max_lead raised from %d to 120 so the 120-day mark can run" % max_lead)
        max_lead = 120
    majority = _majority_target_by_channel(out_rows)
    seg_map = dict(TARGET_OF)
    seg_map["OFFLINE_TO_TRANSIENT"] = "OTA" if verdict["verdict"] == "OTA" else "CORP"
    for ch, branch in CHANNEL_BRANCH.items():
        seg_map[branch] = majority.get(ch, "RETAIL")
    return {
        "name": {"H1": "H1 Resort Hotel, Algarve", "H2": "H2 City Hotel, Lisbon"}[hotel_code],
        "currency": "EUR", "fx": {}, "sellable_rooms": None, "rates_include_tax": "unknown",
        "group_threshold_rooms": int(settings["group_threshold_rooms"]), "detect_groups": False,
        "rate_floor": floor, "rate_ceiling": ceiling, "rate_step": rate_step(floor, ceiling),
        "base_rate": round(br, 2),
        "variable_cost": round(br * float(settings["variable_cost_low_share"]), 2),
        "max_lead": max_lead, "max_los": max(1, int(_percentile(nights, 0.99))) if nights else 7,
        "sellout_threshold": float(settings["sellout_threshold"]),
        "price_month_factor": {str(m): round(f, 4) for m, f in factors.items()},
        "demand_season_band": {str(m): b for m, b in bands_gross.items()},
        "segment_rate_ratio": ratios,
        "segment_commission": {"RETAIL": 0.0, "OTA": float(settings["ota_commission_main"]), "CORP": 0.0, "GROUP": 0.0},
        "events": [],
        "segment_map_order": ["segment"],
        "segment_map": {"segment": seg_map},
        "_derivation": {"window": [WARMUP[0].isoformat(), WARMUP[1].isoformat()], "widened_months": widened,
                        "bands_from_occupancy": {str(m): b for m, b in bands_occ.items()},
                        "gross_by_month": {str(m): v for m, v in gross.items()},
                        "ratio_per_origin": per_origin, "ratio_by_band": by_band, "bar_test": verdict, "notes": notes},
    }
```

`_derivation` is not a `HotelConfig` field; `convert()` writes it to `audit.md` and strips it from the JSON before saving (the loader rejects unknown keys on purpose).

```python
def audit(out_rows, hotel_code: str, settings: dict, derived: dict) -> str:
    raw = [o["_raw"] for o in out_rows]
    counts = Counter(o["_branch"] for o in out_rows)
    lines = ["# Audit %s" % hotel_code, "", "## Rows per rule branch", ""]
    lines += ["- %s: %d" % (b, n) for b, n in counts.most_common()]
    adr0 = Counter((o["_raw"]["market_segment"], o["status"]) for o in out_rows if float(o["rate"]) == 0)
    lines += ["", "## adr = 0 rows by market segment and status", ""] + ["- %s / %s: %d" % (k[0], k[1], n) for k, n in adr0.most_common()]
    cross: Dict[str, Counter] = defaultdict(Counter)
    for r in raw:
        cross[r["market_segment"]][r["distribution_channel"]] += 1
    lines += ["", "## market_segment by distribution_channel", ""] + ["- %s: %s" % (s, dict(c)) for s, c in cross.items()]
    av_nights = {o["arrival"] for o in out_rows if o["_branch"] == "AVIATION"}
    lines += ["", "## Aviation", "", "- rows: %d, nights with at least one Aviation room: %d" % (counts["AVIATION"], len(av_nights))]
    dup = Counter(tuple(sorted(r.items())) for r in raw)
    dups = [k for k, n in dup.items() if n > 1]
    dup_status = Counter(dict(k)["reservation_status"] for k in dups)
    lines += ["", "## Duplicate rows", "", "- identical rows appearing more than once: %d, by status %s" % (len(dups), dict(dup_status))]
    from pace import ingest as _ing   # local import keeps tools/ free of pace at module load
    hb = [float(o["rate"]) for o in out_rows if o["_raw"]["meal"] == "HB" and o["status"] == "stayed" and float(o["rate"]) > 0]
    bb = [float(o["rate"]) for o in out_rows if o["_raw"]["meal"] == "BB" and o["status"] == "stayed" and float(o["rate"]) > 0]
    if hb and bb:
        lines += ["", "## Meal check", "", "- median adr HB %.2f vs BB %.2f (same hotel, all room types); a steady large gap means adr includes meals"
                  % (statistics.median(hb), statistics.median(bb))]
    canc = [r for r in raw if r["reservation_status"] == "Canceled"]
    nr = [r for r in raw if r["deposit_type"] == "Non Refund"]
    lines += ["", "## Cancellations", "",
              "- cancellation rate all rows: %.1f%%" % (100 * len(canc) / max(1, len(raw))),
              "- cancellation rate excluding Non Refund: %.1f%%" % (100 * sum(1 for r in canc if r["deposit_type"] != "Non Refund") / max(1, len(raw) - len(nr))),
              "- Non Refund rows: %d, of which cancelled: %d" % (len(nr), sum(1 for r in nr if r["reservation_status"] == "Canceled"))]
    leads = sorted(int(r["lead_time"]) for r in raw)
    lines += ["", "## Lead time", "", "- median %d, p90 %d, p99 %d, max %d" % (leads[len(leads) // 2], leads[int(0.9 * (len(leads) - 1))], leads[int(0.99 * (len(leads) - 1))], leads[-1])]
    d = derived["_derivation"]
    lines += ["", "## Derived hotel.json (window %s to %s)" % tuple(d["window"]), "",
              "- base_rate %.2f, floor %.2f, ceiling %.2f, step %.1f" % (derived["base_rate"], derived["rate_floor"], derived["rate_ceiling"], derived["rate_step"]),
              "- price_month_factor %s" % derived["price_month_factor"],
              "- widened months %s" % d["widened_months"],
              "- demand bands from gross demand %s" % derived["demand_season_band"],
              "- demand bands from occupancy %s" % d["bands_from_occupancy"],
              "- segment_rate_ratio %s (simulated hotel: CORP 0.82, GROUP 0.70)" % derived["segment_rate_ratio"],
              "- ratio per origin %s" % d["ratio_per_origin"],
              "- ratio by band %s" % d["ratio_by_band"],
              "- BAR test %s" % d["bar_test"],
              "- notes %s" % d["notes"]]
    return "\n".join(lines) + "\n"


def convert(csv_path: str, out_dir: str, settings_path: str) -> None:
    with open(settings_path, encoding="utf-8") as fh:
        settings = json.load(fh)
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    os.makedirs(out_dir, exist_ok=True)
    audits = []
    for hotel_name, code in HOTEL_CODE.items():
        sub = [r for r in rows if r["hotel"] == hotel_name]
        out, counts, notes = branch_rows(sub, settings)
        derived = derive_hotel_json(out, code, settings)
        derived["_derivation"]["notes"] += notes
        text = audit(out, code, settings, derived)
        with open(os.path.join(out_dir, "%s-bookings.csv" % code.lower()), "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=OUT_COLUMNS, extrasaction="ignore")
            w.writeheader()
            w.writerows(out)
        clean = {k: v for k, v in derived.items() if k != "_derivation"}
        with open(os.path.join(out_dir, "%s-hotel.json" % code.lower()), "w", encoding="utf-8") as fh:
            json.dump(clean, fh, indent=2)
        audits.append(text)
        print(text)
    with open(os.path.join(out_dir, "audit.md"), "w", encoding="utf-8") as fh:
        fh.write("\n\n".join(audits))


def main(argv):
    if len(argv) != 4:
        print("usage: python3 -m tools.convert_antonio hotels.csv out_dir settings.json")
        return 1
    convert(argv[1], argv[2], argv[3])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

Remove the stray `from pace import ingest as _ing` line inside `audit` (it is not needed; the note about keeping `tools/` free of `pace` still holds).

- [ ] **Step 4: Run tests and suite**

Run: `python3 -m unittest tests.test_convert_antonio -v && python3 run.py test 2>&1 | tail -3`
Expected: PASS; OK. If `Ratios` or `BarTest` fails on a threshold detail, fix the fixture rows, not the rule; the rules are the spec's.

- [ ] **Step 5: Commit**

```bash
git add tools/convert_antonio.py tests/test_convert_antonio.py
git commit -m "The converter derives what a hotel would know about itself from its first year, from one price basket, and writes down how

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 12: settings fixed before the download, then the download gate

**Files:**
- Create: `data/antonio/settings.json`, `data/antonio/README.md`
- Modify: `data/.gitignore` (already covers generated files)

**Interfaces:**
- Produces: `data/antonio/settings.json` read by `convert()` and later by the pilot plan.

- [ ] **Step 1: Write settings.json from spec section 9**

```json
{
  "_source": "docs/superpowers/specs/2026-09-17-real-data-ingest-and-pilot-design.md, section 9. Committed before hotels.csv was downloaded.",
  "seed": 20250115,
  "group_threshold_rooms": 10,
  "undefined_stop_share": 0.01,
  "bar_corr_threshold": 0.6,
  "min_basket_rows": 30,
  "min_ratio_rows": 30,
  "floor_ceiling_widen": 0.10,
  "basket_trim_percentiles": [0.01, 0.99],
  "sellout_threshold": 0.97,
  "ota_commission_main": 0.15,
  "ota_commission_secondary": 0.00,
  "variable_cost_low_share": 0.08,
  "variable_cost_high_share": 0.18,
  "nonrev_rerun_p90_share": 0.02,
  "nonrev_near_full_share": 0.90,
  "close_cheap_first_mark": 0.90,
  "baseline_window_weeks": 10,
  "lead_marks": [120, 90, 60, 30, 14, 7],
  "lead_marks_h1_only": [120],
  "rate_windows": {"60": [75, 45], "30": [40, 21], "14": [21, 7], "7": [10, 4]},
  "holdout_clean_thresholds": [0.80, 0.85, 0.90],
  "holdout_caps": [0.60, 0.70, 0.80],
  "demand_band_split": [4, 3, 5],
  "rate_step_min": 1.0,
  "excluded_weeks": [["2016-03-21", "2016-03-27"], ["2015-12-24", "2016-01-01"]],
  "warmup_window": ["2015-07-01", "2016-06-30"],
  "scoring_window": ["2016-07-01", "2017-08-31"]
}
```

- [ ] **Step 2: Write data/antonio/README.md**

```markdown
# Public-data pilot inputs

`settings.json` holds every number that could have been tuned after seeing
the data. It was committed before `hotels.csv` was downloaded; the pilot
report prints the hash of that commit.

Source: Antonio, de Almeida and Nunes (2019), "Hotel booking demand datasets",
Data in Brief 22, CC BY 4.0. The CSV used is the TidyTuesday 2020-02-11 copy,
119,390 rows. Download (about 16 MB), never committed:

    curl -L -o data/antonio/hotels.csv https://raw.githubusercontent.com/rfordatascience/tidytuesday/main/data/2020/2020-02-11/hotels.csv

Convert:

    python3 -m tools.convert_antonio data/antonio/hotels.csv data/antonio data/antonio/settings.json

Outputs `h1-bookings.csv`, `h2-bookings.csv`, `h1-hotel.json`, `h2-hotel.json`
and `audit.md`, all ignored by git.
```

- [ ] **Step 3: Commit, and record the hash**

```bash
git add data/antonio/settings.json data/antonio/README.md
git commit -m "Every number that could be tuned after seeing the data is committed before the data exists on this machine

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git rev-parse --short HEAD
```

Write the printed hash into `data/antonio/README.md` under a line `Pre-registration commit: <hash>` and amend nothing; make it a second small commit: `git commit -am "The pre-registration commit is named where the pilot report will look for it"` with the Co-Authored-By line.

- [ ] **Step 4: STOP and ask Elle in chat before downloading**

State: file `hotels.csv`, source `raw.githubusercontent.com/rfordatascience/tidytuesday/main/data/2020/2020-02-11/hotels.csv`, about 16 MB, CC BY 4.0. Do not run the curl until she says yes. If she says no, the plan ends after Task 13 with the converter tested on fixtures only.

- [ ] **Step 5: After the yes: download, convert, read the audit**

```bash
curl -L -o data/antonio/hotels.csv https://raw.githubusercontent.com/rfordatascience/tidytuesday/main/data/2020/2020-02-11/hotels.csv
wc -l data/antonio/hotels.csv          # expect 119391 (header plus 119,390 rows)
python3 -m tools.convert_antonio data/antonio/hotels.csv data/antonio data/antonio/settings.json
python3 run.py ingest data/antonio/h1-bookings.csv data/antonio/h1-hotel.json
python3 run.py ingest data/antonio/h2-bookings.csv data/antonio/h2-hotel.json
```

(`run.py ingest` is added in Task 13; run Task 13 before this step if it is not there yet.) Compare the audit against spec section 8's recollections and write what the data actually says into a new section 10 of the spec, "What the audit found", one line per recollection. If `ConvertStop` fires, stop and report; do not change the rule.

---

### Task 13: run.py ingest command and docs sync

**Files:**
- Modify: `run.py` (docstring at top, dispatch near line 85), `README.md` (PMS paragraph and commands), `EXTENDING.md` ("Connecting it to a real property management system", line 170 onward), `CLAUDE.md` (commands, sources of truth)

**Interfaces:**
- Produces: `python3 run.py ingest <bookings.csv> <hotel.json>`: loads through `ingest.load`, prints rows, nights, rooms (inferred or given, with the ceiling check), warnings and notes, and the first 30 recommendations from a `PaceEngine` fitted on the ledger, as a smoke test that the engine runs on the data. Exit 1 on `IngestError` with its message.

- [ ] **Step 1: Add the command**

In `run.py`, before `if cmd == "test":`:

```python
    if cmd == "ingest":
        if len(argv) < 4:
            print("usage: python3 run.py ingest <bookings.csv> <hotel.json>")
            return 1
        from pace import ingest
        from pace.hotelconfig import event_calendar
        from pace.policy import PaceEngine
        try:
            res = ingest.load(argv[2], argv[3])
        except ingest.IngestError as exc:
            print(exc)
            return 1
        rep = res.report
        print("rows %d, ledger bookings %d, cancels %d, nights %s to %s"
              % (len(res.bookings), res.ledger.n_bookings, res.ledger.n_cancels, res.first_stay, res.last_stay))
        print("rooms %d (%s)" % (res.hotel.rooms, "inferred" if res.inference else "from hotel.json"))
        for k, v in sorted(rep.warnings.items()):
            if not k.startswith("_"):
                print("warning %-28s %d" % (k, v))
        for n in rep.notes:
            print("note", n)
        engine = PaceEngine(res.hotel, event_calendar(res.cfg))
        engine.observe(res.last_stay + dt.timedelta(days=1), res.ledger)
        print("engine fitted:", engine.ready)
        return 0
```

Add `import datetime as dt` at the top of `run.py` if it is not already there, and a line for `ingest` in the module docstring that `run.py` prints as usage.

- [ ] **Step 2: Smoke test on the sample**

Run: `python3 run.py ingest data/sample-bookings.csv data/sample-hotel.json`
Expected: prints rows, rooms 40 (from hotel.json), warnings, `engine fitted: False` or `True` (the sample is small; either is acceptable, the point is no traceback).

- [ ] **Step 3: Docs**

README.md: in the commands block add `python3 run.py ingest bookings.csv hotel.json   # a real booking log into the ledger`; in "Honesty about what this is" or the closing section add two sentences: the engine can now read a real booking log through the schema in `docs/booking-log.md`, and the first real-data pilot on the public Portuguese dataset is described in the spec (link) with its results to follow.

EXTENDING.md, section "Connecting it to a real property management system": keep the four points, then add:

```markdown
The concrete way to do this is a booking log in the shape of
`docs/booking-log.md`, read by `pace/ingest.py`. Write a converter from your
PMS export to that shape; `tools/convert_antonio.py` is the worked example
for a public dataset, including how the hotel's own seasonality, contract
ratios and sell-out threshold are derived and written to `hotel.json`
(ADR 0007). Complimentary and house-use rooms stay outside the ledger and
are reported beside it, because the engine has no capacity block yet.
```

CLAUDE.md: add the `ingest` command to the commands block; under "Sources of truth" add `docs/booking-log.md` (schema) and note that `hotel.json` fields are mandatory on the ingest path; under "Things that look like bugs and are not" add: "`tests/test_golden.py::ExplicitTorontoConfig` takes about 14 s; it proves the configuration path reproduces the golden numbers (ADR 0007)."

- [ ] **Step 4: Full check**

Run: `python3 run.py test 2>&1 | tail -3 && python3 run.py build --quick | tail -4 && wc -l CLAUDE.md`
Expected: OK; 64.50 / 78.80 / 82.98; CLAUDE.md under 120 lines.

- [ ] **Step 5: Commit**

```bash
git add run.py README.md EXTENDING.md CLAUDE.md
git commit -m "A real booking log has a command, and the docs point a hotel at the shape it needs to send

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review against the spec

- Section 3 schema, columns, `hotel.json`, mapping order, prefix `*`, unmapped grouped, group rule with `booked_on` and `detect_groups`, NONREV outside the ledger with dates, validation (50 errors, warnings), clamps, undated cancellation imputation with bounds, sellable rooms from records with ceiling check and yearly maxima, replay with snapshots and settle, no_show on the books until arrival: Tasks 5 to 9.
- Section 3 "What the engine needs": seasonality as two fields (Task 2), `sellout_threshold` on Hotel and in the fit functions (Task 3), segment ratios and commissions (Task 3), events via `event_calendar` (Task 4), strict loader with mandatory fields and the explicit-Toronto golden test (Task 4), ADR 0007 (Tasks 1 and 4). Prior-versus-data cell share and fitted elasticity sign: those are pilot-report outputs, they belong to the pilot plan; noted there.
- Section 4 converter: rule order, branch codes, Transient-party clustering with empty-field rule, Undefined by channel majority and the 1 percent stop, `booked_on` from lead, status from `reservation_status`, duplicates kept, `first_stay` and `last_stay` trimming (the pilot plan applies the trim when it calls `ingest.load(first_stay=..., last_stay=...)`; the converter's audit prints p99 nights so the value is known), BAR test with month and two-week demeaning and the basket filter, audit contents: Tasks 10 and 11. Audit items not yet produced here and deferred to the pilot plan: inferred room count with and without duplicates (needs `ingest.infer_sellable_rooms` on both sets; add to the pilot plan's audit step), share of censored nights, prior share, fitted elasticity.
- Section 9 settings committed before download, hash recorded: Task 12.
- Section 7 order: Tasks follow 0 to 3 and the download gate; steps 4 and 5 of the spec (baselines, holdout, report, run, ADRs) are the pilot plan.
- Placeholder scan: no TBD/TODO; every code step has code. Type check: `Booking.target` is `Optional[str]` everywhere; `Report.warnings` is a `Counter`; `IngestResult.nonrev` is `List[Booking]` and `nonrev_on(nonrev, night, asof)` is the only reader; `HotelConfig.sellable_rooms` is `Optional[int]` and `load()` fills it before `apply()`.
- Known gaps for the pilot plan: `first_stay`/`last_stay` trim; two `variable_cost` values; commission secondary run; NONREV rerun rule; the report; ADRs for weaknesses; spec section 10 "What the audit found".
