from __future__ import annotations

"""PMB v2 — shared scanner strategy for live IBKR scanning and Yahoo replay.

PMB v2 removes Confidence as a trade blocker. The strategy now evaluates setup
quality directly using Score + Grade. Confidence is still returned only as a
compatibility/debug field so older engine/dashboard code does not break.
"""

from datetime import time as dtime
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd

EASTERN = ZoneInfo("America/New_York")
REQUIRED_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]
MIN_SCORE = 70
DEFAULT_ORB_MINUTES = 15
HUGE_ORB_ATR_MULTIPLE = 1.5
FOLLOW_THROUGH_VOLUME_RATIO = 0.30
MIDDAY_VOLUME_START = dtime(12, 0)
MIDDAY_VOLUME_RATIO = 0.40


def _normalize_ohlcv(df: pd.DataFrame | None, timezone: ZoneInfo = EASTERN) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=REQUIRED_COLUMNS)
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = out.columns.get_level_values(0)
    out = out.rename(columns={str(c): str(c).strip().title() for c in out.columns})
    missing = [c for c in REQUIRED_COLUMNS if c not in out.columns]
    if missing:
        return pd.DataFrame(columns=REQUIRED_COLUMNS)
    out = out[REQUIRED_COLUMNS].copy().apply(pd.to_numeric, errors="coerce")
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
    return out[~out.index.duplicated(keep="last")].sort_index()[REQUIRED_COLUMNS]


def _bar_minutes(index: pd.Index, fallback: int = 5) -> int:
    try:
        idx = pd.to_datetime(index)
        diffs = pd.Series(idx).diff().dropna().dt.total_seconds() / 60
        diffs = diffs[(diffs > 0) & (diffs <= 120)]
        if diffs.empty:
            return fallback
        return max(1, int(round(float(diffs.median()))))
    except Exception:
        return fallback


def _grade(score: float) -> str:
    score = float(score or 0)
    if score >= 95: return "A+"
    if score >= 90: return "A"
    if score >= 85: return "A-"
    if score >= 80: return "B+"
    if score >= 75: return "B"
    if score >= 70: return "B-"
    if score >= 60: return "C"
    return "Ignore"


def _quality(score: float) -> str:
    return {
        "A+": "Elite PMB setup",
        "A": "High-quality PMB setup",
        "A-": "Strong PMB setup",
        "B+": "Tradable PMB setup",
        "B": "Valid PMB setup",
        "B-": "Borderline PMB setup",
        "C": "Watch only",
        "Ignore": "No trade",
    }.get(_grade(score), "No trade")


def _distance_pct(price: float, level: float) -> float:
    if not price or not level:
        return 0.0
    return ((float(price) - float(level)) / float(price)) * 100.0


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(value)))


def _candle_range(candle: pd.Series) -> float:
    try:
        return max(float(candle["High"]) - float(candle["Low"]), 0.0)
    except Exception:
        return 0.0


def calculate_vwap(df: pd.DataFrame) -> pd.Series:
    session = pd.Series(df.index.date, index=df.index)
    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3
    dollar_volume = typical_price * df["Volume"]
    return dollar_volume.groupby(session).cumsum() / df["Volume"].groupby(session).cumsum().replace(0, np.nan)


