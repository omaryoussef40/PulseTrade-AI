from __future__ import annotations

"""Central market-data layer for PulseTrade-AI.

This module is the single place where broker/data-provider access should live.
Live scanner, dashboard, engine, and future Strategy Lab integrations should call
this service instead of calling Yahoo/yfinance or raw IBKR historical APIs directly.

Phase 1 implements IBKR as the primary provider and keeps the interface small:
- connection helpers
- account summary
- stock contract qualification
- historical intraday bars
- historical daily bars
- snapshot quotes
- positions / open orders / executions helpers

The PMB strategy stays broker-agnostic: it receives normalized OHLCV DataFrames.
"""

import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

IB_IMPORT_ERROR: str | None = None
try:
    import nest_asyncio
    nest_asyncio.apply()
except Exception:
    pass

try:
    from ib_insync import IB, Stock, util
    IB_AVAILABLE = True
except Exception as exc:  # pragma: no cover - environment dependent
    IB_AVAILABLE = False
    IB_IMPORT_ERROR = repr(exc)
    IB = Any  # type: ignore
    Stock = Any  # type: ignore
    util = None  # type: ignore


EASTERN = ZoneInfo("America/New_York")
REQUIRED_OHLCV = ["Open", "High", "Low", "Close", "Volume"]


@dataclass
class MarketDataConfig:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 11
    account: str | None = None
    readonly: bool = False
    timezone: ZoneInfo = EASTERN


class IBKRMarketDataProvider:
    """IBKR-backed market-data provider.

    You may pass an existing connected IB instance to reuse the dashboard/engine
    connection, or call connect() and let the provider own the connection.
    """

    def __init__(self, config: MarketDataConfig | None = None, ib: IB | None = None):
        self.config = config or MarketDataConfig()
        self.ib = ib
        self._owns_connection = ib is None
        self._stock_contract_cache: dict[str, Any] = {}

    # -------------------------
    # Connection
    # -------------------------
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

    # -------------------------
    # Account / portfolio
    # -------------------------
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

        return {
            "account_id": account_id,
            "currency": currency,
            "connected": self.is_connected(),
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            **wanted,
        }

    def positions(self) -> list[dict[str, Any]]:
        ib = self.connect()
        out: list[dict[str, Any]] = []
        for p in ib.positions():
            if self.config.account and p.account != self.config.account:
                continue
            contract = p.contract
            out.append({
                "account": p.account,
                "symbol": getattr(contract, "symbol", ""),
                "secType": getattr(contract, "secType", ""),
                "exchange": getattr(contract, "exchange", ""),
                "currency": getattr(contract, "currency", ""),
                "position": p.position,
                "avgCost": p.avgCost,
                "conId": getattr(contract, "conId", None),
                "localSymbol": getattr(contract, "localSymbol", ""),
                "lastTradeDateOrContractMonth": getattr(contract, "lastTradeDateOrContractMonth", ""),
                "strike": getattr(contract, "strike", None),
                "right": getattr(contract, "right", ""),
            })
        return out

    def open_orders(self) -> list[dict[str, Any]]:
        ib = self.connect()
        out: list[dict[str, Any]] = []
        for trade in ib.openTrades():
            contract = trade.contract
            order = trade.order
            status = trade.orderStatus
            out.append({
                "symbol": getattr(contract, "symbol", ""),
                "secType": getattr(contract, "secType", ""),
                "localSymbol": getattr(contract, "localSymbol", ""),
                "action": getattr(order, "action", ""),
                "orderType": getattr(order, "orderType", ""),
                "totalQuantity": getattr(order, "totalQuantity", None),
                "lmtPrice": getattr(order, "lmtPrice", None),
                "auxPrice": getattr(order, "auxPrice", None),
                "status": getattr(status, "status", ""),
                "filled": getattr(status, "filled", None),
                "remaining": getattr(status, "remaining", None),
                "avgFillPrice": getattr(status, "avgFillPrice", None),
                "orderId": getattr(order, "orderId", None),
                "permId": getattr(order, "permId", None),
            })
        return out

    # -------------------------
    # Contracts / quotes / bars
    # -------------------------
    def qualify_stock(self, symbol: str):
        symbol = symbol.strip().upper()
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
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            mid = (bid + ask) / 2
            spread_pct = ((ask - bid) / mid * 100) if mid else None
        elif last is not None and last > 0:
            mid = last
        elif close is not None and close > 0:
            mid = close

        return {
            "symbol": symbol.strip().upper(),
            "bid": bid,
            "ask": ask,
            "last": last,
            "close": close,
            "mid": mid,
            "spread_pct": spread_pct,
            "volume": int(ticker.volume or 0),
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }

    # -------------------------
    # Normalization helpers
    # -------------------------
    def _bars_to_ohlcv(self, bars: Any) -> pd.DataFrame:
        if not bars:
            return pd.DataFrame(columns=REQUIRED_OHLCV)
        df = util.df(bars) if util is not None else pd.DataFrame()
        if df.empty:
            return pd.DataFrame(columns=REQUIRED_OHLCV)

        df = df.rename(columns={
            "date": "Datetime",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        })
        if "Datetime" not in df.columns:
            return pd.DataFrame(columns=REQUIRED_OHLCV)

        df["Datetime"] = pd.to_datetime(df["Datetime"], errors="coerce")
        df = df.dropna(subset=["Datetime"])
        if df.empty:
            return pd.DataFrame(columns=REQUIRED_OHLCV)

        try:
            if df["Datetime"].dt.tz is None:
                df["Datetime"] = df["Datetime"].dt.tz_localize(self.config.timezone)
            else:
                df["Datetime"] = df["Datetime"].dt.tz_convert(self.config.timezone)
        except Exception:
            pass

        df = df.set_index("Datetime")
        for col in REQUIRED_OHLCV:
            if col not in df.columns:
                df[col] = 0 if col == "Volume" else np.nan
            df[col] = pd.to_numeric(df[col], errors="coerce")

        return df[REQUIRED_OHLCV].dropna(subset=["Open", "High", "Low", "Close"]).sort_index()

    @staticmethod
    def _clean_num(value: Any) -> float | None:
        try:
            val = float(value)
            if np.isnan(val) or np.isinf(val):
                return None
            return val
        except Exception:
            return None


