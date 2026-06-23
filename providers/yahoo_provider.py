from __future__ import annotations

"""Yahoo Finance market-data provider wrapper.

Temporary fallback only. The goal is to validate IBKR parity, then remove Yahoo
when Strategy Lab no longer needs it.
"""

from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .base import EASTERN, ProviderStatus, normalize_ohlcv

try:
    from backtester.data import YahooDataClient, YFINANCE_AVAILABLE
except Exception:  # allows standalone compile before being placed in project root
    try:
        from data import YahooDataClient, YFINANCE_AVAILABLE  # type: ignore
    except Exception:
        YahooDataClient = None  # type: ignore
        YFINANCE_AVAILABLE = False


class YahooMarketDataProvider:
    name = "Yahoo"

    def __init__(self, cache_dir: str | Path | None = None, timezone: ZoneInfo = EASTERN):
        self.timezone = timezone
        self.cache_dir = cache_dir
        self.client = YahooDataClient(cache_dir=cache_dir, timezone=timezone) if YahooDataClient is not None else None

    def status(self) -> ProviderStatus:
        ok = bool(YFINANCE_AVAILABLE and self.client is not None)
        return ProviderStatus(name=self.name, connected=ok, message="Available" if ok else "Unavailable / yfinance not installed")

    def load(
        self,
        symbol: str,
        period: str | None = "30d",
        interval: str = "5m",
        start: str | None = None,
        end: str | None = None,
        regular_hours_only: bool = True,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        if self.client is None:
            raise RuntimeError("Yahoo provider unavailable. Install yfinance or restore backtester/data.py.")
        df = self.client.load(
            symbol=symbol,
            period=period,
            interval=interval,
            start=start,
            end=end,
            regular_hours_only=regular_hours_only,
            force_refresh=force_refresh,
        )
        return normalize_ohlcv(df, timezone=self.timezone)

    def load_many(
        self,
        symbols: list[str],
        period: str | None = "30d",
        interval: str = "5m",
        start: str | None = None,
        end: str | None = None,
        regular_hours_only: bool = True,
        force_refresh: bool = False,
    ) -> dict[str, pd.DataFrame]:
        data: dict[str, pd.DataFrame] = {}
        for raw in symbols:
            symbol = str(raw).strip().upper()
            if not symbol:
                continue
            data[symbol] = self.load(symbol, period=period, interval=interval, start=start, end=end, regular_hours_only=regular_hours_only, force_refresh=force_refresh)
        return data

    def intraday_bars(self, symbol: str, duration: str = "5 D", bar_size: str = "5 mins", use_rth: bool = True) -> pd.DataFrame:
        return self.load(symbol, period=self._duration_to_period(duration), interval=self._bar_size_to_interval(bar_size), regular_hours_only=use_rth)

    def daily_bars(self, symbol: str, duration: str = "20 D", use_rth: bool = True) -> pd.DataFrame:
        return self.load(symbol, period=self._duration_to_period(duration), interval="1d", regular_hours_only=use_rth)

    def cache_info(self) -> pd.DataFrame:
        if self.client is None:
            return pd.DataFrame()
        return self.client.cache_info()

    def clear_cache(self, symbol: str | None = None) -> int:
        if self.client is None:
            return 0
        return self.client.clear_cache(symbol)

    @staticmethod
    def _duration_to_period(duration: str) -> str:
        parts = str(duration or "30 D").strip().split()
        if len(parts) != 2:
            return "30d"
        n, unit = parts[0], parts[1].upper()
        suffix = {"D": "d", "W": "wk", "M": "mo", "Y": "y"}.get(unit, "d")
        return f"{n}{suffix}"

    @staticmethod
    def _bar_size_to_interval(bar_size: str) -> str:
        s = str(bar_size or "5 mins").lower().strip()
        mapping = {
            "1 min": "1m",
            "2 mins": "2m",
            "5 mins": "5m",
            "15 mins": "15m",
            "30 mins": "30m",
            "1 hour": "60m",
            "1 day": "1d",
        }
        return mapping.get(s, "5m")
