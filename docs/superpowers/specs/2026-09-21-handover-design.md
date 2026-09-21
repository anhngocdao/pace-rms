# The handover: one forecast, three departments

A hotel does not lack a forecast. The forecast exists, in the revenue
manager's spreadsheet, and it does not leave that room. Housekeeping rosters
against the rooms occupied today, which says nothing about the rooms that will
be booked this week. The kitchen orders against last week. The front desk
learns it is oversold when a guest is standing at the counter.

This design turns the engine's nightly forecast into the three answers those
departments actually need, and measures whether each answer held.

## 1. Scope

Three departments, in the two-block model the hotel itself uses.

| Block | Department | Question |
|---|---|---|
| Front of house | Front desk | Which nights am I at risk of having no room for a guest who holds a booking? |
| Back of house | Kitchen | How many covers? |
| Back of house | Housekeeping | How many rooms, of which kind? |

All three read one forecast, so the core is built once. A fourth department is
not in scope and the design does not prepare for one: a fourth output is a
fourth way to be misread, and the cost of adding it later is the cost of
adding it later.

**Out of scope.** Staffing minutes per room, the hotel's own labour standards,
any link to a PMS or a channel manager, any write path at all. The handover
reads and reports. It never prices, never authorises and never sends.

## 2. Two modes, proof first

**Proof mode** replays a window of real history. For every night in the
window, it produces what Pace would have told each department at each lead
mark, and sets it beside what actually happened. Everything here is
checkable, and nothing needs anyone's permission to run.

**Forward mode** takes a booking log exported up to today and answers for the
next fourteen nights. Nothing here is checkable, and the page says so.

The two modes share every computation. They differ only in where the walk
stops and whether an answer exists to compare against.

## 3. A schema change: guests

`pace/ingest.py`'s `Booking` carries `rooms` but not the number of people in
them. A kitchen needs covers, not rooms.

The public dataset has the field: across stayed bookings, the resort averages
1.94 guests per room and the city hotel 1.93. The converter drops it today.

`guests` becomes a schema column, the same decision taken for `meal` in phase
5b task 2, and for the same reason: the alternative is to publish rooms and
let the hotel multiply by a ratio it picks, which makes the final number
unverifiable, and proof mode is the point.

The change is recorded in `data/antonio/settings.json`'s
`_added_after_the_download` block, because a pre-registered schema must not
change quietly.

## 4. What each department gets

Every figure below is a band, not a point. A chef handed "144 breakfasts"
prepares 144 and runs short about half the time.

### Front desk

Per night: rooms on the books, the forecast band for rooms that will actually
stay, the rooms authorised for sale above the room count, and a warning when
the **top** of the band exceeds the rooms that are actually sellable. The
estimated cost of relocating a guest is printed beside the warning;
`walk_cost` is already in `hotel.json`.

The front desk does not choose an end of the band. It always reads the top,
because relocating a guest is a failure money does not undo.

### Kitchen

Per night: breakfast covers, and dinner covers separately for half-board and
full-board guests. The arithmetic is forecast rooms times each meal plan's
share times guests per room, and all three come from that hotel's own log.

Two tiers are printed, each named by its **measured** rate rather than by a
promise:

- the tier that covered demand on N percent of nights in the proof window;
- the tier that minimised waste, with the share of nights it fell short.

Both tiers are promises and both are measured. Which one to take is the
hotel's policy: running out of breakfast is a service failure, preparing too
much is money, and the two hurt differently. Pace does not choose.

### Housekeeping

Two kinds of work, counted separately because they cost differently:

- **departures**, which need a deep clean;
- **stayovers**, which need a light one.

Plus one **priority line**, not a workload line: rooms with an arrival that
night. These are mostly the same rooms as the departures, so adding them to
the workload would double count. They say which rooms must be finished before
check-in, not how much work there is.

Housekeeping takes the bottom of the band to roster, and the top is printed
beside it so the supervisor knows how many extra to call in.

**Limit, stated on the page.** Pace does not know how many minutes a room
takes. That is each hotel's own number. Pace reports rooms by kind; converting
to people requires the hotel to supply its own minutes.

## 5. Where the band comes from

The band for lead L is the empirical quantile spread of the engine's own
forecast error at lead L, measured **on the warm-up window**, then applied to
the scoring window.

Measuring the error on the same nights being scored would produce a band that
contains the answer by construction, and a coverage figure that means nothing.
This is the same leak the phase 5b task 1 guard exists to catch, and the same
guard applies here.

Quantiles are empirical, not fitted to a normal distribution: room forecast
error is visibly skewed, and `pilot.percentile` already exists.

A consequence that needs no explanation on the page: a fourteen-night view
runs from lead 1 to lead 14, so the bands narrow towards the near end on their
own. Further out is visibly blinder.

