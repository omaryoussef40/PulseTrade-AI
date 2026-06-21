from __future__ import annotations

"""Shared scanner strategy for live IBKR scanning and Yahoo replay backtesting.

This module is intentionally broker/data-source agnostic. It accepts already-loaded
OHLCV DataFrames and returns the same signal dictionary used by the live scanner.

Inputs:
- intraday: 5m/15m/etc. bars with Open, High, Low, Close, Volume indexed by time.
- daily: optional daily bars. If daily is unavailable, PDH/PDL falls back to the
  previous complete intraday session, which is what the Yahoo replay currently uses.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

EASTERN = ZoneInfo("America/New_York")
REQUIRED_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]
MIN_SCORE = 70


def _normalize_ohlcv(df: pd.DataFrame | None, timezone: ZoneInfo = EASTERN) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=REQUIRED_COLUMNS)

    out = df.copy()

    # Flatten possible yfinance MultiIndex columns if they slipped through.
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = out.columns.get_level_values(0)

    rename = {str(c): str(c).strip().title() for c in out.columns}
    out = out.rename(columns=rename)

    missing = [c for c in REQUIRED_COLUMNS if c not in out.columns]
    if missing:
        return pd.DataFrame(columns=REQUIRED_COLUMNS)

    out = out[REQUIRED_COLUMNS].copy()
    out = out.apply(pd.to_numeric, errors="coerce")
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
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out[REQUIRED_COLUMNS]


def calculate_vwap(df: pd.DataFrame) -> pd.Series:
    session = pd.Series(df.index.date, index=df.index)
    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3
    dollar_volume = typical_price * df["Volume"]
    cumulative_dollar_volume = dollar_volume.groupby(session).cumsum()
    cumulative_volume = df["Volume"].groupby(session).cumsum()
    return cumulative_dollar_volume / cumulative_volume.replace(0, np.nan)


def calculate_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close = (df["Low"] - df["Close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.rolling(length).mean()


def get_valid_sessions(intraday: pd.DataFrame, min_bars: int = 50) -> list:
    dates = sorted(set(intraday.index.date))
    valid = []
    for session_date in dates:
        session_data = intraday[intraday.index.date == session_date]
        if len(session_data) >= min_bars:
            valid.append(session_date)
    return valid


def previous_trading_day_from_daily(daily: pd.DataFrame, today_date) -> pd.Timestamp | None:
    if daily is None or daily.empty:
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


def calculate_support_resistance(
    daily: pd.DataFrame,
    current_price: float,
    lookback: int = 30,
    pivot_window: int = 2,
) -> dict:
    if daily is None or daily.empty or current_price <= 0:
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


def scan_dataframe(
    symbol: str,
    intraday: pd.DataFrame,
    daily: pd.DataFrame | None = None,
    use_rvol_score: bool = False,
    min_score: float = MIN_SCORE,
    timezone: ZoneInfo = EASTERN,
) -> dict | None:
    """Run the PulseTrade scanner on provided historical bars.

    This is the single shared strategy function for:
    - IBKR live scanner, through bot_core.scan_symbol_ib
    - Yahoo replay/backtesting, through backtester.replay
    """
    symbol = str(symbol).strip().upper()
    intraday = _normalize_ohlcv(intraday, timezone=timezone)
    daily = _normalize_ohlcv(daily, timezone=timezone) if daily is not None else pd.DataFrame(columns=REQUIRED_COLUMNS)

    if intraday.empty or len(intraday) < 50:
        return None

    intraday = intraday.dropna().copy()
    daily = daily.dropna().copy() if daily is not None else pd.DataFrame(columns=REQUIRED_COLUMNS)

    intraday["VWAP"] = calculate_vwap(intraday)
    intraday["EMA9"] = intraday["Close"].ewm(span=9).mean()
    intraday["EMA21"] = intraday["Close"].ewm(span=21).mean()
    intraday["ATR"] = calculate_atr(intraday)
    intraday = intraday.dropna()

    valid_sessions = get_valid_sessions(intraday, min_bars=7)
    if not valid_sessions:
        return None

    today_date = valid_sessions[-1]
    today_data = intraday[intraday.index.date == today_date]

    # 30-minute ORB uses first 6 x 5-minute candles. The first valid scanner
    # check is after at least one full post-ORB candle has closed.
    if len(today_data) < 7:
        return None

    opening_range = today_data.iloc[:6]
    post_orb_data = today_data.iloc[6:]
    orb_high = float(opening_range["High"].max())
    orb_low = float(opening_range["Low"].min())

    confirmation_candle = post_orb_data.iloc[-1]
    confirmation_close = float(confirmation_candle["Close"])
    confirmation_time = confirmation_candle.name
    orb_confirmed_up = bool(confirmation_close > orb_high)
    orb_confirmed_down = bool(confirmation_close < orb_low)

    previous_daily_index = previous_trading_day_from_daily(daily, today_date)
    if previous_daily_index is not None:
        previous_day = daily.loc[previous_daily_index]
        pdh = float(previous_day["High"])
        pdl = float(previous_day["Low"])
        pdh_source = str(pd.to_datetime(previous_daily_index).date())
        pdh_method = "daily"
    else:
        valid_prior_sessions = [d for d in valid_sessions if d < today_date]
        if not valid_prior_sessions:
            return None
        previous_session_date = valid_prior_sessions[-1]
        previous_session = intraday[intraday.index.date == previous_session_date]
        pdh = float(previous_session["High"].max())
        pdl = float(previous_session["Low"].min())
        pdh_source = str(previous_session_date)
        pdh_method = "intraday fallback"

    rvol = calculate_rvol(intraday, today_date)
    last = intraday.iloc[-1]

    price = float(last["Close"])
    atr_percent = float((last["ATR"] / price) * 100) if price else 0.0

    above_vwap = bool(price > last["VWAP"])
    below_vwap = bool(price < last["VWAP"])
    ema_bullish = bool(last["EMA9"] > last["EMA21"])
    ema_bearish = bool(last["EMA9"] < last["EMA21"])
    orb_up = orb_confirmed_up
    orb_down = orb_confirmed_down
    pdh_break = bool(price > pdh)
    pdl_break = bool(price < pdl)

    bull_score = 0
    bear_score = 0
    reasons = []

    if above_vwap:
        bull_score += 20
        reasons.append("Above VWAP")
    if below_vwap:
        bear_score += 20
        reasons.append("Below VWAP")
    if ema_bullish:
        bull_score += 20
        reasons.append("EMA9 above EMA21")
    if ema_bearish:
        bear_score += 20
        reasons.append("EMA9 below EMA21")
    if orb_up:
        bull_score += 25
        reasons.append("ORB breakout up")
    if orb_down:
        bear_score += 25
        reasons.append("ORB breakdown")
    if pdh_break:
        bull_score += 20
        reasons.append("Broke PDH")
    if pdl_break:
        bear_score += 20
        reasons.append("Broke PDL")

    # RVOL is informational by default for your current strategy preferences.
    if use_rvol_score:
        if rvol >= 2.0:
            bull_score += 10
            bear_score += 10
            reasons.append(f"RVOL bonus: {rvol:.2f}")
        elif rvol >= 1.5:
            bull_score += 5
            bear_score += 5
            reasons.append(f"Light RVOL bonus: {rvol:.2f}")
    else:
        reasons.append(f"RVOL observed: {rvol:.2f}")

    if atr_percent >= 0.5:
        bull_score += 10
        bear_score += 10
    if atr_percent >= 1.0:
        bull_score += 10
        bear_score += 10
        reasons.append(f"Good ATR%: {atr_percent:.2f}")

    bull_score = min(bull_score, 100)
    bear_score = min(bear_score, 100)
    confidence = abs(bull_score - bear_score)

    if bull_score >= min_score and bull_score > bear_score:
        signal = "CALL"
    elif bear_score >= min_score and bear_score > bull_score:
        signal = "PUT"
    else:
        signal = "WAIT"

    score = max(bull_score, bear_score)
    sr_levels = calculate_support_resistance(daily, price)

    return {
        "Symbol": symbol,
        "Signal": signal,
        "Score": round(score, 1),
        "Confidence": round(confidence, 1),
        "Bull Score": round(bull_score, 1),
        "Bear Score": round(bear_score, 1),
        "Price": round(price, 2),
        "RVOL": round(rvol, 2),
        "ATR %": round(atr_percent, 2),
        "VWAP": round(float(last["VWAP"]), 2),
        "ORB High": round(orb_high, 2),
        "ORB Low": round(orb_low, 2),
        "PDH": round(pdh, 2),
        "PDL": round(pdl, 2),
        **sr_levels,
        "PDH Source": pdh_source,
        "PDH Method": pdh_method,
        "ORB Up": orb_up,
        "ORB Down": orb_down,
        "ORB Confirmation Close": round(confirmation_close, 2),
        "ORB Confirmation Time": confirmation_time.strftime("%Y-%m-%d %H:%M %Z") if hasattr(confirmation_time, "strftime") else str(confirmation_time),
        "PDH Break": pdh_break,
        "PDL Break": pdl_break,
        "Above VWAP": above_vwap,
        "Below VWAP": below_vwap,
        "EMA Bullish": ema_bullish,
        "EMA Bearish": ema_bearish,
        "Reasons": " | ".join(reasons),
        "Intraday Data": intraday,
    }


def scan_replay_history(
    symbol: str,
    history: pd.DataFrame,
    daily: pd.DataFrame | None = None,
    use_rvol_score: bool = False,
    min_score: float = MIN_SCORE,
) -> dict | None:
    """Convenience wrapper used by Yahoo replay events."""
    return scan_dataframe(
        symbol=symbol,
        intraday=history,
        daily=daily,
        use_rvol_score=use_rvol_score,
        min_score=min_score,
    )


def clean_signal_row(result: dict | None) -> dict | None:
    if not result:
        return None
    row = dict(result)
    row.pop("Intraday Data", None)
    return row
