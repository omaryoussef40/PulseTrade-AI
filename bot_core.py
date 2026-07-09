from __future__ import annotations
# automatedapp.py
# Streamlit + Interactive Brokers + Telegram automated options scanner
#
# Run:
#   pip install streamlit pandas numpy plotly streamlit-autorefresh ib-insync requests pytz
#   streamlit run automatedapp.py
#
# IBKR setup:
#   1) Open Trader Workstation or IB Gateway
#   2) Enable API access: Configure > API > Settings > Enable ActiveX and Socket Clients
#   3) Paper trading default port is usually 7497, live TWS is usually 7496
#   4) Keep AUTO TRADING disabled until you have tested paper trading carefully

import os
import sys
import math
import time
import json
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
from market_data import IBKRMarketDataProvider, MarketDataConfig, provider_from_ib

IB_IMPORT_ERROR = None
try:
    import nest_asyncio
    nest_asyncio.apply()
except Exception:
    pass

try:
    from ib_insync import (
        IB,
        Stock,
        Option,
        MarketOrder,
        LimitOrder,
        StopOrder,
        ExecutionFilter,
        util,
    )
    IB_AVAILABLE = True
except Exception as exc:
    IB_AVAILABLE = False
    IB_IMPORT_ERROR = repr(exc)


# =========================
# CONFIG
# =========================

APP_NAME = "Automated Options App"
EASTERN = ZoneInfo("America/New_York")
EXPORT_DIR = "exports"
os.makedirs(EXPORT_DIR, exist_ok=True)

WATCHLIST = [
    "SPY", "QQQ", "IWM",
    "NVDA", "AAPL", "MSFT", "META", "AMZN", "GOOGL",
    "TSLA", "AMD", "PLTR", "COIN", "MSTR",
    "AVGO", "SMCI", "MU", "ARM", "TSM", "MRVL",
    "JPM", "GS", "BAC",
    "NFLX", "UBER", "XOM", "COST",
    "RBLX", "HOOD", "SOFI", "RKLB", "HIMS", "CRWD"
]

MIN_SCORE = 70
MIN_CONFIDENCE = 60
MIN_RVOL = 1.5
TARGET_DELTA = 0.50
DEFAULT_OPTION_DTE = 7
DEFAULT_RISK_PER_TRADE = 250
DEFAULT_MAX_CONTRACTS = 2
DEFAULT_ACCOUNT_SIZE = 1000
DEFAULT_MAX_TRADES_PER_DAY = 2
DEFAULT_TOP_N_TICKERS = 2
DEFAULT_MAX_SPEND_PER_TRADE = 250
DEFAULT_MAX_DAILY_CAPITAL = 500
DEFAULT_MIN_SCORE_UI = 70
DEFAULT_MIN_CONFIDENCE_UI = 75
DEFAULT_MIN_RVOL_UI = 1.5
DEFAULT_USE_RVOL_FILTER = False
DEFAULT_USE_RVOL_SCORE = False
DEFAULT_USE_RVOL_RANKING = False
DEFAULT_MIN_ATR_UI = 0.3
DEFAULT_ORDER_TYPE = "LIMIT"
TRADE_LOG_FILE = os.path.join(EXPORT_DIR, "trade_log.csv")
ALERT_LOG_FILE = os.path.join(EXPORT_DIR, "alert_log.csv")
ACTIVE_POSITIONS_FILE = os.path.join(EXPORT_DIR, "active_positions.json")

# Trade management defaults for intraday 7 DTE options
DEFAULT_STOP_LOSS_PCT = 20.0
DEFAULT_TAKE_PROFIT_PCT = 30.0
MAX_OPTION_ORDER_CHUNK_QTY = 5
DEFAULT_BREAKEVEN_TRIGGER_PCT = 15.0
DEFAULT_TRAILING_TRIGGER_PCT = 25.0
DEFAULT_TRAILING_STOP_PCT = 10.0
DEFAULT_FORCE_EXIT_TIME = dtime(15, 55)
DEFAULT_MAX_CONSECUTIVE_LOSSES = 2
DEFAULT_MAX_DAILY_DRAWDOWN_PCT = 5.0


# =========================
# DATA CLASSES
# =========================

@dataclass
class IBConfig:
    host: str
    port: int
    client_id: int
    account: str | None = None
    readonly: bool = False


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str


# =========================
# TELEGRAM
# =========================

