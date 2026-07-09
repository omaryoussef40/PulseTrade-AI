from __future__ import annotations

"""Interactive Brokers market-data provider."""

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

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


@dataclass
class IBKRProviderConfig:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 11
    account: str | None = None
    readonly: bool = False
    timezone: ZoneInfo = EASTERN


class IBKRMarketDataProvider:
    name = "IBKR"

    def __init__(self, config: IBKRProviderConfig | None = None, ib: IB | None = None):
        self.config = config or IBKRProviderConfig()
        self.ib = ib
        self._owns_connection = ib is None
        self._stock_contract_cache: dict[str, Any] = {}

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
        duration = self._period_to_ib_duration(period or "30d")
        bar_size = self._interval_to_ib_bar_size(interval)
        return self.historical_bars(symbol=symbol, duration=duration, bar_size=bar_size, use_rth=regular_hours_only)

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
        ib = self.connect()
        symbol = str(symbol).strip().upper()
        signal = str(signal).strip().upper()
        if signal not in {"CALL", "PUT"}:
            raise RuntimeError(f"Unsupported option signal for {symbol}: {signal}")
        right = "C" if signal == "CALL" else "P"
        stock = self.qualify_stock(symbol)
        params = ib.reqSecDefOptParams(symbol, "", stock.secType, stock.conId)
        if not params:
            raise RuntimeError(f"No IBKR option chain returned for {symbol}")
        chain = next((p for p in params if p.exchange == "SMART"), params[0])

        ref_ts = pd.Timestamp(reference_time)
        if ref_ts.tzinfo is None:
            ref_ts = ref_ts.tz_localize(self.config.timezone)
        else:
            ref_ts = ref_ts.tz_convert(self.config.timezone)
        ref_date = ref_ts.date()
        target_date = ref_date + timedelta(days=int(option_dte))

        expirations = []
        for raw in sorted(chain.expirations):
            try:
                exp_date = datetime.strptime(str(raw), "%Y%m%d").date()
            except Exception:
                continue
            if exp_date >= ref_date:
                expirations.append((str(raw), exp_date))
        if not expirations:
            raise RuntimeError(f"No valid expirations for {symbol} on {ref_date}")
        expiry = min(expirations, key=lambda item: abs((item[1] - target_date).days))[0]

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
            raise RuntimeError(f"No valid strikes for {symbol} {expiry}")
        strike = min(strike_pool, key=lambda value: abs(float(value) - float(underlying_price)))

        contract = Option(symbol, expiry, strike, right, "SMART", currency="USD", multiplier="100")
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            raise RuntimeError(f"Could not qualify option {symbol} {expiry} {strike:g} {right}")
        contract = qualified[0]

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
        info = {
            "symbol": symbol,
            "option_dte": int(option_dte),
            "expiry": expiry,
            "strike": float(strike),
            "right": right,
            "localSymbol": getattr(contract, "localSymbol", ""),
            "conId": getattr(contract, "conId", None),
        }
        return info, df

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

    @staticmethod
    def _period_to_ib_duration(period: str) -> str:
        period = str(period or "30d").strip().lower()
        if period.endswith("d"):
            return f"{int(period[:-1])} D"
        if period.endswith("wk"):
            return f"{int(period[:-2])} W"
        if period.endswith("mo"):
            return f"{int(period[:-2])} M"
        if period.endswith("y"):
            return f"{int(period[:-1])} Y"
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
