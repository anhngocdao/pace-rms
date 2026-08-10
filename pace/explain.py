"""Turn a decision into something a duty manager can act on.

A revenue system that cannot say why it moved the rate does not get used.
The first time it makes an unpopular call, somebody overrides it, and after
enough overrides it is switched off.  So every recommendation carries the
same four things a human analyst would put in a note: what changed, what the
forecast says, what the room is worth, and what is holding the answer back.
"""

from typing import List

from .calendar import DOW_NAMES, class_label
from .config import SEGMENTS, Hotel
from .recommendation import Recommendation


def _money(hotel: Hotel, x: float) -> str:
    return "%s %s" % (hotel.currency, format(round(x), ","))


def _pct(x: float, digits: int = 0) -> str:
    return format(round(x * 100, digits), "." + str(digits) + "f") + "%"


def describe(hotel: Hotel, rec: Recommendation) -> Recommendation:
    drivers: List[str] = []

    move = rec.rate - rec.prior_rate
    if abs(move) < 0.01:
        action = "Hold at %s" % _money(hotel, rec.rate)
    elif move > 0:
        action = "Raise to %s (up %s)" % (_money(hotel, rec.rate), _money(hotel, move))
    else:
        action = "Cut to %s (down %s)" % (_money(hotel, rec.rate), _money(hotel, -move))

    pace_gap = rec.pace_index - 1.0
    if rec.otb == 0:
        pace_word = "nothing on the books yet"
    elif abs(pace_gap) < 0.08:
        pace_word = "pace is normal"
    elif pace_gap > 0:
        pace_word = "pace is %s ahead of a typical %s" % (
            _pct(pace_gap), class_label((rec.forecast.class_key if rec.forecast else ("", 0))))
    else:
        pace_word = "pace is %s behind a typical %s" % (
            _pct(-pace_gap), class_label((rec.forecast.class_key if rec.forecast else ("", 0))))

    rec.headline = "%s. %s." % (action, pace_word[0].upper() + pace_word[1:])

    drivers.append("On the books %d of %d rooms at %d days out (%s of the house)."
                   % (rec.otb, hotel.rooms, rec.lead, _pct(rec.otb / hotel.rooms)))
    drivers.append("Forecast %s rooms, %s occupancy, %s RevPAR."
                   % (round(rec.expected_final), _pct(rec.expected_occupancy),
                      _money(hotel, rec.expected_revpar)))

    if rec.censoring_uplift > 1.02:
        drivers.append("Unconstrained demand runs %s above booked demand on nights like this, "
                       "so the booked history understates it."
                       % _pct(rec.censoring_uplift - 1.0))

    if rec.bid_price <= 0.5:
        drivers.append("Bid price is nil: rooms are not scarce, so the rate is set purely "
                       "by where the demand curve pays best.")
    else:
        drivers.append("Bid price %s: that is what the last room is worth if it is held for "
                       "later demand, and the rate must clear it."
                       % _money(hotel, rec.bid_price))

    for name in rec.events:
        drivers.append("Calendar: %s." % name)
    for sig in rec.signals:
        drivers.append("Signal %s (x%.2f)%s"
                       % (sig.name, sig.multiplier,
                          ": " + sig.reason if sig.reason else "."))

    if rec.authorized > hotel.rooms:
        drivers.append("Authorised %d rooms against %d physical: expected cancellations and "
                       "no-shows justify selling %d over."
                       % (rec.authorized, hotel.rooms, rec.authorized - hotel.rooms))

    if rec.cta:
        drivers.append("Closed to arrival: no length of stay pays for the nights it consumes.")
    elif rec.mlos > 1:
        drivers.append("Minimum stay %d nights: a single night no longer covers the bid price, "
                       "but a stay reaching into the softer nights either side does."
                       % rec.mlos)

    if rec.closed:
        names = ", ".join(SEGMENTS[c].name for c in sorted(rec.closed))
        drivers.append("Closed to %s: the net rate falls below the bid price." % names)

    if rec.experiment_arm:
        drivers.append("This night is in the rate experiment, assigned %+d%% at random. "
                       "The variation is what makes the price response measurable at all, "
                       "and the assignment depends on the date and nothing else."
                       % round(rec.experiment_arm * 100))

    if rec.bound_by == "rate ceiling":
        drivers.append("The rate is at the ceiling. Beyond this point the answer is length of "
                       "stay control, not price.")
    elif rec.bound_by == "bid price floor":
        drivers.append("The demand curve wanted a lower rate, but the bid price floor overrode "
                       "it: selling cheaper now costs more than the booking is worth.")

    for line in rec.rule_trace:
        drivers.append("Rule applied, %s." % line)

    rec.drivers = drivers
    rec.narrative = _narrative(hotel, rec)
    rec.confidence = _confidence(rec)
    return rec


def _narrative(hotel: Hotel, rec: Recommendation) -> str:
    day = "%s %s" % (DOW_NAMES[rec.stay_date.weekday()], rec.stay_date.isoformat())
    bits = ["%s is %d days out with %d rooms sold and a forecast of %d."
            % (day, rec.lead, rec.otb, round(rec.expected_final))]
    if rec.bid_price > 0.5:
        bits.append("The last room is worth %s, so the rate is held at or above that line."
                    % _money(hotel, rec.bid_price))
    else:
        bits.append("Rooms are not scarce, so the rate is chosen to maximise revenue on volume "
                    "rather than to protect inventory.")
    if rec.mlos > 1 or rec.cta or rec.closed:
        bits.append("Rate alone cannot express the decision here, so the restriction does the "
                    "rest of the work.")
    if rec.expected_occupancy > 0.95:
        bits.append("Watch this one: it is forecast to sell out, and any further demand is "
                    "revenue that walks to a competitor.")
    return " ".join(bits)


def _confidence(rec: Recommendation) -> str:
    obs = rec.forecast.class_observations if rec.forecast else 0
    if rec.lead > 120 or obs < 8:
        return "low"
    if rec.lead > 45 or obs < 20:
        return "medium"
    return "high"
