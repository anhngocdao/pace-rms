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
