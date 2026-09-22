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
| Front of house | Front desk | Which nights are going to sell out, and how much notice do I get? |
| Back of house | Kitchen | How many covers? |
| Back of house | Housekeeping | How many rooms, of which kind? |

All three read one forecast, so the core is built once. A fourth department is
not in scope, and nothing here is built to make one cheaper: adding one later
costs a new output and a new scoring rule, and no work done now reduces that.

**Out of scope.** Staffing minutes per room, the hotel's own labour standards,
any link to a PMS or a channel manager, any write path at all. The handover
reads and reports. It never prices, never authorises and never sends.

## 2. Two modes, proof first

**Proof mode** replays a window of real history. For every night in the
window, it produces what Pace would have told each department at each lead,
and sets it beside what actually happened. Everything here is checkable, and
nothing needs anyone's permission to run.

**Forward mode** takes a booking log exported up to today and answers for the
next fourteen days. Nothing here is checkable, and the page says so.

The two modes share every computation. They differ in where the walk stops,
in which history the bands are measured on (section 6), and in whether an
answer exists to compare against.

## 3. Day convention

The engine thinks in stay nights. Two of the three departments work in
service days, and the offset between them is a day. Stated once, here, and
every table on the page carries the label this section defines.

- A **stay night N** is the night beginning on calendar day N.
- **Breakfast on morning D** is served to the guests who stayed **night
  D − 1**. Breakfast belongs to the previous night.
- **Dinner on evening D** is served to the half-board and full-board guests
  staying **night D**. Dinner belongs to the same night.
- **Departures on day D** are bookings whose last stay night was **D − 1**.
- **Stayovers on day D** are bookings that cover **both** night D − 1 and
  night D. Per booking, not per room: this log has a room *type* but no room
  *number*, so "the same room on two nights" is not a thing it can express.
  One booking spanning both nights is a stayover; two consecutive bookings by
  the same guest are a departure and an arrival, which is also what the floor
  sees, because the room is stripped and made up between them.
- **Arrivals on day D** are bookings whose first stay night is **D**.

So `rooms occupied on night D − 1 = departures on day D + stayovers on day D`.
That identity is the one the housekeeping forecast is built on, and a test
asserts it holds on every day of the window.

The front desk tab is the only one indexed by stay night. The kitchen and
housekeeping tabs are indexed by service day, and each table says which.

**Which lead a service day carries.** A quantity is at the lead of the night
it belongs to, not the day it is served. Breakfast on morning D and departures
on day D both belong to night D − 1, so both carry night D − 1's lead. Dinner
on evening D belongs to night D and carries night D's lead.

The consequence is the first row of a fourteen-day view: tomorrow morning's
breakfast belongs to tonight, which is lead 0. The walk therefore records lead
0 as well as leads 1 to 14. `Ledger.otb_at(d, 0)` already answers it, the band
at lead 0 is narrow because almost nothing is still to come, and the first row
is the one the chef needs most.

## 4. Schema and data rules

### 4.1 A new column: `guests`

`pace/ingest.py`'s `Booking` carries `rooms` but not the number of people in
them. A kitchen needs covers, not rooms.

The public dataset has the fields: across stayed bookings, the resort averages
1.94 guests per room and the city hotel 1.93. The converter drops them today.

`guests` becomes a schema column, the same decision taken for `meal` in phase
5b task 2, and for the same reason: the alternative is to publish rooms and
let the hotel multiply by a ratio it picks, which makes the final number
unverifiable, and proof mode is the point.

**`guests` = `adults` + `children`.** Babies are excluded: a baby is not a
cover.

**A zero-adult row is not an error**, and the data says why. Among stayed
bookings, 294 rows have no adults. 139 of them carry children, which is a
child-only booking: odd, but somebody slept there. The other 155 have no
occupants at all, and 124 of those already price at zero, so the converter's
existing `ADR0` branch sends them to NONREV before any of this arithmetic
runs. That leaves **31 rows across both hotels** that reach the ledger as
revenue with nobody in them, 0.04 percent of stayed bookings.

Rejecting a file over 31 rows would be a worse failure than counting them, so
a revenue row with `guests` of zero is counted, contributes no covers, and is
reported as a `guests_zero` warning in the ingest block, on the pattern of the
reader's existing counts. It still counts as a room for housekeeping: nobody
ate, but the room was slept in and has to be cleaned. Covers and rooms part
company here, which is the reason they are banded separately in section 6.

