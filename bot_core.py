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
import math
import time
import json
import sqlite3
import uuid
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests

from backtester.strategy import scan_dataframe

try:
    from ib_insync import (
        IB,
        Stock,
        Option,
        MarketOrder,
        LimitOrder,
        StopOrder,
        util,
    )
    IB_AVAILABLE = True
except Exception:
    IB_AVAILABLE = False


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
        raise RuntimeError("ib_insync is not installed. Run: pip install ib-insync")

    ib = IB()
    ib.connect(host, port, clientId=client_id, readonly=readonly, timeout=10)
    return ib


def connect_ib(cfg: IBConfig):
    return get_ib_connection(cfg.host, cfg.port, cfg.client_id, cfg.readonly)


def qualify_stock(ib: IB, symbol: str):
    contract = Stock(symbol, "SMART", "USD")
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        raise RuntimeError(f"Could not qualify stock contract for {symbol}")
    return qualified[0]


def fetch_ib_intraday(ib: IB, symbol: str, duration="5 D", bar_size="5 mins") -> pd.DataFrame:
    contract = qualify_stock(ib, symbol)
    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting=bar_size,
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1,
        keepUpToDate=False,
    )

    if not bars:
        return pd.DataFrame()

    df = util.df(bars)
    if df.empty:
        return df

    df = df.rename(columns={
        "date": "Datetime",
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    })
    df["Datetime"] = pd.to_datetime(df["Datetime"])

    if df["Datetime"].dt.tz is None:
        df["Datetime"] = df["Datetime"].dt.tz_localize(EASTERN)
    else:
        df["Datetime"] = df["Datetime"].dt.tz_convert(EASTERN)

    df = df.set_index("Datetime")
    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()


def fetch_ib_daily(ib: IB, symbol: str, duration="20 D") -> pd.DataFrame:
    contract = qualify_stock(ib, symbol)
    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1,
        keepUpToDate=False,
    )

    if not bars:
        return pd.DataFrame()

    df = util.df(bars)
    if df.empty:
        return df

    df = df.rename(columns={
        "date": "Date",
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    })
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date")
    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()


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
    if daily.empty:
        return None

    daily_dates = pd.Series(pd.to_datetime(daily.index).date, index=daily.index)
    prior = daily[daily_dates < today_date]
    if prior.empty:
        return None
    return prior.index[-1]


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

def scan_symbol_ib(ib: IB, symbol: str, use_rvol_score: bool = False) -> dict | None:
    """Live IBKR scanner wrapper.

    The trading logic itself lives in backtester.strategy.scan_dataframe so the
    live bot and Yahoo backtester use the same decision engine.
    """
    intraday = fetch_ib_intraday(ib, symbol)
    daily = fetch_ib_daily(ib, symbol)
    result = scan_dataframe(
        symbol=symbol,
        intraday=intraday,
        daily=daily,
        use_rvol_score=use_rvol_score,
    )
    if result and result.get("PDH Method") == "daily":
        result["PDH Method"] = "IBKR daily"
    elif result and result.get("PDH Method") == "intraday fallback":
        result["PDH Method"] = "IBKR intraday fallback"
    return result


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
        mid = market["Mid"]

        if np.isnan(mid) or mid <= 0:
            continue

        spread_pct = market["Spread %"]
        if not np.isnan(spread_pct) and spread_pct > 0.25:
            continue

        option_rows.append({
            "Contract": contract,
            "Option": f"{symbol} {expiry} {strike:g} {option_type}",
            "Expiry": expiry,
            "Strike": strike,
            "Type": option_type,
            "Delta": round(est_delta, 2),
            "Bid": round(market["Bid"], 2) if not np.isnan(market["Bid"]) else None,
            "Ask": round(market["Ask"], 2) if not np.isnan(market["Ask"]) else None,
            "Last": round(market["Last"], 2) if not np.isnan(market["Last"]) else None,
            "Mid": round(mid, 2),
            "Spread %": round(spread_pct * 100, 1) if not np.isnan(spread_pct) else None,
            "Volume": int(market["Volume"] or 0),
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

        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
        today_et = datetime.now(EASTERN).date()
        today_df = df[df["timestamp"].dt.date == today_et]

        if today_df.empty:
            return 0, 0.0

        trade_count = len(today_df)
        deployed = 0.0

        if "estimated_cost" in today_df.columns:
            deployed = float(pd.to_numeric(today_df["estimated_cost"], errors="coerce").fillna(0).sum())
        elif {"quantity", "limit_price"}.issubset(today_df.columns):
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
        float(result.get("Score", 0)) * 0.35
        + float(result.get("Confidence", 0)) * 0.35
        + rvol_component
        + option_score * 0.20
        + atr_component,
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

    trade = ib.placeOrder(option_contract, order)
    ib.sleep(1)
    return trade


def log_trade(row: dict):
    """Write the legacy CSV trade log and mirror the event into SQLite journal."""
    df = pd.DataFrame([row])
    exists = os.path.exists(TRADE_LOG_FILE)
    df.to_csv(TRADE_LOG_FILE, mode="a", index=False, header=not exists)
    try:
        journal_log_event(row)
    except Exception as exc:
        try:
            app_log(f"Trade journal mirror failed: {exc}", "ERROR")
        except Exception:
            pass


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


def get_today_loss_stats() -> tuple[int, float]:
    """Return today's consecutive losses and realized P/L from the local trade log."""
    if not os.path.exists(TRADE_LOG_FILE):
        return 0, 0.0
    try:
        df = pd.read_csv(TRADE_LOG_FILE)
        if df.empty or "timestamp" not in df.columns:
            return 0, 0.0
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
        today = datetime.now(EASTERN).date()
        df = df[df["timestamp"].dt.date == today].copy()
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

    order = MarketOrder("SELL", qty) if use_market else LimitOrder("SELL", qty, round(float(exit_price), 2))
    if account:
        order.account = account
    trade = ib.placeOrder(contract, order)
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
        "status": str(trade.orderStatus.status),
    })
    return trade, realized_pnl


