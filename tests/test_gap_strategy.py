from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import pandas as pd

from backtester.gap_data import load_ibkr_gap_candles
from backtester.gap_simulator import GapStockSimulationConfig, simulate_gap_stock_trades_with_decisions
from backtester.gap_universe import YahooGapUniverseConfig, YahooGapUniverseScanner
from backtester.controller import StrategyLabController, StrategyLabSettings
from backtester.ui import _apply_strategy_lab_to_engine
from strategies.gap.strategy import GapScanConfig, evaluate_gap_session

EASTERN = ZoneInfo("America/New_York")


def _sample_gap_down_data() -> pd.DataFrame:
    rows = []
    start = date(2026, 7, 6)
    for offset in range(6):
        day = start + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        for minute, close, volume in [(0, 10.0, 50_000), (5, 10.0, 50_000)]:
            ts = datetime.combine(day, datetime.min.time(), tzinfo=EASTERN).replace(hour=8, minute=minute)
            rows.append((ts, close, close + 0.02, close - 0.02, close, volume))
        for minute in [30, 35, 40, 45, 50]:
            ts = datetime.combine(day, datetime.min.time(), tzinfo=EASTERN).replace(hour=9, minute=minute)
            rows.append((ts, 10.0, 10.1, 9.9, 10.0, 250_000))

    current = date(2026, 7, 13)
    for minute, close in [(0, 8.9), (5, 8.95), (10, 9.0)]:
        ts = datetime.combine(current, datetime.min.time(), tzinfo=EASTERN).replace(hour=8, minute=minute)
        rows.append((ts, close - 0.03, close + 0.05, close - 0.05, close, 200_000))
    opening = [
        (30, 8.80, 8.95, 8.70, 8.90, 180_000),
        (35, 8.90, 9.05, 8.88, 9.00, 180_000),
        (40, 9.00, 9.20, 8.98, 9.15, 180_000),
        (45, 9.15, 9.35, 9.05, 9.25, 180_000),
        (50, 9.25, 10.20, 9.20, 10.00, 180_000),
    ]
    for minute, open_, high, low, close, volume in opening:
        ts = datetime.combine(current, datetime.min.time(), tzinfo=EASTERN).replace(hour=9, minute=minute)
        rows.append((ts, open_, high, low, close, volume))
    frame = pd.DataFrame(rows, columns=["Datetime", "Open", "High", "Low", "Close", "Volume"]).set_index("Datetime")
    return frame.sort_index()