**`children` carries four missing values**, written as the literal string
`NA`, all four at the city hotel. They are read as zero, and counted in a
`children_missing` warning so the reading is visible rather than silent. Four
rows in 119,390 changes nothing; the count is reported because a rule applied
without a count is a rule nobody can check.

The change is recorded in `data/antonio/settings.json`'s
`_added_after_the_download` block, because a pre-registered schema must not
change quietly.

### 4.2 Which board codes eat

- **Breakfast**: `BB`, `HB`, `FB`.
- **Dinner**: `HB`, `FB`.
- **Neither**: `SC` and `Undefined`.

This is the dataset's own documentation, not an interpretation: the data
dictionary published with Antonio, de Almeida and Nunes lists `Undefined/SC`
together as the no-meal-package category. Reading them apart would be the
departure.

The share is still printed beside the kitchen table, because a documented
reading is worth no more than its size if it turns out to be wrong: at H1
`Undefined` is 3,890 of 119,887 stayed room nights, 3.2 percent, and at H2
there are none.

## 5. What each department gets

Every figure below is a band, not a point. A chef handed "144 breakfasts"
prepares 144 and runs short about half the time.

### Front desk

Indexed by stay night. Per night: rooms on the books, the forecast band for
rooms that will actually stay, the rooms authorised for sale above the room
count, and a **sell-out warning** on the condition set out in section 7.1.

The warning says the night is going to fill, not that anyone will be
relocated. Section 7.1 shows why the second claim cannot be made on a hotel
whose room count Pace inferred, and this tab does not make it: `walk_cost` is
not printed here, because a cost attached to an event the data cannot show is
an invitation to believe the event was shown.

The front desk does not choose an end of the band. It always reads the top,
because the actions a sell-out warning triggers (stop discounting, tightening
the minimum stay, briefing the desk) are cheap to take on a night that then
does not fill, and expensive to skip on a night that does.

### Kitchen

Indexed by service day. Per day: breakfast covers for that morning, and
dinner covers for that evening, counted separately because they are different
nights of guests (section 3).

Two tiers are printed, each named by its **measured** rate rather than by a
promise:

- **The covering tier**, the top of the band: "enough on N percent of days",
  where N is measured, not intended.
- **The balanced tier**, the middle of the band: the point at which a cover
  short and a cover wasted weigh the same. This is the median, and it is the
  median for a reason: minimising waste alone is minimised by preparing
  nothing, so a tier that optimises one side is not a tier. The median is the
  quantity that minimises total absolute error, which is the case where a
  shortfall costs exactly as much as an over-prep.

A hotel that knows a shortfall costs `r` times an over-prep should read the
`r / (1 + r)` quantile instead, and the page says so and prints the band's
quantiles so the reader can. Pace does not ask for `r` and does not assume
one: the median is the `r = 1` case, and it is the only case that needs no
number the hotel has not measured.

Both tiers are promises, and section 7.2 measures both.

### Housekeeping

Indexed by service day. Two kinds of work, counted separately because they
cost differently:

- **departures**, which need a deep clean;
- **stayovers**, which need a light one.

Plus one **priority line**, not a workload line: rooms with an arrival that
day. These are mostly the same rooms as the departures, so adding them to the
workload would double count. They say which rooms must be finished before
check-in, not how much work there is.

Housekeeping rosters at the bottom of the band, and the top is printed beside
it so the supervisor knows how many extra to call in.

**Limit, stated on the page.** Pace does not know how many minutes a room
takes. That is each hotel's own number. Pace reports rooms by kind;
converting to people requires the hotel to supply its own minutes.

## 6. The bands

### 6.1 Each quantity gets its own band

There are five forecast quantities: rooms, breakfast covers, dinner covers,
departures and stayovers.

**A band is measured on the error of the quantity it is a band of**, never
derived from another quantity's band. Taking the rooms band and multiplying it
by a meal share and a guests-per-room ratio would discard the variation in
both ratios, and the measured coverage would then come in below the label,
which breaks the one promise this design makes.

Measuring end to end puts the ratios' variation inside the band, because the
error being measured is the error of the finished number.

### 6.2 How each point forecast is produced

