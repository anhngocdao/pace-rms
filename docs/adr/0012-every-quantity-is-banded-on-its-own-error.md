# ADR 0012: Every quantity is banded on its own error
Status: Proposed · Date: 2026-09-21

## Context
The handover prints five quantities as bands: revenue rooms, breakfast covers, dinner covers, departures and stayovers. The engine forecasts only the first. The cheap way to band the other four is to take the rooms band and multiply it by a meal share and a guests-per-room ratio. That discards the variation in the ratios, so the measured coverage comes in below the label, which breaks the one promise a band makes.

## Decision
A band is the empirical p10 to p90 of the error of the finished number, measured end to end on nights the scoring never touches (the warm-up window in proof mode, the trailing year of settled nights in forward mode), with a floor of 30 nights per lead before any band is printed. Quantiles are empirical, not fitted. The walk records every lead from 0 to 14; nothing is interpolated between the pilot's marks.

## Consequences
Width includes the ratios' variation, so a worse point forecast produces a wider band and a truthful coverage figure rather than a narrower band and a false one. Coverage is reported as measured, beside its count, and the label on the page is written from the measurement. The measurement is not uniformly at the label: a band drawn on 200 warm-up nights and scored on 427 nights a year later covers between 0.67 (stayovers at lead 0, H2) and 0.98 (rooms at lead 0, H2) at the leads read, and the page prints each figure with its count rather than the nominal 0.80.

## Evidence
`handover.bands`, `handover.coverage`, the `Bands` tests. From the runs of 6 October 2026, rooms band coverage at lead 7: 0.857 (366 of 427) at H1, 0.867 (370 of 427) at H2; breakfast covers at lead 7: 0.820 (350 of 427) and 0.829 (354 of 427); dinner covers at lead 7: 0.759 and 0.836. Band nights per lead: 186 to 200 at H1, 186 to 200 at H2, from the same pre-registered warm-up window of 2015-11-28 to 2016-06-30 with the excluded weeks removed.
