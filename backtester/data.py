from __future__ import annotations

"""Yahoo/yfinance data layer for the PulseTrade AI backtester.

Drop this file inside:

    TradingBot/backtester/data.py

Purpose:
- Fetch historical OHLCV data from Yahoo Finance through yfinance.
- Normalize all bars to America/New_York time.
- Keep regular trading hours only by default.
- Cache files locally so repeated backtests do not keep hitting Yahoo.
- Return clean scanner-ready DataFrames with columns:
  Open, High, Low, Close, Volume

Important Yahoo limits:
- 1m data is usually limited to a short recent window.
- 5m data is suitable for recent intraday backtests, but Yahoo may not provide
  unlimited history. This module fails cleanly when Yahoo returns no data.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except Exception:  # pragma: no cover - depends on local environment
    yf = None
    YFINANCE_AVAILABLE = False

EASTERN = ZoneInfo("America/New_York")
REQUIRED_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]
VALID_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d"}


@dataclass(frozen=True)
class YahooDataRequest:
    """Parameters for one Yahoo historical data request."""

    symbol: str
    interval: str = "5m"
    period: str | None = "60d"
    start: date | str | None = None
    end: date | str | None = None
    regular_hours_only: bool = True
    auto_adjust: bool = False
    force_refresh: bool = False

    def normalized_symbol(self) -> str:
        return self.symbol.strip().upper()


class YahooDataClient:
    """Download and cache Yahoo OHLCV data.

    Example:
        client = YahooDataClient()
        df = client.load("SPY", period="30d", interval="5m")
    """

    def __init__(self, cache_dir: str | Path | None = None, timezone: ZoneInfo = EASTERN):
        base = Path(__file__).resolve().parent
        self.cache_dir = Path(cache_dir) if cache_dir else base / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timezone = timezone

    def load(
        self,
        symbol: str,
        period: str | None = "60d",
        interval: str = "5m",
        start: date | str | None = None,
        end: date | str | None = None,
        regular_hours_only: bool = True,
        auto_adjust: bool = False,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        request = YahooDataRequest(
            symbol=symbol,
            interval=interval,
            period=period,
            start=start,
            end=end,
            regular_hours_only=regular_hours_only,
            auto_adjust=auto_adjust,
            force_refresh=force_refresh,
        )
        return self.load_request(request)

    def load_many(
        self,
        symbols: Iterable[str],
        period: str | None = "60d",
        interval: str = "5m",
        start: date | str | None = None,
        end: date | str | None = None,
        regular_hours_only: bool = True,
        auto_adjust: bool = False,
        force_refresh: bool = False,
    ) -> dict[str, pd.DataFrame]:
        data: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            clean = str(symbol).strip().upper()
            if not clean:
                continue
            data[clean] = self.load(
                clean,
                period=period,
                interval=interval,
                start=start,
                end=end,
                regular_hours_only=regular_hours_only,
                auto_adjust=auto_adjust,
                force_refresh=force_refresh,
            )
        return data

    def load_request(self, request: YahooDataRequest) -> pd.DataFrame:
        self._validate_request(request)
        cache_path = self._cache_path(request)

        if cache_path.exists() and not request.force_refresh:
            cached = self._read_cache(cache_path)
            if not cached.empty:
                return cached

        downloaded = self._download(request)
        if downloaded.empty:
            return downloaded

        self._write_cache(downloaded, cache_path)
        return downloaded

    def cache_info(self) -> pd.DataFrame:
        rows = []
        for path in sorted(self.cache_dir.glob("*.parquet")) + sorted(self.cache_dir.glob("*.csv")):
            rows.append({
                "file": path.name,
                "size_kb": round(path.stat().st_size / 1024, 1),
                "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            })
        return pd.DataFrame(rows)

    def clear_cache(self, symbol: str | None = None) -> int:
        pattern = "*" if not symbol else f"{symbol.strip().upper()}_*"
        removed = 0
        for path in self.cache_dir.glob(pattern):
            if path.is_file() and path.suffix.lower() in {".parquet", ".csv"}:
                path.unlink()
                removed += 1
        return removed

    def _validate_request(self, request: YahooDataRequest) -> None:
        if not YFINANCE_AVAILABLE:
            raise RuntimeError("yfinance is not installed. Add yfinance to requirements.txt and run: pip install -r requirements.txt")
        if not request.normalized_symbol():
            raise ValueError("Symbol is required")
        if request.interval not in VALID_INTERVALS:
            raise ValueError(f"Unsupported interval: {request.interval}. Valid intervals: {sorted(VALID_INTERVALS)}")
        if request.period and (request.start or request.end):
            # yfinance accepts both in some cases, but for reproducibility we force one mode.
            raise ValueError("Use either period OR start/end, not both")

    def _cache_path(self, request: YahooDataRequest) -> Path:
        symbol = request.normalized_symbol().replace("/", "-").replace(".", "-")
        mode = request.period or f"{self._date_key(request.start)}_{self._date_key(request.end)}"
        rth = "rth" if request.regular_hours_only else "all"
        adjusted = "adj" if request.auto_adjust else "raw"
        filename = f"{symbol}_{request.interval}_{mode}_{rth}_{adjusted}.parquet"
        return self.cache_dir / filename

    @staticmethod
    def _date_key(value: date | str | None) -> str:
        if value is None:
            return "none"
        if isinstance(value, date):
            return value.isoformat()
        return str(value).replace("/", "-")

    def _download(self, request: YahooDataRequest) -> pd.DataFrame:
        kwargs = {
            "tickers": request.normalized_symbol(),
            "interval": request.interval,
            "prepost": not request.regular_hours_only,
            "auto_adjust": request.auto_adjust,
            "progress": False,
            "threads": False,
        }
        if request.period:
            kwargs["period"] = request.period
        else:
            kwargs["start"] = request.start
            kwargs["end"] = request.end

        raw = yf.download(**kwargs)
        return self._normalize(raw, request)

    def _normalize(self, raw: pd.DataFrame, request: YahooDataRequest) -> pd.DataFrame:
        if raw is None or raw.empty:
            return pd.DataFrame(columns=REQUIRED_COLUMNS)

        df = raw.copy()
        symbol = request.normalized_symbol()

        # yfinance sometimes returns MultiIndex columns even for one ticker.
        if isinstance(df.columns, pd.MultiIndex):
            levels = [list(map(str, df.columns.get_level_values(i))) for i in range(df.columns.nlevels)]
            if symbol in levels[-1]:
                df = df.xs(symbol, axis=1, level=-1)
            elif symbol in levels[0]:
                df = df.xs(symbol, axis=1, level=0)
            else:
                df.columns = df.columns.get_level_values(0)

        rename = {str(col): str(col).strip().title().replace("Adj Close", "Adj Close") for col in df.columns}
        df = df.rename(columns=rename)

        missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
        if missing:
            return pd.DataFrame(columns=REQUIRED_COLUMNS)

        out = df[REQUIRED_COLUMNS].copy()
        out = out.apply(pd.to_numeric, errors="coerce").dropna(subset=["Open", "High", "Low", "Close"])
        out["Volume"] = out["Volume"].fillna(0)

        idx = pd.to_datetime(out.index, errors="coerce")
        valid_index = ~pd.isna(idx)
        out = out.loc[valid_index].copy()
        idx = idx[valid_index]

        if getattr(idx, "tz", None) is None:
            # yfinance intraday usually returns UTC-aware, daily often naive.
            # For naive daily/index data, localize to NY to keep dates consistent.
            idx = idx.tz_localize(self.timezone)
        else:
            idx = idx.tz_convert(self.timezone)
        out.index = idx
        out.index.name = "Datetime"

        if request.regular_hours_only and request.interval != "1d":
            out = out.between_time("09:30", "16:00", inclusive="both")
            out = out[out["Volume"] > 0]

        out = out[~out.index.duplicated(keep="last")].sort_index()
        return out[REQUIRED_COLUMNS]

    def _read_cache(self, path: Path) -> pd.DataFrame:
        try:
            if path.suffix.lower() == ".parquet":
                df = pd.read_parquet(path)
            else:
                df = pd.read_csv(path, index_col=0, parse_dates=True)
            if df.empty:
                return pd.DataFrame(columns=REQUIRED_COLUMNS)
            df = df[REQUIRED_COLUMNS].copy()
            idx = pd.to_datetime(df.index)
            if getattr(idx, "tz", None) is None:
                idx = idx.tz_localize(self.timezone)
            else:
                idx = idx.tz_convert(self.timezone)
            df.index = idx
            df.index.name = "Datetime"
            return df.sort_index()
        except Exception:
            return pd.DataFrame(columns=REQUIRED_COLUMNS)

    def _write_cache(self, df: pd.DataFrame, path: Path) -> None:
        try:
            df.to_parquet(path)
        except Exception:
            fallback = path.with_suffix(".csv")
            df.to_csv(fallback)


def load_symbol_data(
    symbol: str,
    period: str | None = "60d",
    interval: str = "5m",
    start: date | str | None = None,
    end: date | str | None = None,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Convenience wrapper for quick tests."""
    return YahooDataClient().load(
        symbol=symbol,
        period=period,
        interval=interval,
        start=start,
        end=end,
        force_refresh=force_refresh,
    )