def send_telegram_message(cfg: TelegramConfig, text: str) -> bool:
    if not cfg.bot_token or not cfg.chat_id:
        return False

    try:
        url = f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage"
        payload = {
            "chat_id": cfg.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception:
        return False


def log_alert(row: dict):
    df = pd.DataFrame([row])
    exists = os.path.exists(ALERT_LOG_FILE)
    df.to_csv(ALERT_LOG_FILE, mode="a", index=False, header=not exists)


# =========================
# IBKR CONNECTION
# =========================

def get_ib_connection(host: str, port: int, client_id: int, readonly: bool):
    if not IB_AVAILABLE:
        raise RuntimeError(
            "ib_insync could not be imported by the Python running this app. "
            f"Python: {sys.executable} | Version: {sys.version.split()[0]} | "
            f"Import error: {IB_IMPORT_ERROR}. "
            "Install with: python -m pip install ib-insync nest-asyncio"
        )

    ib = IB()
    ib.connect(host, port, clientId=client_id, readonly=readonly, timeout=10)
    return ib


def connect_ib(cfg: IBConfig):
    return get_ib_connection(cfg.host, cfg.port, cfg.client_id, cfg.readonly)


def qualify_stock(ib: IB, symbol: str):
    """Qualify a stock contract through the central market-data provider."""
    return provider_from_ib(ib, timezone=EASTERN).qualify_stock(symbol)

def fetch_ib_intraday(ib: IB, symbol: str, duration="5 D", bar_size="5 mins") -> pd.DataFrame:
    """Fetch normalized intraday OHLCV through market_data.py.

    Kept as a compatibility wrapper so existing scanner/engine calls continue
    to work while the codebase migrates to MarketDataService.
    """
    return provider_from_ib(ib, timezone=EASTERN).intraday_bars(
        symbol=symbol,
        duration=duration,
        bar_size=bar_size,
        use_rth=True,
    )


def fetch_ib_daily(ib: IB, symbol: str, duration="20 D") -> pd.DataFrame:
    """Fetch normalized daily OHLCV through market_data.py."""
    return provider_from_ib(ib, timezone=EASTERN).daily_bars(
        symbol=symbol,
        duration=duration,
        use_rth=True,
    )


def fetch_ibkr_account_summary_dict(cfg: IBConfig) -> dict:
    """Return key account fields through market_data.py for dashboard/engine use."""
    provider = IBKRMarketDataProvider(
        MarketDataConfig(
            host=cfg.host,
            port=cfg.port,
            client_id=cfg.client_id,
            account=cfg.account,
            readonly=cfg.readonly,
            timezone=EASTERN,
        )
    )
    try:
        return provider.account_summary()
    finally:
        provider.disconnect()


def fetch_ibkr_positions_list(cfg: IBConfig) -> list[dict]:
    """Return live broker positions through market_data.py for dashboard display."""
    provider = IBKRMarketDataProvider(
        MarketDataConfig(
            host=cfg.host,
            port=cfg.port,
            client_id=cfg.client_id,
            account=cfg.account,
            readonly=True,
            timezone=EASTERN,
        )
    )
    try:
        return provider.positions()
    finally:
        provider.disconnect()


# =========================
# INDICATORS
# =========================

def calculate_vwap(df: pd.DataFrame) -> pd.Series:
    df = df.copy()
    session = pd.Series(df.index.date, index=df.index)
    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3
    dollar_volume = typical_price * df["Volume"]
    cumulative_dollar_volume = dollar_volume.groupby(session).cumsum()
    cumulative_volume = df["Volume"].groupby(session).cumsum()
    return cumulative_dollar_volume / cumulative_volume.replace(0, np.nan)


def calculate_atr(df: pd.DataFrame, length=14) -> pd.Series:
    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close = (df["Low"] - df["Close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.rolling(length).mean()


def get_valid_sessions(intraday: pd.DataFrame, min_bars=50) -> list:
    dates = sorted(set(intraday.index.date))
    valid = []
    for session_date in dates:
        session_data = intraday[intraday.index.date == session_date]
        if len(session_data) >= min_bars:
            valid.append(session_date)
    return valid


def previous_trading_day_from_daily(daily: pd.DataFrame, today_date) -> pd.Timestamp | None:
    """Return the previous completed trading session from actual IBKR daily bars.

    This intentionally does not use weekday math. If Friday is a market
    holiday, the previous trading day for Monday should be Thursday.
    If today exists as an unfinished daily bar, it is excluded by date.
    """
    if daily.empty:
        return None

    df = daily.dropna().copy()
    if "Volume" in df.columns:
        df = df[pd.to_numeric(df["Volume"], errors="coerce").fillna(0) > 0]

    daily_dates = pd.Series(pd.to_datetime(df.index).date, index=df.index)
    prior = df[daily_dates < today_date]
    if prior.empty:
        return None
    return prior.index[-1]


def previous_completed_intraday_session(intraday: pd.DataFrame, today_date, min_bars: int = 50) -> tuple | None:
    """Fallback PDH/PDL source from actual intraday sessions, not weekdays.

    Uses the last session before today that has enough RTH 5-minute bars to
    represent a completed session. This skips weekends, market holidays, and
    empty/partial data days.
    """
    if intraday.empty:
        return None

    prior_dates = []
    for session_date in sorted(set(intraday.index.date)):
        if session_date >= today_date:
            continue
        session_data = intraday[intraday.index.date == session_date]
        if len(session_data) >= min_bars and float(session_data["Volume"].sum()) > 0:
            prior_dates.append(session_date)

    if not prior_dates:
        return None

    previous_session_date = prior_dates[-1]
    previous_session = intraday[intraday.index.date == previous_session_date]
    return previous_session_date, previous_session


def calculate_rvol(intraday: pd.DataFrame, today_date) -> float:
    today_data = intraday[intraday.index.date == today_date]
    if today_data.empty:
        return 0.0

    today_volume = float(today_data["Volume"].sum())
    previous_session_volumes = []

    for session_date in sorted(set(intraday.index.date)):
        if session_date == today_date:
            continue
        session_data = intraday[intraday.index.date == session_date]
        if len(session_data) >= 50:
            session_volume = float(session_data["Volume"].sum())
            if session_volume > 0:
                previous_session_volumes.append(session_volume)

    if not previous_session_volumes:
        return 0.0

    return today_volume / (sum(previous_session_volumes) / len(previous_session_volumes))


def calculate_support_resistance(daily: pd.DataFrame, current_price: float, lookback: int = 30, pivot_window: int = 2) -> dict:
    """Return nearest support/resistance as informational context only. Does not affect scoring."""
    if daily.empty or current_price <= 0:
        return {
            "Nearest Support": None,
            "Support Distance %": None,
            "Nearest Resistance": None,
            "Resistance Distance %": None,
        }

    df = daily.tail(lookback).copy()
    if len(df) < (pivot_window * 2 + 1):
        return {
            "Nearest Support": None,
            "Support Distance %": None,
            "Nearest Resistance": None,
            "Resistance Distance %": None,
        }

    supports = []
    resistances = []

    for i in range(pivot_window, len(df) - pivot_window):
        window = df.iloc[i - pivot_window:i + pivot_window + 1]
        center = df.iloc[i]
        low = float(center["Low"])
        high = float(center["High"])

        if low == float(window["Low"].min()):
            supports.append(low)
        if high == float(window["High"].max()):
            resistances.append(high)

    support_levels = sorted([x for x in supports if x < current_price], reverse=True)
    resistance_levels = sorted([x for x in resistances if x > current_price])

    nearest_support = support_levels[0] if support_levels else None
    nearest_resistance = resistance_levels[0] if resistance_levels else None

    support_distance = ((current_price - nearest_support) / current_price * 100) if nearest_support else None
    resistance_distance = ((nearest_resistance - current_price) / current_price * 100) if nearest_resistance else None

    return {
        "Nearest Support": round(nearest_support, 2) if nearest_support else None,
        "Support Distance %": round(support_distance, 2) if support_distance is not None else None,
        "Nearest Resistance": round(nearest_resistance, 2) if nearest_resistance else None,
        "Resistance Distance %": round(resistance_distance, 2) if resistance_distance is not None else None,
    }


# =========================
# SCANNER
# =========================

def scan_symbol_ib(
    ib: IB,
    symbol: str,
    use_rvol_score: bool = False,
    strategy_name: str = "pmb",
    orb_minutes: int = 15,
    min_session_bars: int = 7,
) -> dict | None:
    """Run the active scanner strategy on IBKR historical bars.

    PMB is the production strategy. The strategy logic lives in
    strategies/pmb/strategy.py so the live scanner and Strategy Lab can stay in
    parity. Confidence is returned only as a compatibility/debug field and is
    not used as a live trade blocker.
    """
    intraday = fetch_ib_intraday(ib, symbol)
    daily = fetch_ib_daily(ib, symbol)

    if intraday.empty or len(intraday) < 50:
        return None

    try:
        from strategies.registry import scan_dataframe as strategy_scan_dataframe
    except Exception:
        # Fallback to PMB directly if the registry is unavailable.
        from strategies.pmb.strategy import scan_dataframe as strategy_scan_dataframe
        return strategy_scan_dataframe(
            symbol=symbol,
            intraday=intraday,
            daily=daily,
            use_rvol_score=use_rvol_score,
            min_score=MIN_SCORE,
            timezone=EASTERN,
            orb_minutes=int(orb_minutes),
            min_session_bars=int(min_session_bars),
        )

    return strategy_scan_dataframe(
        strategy_name=strategy_name or "pmb",
        symbol=symbol,
        intraday=intraday,
        daily=daily,
        use_rvol_score=use_rvol_score,
        min_score=MIN_SCORE,
        timezone=EASTERN,
        orb_minutes=int(orb_minutes),
        min_session_bars=int(min_session_bars),
    )


# =========================
# OPTIONS VIA IBKR
# =========================

def get_option_expiry_and_strikes(ib: IB, symbol: str, dte_target=7):
    stock = qualify_stock(ib, symbol)
    params = ib.reqSecDefOptParams(symbol, "", stock.secType, stock.conId)
    if not params:
        return None, []

    chain = next((p for p in params if p.exchange == "SMART"), params[0])
    today = datetime.now(EASTERN).date()
    target = today + timedelta(days=dte_target)

    expirations = sorted(chain.expirations)
    expiration_dates = []
    for exp in expirations:
        try:
            expiration_dates.append((exp, datetime.strptime(exp, "%Y%m%d").date()))
        except Exception:
            continue

    valid = [(raw, dt) for raw, dt in expiration_dates if dt >= today]
    if not valid:
        return None, []

    best_exp = min(valid, key=lambda item: abs((item[1] - target).days))[0]
    strikes = sorted(float(s) for s in chain.strikes if s and s > 0)
    return best_exp, strikes


def estimate_delta(option_type: str, strike: float, stock_price: float) -> float:
    distance_pct = (strike - stock_price) / stock_price
    if option_type == "CALL":
        delta = 0.50 - (distance_pct * 5)
        return max(0.10, min(0.90, delta))
    delta = -0.50 - (distance_pct * 5)
    return max(-0.90, min(-0.10, delta))


def get_snapshot_mid(ib: IB, contract) -> dict:
    ticker = ib.reqMktData(contract, "", snapshot=True, regulatorySnapshot=False)
    ib.sleep(2)

    bid = ticker.bid if ticker.bid and ticker.bid > 0 else np.nan
    ask = ticker.ask if ticker.ask and ticker.ask > 0 else np.nan
    last = ticker.last if ticker.last and ticker.last > 0 else np.nan

    if not np.isnan(bid) and not np.isnan(ask):
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid if mid else np.nan
    elif not np.isnan(last):
        mid = last
        spread_pct = np.nan
    else:
        mid = np.nan
        spread_pct = np.nan

    return {
        "Bid": bid,
        "Ask": ask,
        "Last": last,
        "Mid": mid,
        "Spread %": spread_pct,
        "Volume": ticker.volume if ticker.volume else 0,
    }


def _finite_number(value) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if np.isfinite(numeric) else None


def recommend_option_ib(ib: IB, symbol: str, signal: str, stock_price: float, dte_target=7) -> dict | None:
    if signal not in ["CALL", "PUT"]:
        return None

    expiry, strikes = get_option_expiry_and_strikes(ib, symbol, dte_target)
    if not expiry or not strikes:
        return None

    right = "C" if signal == "CALL" else "P"
    option_type = "CALL" if signal == "CALL" else "PUT"

    nearby = [s for s in strikes if stock_price * 0.90 <= s <= stock_price * 1.10]
    if not nearby:
        return None

    # Start with strikes close to target estimated delta to reduce API calls.
    candidates = []
    for strike in nearby:
        delta = estimate_delta(option_type, strike, stock_price)
        target_distance = abs(delta - TARGET_DELTA) if option_type == "CALL" else abs(delta + TARGET_DELTA)
        candidates.append((target_distance, abs(strike - stock_price), strike, delta))

    candidates = sorted(candidates)[:12]
    option_rows = []

    for _, strike_distance, strike, est_delta in candidates:
        contract = Option(symbol, expiry, strike, right, "SMART", currency="USD", multiplier="100")
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            continue
        contract = qualified[0]
        market = get_snapshot_mid(ib, contract)
        mid = _finite_number(market.get("Mid"))

        if mid is None or mid <= 0:
            continue

        spread_pct = _finite_number(market.get("Spread %"))
        if spread_pct is not None and spread_pct > 0.25:
            continue
        bid = _finite_number(market.get("Bid"))
        ask = _finite_number(market.get("Ask"))
        last = _finite_number(market.get("Last"))
        volume = _finite_number(market.get("Volume")) or 0

        option_rows.append({
            "Contract": contract,
            "Option": f"{symbol} {expiry} {strike:g} {option_type}",
            "Expiry": expiry,
            "Strike": strike,
            "Type": option_type,
            "Delta": round(est_delta, 2),
            "Bid": round(bid, 2) if bid is not None else None,
            "Ask": round(ask, 2) if ask is not None else None,
            "Last": round(last, 2) if last is not None else None,
            "Mid": round(mid, 2),
            "Spread %": round(spread_pct * 100, 1) if spread_pct is not None else None,
            "Volume": int(volume),
            "Risk / Contract": round(mid * 100, 2),
            "Strike Distance": strike_distance,
        })

    if not option_rows:
        return None

    df = pd.DataFrame(option_rows)
    df["Delta Distance"] = df["Delta"].apply(lambda d: abs(d - TARGET_DELTA) if option_type == "CALL" else abs(d + TARGET_DELTA))
    df = df.sort_values(["Delta Distance", "Strike Distance", "Spread %"], na_position="last")
    best = df.iloc[0].to_dict()

    option_score = 0
    spread_pct = best.get("Spread %")
    if spread_pct is not None:
        if spread_pct <= 5:
            option_score += 35
        elif spread_pct <= 10:
            option_score += 25
        elif spread_pct <= 20:
            option_score += 15
    if abs(best["Delta"]) >= 0.40:
        option_score += 35
    elif abs(best["Delta"]) >= 0.30:
        option_score += 20
    if best["Mid"] > 0:
        option_score += 30

    best["Option Score"] = option_score
    return best


# =========================
# ORDER PLACEMENT
# =========================

def calculate_contract_quantity(mid_price: float, max_spend_per_trade: float, max_contracts: int) -> int:
    """Return how many option contracts fit inside the allowed dollar spend."""
    if not mid_price or mid_price <= 0:
        return 0
    per_contract_cost = mid_price * 100
    qty = math.floor(max_spend_per_trade / per_contract_cost)
    return max(0, min(qty, max_contracts))


def get_today_trade_stats() -> tuple[int, float]:
    """Read local trade log and return today's trade count and deployed capital."""
    if not os.path.exists(TRADE_LOG_FILE):
        return 0, 0.0

    try:
        df = pd.read_csv(TRADE_LOG_FILE)
        if df.empty or "timestamp" not in df.columns:
            return 0, 0.0

        df["timestamp"] = _parse_timestamps_utc(df["timestamp"])
        today_et = datetime.now(EASTERN).date()
        today_df = df[df["timestamp"].dt.tz_convert(EASTERN).dt.date == today_et]

        if today_df.empty:
            return 0, 0.0

        if "event" in today_df.columns:
            today_df = today_df[today_df["event"].fillna("").astype(str).str.upper() == "ENTRY"]
        if "status" in today_df.columns:
            active_statuses = {"filled", "partiallyfilled", "submitted", "presubmitted"}
            today_df = today_df[today_df["status"].fillna("").astype(str).str.lower().isin(active_statuses)]
        if today_df.empty:
            return 0, 0.0

        trade_count = len(today_df)
        deployed = 0.0

        if {"filled_quantity", "entry_price"}.issubset(today_df.columns):
            deployed = float(
                (
                    pd.to_numeric(today_df["filled_quantity"], errors="coerce").fillna(0)
                    * pd.to_numeric(today_df["entry_price"], errors="coerce").fillna(0)
                    * 100
                ).sum()
            )
        if deployed <= 0 and "estimated_cost" in today_df.columns:
            deployed = float(pd.to_numeric(today_df["estimated_cost"], errors="coerce").fillna(0).sum())
        elif deployed <= 0 and {"quantity", "limit_price"}.issubset(today_df.columns):
            deployed = float(
                (
                    pd.to_numeric(today_df["quantity"], errors="coerce").fillna(0)
                    * pd.to_numeric(today_df["limit_price"], errors="coerce").fillna(0)
                    * 100
                ).sum()
            )

        return trade_count, deployed
    except Exception:
        return 0, 0.0


def opportunity_rank_score(result: dict, option: dict | None, use_rvol_ranking: bool = False) -> float:
    """Composite score used to choose only the best tickers of the day."""
    option_score = float(option.get("Option Score", 0)) if option else 0.0
    rvol_component = min(float(result.get("RVOL", 0)) * 10, 30) if use_rvol_ranking else 0
    atr_component = min(float(result.get("ATR %", 0)) * 10, 15)

    return round(
        float(result.get("Score", 0)) * 0.60
        + option_score * 0.25
        + atr_component
        + rvol_component,
        2,
    )


def place_option_order(
    ib: IB,
    option_contract,
    action: str,
    quantity: int,
    order_type: str,
    limit_price: float | None,
    account: str | None = None,
):
    if quantity <= 0:
        raise ValueError("Quantity must be greater than zero")

    if order_type == "MARKET":
        order = MarketOrder(action, quantity)
    else:
        if limit_price is None or limit_price <= 0:
            raise ValueError("Limit price must be greater than zero")
        order = LimitOrder(action, quantity, round(limit_price, 2))

    if account:
        order.account = account

    submitted_at = datetime.now(EASTERN)
    trade = ib.placeOrder(option_contract, order)
    execution_fills = []
    for attempt in range(60):
        ib.sleep(1)
        status = str(getattr(trade.orderStatus, "status", "") or "")
        filled = float(getattr(trade.orderStatus, "filled", 0) or 0)
        remaining = float(getattr(trade.orderStatus, "remaining", quantity) or 0)
        if attempt % 3 == 0 or status.lower() in {"cancelled", "canceled", "apicancelled", "inactive"}:
            execution_fills = recent_contract_execution_fills(
                ib,
                option_contract,
                action=action,
                account=account,
                since=submitted_at - timedelta(seconds=5),
            )
        execution_qty = sum(float(getattr(getattr(fill, "execution", None), "shares", 0) or 0) for fill in execution_fills)
        if filled >= quantity or execution_qty >= quantity or status.lower() == "filled":
            break
        if filled > 0 and remaining <= 0:
            break
    setattr(trade, "pulse_execution_fills", execution_fills)
    return trade


def recent_contract_execution_fills(
    ib: IB,
    option_contract,
    action: str,
    account: str | None = None,
    since: datetime | None = None,
) -> list:
    """Fetch recent IBKR executions for the same contract/action.

    IBKR orderStatus can briefly report Cancelled/Inactive while executions are
    still the real source of truth. This helper lets entry logging prefer fills.
    """
    since = since or (datetime.now(EASTERN) - timedelta(minutes=5))
    if since.tzinfo is None:
        since = since.replace(tzinfo=EASTERN)
    else:
        since = since.astimezone(EASTERN)

    filt = ExecutionFilter()
    filt.time = since.strftime("%Y%m%d %H:%M:%S")
    if account:
        filt.acctCode = account

    try:
        fills = ib.reqExecutions(filt)
    except Exception:
        return []

    target_con_id = getattr(option_contract, "conId", None)
    target_symbol = str(getattr(option_contract, "localSymbol", "") or getattr(option_contract, "symbol", "") or "")
    wanted_sides = {"BOT", "BUY"} if str(action).upper() == "BUY" else {"SLD", "SELL"}
    matched = []
    for fill in fills or []:
        contract = getattr(fill, "contract", None)
        execution = getattr(fill, "execution", None)
        if contract is None or execution is None:
            continue
        if account and getattr(execution, "acctNumber", None) and getattr(execution, "acctNumber", None) != account:
            continue
        side = str(getattr(execution, "side", "") or "").upper()
        if side not in wanted_sides:
            continue
        con_id = getattr(contract, "conId", None)
        local_symbol = str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or "")
        same_con_id = False
        try:
            same_con_id = bool(target_con_id and con_id and int(float(con_id)) == int(float(target_con_id)))
        except Exception:
            same_con_id = False
        if same_con_id:
            matched.append(fill)
        elif target_symbol and local_symbol and target_symbol == local_symbol:
            matched.append(fill)
    return matched


def trade_fill_details(trade, requested_quantity: int, fallback_price: float | None = None) -> dict:
    """Normalize IBKR order status using fills first, then status text."""
    order_status = getattr(trade, "orderStatus", None)
    raw_status = str(getattr(order_status, "status", "") or "")
    filled_qty = float(getattr(order_status, "filled", 0) or 0)
    remaining_qty = float(getattr(order_status, "remaining", max(int(requested_quantity), 0)) or 0)
    avg_fill_price = float(getattr(order_status, "avgFillPrice", 0) or 0)

    fills = list(getattr(trade, "fills", []) or []) + list(getattr(trade, "pulse_execution_fills", []) or [])
    if fills:
        fill_qty = 0.0
        fill_value = 0.0
        seen_exec_ids = set()
        for fill in fills:
            execution = getattr(fill, "execution", None)
            exec_id = str(getattr(execution, "execId", "") or "")
            if exec_id and exec_id in seen_exec_ids:
                continue
            if exec_id:
                seen_exec_ids.add(exec_id)
            shares = float(getattr(execution, "shares", 0) or 0)
            price = float(getattr(execution, "price", 0) or 0)
            fill_qty += shares
            fill_value += shares * price
        if fill_qty > 0:
            filled_qty = max(filled_qty, fill_qty)
            avg_fill_price = fill_value / fill_qty if fill_value else avg_fill_price

    if filled_qty > 0:
        status = "Filled" if filled_qty >= int(requested_quantity) or remaining_qty <= 0 else "PartiallyFilled"
    else:
        status = raw_status or "Submitted"

    if avg_fill_price <= 0 and fallback_price is not None:
        avg_fill_price = float(fallback_price or 0)

    return {
        "status": status,
        "raw_status": raw_status,
        "filled_qty": int(filled_qty) if float(filled_qty).is_integer() else filled_qty,
        "remaining_qty": int(remaining_qty) if float(remaining_qty).is_integer() else remaining_qty,
        "avg_fill_price": round(float(avg_fill_price), 4) if avg_fill_price else None,
    }


def log_trade(row: dict):
    exists = os.path.exists(TRADE_LOG_FILE)
    columns = None
    if exists:
        try:
            columns = list(pd.read_csv(TRADE_LOG_FILE, nrows=0).columns)
        except Exception:
            columns = None
    if columns:
        row = {column: row.get(column) for column in columns}
    df = pd.DataFrame([row])
    df.to_csv(TRADE_LOG_FILE, mode="a", index=False, header=not exists)


def sync_today_executions_to_trade_log(ib: IB, account: str | None = None, target_date=None) -> tuple[int, str]:
    """Import IBKR executions for one ET date into the local journal.

    Gateway exposes recent executions before Flex statements are available.
    Rows are grouped by option contract and side so partial fills become one
    entry/exit row in the performance journal.
    """
    if not IB_AVAILABLE:
        return 0, "IBKR API is not available."

    target_date = target_date or datetime.now(EASTERN).date()
    if hasattr(target_date, "date"):
        target_date = target_date.date()

    filt = ExecutionFilter()
    filt.time = datetime.combine(target_date, datetime.min.time(), tzinfo=EASTERN).strftime("%Y%m%d %H:%M:%S")
    if account:
        filt.acctCode = account

    fills = ib.reqExecutions(filt)
    if not fills:
        return 0, f"No IBKR executions found for {target_date}."

    grouped: dict[tuple, dict] = {}
    for fill in fills:
        contract = getattr(fill, "contract", None)
        execution = getattr(fill, "execution", None)
        if contract is None or execution is None:
            continue
        if account and getattr(execution, "acctNumber", None) and getattr(execution, "acctNumber", None) != account:
            continue

        symbol = str(getattr(contract, "symbol", "") or "").upper()
        sec_type = str(getattr(contract, "secType", "") or "").upper()
        side = str(getattr(execution, "side", "") or "").upper()
        if not symbol or side not in {"BOT", "BUY", "SLD", "SELL"}:
            continue

        con_id = getattr(contract, "conId", None)
        expiry = str(getattr(contract, "lastTradeDateOrContractMonth", "") or "")
        strike = getattr(contract, "strike", "")
        right = str(getattr(contract, "right", "") or "").upper()
        signal = "CALL" if right.startswith("C") else "PUT" if right.startswith("P") else ""
        local_symbol = str(getattr(contract, "localSymbol", "") or "")
        option_label = local_symbol or " ".join(x for x in [symbol, expiry, str(strike), signal] if x)
        event = "ENTRY" if side in {"BOT", "BUY"} else "EXIT"
        group_side = "BUY" if event == "ENTRY" else "SELL"
        key = (con_id or option_label, group_side)

        shares = abs(float(getattr(execution, "shares", 0) or 0))
        price = float(getattr(execution, "price", 0) or 0)
        exec_time = getattr(execution, "time", None) or datetime.now(EASTERN)
        if hasattr(exec_time, "astimezone"):
            exec_time = exec_time.astimezone(EASTERN)
        try:
            if exec_time.date() != target_date:
                continue
        except Exception:
            continue

        row = grouped.setdefault(key, {
            "timestamp": exec_time,
            "event": event,
            "source": "IBKR_EXECUTION",
            "external_id": f"IBKR_EXEC-{target_date}-{key[0]}-{group_side}",
            "symbol": symbol,
            "signal": signal,
            "option": option_label,
            "expiry": expiry,
            "strike": strike,
            "con_id": con_id,
            "quantity": 0.0,
            "value": 0.0,
            "exec_ids": [],
        })
        row["quantity"] += shares
        row["value"] += shares * price
        row["exec_ids"].append(str(getattr(execution, "execId", "")))
        if exec_time and exec_time < row["timestamp"]:
            row["timestamp"] = exec_time

    if not grouped:
        return 0, f"No stock/option executions found for {target_date}."

    rows = []
    entry_price_by_contract = {}
    for key, item in grouped.items():
        avg_price = item["value"] / item["quantity"] if item["quantity"] else 0.0
        if item["event"] == "ENTRY":
            entry_price_by_contract[key[0]] = avg_price
        rows.append(item | {
            "timestamp": item["timestamp"].isoformat() if hasattr(item["timestamp"], "isoformat") else str(item["timestamp"]),
            "quantity": int(item["quantity"]) if float(item["quantity"]).is_integer() else item["quantity"],
            "filled_quantity": int(item["quantity"]) if float(item["quantity"]).is_integer() else item["quantity"],
            "entry_price": round(avg_price, 4) if item["event"] == "ENTRY" else None,
            "exit_price": round(avg_price, 4) if item["event"] == "EXIT" else None,
            "limit_price": round(avg_price, 4) if item["event"] == "ENTRY" else None,
            "realized_pnl": 0.0,
            "status": "Filled",
            "broker_status": "Execution",
            "exec_ids": ",".join(item["exec_ids"]),
        })

    for row in rows:
        if row["event"] != "EXIT":
            continue
        entry_price = entry_price_by_contract.get((row.get("con_id") or row.get("option")))
        if entry_price:
            row["entry_price"] = round(float(entry_price), 4)
            row["realized_pnl"] = round((float(row["exit_price"]) - float(entry_price)) * float(row["quantity"]) * 100, 2)

    path = _Path(TRADE_LOG_FILE)
    existing = pd.read_csv(path) if path.exists() else pd.DataFrame()
    existing_ids = set(existing.get("external_id", pd.Series(dtype=str)).dropna().astype(str).tolist()) if not existing.empty else set()
    existing_trade_keys = set()
    if not existing.empty:
        existing_ts = _parse_timestamps_utc(existing.get("timestamp", pd.Series(dtype=str))).dt.tz_convert(EASTERN)
        for pos, (_, old) in enumerate(existing.iterrows()):
            try:
                old_date = existing_ts.iloc[pos].date()
            except Exception:
                old_date = None
            old_event = str(old.get("event", "") or "").upper()
            old_contract = str(old.get("con_id", "") or old.get("option", "") or "")
            old_qty = _number_or_none(old.get("filled_quantity")) or _number_or_none(old.get("quantity")) or 0.0
            old_price = _number_or_none(old.get("entry_price")) if old_event == "ENTRY" else _number_or_none(old.get("exit_price"))
            existing_trade_keys.add((old_date, old_event, old_contract, round(float(old_qty), 4), round(float(old_price or 0.0), 4)))
    imported = 0
    new_rows = []

    for row in rows:
        if row["external_id"] in existing_ids:
            continue
        row_contract = str(row.get("con_id", "") or row.get("option", "") or "")
        row_price = row.get("entry_price") if row["event"] == "ENTRY" else row.get("exit_price")
        trade_key = (
            target_date,
            str(row.get("event", "") or "").upper(),
            row_contract,
            round(float(row.get("filled_quantity") or row.get("quantity") or 0.0), 4),
            round(float(row_price or 0.0), 4),
        )
        if trade_key in existing_trade_keys:
            continue
        new_rows.append({k: v for k, v in row.items() if k != "value"})
        imported += 1
        existing_ids.add(row["external_id"])
        existing_trade_keys.add(trade_key)

    combined = pd.concat([existing, pd.DataFrame(new_rows)], ignore_index=True, sort=False) if new_rows else existing
    if not combined.empty and "timestamp" in combined.columns:
        sort_ts = _parse_timestamps_utc(combined["timestamp"])
        combined = combined.assign(_sort_ts=sort_ts).sort_values("_sort_ts", na_position="last").drop(columns=["_sort_ts"])
    combined.to_csv(path, index=False)
    return imported, f"IBKR executions grouped={len(rows)} imported_or_updated={imported}"


def read_active_positions() -> list[dict]:
    if not os.path.exists(ACTIVE_POSITIONS_FILE):
        return []
    try:
        with open(ACTIVE_POSITIONS_FILE, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def write_active_positions(positions: list[dict]):
    with open(ACTIVE_POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2, default=str)


def is_filled_order_status(status: str) -> bool:
    """Return True only when an entry order created a real broker position."""
    return str(status or "").strip().lower() in {"filled", "partiallyfilled", "partially filled"}


def _number_or_none(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(str(value).replace(",", ""))
    except Exception:
        return None


def _parse_timestamps_utc(values):
    try:
        return pd.to_datetime(values, errors="coerce", utc=True, format="mixed")
    except TypeError:
        return pd.to_datetime(values, errors="coerce", utc=True)


def _broker_position_contract_key(contract) -> str:
    con_id = getattr(contract, "conId", None)
    if con_id:
        return f"conid:{int(con_id)}"
    symbol = str(getattr(contract, "symbol", "") or "").upper()
    expiry = str(getattr(contract, "lastTradeDateOrContractMonth", "") or "")
    strike = str(getattr(contract, "strike", "") or "")
    right = str(getattr(contract, "right", "") or "").upper()
    return f"opt:{symbol}:{expiry}:{strike}:{right}"


def broker_open_option_positions(ib: IB, account: str | None = None) -> list[dict]:
    """Return non-zero option positions currently visible at IBKR."""
    positions = []
    for broker_pos in ib.positions():
        if account and getattr(broker_pos, "account", None) != account:
            continue
        contract = getattr(broker_pos, "contract", None)
        if contract is None:
            continue
        if str(getattr(contract, "secType", "") or "").upper() != "OPT":
            continue
        try:
            qty = float(getattr(broker_pos, "position", 0) or 0)
        except Exception:
            continue
        if qty == 0:
            continue
        right = str(getattr(contract, "right", "") or "").upper()
        positions.append({
            "key": _broker_position_contract_key(contract),
            "symbol": str(getattr(contract, "symbol", "") or "").upper(),
            "signal": "CALL" if right.startswith("C") else "PUT" if right.startswith("P") else "",
            "option": str(getattr(contract, "localSymbol", "") or ""),
            "expiry": str(getattr(contract, "lastTradeDateOrContractMonth", "") or ""),
            "strike": float(getattr(contract, "strike", 0) or 0),
            "con_id": getattr(contract, "conId", None),
            "quantity": int(abs(qty)) if float(abs(qty)).is_integer() else abs(qty),
            "avg_cost": float(getattr(broker_pos, "avgCost", 0) or 0),
            "account": getattr(broker_pos, "account", None),
        })
    return positions


def broker_open_option_symbols(ib: IB, account: str | None = None) -> set[str]:
    return {pos["symbol"] for pos in broker_open_option_positions(ib, account=account) if pos.get("symbol")}


def _trade_log_entry_lookup() -> dict[str, dict]:
    if not os.path.exists(TRADE_LOG_FILE):
        return {}
    try:
        df = pd.read_csv(TRADE_LOG_FILE)
    except Exception:
        return {}
    if df.empty or "event" not in df.columns:
        return {}

    df = df[df["event"].fillna("").astype(str).str.upper() == "ENTRY"].copy()
    if df.empty:
        return {}
    if "timestamp" in df.columns:
        df["timestamp"] = _parse_timestamps_utc(df["timestamp"])
        df = df.sort_values("timestamp")

    lookup: dict[str, dict] = {}
    for _, row in df.iterrows():
        con_id = row.get("con_id")
        if pd.notna(con_id) and str(con_id).strip():
            try:
                lookup[f"conid:{int(float(con_id))}"] = row.to_dict()
            except Exception:
                pass
        symbol = str(row.get("symbol", "") or "").upper()
        expiry = str(row.get("expiry", "") or "")
        strike = str(row.get("strike", "") or "")
        signal = str(row.get("signal", "") or "").upper()
        right = "C" if signal == "CALL" else "P" if signal == "PUT" else ""
        if symbol and expiry and strike and right:
            lookup[f"opt:{symbol}:{expiry}:{strike}:{right}"] = row.to_dict()
        option = str(row.get("option", "") or "")
        if option:
            lookup[f"label:{option}"] = row.to_dict()
    return lookup


def sync_active_positions_from_broker(
    ib: IB,
    account: str | None = None,
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
    take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT,
    submit_protection: bool = False,
) -> list[dict]:
    """Add missing local active-position records for filled IBKR option positions.

    This covers the case where the order callback said Inactive/Submitted locally
    but IBKR later reports a real execution/position.
    """
    broker_positions = broker_open_option_positions(ib, account=account)
    if not broker_positions:
        return []

    local_positions = read_active_positions()
    local_keys = set()
    for pos in local_positions:
        if pos.get("con_id"):
            try:
                local_keys.add(f"conid:{int(pos.get('con_id'))}")
            except Exception:
                pass
        option = str(pos.get("option", "") or "")
        if option:
            local_keys.add(f"label:{option}")

    entry_lookup = _trade_log_entry_lookup()
    added = []
    for broker_pos in broker_positions:
        key = broker_pos["key"]
        option = broker_pos.get("option") or " ".join(
            str(x) for x in [
                broker_pos.get("symbol"),
                broker_pos.get("expiry"),
                broker_pos.get("strike"),
                broker_pos.get("signal"),
            ] if x
        )
        if key in local_keys or f"label:{option}" in local_keys:
            continue

        log_row = entry_lookup.get(key) or entry_lookup.get(f"label:{option}") or {}
        entry_price = _number_or_none(log_row.get("entry_price")) if log_row else None
        if entry_price is None:
            entry_price = _number_or_none(log_row.get("limit_price")) if log_row else None
        if entry_price is None:
            avg_cost = float(broker_pos.get("avg_cost") or 0)
            entry_price = avg_cost / 100.0 if avg_cost > 10 else avg_cost
        if not entry_price or entry_price <= 0:
            continue

        symbol = broker_pos.get("symbol") or str(log_row.get("symbol", "") or "").upper()
        signal = broker_pos.get("signal") or str(log_row.get("signal", "") or "").upper()
        position_id = f"{symbol}-{broker_pos.get('expiry')}-{broker_pos.get('strike')}-{signal}-{datetime.now(EASTERN).strftime('%Y%m%d%H%M%S')}"
        position = {
            "id": position_id,
            "symbol": symbol,
            "signal": signal,
            "option": option,
            "expiry": broker_pos.get("expiry"),
            "strike": float(broker_pos.get("strike") or 0),
            "con_id": broker_pos.get("con_id"),
            "quantity": broker_pos.get("quantity"),
            "entry_price": round(float(entry_price), 2),
            "current_stop_price": round(float(entry_price) * (1 - float(stop_loss_pct) / 100), 2),
            "take_profit_price": round(float(entry_price) * (1 + float(take_profit_pct) / 100), 2),
            "highest_price": round(float(entry_price), 2),
            "breakeven_active": False,
            "trailing_active": False,
            "entry_time": datetime.now(EASTERN).isoformat(),
            "entry_status": "Filled via IBKR sync",
            "source": "IBKR_POSITION_SYNC",
        }
        if submit_protection:
            protection_events = ensure_protective_orders(
                ib,
                position,
                account=account,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
            )
        else:
            protection_events = []
        local_positions.append(position)
        added.append({
            "Symbol": symbol,
            "Option": option,
            "Action": "ADDED",
            "Reason": "Open IBKR position missing from local active positions",
        })
        added.extend(protection_events)

    if added:
        write_active_positions(local_positions)
    return added


def get_today_loss_stats() -> tuple[int, float]:
    """Return today's consecutive losses and realized P/L from the local trade log."""
    if not os.path.exists(TRADE_LOG_FILE):
        return 0, 0.0
    try:
        df = pd.read_csv(TRADE_LOG_FILE)
        if df.empty or "timestamp" not in df.columns:
            return 0, 0.0
        df["timestamp"] = _parse_timestamps_utc(df["timestamp"])
        today = datetime.now(EASTERN).date()
        df = df[df["timestamp"].dt.tz_convert(EASTERN).dt.date == today].copy()
        if df.empty:
            return 0, 0.0

        realized = float(pd.to_numeric(df.get("realized_pnl", 0), errors="coerce").fillna(0).sum())
        exits = df[df.get("event", "") == "EXIT"] if "event" in df.columns else pd.DataFrame()
        consecutive_losses = 0
        if not exits.empty and "realized_pnl" in exits.columns:
            exits = exits.sort_values("timestamp")
            for pnl in reversed(pd.to_numeric(exits["realized_pnl"], errors="coerce").fillna(0).tolist()):
                if pnl < 0:
                    consecutive_losses += 1
                else:
                    break
        return consecutive_losses, realized
    except Exception:
        return 0, 0.0


def reconstruct_option_contract(pos: dict):
    right = "C" if pos.get("signal") == "CALL" else "P"
    contract = Option(
        pos["symbol"],
        str(pos["expiry"]),
        float(pos["strike"]),
        right,
        "SMART",
        currency="USD",
        multiplier="100",
    )
    if pos.get("con_id"):
        contract.conId = int(pos["con_id"])
    return contract


def _trade_is_active(trade) -> bool:
    status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "").lower()
    return status not in {"filled", "cancelled", "canceled", "apicancelled", "inactive"}


def _order_id(order) -> int | None:
    try:
        value = int(getattr(order, "orderId", 0) or 0)
        return value if value > 0 else None
    except Exception:
        return None


def _coerce_order_id(value) -> int | None:
    try:
        value = int(float(value or 0))
        return value if value > 0 else None
    except Exception:
        return None


def _order_chunks(quantity: int, max_chunk: int = MAX_OPTION_ORDER_CHUNK_QTY) -> list[int]:
    quantity = int(abs(quantity or 0))
    if quantity <= 0:
        return []
    max_chunk = max(1, int(max_chunk or 1))
    return [min(max_chunk, quantity - offset) for offset in range(0, quantity, max_chunk)]


def _coerce_order_ids(value) -> list[int]:
    if isinstance(value, (list, tuple, set)):
        values = value
    elif isinstance(value, str) and "," in value:
        values = value.split(",")
    else:
        values = [value]
    ids = []
    for item in values:
        order_id = _coerce_order_id(item)
        if order_id:
            ids.append(order_id)
    return ids


def _open_sell_trades_for_position(ib: IB, pos: dict) -> list:
    try:
        ib.reqOpenOrders()
        ib.sleep(0.2)
    except Exception:
        pass

    con_id = pos.get("con_id")
    matches = []
    for trade in ib.openTrades() or []:
        if not _trade_is_active(trade):
            continue
        order = getattr(trade, "order", None)
        contract = getattr(trade, "contract", None)
        if str(getattr(order, "action", "") or "").upper() != "SELL":
            continue
        try:
            same_contract = bool(con_id and int(float(getattr(contract, "conId", 0) or 0)) == int(float(con_id)))
        except Exception:
            same_contract = False
        if same_contract:
            matches.append(trade)
    return matches


def ensure_protective_orders(
    ib: IB,
    pos: dict,
    account: str | None = None,
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
    take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT,
) -> list[dict]:
    """Create missing broker-side fixed SL/TP OCA orders for an open option position."""
    qty = int(abs(float(pos.get("quantity", 0) or 0)))
    entry_price = float(pos.get("entry_price", 0) or 0)
    if qty <= 0 or entry_price <= 0:
        return []

    contract = reconstruct_option_contract(pos)
    try:
        qualified = ib.qualifyContracts(contract)
        if qualified:
            contract = qualified[0]
            pos["con_id"] = getattr(contract, "conId", pos.get("con_id"))
    except Exception:
        pass

    stop_price = round(max(0.01, float(pos.get("current_stop_price") or entry_price * (1 - float(stop_loss_pct) / 100))), 2)
    take_profit_price = round(max(0.01, float(pos.get("take_profit_price") or entry_price * (1 + float(take_profit_pct) / 100))), 2)
    pos["current_stop_price"] = stop_price
    pos["take_profit_price"] = take_profit_price

    open_sell_trades = _open_sell_trades_for_position(ib, pos)
    chunks = _order_chunks(qty)
    want_take_profit = not bool(pos.get("trailing_active", False))
    if len(chunks) > 1:
        existing_stop_ids = []
        existing_take_profit_ids = []
        for trade in open_sell_trades:
            order = getattr(trade, "order", None)
            order_type = str(getattr(order, "orderType", "") or "").upper()
            if "STP" in order_type:
                existing_stop_ids.append(_order_id(order))
            elif order_type == "LMT":
                existing_take_profit_ids.append(_order_id(order))
        existing_stop_ids = [order_id for order_id in existing_stop_ids if order_id]
        existing_take_profit_ids = [order_id for order_id in existing_take_profit_ids if order_id]
        if existing_stop_ids and (existing_take_profit_ids or not want_take_profit):
            pos["ibkr_stop_order_ids"] = existing_stop_ids
            pos["ibkr_take_profit_order_ids"] = existing_take_profit_ids
            pos["protective_orders_status"] = "Already protected"
            return []

        events = []
        stop_ids = []
        take_profit_ids = []
        base_group = str(pos.get("ibkr_oca_group") or f"PulseProtect-{pos.get('id') or pos.get('con_id')}-{int(time.time())}")
        pos["ibkr_oca_group"] = base_group
        for idx, chunk_qty in enumerate(chunks, start=1):
            oca_group = f"{base_group}-{idx}"
            stop_order = StopOrder("SELL", chunk_qty, stop_price)
            stop_order.ocaGroup = oca_group
            stop_order.ocaType = 1
            stop_order.tif = "DAY"
            if account:
                stop_order.account = account
            stop_trade = ib.placeOrder(contract, stop_order)
            ib.sleep(0.2)
            stop_ids.append(_order_id(getattr(stop_trade, "order", None)))
            events.append({"Symbol": pos.get("symbol"), "Option": pos.get("option"), "Action": "PROTECT_STOP", "Stop": stop_price, "Qty": chunk_qty})

            if want_take_profit:
                target_order = LimitOrder("SELL", chunk_qty, take_profit_price)
                target_order.ocaGroup = oca_group
                target_order.ocaType = 1
                target_order.tif = "DAY"
                if account:
                    target_order.account = account
                target_trade = ib.placeOrder(contract, target_order)
                ib.sleep(0.2)
                take_profit_ids.append(_order_id(getattr(target_trade, "order", None)))
                events.append({"Symbol": pos.get("symbol"), "Option": pos.get("option"), "Action": "PROTECT_TP", "TP": take_profit_price, "Qty": chunk_qty})

        pos["ibkr_stop_order_ids"] = [order_id for order_id in stop_ids if order_id]
        pos["ibkr_take_profit_order_ids"] = [order_id for order_id in take_profit_ids if order_id]
        pos["protective_orders_status"] = "Submitted" if events else "Already protected"
        return events

    existing_order_ids = {_order_id(getattr(trade, "order", None)) for trade in open_sell_trades}
    existing_order_ids.discard(None)

    stop_order_id = _coerce_order_id(pos.get("ibkr_stop_order_id"))
    take_profit_order_id = _coerce_order_id(pos.get("ibkr_take_profit_order_id"))
    has_stop = bool(stop_order_id and stop_order_id in existing_order_ids)
    has_take_profit = bool(take_profit_order_id and take_profit_order_id in existing_order_ids)
    stop_trade = None

    for trade in open_sell_trades:
        order = getattr(trade, "order", None)
        order_type = str(getattr(order, "orderType", "") or "").upper()
        if "STP" in order_type and not has_stop:
            pos["ibkr_stop_order_id"] = _order_id(order)
            has_stop = True
            stop_trade = trade
        elif order_type == "LMT" and not has_take_profit:
            pos["ibkr_take_profit_order_id"] = _order_id(order)
            has_take_profit = True
        elif "STP" in order_type and _order_id(order) == stop_order_id:
            stop_trade = trade

    events = []
    oca_group = str(pos.get("ibkr_oca_group") or f"PulseProtect-{pos.get('id') or pos.get('con_id')}-{int(time.time())}")
    pos["ibkr_oca_group"] = oca_group

    if not want_take_profit:
        for trade in open_sell_trades:
            order = getattr(trade, "order", None)
            order_type = str(getattr(order, "orderType", "") or "").upper()
            order_id = _order_id(order)
            if order_type != "LMT":
                continue
            if take_profit_order_id and order_id != take_profit_order_id:
                continue
            try:
                ib.cancelOrder(order)
                ib.sleep(0.2)
                events.append({"Symbol": pos.get("symbol"), "Option": pos.get("option"), "Action": "CANCEL_TP_FOR_TRAIL", "Order ID": order_id})
            except Exception as exc:
                events.append({"Symbol": pos.get("symbol"), "Option": pos.get("option"), "Action": "CANCEL_TP_FAILED", "Status": str(exc)})
        pos["ibkr_take_profit_order_id"] = None
        has_take_profit = True

    if not has_stop:
        stop_order = StopOrder("SELL", qty, stop_price)
        stop_order.ocaGroup = oca_group
        stop_order.ocaType = 1
        stop_order.tif = "DAY"
        if account:
            stop_order.account = account
        trade = ib.placeOrder(contract, stop_order)
        ib.sleep(0.2)
        pos["ibkr_stop_order_id"] = _order_id(getattr(trade, "order", None))
        events.append({"Symbol": pos.get("symbol"), "Option": pos.get("option"), "Action": "PROTECT_STOP", "Stop": stop_price})
    elif stop_trade is not None:
        stop_order = getattr(stop_trade, "order", None)
        old_stop = float(getattr(stop_order, "auxPrice", 0) or 0)
        if stop_price > old_stop + 0.009:
            stop_order.auxPrice = stop_price
            if account:
                stop_order.account = account
            ib.placeOrder(contract, stop_order)
            ib.sleep(0.2)
            events.append({"Symbol": pos.get("symbol"), "Option": pos.get("option"), "Action": "UPDATE_STOP", "Stop": stop_price})

    if want_take_profit and not has_take_profit:
        target_order = LimitOrder("SELL", qty, take_profit_price)
        target_order.ocaGroup = oca_group
        target_order.ocaType = 1
        target_order.tif = "DAY"
        if account:
            target_order.account = account
        trade = ib.placeOrder(contract, target_order)
        ib.sleep(0.2)
        pos["ibkr_take_profit_order_id"] = _order_id(getattr(trade, "order", None))
        events.append({"Symbol": pos.get("symbol"), "Option": pos.get("option"), "Action": "PROTECT_TP", "TP": take_profit_price})

    pos["protective_orders_status"] = "Submitted" if events else "Already protected"
    return events


def cancel_protective_orders(ib: IB, pos: dict) -> list[dict]:
    """Cancel local-position protective orders before Pulse submits its own exit."""
    wanted_ids = {
        _coerce_order_id(pos.get("ibkr_stop_order_id")),
        _coerce_order_id(pos.get("ibkr_take_profit_order_id")),
    }
    wanted_ids.update(_coerce_order_ids(pos.get("ibkr_stop_order_ids")))
    wanted_ids.update(_coerce_order_ids(pos.get("ibkr_take_profit_order_ids")))
    wanted_ids.discard(None)
    if not wanted_ids:
        return []

    events = []
    for trade in _open_sell_trades_for_position(ib, pos):
        order = getattr(trade, "order", None)
        if _order_id(order) not in wanted_ids:
            continue
        try:
            ib.cancelOrder(order)
            ib.sleep(0.2)
            events.append({"Symbol": pos.get("symbol"), "Option": pos.get("option"), "Action": "CANCEL_PROTECTION", "Order ID": _order_id(order)})
        except Exception as exc:
            events.append({"Symbol": pos.get("symbol"), "Option": pos.get("option"), "Action": "CANCEL_PROTECTION_FAILED", "Status": str(exc)})
    return events


def _position_matches_broker_position(pos: dict, broker_pos) -> bool:
    contract = getattr(broker_pos, "contract", None)
    if contract is None:
        return False

    pos_con_id = pos.get("con_id")
    broker_con_id = getattr(contract, "conId", None)
    if pos_con_id and broker_con_id and int(pos_con_id) == int(broker_con_id):
        return True

    right = "C" if str(pos.get("signal", "")).upper() == "CALL" else "P"
    return (
        str(getattr(contract, "symbol", "")).upper() == str(pos.get("symbol", "")).upper()
        and str(getattr(contract, "lastTradeDateOrContractMonth", "")) == str(pos.get("expiry", ""))
        and float(getattr(contract, "strike", 0) or 0) == float(pos.get("strike", 0) or 0)
        and str(getattr(contract, "right", "")).upper() == right
    )


def reconcile_active_positions_with_broker(ib: IB, account: str | None = None, log_closures: bool = True) -> list[dict]:
    """Remove or resize bot-managed positions that no longer exist at IBKR.

    This protects against duplicate exits when a user manually closes a bot
    position from TWS/IBKR before the dashboard or engine sees it.
    """
    local_positions = read_active_positions()
    if not local_positions:
        return []

    broker_positions = []
    for broker_pos in ib.positions():
        if account and getattr(broker_pos, "account", None) != account:
            continue
        try:
            if float(getattr(broker_pos, "position", 0) or 0) != 0:
                broker_positions.append(broker_pos)
        except Exception:
            continue

    reconciled = []
    events = []
    for pos in local_positions:
        entry_status = str(pos.get("entry_status", ""))
        if entry_status and not is_filled_order_status(entry_status):
            events.append({
                "Symbol": pos.get("symbol"),
                "Option": pos.get("option"),
                "Action": "REMOVED",
                "Reason": "Entry order was not filled",
                "Status": entry_status,
            })
            continue

        matches = [bp for bp in broker_positions if _position_matches_broker_position(pos, bp)]
        broker_qty = sum(float(getattr(bp, "position", 0) or 0) for bp in matches)
        local_qty = int(pos.get("quantity", 0) or 0)

        if broker_qty <= 0:
            events.append({
                "Symbol": pos.get("symbol"),
                "Option": pos.get("option"),
                "Action": "REMOVED",
                "Reason": "Position not found at IBKR",
                "Status": "Closed manually or no broker position exists",
            })
            if log_closures:
                log_trade({
                    "timestamp": datetime.now(EASTERN).isoformat(),
                    "event": "EXTERNAL_CLOSE",
                    "symbol": pos.get("symbol"),
                    "signal": pos.get("signal"),
                    "option": pos.get("option"),
                    "quantity": local_qty,
                    "exit_reason": "Position not found at IBKR",
                    "entry_price": pos.get("entry_price"),
                    "exit_price": None,
                    "realized_pnl": 0.0,
                    "status": "Closed manually or removed during broker reconciliation",
                })
            continue

        if local_qty > 0 and broker_qty < local_qty:
            pos["quantity"] = int(abs(broker_qty))
            events.append({
                "Symbol": pos.get("symbol"),
                "Option": pos.get("option"),
                "Action": "RESIZED",
                "Reason": "IBKR position quantity is lower than bot record",
                "Local Qty": local_qty,
                "Broker Qty": int(abs(broker_qty)),
            })
        reconciled.append(pos)

    if len(reconciled) != len(local_positions) or events:
        write_active_positions(reconciled)
    return events


def submit_exit_order(
    ib: IB,
    pos: dict,
    exit_price: float | None,
    reason: str,
    account: str | None = None,
    use_market: bool = True,
):
    contract = reconstruct_option_contract(pos)
    try:
        qualified = ib.qualifyContracts(contract)
        if qualified:
            contract = qualified[0]
    except Exception:
        pass

    qty = int(pos.get("quantity", 0))
    if qty <= 0:
        raise ValueError("No quantity to exit")

    cancel_protective_orders(ib, pos)
    trades = []
    for chunk_qty in _order_chunks(qty):
        order = MarketOrder("SELL", chunk_qty) if use_market else LimitOrder("SELL", chunk_qty, round(float(exit_price), 2))
        if account:
            order.account = account
        trade = ib.placeOrder(contract, order)
        trades.append(trade)
        ib.sleep(1)

    entry_price = float(pos.get("entry_price", 0))
    mark = float(exit_price or entry_price)
    realized_pnl = round((mark - entry_price) * qty * 100, 2)

    log_trade({
        "timestamp": datetime.now(EASTERN).isoformat(),
        "event": "EXIT",
        "symbol": pos.get("symbol"),
        "signal": pos.get("signal"),
        "option": pos.get("option"),
        "quantity": qty,
        "exit_reason": reason,
        "entry_price": entry_price,
        "exit_price": round(mark, 2),
        "realized_pnl": realized_pnl,
        "status": ", ".join(str(getattr(trade.orderStatus, "status", "")) for trade in trades),
    })
    return trades[-1], realized_pnl


def add_active_position_from_entry(
    row: dict,
    option_full: dict,
    qty: int,
    entry_price: float,
    trade_status: str,
    ib: IB | None = None,
    account: str | None = None,
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
    take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT,
):
    if not is_filled_order_status(trade_status):
        return None

    contract = option_full["Contract"]
    positions = read_active_positions()
    position_id = f"{row['Symbol']}-{option_full['Expiry']}-{option_full['Strike']}-{option_full['Type']}-{datetime.now(EASTERN).strftime('%Y%m%d%H%M%S')}"
    position = {
        "id": position_id,
        "symbol": row["Symbol"],
        "signal": row["Signal"],
        "option": option_full["Option"],
        "expiry": option_full["Expiry"],
        "strike": float(option_full["Strike"]),
        "con_id": getattr(contract, "conId", None),
        "quantity": int(qty),
        "entry_price": round(float(entry_price), 2),
        "current_stop_price": round(float(entry_price) * (1 - float(stop_loss_pct) / 100), 2),
        "take_profit_price": round(float(entry_price) * (1 + float(take_profit_pct) / 100), 2),
        "highest_price": round(float(entry_price), 2),
        "breakeven_active": False,
        "trailing_active": False,
        "entry_time": datetime.now(EASTERN).isoformat(),
        "entry_status": trade_status,
    }
    if ib is not None:
        ensure_protective_orders(
            ib,
            position,
            account=account,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
        )
    positions.append(position)
    write_active_positions(positions)
    return position


def manage_open_positions(
    ib: IB,
    account: str | None,
    stop_loss_pct: float,
    take_profit_pct: float,
    breakeven_trigger_pct: float,
    trailing_trigger_pct: float,
    trailing_stop_pct: float,
    force_exit_time: dtime,
    allow_live_orders: bool,
) -> list[dict]:
    """Manage active option positions across Streamlit refresh cycles."""
    positions = read_active_positions()
    if not positions:
        return []

    reconciliation_events = reconcile_active_positions_with_broker(ib, account=account, log_closures=True)
    positions = read_active_positions()
    if not positions:
        return reconciliation_events

    now_et = datetime.now(EASTERN)
    still_active = []
    events = list(reconciliation_events)

    for pos in positions:
        try:
            contract = reconstruct_option_contract(pos)
            qualified = ib.qualifyContracts(contract)
            if qualified:
                contract = qualified[0]
                pos["con_id"] = getattr(contract, "conId", pos.get("con_id"))
            if allow_live_orders:
                protection_events = ensure_protective_orders(
                    ib,
                    pos,
                    account=account,
                    stop_loss_pct=stop_loss_pct,
                    take_profit_pct=take_profit_pct,
                )
                events.extend(protection_events)
            market = get_snapshot_mid(ib, contract)
            current_price = market.get("Mid")
            if current_price is None or np.isnan(current_price) or current_price <= 0:
                still_active.append(pos)
                events.append({"Symbol": pos.get("symbol"), "Status": "No option price available", "Action": "Hold"})
                continue

            current_price = float(current_price)
            entry_price = float(pos["entry_price"])
            pnl_pct = ((current_price - entry_price) / entry_price) * 100
            highest = max(float(pos.get("highest_price", entry_price)), current_price)
            pos["highest_price"] = round(highest, 2)

            stop_price = float(pos.get("current_stop_price", entry_price * (1 - stop_loss_pct / 100)))
            take_profit_price = float(pos.get("take_profit_price", entry_price * (1 + take_profit_pct / 100)))
            reason = None

            # Force flat before close. This exits winners and losers that did not hit TP/SL.
            if now_et.time() >= force_exit_time:
                reason = "End-of-day forced exit"

            # Initial hard stop.
            elif current_price <= stop_price:
                reason = "Stop loss hit"

            # At +15%, move stop to breakeven.
            elif pnl_pct >= breakeven_trigger_pct and not bool(pos.get("breakeven_active", False)):
                pos["current_stop_price"] = round(entry_price, 2)
                pos["breakeven_active"] = True
                stop_price = entry_price

            # At +25%, activate trailing stop and cancel the fixed TP behavior.
            if reason is None and pnl_pct >= trailing_trigger_pct:
                pos["trailing_active"] = True
                trail_stop = highest * (1 - trailing_stop_pct / 100)
                pos["current_stop_price"] = round(max(float(pos.get("current_stop_price", stop_price)), trail_stop), 2)
                stop_price = float(pos["current_stop_price"])

            if reason is None and allow_live_orders:
                protection_events = ensure_protective_orders(
                    ib,
                    pos,
                    account=account,
                    stop_loss_pct=stop_loss_pct,
                    take_profit_pct=take_profit_pct,
                )
                events.extend(protection_events)

            # Before trailing starts, +30% fixed take-profit is active.
            if reason is None and not bool(pos.get("trailing_active", False)) and current_price >= take_profit_price:
                reason = "Take profit hit"

            # Once trailing is active, exit only on trailing stop or EOD.
            if reason is None and bool(pos.get("trailing_active", False)) and current_price <= stop_price:
                reason = "Trailing stop hit"

            if reason:
                if allow_live_orders:
                    trade, realized = submit_exit_order(ib, pos, current_price, reason, account=account, use_market=True)
                    status = str(trade.orderStatus.status)
                else:
                    realized = round((current_price - entry_price) * int(pos.get("quantity", 0)) * 100, 2)
                    status = "Exit signal only / trading disabled"
                events.append({
                    "Symbol": pos.get("symbol"),
                    "Option": pos.get("option"),
                    "Action": "EXIT",
                    "Reason": reason,
                    "Entry": entry_price,
                    "Current": round(current_price, 2),
                    "P/L %": round(pnl_pct, 1),
                    "P/L $": realized,
                    "Status": status,
                })
            else:
                still_active.append(pos)
                events.append({
                    "Symbol": pos.get("symbol"),
                    "Option": pos.get("option"),
                    "Action": "HOLD",
                    "Entry": entry_price,
                    "Current": round(current_price, 2),
                    "P/L %": round(pnl_pct, 1),
                    "Stop": round(float(pos.get("current_stop_price", stop_price)), 2),
                    "TP": round(take_profit_price, 2),
                    "BE Active": bool(pos.get("breakeven_active", False)),
                    "Trailing Active": bool(pos.get("trailing_active", False)),
                })
        except Exception as e:
            still_active.append(pos)
            events.append({"Symbol": pos.get("symbol"), "Action": "ERROR", "Status": str(e)})

    write_active_positions(still_active)
    return events


# =========================
# CHART
# =========================

def make_chart(symbol: str, breakdown: dict):
    df = breakdown["Intraday Data"].copy()
    intraday_dates = sorted(set(df.index.date))
    if len(intraday_dates) >= 2:
        keep_from = intraday_dates[-2]
        df = df[df.index.date >= keep_from]

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=df.index,
        open=df["Open"],
        high=df["High"],
        low=df["Low"],
        close=df["Close"],
        name=symbol,
    ))
    fig.add_trace(go.Scatter(x=df.index, y=df["VWAP"], mode="lines", name="VWAP"))
    fig.add_hline(y=breakdown["ORB High"], line_dash="dash", line_color="yellow", annotation_text="ORB High")
    fig.add_hline(y=breakdown["ORB Low"], line_dash="dash", line_color="yellow", annotation_text="ORB Low")
    fig.add_hline(y=breakdown["PDH"], line_dash="dot", line_color="red", annotation_text="PDH")
    fig.add_hline(y=breakdown["PDL"], line_dash="dot", line_color="red", annotation_text="PDL")
    if breakdown.get("Nearest Support"):
        fig.add_hline(y=breakdown["Nearest Support"], line_dash="dot", line_color="green", annotation_text="Support")
    if breakdown.get("Nearest Resistance"):
        fig.add_hline(y=breakdown["Nearest Resistance"], line_dash="dot", line_color="orange", annotation_text="Resistance")
    fig.update_layout(height=600, xaxis_rangeslider_visible=False, title=f"{symbol} 5m Chart")
    fig.update_xaxes(rangebreaks=[dict(bounds=[16, 9.5], pattern="hour"), dict(bounds=["sat", "mon"])])
    return fig



def summarize_trade_log() -> dict:
    """Return simple performance summary from exports/trade_log.csv."""
    if not os.path.exists(TRADE_LOG_FILE):
        return {
            "total_entries": 0,
            "total_exits": 0,
            "realized_pnl": 0.0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
        }
    try:
        df = pd.read_csv(TRADE_LOG_FILE)
        if df.empty:
            return {
                "total_entries": 0,
                "total_exits": 0,
                "realized_pnl": 0.0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
            }
        events = df.get("event", pd.Series([], dtype=str)).fillna("").astype(str)
        exits = df[events == "EXIT"].copy()
        pnl = pd.to_numeric(exits.get("realized_pnl", 0), errors="coerce").fillna(0)
        wins = int((pnl > 0).sum())
        losses = int((pnl < 0).sum())
        total_exits = int(len(exits))
        return {
            "total_entries": int((events == "ENTRY").sum()),
            "total_exits": total_exits,
            "realized_pnl": round(float(pnl.sum()), 2),
            "wins": wins,
            "losses": losses,
            "win_rate": round((wins / total_exits * 100), 1) if total_exits else 0.0,
        }
    except Exception:
        return {
            "total_entries": 0,
            "total_exits": 0,
            "realized_pnl": 0.0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
        }


def make_equity_curve_chart():
    """Build a simple realized P/L equity curve from closed trades."""
    if not os.path.exists(TRADE_LOG_FILE):
        return None
    try:
        df = pd.read_csv(TRADE_LOG_FILE)
        if df.empty or "event" not in df.columns or "realized_pnl" not in df.columns:
            return None
        df = df[df["event"].astype(str) == "EXIT"].copy()
        if df.empty:
            return None
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
        df["realized_pnl"] = pd.to_numeric(df["realized_pnl"], errors="coerce").fillna(0)
        df["cumulative_pnl"] = df["realized_pnl"].cumsum()
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["cumulative_pnl"], mode="lines+markers", name="Cumulative P/L"))
        fig.update_layout(height=420, title="Realized P/L Equity Curve", xaxis_title="Time", yaxis_title="Realized P/L USD")
        return fig
    except Exception:
        return None


