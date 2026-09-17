# Real data in, honest report out: booking-log ingest and the public-data pilot

Status: Draft for review, 2026-09-17. Plan phases 5a and 5b of
`Pace-RMS-Hardening-Plan.md`. Reviewed line by line with a revenue manager;
every rule below that reads like hotel practice came from that review.

## 1. Goal and non-goals

Pace has only ever seen its own simulated market. This work gives it a door
for real booking history and runs the first real-data pilot on a public
dataset, because no hotel has yet shared data.

What the pilot can honestly measure on real history: forecast accuracy
against the methods a revenue manager uses without an engine, how the
engine's recommended rate compares with what was actually charged, and how
well unconstraining recovers demand that was hidden by a capacity cap we
impose ourselves. What it cannot measure: a RevPAR lift. History only records
what sold at the rate that was charged. No counterfactual policy can be
replayed on it, and the report says so in its first lines.

Non-goals: changing the engine, adding a fifth demand segment to the engine,
calibrating the simulator to real data (that is a different project and a
different kind of claim, see ADR 0006), and any machine learning.

## 2. Shape of the work

Three units, each testable alone:

1. `pace/ingest.py`: reads Pace's own booking-log CSV plus a `hotel.json`,
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

One row per booking (one room; multi-room bookings are expanded to one row
per room by the converter). Required columns:

| column | type | rule |
|---|---|---|
| booking_id | string | unique per row; converters may synthesise it |
| booked_on | YYYY-MM-DD | date the booking entered the PMS |
| arrival | YYYY-MM-DD | |
| nights | integer >= 0 | 0 is day use; exactly one of nights / departure required |
| segment | string | hotel's own code, mapped via hotel.json |
| status | booked / in_house / stayed / cancelled / no_show | in_house counts as stayed |

Optional columns:

| column | rule |
|---|---|
| departure | YYYY-MM-DD; nights = departure minus arrival when nights is absent |
| rate | per room night, in the file's currency; exactly one of rate / total_revenue required |
| total_revenue | room revenue for the whole stay; rate = total_revenue / nights (nights > 0) |
| currency | ISO code; defaults to hotel.json currency; a file may mix currencies only if this column is present |
| status_date | cancel date for cancelled; check-out date for stayed; ignored for no_show (equals arrival) |
| rate_code | BAR, PROMO, CORP-xxx and so on; second key for mapping |
| source | channel or agent name; third key for mapping |
| room_type | kept and counted, not used by the engine yet |
| company | company, travel agent or block code; used to recognise groups |
| rooms | integer >= 1, default 1; rows with rooms > 1 are expanded |

`hotel.json`:

```json
{
  "name": "H1 Resort",
  "currency": "EUR",
  "sellable_rooms": null,
  "rates_include_tax": "unknown",
  "group_threshold_rooms": 10,
  "rate_floor": 40, "rate_ceiling": 600,
  "segment_map": {
    "segment":   {"Direct": "RETAIL", "Online TA": "OTA"},
    "rate_code": {"BAR": "RETAIL", "CORP-": "CORP"},
    "source":    {"Booking.com": "OTA", "Walk-in": "RETAIL"}
  }
}
```

Mapping order per row: `segment`, then `rate_code`, then `source`. A prefix
match is allowed for rate codes (`CORP-`). Targets are RETAIL, OTA, CORP,
GROUP, and NONREV. NONREV rows (complimentary, house use, staff) occupy a
room in `rooms_on` but carry no revenue and never enter a segment forecast.
A test asserts that a NONREV booking changes `rooms_on` and leaves
`seg_rooms`, `seg_revenue` and the segment mix for the four real segments
unchanged.

Segment meaning, by price behaviour rather than channel name: RETAIL floats
with BAR and books directly; OTA floats with BAR through a commissioned
channel; CORP pays a negotiated rate that does not move with BAR; GROUP is
many rooms in one decision, accepted or refused as a block.

### Validation rules