**Every lead, not the pilot's marks.** The pilot snapshots at 120, 90, 60, 30,
14, 7 and 1, because those are the pre-registered marks table 1 is scored at.
The handover needs all fourteen. `Ledger.otb_at` answers at any lead up to
`max_lead`, so the handover walk records every lead from 1 to 14 rather than
interpolating between marks. Interpolation would invent a width for eleven of
the fourteen leads and then report coverage against it.

## 6. How each department's answer is scored

### Coverage, the shared definition

For a band `[lo, hi]` at lead L, **coverage** is the share of scored nights
whose actual value fell inside it. A band built to hold 80 percent of nights
is reported with its measured coverage, not its intended one. A promise is
kept when measured coverage matches the label; the label is written from the
measurement.

### Front desk: an alarm, not a forecast

Mean absolute error is the wrong instrument for a warning. Three figures:

- **Of the nights that were warned, how many were genuinely at risk.**
- **Of the nights that were genuinely at risk, how many were warned.** For an
  alarm the miss is the expensive error, and precision alone hides it.
- **Notice**, defined as the deepest lead L such that the warning was on at
  every mark from L through to the night. A warning that switches on, off and
  on again gives notice from the last time it came on, not the first. A
  correct warning delivered six hours out is useless: there is no time to hold
  a room next door.

A night is "genuinely at risk" when the settled ledger shows rooms sold at or
above sellable rooms, or shows a walk. Both are already recorded.

**And that definition undercounts, which the page must say.** Phase 5b task 3
measured nights that were physically full in the house while the ledger could
not tell: over the scoring window, 94 full nights at H1 of which 29 were
invisible, and 132 at H2 of which 32 were invisible. A night the ledger cannot
see as full cannot be counted as a miss, so measured recall is an upper bound
on the truth, not the truth. The handover reports the miss count beside the
invisible-night count from the same window, so the reader can see how much
room the figure has to be wrong in.

### Kitchen

- Mean absolute error in covers, by lead.
- For each of the two tiers: the share of nights it fell short, and the
  average number of covers over-prepared when it did not.

Both tiers get both figures. A waste-minimising tier is a promise too.

### Housekeeping

- Mean absolute error on departures and on stayovers **separately**. One
  combined figure hides an error in the expensive half.
- Rostering at the bottom of the band, the share of nights that would have
  been short-staffed, and by how many rooms.

## 7. The warning that defeats itself

In proof mode nobody ever acted on Pace, so the measurement is clean.

Run live, a front-desk warning changes the outcome it predicts. The desk stops
selling, confirms guests, holds rooms next door, and the night does not sell
out. The warning then scores as a false alarm, and the more useful it was, the
worse it looks.

Pace cannot separate a warning that was wrong from a warning that worked, not
from a booking log alone. Doing so needs a record of what the desk did, which
is an input Pace does not have and this design does not add.

This is stated on the forward-mode page rather than solved. Forward-mode
warnings are not scored at all; the page links to the proof run, which was
measured on nights nobody interfered with, and lets the reader decide.

## 8. Output

One self-contained HTML page, three tabs, one per department, opening in a
browser with nothing installed and forwardable by email. It reuses the
existing dashboard's build path.

A machine-readable payload is written beside it, on the same pattern as
`out/pilot-<code>.json`.

Each tab carries a plain-sentence explanation of its own numbers, the way
`pace/explain.py` already does for a pricing decision, because a number whose
reasoning is not attached gets overridden once and switched off after that.

## 9. Architecture

A new module, `pace/handover.py`, inside the package. It reads the same
ledger, bookings and walk that the phase 5b pilot builds.

Rejected alternatives:

- **A separate project reading `out/pilot-<code>.json`.** A cleaner boundary,
  and it would force the payload to become a real interface. But most of what
  the three departments need (departures per night, meal shares, guest counts)
  is in the detailed log rather than in the summary, so the payload would have
  to grow to carry it and the boundary would stop being clean.
- **Three more tabs on the existing dashboard.** Cheapest, but that dashboard
  exists to compare three pricing policies on a simulated market. Two purposes
  on one page makes both harder to read.

One discipline goes with the choice: `handover.py` reads and never prices. It
translates a forecast into operational language and makes no further decision.

## 10. Dependencies

Phase 5b must be finished first. The band's width comes from the measured
error distribution the 5b scoring machinery produces, and the leak guard the
band depends on is 5b task 1. Starting before 5b lands means building the band
on numbers that are still moving.

The `guests` column is its own task, ahead of the rest, on the pattern of
task 2.

## 11. Decisions taken

- Three departments: front desk, kitchen, housekeeping. Not four.
- Both modes, proof built first.
- `guests` becomes a schema column.
- Self-contained HTML, three tabs.
- Bands, not points, with width measured on the warm-up window.
- Band ends: front desk always the top; housekeeping the bottom to roster with
  the top beside it; kitchen both tiers named by measured rate, the choice left
  to the hotel.
- Arrivals are a priority line for housekeeping, never a workload line.
- Live warnings are not scored, and the page says why.