def add_active_position_from_entry(row: dict, option_full: dict, qty: int, entry_price: float, trade_status: str):
    contract = option_full["Contract"]
    positions = read_active_positions()
    position_id = f"{row['Symbol']}-{option_full['Expiry']}-{option_full['Strike']}-{option_full['Type']}-{datetime.now(EASTERN).strftime('%Y%m%d%H%M%S')}"
    positions.append({
        "id": position_id,
        "symbol": row["Symbol"],
        "signal": row["Signal"],
        "option": option_full["Option"],
        "expiry": option_full["Expiry"],
        "strike": float(option_full["Strike"]),
        "con_id": getattr(contract, "conId", None),
        "quantity": int(qty),
        "entry_price": round(float(entry_price), 2),
        "current_stop_price": round(float(entry_price) * 0.80, 2),
        "take_profit_price": round(float(entry_price) * 1.30, 2),
        "highest_price": round(float(entry_price), 2),
        "breakeven_active": False,
        "trailing_active": False,
        "entry_time": datetime.now(EASTERN).isoformat(),
        "entry_status": trade_status,
    })
    write_active_positions(positions)


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

    now_et = datetime.now(EASTERN)
    still_active = []
    events = []

    for pos in positions:
        try:
            contract = reconstruct_option_contract(pos)
            qualified = ib.qualifyContracts(contract)
            if qualified:
                contract = qualified[0]
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
                    log_trade({
                        "timestamp": datetime.now(EASTERN).isoformat(),
                        "event": "EXIT_SIGNAL",
                        "symbol": pos.get("symbol"),
                        "signal": pos.get("signal"),
                        "option": pos.get("option"),
                        "quantity": int(pos.get("quantity", 0)),
                        "exit_reason": reason,
                        "entry_price": entry_price,
                        "exit_price": round(current_price, 2),
                        "estimated_cost": round(entry_price * int(pos.get("quantity", 0)) * 100, 2),
                        "realized_pnl": realized,
                        "status": status,
                    })
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
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
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
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
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
    """Filter candidates using the sidebar controls."""
    if result["Signal"] not in ["CALL", "PUT"]:
        return False

    if float(result["Score"]) < min_score:
        return False
    if float(result["Confidence"]) < min_confidence:
        return False
    if use_rvol_filter and float(result["RVOL"]) < min_rvol:
        return False
    if float(result["ATR %"]) < min_atr:
        return False

    if result["Signal"] == "CALL":
        return float(result["Bull Score"]) >= min_score
    if result["Signal"] == "PUT":
        return float(result["Bear Score"]) >= min_score
    return False


def clean_for_table(result: dict) -> dict:
    row = result.copy()
    row.pop("Intraday Data", None)
    return row


