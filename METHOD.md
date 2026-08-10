# Method

Each section states the mechanism, then the failure it exists to prevent. The
failures are the interesting part. Most of them produce a system that looks
like it is working.

## 1. The market

`pace/simulate.py`

Demand is generated per arrival date and per segment as a Poisson count, with
the mean built from a seasonal factor, a day of week profile, a city event
multiplier and a lognormal day level shock. Each request draws a lead time
from a gamma, a length of stay from a categorical, and a **willingness to
pay** from a lognormal centred on the rate that segment is normally quoted.
Cancellation and no-show are drawn at booking time.

Four segments. Two are quoted off the public rate, two are contracted.

| | Pays | Books | Cancels | Price response |
|---|---|---|---|---|
| Retail transient | today's BAR | 18 days out | 14% | high |
| Online travel agency | BAR, 17% commission | 26 days out | 24% | highest |
| Negotiated corporate | contracted LNR | 8 days out | 10% | none, by contract |
| Group and contract | signed group rate | 105 days out | 8% | none, by contract |

**The failure this prevents.** An earlier version had all four segments float
with the public rate. Corporate then fitted as almost perfectly inelastic,
which is exactly what the data said, and the optimizer discovered it could
raise the rate without limit and keep the volume. It walked every peak night
to the rate ceiling. The bug was not in the optimizer. It was in the model of
the world: a negotiated corporate rate does not move when the BAR moves, so
raising the BAR neither earns more from that segment nor drives it away.
Getting this wrong turns a revenue system into a machine for inventing
revenue.

## 2. The ledger, and what the engine is not allowed to see

`pace/ledger.py`

The engine reads a ledger, never the request stream. The ledger holds accepted
bookings, the on-the-books picture at every past lead time, and the denials a
hotel could actually log: no room, below the minimum stay, closed to arrival,
that rate was closed. It does not hold price refusals, because no property
management system has ever recorded the guest who looked at the rate and
closed the tab.

In the backtest window the engine turned away 350 room nights on capacity, 385
on segment closures, 175 on minimum stay and 6 on closed to arrival. It also
lost 6,700 on price, and never knew.

## 3. Booking pace

`pace/otb.py`

For each demand class, being the pair of season band and day of week, two
curves are estimated at every lead time from zero to 180.

- **Ratio.** Final rooms equals on the books divided by the share normally
  booked by now. Sharp late, violent early. At 120 days out a single group
  booking can double the forecast.
- **Pickup.** Final rooms equals on the books plus the rooms that normally
  still come. Stable early, blind to a date that is genuinely running hot.

They are blended by inverse variance, so whichever is more reliable at that
lead carries the weight. A class with fewer than eight observations falls back
to the house-wide curve rather than pretending.

A third set of curves is estimated per segment, pooled across the house: the
share of each segment's business normally on the books at each lead. Group is
almost entirely signed three weeks out; corporate has barely started. Without
this the engine cannot tell a night with no demand left from a night whose
demand has not arrived yet.

## 4. Unconstrained demand

`pace/unconstrain.py`

Two corrections, applied in order.

**Price.** Each night's rooms are divided by the acceptance rate at the price
that night actually charged, per segment, so dates priced differently become
comparable. Contracted segments divide by one.

**Censoring.** A night that sold out is right censored: true demand is
somewhere above what was booked, and nobody wrote the number down. Projection
detruncation, following Weatherford and Bodily: assume demand within a class
is normal, replace each censored observation with its conditional expectation
above the censoring point,

    E[X | X > c] = mu + sigma * phi(z) / (1 - Phi(z)),   z = (c - mu) / sigma

re-estimate the class, and repeat until it settles. Observable denials are
added back first, since those are demand the hotel did see.

**The failure this prevents.** The most common way a revenue system quietly
teaches itself to under-price. The forecast says the hotel sold 150, the
system concludes demand was 150, and next year it opens the same night at the
same rate. House-wide the correction here is around eight percent. On the film
festival nights it is far larger, and those are the only nights where the rate
decision is worth much.

## 5. Price response

`pace/elasticity.py`

    acceptance(r) = 2 / (1 + exp(k * (r / reference - 1)))

normalised to one at the reference rate. The parameter `k` is twice the local
elasticity there, so it stays readable: `k = 3.8` means an elasticity of 1.9
where the hotel normally prices. Fitted by grid search with demand class fixed
effects removed, shrunk toward a configured prior by forty pseudo
observations, sold-out nights dropped.

**The failure this prevents.** The obvious model is a constant elasticity
power law, and it is a trap. Whenever the fitted elasticity comes out below
one, revenue rises without bound as the rate rises and the optimizer walks
into the ceiling. There is a test named after exactly this. The logistic form
decays to zero, so every segment has a genuine interior optimum however
insensitive it is.

**What is not claimed.** Estimated on observational history alone, this is
biased, and measurably so. Every rate in that history was set by looking at
how full the night already was, so rate and demand were decided together. The
observational fit returns an elasticity of 2.65 for retail against a true
2.08, and 2.82 for the online channel against a true 2.52: over-elastic by
27% and 12%. The direction is worth stating because it is the opposite of
what intuition suggests. Simultaneity should bias toward insensitivity, and
here the near-capacity nights dominate it: on a busy night the rate is high
and the rooms sold are capped by the building, which reads as a high rate
having destroyed the demand.

