# What a survey of the engine found before the pilot was planned

Five parallel readers surveyed the surfaces phase 5b has to drive, each
checked by a second reader told to find what the first missed. Every number
below was measured on the real converted files, not estimated. Written
19 September 2026, before any pilot code exists.

## The finding that would have made the pilot worthless

`Forecaster.forecast` reads the ledger's **current** state, not an as-of view.
It takes `ledger.rooms_on(stay_date)` at `forecast.py:67`, and `adr_on`,
`segment_mix` and `seg_revenue` the same way. The `asof` argument is used for
exactly one thing: `lead = (stay_date - asof).days`.

So handing it the ledger `ingest.load` returns, and sweeping `asof` backwards
to collect the lead marks, gives the engine the answer and then adds pickup on
top. It does not raise, warn, or return None. Measured on H1 stay date
2016-08-13, which finished at 183 rooms:

| Lead | What the engine was handed as on the books | What was really on the books |
|---|---|---|
| 120 | 183 | 131 |
| 90 | 183 | 154 |
| 60 | 183 | 170 |
| 30 | 183 | 177 |
| 14 | 183 | 176 |
| 7 | 183 | 180 |
| 1 | 183 | 183 |

Its forecasts came out 275, 250, 219, 197, 192, 189 and 184 rooms. Any pilot
written the obvious way would have produced a full set of plausible tables
describing nothing.

Corrected 26 September 2026, when the table was re-run at HEAD. The
handed-versus-real half reproduces exactly, 183 against 131 at lead 120. The
forecast row does not: the fit it came from was not recorded, and a fit over
the whole history gives 245.5 at lead 120, not 275. The row stands as a dated
record of what one unrecorded fit produced. The leak is the finding, and it is
the half that reproduces; nothing in the plan depends on the forecast row.

The consequence is structural: the pilot must own a day loop that mutates one
`Ledger` forward through time and calls the engine inside it. `ingest.replay`
has no hook of any kind, so that loop is roughly thirty lines the pilot writes
for itself, mirroring book, cancel, snapshot, settle. Reconstructing an as-of
ledger from the finished one is not available either: `ledger.holds` keeps only
the bookings that survived, so at lead 90 for one night only 123 of the 154
rooms then on the books still exist as holds.

## What does not need the walk

The four baselines read `Ledger.snapshots`, which is already the leak-free
as-of series and is complete after one `ingest.load`: 335,995 entries across
1,276 stay dates at H1. Only the engine needs the forward walk.

## Six more traps, each measured

1. **The lead-0 snapshot is not the actual.** The replay snapshots a day before
   it settles that night, so lead 0 still holds no-shows and any rooms the
   settlement walks. It differs from the settled figure on 108 of 427 scoring
   nights at H1 and 220 of 427 at H2. Actuals must come from
   `ledger.settled[d]["rooms_sold"]`.
2. **Loading the second hotel rewrites the first one's engine.**
   `hotelconfig.apply` rebinds the module-level seasonality and mutates the
   shared segments dict in place. H1 sets CORP 0.6275 and GROUP 0.9949, H2 sets
   0.7391 and 0.7653. A report that puts the two side by side must run them in
   separate processes.
3. **The engine cannot forecast a decline.** `expected = max(float(otb), expected)`
   at `forecast.py:91` floors every forecast at on the books. At H1, 7.5 percent
   of scoring nights finish below their lead-1 on the books, and the measured
   lead-1 bias is +0.9 rooms. That is the whole of the lead-1 line the design
   asks for, and it is a property of the engine rather than a mistake.
4. **The clamp does half the work at long leads.** On a correct forward walk at
   H1: lead 120 gives a mean absolute error of 38.5 rooms raw and 16.4 after
   clamping, with 50.4 percent of forecasts above capacity; lead 90 gives 31.0
   and 15.4. The clamp-share column the design asks for is not optional, it is
   the column that makes the other two readable.
5. **The published rate is path dependent.** The damping at `policy.py:171`
   reads yesterday's published rate and caps the daily move, so a one-off
   `recommend()` at a lead mark is not the rate the engine would have published.
   The walk has to drive `controls()` from at least 120 days before the scoring
   window opens.
6. **`controls()` re-solves on a cadence**, 14 days beyond lead 90 and 1 day
   inside lead 14, so a recommendation read at lead 120 may have been computed
   at lead 133. The published rate is still right; the label is not.

Corrected 26 September 2026, from a full H1 payload at HEAD, for traps 3 and
4. Trap 3: 41 of 427 scoring nights, 9.6 percent, finish below their lead-1 on
the books and the lead-1 bias is +1.19 rooms, not 7.5 percent and +0.9. Trap 4:
on all 427 scoring nights lead 120 gives 38.96 raw, 16.33 clamped and 50.4
percent above capacity, and lead 90 gives 31.90, 15.26 and 50.4; on the 415
nights table 1 prints, which is the population the report quotes, lead 120
gives 37.22, 16.58 and 48.9 and lead 90 gives 30.61, 15.48 and 48.9. The clamp
still does half the work; the figures above were measured before the walk
existed.