def make_alert_message(result: dict, option: dict | None) -> str:
    text = (
        f"<b>{result['Symbol']} {result['Signal']} setup</b>\n"
        f"Score: {result['Score']} | Confidence: {result['Confidence']}\n"
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
DATABASE_DIR = BASE_DIR / "database"
DATABASE_DIR.mkdir(exist_ok=True)
TRADE_JOURNAL_DB = DATABASE_DIR / "trades.db"


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
        "automation": {"enabled": False, "place_orders": False, "confirm_order_risk": False, "live_confirm_text": "", "scan_interval_seconds": 60, "market_timezone": "America/New_York", "scan_only_market_hours": True},
        "strategy": {"option_dte": 7, "min_score": 70, "min_confidence": 75, "use_rvol_filter": False, "min_rvol": 1.5, "use_rvol_score": False, "use_rvol_ranking": False, "min_atr": 0.3, "top_n_tickers": 2},
        "risk": {"account_size": 1000, "max_trades_per_day": 2, "max_spend_per_trade": 250, "max_daily_capital": 500, "max_contracts": 2, "stop_loss_pct": 20.0, "take_profit_pct": 30.0, "breakeven_trigger_pct": 15.0, "trailing_trigger_pct": 25.0, "trailing_stop_pct": 10.0, "force_exit_hour": 15, "force_exit_minute": 55, "max_consecutive_losses": 2, "max_daily_drawdown_pct": 5.0},
        "order": {"type": "LIMIT"},
        "watchlist": WATCHLIST,
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
# SQLITE TRADE JOURNAL HELPERS
# =========================

def init_trade_journal_db() -> None:
    """Create the local SQLite journal database if it does not exist."""
    with sqlite3.connect(TRADE_JOURNAL_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY,
                opened_at TEXT,
                closed_at TEXT,
                status TEXT,
                account_mode TEXT,
                symbol TEXT,
                signal TEXT,
                option_symbol TEXT,
                expiry TEXT,
                strike REAL,
                option_type TEXT,
                quantity INTEGER,
                entry_price REAL,
                exit_price REAL,
                estimated_cost REAL,
                realized_pnl REAL DEFAULT 0,
                return_pct REAL DEFAULT 0,
                r_multiple REAL DEFAULT 0,
                score REAL,
                confidence REAL,
                rank_score REAL,
                bull_score REAL,
                bear_score REAL,
                price REAL,
                rvol REAL,
                atr_pct REAL,
                vwap REAL,
                orb_high REAL,
                orb_low REAL,
                pdh REAL,
                pdl REAL,
                nearest_support REAL,
                nearest_resistance REAL,
                reasons TEXT,
                exit_reason TEXT,
                ai_review_status TEXT DEFAULT 'Pending',
                ai_grade TEXT,
                ai_notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_events (
                event_id TEXT PRIMARY KEY,
                trade_id TEXT,
                timestamp TEXT,
                event TEXT,
                symbol TEXT,
                signal TEXT,
                option_symbol TEXT,
                quantity INTEGER,
                price REAL,
                estimated_cost REAL,
                realized_pnl REAL DEFAULT 0,
                status TEXT,
                notes TEXT,
                raw_json TEXT,
                FOREIGN KEY(trade_id) REFERENCES trades(trade_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_opened ON trades(opened_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_trade ON trade_events(trade_id)")
        conn.commit()


def _journal_now() -> str:
    return datetime.now(EASTERN).isoformat()


def _to_float(value, default: float | None = None):
    try:
        if value is None or value == "":
            return default
        if pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def _to_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        if pd.isna(value):
            return default
        return int(float(value))
    except Exception:
        return default


def _clean_key(row: dict, *keys, default=None):
    for key in keys:
        if key in row and row.get(key) not in [None, ""]:
            return row.get(key)
    return default


def _parse_option_fields(option_text: str | None, signal: str | None = None) -> dict:
    out = {"expiry": None, "strike": None, "option_type": signal}
    if not option_text:
        return out
    try:
        parts = str(option_text).split()
        if len(parts) >= 4:
            out["expiry"] = parts[1]
            out["strike"] = _to_float(parts[2])
            out["option_type"] = parts[3]
    except Exception:
        pass
    return out


def _find_open_trade_id(conn: sqlite3.Connection, row: dict) -> str | None:
    symbol = _clean_key(row, "symbol", "Symbol")
    option_symbol = _clean_key(row, "option", "Option")
    if not symbol:
        return None
    if option_symbol:
        found = conn.execute(
            """
            SELECT trade_id FROM trades
            WHERE status IN ('OPEN', 'ENTRY_SUBMITTED') AND symbol=? AND option_symbol=?
            ORDER BY opened_at DESC LIMIT 1
            """,
            (symbol, option_symbol),
        ).fetchone()
        if found:
            return found[0]
    found = conn.execute(
        """
        SELECT trade_id FROM trades
        WHERE status IN ('OPEN', 'ENTRY_SUBMITTED') AND symbol=?
        ORDER BY opened_at DESC LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    return found[0] if found else None


def _make_trade_id(row: dict) -> str:
    seed = "|".join(str(_clean_key(row, k, default="")) for k in ["timestamp", "symbol", "Symbol", "option", "Option", "event"])
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def journal_log_event(row: dict) -> str:
    """Insert/update a normalized trade journal record from an engine trade event."""
    init_trade_journal_db()
    row = dict(row or {})
    timestamp = str(_clean_key(row, "timestamp", default=_journal_now()))
    event = str(_clean_key(row, "event", default="EVENT")).upper()
    symbol = _clean_key(row, "symbol", "Symbol")
    signal = _clean_key(row, "signal", "Signal")
    option_symbol = _clean_key(row, "option", "Option")
    option_fields = _parse_option_fields(option_symbol, signal)
    quantity = _to_int(_clean_key(row, "quantity", "Qty"), 0)
    entry_price = _to_float(_clean_key(row, "entry_price", "limit_price", "Mid"))
    exit_price = _to_float(_clean_key(row, "exit_price"))
    estimated_cost = _to_float(_clean_key(row, "estimated_cost", "Estimated Cost"), 0.0)
    realized_pnl = _to_float(_clean_key(row, "realized_pnl"), 0.0) or 0.0
    status = str(_clean_key(row, "status", "order_status", default=""))
    notes = str(_clean_key(row, "notes", "exit_reason", default=""))

    with sqlite3.connect(TRADE_JOURNAL_DB) as conn:
        trade_id = _clean_key(row, "trade_id")
        if not trade_id and event.startswith("EXIT"):
            trade_id = _find_open_trade_id(conn, row)
        if not trade_id:
            trade_id = _make_trade_id({**row, "timestamp": timestamp})

        if event in ["ENTRY", "SIGNAL_ONLY", "SIGNAL", "ENTRY_SUBMITTED"]:
            trade_status = "OPEN" if event == "ENTRY" else "SIGNAL_ONLY"
            conn.execute(
                """
                INSERT INTO trades (
                    trade_id, opened_at, status, account_mode, symbol, signal, option_symbol,
                    expiry, strike, option_type, quantity, entry_price, estimated_cost,
                    score, confidence, rank_score, bull_score, bear_score, price, rvol, atr_pct,
                    vwap, orb_high, orb_low, pdh, pdl, nearest_support, nearest_resistance,
                    reasons, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    status=excluded.status,
                    quantity=COALESCE(excluded.quantity, trades.quantity),
                    entry_price=COALESCE(excluded.entry_price, trades.entry_price),
                    estimated_cost=COALESCE(excluded.estimated_cost, trades.estimated_cost),
                    updated_at=excluded.updated_at
                """,
                (
                    trade_id, timestamp, trade_status, _clean_key(row, "account_mode"), symbol, signal, option_symbol,
                    option_fields["expiry"], option_fields["strike"], option_fields["option_type"], quantity,
                    entry_price, estimated_cost, _to_float(_clean_key(row, "score", "Score")),
                    _to_float(_clean_key(row, "confidence", "Confidence")), _to_float(_clean_key(row, "rank_score", "Rank Score")),
                    _to_float(_clean_key(row, "bull_score", "Bull Score")), _to_float(_clean_key(row, "bear_score", "Bear Score")),
                    _to_float(_clean_key(row, "price", "Price")), _to_float(_clean_key(row, "rvol", "RVOL")),
                    _to_float(_clean_key(row, "atr_pct", "ATR %")), _to_float(_clean_key(row, "vwap", "VWAP")),
                    _to_float(_clean_key(row, "orb_high", "ORB High")), _to_float(_clean_key(row, "orb_low", "ORB Low")),
                    _to_float(_clean_key(row, "pdh", "PDH")), _to_float(_clean_key(row, "pdl", "PDL")),
                    _to_float(_clean_key(row, "nearest_support", "Nearest Support")), _to_float(_clean_key(row, "nearest_resistance", "Nearest Resistance")),
                    _clean_key(row, "reasons", "Reasons"), _journal_now(),
                ),
            )
        elif event.startswith("EXIT"):
            if not trade_id:
                trade_id = _make_trade_id({**row, "event": "UNMATCHED_EXIT"})
            existing = conn.execute("SELECT entry_price, quantity, estimated_cost FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
            entry_for_calc = _to_float(existing[0]) if existing else entry_price
            qty_for_calc = _to_int(existing[1]) if existing else quantity
            cost_for_calc = _to_float(existing[2], 0.0) if existing else estimated_cost
            return_pct = round((realized_pnl / cost_for_calc * 100), 2) if cost_for_calc else 0.0
            risk_dollars = abs((entry_for_calc or 0) * qty_for_calc * 100 * 0.20)
            r_multiple = round(realized_pnl / risk_dollars, 2) if risk_dollars else 0.0
            conn.execute(
                """
                UPDATE trades SET
                    closed_at=?, status='CLOSED', exit_price=?, realized_pnl=?, return_pct=?,
                    r_multiple=?, exit_reason=?, updated_at=?
                WHERE trade_id=?
                """,
                (timestamp, exit_price, realized_pnl, return_pct, r_multiple, _clean_key(row, "exit_reason", default=notes), _journal_now(), trade_id),
            )
            if conn.total_changes == 0:
                conn.execute(
                    """
                    INSERT INTO trades (trade_id, opened_at, closed_at, status, symbol, signal, option_symbol,
                    quantity, entry_price, exit_price, estimated_cost, realized_pnl, return_pct, r_multiple, exit_reason, updated_at)
                    VALUES (?, ?, ?, 'CLOSED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (trade_id, timestamp, timestamp, symbol, signal, option_symbol, quantity, entry_price, exit_price, estimated_cost, realized_pnl, return_pct, r_multiple, notes, _journal_now()),
                )

        event_price = exit_price if event.startswith("EXIT") else entry_price
        conn.execute(
            """
            INSERT INTO trade_events (event_id, trade_id, timestamp, event, symbol, signal, option_symbol,
            quantity, price, estimated_cost, realized_pnl, status, notes, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), trade_id, timestamp, event, symbol, signal, option_symbol, quantity,
             event_price, estimated_cost, realized_pnl, status, notes, json.dumps(row, default=str)),
        )
        conn.commit()
        return trade_id


def journal_log_replay_snapshot(replay: dict) -> str:
    """Store signal-only/entry setup snapshots in SQLite without replacing the CSV replay."""
    event = str(replay.get("event", "SIGNAL")).upper()
    if event in ["SIGNAL_ONLY", "SIGNAL", "ENTRY"]:
        return journal_log_event(replay)
    return ""


def load_trade_journal() -> pd.DataFrame:
    init_trade_journal_db()
    try:
        with sqlite3.connect(TRADE_JOURNAL_DB) as conn:
            df = pd.read_sql_query("SELECT * FROM trades ORDER BY opened_at DESC", conn)
        for col in ["opened_at", "closed_at", "created_at", "updated_at"]:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)
        return df
    except Exception:
        return pd.DataFrame()


def load_trade_events() -> pd.DataFrame:
    init_trade_journal_db()
    try:
        with sqlite3.connect(TRADE_JOURNAL_DB) as conn:
            df = pd.read_sql_query("SELECT * FROM trade_events ORDER BY timestamp DESC", conn)
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
        return df
    except Exception:
        return pd.DataFrame()


def migrate_csv_logs_to_journal() -> int:
    """One-click backfill from existing exports/trade_log.csv and trade_replay.csv."""
    init_trade_journal_db()
    imported = 0
    for path in [TRADE_LOG_FILE, TRADE_REPLAY_FILE if "TRADE_REPLAY_FILE" in globals() else None]:
        if not path or not os.path.exists(path):
            continue
        try:
            df = pd.read_csv(path)
            for _, row in df.iterrows():
                journal_log_event(row.dropna().to_dict())
                imported += 1
        except Exception as exc:
            try:
                app_log(f"CSV to journal migration skipped {path}: {exc}", "WARN")
            except Exception:
                pass
    return imported


def journal_db_path() -> str:
    init_trade_journal_db()
    return str(TRADE_JOURNAL_DB)

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
    try:
        journal_log_replay_snapshot(replay)
    except Exception as exc:
        try:
            app_log(f"Replay journal mirror failed: {exc}", "ERROR")
        except Exception:
            pass
    return replay


def load_trade_replay() -> pd.DataFrame:
    if not os.path.exists(TRADE_REPLAY_FILE):
        return pd.DataFrame()
    try:
        df = pd.read_csv(TRADE_REPLAY_FILE)
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
            df = df.dropna(subset=["timestamp"]).sort_values("timestamp", ascending=False)
        return df
    except Exception:
        return pd.DataFrame()


def setup_quality_label(score: float | int | None, confidence: float | int | None) -> str:
    try:
        score = float(score or 0)
        confidence = float(confidence or 0)
        if score >= 90 and confidence >= 80:
            return "A+ setup"
        if score >= 80 and confidence >= 70:
            return "A setup"
        if score >= 70 and confidence >= 60:
            return "B setup"
        return "Watchlist"
    except Exception:
        return "Watchlist"