def make_trade_pnl_bar_chart():
    """Build a per-trade realized P/L bar chart from closed trades."""
    if not os.path.exists(TRADE_LOG_FILE):
        return None
    try:
        df = pd.read_csv(TRADE_LOG_FILE)
        if df.empty or "event" not in df.columns or "realized_pnl" not in df.columns:
            return None
        df = df[df["event"].astype(str) == "EXIT"].copy()
        if df.empty:
            return None
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
        df["realized_pnl"] = pd.to_numeric(df["realized_pnl"], errors="coerce").fillna(0)
        labels = df.get("symbol", pd.Series(range(len(df)))).astype(str) + " " + df["timestamp"].dt.strftime("%m-%d %H:%M")
        fig = go.Figure()
        fig.add_trace(go.Bar(x=labels, y=df["realized_pnl"], name="Trade P/L"))
        fig.update_layout(height=420, title="Closed Trade P/L", xaxis_title="Trade", yaxis_title="P/L USD")
        return fig
    except Exception:
        return None

# =========================
# UI HELPERS
# =========================

def signal_badge(signal: str) -> str:
    if signal == "CALL":
        return "🟢 CALL"
    if signal == "PUT":
        return "🔴 PUT"
    return "⚪ WAIT"


def is_top_candidate(
    result: dict,
    min_score: float,
    min_confidence: float,
    min_rvol: float,
    min_atr: float,
    use_rvol_filter: bool,
) -> bool:
    """Filter live candidates.

    PMB v2 uses Score + Grade as the trade-quality filter. The min_confidence
    argument remains in the function signature so older dashboard/engine calls
    do not break, but it is intentionally ignored.
    """
    if not result or result.get("Signal") not in ["CALL", "PUT"]:
        return False
    try:
        if float(result.get("Score", 0)) < float(min_score):
            return False
        if bool(use_rvol_filter) and float(result.get("RVOL", 0)) < float(min_rvol):
            return False
        if float(result.get("ATR %", 0)) < float(min_atr):
            return False
    except Exception:
        return False
    return True