- Errors are collected, up to 50, then the run stops with a list of row
  numbers and columns. It never stops at the first bad row.
- Warnings never stop the run and are counted in the report: rate outside
  floor or ceiling (comp rooms, staff rates, long stays), rate <= 0,
  cancelled without status_date, status_date clamped, occupancy above
  sellable rooms on any night.
- `cancelled` with no `status_date`: kept for the cancellation rate, excluded
  from snapshots, and the share of such rows is printed. Assuming cancel on
  booking day understates every snapshot; assuming cancel on arrival
  overstates them; neither is honest.
- `status_date` after `arrival` for a cancellation is clamped to `arrival`
  (late cancellation). `status_date` before `booked_on` is clamped to
  `booked_on`. Both counts are reported.
- `stayed` needs no `status_date`; `nights` is nights actually stayed.

### Sellable rooms

`sellable_rooms` is asked for first and is the normal path. It means rooms
that can be sold, not physical rooms. When it is null the ingest infers it as
the maximum concurrent occupancy counting `stayed` and `in_house` rows only
(never `no_show`), and then checks how many nights sit within 2 percent of
that maximum. Fourteen nights at 178 to 180 make 180 believable; a second
highest night at 150 makes 180 a one-off and the warning is loud. The
inferred number is biased low by construction, so the report's first line
says the count is inferred and how many nights touch the ceiling.

### Replay into the ledger

`replay(records, hotel, first_stay, last_stay)` walks one calendar day at a
time from the earliest `booked_on`: books every row entered that day, cancels
every row whose clamped cancel date is that day, calls `Ledger.snapshot`,
and settles nights that have passed. Groups recognised via `company` or the
Transient-party clustering (section 4) are booked as GROUP rows. Observable
denials are zero, as EXTENDING.md already promises for systems that do not
log them.

Known limit, stated in the report: an export holds the final state of each
booking, so a guest who moved dates is replayed onto the new dates from the
original booking day. Pickup therefore reflects final state, not the state
on the day of booking. Group `booked_on` is often the rooming-list entry date
rather than the contract date, and unpicked blocks are absent, so GROUP
snapshots at long leads are low and nothing about group pickup should be
concluded from this source.

## 4. Converter for the public dataset

Input: the TidyTuesday `hotels.csv` (119,390 rows: 40,060 H1 resort in the
Algarve, 79,330 H2 city in Lisbon). Output: `data/antonio/h1-bookings.csv`,
`h2-bookings.csv`, `h1-hotel.json`, `h2-hotel.json`, and `audit.md`.

Row mapping:

- `booking_id` = hotel code plus row number (`H1-000123`). Duplicated rows
  are kept on purpose: the data is anonymised and most duplicates are rooms
  of one group. The converter says so in its header comment and in the audit.
- `booked_on` = arrival minus `lead_time`. `nights` = weekend nights plus
  week nights, zero kept as day use. `rooms` = 1.
- Status from `reservation_status`, never from `is_canceled` (no-shows also
  carry `is_canceled` = 1): Check-Out to stayed, No-Show to no_show, Canceled
  to cancelled with `status_date` = `reservation_status_date`.
- `rate` = `adr`, currency EUR. `room_type` = `reserved_room_type`,
  `source` = `distribution_channel`, `company` = company else agent.
- `first_stay` is pulled forward from 1 July 2015 by the 99th percentile of
  `nights`, because guests who arrived before the window and were still in
  house are missing. `last_stay` never exceeds 31 August 2017.

Segment mapping, by `market_segment` and `customer_type`:

| in the data | Pace |
|---|---|
| Direct | RETAIL |
| Online TA | OTA |
| Corporate | CORP |
| Aviation | CORP (a few hundred rows, under one room a night, not a block) |
| Groups | GROUP |
| Offline TA/TO, customer_type Group | GROUP |
| Offline TA/TO, customer_type Contract | CORP |
| Offline TA/TO, customer_type Transient | CORP by default; see the BAR test below |
| Complementary | NONREV |
| adr = 0, nights > 0, market_segment not Groups | NONREV (group members on a master folio keep their segment) |
| Undefined | by distribution_channel: Direct to RETAIL, Corporate or GDS to CORP, TA/TO to OTA; channel also Undefined then RETAIL with a warning |

