from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import pandas as pd

from bot_core import EASTERN
from strategies.pmb.strategy import detect_break_and_retest, scan_dataframe


def _bars(*post_orb_rows: tuple[int, float, float, float, float]) -> pd.DataFrame:
    start = datetime(2026, 8, 31, 9, 30, tzinfo=EASTERN)
    rows = []
    for minute in range(5):
        rows.append((start + timedelta(minutes=minute), 99.8, 100.0, 99.7, 99.9, 1_000))
    for minute, open_, high, low, close in post_orb_rows:
        rows.append((start + timedelta(minutes=minute), open_, high, low, close, 1_500))
    return pd.DataFrame(
        rows,
        columns=["Datetime", "Open", "High", "Low", "Close", "Volume"],
    ).set_index("Datetime")


class BreakAndRetestTests(unittest.TestCase):
    def test_one_minute_retest_uses_five_minute_orb_analysis(self):
        rows = []
        prior_start = datetime(2026, 8, 28, 9, 30, tzinfo=EASTERN)
        for minute in range(390):
            rows.append((prior_start + timedelta(minutes=minute), 99.95, 100.05, 99.90, 100.0, 1_000))
        current_start = datetime(2026, 8, 31, 9, 30, tzinfo=EASTERN)
        for minute in range(5):
            rows.append((current_start + timedelta(minutes=minute), 99.90, 100.0, 99.80, 99.95, 1_500))
        rows.extend([
            (current_start + timedelta(minutes=5), 100.25, 101.20, 100.25, 101.0, 2_000),
            (current_start + timedelta(minutes=6), 101.0, 101.20, 100.80, 101.10, 1_800),
            (current_start + timedelta(minutes=7), 101.0, 101.10, 100.15, 100.80, 2_200),
        ])
        intraday = pd.DataFrame(
            rows,
            columns=["Datetime", "Open", "High", "Low", "Close", "Volume"],
        ).set_index("Datetime")
        daily = pd.DataFrame(
            [{"Open": 100.0, "High": 100.20, "Low": 98.0, "Close": 99.5, "Volume": 1_000_000}],
            index=[pd.Timestamp("2026-08-28", tz=EASTERN)],
        )

        result = scan_dataframe(
            "TEST",
            intraday,
            daily,
            use_rvol_score=True,
            min_score=70,
            orb_minutes=5,
            min_session_bars=2,
            require_retest=True,
            analysis_bar_minutes=5,
        )

        self.assertEqual(result["ORB Bars"], 1)
        self.assertTrue(result["Retest Confirmed"])
        self.assertEqual(result["Retest Trigger Level"], 100.20)
        self.assertIn("09:37", result["Retest Time"])

    def test_call_confirms_first_fresh_retest_of_higher_orb_pdh_trigger(self):
        bars = _bars(
            (5, 101.10, 101.40, 101.08, 101.30),
            (6, 101.30, 101.45, 101.20, 101.35),
            (7, 101.20, 101.30, 100.95, 101.15),
        )
        result = detect_break_and_retest(
            bars,
            direction="CALL",
            orb_high=100.0,
            orb_low=99.0,
            pdh=101.0,
            pdl=98.0,
        )

        self.assertTrue(result["confirmed"])
        self.assertEqual(result["trigger_level"], 101.0)
        self.assertEqual(result["elapsed_minutes"], 2.0)

    def test_put_confirms_first_fresh_retest_of_lower_orb_pdl_trigger(self):
        bars = _bars(
            (5, 97.90, 97.92, 97.60, 97.70),
            (6, 97.70, 97.82, 97.55, 97.65),
            (7, 97.80, 98.05, 97.70, 97.85),
        )
        result = detect_break_and_retest(
            bars,
            direction="PUT",
            orb_high=100.0,
            orb_low=99.0,
            pdh=102.0,
            pdl=98.0,
        )

        self.assertTrue(result["confirmed"])
        self.assertEqual(result["trigger_level"], 98.0)

    def test_stale_retest_is_not_actionable(self):
        bars = _bars(
            (5, 101.10, 101.40, 101.08, 101.30),
            (6, 101.20, 101.30, 100.95, 101.15),
            (7, 101.15, 101.40, 101.10, 101.35),
        )
        result = detect_break_and_retest(
            bars,
            direction="CALL",
            orb_high=100.0,
            orb_low=99.0,
            pdh=101.0,
            pdl=98.0,
        )

        self.assertFalse(result["confirmed"])
        self.assertIn("stale", result["reason"].lower())

    def test_failed_first_retest_cannot_be_replaced_by_later_reclaim(self):
        bars = _bars(
            (5, 101.10, 101.40, 101.08, 101.30),
            (6, 101.10, 101.20, 100.80, 100.85),
            (7, 101.10, 101.30, 100.95, 101.20),
        )
        result = detect_break_and_retest(
            bars,
            direction="CALL",
            orb_high=100.0,
            orb_low=99.0,
            pdh=101.0,
            pdl=98.0,
        )

        self.assertFalse(result["confirmed"])
        self.assertIn("failed", result["reason"].lower())

    def test_retest_after_45_minutes_is_rejected(self):
        bars = _bars(
            (5, 101.10, 101.40, 101.08, 101.30),
            (51, 101.20, 101.30, 100.95, 101.15),
        )
        result = detect_break_and_retest(
            bars,
            direction="CALL",
            orb_high=100.0,
            orb_low=99.0,
            pdh=101.0,
            pdl=98.0,
            max_minutes=45,
        )

        self.assertFalse(result["confirmed"])
        self.assertIn("too late", result["reason"].lower())


if __name__ == "__main__":
    unittest.main()