def clean_for_table(result: dict) -> dict:
    row = result.copy()
    row.pop("Intraday Data", None)
    return row


def make_alert_message(result: dict, option: dict | None) -> str:
    text = (
        f"<b>{result['Symbol']} {result['Signal']} setup</b>\n"
        f"Score: {result['Score']} | Grade: {result.get('Grade', 'N/A')} | Quality: {result.get('Setup Quality', 'N/A')}\n"
        f"Price: ${result['Price']} | RVOL: {result['RVOL']} | ATR%: {result['ATR %']}\n"
        f"VWAP: {result['VWAP']} | ORB: {result['ORB High']} / {result['ORB Low']}\n"
        f"PDH/PDL: {result['PDH']} / {result['PDL']}\n"
        f"ORB confirmed: {result.get('ORB Confirmation Close', 'N/A')} at {result.get('ORB Confirmation Time', 'N/A')}\n"
        f"Support/Resistance: {result.get('Nearest Support', 'N/A')} / {result.get('Nearest Resistance', 'N/A')}\n"
        f"Why: {result['Reasons']}"
    )
    if option:
        text += (
            f"\n\n<b>Option</b>: {option['Option']}\n"
            f"Mid: ${option['Mid']} | Bid/Ask: {option['Bid']} / {option['Ask']}\n"
            f"Risk/Contract: ${option['Risk / Contract']}"
        )
    return text




