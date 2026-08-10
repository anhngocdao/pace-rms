"""The same system, explained four times, at four depths.

A revenue model has to survive four different rooms.  A general manager wants
to know what it will do to occupancy.  An owner wants the number.  A revenue
director wants the mechanism, and will find the one place it breaks.  A
reviewer wants to know which assumption is doing the work.  Writing one
explanation and hoping means three of those four rooms get an answer pitched
at somebody else.

So each idea is written out four times.  Not a summary followed by longer
versions of the summary: four complete accounts, each standing on its own,
each using the vocabulary of the person it is for.  The plain version never
says "elasticity" and the formal version never apologises for saying "envelope
theorem".  Nobody should have to read a level below their own to follow the
one they are on.

This module is the single source for that text.  The dashboard embeds it and
the portfolio case page reproduces it, so the two cannot drift apart.
"""

from typing import Dict, List

LEVEL_ORDER = ("plain", "working", "technical", "formal")

LEVEL_LABELS: Dict[str, Dict[str, str]] = {
    "plain": {"name": "Plain",
              "for": "no hotel vocabulary assumed"},
    "working": {"name": "Working",
                "for": "how a revenue team would say it"},
    "technical": {"name": "Technical",
                  "for": "the mechanism, named"},
    "formal": {"name": "Formal",
               "for": "the mathematics and its assumptions"},
}

