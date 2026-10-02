# ADR 0009: A frozen rate band has no ceiling to reach
Status: Proposed · Date: 2026-09-19

## Context
`Hotel.rate_ladder` builds rungs upward from `rate_floor` in steps of `rate_step` while the rung is at or below `rate_ceiling`, so the top rung is below the ceiling by up to one step: 188.02 against 189.09 at H1, and 165.41 against 166.10 at H2. Any test of the form `chosen >= hotel.rate_ceiling` can therefore never fire. The band itself was derived from the first twelve months of history and frozen, and 14 percent of H1's rows and 9 percent of H2's fall outside it (5,555 of 40,060 and 6,965 of 79,330 `rate_out_of_range` warnings from the converter). At H1, 655 of the 1,498 night-marks table 2 scores are pinned on the top rung, 37.9 to 48.4 percent per mark. At H2 it is 1,532 of 1,607, 92.9 to 97.5 percent per mark: the city hotel's second year ran almost entirely above the band its first year fitted. Both figures are copied from the run's `band_note` in `out/pilot-<code>.json`, not from the survey.

## Decision
Record it and change nothing here. The band is not widened for the pilot and no second run is added: a band chosen after seeing the data it is scored on is not a band, it is a fit. Table 2 prints the share of nights pinned on the top rung and says the gap at the seasonal peak is an artefact of the frozen band.

## Consequences
On a hotel whose season runs above the band it was fitted on, the engine is capped and the cap is silent: nothing in the recommendation says the rate wanted to go higher. At H2 that is nearly every scored night, so table 2 there compares a frozen rung with a market that moved, and says so. Two things would fix it and neither belongs in this project: a ladder that includes the ceiling as its own rung, and a band that is re-derived on a rolling window with the re-derivation recorded.

## Evidence
`pace/config.py` `Hotel.rate_ladder`. Table 2's pinned share, both hotels, from the full runs of 2 October 2026 (commit 7b83cc9). The survey of 19 September 2026.
