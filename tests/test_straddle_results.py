from __future__ import annotations

import unittest

import pandas as pd

from backtester.straddle_results import (
    StraddleResultsConfig,
    scan_straddle_sessions,
    simulate_straddles,
    summarize_straddles,
)


def _stock_session(outcome: str) -> pd.DataFrame:
    index = pd.date_range("2026-09-01 09:30", periods=6, freq="5min", tz="America/New_York")
    rows = [
        (100.0, 100.5, 99.5, 100.0),
        (100.0, 101.0, 99.8, 100.5),
        (100.5, 100.8, 100.0, 100.4),
    ]
    if outcome == "CALL":
        rows += [
            (100.8, 101.4, 100.7, 101.2),
            (101.2, 101.8, 101.0, 101.5),
            (101.5, 102.0, 101.4, 101.9),
        ]
    else:
        rows += [
            (100.2, 100.8, 99.8, 100.4),
            (100.4, 100.7, 99.9, 100.2),
            (100.2, 100.6, 99.7, 100.1),
        ]
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


def _option_bars(opens: list[float], closes: list[float]) -> pd.DataFrame:
    index = pd.to_datetime(
        ["2026-09-01 09:30", "2026-09-01 10:00", "2026-09-01 10:05"],
    ).tz_localize("America/New_York")
    return pd.DataFrame(
        {
            "Open": opens,
            "High": [max(a, b) for a, b in zip(opens, closes)],
            "Low": [min(a, b) for a, b in zip(opens, closes)],
            "Close": closes,
            "Volume": 10,
        },
        index=index,
    )


class StraddleResultsTests(unittest.TestCase):
    def test_combined_return_pays_for_both_legs(self):
        sessions = scan_straddle_sessions({"AAPL": _stock_session("CALL")})

        def pair_provider(**_kwargs):
            call = _option_bars([4.0, 5.2, 7.0], [4.0, 7.0, 8.0])
            put = _option_bars([4.0, 2.8, 2.5], [4.0, 2.5, 2.0])
            return (
                {"expiry": "20260911", "strike": 100.0, "localSymbol": "CALL"},
                call,
                {"expiry": "20260911", "strike": 100.0, "localSymbol": "PUT"},
                put,
            )

        settings = StraddleResultsConfig(
            slippage_pct=0.0,
            commission_per_contract=0.0,
            trailing_trigger_pct=200.0,
            winner_target_pct=300.0,
        )
        results = simulate_straddles(sessions, pair_provider, settings)
        trade = results.iloc[0]

        self.assertEqual(trade["direction"], "CALL")
        self.assertEqual(float(trade["combined_entry_debit"]), 8.0)
        self.assertEqual(float(trade["put_exit"]), 2.8)
        self.assertEqual(float(trade["call_exit"]), 8.0)
        self.assertEqual(float(trade["realized_pnl"]), 280.0)
        self.assertEqual(float(trade["combined_return_pct"]), 35.0)

        summary = summarize_straddles(results).iloc[0]
        self.assertEqual(int(summary["Completed Pairs"]), 1)
        self.assertEqual(float(summary["Win Rate %"]), 100.0)

    def test_no_break_closes_both_legs_at_confirmation(self):
        sessions = scan_straddle_sessions({"AAPL": _stock_session("NONE")})

        def pair_provider(**_kwargs):
            call = _option_bars([4.0, 3.5, 3.4], [4.0, 3.4, 3.3])
            put = _option_bars([4.0, 3.6, 3.5], [4.0, 3.5, 3.4])
            info = {"expiry": "20260911", "strike": 100.0}
            return info, call, info, put

        settings = StraddleResultsConfig(slippage_pct=0.0, commission_per_contract=0.0)
        trade = simulate_straddles(sessions, pair_provider, settings).iloc[0]

        self.assertFalse(bool(trade["broke_orb"]))
        self.assertEqual(trade["exit_reason"], "No ORB break; both legs closed")
        self.assertEqual(float(trade["realized_pnl"]), -90.0)

    def test_rejects_unmatched_call_and_put_contracts(self):
        sessions = scan_straddle_sessions({"AAPL": _stock_session("CALL")})

        def pair_provider(**_kwargs):
            bars = _option_bars([4.0, 4.5, 5.0], [4.0, 5.0, 5.5])
            return (
                {"expiry": "20260911", "strike": 100.0}, bars,
                {"expiry": "20260911", "strike": 102.5}, bars,
            )

        result = simulate_straddles(sessions, pair_provider).iloc[0]
        self.assertEqual(result["option_status"], "UNAVAILABLE")
        self.assertIn("matched pair", result["option_error"])


if __name__ == "__main__":
    unittest.main()