Transient-party rows are clustered on hotel, arrival, nights, lead_time,
agent and company. A cluster at or above `group_threshold_rooms` becomes
GROUP; below it each row is treated as Transient of its own market segment.

BAR test for Offline TA/TO Transient, run per hotel: take mean `adr` by
arrival week and room type for that cell and for Direct plus Online TA. High
correlation across weeks means the cell floats with BAR and maps to OTA; a
flat seasonal profile means contracted and maps to CORP. The threshold and
the correlation are printed; when inconclusive the default is CORP.

Audit printed before writing anything, and copied into the pilot report:

- rows per mapping branch, including how many adr = 0 rows went where;
- Aviation: number of nights with at least one Aviation room, to confirm it
  is not a standing block;
- meal check: `adr` of HB against BB for the same room type and arrival
  week; a steady, large gap means `adr` includes meals;
- cancellation rate with and without `deposit_type` = Non Refund, because
  that cohort cancels almost entirely and looks like agent allotments being
  released rather than guest behaviour;
- clamped cancel dates, negative or extreme `adr`, both counted;
- `lead_time` distribution per hotel, to justify the lead marks in section 5.

`sellable_rooms` is null for both hotels. `rates_include_tax` is "unknown".

## 5. The pilot command and report

`run.py pilot <bookings.csv> <hotel.json> [--out out/]`. Ingest, then a
forward walk: the engine's first forecast day comes after six settled months
of history; from then on every forecast for a night uses only snapshots up to
that day. Six months of warm-up beginning in early 2016 means the first
summer's pickup is learned from winter months, and the report says so.

Every table compares only nights on which every method produced a forecast,
and prints that count.

### Table 1, forecast accuracy

Lead marks 120 (H1 only, if the data supports it), 90, 60, 30, 14 and 7.
Lead 1 is a separate line labelled as measuring late cancellations and
no-shows, an overbooking question, not a demand question.

For each mark and method: rooms forecast for the night versus rooms actually
stayed. Forecasts are clamped to sellable rooms before scoring, because on a
full night an unconstrained forecast above capacity is not wrong. Reported:
mean absolute error in rooms and as a share of capacity, and signed error
(bias), since a forecast that is always high keeps rates too high and one
that is always low sells full nights cheap.

Cuts: three seasons per hotel from the tercile of monthly occupancy (high,
shoulder, low), with Easter, Christmas and New Year flagged separately; and
full nights, labelled as full against the inferred room count.

Methods:

- Engine (`Forecaster` as it runs today).
- Additive pickup, the primary baseline: rooms on the books now plus the
  mean rooms still to come from this lead, taken over the last 8 to 12 weeks
  of the same weekday.
- Multiplicative pickup: rooms on the books divided by the mean share sold
  at this lead, same window. Reported because it is common, and expected to
  blow up at long leads when few rooms are on the books.
- Same time last year, pace-adjusted: last year's final rooms, aligned by
  weekday (364 days back), times rooms on the books today over rooms on the
  books at the same lead last year. Available only from July 2016.
- Average of additive pickup and pace-adjusted last year. This is the
  engine's real opponent: the forecast a good revenue manager builds in
  Excel. If the engine does not beat it, that is the pilot's headline.

### Table 2, recommended rate against realised rate

At leads 60 (H1), 30, 14 and 7: the engine's recommended BAR against the
`adr` of RETAIL and OTA bookings made in a window around that lead (for mark
14, bookings with lead 21 to 7), restricted to the most common
`reserved_room_type` and split by meal plan, so the gap reflects price and
not room or board mix. Distribution of the gap per season.

Fixed wording next to the table: this is a comparison, not evidence of
revenue, because demand at any other rate was never observed; and if Online
TA `adr` is a net rate the OTA gap is biased by a constant.

