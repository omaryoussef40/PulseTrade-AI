from __future__ import annotations

import unittest

import pandas as pd

from backtester.orb_results import (
    ORBResultsConfig,
    add_option_outcomes,
    scan_orb_sessions,
    summarize_orb_results,
)


def _stock_session(day: str, outcome: str) -> pd.DataFrame:
    index = pd.date_range(f"{day} 09:30", periods=6, freq="5min", tz="America/New_York")
    opening = [
        (100.0, 100.5, 99.5, 100.0),
        (100.0, 101.0, 99.8, 100.5),
        (100.5, 100.8, 100.0, 100.4),
    ]
    if outcome == "CALL":
        confirmation = [
            (100.8, 101.4, 100.7, 101.2),
            (101.2, 101.8, 101.0, 101.5),
            (101.5, 102.0, 101.4, 101.9),
        ]
    elif outcome == "PUT":
        confirmation = [
            (99.6, 99.8, 99.1, 99.3),
            (99.3, 99.5, 98.7, 99.0),
            (99.0, 99.1, 98.3, 98.5),
        ]
    else:
        confirmation = [
            (100.2, 100.8, 99.8, 100.4),
            (100.4, 100.7, 99.9, 100.2),
            (100.2, 100.6, 99.7, 100.1),
        ]
    rows = opening + confirmation
    return pd.DataFrame(
        {
            "Open": [row[0] for row in rows],
            "High": [row[1] for row in rows],
            "Low": [row[2] for row in rows],
            "Close": [row[3] for row in rows],
            "Volume": 1_000,
        },
        index=index,
    )


class ORBResultsTests(unittest.TestCase):
    def test_scans_breaks_and_measures_twenty_percent_contract_gain(self):
        data = {
            "AAPL": pd.concat([
                _stock_session("2026-09-08", "CALL"),
                _stock_session("2026-09-09", "PUT"),
                _stock_session("2026-09-10", "NONE"),
            ])
        }
        sessions = scan_orb_sessions(data)
        calls = []

        def option_provider(**kwargs):
            calls.append(kwargs)
            start = pd.Timestamp(kwargs["reference_time"])
            index = pd.date_range(start, periods=4, freq="5min")
            max_high = 2.50 if kwargs["signal"] == "CALL" else 2.20
            bars = pd.DataFrame(
                {
                    "Open": [2.00, 2.05, 2.10, 2.10],
                    "High": [2.10, max_high, 2.15, 2.12],
                    "Low": [1.95, 2.00, 2.00, 2.00],
                    "Close": [2.05, 2.10, 2.10, 2.05],
                    "Volume": 10,
                },
                index=index,
            )
            return {"expiry": "20260918", "strike": 100.0, "localSymbol": "TEST"}, bars

        enriched = add_option_outcomes(sessions, option_provider)
        summary = summarize_orb_results(enriched).iloc[0]

        self.assertEqual(len(sessions), 3)
        self.assertEqual(sessions["direction"].tolist(), ["CALL", "PUT", ""])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(pd.Timestamp(call["reference_time"]).strftime("%H:%M") == "09:45" for call in calls))
        self.assertTrue(all(call["option_dte"] == 7 for call in calls))
        self.assertEqual(int(summary["ORB Breaks"]), 2)
        self.assertEqual(int(summary["Contracts Found"]), 2)
        self.assertEqual(int(summary["+20% Hits"]), 1)
        self.assertEqual(float(summary["+20% Hit Rate %"]), 50.0)

    def test_unavailable_option_is_excluded_from_hit_rate_denominator(self):
        sessions = scan_orb_sessions({"AAPL": _stock_session("2026-09-08", "CALL")})

        def unavailable_provider(**_kwargs):
            raise RuntimeError("Expired option history unavailable")

        enriched = add_option_outcomes(sessions, unavailable_provider)
        summary = summarize_orb_results(enriched).iloc[0]

        self.assertEqual(enriched.iloc[0]["option_status"], "UNAVAILABLE")
        self.assertEqual(int(summary["ORB Breaks"]), 1)
        self.assertEqual(int(summary["Contracts Found"]), 0)
        self.assertEqual(float(summary["+20% Hit Rate %"]), 0.0)


if __name__ == "__main__":
    unittest.main()
