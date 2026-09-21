from __future__ import annotations

import unittest

import pandas as pd

from backtester.simulator import OptionSimulationConfig, simulate_option_trades_with_decisions, summarize_trades


EASTERN = "America/New_York"


def _stock_bars() -> dict[str, pd.DataFrame]:
    index = pd.date_range("2026-09-10 09:30", "2026-09-10 16:00", freq="5min", tz=EASTERN)
    frame = pd.DataFrame(
        {
            "Open": 100.0,
            "High": 100.5,
            "Low": 99.5,
            "Close": 100.0,
            "Volume": 1000,
        },
        index=index,
    )
    return {symbol: frame.copy() for symbol in ("AAPL", "MSFT")}


def _signals() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-09-10 09:50", tz=EASTERN),
                "symbol": "AAPL",
                "signal": "CALL",
                "price": 100.0,
                "selection_rank": 1,
            },
            {
                "timestamp": pd.Timestamp("2026-09-10 10:00", tz=EASTERN),
                "symbol": "MSFT",
                "signal": "CALL",
                "price": 100.0,
                "selection_rank": 1,
            },
        ]
    )


class OverlappingPositionTests(unittest.TestCase):
    def test_open_trade_keeps_capital_reserved_for_later_signal(self):
        def option_bars_provider(**kwargs):
            start = pd.Timestamp(kwargs["reference_time"]).tz_convert(EASTERN)
            index = pd.DatetimeIndex([start, pd.Timestamp("2026-09-10 15:55", tz=EASTERN)])
            bars = pd.DataFrame({"Close": [6.0, 6.0]}, index=index)
            return {"expiry": "20260918", "strike": 100.0, "right": "C"}, bars

        config = OptionSimulationConfig(
            starting_capital=1000.0,
            max_trades_per_day=3,
            sizing_method="fixed_dollar",
            max_spend_per_trade=1000.0,
            max_daily_capital=2000.0,
            reserve_capital_for_remaining_trades=False,
            recycle_capital_after_exit=True,
            option_bars_provider=option_bars_provider,
            slippage_pct=0.0,
            max_consecutive_losses=99,
            max_daily_drawdown_pct=100.0,
            entry_cutoff_time=pd.Timestamp("11:00").time(),
        )

        trades, decisions = simulate_option_trades_with_decisions(_signals(), _stock_bars(), config)

        self.assertEqual(list(trades["symbol"]), ["AAPL"])
        msft = decisions.loc[decisions["symbol"] == "MSFT"].iloc[0]
        self.assertEqual(msft["status"], "REJECTED")
        self.assertEqual(msft["stage"], "SIZING")
        self.assertIn("buying power", msft["reason"].lower())
        self.assertAlmostEqual(float(msft["buying_power_before"]), 399.35, places=2)

    def test_capital_is_released_when_exit_precedes_later_signal(self):
        def option_bars_provider(**kwargs):
            start = pd.Timestamp(kwargs["reference_time"]).tz_convert(EASTERN)
            if kwargs["symbol"] == "AAPL":
                index = pd.DatetimeIndex([start, pd.Timestamp("2026-09-10 09:55", tz=EASTERN)])
                bars = pd.DataFrame({"Close": [6.0, 3.0]}, index=index)
            else:
                index = pd.DatetimeIndex([start, pd.Timestamp("2026-09-10 15:55", tz=EASTERN)])
                bars = pd.DataFrame({"Close": [4.0, 4.0]}, index=index)
            return {"expiry": "20260918", "strike": 100.0, "right": "C"}, bars

        config = OptionSimulationConfig(
            starting_capital=1000.0,
            max_trades_per_day=3,
            sizing_method="fixed_dollar",
            max_spend_per_trade=1000.0,
            max_daily_capital=2000.0,
            reserve_capital_for_remaining_trades=False,
            recycle_capital_after_exit=True,
            option_bars_provider=option_bars_provider,
            slippage_pct=0.0,
            max_consecutive_losses=99,
            max_daily_drawdown_pct=100.0,
            entry_cutoff_time=pd.Timestamp("11:00").time(),
        )

        trades, decisions = simulate_option_trades_with_decisions(_signals(), _stock_bars(), config)

        self.assertEqual(list(trades["symbol"]), ["AAPL", "MSFT"])
        self.assertTrue((decisions["status"] == "TRADED").all())
        msft = decisions.loc[decisions["symbol"] == "MSFT"].iloc[0]
        self.assertGreater(float(msft["buying_power_before"]), 800.0)

    def test_equity_summary_follows_exit_order(self):
        trades = pd.DataFrame(
            [
                {
                    "symbol": "AAPL",
                    "exit_time": pd.Timestamp("2026-09-10 15:55", tz=EASTERN),
                    "realized_pnl": 100.0,
                    "equity": 1050.0,
                    "commissions": 1.30,
                },
                {
                    "symbol": "MSFT",
                    "exit_time": pd.Timestamp("2026-09-10 11:00", tz=EASTERN),
                    "realized_pnl": -50.0,
                    "equity": 950.0,
                    "commissions": 1.30,
                },
            ]
        )

        metrics = summarize_trades(trades, starting_capital=1000.0)

        self.assertEqual(metrics["net_pnl"], 50.0)
        self.assertEqual(metrics["ending_equity"], 1050.0)
        self.assertEqual(metrics["max_drawdown"], -50.0)


if __name__ == "__main__":
    unittest.main()
