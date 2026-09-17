# Pace

A working hotel revenue management engine, in the Python standard library.

It generates a market, learns from the booking history that market produces,
and then decides five things every day for every future night: what rate to
publish, how many rooms to authorise, how short a stay to accept, which rate
categories to close, and what the last available room is actually worth.
Every decision comes with the reasoning attached, in sentences.

```
cd pace-rms
python3 run.py build        # about 45 seconds, no install step
open out/dashboard.html
```

No dependencies. No lockfile. Python 3.9 or newer.

[![check](https://github.com/anhngocdao/pace-rms/actions/workflows/check.yml/badge.svg)](https://github.com/anhngocdao/pace-rms/actions/workflows/check.yml)

## Why this exists

Most revenue management projects stop at a demand forecast. Forecasting is the
easy half, and the half that does not decide anything. The hard half is the
control layer: turning a forecast into a rate, knowing when rate has run out
of room and a length of stay restriction has to take over, and deciding
whether the corporate booking in front of you is worth the room it consumes.

This implements the control layer, and the two things that make it hard in
practice.

**Demand you never saw.** A property management system records what it sold.
It has no record of the guest who saw the rate and left, and none of the night
that would have sold two hundred rooms if the building had them. Training a
forecast on booked history teaches a system to under-price its best nights
forever. Pace unconstrains its history before it forecasts.

**Explanation.** A revenue system that cannot say why it moved the rate does
not survive contact with the people who have to defend its numbers. Every
recommendation carries the pace signal, the demand estimate, the value of the
room, and the constraint that was binding, written out.

## What it produces

| | Occupancy | ADR | RevPAR | GOPPAR | vs incumbent |
|---|---|---|---|---|---|
| Static BAR, one rate all year | 77.1% | 174.73 | 134.64 | 110.75 | −5.9% |
| Seasonal ladder, the incumbent | 79.1% | 180.84 | 143.04 | 118.52 | baseline |
| **Pace engine** | **86.6%** | **175.88** | **152.25** | **124.97** | **+6.4%** |

184 settled nights, 1 July to 31 December 2024, all three policies run against
an identical stream of booking requests. Repeated on four independently drawn
markets, the RevPAR lift lands between +6.4% and +7.7% and GOPPAR between
+5.4% and +6.8%:

```
python3 run.py robustness
```

The incumbent baseline is deliberately not a straw man. It is a seasonal base
rate moved up and down by how full the night already looks, which is what a
well run independent hotel actually does. Beating it by six percent is a
believable number. Beating it by forty would mean the baseline was rigged.

## Honesty about what this is

The property is invented and the market is simulated. These numbers say the
decision logic is internally sound and beats a reasonable alternative under a
demand process it was not handed the parameters of. They do not say what any
real hotel would earn.

What keeps the comparison from being circular: the generator emits
**requests**, not bookings. Every request carries its own willingness to pay,
its length of stay, and the cancellation it is already fated to make. A guest
refused by one policy is genuinely still available to another, so replaying
the same stream under different policies is a counterfactual rather than a
re-scoring. And the engine never reads that stream. It reads a ledger holding
only what was accepted plus the denials a real hotel could log, so demand on
sold-out nights reaches it censored, exactly as it would in production.

## The five decisions

| Decision | Mechanism | Where |
|---|---|---|
| What rate to publish | logistic choice model against the bid price | `pace/optimize.py` |
| What the last room is worth | marginal value of capacity, nested classes | `pace/optimize.py` |
| How many rooms to authorise | newsvendor overbooking against walk cost | `pace/controls.py` |
| Shortest stay to accept | total rate over the stay against total bid price | `pace/controls.py` |
| Which segments to close | net contracted rate against the bid price | `pace/controls.py` |

The reasoning behind each, and the failure mode each exists to prevent, is in
[METHOD.md](METHOD.md). Several of those failure modes are ones this project
shipped and then had to find.

## Layout

```
pace/
  config.py        the property: rooms, costs, rate ladder, demand segments
  calendar.py      seasons, demand classes, the city event calendar
  simulate.py      request generation and day by day policy replay
  ledger.py        the only thing the engine may read
  otb.py           booking pace curves, by class and by segment
  unconstrain.py   censored demand, restated at the reference rate
  elasticity.py    the logistic price response, fitted per segment
  forecast.py      one night, one day, the full chain
  optimize.py      bid price and rate selection
  controls.py      overbooking, minimum stay, segment closures
  policy.py        the engine, plus the two baselines it must beat
  explain.py       the decision, in sentences
  dashboard.py     one self contained HTML file
  roomtypes.py     the inventory, and who wants which room
  choice.py        what a guest does when offered more than one room
  network.py       bid prices for (room type, night) cells, from an LP dual
  networkeval.py   whether the network control earns anything, measured
plugins/           extension examples, loaded automatically
tests/             78 checks, including the golden backtest numbers
```

## Commands

```
python3 run.py build              simulate, replay, score, price, export
python3 run.py build --quick      short run, for checking a change
python3 run.py show 2025-02-06    print the full reasoning for one night
python3 run.py robustness         re-run on four independent markets
python3 run.py experiment         score the randomised rate experiment
python3 run.py network            score the network bid price against the nightly one
python3 run.py bench              where the time goes, and how it scales
python3 run.py dashboard          rebuild the HTML from out/run.json
python3 run.py test               78 checks, about 40 seconds
```

Everything the dashboard shows is read from `out/run.json`, so the numbers on
screen cannot drift away from the numbers the engine produced.

## Three things I measured and did not like

**The rate experiment buys nothing.** Elasticity estimated from a hotel's own
history is biased, because every rate in that history was set by looking at
how full the night already was. The fix is to create variation deliberately:
assign each night a random multiplier on the published rate and fit on that
alone. It works, cutting the retail identification error from +27% to +10%,
and it costs 0.91% of RevPAR to run.

Then a third engine was handed the true price response directly, which is only
possible because this market is synthetic. It earned 0.38% *less* than the
biased one. Revenue is flat at its own maximum: believing an elasticity of
2.65 when the truth is 2.08 moves the rate by four dollars and costs a tenth
of a percent. The feature is methodologically right and commercially worth
nothing, and both halves of that sentence are in
[METHOD.md](METHOD.md) section 12.

What it does buy is knowing the error is small, which cannot be known without
measuring. And it says where the money is not: the six percent comes from the
forecast, the unconstraining and the controls, not from the price curve.

**The first version of that experiment made things worse,** +123% error
against +27% for the method it replaced, because only twelve percent of the
assigned variation reached the rate actually charged. The perturbed rate was
becoming the next day's starting point, so the rate move limit spent every
following day quietly undoing it. The engine now measures its own first stage
and refuses to use an estimate when the arms have not separated.

**The network model was right about the money and wrong about the reason.**
[METHOD.md](METHOD.md) section 13 predicted that length of stay was where a
network formulation would earn its keep. `python3 run.py network` builds it and
measures +1.8% contribution over the nightly bid price at 96% occupancy, twelve
trials out of twelve. Then it turns each dimension off in turn. Length of stay
alone: seven wins out of twelve, a coin. Room types alone: nothing. The entire
gain is an interaction between the two, because a group block needs the same
room type on every night of its stay, and that is one more binding constraint
than an additive nightly price can express.

The half of that table I did not expect is that where the problem is *not* a
network, the network machinery loses a percent or more against the arithmetic
it replaces. Sophistication is not free. It is a bet on the hotel having a
particular shape.

## Scale

`python3 run.py bench`. One pricing decision costs 1.22 ms, and that is the
same at fifty rooms and at eight thousand, because the bid price is order of
the number of segments rather than of the capacity. A year long horizon
repriced nightly for a year is about 163 seconds of optimizer time per
property: roughly 176 properties per core per overnight window, in pure Python
with no parallelism.

The limit is the model, not the machine. The engine prices one room type; a
real hotel has five to fifteen with substitution between them, which is a
network problem. [METHOD.md](METHOD.md) section 13 is specific about where it
stops, and section 14 builds the network model it asks for.

## Extending it

Two seams, both narrow on purpose. A **signal** looks at a stay date and
returns a demand multiplier plus a sentence explaining it: competitor rates,
weather, flight arrivals, a scraped event feed. A **rule** receives a finished
recommendation and may change it: brand rate standards, parity constraints, a
manual override table. Rules leave an audit trail, so the dashboard can always
show what the optimizer wanted before policy touched it.

Drop a file into `plugins/` and it loads on the next run. Nothing else to
register. Two worked examples ship in the box; the interfaces are in
[EXTENDING.md](EXTENDING.md).
