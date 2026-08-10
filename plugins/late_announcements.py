"""Example extension: demand signal.

Three citywide events that were announced after the annual calendar was
already set.  This is the ordinary case, not the exotic one: a tour date, a
championship, a conference that moved cities in March.  The class based
forecast cannot know about any of them, because there is no history of a
night like that.

Two things happen here, and it is worth being clear about which is which.

  The events are added to the city calendar, which is the world.  They
  represent demand that genuinely exists.

  A signal is registered, which is what the engine is told.  Its lift fades
  as arrival approaches, because by then the booking pace has picked the
  event up on its own and adding a second lift on top would double count the
  same guests.  Full effect beyond fifty days out, nothing inside a week.

Drop a file like this into plugins/ and it is loaded on the next run.  No
registration anywhere else, no change to the engine.
"""

import datetime as dt

from pace.calendar import Event
from pace.plugins import Signal, signal
from pace.scenario import CALENDAR

ANNOUNCEMENTS = [
    ("Arena tour, two nights", dt.date(2025, 1, 31), 2, 1.45),
    ("Winter technology conference", dt.date(2025, 2, 4), 3, 1.50),
    ("International curling championship", dt.date(2025, 3, 7), 6, 1.35),
]

for _name, _start, _nights, _mult in ANNOUNCEMENTS:
    CALENDAR.add(Event(_name, _start, _start + dt.timedelta(days=_nights - 1), _mult,
                       "announced after the annual calendar was set"))

_INDEX = {}
for _name, _start, _nights, _mult in ANNOUNCEMENTS:
    for _i in range(_nights):
        _INDEX[_start + dt.timedelta(days=_i)] = (_name, _mult)


@signal("late_announcement")
def late_announcement(stay_date, ctx):
    hit = _INDEX.get(stay_date)
    if hit is None:
        return None
    name, mult = hit
    lead = ctx["forecast"].lead if ctx.get("forecast") else 0
    fade = max(0.0, min(1.0, (lead - 7) / 45.0))
    lift = 1.0 + (mult - 1.0) * fade
    if lift <= 1.001:
        return None
    return Signal("late_announcement", lift,
                  "%s, not in the historical pattern for this date" % name)