Class fixed effects and shrinkage limit the damage. They do not remove it.
Removing it needs variation somebody deliberately created, which is section
12, and section 12 also reports what that turned out to be worth.

## 6. The forecast, and making it sum to itself

`pace/forecast.py`

    booked share now = sum over segments of ( mix * share already booked )
    date estimate    = rooms on the books at reference / booked share now
    demand           = booked share * date estimate + (1 - booked share) * class level
    remaining[s]     = demand * mix[s] * share still to come[s]

The weight on the date's own signal is the share of its business already in.
At four months out that is almost nothing and the class norm carries the
forecast; in the last fortnight the date speaks for itself. There is no tuning
constant here: the weight is a quantity the model already had to estimate.

**The failure this prevents.** An earlier version derived the booked share
from the house pace curve and the remaining demand from the segment curves.
Both were individually correct and they did not agree with each other, so
rooms booked plus rooms still to come did not equal the demand the engine
believed in. The forecast had stopped summing to itself, which no single
number on a dashboard would ever have revealed.

## 7. Bid price

`pace/optimize.py`

The bid price is the marginal value of the last available room: what the hotel
gives up by selling it now instead of holding it for whoever comes later. Two
implementations.

**Dynamic program.** With c rooms and one more arrival period left,

    V(t, c) = V(t-1, c) + sum_s p_s * max(0, r_s - [V(t-1, c) - V(t-1, c-1)])

and the bid price is `V(t,c) - V(t,c-1)`.

**Closed form, which is what runs.** For a single resource with nested classes
ordered by value, expected revenue on c rooms is

    R(c) = sum_j (v_j - v_{j+1}) * E[min(D_j, c)]

with `D_j` the cumulative demand down to class j. Differentiating,

    bid(c) = sum_j (v_j - v_{j+1}) * P(D_j > c)

which is the foundation the EMSR heuristics are built on. Four normal tail
probabilities instead of a quarter of a million operations. The tests check
the two against each other.

Demand variance is arrival noise plus forecast error, `sd = sqrt(mu + (0.25 *
mu)^2)`. At sixty days out the second term dominates, which is correct: the
uncertainty about a distant night is mostly uncertainty about the forecast,
not about the arrivals.

**The failure this prevents.** The dynamic program consumes at most one room
per period, so the number of periods must exceed the capacity being valued.
The first version capped periods at sixty while remaining capacity ran to 159.
Every room past the sixtieth was valued at exactly zero. The engine reported
nights forecast to sell out with a bid price of zero and no restrictions, and
priced them at the seasonal average. The arithmetic was right and the answer
was worthless.

## 8. Rate selection

Given the bid price, the rate is chosen from a discrete ladder by maximising
expected contribution above it, with expected bookings capped at the rooms
that exist. Then a hard floor. Never sell the marginal room below what it is
worth:

    floor = (bid price + variable cost) / (1 - commission)

Bid price and rate are solved together, three passes, because each depends on
the other.

Two guards on top. A daily move limit of twelve percent, because a rate that
swings forty percent overnight reads as a fault to a guest and to a channel
manager alike. And a dead band, because moving four dollars is not worth the
churn it causes downstream. Neither guard may push the rate below the bid
price floor: smoothing is a preference about how the hotel looks to the
market, and a preference does not overrule a statement that the sale loses
money.

## 9. Overbooking

`pace/controls.py`

Newsvendor. Selling one more room earns its contribution if somebody hands a
room back, and costs a walk if nobody does. Authorise up to the point where
the chance of walking anyone equals the critical ratio,

    critical ratio = (ADR - variable cost) / (ADR - variable cost + walk cost)

with shows binomial and a hard cap at six percent over. The remaining
cancellation probability is scaled by lead time: almost nobody cancels the
night before, and a booking made six months out has most of its risk ahead of
it. Walk cost is set at 620, roughly three times ADR, covering relocation,
transport, compensation and the goodwill that appears on no invoice.

Across 184 nights this walks fifteen guests, around one tenth of one percent
of arrivals. It is a cost the model has priced, not an accident.

## 10. Length of stay and segment closures

A stay is worth taking when the total net rate over its nights clears the
total bid price over those nights. The minimum stay is then the shortest n for
which that holds:

    MLOS(arrival) = min n such that sum over n nights of (net rate - bid price) >= 0

On a compressed night whose rate has already hit the ceiling, one night fails
and a stay reaching into the softer nights either side passes. That is the
whole idea behind a minimum stay restriction, stated as arithmetic rather than
as a rule of thumb. If no stay up to five nights clears, the date closes to
arrival.

Segment closures are displacement analysis stated as one comparison. A
contracted corporate room at 155 net is worth taking when the last room is
worth 90 and worth refusing when it is worth 210, and the only thing that
changed is the demand still to come. The highest paying door never closes.

