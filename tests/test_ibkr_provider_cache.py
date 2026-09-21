from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

from providers.ibkr_provider import IBKRMarketDataProvider


def _bars(start: str, end: str) -> pd.DataFrame:
    index = pd.date_range(start, end, freq="1D", tz="America/New_York")
    return pd.DataFrame(
        {
            "Open": 100.0,
            "High": 101.0,
            "Low": 99.0,
            "Close": 100.5,
            "Volume": 1_000,
        },
        index=index,
    )


def _five_minute_bars(start: str, days: int = 10) -> pd.DataFrame:
    timestamps = []
    for day in pd.date_range(start, periods=days, freq="1D", tz="America/New_York"):
        timestamps.extend(day.replace(hour=9, minute=30) + pd.Timedelta(minutes=offset) for offset in range(0, 60, 5))
    index = pd.DatetimeIndex(timestamps)
    values = pd.Series(range(len(index)), index=index, dtype=float)
    return pd.DataFrame(
        {
            "Open": 100.0 + values / 100,
            "High": 101.0 + values / 100,
            "Low": 99.0 + values / 100,
            "Close": 100.5 + values / 100,
            "Volume": 1_000,
        },
        index=index,
    )


class IBKRProviderCacheTests(unittest.TestCase):
    def test_period_parser_supports_one_three_and_six_month_research(self):
        self.assertEqual(IBKRMarketDataProvider._period_to_days("1mo"), 30)
        self.assertEqual(IBKRMarketDataProvider._period_to_days("3mo"), 90)
        self.assertEqual(IBKRMarketDataProvider._period_to_days("6mo"), 180)
        self.assertEqual(IBKRMarketDataProvider._period_to_ib_duration("180d"), "180 D")

    def test_load_reuses_sufficient_cache_without_network_request(self):
        with TemporaryDirectory() as temp_dir:
            provider = IBKRMarketDataProvider(cache_dir=Path(temp_dir))
            cache_path = provider._stock_cache_path("AAPL", "15m", True)
            provider._write_cache(_bars("2026-01-01", "2026-01-10"), cache_path)

            with patch.object(provider, "_expected_latest_session_date", return_value=date(2026, 1, 10)), \
                    patch.object(provider, "historical_bars", side_effect=AssertionError("network request made")):
                result = provider.load("AAPL", period="7d", interval="15m")

            self.assertFalse(result.empty)
            self.assertEqual(provider.last_load_source("AAPL"), "cache")

    def test_second_identical_load_uses_data_written_by_first_load(self):
        with TemporaryDirectory() as temp_dir:
            provider = IBKRMarketDataProvider(cache_dir=Path(temp_dir))
            downloaded = _bars("2026-01-01", "2026-01-10")

            with patch.object(provider, "_expected_latest_session_date", return_value=date(2026, 1, 10)), \
                    patch.object(provider, "historical_bars", return_value=downloaded) as request:
                first = provider.load("AAPL", period="7d", interval="15m")
                second = provider.load("AAPL", period="7d", interval="15m")

            self.assertEqual(request.call_count, 1)
            pd.testing.assert_frame_equal(first, second)
            self.assertEqual(provider.last_load_source("AAPL"), "cache")

    def test_force_refresh_bypasses_existing_cache(self):
        with TemporaryDirectory() as temp_dir:
            provider = IBKRMarketDataProvider(cache_dir=Path(temp_dir))
            cache_path = provider._stock_cache_path("AAPL", "15m", True)
            provider._write_cache(_bars("2026-01-01", "2026-01-10"), cache_path)
            refreshed = _bars("2026-01-02", "2026-01-11")

            with patch.object(provider, "_expected_latest_session_date", return_value=date(2026, 1, 11)), \
                    patch.object(provider, "historical_bars", return_value=refreshed) as request:
                result = provider.load("AAPL", period="7d", interval="15m", force_refresh=True)

            request.assert_called_once()
            self.assertEqual(result.index.max(), refreshed.index.max())
            self.assertEqual(provider.last_load_source("AAPL"), "download")

    def test_larger_interval_is_built_from_newer_five_minute_cache(self):
        with TemporaryDirectory() as temp_dir:
            provider = IBKRMarketDataProvider(cache_dir=Path(temp_dir))
            provider._write_cache(
                _bars("2026-01-01", "2026-01-10"),
                provider._stock_cache_path("AAPL", "15m", True),
            )
            five_minute = _five_minute_bars("2026-02-01")
            provider._write_cache(
                five_minute,
                provider._stock_cache_path("AAPL", "5m", True),
            )

            with patch.object(provider, "_expected_latest_session_date", return_value=date(2026, 2, 10)), \
                    patch.object(provider, "historical_bars", side_effect=AssertionError("network request made")):
                result = provider.load("AAPL", period="7d", interval="15m")

            self.assertEqual(provider.last_load_source("AAPL"), "derived_cache")
            self.assertEqual(result.index.max().date(), five_minute.index.max().date())
            self.assertLess(len(result), len(five_minute))

    def test_stale_cache_is_not_used_when_refresh_fails(self):
        with TemporaryDirectory() as temp_dir:
            provider = IBKRMarketDataProvider(cache_dir=Path(temp_dir))
            cache_path = provider._stock_cache_path("AAPL", "15m", True)
            provider._write_cache(_bars("2026-08-11", "2026-08-24"), cache_path)

            with patch.object(provider, "_expected_latest_session_date", return_value=date(2026, 9, 10)), \
                    patch.object(provider, "historical_bars", side_effect=TimeoutError("timed out")):
                with self.assertRaisesRegex(RuntimeError, "ends on 2026-08-24; expected 2026-09-10"):
                    provider.load("AAPL", period="7d", interval="15m")

            self.assertEqual(provider.last_load_source("AAPL"), "error")

    def test_cached_option_failure_skips_repeated_contract_lookup(self):
        with TemporaryDirectory() as temp_dir:
            provider = IBKRMarketDataProvider(cache_dir=Path(temp_dir))
            reference_time = pd.Timestamp("2026-01-05 10:00", tz="America/New_York")
            failure_path = provider._option_failure_cache_path(
                "AAPL", "CALL", 250.0, 7, reference_time, "5 mins", "TRADES", True
            )
            provider._write_option_failure_cache(failure_path, "Could not qualify historical option AAPL C")

            with patch.object(provider, "connect", side_effect=AssertionError("broker lookup made")):
                with self.assertRaisesRegex(RuntimeError, "Could not qualify historical option"):
                    provider.historical_option_bars("AAPL", "CALL", 250.0, 7, reference_time)

    def test_exact_option_contract_loader_reuses_cached_pair_leg(self):
        with TemporaryDirectory() as temp_dir:
            provider = IBKRMarketDataProvider(cache_dir=Path(temp_dir))
            reference_time = pd.Timestamp("2026-09-01 09:30", tz="America/New_York")
            info = {
                "symbol": "AAPL",
                "option_dte": 7,
                "expiry": "20260911",
                "strike": 322.5,
                "right": "P",
                "localSymbol": "AAPL  260911P00322500",
                "conId": 123,
            }
            cache_path = provider._contract_option_cache_path(
                info, reference_time, "5 mins", "TRADES", True
            )
            provider._write_option_cache(cache_path, info, _five_minute_bars("2026-09-01", days=1))

            with patch.object(provider, "connect", side_effect=AssertionError("broker request made")):
                cached_info, bars = provider.historical_option_bars_for_contract(
                    "AAPL", "PUT", "20260911", 322.5, 7, reference_time
                )

            self.assertEqual(cached_info["localSymbol"], "AAPL  260911P00322500")
            self.assertFalse(bars.empty)

    def test_nearest_option_cache_does_not_reuse_distant_strike(self):
        with TemporaryDirectory() as temp_dir:
            provider = IBKRMarketDataProvider(cache_dir=Path(temp_dir))
            reference_time = pd.Timestamp("2026-09-01 09:30", tz="America/New_York")
            info = {
                "symbol": "AAPL",
                "option_dte": 7,
                "expiry": "20260911",
                "strike": 105.0,
                "right": "C",
                "localSymbol": "AAPL  260911C00105000",
                "conId": 456,
            }
            cache_path = provider._contract_option_cache_path(
                info, reference_time, "5 mins", "TRADES", True
            )
            provider._write_option_cache(cache_path, info, _five_minute_bars("2026-09-01", days=1))

            cached_info, bars = provider._find_cached_option_session(
                "AAPL", "CALL", 100.0, 7, reference_time, "5 mins", "TRADES", True
            )

            self.assertEqual(cached_info, {})
            self.assertTrue(bars.empty)

    def test_nearest_option_cache_accepts_listed_strike_spacing(self):
        with TemporaryDirectory() as temp_dir:
            provider = IBKRMarketDataProvider(cache_dir=Path(temp_dir))
            reference_time = pd.Timestamp("2026-09-11 10:05", tz="America/New_York")
            info = {
                "symbol": "COIN",
                "option_dte": 7,
                "expiry": "20260918",
                "strike": 180.0,
                "right": "C",
                "localSymbol": "COIN  260918C00180000",
                "conId": 789,
            }
            cache_path = provider._contract_option_cache_path(
                info, reference_time, "5 mins", "TRADES", True
            )
            provider._write_option_cache(cache_path, info, _five_minute_bars("2026-09-11", days=1))

            cached_info, bars = provider._find_cached_option_session(
                "COIN", "CALL", 181.20, 7, reference_time, "5 mins", "TRADES", True
            )

            self.assertEqual(cached_info["strike"], 180.0)
            self.assertFalse(bars.empty)


if __name__ == "__main__":
    unittest.main()
