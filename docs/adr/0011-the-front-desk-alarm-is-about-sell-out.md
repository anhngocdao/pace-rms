# ADR 0011: The front desk alarm is about sell-out, not about being oversold
Status: Proposed · Date: 2026-09-21

## Context
The alarm was first written to warn the desk that a night would be oversold. Measured over the 427-night scoring window on both pilot hotels, nights where rooms sold exceeded capacity: 0 and 0; walks recorded: 0 and 0. The reason is structural: `sellable_rooms` is inferred from the busiest night the log contains (187 of 187 at H1, 226 of 226 at H2), so occupancy cannot exceed it, and the public dataset records no walk. A warning about being oversold is unmeasurable on any hotel whose room count Pace infers, which is every hotel that arrives without stating one.

## Decision
The event is the night reaching the pre-registered sell-out cut, settled revenue rooms at or above `sellout_threshold` (0.97) times `pilot.capacity_on`, and the warning fires when the top of the rooms band reaches the same cut on the same quantity. Forecast and outcome net out the comped rooms on both sides. The tab does not print `walk_cost` and does not claim anyone will be relocated. Nights full in the house but short in the ledger are printed as their own count and folded into no rate.

## Consequences
Both event counts are upper bounds, because an inferred room count is biased low. Precision, recall and notice are measured against reaching the cut, which is worth a desk knowing for reasons that are not overbooking: stop discounting, tighten the minimum stay, warn that walk-ins will be turned away. The warning quantile is the top of the printed band, and the measurement says what that choice costs: recall of one at every lead on both hotels, bought with a precision that falls from 0.96 and 0.88 at lead 0 to 0.31 and 0.43 at lead 14, so at a week out two warnings in three at H1 and one in two at H2 are false. Moving the quantile is now a decision with numbers attached. Run live, a warning that worked scores as a false alarm; forward-mode warnings are therefore not scored (spec section 8).

## Evidence
`out/handover-h1.json` and `out/handover-h2.json` from the runs of 6 October 2026: 94 of 427 nights reached the cut at H1 and 132 at H2; 29 and 32 were full in the house while the ledger was short; precision and recall at lead 7 0.322 / 1.000 (292 warned, 94 hits) and 0.498 / 1.000 (265 warned, 132 hits); median notice 14 or more, with 93 of 94 and 127 of 132 censored at 14, the deepest lead the walk records. Spec section 7.1 for the measurement that forced the decision.
