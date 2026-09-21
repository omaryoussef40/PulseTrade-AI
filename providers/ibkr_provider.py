from __future__ import annotations

"""Interactive Brokers market-data provider."""

import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from pandas.tseries.holiday import (
    AbstractHolidayCalendar,
    GoodFriday,
    Holiday,
    USLaborDay,
    USMartinLutherKingJr,
    USMemorialDay,
    USPresidentsDay,
    USThanksgivingDay,
    nearest_workday,
)

from .base import EASTERN, ProviderStatus, REQUIRED_OHLCV, normalize_ohlcv

IB_IMPORT_ERROR: str | None = None
try:
    import nest_asyncio
    nest_asyncio.apply()
except Exception:
    pass

try:
    from ib_insync import IB, Option, Stock, util
    IB_AVAILABLE = True
except Exception as exc:  # pragma: no cover
    IB_AVAILABLE = False
    IB_IMPORT_ERROR = repr(exc)
    IB = Any  # type: ignore
    Option = Any  # type: ignore
    Stock = Any  # type: ignore
    util = None  # type: ignore


class _NYSEHolidayCalendar(AbstractHolidayCalendar):
    """Regular full-day US equity-market holidays."""

    rules = [
        Holiday("New Year's Day", month=1, day=1, observance=nearest_workday),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday(
            "Juneteenth",
            month=6,
            day=19,
            start_date="2022-06-19",
            observance=nearest_workday,
        ),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas Day", month=12, day=25, observance=nearest_workday),
    ]


@dataclass
class IBKRProviderConfig:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 11
    account: str | None = None
    readonly: bool = False
    timezone: ZoneInfo = EASTERN
    request_timeout_seconds: int = 45