# =========================
# PRODUCTION HELPERS
# =========================
from pathlib import Path as _Path
import logging as _logging

BASE_DIR = _Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
HEALTH_FILE = LOG_DIR / "health.json"
DAILY_LOG_FILE = LOG_DIR / f"{datetime.now(EASTERN).date()}.log"

# Override relative export files so dashboard and engine use the same folder everywhere.
EXPORT_DIR = str(BASE_DIR / "exports")
os.makedirs(EXPORT_DIR, exist_ok=True)
TRADE_LOG_FILE = os.path.join(EXPORT_DIR, "trade_log.csv")
ALERT_LOG_FILE = os.path.join(EXPORT_DIR, "alert_log.csv")
ACTIVE_POSITIONS_FILE = os.path.join(EXPORT_DIR, "active_positions.json")


def deep_merge(default: dict, override: dict) -> dict:
    out = dict(default)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def default_config() -> dict:
    return {
        "account_mode": "Simulation",
        "ib": {"host": "127.0.0.1", "paper_port": 7497, "live_port": 7496, "client_id": 11, "account": "", "readonly": False},
        "telegram": {"bot_token": "", "chat_id": "", "send_alerts": False},
        "automation": {"enabled": False, "place_orders": False, "confirm_order_risk": False, "require_trade_approval": False, "live_confirm_text": "", "scan_interval_seconds": 60, "market_timezone": "America/New_York", "scan_only_market_hours": True},
        "strategy": {"option_dte": 7, "min_score": 70, "min_confidence": 0, "use_rvol_filter": False, "min_rvol": 1.5, "use_rvol_score": False, "use_rvol_ranking": False, "min_atr": 0.3, "top_n_tickers": 2, "active_strategy": "pmb", "orb_minutes": 15, "first_signal_minutes": 20, "min_session_bars": 7},
        "risk": {"account_size": 1000, "use_ibkr_buying_power": False, "max_trades_per_day": 2, "max_spend_per_trade_pct": 20.0, "max_daily_capital_pct": 35.0, "max_spend_per_trade": 200, "max_daily_capital": 350, "max_contracts": 5, "entry_cutoff_hour": 11, "entry_cutoff_minute": 0, "stop_loss_pct": 20.0, "take_profit_pct": 30.0, "breakeven_trigger_pct": 15.0, "trailing_trigger_pct": 25.0, "trailing_stop_pct": 10.0, "force_exit_hour": 15, "force_exit_minute": 55, "max_consecutive_losses": 2, "max_daily_drawdown_pct": 5.0},
        "order": {"type": "LIMIT"},
        "watchlist": WATCHLIST,
        "dynamic_watchlist": {"enabled": False, "mode": "Manual", "max_symbols": 5, "refresh_hour": 9, "refresh_minute": 30, "source_universe": WATCHLIST},
        "ibkr_flex": {"token": "", "trade_query_id": "", "base_url": ""},
        "performance": {"original_deposited_capital": 2300.0},
    }


