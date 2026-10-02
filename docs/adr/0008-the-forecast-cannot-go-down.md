# ADR 0008: The forecast cannot go down
Status: Proposed · Date: 2026-09-19

## Context
`pace/forecast.py:91` ends the blend with `expected = max(float(otb), expected)`, so a forecast is never below the rooms already on the books. On the simulated hotel that is almost free: demand there is generated, cancellations are drawn from a fixed rate, and a night rarely finishes below where it stood. On the real history it is not free. At H1, 41 of 427 scoring nights, 9.6 percent, finish below their lead-1 on the books, and the measured lead-1 bias is +1.20 rooms over the 415 nights every method forecasts. At H2 it is 126 of 427, 29.5 percent, and the lead-1 bias is +1.19 rooms. The floor is the whole of that bias.

## Decision
Record it and change nothing here. This project drives the engine and does not alter how it decides. The pilot prints the lead-1 line separately, labelled as an overbooking question rather than a demand question, so the floor is visible rather than absorbed into a headline.

## Consequences
Anywhere the forecast feeds an overbooking decision, the engine is structurally optimistic at short leads by roughly the cancellation and no-show rate it cannot see coming. `pace/controls.py` already estimates cancellation and no-show probabilities separately, so the two corrections are not the same number twice; a future change that lets the forecast fall has to check that they do not become so.

## Evidence
Table 1, lead 1, both hotels, in `out/pilot-h1.md` and `out/pilot-h2.md` from the full runs of 2 October 2026 (commit 7b83cc9). The survey of 19 September 2026 measured 7.5 percent and a bias of +0.9 at H1 before the walk existed; the full payload gives 41 of 427 and +1.20, and the payload's figure is the one quoted. H2's 29.5 percent is the larger number and the one a city hotel with short leads should expect.
