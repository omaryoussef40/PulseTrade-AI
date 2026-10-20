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
    def evaluate(self, intraday, today, config, direction="CALL"):
        return evaluate_breakout_follow_through(intraday, today, direction=direction, orb_high=101.0, orb_low=99.0, orb_bars=3, config=config)

    def test_volume_only_accepts_breakout_without_next_candle(self):
        intraday, today = _frames()
        today = today.iloc[:-1]
        intraday = intraday.loc[intraday.index <= today.index[-1]]
        result = self.evaluate(intraday, today, {**CONFIG, "volume_enabled": True, "follow_through_enabled": False})
        self.assertTrue(result["confirmed"])
        self.assertTrue(result["volume_confirmed"])
        self.assertIsNone(result["follow_through_time"])

    def test_volume_only_still_rejects_weak_volume(self):
        intraday, today = _frames()
        intraday.loc[today.index[-2], "Volume"] = 1000
        today = intraday.loc[today.index]
        result = self.evaluate(intraday, today, {**CONFIG, "volume_enabled": True, "follow_through_enabled": False})
        self.assertFalse(result["confirmed"])
        self.assertIn("volume too weak", result["reason"])

    def test_volume_only_still_requires_historical_baseline(self):
        _, today = _frames()
        result = self.evaluate(today, today, {**CONFIG, "volume_enabled": True, "follow_through_enabled": False})
        self.assertFalse(result["confirmed"])
        self.assertIn("baseline unavailable", result["reason"])

    def test_follow_through_only_needs_no_volume_history(self):
        _, today = _frames()
        result = self.evaluate(today, today, {**CONFIG, "volume_enabled": False, "follow_through_enabled": True})
        self.assertTrue(result["confirmed"])
        self.assertIsNone(result["volume_ratio"])

    def test_follow_through_only_still_waits_for_next_candle(self):
        _, today = _frames()
        today = today.iloc[:-1]
        result = self.evaluate(today, today, {**CONFIG, "volume_enabled": False, "follow_through_enabled": True})
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["reason"], "Waiting for the next candle")

    def test_explicit_disabled_controls_override_legacy_enabled(self):
        result = self.evaluate(pd.DataFrame(), pd.DataFrame(), {**CONFIG, "volume_enabled": False, "follow_through_enabled": False})
        self.assertFalse(result["required"])
        self.assertTrue(result["confirmed"])

    def test_explicit_volume_control_overrides_legacy_disabled_for_put(self):
        intraday, today = _frames()
        # Mirror prices around 100 to exercise the short direction.
        for frame in (intraday, today):
            high, low = frame["High"].copy(), frame["Low"].copy()
            frame["High"], frame["Low"] = 200 - low, 200 - high
            frame["Open"], frame["Close"] = 200 - frame["Open"], 200 - frame["Close"]
        today = today.iloc[:-1]
        result = self.evaluate(intraday.loc[intraday.index <= today.index[-1]], today, {**CONFIG, "enabled": False, "volume_enabled": True, "follow_through_enabled": False}, direction="PUT")
        self.assertTrue(result["confirmed"])

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
