# Extending Pace

Two seams, both narrow on purpose. A plugin that can reach anywhere is a
plugin nobody can reason about six months later.

Anything dropped into `plugins/` is imported on the next run. There is no
registry to update and no engine code to touch.

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

- `floats_with_bar` — `False` for contracted business. This is the one to get
  right. A contracted rate does not move when the public rate moves, so the
  engine must not believe it can earn more from that segment by raising the
  BAR. Section 1 of [METHOD.md](METHOD.md) describes what happens when this is
  wrong.
- `prior_elasticity` — where the fit starts before there is data. Doubled to
  give the logistic `k`.
- `lead_time_mean` and `lead_time_shape` — how far ahead this segment books,
  which drives how much of its demand is still to come at any lead time.
- `commission` — subtracted before the rate is compared against the bid price.

Pace curves, unconstraining, price response and the optimizer all pick the new
segment up without further changes.

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