The engine forecasts rooms. It does not forecast covers or departures, so
those are derived, and the derivation uses only what was knowable on the
forecast day.

**Rooms on night N** are the engine's own forecast. That is the quantity
phase 5b scores, and the only one taken from the engine unchanged.

**The other four are split in two before any ratio is applied**, because at
these leads most of the night is already known. At lead 7 the bookings that
will occupy that night are largely on the books already, and each of them
states its own board, its own guest count and its own departure date. A
half-board tour group of forty rooms is sitting in the log with `HB` written
on it; multiplying a room forecast by last month's average board share
dissolves that group into the average and then reports a band wide enough to
have hidden it.

So each quantity is `booked + pickup`:

- **The booked part** is counted from the bookings on the books at that lead,
  using their own board codes, their own guest counts and their own
  departure dates. Nothing is estimated.
- **The pickup part** is `rooms forecast − rooms on the books`, the rooms
  still to come, and only that part is multiplied by a historical ratio.

Which gives:

| Quantity | Booked part | Pickup part |
|---|---|---|
| Breakfast covers, morning D | guests on the books for night D − 1 whose board is BB, HB or FB | pickup for night D − 1 × breakfast share × guests per room |
| Dinner covers, evening D | guests on the books for night D whose board is HB or FB | pickup for night D × dinner share × guests per room |
| Stayovers, day D | bookings on the books covering night D − 1 and night D | pickup for night D − 1 × stayover share |
| Departures, day D | rooms forecast for night D − 1 − stayovers forecast | (identity, section 3) |

The four ratios (breakfast share, dinner share, guests per room, stayover
share) are estimated from the settled history available **on the forecast
day**, over
a trailing window of the same weekday, on the pattern `pace/baselines.py`
already uses. A ratio estimated from the whole file would be a leak, and the
phase 5b task 1 guard catches exactly that.

Nothing in the booked part is a leak either: those bookings were in the log on
the forecast day, which is the same test every other figure here passes.

This does not change whether a band is honest. Coverage is measured end to end
either way (section 6.1), so a worse point forecast would simply produce a
wider band and a truthful coverage figure. It changes how wide the band has to
be, which is what the departments actually feel.

### 6.3 Where the width comes from

The band for a quantity at lead L is the empirical quantile spread of that
quantity's own forecast error at lead L.

**Proof mode** measures it on the warm-up window and applies it to the scoring
window. Measuring it on the nights being scored would produce a band that
contains the answer by construction, and a coverage figure that means nothing.

**Forward mode** measures it on the most recent twelve months of **settled**
nights ending on the last night that had fully settled before the export date.
The same rule applies: a night still open cannot contribute an error, because
its error is not yet known.

Quantiles are empirical, not fitted to a normal distribution: these errors are
visibly skewed, and `pilot.percentile` already exists.

**Every lead, not the pilot's marks.** The pilot snapshots at 120, 90, 60, 30,
14, 7 and 1, because those are the pre-registered marks table 1 is scored at.
The handover needs all of them. `Ledger.otb_at` answers at any lead up to
`max_lead`, so the handover walk records **every lead from 0 to 14** rather
than interpolating between marks. Interpolation would invent a width for
twelve of the fifteen leads and then report coverage against it. Lead 0 is
there because tomorrow morning's breakfast belongs to tonight (section 3).

### 6.4 The quantiles, decided

| Use | Quantile |
|---|---|
| The printed band | p10 to p90 |
| The front desk warning | p90, the top of the printed band |
| The kitchen's covering tier | p90, the top of the printed band |
| The kitchen's balanced tier | p50 |
| Housekeeping's roster line | p10, the bottom of the printed band |

The warning quantile is the knob between false alarms and misses, and it is
set to p90 for one reason beyond its level: it is the number already printed.
A warning that fires on a threshold the reader cannot see on the page is a
warning the reader cannot check. Section 7.1 measures what p90 costs in both
directions, so moving it is a decision with numbers attached rather than a
preference.

**Minimum sample.** A p90 needs enough nights to be a p90. A band is printed
for a lead only when that lead has at least **30 usable nights** in the window
the width is measured on. Below that the cell prints no band and says why.
The warm-up window is roughly 366 nights, so every lead from 1 to 14 clears
this comfortably on both hotels; the rule exists for the hotel that arrives
with eight months of history.

## 7. How each department's answer is scored