TOPICS: List[Dict[str, object]] = [
    {
        "id": "decides",
        "title": "What the engine decides",
        "levels": {
            "plain":
                "A hotel has 150 rooms, and tonight it will end with some of "
                "them empty. Every empty room is gone for good: nobody can "
                "sell last Tuesday. So the only question that matters is what "
                "to charge, and the answer changes every day, because the "
                "guests coming next Friday are not the guests coming next "
                "Tuesday. The engine takes that decision ninety times over, "
                "once for each of the next ninety nights, and leaves the "
                "reasoning attached to every one of them.",
            "working":
                "Forecasting is the easy half of revenue management and it "
                "decides nothing. The hard half is the control layer: turning "
                "a forecast into a published rate, knowing when rate has run "
                "out of room and a minimum stay has to take over, and deciding "
                "whether the corporate booking in front of you is worth the "
                "room it consumes. Five decisions a day, for every night in "
                "the next three months.",
            "technical":
                "The engine reads a booking ledger and nothing else. From it: "
                "pace curves by demand class and by segment, an unconstraining "
                "pass over the censored nights, a fitted price response, "
                "remaining demand forecast per segment, and then rate and bid "
                "price solved jointly, because the bid price depends on the "
                "rates the remaining demand will be quoted and those rates are "
                "chosen against the bid price. Overbooking, minimum stay and "
                "segment closure are all derived from the bid price rather "
                "than set alongside it.",
            "formal":
                "Capacity control on a perishable resource facing stochastic, "
                "segmented, price sensitive demand over a finite horizon. The "
                "state is remaining capacity and time to arrival, the control "
                "is a rate together with an acceptance rule, and the objective "
                "is expected contribution. Because the value function is "
                "concave in capacity, the optimal acceptance rule is a "
                "threshold on its first difference, which is what allows a "
                "single scalar per night to serve as a sufficient statistic "
                "for every downstream restriction.",
        },
    },
    {
        "id": "bid",
        "title": "What the last room is worth",
        "levels": {
            "plain":
                "One room left on a quiet Tuesday: sell it to whoever walks "
                "in. One room left on the night of the film festival: do not, "
                "because someone will pay far more for it within the hour. The "
                "room is worth something beyond whatever is on its price tag, "
                "and that hidden value is the number a revenue system is "
                "really computing. Everything else follows from it.",
            "working":
                "The bid price is the opportunity cost of the last available "
                "room: what the hotel gives up by selling it now instead of "
                "holding it for whoever comes later. Every control comes off "
                "it. Publish a rate above it. Close a segment whose net rate "
                "falls below it. Set a minimum stay on a night where one night "
                "of business cannot clear it but three nights can.",
            "technical":
                "bid(c) = sum over classes j of (v_j minus v_j+1) times the "
                "probability that cumulative demand down to class j exceeds c, "
                "with classes ordered by net value. Four normal tail "
                "probabilities. It is order of the number of segments and not "
                "of the capacity, which is why one decision costs 1.22 "
                "milliseconds at fifty rooms and the same at eight thousand.",
            "formal":
                "V(t,c) minus V(t,c-1), where V solves V(t,c) = V(t-1,c) plus "
                "the sum over s of p_s max(0, r_s minus [V(t-1,c) minus "
                "V(t-1,c-1)]). Concavity of V in c gives the threshold policy "
                "and makes the first difference the dual variable on capacity. "
                "The closed form above is the derivative of the continuous "
                "relaxation R(c) = sum_j (v_j minus v_j+1) E[min(D_j, c)], "
                "which is the object EMSR approximates and of which "
                "Littlewood's rule is the two class case.",
        },
    },
    {
        "id": "unseen",
        "title": "The demand nobody wrote down",
        "levels": {
            "plain":
                "The books say the hotel sold 150 rooms on New Year's Eve. The "
                "hotel has 150 rooms. That number is not telling you about "
                "demand, it is telling you about the building. Four hundred "
                "people might have wanted it. Plan next year from the books "
                "and you will price that night exactly the same way, and lose "
                "exactly the same money, forever.",
            "working":
                "A property management system records what it sold and never "
                "what it turned away. Feed that history straight into a "
                "forecast and you teach a system to under price its best "
                "nights permanently. Pace divides each night's rooms by the "
                "acceptance rate at the price that night actually charged, so "
                "differently priced nights become comparable, then treats sold "
                "out nights as censored rather than complete.",
            "technical":
                "Projection detruncation. A sold out night is a right censored "
                "observation, so it is replaced by its conditional expectation, "
                "mu plus sigma times phi(z) over 1 minus Phi(z), with z the "
                "standardised censoring point, iterated to a fixed point on the "
                "class mean. House wide the correction runs about 8%. On the "
                "festival nights it is several times that, and those are the "
                "only nights where the rate decision is worth much.",
            "formal":
                "Demand is observed through a selection operator: what is "
                "recorded is min(D, c) whenever the capacity constraint binds. "
                "The naive estimator of E[D] from booked data is inconsistent "
                "and biased downward, and the bias is concentrated precisely "
                "where the constraint binds, which is where the decision has "
                "the highest value of information. Unconstraining inverts that "
                "operator under a distributional assumption, and the "
                "assumption is the price paid for identification.",
        },
    },
    {
        "id": "response",
        "title": "What happens when the rate moves",
        "levels": {
            "plain":
                "Raise the price and fewer people book. How many fewer is the "
                "entire question, and a hotel cannot answer it from its own "
                "records, because it only ever raised prices on nights that "
                "were already filling up. Those nights sold out at high prices. "
                "That does not show high prices caused them to sell out. It "
                "shows the manager could see what you can see.",
            "working":
                "Elasticity fitted from a hotel's own history is contaminated, "
                "because the incumbent set every rate by looking at how full "
                "the night already was. Rate and demand were decided together, "
                "so the comparison has the answer built into the question. The "
                "clean fix is to create the variation deliberately: give each "
                "night a randomly assigned multiplier and fit on that alone.",
            "technical":
                "Logistic choice curve: acceptance(r) = 2 over 1 plus "
                "exp(k(r/ref minus 1)), normalised to one at the reference "
                "rate, with k twice the local elasticity there. A constant "
                "elasticity power law is the obvious choice and it is a trap. "
                "Whenever the fitted elasticity comes out below one, revenue "
                "rises without bound as the rate rises and the optimizer walks "
                "into the ceiling. Negotiated corporate business fits below one "
                "almost every time.",
            "formal":
                "Rate is endogenous: it is a function of the same latent demand "
                "state that drives bookings, so least squares identifies a "
                "mixture of the demand curve and the incumbent's policy "
                "function rather than either one. Randomised rate assignment is "
                "an instrument, independent of the state by construction, and "
                "two stage least squares is consistent where the first stage "
                "separates. Where it does not, weak instrument bias exceeds the "
                "endogeneity bias it was introduced to remove, so the engine "
                "measures its own first stage and declines to use the estimate.",
        },
    },
    {
        "id": "measured",
        "title": "What was measured, including what failed",
        "levels": {
            "plain":
                "The engine made about six percent more money than a sensible "
                "manager doing the same job by hand. Two of the cleverest parts "
                "made no money at all. Both are still in the write up, with the "
                "numbers, because a portfolio that shows only the parts that "
                "worked is not evidence of anything.",
            "working":
                "+6.4% RevPAR and +5.5% GOPPAR against an occupancy based "
                "baseline over 184 settled nights, holding between +6.4% and "
                "+7.7% across four independently drawn markets. The randomised "
                "rate experiment cut the elasticity error from +27% to +10% and "
                "bought no revenue whatsoever. A network model over room types "
                "beat the nightly one by 1.8%, but only where room types and "
                "multi night stays were both present.",
            "technical":
                "Three policies replayed against an identical stream of booking "
                "requests, each request carrying its own willingness to pay, so "
                "a guest refused by one policy is genuinely still available to "
                "another. That is what makes it a counterfactual rather than a "
                "rescoring of fixed history. A fourth engine, handed the true "
                "price response directly, earned 0.38% less than the biased "
                "one, and the network model lost more than a percent on "
                "properties whose shape it did not fit.",
            "formal":
                "At an interior optimum the first order condition vanishes, so "
                "the loss from a parameter error is second order: a 27% "
                "elasticity error moves the chosen rate by four units and costs "
                "between 0.05% and 0.11% of contribution. The envelope theorem "
                "is why estimation effort and decision value come apart, and "
                "why establishing that an error is small is worth more than "
                "removing it. The same logic bounds the network result: "
                "additive nightly prices and deterministic network duals err in "
                "opposite directions, so the cruder model lands closer without "
                "being closer to correct.",
        },
    },
]


def as_payload() -> dict:
    """The structure the dashboard and the case page both read."""
    return {"order": list(LEVEL_ORDER), "labels": LEVEL_LABELS, "topics": TOPICS}