### Table 3, unconstraining checked by holdout

Clean nights: occupancy below the threshold, and no night at or above the
threshold within a window around it (window = 90th percentile of `nights`),
because a stay spanning a full night is refused for all its nights. Run at
thresholds 80, 85 and 90 percent and report all three; if the answer moves a
lot between them the threshold is deciding the result.

Simulated cap, applied to the whole history the unconstrainer learns from,
not only to scored nights, so it cannot peek at uncut neighbours. Two
cutting rules:

- Sell until full: a booking is refused if any night it covers has reached
  the cap, and loses all its nights. Cancellations return rooms on their
  cancel date and later bookings may fill them. Groups are refused whole.
- Close cheap first: OTA and promotional rate codes close at 90 percent of
  the cap, CORP and RETAIL stay open until the cap. This is how a revenue
  manager actually sells the last rooms; an unconstrainer that is right only
  under the first rule will be wrong by segment on real data.

Scored only on nights that were actually cut, grouped by how much was cut
(under 5 percent, 5 to 15, over 15). Reported: estimated versus known demand,
overall and by segment. Stated limit: refused guests are assumed to vanish,
while some would move to another night of the same hotel.

### Report

`out/pilot-<hotel>.md` opens with three lines: the room count is inferred,
with the count of nights touching the ceiling; rates are of unknown tax and
meal treatment, with the meal-check result; there is no booking change
history, so pickup reflects final state. Then the audit, then tables 1 to 3
for H1 and H2 side by side. `out/pilot-<hotel>.json` holds the same numbers
for the dashboard or a later notebook. No number in the report is rounded in
the engine's favour; wins and losses against each baseline are printed as
they fall.

If the engine shows a weakness on real data, it is recorded as an ADR with
status Proposed. Nothing in the engine changes inside this project.

## 6. Testing

- `tests/test_ingest.py`: a hand-built 30-row log covering every status,
  day use, a multi-room row, a cancelled row without date, a clamped cancel
  date, a NONREV row, mixed currency with and without the column, and 60
  deliberate errors to prove collection stops at 50. Asserts nightly
  `rooms_on`, snapshots at leads 0, 7 and 30, `seg_revenue` per segment,
  sellable-room inference and its ceiling check, and the NONREV invariant.
- `tests/test_convert_antonio.py`: a 40-row fixture in the public dataset's
  layout exercising every mapping branch, the Transient-party clustering,
  status from `reservation_status`, and window trimming. The real file is not
  in the repository.
- `tests/test_pilot.py`: baselines and scoring on a tiny synthetic ledger
  with known answers; the clamp at capacity; the holdout cut under both rules
  including a cancellation that frees a room.
- Golden numbers are untouched; `run.py test` must stay green and the quick
  build must still print 64.50 / 78.80 / 82.98.

## 7. Order of work

1. Schema document `docs/booking-log.md` and sample `data/sample-bookings.csv`.
2. `pace/ingest.py` with tests.
3. Converter with fixture tests; download of `hotels.csv` (about 16 MB from
   raw.githubusercontent.com, TidyTuesday 2020-02-11) happens here, with
   Elle's explicit go-ahead, and the file stays out of git.
4. Baselines and scoring, then the holdout, then the report writer.
5. Run on H1 and H2, read the audit, write the report, and open ADRs for
   whatever the engine got wrong.
6. README and EXTENDING: point the PMS section at the schema and the
   converter; CLAUDE.md gains the `pilot` command.

## 8. Open items

- Real PMS column names (Opera, ezCloud, Smile) are unverified; the schema
  is locked only after one or two real exports.
- Whether `adr` in the public dataset includes tax and meals: answered by
  the audit, not assumed.
- Mean `lead_time` near three months and a negative `adr` are recollections
  to verify when the data is on disk.
- A standing airline-crew block (rooms removed from capacity but paid) has
  no home in the engine. Not needed for this dataset; noted for a real hotel.
