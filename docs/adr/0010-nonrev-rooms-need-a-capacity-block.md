# ADR 0010: NONREV rooms need a capacity block
Status: Proposed · Date: 2026-09-19

## Context
Complimentary, house-use and staff rooms occupy a room and carry no revenue. `pace/ingest.py` keeps them out of the ledger, because `forecast.py` iterates every key of `segment_mix` and `adr_on` divides all revenue by all occupied rooms, so a NONREV segment inside the ledger would either raise on an unknown segment or dilute ADR. The consequence is that the engine sees more rooms free than there are. The pilot measures how often that matters: it counts the nights that were physically full while the ledger was below the sell-out threshold, and table 2 shows the rate gap on nights holding a comp room separately from nights without one. On the full runs of 2 October 2026, 153 of 779 nights at H1 were physically full and 49 of them read as not full to the ledger; at H2, 151 of 784 and 35.

## Decision
Record it and build nothing here. The engine has no way to say "these rooms are gone and no revenue is coming", which is what a standing airline-crew block or a maintenance block needs. The pilot's diagnostic is the measurement that a future capacity block would have to improve on.

## Consequences
Until it exists, every hotel with a standing block has an inflated authorised capacity by the size of that block, and the unconstrainer reads its fullest nights as uncensored. On this dataset the effect is a third of the full nights at H1 and a quarter at H2; on a hotel with a crew contract it would be larger and permanent.

## Evidence
`pilot.full_night_gap` for both hotels, printed under "What the ledger could not see" in `out/pilot-h1.md` and `out/pilot-h2.md`. Table 2's comp-room split. Section 8 of the design, which lists the capacity block as an open item.