**Coverage** is the shared measure: for a band `[lo, hi]` at lead L, the share
of scored days whose actual value fell inside it. A band built to hold 80
percent is reported with its measured coverage, not its intended one. The
label on the page is written from the measurement.

### 7.1 Front desk: an alarm, not a forecast

**The event this alarm was first written for does not occur in this data, at
all.** Measured over the 427-night scoring window, on both hotels:

| | H1 | H2 |
|---|---|---|
| Nights where rooms sold exceeded capacity | 0 | 0 |
| Nights with a walk recorded | 0 | 0 |
| Busiest night, as a share of capacity | 98.9% | 100.0% |

And the reason is structural rather than lucky. `sellable_rooms` is inferred
from the busiest night the log contains, 187 of 187 at H1 and 226 of 226 at
H2, so occupancy cannot exceed it: the ceiling is defined as the highest point
of the floor. The public dataset records no walk either, so a walk could only
ever have come from the replay's own arithmetic, and the replay never had a
night to walk anyone from.

A warning about being oversold is therefore unmeasurable on any hotel whose
room count Pace has to infer, which is every hotel that arrives without
stating one. This is not a gap to be closed later by better code.

**So the alarm is about sell-out, and says so.** The event is the night
reaching the pre-registered sell-out cut: settled revenue rooms at or above
`sellout_threshold` times `pilot.capacity_on`. The threshold is 0.97 and it
was pre-registered in `data/antonio/settings.json` before the data was
downloaded, so it is not a number chosen to make the event set a convenient
size. What it produces:

| | H1 | H2 |
|---|---|---|
| Nights reaching the cut | 94 | 132 |
| Share of the scoring window | 22.0% | 30.9% |

That is worth a front desk knowing, and it is worth it for reasons that are
not overbooking: stop discounting, tighten the minimum stay, and warn the desk
that walk-ins will be turned away. It is **not** a claim that anyone will be
relocated, and the tab does not make one.

**Both sides use the same quantity and the same cut.** The warning fires when
the top of the band reaches `sellout_threshold × capacity_on`. The event is
settled revenue rooms reaching `sellout_threshold × capacity_on`. Forecast and
outcome are both revenue rooms, and `capacity_on` nets out the comped rooms on
both sides, so the two can never disagree about how large the hotel was.

Three figures:

- **Precision**: of the nights warned, the share that reached the cut.
- **Recall**: of the nights that reached the cut, the share warned. For an
  alarm the miss is the expensive error, and precision alone hides it.
- **Notice**: the deepest lead L such that the warning was on at **every lead**
  from L through to the night. A warning that switches on, off and on again
  gives notice from the last time it came on, not the first.

**Notice is censored at 14** because the walk records no lead deeper than 14.
A night whose warning was on at every lead from 14 down is reported as **"14 or
more"**, never as 14, and the count of censored nights is printed beside the
median notice. A censored figure presented as an exact one is a smaller lie
than most but it is still one.

**Every rate is printed beside its count**, and a rate is not printed at all
below the same 30-night floor section 6.4 sets for bands: a recall of "3 of 4"
is a number about four nights, and rendering it as 75 percent invites the
reader to treat it as a property of the hotel. Both hotels clear the floor
here, 94 and 132 against 30; the rule exists for the cut, the season or the
hotel where they do not.

**The nights the log cannot speak for.** Phase 5b task 3 counted nights that
were physically full in the house while the settled ledger showed fewer rooms
sold: 29 of 94 at H1 and 32 of 132 at H2.

Those nights are not missed events. The event here is reaching the sell-out
cut, and reaching it is exactly what they did. They are the nights where the
booking log cannot say **whether anyone was turned away**, because the
difference between a full house and a ledger that is short of one is made of
no-shows and rooms released at the last moment, and neither is recorded as a
refusal.

So they are reported as their own count, under their own heading, as the part
of the picture the data does not contain. They are not folded into precision,
they are not folded into recall, and no bound is claimed from them. The
earlier draft called recall an upper bound on their account; that was wrong in
both directions, and the honest reply is that they measure something else.

### 7.2 Kitchen

- Mean absolute error in covers, by lead, for breakfast and dinner separately.
- For each of the two tiers: the share of days it fell short, and the average
  number of covers over-prepared on the days it did not.