class MarketDataService:
    """Provider router.

    Phase 1 routes to IBKR only. Yahoo fallback can be added here later without
    changing scanner/engine/strategy calls.
    """

    def __init__(self, provider: IBKRMarketDataProvider):
        self.provider = provider

    def get_intraday(self, symbol: str, duration: str = "5 D", bar_size: str = "5 mins", use_rth: bool = True) -> pd.DataFrame:
        return self.provider.intraday_bars(symbol, duration=duration, bar_size=bar_size, use_rth=use_rth)

    def get_daily(self, symbol: str, duration: str = "20 D", use_rth: bool = True) -> pd.DataFrame:
        return self.provider.daily_bars(symbol, duration=duration, use_rth=use_rth)

    def get_quote(self, symbol: str) -> dict[str, Any]:
        return self.provider.snapshot_quote(symbol)

    def get_account_summary(self) -> dict[str, Any]:
        return self.provider.account_summary()

    def get_positions(self) -> list[dict[str, Any]]:
        return self.provider.positions()

    def get_open_orders(self) -> list[dict[str, Any]]:
        return self.provider.open_orders()


def provider_from_ib(ib: IB, timezone: ZoneInfo = EASTERN) -> IBKRMarketDataProvider:
    return IBKRMarketDataProvider(config=MarketDataConfig(timezone=timezone), ib=ib)


# =========================
# STRATEGY LAB PROVIDER FACTORY
# =========================
def create_market_data_provider(source: str = "IBKR", app_config: dict | None = None, ib=None):
    """Create a market-data provider for Strategy Lab and replay workflows.

    source: "IBKR" or "Yahoo".
    app_config: dashboard/config.json dict. Used for IBKR host/port/client settings.
    """
    source_clean = str(source or "IBKR").strip().lower()

    if source_clean in {"ibkr", "interactive brokers", "interactive_brokers"}:
        from providers.ibkr_provider import IBKRMarketDataProvider as _Provider
        from providers.ibkr_provider import IBKRProviderConfig as _Config

        cfg = app_config or {}
        ib_cfg = cfg.get("ib", {}) if isinstance(cfg, dict) else {}
        mode = str(cfg.get("account_mode", "Simulation")) if isinstance(cfg, dict) else "Simulation"
        port = int(ib_cfg.get("live_port", 7496) if mode == "Live" else ib_cfg.get("paper_port", 7497))
        return _Provider(
            config=_Config(
                host=ib_cfg.get("host", "127.0.0.1"),
                port=port,
                client_id=int(ib_cfg.get("client_id", 11)) + 200,
                account=ib_cfg.get("account") or None,
                readonly=bool(ib_cfg.get("readonly", False)),
                timezone=EASTERN,
            ),
            ib=ib,
        )

    if source_clean in {"yahoo", "yfinance", "yahoo finance"}:
        from providers.yahoo_provider import YahooMarketDataProvider as _Provider
        return _Provider(timezone=EASTERN)

    raise ValueError(f"Unsupported market data source: {source}")


def available_market_data_sources() -> list[str]:
    return ["IBKR", "Yahoo"]
