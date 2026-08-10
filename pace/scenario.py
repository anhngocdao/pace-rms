"""The scenario the demo runs on.

One property, one city, a two and a quarter year window.  Dates are fixed so
every run of the project reproduces exactly the same numbers.
"""

import datetime as dt

from .calendar import default_calendar
from .config import DEFAULT_HOTEL

HOTEL = DEFAULT_HOTEL

FIRST_ARRIVAL = dt.date(2023, 1, 1)
LAST_ARRIVAL = dt.date(2025, 4, 30)
FIRST_BOOKING_DAY = FIRST_ARRIVAL - dt.timedelta(days=HOTEL.max_lead)

# The day the engine takes the house over from the incumbent manager.
HANDOVER = dt.date(2024, 1, 1)

# The day the dashboard is written.  Everything after this is a forecast.
TODAY = dt.date(2025, 1, 15)

# A fully settled, fully engine-controlled window used to score the policies.
EVAL_FIRST = dt.date(2024, 7, 1)
EVAL_LAST = dt.date(2024, 12, 31)

# The forward window the dashboard prices.
DASHBOARD_DAYS = 90
DASHBOARD_FIRST = TODAY + dt.timedelta(days=1)
DASHBOARD_LAST = DASHBOARD_FIRST + dt.timedelta(days=DASHBOARD_DAYS - 1)

SEED = 20250115

CALENDAR = default_calendar(range(FIRST_ARRIVAL.year - 1, LAST_ARRIVAL.year + 2))
