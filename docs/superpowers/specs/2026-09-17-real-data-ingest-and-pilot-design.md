# Real data in, honest report out: booking-log ingest and the public-data pilot

Status: Draft for review, revised 2026-09-17. Plan phases 5a and 5b of
`Pace-RMS-Hardening-Plan.md`. Reviewed with Claude in a revenue-manager
role, in two rounds; not yet checked by a practising revenue manager. Where
a rule rests on a recollection about the data rather than a checked fact it
is listed in section 8 for the audit to confirm.

## 1. Goal and non-goals

Pace has only ever seen its own simulated market. This work gives it a door
for real booking history and runs the first real-data pilot on a public
dataset, because no hotel has yet shared data.

What the pilot can honestly measure on real history: forecast accuracy
against the methods a revenue manager uses without an engine, how the
engine's recommended rate compares with what was actually charged, and how
well unconstraining recovers demand hidden by a capacity cap we impose
ourselves. What it cannot measure: a RevPAR lift. History only records what
sold at the rate that was charged; no counterfactual policy can be replayed
on it, and the report says so in its first lines.

Non-goals: changing engine mechanisms, adding a fifth demand segment to the
engine, calibrating the simulator to real data (a different project and a
different kind of claim, see ADR 0006), and any machine learning. One
configuration seam does have to open (section 3, "What the engine needs
from hotel.json"): the seasonality table is hard-coded for the simulated
Toronto hotel and cannot stay that way on an Algarve resort.

## 2. Shape of the work

Three units, each testable alone:

1. `pace/ingest.py`: reads Pace's booking-log CSV plus a `hotel.json`,
   validates, and replays the log into the existing `Ledger`. The engine is
   untouched and reads the ledger exactly as it does today.
2. `tools/convert_antonio.py`: turns the public dataset (Antonio, de Almeida
   and Nunes 2019, two Portuguese hotels, arrivals 1 July 2015 to 31 August
   2017, CC BY 4.0) into two booking logs and two `hotel.json` files, and
   prints a data audit first.
3. `run.py pilot <bookings.csv> <hotel.json>`: ingests, lets the engine learn
   forward in time, and writes `out/pilot-<hotel>.md` and `.json`.

A real hotel later needs only a new converter. The schema is the thing to
hand a hotel when asking for data.

## 3. Booking-log schema (version 0, not locked)

The schema stays "version 0" until one or two real PMS exports have been
seen. Column names of Opera, ezCloud and Smile exports are not verified.

One row per room. Multi-room rows are expanded by the ingest. Required:

| column | type | rule |
|---|---|---|
| booking_id | string | unique per row; converters may synthesise it |
| booked_on | YYYY-MM-DD | date the booking entered the PMS |
| arrival | YYYY-MM-DD | |
| nights | integer >= 0 | 0 is day use; exactly one of nights / departure required |
| segment | string | hotel's own code, mapped via hotel.json |
| status | booked / in_house / stayed / cancelled / no_show | in_house counts as stayed |

Optional:

| column | rule |
|---|---|
| departure | YYYY-MM-DD; nights = departure minus arrival when nights is absent |
| rate | per room night, in the file's currency; exactly one of rate / total_revenue required |
| total_revenue | room revenue for the whole stay; rate = total_revenue / nights when nights > 0 |
| currency | ISO code; defaults to hotel.json currency. More than one currency in a file is an error unless hotel.json carries an `fx` table to the hotel currency |
| status_date | cancel date for cancelled; check-out date for stayed; ignored for no_show (equals arrival) |
| rate_code | BAR, PROMO, CORP-xxx and so on; a mapping key |
| source | channel or agent name; a mapping key |
| room_type | kept and counted, not used by the engine yet |
| company | company, travel agent or block code; used to recognise groups |
| updated_on | YYYY-MM-DD, the PMS "last updated" date; upper bound when a cancel date has to be imputed |
| rooms | integer >= 1, default 1; rows with rooms > 1 are expanded into one row per room |

`hotel.json`:

```json
{
  "name": "H1 Resort",
  "currency": "EUR",
  "fx": {"USD": 0.92},
  "sellable_rooms": null,
  "rates_include_tax": "unknown",
  "group_threshold_rooms": 10,
  "detect_groups": true,
  "rate_floor": 40, "rate_ceiling": 600,
  "segment_map_order": ["segment", "rate_code", "source"],
  "segment_map": {
    "segment":   {"Direct": "RETAIL", "Online TA": "OTA"},
    "rate_code": {"BAR": "RETAIL", "CORP-*": "CORP"},
    "source":    {"Booking.com": "OTA", "Walk-in": "RETAIL"}
  }
}
```

Mapping rules:

- Keys are tried in `segment_map_order`. A row falls through to the next
  key only when the value is empty or absent from that key's table. A
  `segment` column full of a default such as "DEFAULT" therefore does not
  win: "DEFAULT" is not in the table, so `rate_code` is consulted.
- A trailing `*` marks a prefix match (`CORP-*`). Without it the match is
  exact.
- A row that matches no key is an error. Unmapped values are reported as a
  list of distinct values with a row count each, separate from the row-level
  error list, so a new rate code on 2 percent of the file shows up as one
  line and does not use up the 50-error budget. There is no default segment.
- Targets are RETAIL, OTA, CORP, GROUP, and NONREV.

Group recognition runs after the mapping table and only over rows whose
target is not NONREV, so a complimentary room for a tour leader stays
NONREV here as it does in the converter. Rows with the same `company`,
`booked_on`, `arrival` and `nights` whose count reaches
`group_threshold_rooms` become GROUP: a group is one decision, so it is
entered on one day. `company` alone never makes a group; a corporate
account with hundreds of single bookings a year stays CORP, and ten
unrelated OTA guests through the same agent on the same arrival do not
become a group unless they were also booked on the same day. Empty
`company` never matches. `detect_groups: false` switches this off for
files whose converter has already decided groups, which is the case for
the public dataset; otherwise it would be clustered twice.

Segment meaning, by price behaviour rather than channel name: RETAIL floats
with BAR and books directly; OTA floats with BAR through a commissioned
channel; CORP pays a negotiated rate that does not move with BAR; GROUP is
many rooms in one decision, accepted or refused as a block; NONREV is a room
occupied without revenue (complimentary, house use, staff).

### NONREV and the ledger

`Ledger.rooms_on` reads its own `occ` counter, but `forecast.py` iterates
every key of `segment_mix` and `adr_on` divides all revenue by all occupied
rooms, so a NONREV segment inside the ledger would either raise on an
unknown segment or dilute ADR. NONREV rows are therefore not booked into the
ledger. The ingest keeps a separate per-night NONREV count and returns it
beside the ledger. Consequences, all stated in the report:

- Actuals used for scoring exclude NONREV rooms.
- Capacity used to clamp forecasts at scoring time is sellable rooms minus
  the final NONREV rooms held that night. A baseline that uses capacity
  while forecasting sees only the NONREV rooms already on the books at that
  lead; otherwise it would look into the future.
- NONREV rooms are never cut in the holdout.
- The engine compares `rooms_on` with `hotel.rooms` to decide whether a
  night was censored (`unconstrain.py`, `elasticity.py` with its sell-out
  threshold, `policy.py` for the expected sell-out). With NONREV outside
  the ledger, a night that was really full with 175 paid rooms and 5
  complimentary ones looks like 175 of 180 to the engine: not censored, so
  the unconstrainer does not lift demand on exactly the busiest nights, and
  the engine forecasts low there while the baselines, which never
  unconstrain, are untouched. That would be an ingest error scored against
  the engine. The report therefore prints the number of nights that were
  physically full (paid plus NONREV at or above sellable rooms) but that the
  ledger does not see as full.
- Rule fixed before the run: the audit prints NONREV rooms per night on
  nights at or above 90 percent physical occupancy (median and p90) and the
  number of full nights with at least one NONREV room. If that p90 exceeds 2
  percent of sellable rooms, the engine is run a second time with
  `hotel.rooms` set to sellable rooms minus the p90, and both runs are
  reported. This changes a configuration value, not engine code.
- A capacity block that removes rooms from sale while keeping revenue (an
  airline-crew contract) or dropping it (comp rooms) is recorded as an ADR
  with status Proposed and is not built here. The ADR also has to say which
  ADR definition the engine uses, with or without free rooms, because hotels
  report both ways and a comparison with a real hotel's report must use the
  same one. A free room for a group leader (adr = 0, still GROUP) lowers the
  group ADR a little; that is how hotels price groups and stays as is.

Two occupancy measures, and where each is used:

| measure | counts | used for |
|---|---|---|
| physical occupancy | paid rooms plus NONREV rooms stayed | inferring sellable rooms and the ceiling check; the over-capacity warning; the three seasons; full nights in table 1; clean nights in the holdout |
| paid demand | paid rooms only | actuals for scoring; known demand in the holdout; ADR |

A night at 80 percent paid plus 8 percent complimentary is constrained and
is not a clean night at an 85 percent threshold. Only NONREV rows with
status stayed or in_house occupy a room; cancelled or no-show NONREV rows
occupy nothing. Each NONREV row is kept with its `booked_on`, not folded
into a per-night total, because three consumers need different views:
scoring clamps with the final total (fair, every method is clamped alike);
the holdout uses the NONREV rooms on the books at the moment of each sale,
so a comp room entered after paid rooms reached the cap overshoots the cap
and neither evicts an accepted booking nor gets cut; and any baseline that
uses capacity while forecasting sees only NONREV entered up to that day.
Sellable-room inference reads the records, not the ledger, so NONREV rows
are counted; a function that inferred from the ledger would lose them.

### What the engine needs from hotel.json

The engine reads these `Hotel` fields: `rooms`, `base_rate`, `rate_floor`,
`rate_ceiling`, `rate_step`, `variable_cost`, `max_lead`, `max_los`, and
`rate_ladder`, which derives from them. `hotel.json` must supply them all.
For the public dataset the converter derives what it can and states the
rest: `base_rate` = median `adr` of Direct bookings in shoulder months for
the most common room type; floor and ceiling from the 2nd and 98th
percentile of paid `adr`; `rate_step` 1 EUR; `max_lead` from the 99th
percentile of `lead_time`; `max_los` from the 99th percentile of `nights`;
`variable_cost` is not in the data and is set to a stated assumption, which
touches GOPPAR-style figures only and none of the three tables.

Seasonality is the seam that has to open. `calendar.py` holds a fixed
`MONTH_FACTOR` table and fixed season bands for the simulated Toronto
hotel; `policy.py` uses them for the ladder prior and the rate ceiling
walk, `forecast.py` and `unconstrain.py` use `demand_class` (season band by
weekday) as the estimation cell, and `reference_rate` uses the month factor
as the denominator of every price ratio. Run unchanged on the Algarve, the
engine would normalise prices against Toronto's seasons and pool nights
into Toronto's demand classes. Required change, recorded first as an ADR
with status Proposed and then built as step 0 of section 7: the month
factors and season bands become per-hotel inputs carried in `hotel.json`,
with the current table as the default so the golden numbers do not move.
The converter derives them from the data: month factor = mean paid `adr` of
the month over the annual mean; season bands from terciles of monthly
physical occupancy, the same split table 1 uses. Nothing else in the engine
changes; a mechanism the pilot shows to be wrong is written up, not fixed.

### Validation rules

- Errors are collected, up to 50, then the run stops with a list of row
  numbers and columns. It never stops at the first bad row.
- Warnings never stop the run and are counted in the report: rate outside
  floor or ceiling (comp rooms, staff rates, long stays), rate <= 0,
  cancelled without status_date, status_date clamped, occupancy above
  sellable rooms on any night (duplicate rows or overbooking).
- `status_date` after `arrival` for a cancellation is clamped to `arrival`
  (late cancellation). `status_date` before `booked_on` is clamped to
  `booked_on`. Both counts are reported.
- `stayed` needs no `status_date`; `nights` is nights actually stayed.

### Cancelled rows without a cancel date

Dropping such a row from snapshots gives the same numbers as assuming it
cancelled on its booking day, so "exclude" is not a neutral choice. Rule:
impute the cancel date by drawing, among dated cancellations of the same
mapped segment, the ratio of days-to-cancel over lead time (a number in 0
to 1), and applying it to this row's own lead, so a lead-5 booking never
receives a 60-day delay that is then clamped to arrival and drags the
distribution toward the arrival date. When `updated_on` is present it is
the upper bound instead of arrival. The draw is seeded, so the run is
reproducible. The report prints the share of
imputed rows and two bounds for every affected snapshot statistic: all such
rows cancelled on booking day (low) and all cancelled on arrival (high). If a
segment has no dated cancellations to learn from, the low bound is used and
the report says so. The public dataset is unaffected: every cancellation
carries a date.

### Sellable rooms

`sellable_rooms` is asked for first and is the normal path. It means rooms
that can be sold, not physical rooms. When it is null the ingest infers it as
the maximum concurrent occupancy counting `stayed` and `in_house` rows only
(never `no_show`, never day use), then checks how many nights sit within 2
percent of that maximum. Fourteen nights at 178 to 180 make 180 believable;
a second highest night at 150 makes 180 a one-off and the warning is loud.
The inferred number is biased low by construction, so the report's first
line says the count is inferred and how many nights touch the ceiling.

### Replay into the ledger

`replay(records, hotel, first_stay, last_stay)` walks one calendar day at a
time from the earliest `booked_on`: books every row entered that day,
cancels every row whose (clamped or imputed) cancel date is that day, calls
`Ledger.snapshot`, and settles nights that have passed. Observable denials
are zero, as EXTENDING.md already promises for systems that do not log them.

Known limits, stated in the report: an export holds the final state of each
booking, so a guest who moved dates is replayed onto the new dates from the
original booking day; pickup reflects final state, not the state on the day
of booking. Group `booked_on` is often the rooming-list entry date rather
than the contract date, and unpicked blocks are absent, so GROUP snapshots at
long leads are low and nothing about group pickup should be concluded from
this source.

## 4. Converter for the public dataset

Input: the TidyTuesday `hotels.csv` (119,390 rows: 40,060 H1 resort in the
Algarve, 79,330 H2 city in Lisbon). Output: `data/antonio/h1-bookings.csv`,
`h2-bookings.csv`, `h1-hotel.json`, `h2-hotel.json`, and `audit.md`.

Row mapping:

- `booking_id` = hotel code plus row number (`H1-000123`). Duplicated rows
  are kept for one reason only: the data is anonymised, so an identical row
  cannot be told apart from a distinct room. If they are real rooms,
  dropping them lowers the busiest night; if they are data errors, keeping
  them inflates the inferred room count. The audit prints the inferred room
  count with and without duplicates, and duplicates split by status (a
  duplicated cancellation never touches the busiest night). The converter
  says so in its header comment.
- `booked_on` = arrival minus `lead_time`. `nights` = weekend nights plus
  week nights, zero kept as day use. `rooms` = 1.
- Status from `reservation_status`, never from `is_canceled` (no-shows also
  carry `is_canceled` = 1): Check-Out to stayed, No-Show to no_show, Canceled
  to cancelled with `status_date` = `reservation_status_date`.
- `rate` = `adr`, currency EUR. `room_type` = `reserved_room_type`,
  `source` = `distribution_channel`, `company` = company else agent.
- The `segment` column carries the code of the rule branch the converter
  applied, not the raw market segment: `COMP`, `ADR0`, `TP_CLUSTER`,
  `GROUPS`, `OFFLINE_TO_GROUP`, `OFFLINE_TO_CONTRACT`,
  `OFFLINE_TO_TRANSIENT`, `DIRECT`, `ONLINE_TA`, `CORPORATE`, `AVIATION`,
  `UNDEFINED_BY_CHANNEL`, `UNDEFINED_FALLBACK`. `hotel.json` maps each
  branch one-to-one to a Pace target. A key such as
  `Online TA|Transient` could not do this, because two rows with the same
  key can land in different targets (one with adr = 0 becomes NONREV,
  another OTA) and cluster or BAR-test outcomes depend on other rows. The
  branch code also keeps the origin the pilot's "close cheap first" rule
  needs, and the audit's rows-per-branch table is the same count.
- `first_stay` = 1 July 2015 plus the 99th percentile of `nights`, that is,
  later than the first arrival, because guests who arrived before the window
  and were still in house are missing. `last_stay` never exceeds 31 August
  2017.

Segment rules, applied in this order:

1. `market_segment` Complementary: NONREV.
2. Group clustering: Transient-party rows are clustered on hotel,
   `market_segment`, `arrival`, `nights`, `lead_time`, `agent` and `company`.
   The pair (`agent`, `company`) may not be empty on both sides; an empty
   field does match another empty field as long as the other field of the
   pair is present and equal (agent 9 with no company clusters with agent 9
   with no company). The fixture has exactly this case. A cluster
   at or above `group_threshold_rooms` becomes GROUP. `market_segment`
   Groups and Offline TA/TO with customer_type Group are GROUP directly.
3. `adr` = 0 with `nights` > 0, status stayed or no_show, and mapped target
   not GROUP: NONREV. Group members billed on a master folio keep their
   segment. Cancelled rows are never moved to NONREV, so cancellation rates
   by segment stay intact.
4. The rest of the table:

| in the data | Pace |
|---|---|
| Direct | RETAIL |
| Online TA | OTA |
| Corporate | CORP |
| Aviation | CORP, provided the audit confirms it is not a standing block |
| Offline TA/TO, customer_type Contract | CORP |
| Offline TA/TO, customer_type Transient | CORP by default; the BAR test below may move it to OTA |
| Undefined | the majority target of the same `distribution_channel` in the same hotel, read from the audit's market_segment by channel cross-table; if the channel is also Undefined, RETAIL with a warning |

Rule carried to real hotels: if Undefined exceeds 1 percent of room nights,
the converter stops and asks rather than assigning a default.

BAR test for Offline TA/TO Transient, per hotel: restrict both sides to one
meal plan (the most common, expected BB) and two adults, so board and
occupancy do not add noise; take weekly mean `adr` by room type for that
cell and for Direct plus Online TA; remove each series' monthly mean, so
that a stepped seasonal contract does not correlate with BAR merely because
both follow the season. Correlation of the residuals above 0.6 (fixed
before the run) maps the cell to OTA; otherwise CORP. Stability check: the
verdict must be the same when the mean is removed by two-week blocks
instead of by month, since a contract step on the 15th survives monthly
demeaning; if the two disagree the cell stays CORP and the report says the
test was inconclusive. The correlations, the threshold and the number of
weeks with enough rows are printed.

Audit printed before writing anything, and copied into the pilot report:

- rows per rule and branch above, including how many `adr` = 0 rows went
  where and how many rows each Transient-party cluster rule caught;
- the market_segment by distribution_channel cross-table per hotel;
- Aviation: rows, and the number of nights with at least one Aviation room;
- duplicate rows: count, split by status, how many share `agent` or
  `company` with another row on the same arrival (a check on the group
  explanation), and the inferred room count with and without them;
- meal check: `adr` of HB against BB for the same room type and arrival
  week; a steady, large gap means `adr` includes meals;
- cancellation rate with and without `deposit_type` = Non Refund;
- clamped cancel dates, `adr` <= 0 and extreme `adr`, all counted;
- `lead_time` distribution per hotel, to justify the lead marks in section 5.

`sellable_rooms` is null for both hotels. `rates_include_tax` is "unknown".
The dataset has no rate codes, so any rule that mentions promotional rate
codes does not apply to it and the report says so.

## 5. The pilot command and report

`run.py pilot <bookings.csv> <hotel.json> [--out out/]`. Ingest, then a
forward walk. Warm-up is the first six settled months, roughly July to
December 2015; forecasting starts about January 2016, and from then on every
forecast for a night uses only snapshots up to that day. Baselines built on
a trailing window learn the first summer's pickup from winter weeks; the
report says so where it applies.

Every table compares only nights on which every method produced a forecast,
and prints that count.

### Table 1, forecast accuracy

Lead marks 120 (H1 only, if the audit's lead-time distribution supports
it), 90, 60, 30, 14 and 7. Lead 1 is a separate line labelled as measuring
late cancellations and no-shows, an overbooking question, not a demand
question.

For each mark and method: rooms forecast for the night versus rooms actually
stayed, NONREV excluded. Forecasts are clamped to capacity (sellable rooms
minus NONREV held) before scoring, because on a full night an unconstrained
forecast above capacity is not wrong. Reported per method: mean absolute
error in rooms and as a share of capacity, signed error (bias), and the
share of forecasts that hit the clamp, so a method that explodes upward and
is rescued by the clamp is visible.

Cuts: three seasons per hotel from terciles of monthly occupancy (high,
shoulder, low), with Easter, Christmas and New Year flagged separately; and
full nights, labelled as full against the inferred room count.

Methods:

- Engine (`Forecaster` as it runs today).
- Additive pickup, the primary baseline: rooms on the books now plus the
  mean rooms still to come from this lead, over the trailing 10 weeks of the
  same weekday (fixed before the run, not tuned afterwards).
- Multiplicative pickup: rooms on the books divided by the mean share sold
  at this lead, same window. Expected to explode at long leads; kept because
  it is common.
- Same time last year, additive: last year's final rooms aligned by weekday
  (364 days back), plus (rooms on the books today minus rooms on the books at
  the same lead last year). The ratio form was rejected because its
  denominator is tiny at long leads. Available only from July 2016.
- Average of additive pickup and additive last year: the forecast a good
  revenue manager builds in Excel, and the engine's real opponent.

Headline rules:

- The headline is engine against the average, and it rests on about 14
  months (July 2016 to August 2017), one high season per hotel. The report
  says so next to the number.
- A secondary comparison, engine against additive pickup over the whole
  forecast period, is printed so the first half of the data is not lost.
- At any lead mark where a single baseline beats the average, that baseline
  is printed beside it. The headline never compares against the average
  alone.

### Table 2, recommended rate against realised rate

At leads 60 (H1), 30, 14 and 7: the engine's recommended BAR against the
`adr` of RETAIL and OTA bookings made in a window around that lead (for mark
14, bookings with lead 21 to 7), restricted to the most common
`reserved_room_type` and split by meal plan, so the gap reflects price and
not room or board mix. Distribution of the gap per season.

Fixed wording next to the table: this is a comparison, not evidence of
revenue, because demand at any other rate was never observed; and if Online
TA `adr` is a net rate the OTA gap is biased by a constant. Nights with
NONREV rooms are flagged and their gap shown separately, because the engine
sees more rooms free than there were and may recommend a lower BAR on
exactly the nights that were nearly full.

### Table 3, unconstraining checked by holdout

Clean nights: occupancy below the clean threshold, and no night at or above
it within a window around it (window = 90th percentile of `nights`), because
a stay spanning a full night is refused for all its nights.

Combinations run and reported, each with its count of cut nights: clean
threshold 80, 85 and 90 percent, crossed with simulated cap 60, 70 and 80
percent of capacity, keeping only pairs where the cap is below the
threshold. If the answer moves a lot between combinations, the settings are
deciding the result.

The cap is applied to the whole history the unconstrainer learns from, not
only to scored nights, so it cannot peek at uncut neighbours. Two cutting
rules:

- Sell until full: a booking is refused if any night it covers has reached
  the cap, and loses all its nights. Cancellations return rooms on their
  cancel date and later bookings may fill them. Groups are refused whole.
- Close cheap first, primary form: at 90 percent of the cap, close every
  row whose origin is Offline TA/TO, contract and transient alike, because
  tour-operator allotments carry the lowest net rates and are the first to
  be stop-sold. OTA, RETAIL and Corporate stay open to the cap: near full a
  hotel raises BAR and the OTA price follows, and corporate contracts often
  carry last-room availability. A GROUP that would push a night past 90
  percent is refused whole. Promotional and non-refundable rate codes would
  close at the same point where a dataset has them; this one does not.
- Close cheap first, secondary form: as above and OTA closes too, for hotels
  with a channel-closing policy. Reported beside the primary form.

Scored only on nights that were actually cut, grouped by how much was cut
(under 5 percent, 5 to 15, over 15). Reported: estimated versus known
demand, overall and by segment. Stated limit: refused guests are assumed to
vanish, while some would move to another night of the same hotel.

### Report

`out/pilot-<hotel>.md` opens with three lines: the room count is inferred,
with the count of nights touching the ceiling; rates are of unknown tax and
meal treatment, with the meal-check result; there is no booking change
history, so pickup reflects final state. Then the audit, then tables 1 to 3
for H1 and H2 side by side. `out/pilot-<hotel>.json` holds the same numbers.
No number is rounded in the engine's favour; wins and losses against each
baseline are printed as they fall.

If the engine shows a weakness on real data, it is recorded as an ADR with
status Proposed. Nothing in the engine changes inside this project.

## 6. Testing

- `tests/test_ingest.py`: a hand-built 30-row log covering every status,
  day use, a multi-room row, a cancelled row without date (imputation and
  both bounds), a clamped cancel date, a NONREV row, a "DEFAULT" segment
  that falls through to rate_code, a prefix rate code, a company with many
  single bookings that must not become a group, ten same-agent OTA rows on
  one arrival booked on different days that must not become a group,
  `detect_groups: false`, an unmapped value reported once with its row count,
  mixed currency with and
  without an fx table, and 60 deliberate errors to prove collection stops at
  50. Asserts nightly `rooms_on`, snapshots at leads 0, 7 and 30,
  `seg_revenue` per segment, the separate NONREV count with its dates, and
  sellable-room inference with its ceiling check. Invariants: per night,
  paid rooms in the ledger plus NONREV rooms equals the count of stayed
  rows; the inferred room count includes NONREV rooms; a night that is full
  only thanks to comp rooms is marked full for table 1 and is not a clean
  night for the holdout.
- `tests/test_convert_antonio.py`: a 40-row fixture in the public dataset's
  layout exercising every rule in order and asserting the branch code each
  row receives, the Transient-party clustering with agent present and
  company empty on both rows (must cluster) and with both empty (must not),
  the adr = 0 rule on a GROUP member and on a cancelled row (both keep their
  segment), Undefined by
  channel majority, status from `reservation_status`, window trimming, and
  the residual-correlation BAR test on a constructed stepped contract that
  must map to CORP. The real file is not in the repository.
- `tests/test_pilot.py`: baselines and scoring on a tiny synthetic ledger
  with known answers; the clamp and the clamp-rate statistic; the holdout
  cut under all three rules including a cancellation that frees a room, a
  group refused whole at the 90 percent mark, a comp room entered after the
  cap was reached (overshoots, evicts nothing, is not cut), and a baseline
  that must not see a NONREV room entered after its forecast day.
- Golden numbers are untouched; `run.py test` must stay green and the quick
  build must still print 64.50 / 78.80 / 82.98.

## 7. Order of work

0. ADR Proposed for per-hotel seasonality, then the configuration seam in
   `calendar.py` with the current table as default; golden numbers rerun.
1. Schema document `docs/booking-log.md` and sample `data/sample-bookings.csv`.
2. `pace/ingest.py` with tests.
3. Converter with fixture tests; download of `hotels.csv` (about 16 MB from
   raw.githubusercontent.com, TidyTuesday 2020-02-11) happens here, with
   Elle's explicit go-ahead, and the file stays out of git.
4. Baselines and scoring, then the holdout, then the report writer.
5. Run on H1 and H2, read the audit, write the report, and open ADRs for
   whatever the engine got wrong, plus the capacity-block ADR.
6. README and EXTENDING: point the PMS section at the schema and the
   converter; CLAUDE.md gains the `pilot` command.

## 8. Recollections to verify in the audit, and open items

Recollections, not facts until the audit prints them:

- Most duplicated rows are rooms of one group.
- Aviation is a few hundred rows, under one room a night on average.
- Bookings with `deposit_type` Non Refund cancel almost entirely.
- Mean `lead_time` is near three months; at least one `adr` is negative and
  one is in the thousands.
- Complementary is about 700 rows and the adr = 0 branch is larger than
  that; the impact measure that matters is NONREV rooms on near-full nights,
  not the row share.
- Whether `adr` includes tax or meals.

Open items:

- Real PMS column names (Opera, ezCloud, Smile) are unverified; the schema
  is locked only after one or two real exports.
- A standing airline-crew block, and NONREV rooms generally, need a capacity
  block the engine does not have. ADR Proposed, not built here.
- `variable_cost` for the Portuguese hotels is an assumption; it affects no
  table in the report.
- The engine's four segments carry fixed `rate_multiplier` and
  `prior_elasticity` values tuned for the simulated hotel (`config.py`). The
  audit prints realised segment ADR ratios so the gap to those multipliers is
  visible; changing them is an engine calibration and belongs in an ADR, not
  in this project.
