from __future__ import annotations

"""Market-data provider interfaces for PulseTrade-AI.

All providers must return normalized OHLCV DataFrames with columns:
Open, High, Low, Close, Volume and a timezone-aware DatetimeIndex.
"""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

import pandas as pd

EASTERN = ZoneInfo("America/New_York")
REQUIRED_OHLCV = ["Open", "High", "Low", "Close", "Volume"]


@dataclass(frozen=True)
class MarketDataRequest:
    symbol: str
    period: str | None = "30d"
    interval: str = "5m"
    duration: str | None = None
    bar_size: str | None = None
    start: str | None = None
    end: str | None = None
    regular_hours_only: bool = True
    force_refresh: bool = False

    @property
    def clean_symbol(self) -> str:
        return str(self.symbol).strip().upper()


@dataclass
class ProviderStatus:
    name: str
    connected: bool
    message: str = ""
    details: dict[str, Any] | None = None


@runtime_checkable
class MarketDataProvider(Protocol):
    name: str

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
        """Return normalized OHLCV data for Strategy Lab / replay."""

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
        """Return normalized OHLCV data for multiple symbols."""

    def intraday_bars(self, symbol: str, duration: str = "5 D", bar_size: str = "5 mins", use_rth: bool = True) -> pd.DataFrame:
        """Return IBKR-style intraday historical bars."""

    def daily_bars(self, symbol: str, duration: str = "20 D", use_rth: bool = True) -> pd.DataFrame:
        """Return daily historical bars."""

    def status(self) -> ProviderStatus:
        """Return provider health/status."""


def normalize_ohlcv(df: pd.DataFrame | None, timezone: ZoneInfo = EASTERN, regular_hours_only: bool = False) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=REQUIRED_OHLCV)

    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = out.columns.get_level_values(0)

    out = out.rename(columns={str(c): str(c).strip().title() for c in out.columns})
    missing = [c for c in REQUIRED_OHLCV if c not in out.columns]
    if missing:
        return pd.DataFrame(columns=REQUIRED_OHLCV)

    out = out[REQUIRED_OHLCV].copy()
    for col in REQUIRED_OHLCV:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    out["Volume"] = out["Volume"].fillna(0)

    idx = pd.to_datetime(out.index, errors="coerce")
    valid = ~pd.isna(idx)
    out = out.loc[valid].copy()
    idx = idx[valid]

    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize(timezone)
    else:
        idx = idx.tz_convert(timezone)
    out.index = idx
    out.index.name = "Datetime"

    if regular_hours_only:
        out = out.between_time("09:30", "16:00", inclusive="both")
        out = out[out["Volume"] >= 0]

    return out[~out.index.duplicated(keep="last")].sort_index()[REQUIRED_OHLCV]