class IBKRMarketDataProvider:
    name = "IBKR"
    max_cache_days = 180

    def __init__(
        self,
        config: IBKRProviderConfig | None = None,
        ib: IB | None = None,
        cache_dir: str | Path | None = None,
    ):
        self.config = config or IBKRProviderConfig()
        self.ib = ib
        self._owns_connection = ib is None
        self._stock_contract_cache: dict[str, Any] = {}
        self._last_load_sources: dict[str, str] = {}
        base = Path(cache_dir) if cache_dir else Path(__file__).resolve().parent.parent / "backtester" / "cache" / "ibkr"
        self.cache_dir = base
        self.stock_cache_dir = base / "stocks"
        self.option_cache_dir = base / "options"
        self.stock_cache_dir.mkdir(parents=True, exist_ok=True)
        self.option_cache_dir.mkdir(parents=True, exist_ok=True)
        self._prune_old_cache_files()

    def connect(self) -> IB:
        if not IB_AVAILABLE:
            raise RuntimeError(
                "ib_insync could not be imported by the Python running this app. "
                f"Python: {sys.executable} | Version: {sys.version.split()[0]} | "
                f"Import error: {IB_IMPORT_ERROR}. "
                "Install with: python -m pip install ib-insync nest-asyncio"
            )
        if self.ib is not None and self.ib.isConnected():
            return self.ib
        self.ib = IB()
        self.ib.connect(
            self.config.host,
            int(self.config.port),
            clientId=int(self.config.client_id),
            readonly=bool(self.config.readonly),
            timeout=10,
        )
        self._owns_connection = True
        return self.ib

    def disconnect(self) -> None:
        if self._owns_connection and self.ib is not None and self.ib.isConnected():
            self.ib.disconnect()

    def is_connected(self) -> bool:
        return bool(self.ib is not None and self.ib.isConnected())

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name,
            connected=self.is_connected(),
            message="Connected" if self.is_connected() else "Disconnected",
            details={"host": self.config.host, "port": self.config.port, "client_id": self.config.client_id},
        )

    def qualify_stock(self, symbol: str):
        symbol = str(symbol).strip().upper()
        if symbol in self._stock_contract_cache:
            return self._stock_contract_cache[symbol]
        ib = self.connect()
        contract = Stock(symbol, "SMART", "USD")
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            raise RuntimeError(f"Could not qualify stock contract for {symbol}")
        self._stock_contract_cache[symbol] = qualified[0]
        return qualified[0]

    def historical_bars(
        self,
        symbol: str,
        duration: str = "5 D",
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = True,
    ) -> pd.DataFrame:
        ib = self.connect()
        contract = self.qualify_stock(symbol)
        previous_timeout = self._apply_request_timeout(ib)
        try:
            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow=what_to_show,
                useRTH=use_rth,
                formatDate=1,
                keepUpToDate=False,
            )
        finally:
            self._restore_request_timeout(ib, previous_timeout)
        return self._bars_to_ohlcv(bars)

    def intraday_bars(self, symbol: str, duration: str = "5 D", bar_size: str = "5 mins", use_rth: bool = True) -> pd.DataFrame:
        return self.historical_bars(symbol=symbol, duration=duration, bar_size=bar_size, use_rth=use_rth)

    def daily_bars(self, symbol: str, duration: str = "20 D", use_rth: bool = True) -> pd.DataFrame:
        return self.historical_bars(symbol=symbol, duration=duration, bar_size="1 day", use_rth=use_rth)

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
        clean_symbol = str(symbol).strip().upper()
        period_days = self._period_to_days(period or "30d")
        cache_path = self._stock_cache_path(clean_symbol, interval, regular_hours_only)
        if not force_refresh:
            cached = self._read_cache(cache_path)
            derived = self._load_resampled_cache(clean_symbol, interval, regular_hours_only, period_days)
            if not derived.empty and (cached.empty or derived.index.max() > cached.index.max()):
                merged = self._merge_and_prune_cache(cached, derived)
                self._write_cache(merged, cache_path)
                self._last_load_sources[clean_symbol] = "derived_cache"
                return self._slice_cached_period(merged, period_days)
            if self._cache_covers_period(cached, period_days):
                self._last_load_sources[clean_symbol] = "cache"
                sliced = self._slice_cached_period(cached, period_days)
                return sliced

        duration = self._period_to_ib_duration(period or "30d")
        bar_size = self._interval_to_ib_bar_size(interval)
        cached = self._read_cache(cache_path)
        try:
            downloaded = self.historical_bars(symbol=clean_symbol, duration=duration, bar_size=bar_size, use_rth=regular_hours_only)
        except Exception as exc:
            if self._cache_covers_period(cached, period_days):
                self._last_load_sources[clean_symbol] = "cache_fallback"
                return self._slice_cached_period(cached, period_days)
            self._last_load_sources[clean_symbol] = "error"
            if not cached.empty:
                raise RuntimeError(self._stale_data_message(clean_symbol, cached, f"IBKR refresh failed: {exc}")) from exc
            raise
        if downloaded.empty:
            if self._cache_covers_period(cached, period_days):
                self._last_load_sources[clean_symbol] = "cache_fallback"
                return self._slice_cached_period(cached, period_days)
            if not cached.empty:
                self._last_load_sources[clean_symbol] = "error"
                raise RuntimeError(self._stale_data_message(clean_symbol, cached, "IBKR returned no replacement candles"))
            self._last_load_sources[clean_symbol] = "empty"
            return downloaded

        merged = self._merge_and_prune_cache(cached, downloaded)
        self._write_cache(merged, cache_path)
        self._prune_old_cache_files()
        if not self._cache_is_current(merged):
            self._last_load_sources[clean_symbol] = "error"
            raise RuntimeError(self._stale_data_message(clean_symbol, merged, "IBKR returned stale candles"))
        self._last_load_sources[clean_symbol] = "download"
        return self._slice_cached_period(merged, period_days)

    def last_load_source(self, symbol: str) -> str:
        """Return how the most recent stock-candle load was satisfied."""
        return self._last_load_sources.get(str(symbol).strip().upper(), "unknown")

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

    def account_summary(self) -> dict[str, Any]:
        ib = self.connect()
        rows = ib.accountSummary()
        accounts = sorted({r.account for r in rows if getattr(r, "account", None)})
        account_id = self.config.account or (accounts[0] if accounts else "N/A")
        wanted = {
            "NetLiquidation": None,
            "TotalCashValue": None,
            "AvailableFunds": None,
            "BuyingPower": None,
            "MaintMarginReq": None,
            "UnrealizedPnL": None,
            "RealizedPnL": None,
        }
        currency = "USD"
        for item in rows:
            if item.tag not in wanted:
                continue
            if self.config.account and item.account != self.config.account:
                continue
            wanted[item.tag] = item.value
            currency = item.currency or currency
        return {"account_id": account_id, "currency": currency, "connected": self.is_connected(), "fetched_at": datetime.now().isoformat(timespec="seconds"), **wanted}

    def positions(self) -> list[dict[str, Any]]:
        ib = self.connect()
        out: list[dict[str, Any]] = []
        for p in ib.positions():
            if self.config.account and p.account != self.config.account:
                continue
            c = p.contract
            out.append({
                "account": p.account,
                "symbol": getattr(c, "symbol", ""),
                "secType": getattr(c, "secType", ""),
                "exchange": getattr(c, "exchange", ""),
                "currency": getattr(c, "currency", ""),
                "position": p.position,
                "avgCost": p.avgCost,
                "conId": getattr(c, "conId", None),
                "localSymbol": getattr(c, "localSymbol", ""),
                "lastTradeDateOrContractMonth": getattr(c, "lastTradeDateOrContractMonth", ""),
                "strike": getattr(c, "strike", None),
                "right": getattr(c, "right", ""),
            })
        return out

    def open_orders(self) -> list[dict[str, Any]]:
        ib = self.connect()
        out: list[dict[str, Any]] = []
        for trade in ib.openTrades():
            c = trade.contract
            o = trade.order
            s = trade.orderStatus
            out.append({
                "symbol": getattr(c, "symbol", ""),
                "secType": getattr(c, "secType", ""),
                "localSymbol": getattr(c, "localSymbol", ""),
                "action": getattr(o, "action", ""),
                "orderType": getattr(o, "orderType", ""),
                "totalQuantity": getattr(o, "totalQuantity", None),
                "lmtPrice": getattr(o, "lmtPrice", None),
                "auxPrice": getattr(o, "auxPrice", None),
                "status": getattr(s, "status", ""),
                "filled": getattr(s, "filled", None),
                "remaining": getattr(s, "remaining", None),
                "avgFillPrice": getattr(s, "avgFillPrice", None),
                "orderId": getattr(o, "orderId", None),
                "permId": getattr(o, "permId", None),
            })
        return out

    def snapshot_quote(self, symbol: str) -> dict[str, Any]:
        ib = self.connect()
        contract = self.qualify_stock(symbol)
        ticker = ib.reqMktData(contract, "", snapshot=True, regulatorySnapshot=False)
        ib.sleep(2)
        bid = self._clean_num(ticker.bid)
        ask = self._clean_num(ticker.ask)
        last = self._clean_num(ticker.last)
        close = self._clean_num(ticker.close)
        mid = None
        spread_pct = None
        if bid and ask and bid > 0 and ask > 0:
            mid = (bid + ask) / 2
            spread_pct = ((ask - bid) / mid * 100) if mid else None
        elif last and last > 0:
            mid = last
        elif close and close > 0:
            mid = close
        return {"symbol": str(symbol).strip().upper(), "bid": bid, "ask": ask, "last": last, "close": close, "mid": mid, "spread_pct": spread_pct, "volume": int(ticker.volume or 0), "fetched_at": datetime.now().isoformat(timespec="seconds")}

    def historical_option_bars(
        self,
        symbol: str,
        signal: str,
        underlying_price: float,
        option_dte: int,
        reference_time,
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = True,
    ) -> tuple[dict[str, Any], pd.DataFrame]:
        symbol = str(symbol).strip().upper()
        signal = str(signal).strip().upper()
        ref_ts = pd.Timestamp(reference_time)
        if ref_ts.tzinfo is None:
            ref_ts = ref_ts.tz_localize(self.config.timezone)
        else:
            ref_ts = ref_ts.tz_convert(self.config.timezone)
        cached_info, cached_bars = self._find_cached_option_session(
            symbol,
            signal,
            underlying_price,
            option_dte,
            reference_time,
            bar_size,
            what_to_show,
            use_rth,
        )
        if cached_info and not cached_bars.empty:
            return cached_info, cached_bars

        failure_path = self._option_failure_cache_path(
            symbol,
            signal,
            underlying_price,
            option_dte,
            ref_ts,
            bar_size,
            what_to_show,
            use_rth,
        )
        cached_failure = self._read_option_failure_cache(failure_path)
        if cached_failure:
            raise RuntimeError(cached_failure)

        ib = self.connect()
        previous_timeout = self._apply_request_timeout(ib)
        try:
            if signal not in {"CALL", "PUT"}:
                raise RuntimeError(f"Unsupported option signal for {symbol}: {signal}")
            right = "C" if signal == "CALL" else "P"
            stock = self.qualify_stock(symbol)
            params = ib.reqSecDefOptParams(symbol, "", stock.secType, stock.conId)
            if not params:
                raise RuntimeError(f"No IBKR option chain returned for {symbol}")
            chain = next((p for p in params if p.exchange == "SMART"), params[0])

            ref_date = ref_ts.date()
            target_date = ref_date + timedelta(days=int(option_dte))

            chain_expirations = []
            for raw in sorted(chain.expirations):
                try:
                    exp_date = datetime.strptime(str(raw), "%Y%m%d").date()
                except Exception:
                    continue
                if exp_date >= ref_date:
                    chain_expirations.append((str(raw), exp_date))

            today = datetime.now(self.config.timezone).date()
            if target_date >= today and chain_expirations:
                expiry_candidates = sorted(chain_expirations, key=lambda item: abs((item[1] - target_date).days))
            else:
                generated_by_date = {}
                for offset in range(-10, 11):
                    candidate = target_date + timedelta(days=offset)
                    # For expired equity options, blindly probing every weekday
                    # causes many IBKR error-200 "unknown contract" responses.
                    # Start with Fridays, which cover regular/weekly expiries for
                    # the symbols this lab trades, and only try nearby candidates.
                    if candidate.weekday() == 4:
                        generated_by_date[candidate] = (candidate.strftime("%Y%m%d"), candidate)
                if not generated_by_date and target_date.weekday() < 5:
                    generated_by_date[target_date] = (target_date.strftime("%Y%m%d"), target_date)
                expiry_candidates = sorted(generated_by_date.values(), key=lambda item: abs((item[1] - target_date).days))

            if not expiry_candidates:
                raise RuntimeError(f"No valid expirations for {symbol} on {ref_date}")

            strikes = []
            for raw_strike in chain.strikes:
                try:
                    strike_value = float(raw_strike)
                except Exception:
                    continue
                if np.isfinite(strike_value) and strike_value > 0:
                    strikes.append(strike_value)
            strikes = sorted(strikes)
            nearby = [s for s in strikes if float(underlying_price) * 0.90 <= s <= float(underlying_price) * 1.10]
            strike_pool = nearby or strikes
            if not strike_pool:
                raise RuntimeError(f"No valid strikes for {symbol} near {underlying_price:g}")
            strike_candidates = sorted(strike_pool, key=lambda value: abs(float(value) - float(underlying_price)))[:5]

            contract = None
            expiry = None
            strike = None
            attempted = []
            for expiry_value, expiry_date in expiry_candidates[:4]:
                for strike_value in strike_candidates:
                    attempted.append(f"{expiry_value} {strike_value:g}")
                    candidate_contract = Option(symbol, expiry_value, strike_value, right, "SMART", currency="USD", multiplier="100")
                    if expiry_date < today:
                        candidate_contract.includeExpired = True
                    qualified = ib.qualifyContracts(candidate_contract)
                    if qualified:
                        contract = qualified[0]
                        expiry = expiry_value
                        strike = float(strike_value)
                        break
                if contract is not None:
                    break
            if contract is None or expiry is None or strike is None:
                sample = ", ".join(attempted[:12])
                message = f"Could not qualify historical option {symbol} {right}; tried {sample}"
                self._write_option_failure_cache(failure_path, message)
                raise RuntimeError(message)

            info = {
                "symbol": symbol,
                "option_dte": int(option_dte),
                "expiry": expiry,
                "strike": float(strike),
                "right": right,
                "localSymbol": getattr(contract, "localSymbol", ""),
                "conId": getattr(contract, "conId", None),
            }
            option_cache_path = self._contract_option_cache_path(info, ref_ts, bar_size, what_to_show, use_rth)
            cached_info, cached_bars = self._read_option_cache(option_cache_path)
            if cached_info and not cached_bars.empty:
                return cached_info, cached_bars

            end_dt = ref_ts.replace(hour=16, minute=0, second=0, microsecond=0)
            bars = ib.reqHistoricalData(
                contract,
                endDateTime=end_dt.to_pydatetime(),
                durationStr="1 D",
                barSizeSetting=bar_size,
                whatToShow=what_to_show,
                useRTH=use_rth,
                formatDate=1,
                keepUpToDate=False,
            )
            df = self._bars_to_ohlcv(bars)
            self._write_option_cache(option_cache_path, info, df)
            self._prune_old_cache_files()
            return info, df
        finally:
            self._restore_request_timeout(ib, previous_timeout)

    def historical_option_bars_for_contract(
        self,
        symbol: str,
        signal: str,
        expiry: str,
        strike: float,
        option_dte: int,
        reference_time,
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = True,
    ) -> tuple[dict[str, Any], pd.DataFrame]:
        """Load one exact option series, used when paired legs must match."""
        symbol = str(symbol).strip().upper()
        signal = str(signal).strip().upper()
        if signal not in {"CALL", "PUT"}:
            raise RuntimeError(f"Unsupported option signal for {symbol}: {signal}")
        right = "C" if signal == "CALL" else "P"
        expiry = str(expiry).strip()
        strike = float(strike)
        ref_ts = pd.Timestamp(reference_time)
        if ref_ts.tzinfo is None:
            ref_ts = ref_ts.tz_localize(self.config.timezone)
        else:
            ref_ts = ref_ts.tz_convert(self.config.timezone)

        prefix = (
            f"{self._safe_key(symbol)}-{self._safe_key(signal)}-"
            f"{ref_ts.date().isoformat()}-{int(option_dte)}d-"
        )
        for meta_path in self.option_cache_dir.glob(f"{prefix}*.json"):
            if "unavailable" in meta_path.name:
                continue
            try:
                with meta_path.open("r", encoding="utf-8") as file:
                    cached_info = dict(json.load(file) or {})
                if (
                    str(cached_info.get("expiry") or "") == expiry
                    and abs(float(cached_info.get("strike")) - strike) <= 1e-6
                    and str(cached_info.get("right") or "").upper() == right
                ):
                    cached_bars = self._read_cache(meta_path.with_suffix(".parquet"))
                    if not cached_bars.empty:
                        return cached_info, cached_bars
            except Exception:
                continue

        ib = self.connect()
        previous_timeout = self._apply_request_timeout(ib)
        try:
            contract = Option(symbol, expiry, strike, right, "SMART", currency="USD", multiplier="100")
            expiry_date = datetime.strptime(expiry, "%Y%m%d").date()
            if expiry_date < datetime.now(self.config.timezone).date():
                contract.includeExpired = True
            qualified = ib.qualifyContracts(contract)
            if not qualified:
                raise RuntimeError(f"Could not qualify exact historical option {symbol} {right} {expiry} {strike:g}")
            contract = qualified[0]
            info = {
                "symbol": symbol,
                "option_dte": int(option_dte),
                "expiry": expiry,
                "strike": strike,
                "right": right,
                "localSymbol": getattr(contract, "localSymbol", ""),
                "conId": getattr(contract, "conId", None),
            }
            cache_path = self._contract_option_cache_path(info, ref_ts, bar_size, what_to_show, use_rth)
            cached_info, cached_bars = self._read_option_cache(cache_path)
            if cached_info and not cached_bars.empty:
                return cached_info, cached_bars

            end_dt = ref_ts.replace(hour=16, minute=0, second=0, microsecond=0)
            bars = ib.reqHistoricalData(
                contract,
                endDateTime=end_dt.to_pydatetime(),
                durationStr="1 D",
                barSizeSetting=bar_size,
                whatToShow=what_to_show,
                useRTH=use_rth,
                formatDate=1,
                keepUpToDate=False,
            )
            frame = self._bars_to_ohlcv(bars)
            self._write_option_cache(cache_path, info, frame)
            self._prune_old_cache_files()
            return info, frame
        finally:
            self._restore_request_timeout(ib, previous_timeout)

    def _bars_to_ohlcv(self, bars: Any) -> pd.DataFrame:
        if not bars:
            return pd.DataFrame(columns=REQUIRED_OHLCV)
        df = util.df(bars) if util is not None else pd.DataFrame()
        if df.empty:
            return pd.DataFrame(columns=REQUIRED_OHLCV)
        df = df.rename(columns={"date": "Datetime", "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
        if "Datetime" not in df.columns:
            return pd.DataFrame(columns=REQUIRED_OHLCV)
        df["Datetime"] = pd.to_datetime(df["Datetime"], errors="coerce")
        df = df.dropna(subset=["Datetime"]).set_index("Datetime")
        return normalize_ohlcv(df, timezone=self.config.timezone)

    def cache_info(self) -> pd.DataFrame:
        rows = []
        for path in sorted(self.cache_dir.glob("*/*.parquet")) + sorted(self.cache_dir.glob("*/*.csv")):
            rows.append({
                "file": str(path.relative_to(self.cache_dir)),
                "size_kb": round(path.stat().st_size / 1024, 1),
                "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            })
        return pd.DataFrame(rows)

    def clear_cache(self, symbol: str | None = None) -> int:
        clean = str(symbol or "").strip().upper()
        removed = 0
        patterns = [f"{clean}_*" if clean else "*", f"{clean}-*" if clean else "*"]
        for cache_dir, pattern in [(self.stock_cache_dir, patterns[0]), (self.option_cache_dir, patterns[1])]:
            for path in cache_dir.glob(pattern):
                if path.is_file() and path.suffix.lower() in {".parquet", ".csv", ".json"}:
                    path.unlink()
                    removed += 1
        return removed

    def _apply_request_timeout(self, ib) -> float | int:
        previous_timeout = getattr(ib, "RequestTimeout", 0)
        if not previous_timeout or float(previous_timeout) <= 0:
            ib.RequestTimeout = int(self.config.request_timeout_seconds)
        return previous_timeout

    @staticmethod
    def _restore_request_timeout(ib, previous_timeout: float | int) -> None:
        if previous_timeout != getattr(ib, "RequestTimeout", 0):
            ib.RequestTimeout = previous_timeout

    def _prune_old_cache_files(self) -> None:
        cutoff = (datetime.now(self.config.timezone) - timedelta(days=int(self.max_cache_days))).timestamp()
        for cache_dir in [self.stock_cache_dir, self.option_cache_dir]:
            for path in cache_dir.glob("*"):
                if path.is_file() and path.suffix.lower() in {".parquet", ".csv", ".json"}:
                    try:
                        if path.stat().st_mtime < cutoff:
                            path.unlink()
                    except Exception:
                        pass

    def _read_cache(self, path: Path) -> pd.DataFrame:
        try:
            if path.suffix.lower() == ".parquet":
                df = pd.read_parquet(path)
            else:
                df = pd.read_csv(path, index_col=0, parse_dates=True)
            return normalize_ohlcv(df, timezone=self.config.timezone)
        except Exception:
            fallback = path.with_suffix(".csv")
            if fallback == path or not fallback.exists():
                return pd.DataFrame(columns=REQUIRED_OHLCV)
            try:
                df = pd.read_csv(fallback, index_col=0, parse_dates=True)
                return normalize_ohlcv(df, timezone=self.config.timezone)
            except Exception:
                return pd.DataFrame(columns=REQUIRED_OHLCV)

    def _write_cache(self, df: pd.DataFrame, path: Path) -> None:
        if df is None or df.empty:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            df.to_parquet(path)
        except Exception:
            df.to_csv(path.with_suffix(".csv"))

    def _read_option_cache(self, path: Path) -> tuple[dict[str, Any], pd.DataFrame]:
        meta_path = path.with_suffix(".json")
        if not meta_path.exists():
            return {}, pd.DataFrame(columns=REQUIRED_OHLCV)
        bars = self._read_cache(path)
        if bars.empty:
            return {}, bars
        try:
            with meta_path.open("r", encoding="utf-8") as f:
                info = json.load(f)
            return dict(info or {}), bars
        except Exception:
            return {}, pd.DataFrame(columns=REQUIRED_OHLCV)

    def _write_option_cache(self, path: Path, info: dict[str, Any], df: pd.DataFrame) -> None:
        if df is None or df.empty:
            return
        self._write_cache(df, path)
        try:
            with path.with_suffix(".json").open("w", encoding="utf-8") as f:
                json.dump(info, f, indent=2, default=str)
        except Exception:
            pass

    @staticmethod
    def _read_option_failure_cache(path: Path) -> str:
        try:
            with path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            return str(payload.get("error") or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _write_option_failure_cache(path: Path, message: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as f:
                json.dump({"error": str(message)}, f, indent=2)
        except Exception:
            pass

    def _merge_and_prune_cache(self, cached: pd.DataFrame, downloaded: pd.DataFrame) -> pd.DataFrame:
        frames = [df for df in [cached, downloaded] if df is not None and not df.empty]
        if not frames:
            return pd.DataFrame(columns=REQUIRED_OHLCV)
        merged = normalize_ohlcv(pd.concat(frames), timezone=self.config.timezone)
        if merged.empty:
            return merged
        latest = merged.index.max()
        cutoff = latest - pd.Timedelta(days=int(self.max_cache_days))
        return merged[merged.index >= cutoff].sort_index()

    def _slice_cached_period(self, df: pd.DataFrame, period_days: int | None) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame(columns=REQUIRED_OHLCV)
        clean = normalize_ohlcv(df, timezone=self.config.timezone)
        if clean.empty or not period_days:
            return clean
        latest = clean.index.max()
        cutoff = latest - pd.Timedelta(days=min(int(period_days), int(self.max_cache_days)))
        return clean[clean.index >= cutoff].sort_index()

    def _load_resampled_cache(
        self,
        symbol: str,
        interval: str,
        regular_hours_only: bool,
        period_days: int | None,
    ) -> pd.DataFrame:
        clean_interval = str(interval or "5m").strip().lower()
        rules = {"15m": "15min", "30m": "30min", "60m": "60min", "1h": "60min"}
        rule = rules.get(clean_interval)
        if rule is None:
            return pd.DataFrame(columns=REQUIRED_OHLCV)

        base = self._read_cache(self._stock_cache_path(symbol, "5m", regular_hours_only))
        if not self._cache_covers_period(base, period_days):
            return pd.DataFrame(columns=REQUIRED_OHLCV)

        clean = normalize_ohlcv(base, timezone=self.config.timezone)
        if clean.empty:
            return clean
        offset = "30min" if regular_hours_only else None
        aggregated = clean.resample(
            rule,
            origin="start_day",
            offset=offset,
            label="left",
            closed="left",
        ).agg({
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        })
        return normalize_ohlcv(aggregated.dropna(subset=["Open", "High", "Low", "Close"]), timezone=self.config.timezone)

    def _cache_covers_period(self, df: pd.DataFrame, period_days: int | None) -> bool:
        if df is None or df.empty:
            return False
        clean = normalize_ohlcv(df, timezone=self.config.timezone)
        if clean.empty:
            return False
        if not self._cache_is_current(clean):
            return False
        if not period_days:
            return True
        latest = clean.index.max()
        earliest = clean.index.min()
        requested_cutoff = latest - pd.Timedelta(days=min(int(period_days), int(self.max_cache_days)))
        return earliest <= requested_cutoff + pd.Timedelta(days=1)

    def _cache_is_current(self, df: pd.DataFrame) -> bool:
        if df is None or df.empty:
            return False
        clean = normalize_ohlcv(df, timezone=self.config.timezone)
        if clean.empty:
            return False
        return clean.index.max().date() >= self._expected_latest_session_date()

    def _stale_data_message(self, symbol: str, df: pd.DataFrame, reason: str) -> str:
        clean = normalize_ohlcv(df, timezone=self.config.timezone)
        latest = clean.index.max().date().isoformat() if not clean.empty else "unknown"
        expected = self._expected_latest_session_date().isoformat()
        return f"{symbol} candle data ends on {latest}; expected {expected}. {reason}."

    def _expected_latest_session_date(self):
        now = datetime.now(self.config.timezone)
        candidate = now.date()
        if now.hour < 9 or (now.hour == 9 and now.minute < 30):
            candidate -= timedelta(days=1)

        holiday_start = candidate - timedelta(days=10)
        holiday_end = candidate + timedelta(days=1)
        holidays = {
            stamp.date()
            for stamp in _NYSEHolidayCalendar().holidays(start=holiday_start, end=holiday_end)
        }
        while candidate.weekday() >= 5 or candidate in holidays:
            candidate -= timedelta(days=1)
        return candidate

    def _stock_cache_path(self, symbol: str, interval: str, regular_hours_only: bool) -> Path:
        clean_symbol = self._safe_key(str(symbol).strip().upper())
        clean_interval = self._safe_key(str(interval or "5m").strip().lower())
        hours = "rth" if regular_hours_only else "all"
        return self.stock_cache_dir / f"{clean_symbol}_{clean_interval}_{hours}_raw.parquet"

    def _find_cached_option_session(
        self,
        symbol: str,
        signal: str,
        underlying_price: float,
        option_dte: int,
        reference_time,
        bar_size: str,
        what_to_show: str,
        use_rth: bool,
    ) -> tuple[dict[str, Any], pd.DataFrame]:
        ref_ts = pd.Timestamp(reference_time)
        if ref_ts.tzinfo is None:
            ref_ts = ref_ts.tz_localize(self.config.timezone)
        else:
            ref_ts = ref_ts.tz_convert(self.config.timezone)
        symbol_key = self._safe_key(str(symbol).strip().upper())
        signal_key = self._safe_key(str(signal).strip().upper())
        prefix = f"{symbol_key}-{signal_key}-{ref_ts.date().isoformat()}-{int(option_dte)}d-"
        matches = []
        for meta_path in self.option_cache_dir.glob(f"{prefix}*.json"):
            try:
                with meta_path.open("r", encoding="utf-8") as f:
                    info = json.load(f)
                strike = float(info.get("strike"))
            except Exception:
                continue
            distance = abs(strike - float(underlying_price))
            matches.append((distance, meta_path.with_suffix(".parquet"), dict(info or {})))
        if not matches:
            return {}, pd.DataFrame(columns=REQUIRED_OHLCV)

        # Listed strike intervals widen with the underlying price. A fixed
        # $0.55 tolerance rejects valid nearest strikes such as COIN 180 when
        # the stock is 181.20, delaying the replay until price drifts closer.
        # One percent remains tight enough to reject genuinely distant strikes.
        max_distance = max(0.55, abs(float(underlying_price)) * 0.01)
        for distance, path, info in sorted(matches, key=lambda item: item[0]):
            if distance > max_distance:
                continue
            bars = self._read_cache(path)
            if not bars.empty:
                return info, bars
        return {}, pd.DataFrame(columns=REQUIRED_OHLCV)

    def _contract_option_cache_path(
        self,
        info: dict[str, Any],
        reference_time,
        bar_size: str,
        what_to_show: str,
        use_rth: bool,
    ) -> Path:
        ref_ts = pd.Timestamp(reference_time)
        if ref_ts.tzinfo is None:
            ref_ts = ref_ts.tz_localize(self.config.timezone)
        else:
            ref_ts = ref_ts.tz_convert(self.config.timezone)
        key = {
            "symbol": str(info.get("symbol") or "").strip().upper(),
            "right": str(info.get("right") or "").strip().upper(),
            "option_dte": int(info.get("option_dte") or 0),
            "reference_date": ref_ts.date().isoformat(),
            "expiry": str(info.get("expiry") or ""),
            "strike": float(info.get("strike") or 0),
            "conId": str(info.get("conId") or ""),
            "bar_size": str(bar_size),
            "what_to_show": str(what_to_show),
            "use_rth": bool(use_rth),
        }
        digest = hashlib.sha1(json.dumps(key, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        symbol_key = self._safe_key(key["symbol"])
        signal_key = "CALL" if key["right"] == "C" else "PUT" if key["right"] == "P" else self._safe_key(key["right"])
        return self.option_cache_dir / f"{symbol_key}-{signal_key}-{key['reference_date']}-{int(key['option_dte'])}d-{digest}.parquet"

    def _option_failure_cache_path(
        self,
        symbol: str,
        signal: str,
        underlying_price: float,
        option_dte: int,
        reference_time,
        bar_size: str,
        what_to_show: str,
        use_rth: bool,
    ) -> Path:
        ref_ts = pd.Timestamp(reference_time)
        if ref_ts.tzinfo is None:
            ref_ts = ref_ts.tz_localize(self.config.timezone)
        else:
            ref_ts = ref_ts.tz_convert(self.config.timezone)
        key = {
            "symbol": str(symbol).strip().upper(),
            "signal": str(signal).strip().upper(),
            "underlying_price": round(float(underlying_price), 4),
            "option_dte": int(option_dte),
            "reference_time": ref_ts.isoformat(),
            "bar_size": str(bar_size),
            "what_to_show": str(what_to_show),
            "use_rth": bool(use_rth),
        }
        digest = hashlib.sha1(json.dumps(key, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        symbol_key = self._safe_key(key["symbol"])
        signal_key = self._safe_key(key["signal"])
        return self.option_cache_dir / f"{symbol_key}-{signal_key}-{ref_ts.date().isoformat()}-{int(option_dte)}d-unavailable-{digest}.json"

    @staticmethod
    def _safe_key(value: str) -> str:
        return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in str(value)).strip("-") or "unknown"

    @staticmethod
    def _period_to_days(period: str | None) -> int | None:
        value = str(period or "").strip().lower()
        try:
            if value.endswith("d"):
                return min(int(value[:-1]), IBKRMarketDataProvider.max_cache_days)
            if value.endswith("wk"):
                return min(int(value[:-2]) * 7, IBKRMarketDataProvider.max_cache_days)
            if value.endswith("mo"):
                return min(int(value[:-2]) * 30, IBKRMarketDataProvider.max_cache_days)
            if value.endswith("y"):
                return IBKRMarketDataProvider.max_cache_days
        except Exception:
            return None
        return None

    @staticmethod
    def _period_to_ib_duration(period: str) -> str:
        period = str(period or "30d").strip().lower()
        if period.endswith("d"):
            return f"{min(int(period[:-1]), IBKRMarketDataProvider.max_cache_days)} D"
        if period.endswith("wk"):
            days = min(int(period[:-2]) * 7, IBKRMarketDataProvider.max_cache_days)
            return f"{days} D"
        if period.endswith("mo"):
            days = min(int(period[:-2]) * 30, IBKRMarketDataProvider.max_cache_days)
            return f"{days} D"
        if period.endswith("y"):
            return f"{IBKRMarketDataProvider.max_cache_days} D"
        return "30 D"

    @staticmethod
    def _interval_to_ib_bar_size(interval: str) -> str:
        interval = str(interval or "5m").strip().lower()
        mapping = {
            "1m": "1 min",
            "2m": "2 mins",
            "5m": "5 mins",
            "15m": "15 mins",
            "30m": "30 mins",
            "60m": "1 hour",
            "1h": "1 hour",
            "1d": "1 day",
        }
        return mapping.get(interval, "5 mins")

    @staticmethod
    def _clean_num(value: Any) -> float | None:
        try:
            val = float(value)
            if np.isnan(val) or np.isinf(val):
                return None
            return val
        except Exception:
            return None