def load_config(path: str | _Path = CONFIG_PATH) -> dict:
    path = _Path(path)
    if not path.exists():
        cfg = default_config()
        save_config(cfg, path)
        return cfg
    try:
        with path.open("r", encoding="utf-8") as f:
            return deep_merge(default_config(), json.load(f))
    except Exception:
        return default_config()


def save_config(config: dict, path: str | _Path = CONFIG_PATH) -> None:
    path = _Path(path)
    with path.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def ib_port_from_config(config: dict) -> int:
    mode = config.get("account_mode", "Simulation")
    ib = config.get("ib", {})
    return int(ib.get("live_port", 7496) if mode == "Live" else ib.get("paper_port", 7497))


def orders_unlocked_from_config(config: dict) -> bool:
    """Return True only when all order-safety gates are open.

    Simulation never places IBKR orders.
    Paper requires automation, place-orders, and risk confirmation.
    Live requires the same gates plus the exact text phrase TRADE LIVE.
    """
    mode = config.get("account_mode", "Simulation")
    automation = config.get("automation", {})
    ib = config.get("ib", {})
    if mode not in ["Paper", "Live"]:
        return False
    if ib.get("readonly", False):
        return False
    if not automation.get("enabled", False):
        return False
    if not automation.get("place_orders", False):
        return False
    if not automation.get("confirm_order_risk", False):
        return False
    if mode == "Live" and str(automation.get("live_confirm_text", "")).strip().upper() != "TRADE LIVE":
        return False
    return True