def calculate_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close = (df["Low"] - df["Close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.rolling(length).mean()


def get_valid_sessions(intraday: pd.DataFrame, min_bars: int = 7) -> list:
    valid = []
    for session_date in sorted(set(intraday.index.date)):
        session = intraday[intraday.index.date == session_date]
        if len(session) >= int(min_bars) and float(session["Volume"].sum()) > 0:
            valid.append(session_date)
    return valid


def previous_trading_day_from_daily(daily: pd.DataFrame, today_date) -> pd.Timestamp | None:
    if daily is None or daily.empty:
        return None
    df = daily.dropna().copy()
    if "Volume" in df.columns:
        df = df[pd.to_numeric(df["Volume"], errors="coerce").fillna(0) > 0]
    if df.empty:
        return None
    daily_dates = pd.Series(pd.to_datetime(df.index).date, index=df.index)
    prior = df[daily_dates < today_date]
    if prior.empty:
        return None
    return prior.index[-1]


def calculate_rvol(intraday: pd.DataFrame, today_date) -> float:
    today_data = intraday[intraday.index.date == today_date]
    if today_data.empty:
        return 0.0
    current_time = today_data.index[-1].time()
    today_volume = float(today_data["Volume"].sum())
    previous_volumes = []
    for session_date in sorted(set(intraday.index.date)):
        if session_date == today_date:
            continue
        session = intraday[intraday.index.date == session_date]
        if len(session) >= 50:
            session_to_time = session[session.index.time <= current_time]
            if session_to_time.empty:
                continue
            vol = float(session_to_time["Volume"].sum())
            if vol > 0:
                previous_volumes.append(vol)
    return today_volume / (sum(previous_volumes) / len(previous_volumes)) if previous_volumes else 0.0


def calculate_support_resistance(daily: pd.DataFrame, current_price: float, lookback: int = 30, pivot_window: int = 2) -> dict:
    empty = {"Nearest Support": None, "Support Distance %": None, "Nearest Resistance": None, "Resistance Distance %": None}
    if daily is None or daily.empty or current_price <= 0:
        return empty
    df = daily.tail(lookback).copy()
    if len(df) < (pivot_window * 2 + 1):
        return empty
    supports, resistances = [], []
    for i in range(pivot_window, len(df) - pivot_window):
        window = df.iloc[i - pivot_window:i + pivot_window + 1]
        center = df.iloc[i]
        if float(center["Low"]) == float(window["Low"].min()):
            supports.append(float(center["Low"]))
        if float(center["High"]) == float(window["High"].max()):
            resistances.append(float(center["High"]))
    support_levels = sorted([x for x in supports if x < current_price], reverse=True)
    resistance_levels = sorted([x for x in resistances if x > current_price])
    support = support_levels[0] if support_levels else None
    resistance = resistance_levels[0] if resistance_levels else None
    return {
        "Nearest Support": round(support, 2) if support else None,
        "Support Distance %": round(((current_price - support) / current_price * 100), 2) if support else None,
        "Nearest Resistance": round(resistance, 2) if resistance else None,
        "Resistance Distance %": round(((resistance - current_price) / current_price * 100), 2) if resistance else None,
    }


def _score_direction(direction: str, price: float, vwap: float, ema9: float, ema21: float, orb_high: float, orb_low: float, pdh: float, pdl: float, atr_percent: float, rvol: float, candle: pd.Series, use_rvol_score: bool, orb_minutes: int = DEFAULT_ORB_MINUTES) -> tuple[float, list[str], list[str]]:
    direction = direction.upper()
    is_call = direction == "CALL"
    score = 0.0
    reasons: list[str] = []
    components: list[str] = []

    vwap_dist = _distance_pct(price, vwap)
    directional_vwap_dist = vwap_dist if is_call else -vwap_dist
    if directional_vwap_dist > 0:
        points = 8
        if directional_vwap_dist >= 0.35: points = 14
        if directional_vwap_dist >= 0.75: points = 20
        score += points
        reasons.append("Above VWAP" if is_call else "Below VWAP")
        components.append(f"VWAP {points}/20 ({directional_vwap_dist:.2f}%)")

    ema_spread_pct = abs(_distance_pct(ema9, ema21))
    ema_ok = ema9 > ema21 if is_call else ema9 < ema21
    if ema_ok:
        points = 8
        if ema_spread_pct >= 0.08: points = 14
        if ema_spread_pct >= 0.20: points = 20
        score += points
        reasons.append("EMA9 above EMA21" if is_call else "EMA9 below EMA21")
        components.append(f"EMA trend {points}/20 ({ema_spread_pct:.2f}%)")

    orb_level = orb_high if is_call else orb_low
    orb_dist = _distance_pct(price, orb_level)
    directional_orb_dist = orb_dist if is_call else -orb_dist
    if directional_orb_dist > 0:
        points = 15
        if directional_orb_dist >= 0.20: points = 20
        if directional_orb_dist >= 0.50: points = 25
        score += points
        reasons.append(f"{int(orb_minutes)}m ORB breakout up" if is_call else f"{int(orb_minutes)}m ORB breakdown")
        components.append(f"{int(orb_minutes)}m ORB {points}/25 ({directional_orb_dist:.2f}%)")

    key_level = pdh if is_call else pdl
    level_dist = _distance_pct(price, key_level)
    directional_level_dist = level_dist if is_call else -level_dist
    if directional_level_dist > 0:
        points = 15
        if directional_level_dist >= 0.15: points = 20
        if directional_level_dist >= 0.40: points = 25
        score += points
        reasons.append("Broke PDH" if is_call else "Broke PDL")
        components.append(f"{'PDH' if is_call else 'PDL'} break {points}/25 ({directional_level_dist:.2f}%)")

    high = float(candle.get("High", price))
    low = float(candle.get("Low", price))
    close = float(candle.get("Close", price))
    candle_range = max(high - low, 0.01)
    close_position = (close - low) / candle_range if is_call else (high - close) / candle_range
    confirmation_points = 0.0
    confirmation_components: list[str] = []
    if close_position >= 0.60:
        points = 2
        if close_position >= 0.75: points = 3
        if close_position >= 0.88: points = 4
        confirmation_points += points
        reasons.append("Strong breakout candle" if is_call else "Strong breakdown candle")
        confirmation_components.append(f"Candle {points}/4")

    if atr_percent >= 0.30:
        points = 2
        if atr_percent >= 0.50: points = 3
        if atr_percent >= 1.00: points = 4
        confirmation_points += points
        reasons.append(f"Good ATR%: {atr_percent:.2f}")
        confirmation_components.append(f"ATR {points}/4")

    if use_rvol_score:
        if rvol >= 2.0:
            confirmation_points += 2
            reasons.append(f"RVOL bonus: {rvol:.2f}")
            confirmation_components.append("RVOL 2/2")
        elif rvol >= 1.5:
            confirmation_points += 1
            reasons.append(f"Light RVOL bonus: {rvol:.2f}")
            confirmation_components.append("RVOL 1/2")
    else:
        reasons.append(f"RVOL observed: {rvol:.2f}")

    confirmation_points = min(10.0, confirmation_points)
    score += confirmation_points
    if confirmation_components:
        components.append(f"Confirmation {confirmation_points:g}/10 ({', '.join(confirmation_components)})")

    return _clamp(score), reasons, components


def scan_dataframe(
    symbol: str,
    intraday: pd.DataFrame,
    daily: pd.DataFrame | None = None,
    use_rvol_score: bool = False,
    min_score: float = MIN_SCORE,
    timezone: ZoneInfo = EASTERN,
    orb_minutes: int = DEFAULT_ORB_MINUTES,
    min_session_bars: int = 7,
) -> dict | None:
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
    if intraday.empty:
        return None

    valid_sessions = get_valid_sessions(intraday, min_bars=int(min_session_bars))
    if not valid_sessions:
        return None
    today_date = valid_sessions[-1]
    today_data = intraday[intraday.index.date == today_date]

    bar_minutes = _bar_minutes(today_data.index, fallback=5)
    orb_bars = max(1, int(round(float(orb_minutes) / max(bar_minutes, 1))))
    if len(today_data) < orb_bars + 1:
        return None

    opening_range = today_data.iloc[:orb_bars]
    post_orb_data = today_data.iloc[orb_bars:]
    orb_high = float(opening_range["High"].max())
    orb_low = float(opening_range["Low"].min())
    first_orb_candle = opening_range.iloc[0]
    second_orb_candle = post_orb_data.iloc[0]
    opening_range_size = max(orb_high - orb_low, _candle_range(first_orb_candle))
    opening_range_volume = float(opening_range["Volume"].sum())
    average_orb_bar_volume = opening_range_volume / max(len(opening_range), 1)
    second_orb_volume = float(second_orb_candle.get("Volume", 0) or 0)
    second_volume_ratio = second_orb_volume / average_orb_bar_volume if average_orb_bar_volume > 0 else 0.0
    second_candle_volume_ok = bool(second_volume_ratio >= FOLLOW_THROUGH_VOLUME_RATIO)
    second_orb_close = float(second_orb_candle["Close"])
    atr_at_orb = float(second_orb_candle.get("ATR", 0) or 0)
    huge_opening_range = bool(atr_at_orb > 0 and opening_range_size > atr_at_orb * HUGE_ORB_ATR_MULTIPLE)
    second_candle_continues_up = bool(second_orb_close > orb_high)
    second_candle_continues_down = bool(second_orb_close < orb_low)
    confirmation_candle = post_orb_data.iloc[-1]
    confirmation_close = float(confirmation_candle["Close"])
    confirmation_time = confirmation_candle.name
    current_candle_volume = float(confirmation_candle.get("Volume", 0) or 0)
    midday_volume_ratio = current_candle_volume / opening_range_volume if opening_range_volume > 0 else 0.0
    confirmation_clock = confirmation_time.time() if hasattr(confirmation_time, "time") else None
    midday_volume_check_active = bool(confirmation_clock and confirmation_clock >= MIDDAY_VOLUME_START)
    midday_volume_ok = bool((not midday_volume_check_active) or midday_volume_ratio >= MIDDAY_VOLUME_RATIO)
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

    call_score, call_reasons, call_components = _score_direction("CALL", price, float(last["VWAP"]), float(last["EMA9"]), float(last["EMA21"]), orb_high, orb_low, pdh, pdl, atr_percent, rvol, last, use_rvol_score, orb_minutes)
    put_score, put_reasons, put_components = _score_direction("PUT", price, float(last["VWAP"]), float(last["EMA9"]), float(last["EMA21"]), orb_high, orb_low, pdh, pdl, atr_percent, rvol, last, use_rvol_score, orb_minutes)

    if call_score >= float(min_score) and call_score > put_score:
        signal, score, reasons, components = "CALL", call_score, call_reasons, call_components
    elif put_score >= float(min_score) and put_score > call_score:
        signal, score, reasons, components = "PUT", put_score, put_reasons, put_components
    else:
        signal = "WAIT"
        if call_score >= put_score:
            score, reasons, components = call_score, call_reasons, call_components
        else:
            score, reasons, components = put_score, put_reasons, put_components

    opening_exhaustion_block = False

    midday_volume_block = False
    if signal in {"CALL", "PUT"} and not midday_volume_ok:
        midday_volume_block = True
        signal = "WAIT"
        reasons = list(reasons) + ["Midday volume too weak versus opening range"]
        components = list(components) + [f"Midday volume block (current {midday_volume_ratio:.2f}x opening range volume; need {MIDDAY_VOLUME_RATIO:.2f}x)"]

    confidence = 100.0 if signal in {"CALL", "PUT"} else abs(call_score - put_score)
    grade = _grade(score)
    above_vwap = bool(price > last["VWAP"])
    below_vwap = bool(price < last["VWAP"])
    ema_bullish = bool(last["EMA9"] > last["EMA21"])
    ema_bearish = bool(last["EMA9"] < last["EMA21"])
    pdh_break = bool(price > pdh)
    pdl_break = bool(price < pdl)
    sr_levels = calculate_support_resistance(daily, price)

    return {
        "Symbol": symbol,
        "Strategy": "PMB",
        "Strategy Version": "PMB v2",
        "Signal": signal,
        "Score": round(score, 1),
        "Grade": grade,
        "Setup Quality": _quality(score),
        "Score Components": " | ".join(components),
        "Confidence": round(confidence, 1),  # compatibility only
        "Bull Score": round(call_score, 1),
        "Bear Score": round(put_score, 1),
        "Price": round(price, 2),
        "RVOL": round(rvol, 2),
        "ATR %": round(atr_percent, 2),
        "VWAP": round(float(last["VWAP"]), 2),
        "ORB Minutes": int(orb_minutes),
        "ORB Bars": int(orb_bars),
        "ORB High": round(orb_high, 2),
        "ORB Low": round(orb_low, 2),
        "Opening Range Size": round(opening_range_size, 2),
        "Opening Range ATR Multiple": round(opening_range_size / atr_at_orb, 2) if atr_at_orb else None,
        "Opening Range Volume": round(opening_range_volume, 2),
        "Average ORB Bar Volume": round(average_orb_bar_volume, 2),
        "Second Candle Volume": round(second_orb_volume, 2),
        "Second Candle Volume Ratio": round(second_volume_ratio, 2),
        "Second Candle Volume OK": second_candle_volume_ok,
        "Huge Opening Range": huge_opening_range,
        "Second Candle Continuation Up": second_candle_continues_up,
        "Second Candle Continuation Down": second_candle_continues_down,
        "Opening Exhaustion Block": opening_exhaustion_block,
        "Midday Volume Check Active": midday_volume_check_active,
        "Current Candle Volume": round(current_candle_volume, 2),
        "Midday Volume Ratio": round(midday_volume_ratio, 2),
        "Midday Volume OK": midday_volume_ok,
        "Midday Volume Block": midday_volume_block,
        "PDH": round(pdh, 2),
        "PDL": round(pdl, 2),
        **sr_levels,
        "PDH Source": pdh_source,
        "PDH Method": pdh_method,
        "ORB Up": orb_confirmed_up,
        "ORB Down": orb_confirmed_down,
        "ORB Confirmation Close": round(confirmation_close, 2),
        "ORB Confirmation Time": confirmation_time.strftime("%Y-%m-%d %H:%M %Z") if hasattr(confirmation_time, "strftime") else str(confirmation_time),
        "PDH Break": pdh_break,
        "PDL Break": pdl_break,
        "Above VWAP": above_vwap,
        "Below VWAP": below_vwap,
        "EMA Bullish": ema_bullish,
        "EMA Bearish": ema_bearish,
        "Reasons": " | ".join(dict.fromkeys(reasons)),
        "Intraday Data": intraday,
    }


def scan_replay_history(symbol: str, history: pd.DataFrame, daily: pd.DataFrame | None = None, use_rvol_score: bool = False, min_score: float = MIN_SCORE, orb_minutes: int = DEFAULT_ORB_MINUTES) -> dict | None:
    return scan_dataframe(symbol=symbol, intraday=history, daily=daily, use_rvol_score=use_rvol_score, min_score=min_score, orb_minutes=orb_minutes)


def clean_signal_row(result: dict | None) -> dict | None:
    if not result:
        return None
    row = dict(result)
    row.pop("Intraday Data", None)
    return row
