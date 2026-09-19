# Extending Pace

Four seams, all narrow on purpose. A plugin that can reach anywhere is a
plugin nobody can reason about six months later.

Anything dropped into `plugins/` is imported on the next run. There is no
registry to update and no engine code to touch.

A plugin file executes once per process, however many times the pipeline
runs in it. Registering a signal or rule under a name that already exists
replaces the earlier one, and adding an event the calendar already holds is
a no-op, so a plugin can be loaded twice without applying twice.

## Signals

A signal looks at a stay date and returns a demand multiplier plus a sentence
explaining it. Competitor rate shops, weather, flight arrivals, a scraped
event feed, a hotel's own group pipeline: all of these are signals.

```python
from pace.plugins import Signal, signal

@signal("comp_set_scarcity")
def comp_set(stay_date, ctx):
    share = my_rate_shop_feed.get(stay_date)
    if not share:
        return None                      # nothing to say, stay silent
    return Signal("comp_set_scarcity", 1.0 + 0.45 * share,
                  "%d%% of the competitor set is already sold out" % (share * 100))
```

The multiplier is applied to the remaining demand estimate before the
optimizer runs. The sentence reaches the dashboard, so the reason a rate moved
stays legible to whoever has to defend it.

`ctx` carries `hotel`, `ledger`, `asof`, `forecast`, `calendar` and `engine`.
Returning `None`, or a multiplier of exactly 1.0, keeps the signal out of the
explanation entirely.

**One thing worth getting right.** If your signal describes demand the booking
pace will also pick up on its own, fade it out as arrival approaches or you
will count the same guests twice. `plugins/late_announcements.py` shows the
pattern: full effect beyond fifty days out, nothing inside a week.

## Rules

A rule receives a finished recommendation and may change it. Brand rate
standards, parity constraints, a floor for a particular season, a manual
override table, psychological price points.

```python
from pace.plugins import rule

@rule("owner_floor", priority=10)          # lower numbers run first
def owner_floor(rec, ctx):
    if rec.stay_date.month in (6, 7, 8) and rec.rate < 199:
        rec.rate = 199.0
    return rec
```

Every change a rule makes to the rate is recorded in `rec.rule_trace`, and the
dashboard shows it. That audit trail is the point. A rule that silently
overrides the model is how a revenue system loses the trust of the people who
have to stand behind its numbers.

Two worked examples ship in `plugins/`: `late_announcements.py` for signals,
`rate_guardrails.py` for rules. The second contains a lesson about always
rounding rates downward that cost several dollars a night before it was
caught.

## Changing the property

Everything about the hotel lives in `pace/config.py`. Rooms, base rate, rate
ladder, variable cost per occupied room, walk cost, overbooking cap. No engine
module contains a hard-coded hotel number, so a different property is an edit
to one file.

## Adding a segment

Add a `Segment` to `SEGMENTS` and its code to `SEGMENT_ORDER`. The fields that
change behaviour most:

- `floats_with_bar`, set `False` for contracted business. This is the one to get
  right. A contracted rate does not move when the public rate moves, so the
  engine must not believe it can earn more from that segment by raising the
  BAR. Section 1 of [METHOD.md](METHOD.md) describes what happens when this is
  wrong.
- `prior_elasticity`, where the fit starts before there is data. Doubled to
  give the logistic `k`.
- `lead_time_mean` and `lead_time_shape`, how far ahead this segment books,
  which drives how much of its demand is still to come at any lead time.
- `commission`, subtracted before the rate is compared against the bid price.

Pace curves, unconstraining, price response and the optimizer all pick the new
segment up without further changes.

## Adding room types

`pace/roomtypes.py` holds the inventory and, separately, taste. A property
with one room type behaves exactly as it did before any of this existed, which
is what keeps the measured results reproducible, so declaring types is opt in.

```python
from pace.roomtypes import Inventory, RoomType

INVENTORY = Inventory([
    RoomType("STD", "Standard queen", 74, 1.00, 0),
    RoomType("DLX", "Deluxe king",    46, 1.18, 1),
])
INVENTORY.validate(hotel.rooms)      # refuses a building that does not add up
```

The entry type carries the published rate, so its multiplier must be exactly
1.0 and the rest are quoted off it. `validate` raises rather than pricing a
hotel that does not exist.

`SEGMENT_TYPE_PREFERENCE` is a utility offset per segment and type, with the
entry type fixed at zero because only differences matter in a choice model. A
missing entry reads as indifference. Worth calibrating so that demand across
the house roughly tracks the rooms that were actually built: a market wanting
three times the suites a property has will make every result about suites.

**The thing to understand before turning this on.** Substitution appears only
when *relative* prices move. A hotel quoting every type on a fixed multiplier
ladder has four room types and one decision, and the choice model will
correctly tell you that nothing changed. Section 14 of [METHOD.md](METHOD.md)
measures what that is worth, and the answer includes a case where it is worth
less than nothing.

## Using the network bid price

`pace/network.py` prices (room type, night) cells jointly instead of one at a
time. It is not wired into `PaceEngine`; it is a separate layer with its own
command, for the reason section 14 gives.

```python
from pace.network import NetworkInstance, Product, dual_prices

inst = NetworkInstance({("STD", night): 74.0 for night in range(21)})
inst.add(Product("RETAIL", "STD", 0, 3, value=520.0, demand=4.0,
                 cells=(("STD", 0), ("STD", 1), ("STD", 2))))
sol = dual_prices(inst)
sol.accepts(inst.products[0])        # value against the sum of the duals
sol.gap                              # how far from optimal, measured not assumed
```

`value` is the whole stay, net of commission and variable cost, not per night.
Getting that wrong is the easiest mistake here and it will not raise: the
prices simply come out several times too small.

Nights are opaque hashables, so integers or dates both work. `dual_prices` is
deterministic, `decomposed_bid_prices` adds the demand uncertainty back, and
`independent_bid_prices` restates what the engine does today so the three can
be scored against each other by `pace/networkeval.py`.

## Replacing a component

Each stage is a module with one job, so swapping one is a local change.

| To replace | Change | Keep the shape of |
|---|---|---|
| the forecast | `pace/forecast.py` | `DateForecast`, in particular `remaining_by_segment` |
| the price response | `pace/elasticity.py` | `PriceResponse.accept(code, rate, ref)` |
| the optimizer | `pace/optimize.py` | `solve(...) -> {rate, bid_price, bound_by, expected_bookings}` |
| the unconstrainer | `pace/unconstrain.py` | `ClassDemand.mean`, `.booked_mean`, `.censoring_uplift` |

A gradient boosted forecast, or a demand model fitted on real data, drops into
the first two rows without the control layer noticing.

## Connecting it to a real property management system

Replace `pace/ledger.py` with an adapter over the real one. The engine needs
four things and nothing else:

1. rooms on the books for a stay date, now
2. the same figure as it stood at every past lead time, which is the nightly
   snapshot most systems already keep
3. rooms and revenue by segment per stay date
4. denials it could observe: no availability, restriction refusals

Point three is what makes the price normalisation and the segment pace curves
possible. Point four is what makes unconstraining better than a guess. If the
real system does not log denials the engine still runs, it simply learns less
about the nights that matter most.

The concrete way to do this is a booking log in the shape of
`docs/booking-log.md`, read by `pace/ingest.py`. Write a converter from your
PMS export to that shape; `tools/convert_antonio.py` is the worked example
for a public dataset, including how the hotel's own seasonality, contract
ratios and sell-out threshold are derived and written to `hotel.json`
(ADR 0007). Complimentary and house-use rooms stay outside the ledger and
are reported beside it, because the engine has no capacity block yet.