def trading_status_from_config(config: dict) -> str:
    mode = config.get("account_mode", "Simulation")
    if mode == "Simulation":
        return "SIMULATION / NO ORDERS"
    if orders_unlocked_from_config(config):
        return f"{mode.upper()} ARMED"
    return f"{mode.upper()} SAFE / DISABLED"


def is_market_open_now(config: dict | None = None) -> bool:
    now = datetime.now(EASTERN)
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 0)


def write_health(**kwargs) -> None:
    data = read_health()
    data.update(kwargs)
    data["updated_at"] = datetime.now(EASTERN).isoformat()
    with HEALTH_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def read_health() -> dict:
    if not HEALTH_FILE.exists():
        return {}
    try:
        with HEALTH_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def app_log(message: str, level: str = "INFO") -> None:
    line = f"{datetime.now(EASTERN).isoformat()} | {level} | {message}"
    print(line, flush=True)
    with DAILY_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# =========================
# TRADE REPLAY / JOURNAL HELPERS
# =========================
TRADE_REPLAY_FILE = os.path.join(EXPORT_DIR, "trade_replay.csv")
TRADE_SNAPSHOTS_DIR = os.path.join(EXPORT_DIR, "trade_snapshots")
os.makedirs(TRADE_SNAPSHOTS_DIR, exist_ok=True)


