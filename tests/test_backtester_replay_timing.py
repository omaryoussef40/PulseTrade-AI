from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from backtester.replay import MarketReplayEngine, ReplayConfig
from backtester.controller import (
    StrategyLabSettings,
    _opening_stage_directional_passes,
    _scan_strategy_replay,
    _staged_replay_settings,
)
from backtester.simulator import _completed_bar_view


EASTERN = "America/New_York"


def _bars(start: str, periods: int, frequency: str) -> pd.DataFrame:
    index = pd.date_range(start, periods=periods, freq=frequency, tz=EASTERN)
    return pd.DataFrame(
        {
            "Open": range(100, 100 + periods),
            "High": range(101, 101 + periods),
            "Low": range(99, 99 + periods),
            "Close": range(100, 100 + periods),
            "Volume": 1_000,
        },
        index=index,
    )


class ReplayTimingTests(unittest.TestCase):
    def test_strategy_replay_forwards_minimum_session_bars(self):
        settings = StrategyLabSettings(
            symbols=["COIN"],
            orb_minutes=15,
            min_session_bars=4,
            require_break_retest=True,
            retest_tolerance_pct=0.15,
            retest_max_minutes=30,
        )

        with patch("backtester.controller.scan_replay_history", return_value={"Signal": "WAIT"}) as scan:
            _scan_strategy_replay("PMB", "COIN", pd.DataFrame(), settings)

        self.assertEqual(scan.call_args.kwargs["min_session_bars"], 4)
        self.assertTrue(scan.call_args.kwargs["require_retest"])
        self.assertEqual(scan.call_args.kwargs["retest_tolerance_pct"], 0.15)
        self.assertEqual(scan.call_args.kwargs["retest_max_minutes"], 30)

    def test_fifteen_minute_confirmation_is_not_visible_at_0945(self):
        raw = _bars("2026-09-10 09:30", periods=2, frequency="15min")
        engine = MarketReplayEngine(
            {"AAPL": raw},
            ReplayConfig(interval="15m", orb_minutes=15, first_signal_minutes=5, min_session_bars=2),
        )

        events = list(engine.events())

        self.assertEqual([event.timestamp.strftime("%H:%M") for event in events], ["09:45", "10:00"])
        self.assertEqual(len(events[0].history), 1)
        self.assertFalse(events[0].scanner_allowed)
        self.assertEqual(len(events[1].history), 2)
        self.assertTrue(events[1].scanner_allowed)
        self.assertEqual(events[1].close, 101.0)

    def test_live_staged_timeline_switches_orb_and_retest_rules(self):
        settings = StrategyLabSettings(symbols=["AAPL"], use_staged_timeline=True)

        opening_name, opening = _staged_replay_settings(pd.Timestamp("2026-09-14 09:40", tz=EASTERN), settings)
        normal_name, normal = _staged_replay_settings(pd.Timestamp("2026-09-14 09:50", tz=EASTERN), settings)
        retest_name, retest = _staged_replay_settings(pd.Timestamp("2026-09-14 11:30", tz=EASTERN), settings)
        after_name, after = _staged_replay_settings(pd.Timestamp("2026-09-14 13:35", tz=EASTERN), settings)

        self.assertEqual((opening_name, opening.orb_minutes, opening.min_score, opening.min_rvol), ("opening_orb_5m", 5, 94.0, 1.5))
        self.assertTrue(opening.use_rvol_filter)
        self.assertFalse(opening.require_break_retest)
        self.assertEqual((normal_name, normal.orb_minutes, normal.require_break_retest), ("orb_15m", 15, False))
        self.assertEqual((retest_name, retest.orb_minutes, retest.require_break_retest), ("break_retest_15m", 15, True))
        self.assertEqual(after_name, "after_retest_window")
        self.assertIsNone(after)

    def test_opening_stage_requires_the_same_directional_conditions_as_live(self):
        call = {
            "Signal": "CALL",
            "ORB Up": True,
            "PDH Break": True,
            "Above VWAP": True,
            "EMA Bullish": True,
        }

        self.assertTrue(_opening_stage_directional_passes(call))
        call["PDH Break"] = False
        self.assertFalse(_opening_stage_directional_passes(call))

    def test_five_minute_confirmation_first_becomes_available_at_0950(self):
        raw = _bars("2026-09-10 09:30", periods=4, frequency="5min")
        engine = MarketReplayEngine(
            {"AAPL": raw},
            ReplayConfig(interval="5m", orb_minutes=15, first_signal_minutes=5, min_session_bars=2),
        )

        scanner_events = list(engine.events(only_scanner_allowed=True))

        self.assertEqual(len(scanner_events), 1)
        self.assertEqual(scanner_events[0].timestamp.strftime("%H:%M"), "09:50")
        self.assertEqual(len(scanner_events[0].history), 4)

    def test_option_bar_is_available_at_close_not_start(self):
        raw = _bars("2026-09-10 09:45", periods=2, frequency="5min")

        completed = _completed_bar_view(raw)

        self.assertEqual(completed.index[0].strftime("%H:%M"), "09:50")
        self.assertEqual(completed.index[1].strftime("%H:%M"), "09:55")
        self.assertEqual(float(completed.iloc[0]["Close"]), 100.0)


if __name__ == "__main__":
    unittest.main()