class GapStrategyTests(unittest.TestCase):
    def test_gap_down_reclaim_qualifies(self):
        result = evaluate_gap_session(
            "TEST",
            _sample_gap_down_data(),
            date(2026, 7, 13),
            GapScanConfig(),
            lambda *_: {"known": True, "has_catalyst": False, "window_articles": 40},
        )
        self.assertEqual(result["status"], "QUALIFIED")
        self.assertEqual(result["signal"], "LONG")
        self.assertLessEqual(result["gap_pct"], -8.0)
        self.assertGreaterEqual(result["premarket_rvol"], 3.0)
        self.assertEqual(result["catalyst_status"], "CLEAR")

    def test_verified_catalyst_rejects_candidate(self):
        result = evaluate_gap_session(
            "TEST",
            _sample_gap_down_data(),
            date(2026, 7, 13),
            GapScanConfig(),
            lambda *_: {"known": True, "has_catalyst": True, "headline": "Material news", "impact_score": 90, "window_articles": 40},
        )
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("CATALYST_FOUND", result["rejection_codes"])

    def test_unknown_news_coverage_is_not_treated_as_clear(self):
        result = evaluate_gap_session(
            "TEST",
            _sample_gap_down_data(),
            date(2026, 7, 13),
            GapScanConfig(),
        )
        self.assertEqual(result["catalyst_status"], "UNVERIFIED")
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("CATALYST_UNVERIFIED", result["rejection_codes"])

    def test_stock_simulator_uses_risk_sizing_and_target(self):
        candles = _sample_gap_down_data()
        scan = evaluate_gap_session(
            "TEST",
            candles,
            date(2026, 7, 13),
            GapScanConfig(),
            lambda *_: {"known": True, "has_catalyst": False, "window_articles": 40},
        )
        signals = pd.DataFrame([scan])
        trades, decisions = simulate_gap_stock_trades_with_decisions(
            signals,
            {"TEST": candles},
            GapStockSimulationConfig(
                starting_capital=10_000,
                risk_per_trade=100,
                max_capital_per_trade=1_000,
                max_daily_capital=1_000,
                slippage_pct=0,
                commission_per_share=0,
                minimum_order_commission=0,
            ),
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades.iloc[0]["exit_reason"], "Profit target")
        self.assertGreater(trades.iloc[0]["realized_pnl"], 0)
        self.assertAlmostEqual(float(trades.iloc[0]["r_multiple"]), 2.0, places=2)
        self.assertEqual(decisions.iloc[0]["status"], "TRADED")

    def test_strategy_lab_gap_path_is_stock_only(self):
        candles = _sample_gap_down_data()

        class FakeProvider:
            name = "Synthetic"

            def __init__(self):
                self.regular_hours_only = None

            def load(self, **kwargs):
                self.regular_hours_only = kwargs.get("regular_hours_only")
                return candles.copy()

        provider = FakeProvider()
        universe_calls = []

        class FakeGapProvider:
            name = "IBKR"

            def __init__(self):
                self.calls = []

            def load(self, **kwargs):
                self.calls.append(kwargs)
                return candles.copy()

        gap_provider = FakeGapProvider()

        class FakeUniverseScanner:
            def scan(self, config):
                universe_calls.append(config)
                return pd.DataFrame([{
                    "symbol": "TEST",
                    "price": 9.0,
                    "avg_daily_volume_3m": 2_000_000,
                    "reported_total": 1,
                }])

        with TemporaryDirectory() as temp_dir:
            controller = StrategyLabController(
                export_dir=Path(temp_dir),
                data_provider=provider,
                gap_universe_scanner=FakeUniverseScanner(),
                gap_data_provider=gap_provider,
            )
            result = controller.run(StrategyLabSettings(
                symbols=[],
                period="7d",
                interval="5m",
                selected_strategies=("GAP",),
                data_source="Synthetic",
                starting_capital=10_000,
                gap_risk_per_trade=100,
                gap_max_capital_per_trade=1_000,
                gap_max_daily_capital=1_000,
                gap_slippage_pct=0,
                gap_commission_per_share=0,
                gap_minimum_order_commission=0,
                gap_ibkr_request_delay_seconds=0,
                gap_ibkr_max_retries=0,
            ))
        self.assertIsNone(provider.regular_hours_only)
        self.assertEqual(len(universe_calls), 1)
        self.assertEqual(len(gap_provider.calls), 1)
        self.assertFalse(gap_provider.calls[0]["regular_hours_only"])
        self.assertFalse(result["gap_scanner"].empty)
        self.assertEqual(len(result["gap_universe"]), 1)
        self.assertEqual(result["meta"]["gap_data_source"], "IBKR")
        self.assertTrue((result["signals"]["strategy"] == "GAP").all())
        self.assertTrue((result["trades"]["instrument"] == "STOCK").all())
        self.assertFalse("option_dte" in result["trades"].columns)

    def test_pmb_data_loading_remains_regular_hours_only(self):
        candles = _sample_gap_down_data()

        class FakeProvider:
            name = "Synthetic"

            def __init__(self):
                self.regular_hours_only = None

            def load(self, **kwargs):
                self.regular_hours_only = kwargs.get("regular_hours_only")
                return candles.copy()

        provider = FakeProvider()
        controller = StrategyLabController(data_provider=provider)
        controller.load_data(StrategyLabSettings(symbols=["TEST"], selected_strategies=("PMB",)))
        self.assertTrue(provider.regular_hours_only)

    def test_partial_ibkr_coverage_is_persisted_and_not_marked_complete(self):
        candles = _sample_gap_down_data()

        class FakeUniverseScanner:
            def scan(self, config):
                return pd.DataFrame([
                    {"symbol": "TEST", "price": 9.0, "avg_daily_volume_3m": 2_000_000},
                    {"symbol": "MISS", "price": 8.0, "avg_daily_volume_3m": 2_000_000},
                ])

        class PartialIBKRProvider:
            name = "IBKR"

            def load(self, **kwargs):
                return candles.copy() if kwargs["symbol"] == "TEST" else pd.DataFrame()

        with TemporaryDirectory() as temp_dir:
            controller = StrategyLabController(
                export_dir=Path(temp_dir),
                data_provider=PartialIBKRProvider(),
                gap_universe_scanner=FakeUniverseScanner(),
            )
            result = controller.run(StrategyLabSettings(
                symbols=[],
                period="7d",
                interval="5m",
                selected_strategies=("GAP",),
                gap_ibkr_request_delay_seconds=0,
                gap_ibkr_max_retries=0,
            ))
            loaded = controller.load_last_result()

        self.assertEqual(result["meta"]["status"], "COMPLETE_WITH_DATA_ERRORS")
        self.assertEqual(result["meta"]["gap_candle_coverage_pct"], 50.0)
        self.assertEqual(loaded["errors"].iloc[0]["symbol"], "MISS")

    def test_gap_settings_cannot_be_applied_to_live_engine(self):
        config = {"strategy": {"active_strategy": "pmb"}, "risk": {}, "watchlist": ["SPY"]}
        with self.assertRaisesRegex(ValueError, "Only the implemented PMB"):
            _apply_strategy_lab_to_engine(
                config,
                StrategyLabSettings(symbols=[], selected_strategies=("GAP",)),
                [],
            )
        self.assertEqual(config["strategy"]["active_strategy"], "pmb")
        self.assertEqual(config["watchlist"], ["SPY"])

    def test_yahoo_universe_scanner_pages_all_matches(self):
        calls = []
        quotes = [
            {"symbol": "AAA", "quoteType": "EQUITY", "regularMarketPrice": 5.0, "averageDailyVolume3Month": 2_000_000},
            {"symbol": "BBB", "quoteType": "EQUITY", "regularMarketPrice": 10.0, "averageDailyVolume3Month": 3_000_000},
            {"symbol": "CCC", "quoteType": "EQUITY", "regularMarketPrice": 14.0, "averageDailyVolume3Month": 4_000_000},
        ]

        def fake_screen(query, **kwargs):
            calls.append(kwargs)
            offset = int(kwargs["offset"])
            size = int(kwargs["size"])
            return {"total": len(quotes), "quotes": quotes[offset:offset + size]}

        scanner = YahooGapUniverseScanner(screen_fn=fake_screen)
        universe = scanner.scan(YahooGapUniverseConfig(page_size=2, max_symbols=10))
        self.assertEqual(set(universe["symbol"]), {"AAA", "BBB", "CCC"})
        self.assertEqual([call["offset"] for call in calls], [0, 2])

    def test_ibkr_gap_candles_use_extended_hours_and_retry(self):
        candles = pd.DataFrame({
            "Open": [5.0, 5.1, 5.2],
            "High": [5.2, 5.3, 5.4],
            "Low": [4.9, 5.0, 5.1],
            "Close": [5.1, 5.2, 5.3],
            "Volume": [100, 200, 300],
        }, index=pd.date_range("2026-07-13 08:00", periods=3, freq="5min", tz=EASTERN))

        class FlakyIBKRProvider:
            name = "IBKR"

            def __init__(self):
                self.calls = []

            def load(self, **kwargs):
                self.calls.append(kwargs)
                symbol_attempts = sum(1 for call in self.calls if call["symbol"] == kwargs["symbol"])
                if kwargs["symbol"] == "AAA" and symbol_attempts == 1:
                    raise RuntimeError("temporary pacing failure")
                if kwargs["symbol"] == "BBB":
                    return pd.DataFrame()
                return candles.copy()

        provider = FlakyIBKRProvider()
        sleeps = []
        data, errors = load_ibkr_gap_candles(
            provider,
            ["AAA", "BBB"],
            period="7d",
            interval="5m",
            request_delay_seconds=0.1,
            max_retries=1,
            sleep_fn=sleeps.append,
        )
        self.assertEqual(set(data), {"AAA"})
        self.assertEqual(errors.iloc[0]["symbol"], "BBB")
        self.assertEqual(int(errors.iloc[0]["attempts"]), 2)
        self.assertTrue(all(call["regular_hours_only"] is False for call in provider.calls))
        self.assertTrue(all(call["force_refresh"] is True for call in provider.calls))
        self.assertGreaterEqual(len(sleeps), 2)


if __name__ == "__main__":
    unittest.main()
