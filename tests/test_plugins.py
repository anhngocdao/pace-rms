"""Loading the plugin directory must be idempotent.

pipeline.run() loads plugins on every call. Before this test existed a second
call in the same process re-executed every plugin file, so a signal or rule
was registered twice and an event a plugin adds to the shared calendar was
added twice. Scores in the evaluation window happened to survive; the demand
stream and the denial counts did not.
"""
import os
import unittest

from pace import plugins
from pace import scenario as S

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(ROOT, "plugins")


class LoadingTwice(unittest.TestCase):

    def setUp(self):
        plugins.reset()
        plugins.load(PLUGIN_DIR)

    def tearDown(self):
        plugins.reset()

    def test_registers_each_signal_and_rule_once(self):
        plugins.load(PLUGIN_DIR)
        reg = plugins.registered()
        self.assertEqual(reg["signals"], sorted(set(reg["signals"])))
        self.assertEqual(reg["rules"], sorted(set(reg["rules"]), key=reg["rules"].index))
        self.assertEqual(len(reg["signals"]), len(set(reg["signals"])))
        self.assertEqual(len(reg["rules"]), len(set(reg["rules"])))

    def test_does_not_grow_the_shared_calendar(self):
        before = len(S.CALENDAR.events)
        plugins.load(PLUGIN_DIR)
        self.assertEqual(len(S.CALENDAR.events), before)

    def test_reset_then_load_rebuilds_the_registry(self):
        plugins.reset()
        self.assertEqual(plugins.registered(), {"signals": [], "rules": []})
        loaded = plugins.load(PLUGIN_DIR)
        self.assertEqual(loaded, ["late_announcements", "rate_guardrails"])
        self.assertEqual(plugins.registered()["signals"], ["late_announcement"])
        self.assertEqual(plugins.registered()["rules"], ["brand_floor", "charm_pricing"])


class CalendarDedupes(unittest.TestCase):

    def test_adding_the_same_event_twice_keeps_one(self):
        import datetime as dt
        from pace.calendar import Event, EventCalendar
        cal = EventCalendar()
        ev = Event("Twice", dt.date(2025, 3, 1), dt.date(2025, 3, 3), 1.4)
        cal.add(ev)
        cal.add(Event("Twice", dt.date(2025, 3, 1), dt.date(2025, 3, 3), 1.4))
        self.assertEqual(cal.events, [ev])
        self.assertAlmostEqual(cal.multiplier(dt.date(2025, 3, 2)), 1.4)


if __name__ == "__main__":
    unittest.main()