def _safe_replay_value(value):
    """Convert pandas/numpy/contract objects into CSV-safe values."""
    try:
        if isinstance(value, (pd.Timestamp, datetime)):
            return value.isoformat()
        if isinstance(value, (np.integer,)):
            return int(value)
        if isinstance(value, (np.floating,)):
            return float(value)
        if isinstance(value, (np.bool_,)):
            return bool(value)
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, default=str)
        if hasattr(value, "conId"):
            return getattr(value, "conId", None)
        return value
    except Exception:
        return str(value)


def save_trade_replay(row: dict, option: dict | None = None, event: str = "SIGNAL", order_status: str = "", quantity: int = 0, entry_price: float | None = None, estimated_cost: float | None = None, notes: str = "") -> dict:
    """Persist the complete setup context for later review.

    This is intentionally independent from the broker order result. It records
    why the bot liked the setup: score, confidence, ORB/VWAP/PDH/PDL, option
    selected, risk sizing, and the status at that moment.
    """
    option = option or {}
    replay = {
        "timestamp": datetime.now(EASTERN).isoformat(),
        "event": event,
        "symbol": row.get("Symbol"),
        "signal": row.get("Signal"),
        "score": row.get("Score"),
        "confidence": row.get("Confidence"),
        "grade": row.get("Grade"),
        "setup_quality": row.get("Setup Quality"),
        "score_components": row.get("Score Components"),
        "bull_score": row.get("Bull Score"),
        "bear_score": row.get("Bear Score"),
        "rank_score": row.get("Rank Score"),
        "price": row.get("Price"),
        "rvol": row.get("RVOL"),
        "atr_pct": row.get("ATR %"),
        "vwap": row.get("VWAP"),
        "orb_high": row.get("ORB High"),
        "orb_low": row.get("ORB Low"),
        "orb_up": row.get("ORB Up"),
        "orb_down": row.get("ORB Down"),
        "orb_confirmation_close": row.get("ORB Confirmation Close"),
        "orb_confirmation_time": row.get("ORB Confirmation Time"),
        "pdh": row.get("PDH"),
        "pdl": row.get("PDL"),
        "pdh_break": row.get("PDH Break"),
        "pdl_break": row.get("PDL Break"),
        "above_vwap": row.get("Above VWAP"),
        "below_vwap": row.get("Below VWAP"),
        "ema_bullish": row.get("EMA Bullish"),
        "ema_bearish": row.get("EMA Bearish"),
        "nearest_support": row.get("Nearest Support"),
        "nearest_resistance": row.get("Nearest Resistance"),
        "reasons": row.get("Reasons"),
        "option": option.get("Option"),
        "expiry": option.get("Expiry"),
        "strike": option.get("Strike"),
        "type": option.get("Type"),
        "delta": option.get("Delta"),
        "bid": option.get("Bid"),
        "ask": option.get("Ask"),
        "mid": option.get("Mid"),
        "spread_pct": option.get("Spread %"),
        "option_score": option.get("Option Score"),
        "risk_per_contract": option.get("Risk / Contract"),
        "quantity": quantity,
        "entry_price": entry_price,
        "estimated_cost": estimated_cost,
        "order_status": order_status,
        "notes": notes,
    }
    replay = {k: _safe_replay_value(v) for k, v in replay.items()}
    df = pd.DataFrame([replay])
    exists = os.path.exists(TRADE_REPLAY_FILE)
    df.to_csv(TRADE_REPLAY_FILE, mode="a", index=False, header=not exists)

    try:
        replay_id = f"{replay.get('timestamp','')}_{replay.get('symbol','')}_{replay.get('signal','')}".replace(":", "-").replace("/", "-")
        snapshot_path = os.path.join(TRADE_SNAPSHOTS_DIR, f"{replay_id}.json")
        with open(snapshot_path, "w", encoding="utf-8") as f:
            json.dump(replay, f, indent=2, default=str)
        replay["snapshot_path"] = snapshot_path
    except Exception:
        pass
    return replay


def load_trade_replay() -> pd.DataFrame:
    if not os.path.exists(TRADE_REPLAY_FILE):
        return pd.DataFrame()
    try:
        df = pd.read_csv(TRADE_REPLAY_FILE)
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            df = df.dropna(subset=["timestamp"]).sort_values("timestamp", ascending=False)
        return df
    except Exception:
        return pd.DataFrame()


def setup_quality_label(score: float | int | None, confidence: float | int | None = None) -> str:
    """Return PMB v2 quality label from score only. Confidence is ignored."""
    try:
        score = float(score or 0)
        if score >= 95:
            return "A+ / Elite"
        if score >= 90:
            return "A / High quality"
        if score >= 85:
            return "A- / Strong"
        if score >= 80:
            return "B+ / Tradable"
        if score >= 75:
            return "B / Valid"
        if score >= 70:
            return "B- / Borderline"
        if score >= 60:
            return "C / Watch only"
        return "Ignore"
    except Exception:
        return "Watchlist"
    except Exception:
        return "Watchlist"