Both tiers get both figures. The balanced tier is a promise too, and a tier
that is short on half the days is doing what the median is supposed to do,
which the page should say rather than leave a reader to discover.

### 7.3 Housekeeping

- Mean absolute error on departures and on stayovers **separately**. One
  combined figure hides an error in the expensive half.
- Rostering at p10, the share of days that would have been short-staffed, and
  by how many rooms.

## 8. The warning that defeats itself

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

## 9. Output

One self-contained HTML page, three tabs, one per department, opening in a
browser with nothing installed and forwardable by email. It reuses the
existing dashboard's build path.

A machine-readable payload is written beside it, on the same pattern as
`out/pilot-<code>.json`.

Each tab carries a plain-sentence explanation of its own numbers, the way
`pace/explain.py` already does for a pricing decision, because a number whose
reasoning is not attached gets overridden once and switched off after that.

## 10. Architecture

A new module, `pace/handover.py`, inside the package. It reads the same
ledger, bookings and walk that the phase 5b pilot builds.

Rejected alternatives:

- **A separate project reading `out/pilot-<code>.json`.** A cleaner boundary,
  and it would force the payload to become a real interface. But most of what
  the three departments need (departures per day, meal shares, guest counts)
  is in the detailed log rather than in the summary, so the payload would have
  to grow to carry it and the boundary would stop being clean.
- **Three more tabs on the existing dashboard.** Cheapest, but that dashboard
  exists to compare three pricing policies on a simulated market. Two purposes
  on one page makes both harder to read.

One discipline goes with the choice: `handover.py` reads and never prices. It
translates a forecast into operational language and makes no further decision.

## 11. Dependencies

Phase 5b must be finished first. The band widths come from the measured error
distributions the 5b scoring machinery produces, and the leak guard both the
bands and the ratio estimates depend on is 5b task 1. Starting before 5b lands
means building on numbers that are still moving.

The `guests` column is its own task, ahead of the rest, on the pattern of
task 2.

CLAUDE.md requires a new mechanism to start as an ADR marked "Proposed" before
any code lands. Two are owed here, and they are the two decisions a later
reader will most want the reasoning for: that the front desk alarm is about
sell-out rather than overbooking, with the measurement that forced it, and
that every quantity is banded on its own error rather than on the rooms band
times a ratio.

## 12. Decisions taken

1. Three departments: front desk, kitchen, housekeeping. Not four.
2. Both modes, proof built first.
3. `guests` becomes a schema column, defined as adults plus children, babies
   excluded. A zero-guest revenue row is counted and warned about, not
   rejected: it contributes no covers and still counts as a room to clean.
4. `SC` and `Undefined` both count as no meal, and the `Undefined` share is
   printed beside the answer that rests on it.
5. The day convention of section 3: breakfast belongs to the previous night,
   dinner to the same night, departures to the morning after the last night.
6. Self-contained HTML, three tabs.
7. Bands, not points. Each of the five quantities is banded on its own
   measured error, never on another quantity's band multiplied by a ratio.
8. The quantiles of section 6.4: p10 to p90 printed, p90 for the warning and
   the covering tier, p50 for the balanced tier, p10 for the roster line, and
   a 30-night floor per lead before any band is printed.
9. Band ends: front desk always the top; housekeeping p10 to roster with the
   top beside it; kitchen both tiers named by measured rate, the choice left
   to the hotel, with the median as the default that asks the hotel for
   nothing.
10. Arrivals are a priority line for housekeeping, never a workload line.
11. The front desk alarm is about **sell-out**, not about being oversold.
    Measured on both hotels: zero nights above capacity and zero walks, because
    `sellable_rooms` is inferred from the busiest night in the log. The event
    is reaching `sellout_threshold` (0.97, pre-registered) times
    `capacity_on`, which is 94 nights at H1 and 132 at H2, and the warning
    fires on the same quantity against the same cut.
12. Nights full in the house but short in the ledger (29 of 94 at H1, 32 of
    132 at H2) are reported as their own count. They are not missed events and
    no bound is claimed from them: they are where the log cannot say whether
    anyone was turned away.
13. Notice is censored at 14 and reported as "14 or more", with the count of
    censored nights beside it.
14. Every rate is printed beside its count, and no rate is printed below the
    same 30-night floor that governs bands.
15. Live warnings are not scored, and the page says why.
