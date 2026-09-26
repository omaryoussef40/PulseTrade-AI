from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

import pandas as pd

from strategies.pmb.strategy import evaluate_breakout_follow_through


EASTERN = ZoneInfo("America/New_York")
CONFIG = {
    "enabled": True,
    "min_breakout_volume_ratio": 2.0,
    "min_volume_sessions": 3,
    "require_extension": True,
}


def _bar(timestamp, high, low, close, volume):
    return {"timestamp": timestamp, "Open": close, "High": high, "Low": low, "Close": close, "Volume": volume}


def _frames(follow_close=102.4, follow_high=102.8):
    rows = []
    for day in (1, 2, 3):
        for hour, minute in ((9, 30), (9, 35), (9, 40), (9, 45), (9, 50)):
            rows.append(_bar(datetime(2026, 9, day, hour, minute, tzinfo=EASTERN), 100.5, 99.5, 100.0, 1000))
    today = [
        _bar(datetime(2026, 9, 4, 9, 30, tzinfo=EASTERN), 100.0, 99.0, 99.5, 900),
        _bar(datetime(2026, 9, 4, 9, 35, tzinfo=EASTERN), 100.5, 99.4, 100.0, 900),
        _bar(datetime(2026, 9, 4, 9, 40, tzinfo=EASTERN), 101.0, 99.8, 100.5, 900),
        _bar(datetime(2026, 9, 4, 9, 45, tzinfo=EASTERN), 102.0, 100.4, 101.5, 2100),
        _bar(datetime(2026, 9, 4, 9, 50, tzinfo=EASTERN), follow_high, 101.1, follow_close, 1300),
    ]
    intraday = pd.DataFrame(rows + today).set_index("timestamp")
    return intraday, intraday[intraday.index.date == datetime(2026, 9, 4).date()]


class BreakoutFollowThroughTests(unittest.TestCase):
    def test_accepts_high_volume_breakout_with_immediate_extension(self):
        intraday, today = _frames()
        result = evaluate_breakout_follow_through(intraday, today, direction="CALL", orb_high=101.0, orb_low=99.0, orb_bars=3, config=CONFIG)

        self.assertTrue(result["confirmed"])
        self.assertEqual(result["volume_ratio"], 2.1)

    def test_rejects_follow_through_that_closes_back_inside_range(self):
        intraday, today = _frames(follow_close=100.9, follow_high=102.8)
        result = evaluate_breakout_follow_through(intraday, today, direction="CALL", orb_high=101.0, orb_low=99.0, orb_bars=3, config=CONFIG)

        self.assertFalse(result["confirmed"])
        self.assertIn("inside the opening range", result["reason"])

    def test_rejects_stale_follow_through(self):
        intraday, today = _frames()
        extra = _bar(datetime(2026, 9, 4, 9, 55, tzinfo=EASTERN), 103.0, 102.0, 102.5, 1000)
        today = pd.concat([today, pd.DataFrame([extra]).set_index("timestamp")])
        intraday = pd.concat([intraday, pd.DataFrame([extra]).set_index("timestamp")])
        result = evaluate_breakout_follow_through(intraday, today, direction="CALL", orb_high=101.0, orb_low=99.0, orb_bars=3, config=CONFIG)

        self.assertFalse(result["confirmed"])
        self.assertEqual(result["reason"], "Follow-through confirmation is stale")
