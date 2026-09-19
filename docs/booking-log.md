# Booking log, schema version 0

This is the file to send a hotel when asking for data, and the only format
`pace/ingest.py` reads. Version 0 means the column names have not yet been
checked against a real Opera, ezCloud or Smile export; the first two real
exports will fix them. One row per room. Dates are YYYY-MM-DD. Rates are per
room night before tax unless `rates_include_tax` in hotel.json says otherwise.

One row per room. Multi-room rows are expanded by the ingest. Required:

| column | type | rule |
|---|---|---|
| booking_id | string | unique per row; converters may synthesise it |
| booked_on | YYYY-MM-DD | date the booking entered the PMS |
| arrival | YYYY-MM-DD | |
| nights | integer >= 0 | 0 is day use; give nights or departure, or both; when both are present they must agree |
| segment | string | hotel's own code, mapped via hotel.json |
| status | booked / in_house / stayed / cancelled / no_show | in_house counts as stayed |

Optional:

| column | rule |
|---|---|
| departure | YYYY-MM-DD; nights = departure minus arrival when nights is absent |
| rate | per room night, in the file's currency; give rate or total_revenue, or both; when both are present they must agree |
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

This is the whole file, not a sketch of it. Every key below is required on the
ingest path, and the loader stops with a list of what is missing rather than
falling back to another hotel's seasons, contract ratios or walk cost (ADR
0007). It is `data/sample-hotel.json` in this repository, so it can be copied
and edited rather than typed. Change the numbers and the tables, keep the keys.

```json
{
  "name": "Sample Hotel",
  "city": "Porto",
  "currency": "EUR",
  "fx": {"USD": 0.92},
  "sellable_rooms": 40,
  "rates_include_tax": "no",
  "group_threshold_rooms": 5,
  "detect_groups": true,
  "rate_floor": 60,
  "rate_ceiling": 400,
  "rate_step": 4,
  "base_rate": 140,
  "variable_cost": 18,
  "walk_cost": 420,
  "max_lead": 180,
  "max_los": 7,
  "max_overbook_pct": 0.05,
  "sellout_threshold": 0.97,
  "price_month_factor": {
    "1": 0.8, "2": 0.8, "3": 0.9, "4": 1.0, "5": 1.05, "6": 1.15,
    "7": 1.25, "8": 1.25, "9": 1.1, "10": 1.0, "11": 0.85, "12": 0.85
  },
  "demand_season_band": {
    "1": "trough", "2": "trough", "3": "trough", "4": "shoulder", "5": "shoulder",
    "6": "peak", "7": "peak", "8": "peak", "9": "peak", "10": "shoulder", "11": "trough", "12": "trough"
  },
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

Two of these deserve a word. `walk_cost` is what it costs to relocate one guest
whose room was sold twice: a room bought elsewhere at short notice, the
transport there, and the goodwill afterwards. The engine reads it for every
overbooking decision, so a wrong currency or a placeholder here is felt on
every night. `max_overbook_pct` is the cap on that authorisation, as a share of
`sellable_rooms`. Neither can be read out of booking history, so both are
stated here or the file does not load.

`sellable_rooms` may be `null`, and only that one field may be: the ingest then
infers it (see below). Everything else must carry a real value.

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

NONREV rows are therefore not booked into the ledger. The ingest keeps a
separate per-night NONREV count and returns it beside the ledger.

### Validation rules

- Errors are collected, up to 50, then the run stops with a list of row
  numbers and columns. It never stops at the first bad row.
- A `booking_id` that is empty is an error in its own right, not a duplicate
  of the previous empty one. A file with no booking rows at all under an
  acceptable header is an error too, with a sentence saying so.
- Warnings never stop the run and are counted in the report: rate outside
  floor or ceiling (comp rooms, staff rates, long stays), rate <= 0,
  cancelled without status_date, status_date clamped, occupancy above
  sellable rooms on any night (`over_capacity_nights`, with the rooms that
  left counted beside it as `rooms_walked_off_the_actuals`).
- `status_date` after `arrival` for a cancellation is clamped to `arrival`
  (late cancellation). `status_date` before `booked_on` is clamped to
  `booked_on`. Both counts are reported.
- `stayed` needs no `status_date`; `nights` is nights actually stayed.

### Nights that go over the room count

Read this before sending a file, because it changes numbers you will recognise.

When the rooms occupying a night exceed `sellable_rooms`, the ledger walks the
excess: it takes those rooms and their revenue back out of that night, cheapest
rate first, exactly as it would have done had the engine authorised the
overbooking itself. On a real booking log that means the night comes back
**reduced**: fewer rooms sold and less revenue than the PMS recorded, because
the engine will not report a hotel as having sold more rooms than it says it
has. The report counts both the nights this happened on and the number of
rooms removed, so the size of the edit is visible rather than implied.

There are two reasons a night lands here, and they want opposite answers:

- `sellable_rooms` is too low. A hotel that sells 182 rooms on its best night
  and states 180 should state 182, or state the number it really sells. This
  is the common case, and the fix is the number, not the data.
- The hotel genuinely overbooked and walked guests, or the export carries
  duplicate rows. Then the reduction is telling the truth about the rooms and
  the warning count is what to reconcile against the PMS.

If neither is true and the actuals must come through untouched, raise
`sellable_rooms` to the highest occupancy in the file. Nothing else in the
ingest changes a number the hotel sent.

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

## Converters

`tools/convert_antonio.py` is the worked example: it turns a real public
dataset into a booking log and a `hotel.json` that follow this schema
exactly. A new hotel needs only a converter shaped like it.