## 11. Cold start

The engine defers to the incumbent's ladder until it has 150 settled nights
and a house pace curve with enough observations behind it, then refits every
28 nights. A revenue system with no history should behave like a sensible
manager, not like a confident one.

## 12. Randomised rate experimentation, and a null result

`pace/experiment.py`, `python3 run.py experiment`

Each stay date is assigned, once and at random, a multiplier on whatever rate
the optimizer would otherwise publish: ten control slots against eight treated
at plus or minus eight and sixteen percent. The assignment is a blake2b hash
of the date, so it depends on nothing but the date, it can be recomputed for
any past night without being stored, and it does not re-randomise between
processes the way Python's salted built-in hash would.

The perturbation is skipped when the night is forecast to sell out and clipped
when it would push the rate below the bid price floor. Both are refusals to
spend real money on an experiment that would not answer anything, and both
mean compliance is partial. The estimator handles that by treating the
assigned arm as an instrument for the rate actually charged, with a saturated
first stage, which is two stage least squares written out longhand.

**It works, unevenly.** Retail identification error falls from +27% to +10%.
The online channel barely moves, +12% to +11%, because it books further out
and a larger share of its volume lands outside the perturbed window. It costs
0.91% of RevPAR over the scored window, which is the price of publishing a
rate nobody chose on roughly forty percent of nights.

**The first version did not work,** and the failure is the useful part. It
made retail *worse*, +123% error, because only twelve percent of the assigned
variation survived into the rate actually charged. Three causes. The
perturbation applied over too narrow a lead window. Nights forecast full were
skipped, and those are the ones with the distinctive rates. And worst, the
perturbed rate became the next day's starting point, so the move limit spent
every following day walking the deviation back: the experiment was cancelling
itself. A weak instrument does not produce a slightly worse answer. It
produces a wildly wrong one, confidently. The engine now measures its own
first stage and refuses to use an estimate when the arms have not separated:

    MIN_INSTRUMENT_SPREAD = 0.045

**And then the result that matters.** A third engine was handed the true price
response directly, which is possible only because this market is synthetic. It
earned 0.38% *less* RevPAR than the biased one. Getting the elasticity exactly
right is worth nothing here.

The reason is that revenue is flat at its own maximum, which is a second order
property of any smooth optimum and not a quirk of this model. Measured
directly: believing an elasticity of 2.65 when the truth is 2.08 moves the
chosen rate by four dollars and costs between 0.05% and 0.11% of contribution.
On a single floating segment every rate inside a sixteen dollar band returns
99% of the best available. With the real segment mix, where nearly forty
percent of the business is contracted and does not respond to the rate at all,
the band widens to twenty four dollars.

So the honest summary of this feature is: methodologically correct, cuts the
retail identification error by well over half, costs about one percent of
RevPAR, and buys no measurable revenue. What it does buy is the knowledge that the error is small. Nobody can
tell they are standing on the flat part of a curve without having measured the
curve, and an elasticity wrong by a factor of three would matter enormously.
The experiment is how you find out which case you are in.

It also says where the money is not. The six percent this engine earns over
the incumbent comes from the forecast, the unconstraining and the controls,
not from the price response. Effort spent sharpening the elasticity is effort
spent on the flattest part of the surface. There is a test named after this,
and if it ever fails the conclusion needs revisiting.

## 13. Scale, measured

`python3 run.py bench`

One pricing decision costs 1.22 milliseconds, and that number does not change
between a fifty room property and an eight thousand room one:

| Rooms | Per decision | Decisions per second |
|---|---|---|
| 50 | 1.233 ms | 810 |
| 500 | 1.218 ms | 821 |
| 8,000 | 1.216 ms | 822 |

Flat, because the closed form bid price of section 7 is order of the number of
segments, not of the capacity. The dynamic program it replaced was order of
capacity times periods and would have grown quadratically here. Choosing the
closed form for speed turned out to be the thing that made the property size
irrelevant.

A full year horizon repriced every night for a year is about 163 seconds of
optimizer time per property. On one core, in an eight hour overnight window,
that is roughly 176 properties, in pure Python with no vectorisation and no
parallelism. Properties are independent, so the obvious parallelism is
available and unused.

**Where it actually stops scaling is the model, not the machine.** This prices
one room type. A real hotel has five to fifteen, with guests substituting
between them, and that is a network problem rather than a single resource one.
The bid price generalises, but through the dual prices of a deterministic
linear program, not through the formula in section 7. Length of stay is
handled here by summing nightly bid prices, which is the standard
approximation and is exactly where a network formulation would earn its keep.
A thirty room property has too few nights per demand class to estimate
anything, and would need statistics pooled across properties. And every real
integration meets a property management system whose historical snapshots do
not exist and whose segment codes are a mess.

## References

Talluri and van Ryzin, *The Theory and Practice of Revenue Management*, for
the single resource dynamic program and bid price control. Belobaba, on EMSR
and nested protection levels. Weatherford and Bodily, on unconstraining
censored demand. Littlewood, for the two class rule the closed form
generalises.