## Three things the design asks for that the code cannot give yet

- **The meal plan does not survive the converter.** The booking-log schema has
  no meal column, so `Booking` has no meal field and the ledger never sees one.
  Table 2 asks for the comparison split by meal plan. The information exists
  only in the raw dataset.
- **The rate ceiling can never bind.** `Hotel.rate_ladder` steps upward from the
  floor, so H1's top rung is 188.02 against a ceiling of 189.09, and the test
  `chosen >= hotel.rate_ceiling` can never fire. At H1, 168 of 413 scored nights
  sit pinned on that top rung. The band was frozen on the first twelve months
  and 5.7 percent of the comparable rows were sold above it, so table 2 at H1
  would be dominated by an artefact of the band rather than by the engine.
- **The unconstraining metric conflates two corrections.** `censoring_uplift` is
  the ratio of a mean that has been both price-restated and detruncated to a raw
  lead-0 count, so scoring it against known rooms charges the price restatement
  to the unconstrainer. Closing the cheap channels first makes it worse, because
  removing the cheapest rows raises the realised rate, lowers modelled
  acceptance and raises the price-restated demand independently of any censoring.

Corrected 26 September 2026, from a full H1 payload at HEAD. Table 2 scores
the pinned share per mark: 155 of 346 at lead 60, 181 of 374 at 30, 171 of 388
at 14 and 148 of 390 at 7, which is 655 of 1,498 over the four marks and 37.9
to 48.4 percent per mark. The 168 of 413 above was measured before the walk
existed and does not reproduce on a HEAD payload. The 5.7 percent named no
population and reproduces on none tried (13.6 percent of priced occupied rows,
6.8 of room type A); the reproducible figure is the converter's
`rate_out_of_range` warning, 5,555 of H1's 40,060 rows and 6,965 of H2's
79,330, priced outside the band on either side. The conclusion stands: table 2
at H1 is dominated by the band.

## What the holdout actually needs

Lowering `sellable_rooms` does not impose a cap: the replay books every row and
only the settlement walks the excess, one night at a time, after the fact. The
pilot needs its own cut pre-pass over the booking rows, and each capped run
needs its own `Hotel` with `rooms` set to the cap. Without that second part the
table is a null by construction, because a history capped at 60 percent of 187
rooms never reaches the 0.97 sell-out threshold and nothing is ever marked
censored.

The scored sample is small. Clean nights, with the window at the 90th
percentile of nights, come to 50, 99 and 115 at H1 and 158, 185 and 236 at H2
for the three clean thresholds. The design's three cut-size buckets can come
back empty.

Corrected 22 September 2026, when table 3's cut was built and the counts were
measured again. Those six numbers are right for every night the export covers,
806 at H1 and 803 at H2. Table 3 runs on the trimmed stay window instead, the
one `pilot.stay_window` returns, and over that window the counts are 45, 94 and
109 at H1 and 144, 171 and 222 at H2. The survey's window, not its arithmetic,
is what differs.

Corrected 26 September 2026, when table 3 had run on both hotels. At H1 the
sample is not small but empty: no combination reaches the gate of 20 censored
nights, the largest reaches 9, and the ten censored nights of the whole grid
are the same ten under different caps. What empties it is the pre-registered
neighbour window, not the clean nights being quiet. At threshold 0.90 and cap
112, 195 nights are censorable and below the clean threshold; with the window
at 0 all 195 are clean and the combination is scorable (81 censored nights,
MAE 25.4, bias -24.4); with the window of 8 only 16 are clean and 9 censored,
because a stay spanning a busy night is refused for all its nights and the
moderately busy nights a cap censors sit inside busy weeks. The result is H1's.
At H2, with a window of 5, two of 24 combinations are scorable: 0.90/0.60 sell
until full, n 26, MAE 25.63, bias -13.34; and 0.90/0.60 close cheap first,
n 22, MAE 24.78, bias -14.29.

## Measured costs

| Step | H1 | H2 |
|---|---|---|
| `ingest.load` | 4.6 s | 9.1 s |
| One engine fit, by history | 0.79 s at 184 nights to 2.09 s at 793 | similar |
| One forecast | 0.02 ms | 0.02 ms |
| One `controls()`, cold then warm | 0.16 s then 0.01 s | similar |

Curve building is 94 percent of a fit and scales with the hotel's maximum lead,
297 at H1 and 364 at H2. Forecasting itself is free; the walk and the refits are
the cost.
