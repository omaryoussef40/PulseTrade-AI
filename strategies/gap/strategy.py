from __future__ import annotations

"""No-catalyst gap scanner shared by Strategy Lab and future paper trading.

This module only evaluates stock candles. It does not create broker contracts or
place orders. Historical runs can pass a catalyst lookup callback; when coverage
is missing, the result is explicitly marked UNVERIFIED.
"""

from dataclasses import dataclass
from datetime import date, time as dtime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

EASTERN = ZoneInfo("America/New_York")
REQUIRED_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]
CatalystLookup = Callable[[str, date, date | None], dict[str, Any] | None]


@dataclass(frozen=True)
class GapScanConfig:
    price_min: float = 3.0
    price_max: float = 15.0
    min_abs_gap_pct: float = 8.0
    min_premarket_volume: int = 500_000
    min_premarket_rvol: float = 3.0
    min_avg_daily_volume: int = 1_000_000
    premarket_rvol_lookback: int = 5
    avg_daily_volume_lookback: int = 20
    premarket_start: dtime = dtime(4, 0)
    market_open: dtime = dtime(9, 30)
    entry_time: dtime = dtime(9, 45)
    market_close: dtime = dtime(16, 0)
    allow_gap_up_shorts: bool = True
    allow_gap_down_longs: bool = True
    require_no_catalyst: bool = True
    allow_unverified_catalyst: bool = False


def _normalize_ohlcv(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=REQUIRED_COLUMNS)
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = out.columns.get_level_values(0)
    out = out.rename(columns={str(col): str(col).strip().title() for col in out.columns})
    if any(col not in out.columns for col in REQUIRED_COLUMNS):
        return pd.DataFrame(columns=REQUIRED_COLUMNS)
    out = out[REQUIRED_COLUMNS].copy()
    for col in REQUIRED_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    out["Volume"] = out["Volume"].fillna(0).clip(lower=0)
    idx = pd.to_datetime(out.index, errors="coerce")
    valid = ~pd.isna(idx)
    out = out.loc[valid].copy()
    idx = idx[valid]
    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize(EASTERN)
    else:
        idx = idx.tz_convert(EASTERN)
    out.index = idx
    out.index.name = "Datetime"
    return out[~out.index.duplicated(keep="last")].sort_index()


def _time_slice(df: pd.DataFrame, session_date: date, start: dtime, end: dtime, *, include_end: bool = False) -> pd.DataFrame:
    if df.empty:
        return df
    idx = df.index
    date_mask = idx.date == session_date
    if include_end:
        time_mask = [(start <= ts.time() <= end) for ts in idx]
    else:
        time_mask = [(start <= ts.time() < end) for ts in idx]
    return df[date_mask & np.asarray(time_mask, dtype=bool)]


def _regular_session(df: pd.DataFrame, session_date: date, config: GapScanConfig) -> pd.DataFrame:
    return _time_slice(df, session_date, config.market_open, config.market_close, include_end=True)


def _previous_regular_session(df: pd.DataFrame, session_date: date, config: GapScanConfig) -> tuple[date | None, pd.DataFrame]:
    prior_dates = sorted({ts.date() for ts in df.index if ts.date() < session_date}, reverse=True)
    for prior_date in prior_dates:
        session = _regular_session(df, prior_date, config)
        if not session.empty:
            return prior_date, session
    return None, pd.DataFrame(columns=REQUIRED_COLUMNS)


def _average_daily_volume(df: pd.DataFrame, session_date: date, config: GapScanConfig) -> float:
    volumes: list[float] = []
    for prior_date in sorted({ts.date() for ts in df.index if ts.date() < session_date}, reverse=True):
        session = _regular_session(df, prior_date, config)
        if not session.empty:
            volumes.append(float(session["Volume"].sum()))
        if len(volumes) >= max(1, int(config.avg_daily_volume_lookback)):
            break
    return float(np.mean(volumes)) if volumes else 0.0


def _premarket_rvol(df: pd.DataFrame, session_date: date, current_volume: float, config: GapScanConfig) -> tuple[float, float, int]:
    prior_volumes: list[float] = []
    for prior_date in sorted({ts.date() for ts in df.index if ts.date() < session_date}, reverse=True):
        premarket = _time_slice(df, prior_date, config.premarket_start, config.market_open)
        volume = float(premarket["Volume"].sum()) if not premarket.empty else 0.0
        if volume > 0:
            prior_volumes.append(volume)
        if len(prior_volumes) >= max(1, int(config.premarket_rvol_lookback)):
            break
    average = float(np.mean(prior_volumes)) if prior_volumes else 0.0
    rvol = float(current_volume / average) if average > 0 else 0.0
    return rvol, average, len(prior_volumes)


def _bar_minutes(df: pd.DataFrame) -> int:
    if len(df) < 2:
        return 0
    diffs = pd.Series(df.index).diff().dropna().dt.total_seconds().div(60)
    diffs = diffs[(diffs > 0) & (diffs <= 120)]
    return int(round(float(diffs.median()))) if not diffs.empty else 0


def _grade(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B+"
    if score >= 70:
        return "B"
    if score >= 60:
        return "C+"
    return "C"


def _score(abs_gap_pct: float, pm_rvol: float, pm_volume: float, config: GapScanConfig) -> float:
    gap_points = min(abs_gap_pct / max(config.min_abs_gap_pct, 0.01), 2.0) * 17.5
    rvol_points = min(pm_rvol / max(config.min_premarket_rvol, 0.01), 2.0) * 17.5
    volume_points = min(pm_volume / max(float(config.min_premarket_volume), 1.0), 4.0) * 5.0
    return round(min(100.0, gap_points + rvol_points + volume_points + 10.0), 2)


def evaluate_gap_session(
    symbol: str,
    intraday: pd.DataFrame,
    session_date: date | str,
    config: GapScanConfig | None = None,
    catalyst_lookup: CatalystLookup | None = None,
) -> dict[str, Any]:
    """Evaluate one symbol/session and return a complete scanner audit row."""
    cfg = config or GapScanConfig()
    symbol = str(symbol).strip().upper()
    day = pd.Timestamp(session_date).date()
    df = _normalize_ohlcv(intraday)
    rejection_codes: list[str] = []

    previous_date, previous_session = _previous_regular_session(df, day, cfg)
    previous_close = float(previous_session["Close"].iloc[-1]) if not previous_session.empty else 0.0
    if previous_close <= 0:
        rejection_codes.append("NO_PREVIOUS_CLOSE")

    premarket = _time_slice(df, day, cfg.premarket_start, cfg.market_open)
    pm_last = float(premarket["Close"].iloc[-1]) if not premarket.empty else 0.0
    pm_high = float(premarket["High"].max()) if not premarket.empty else 0.0
    pm_low = float(premarket["Low"].min()) if not premarket.empty else 0.0
    pm_volume = float(premarket["Volume"].sum()) if not premarket.empty else 0.0
    if premarket.empty:
        rejection_codes.append("NO_PREMARKET_DATA")

    gap_pct = ((pm_last - previous_close) / previous_close * 100.0) if previous_close > 0 and pm_last > 0 else 0.0
    gap_direction = "UP" if gap_pct > 0 else "DOWN" if gap_pct < 0 else "FLAT"
    avg_daily_volume = _average_daily_volume(df, day, cfg)
    pm_rvol, avg_prior_pm_volume, rvol_sessions = _premarket_rvol(df, day, pm_volume, cfg)

    if not (float(cfg.price_min) <= pm_last <= float(cfg.price_max)):
        rejection_codes.append("PRICE_OUT_OF_RANGE")
    if abs(gap_pct) < float(cfg.min_abs_gap_pct):
        rejection_codes.append("GAP_BELOW_MINIMUM")
    if pm_volume < float(cfg.min_premarket_volume):
        rejection_codes.append("PREMARKET_VOLUME_LOW")
    if rvol_sessions == 0:
        rejection_codes.append("NO_PREMARKET_RVOL_HISTORY")
    elif pm_rvol < float(cfg.min_premarket_rvol):
        rejection_codes.append("PREMARKET_RVOL_LOW")
    if avg_daily_volume < float(cfg.min_avg_daily_volume):
        rejection_codes.append("AVERAGE_DAILY_VOLUME_LOW")

    catalyst = catalyst_lookup(symbol, day, previous_date) if catalyst_lookup else None
    catalyst_known = bool(catalyst and catalyst.get("known"))
    has_catalyst = bool(catalyst and catalyst.get("has_catalyst")) if catalyst_known else None
    catalyst_status = "FOUND" if has_catalyst else "CLEAR" if catalyst_known else "UNVERIFIED"
    if cfg.require_no_catalyst and has_catalyst:
        rejection_codes.append("CATALYST_FOUND")
    if cfg.require_no_catalyst and has_catalyst is None and not cfg.allow_unverified_catalyst:
        rejection_codes.append("CATALYST_UNVERIFIED")

    regular = _regular_session(df, day, cfg)
    bar_minutes = _bar_minutes(regular)
    if bar_minutes > 15:
        rejection_codes.append("INTERVAL_TOO_LARGE")
    opening_range = _time_slice(df, day, cfg.market_open, cfg.entry_time)
    entry_bars = regular[[ts.time() >= cfg.entry_time for ts in regular.index]] if not regular.empty else regular
    if opening_range.empty:
        rejection_codes.append("NO_OPENING_RANGE")
    if entry_bars.empty:
        rejection_codes.append("NO_0945_ENTRY_BAR")

    opening_price = float(opening_range["Open"].iloc[0]) if not opening_range.empty else 0.0
    confirmation_close = float(opening_range["Close"].iloc[-1]) if not opening_range.empty else 0.0
    opening_range_high = float(opening_range["High"].max()) if not opening_range.empty else 0.0
    opening_range_low = float(opening_range["Low"].min()) if not opening_range.empty else 0.0
    opening_range_mid = (opening_range_high + opening_range_low) / 2.0 if opening_range_high and opening_range_low else 0.0
    total_or_volume = float(opening_range["Volume"].sum()) if not opening_range.empty else 0.0
    if total_or_volume > 0:
        typical = (opening_range["High"] + opening_range["Low"] + opening_range["Close"]) / 3.0
        opening_vwap = float((typical * opening_range["Volume"]).sum() / total_or_volume)
    else:
        opening_vwap = confirmation_close
    entry_time = entry_bars.index[0] if not entry_bars.empty else pd.NaT
    entry_price = float(entry_bars["Open"].iloc[0]) if not entry_bars.empty else 0.0
    if entry_price and not (float(cfg.price_min) <= entry_price <= float(cfg.price_max)):
        rejection_codes.append("ENTRY_PRICE_OUT_OF_RANGE")

    signal = ""
    confirmation = ""
    if gap_direction == "UP":
        signal = "SHORT"
        if not cfg.allow_gap_up_shorts:
            rejection_codes.append("SHORTS_DISABLED")
        weak_candle = confirmation_close < opening_price
        below_vwap = confirmation_close < opening_vwap
        below_mid = confirmation_close <= opening_range_mid
        confirmation = "weak open below VWAP and range midpoint"
        if opening_range.empty or not (weak_candle and below_vwap and below_mid):
            rejection_codes.append("NO_GAP_UP_FADE_CONFIRMATION")
    elif gap_direction == "DOWN":
        signal = "LONG"
        if not cfg.allow_gap_down_longs:
            rejection_codes.append("LONGS_DISABLED")
        strong_candle = confirmation_close > opening_price
        above_vwap = confirmation_close > opening_vwap
        above_mid = confirmation_close >= opening_range_mid
        confirmation = "strong open above VWAP and range midpoint"
        if opening_range.empty or not (strong_candle and above_vwap and above_mid):
            rejection_codes.append("NO_GAP_DOWN_RECLAIM_CONFIRMATION")
    else:
        rejection_codes.append("NO_GAP_DIRECTION")

    rank_score = _score(abs(gap_pct), pm_rvol, pm_volume, cfg)
    status = "QUALIFIED" if not rejection_codes else "REJECTED"
    reasons = [f"gap {gap_pct:+.2f}%", f"premarket volume {pm_volume:,.0f}", f"premarket RVOL {pm_rvol:.2f}x"]
    if confirmation:
        reasons.append(confirmation)
    reasons.append(f"catalyst {catalyst_status.lower()}")

    return {
        "strategy": "GAP",
        "symbol": symbol,
        "session_date": day,
        "timestamp": entry_time,
        "status": status,
        "signal": signal,
        "gap_direction": gap_direction,
        "gap_pct": round(gap_pct, 2),
        "previous_session_date": previous_date,
        "previous_close": round(previous_close, 4) if previous_close else None,
        "premarket_last": round(pm_last, 4) if pm_last else None,
        "premarket_high": round(pm_high, 4) if pm_high else None,
        "premarket_low": round(pm_low, 4) if pm_low else None,
        "premarket_volume": int(pm_volume),
        "avg_prior_premarket_volume": round(avg_prior_pm_volume, 2),
        "premarket_rvol": round(pm_rvol, 2),
        "premarket_rvol_sessions": int(rvol_sessions),
        "avg_daily_volume": int(avg_daily_volume),
        "entry_time": entry_time,
        "entry_price": round(entry_price, 4) if entry_price else None,
        "opening_price": round(opening_price, 4) if opening_price else None,
        "confirmation_close": round(confirmation_close, 4) if confirmation_close else None,
        "opening_vwap": round(opening_vwap, 4) if opening_vwap else None,
        "opening_range_high": round(opening_range_high, 4) if opening_range_high else None,
        "opening_range_low": round(opening_range_low, 4) if opening_range_low else None,
        "opening_range_mid": round(opening_range_mid, 4) if opening_range_mid else None,
        "bar_minutes": int(bar_minutes),
        "score": rank_score,
        "rank_score": rank_score,
        "grade": _grade(rank_score),
        "catalyst_status": catalyst_status,
        "catalyst_verified": catalyst_known,
        "has_catalyst": has_catalyst,
        "catalyst_headline": str((catalyst or {}).get("headline") or ""),
        "catalyst_impact_score": int((catalyst or {}).get("impact_score") or 0),
        "catalyst_window_articles": int((catalyst or {}).get("window_articles") or 0),
        "rejection_codes": ", ".join(dict.fromkeys(rejection_codes)),
        "reason": "; ".join(dict.fromkeys(rejection_codes)) if rejection_codes else ", ".join(reasons),
        "reasons": ", ".join(reasons),
    }


def scan_gap_sessions(
    data: dict[str, pd.DataFrame],
    config: GapScanConfig | None = None,
    catalyst_lookup: CatalystLookup | None = None,
) -> pd.DataFrame:
    """Evaluate every available symbol/session and return qualified and rejected rows."""
    rows: list[dict[str, Any]] = []
    cfg = config or GapScanConfig()
    for raw_symbol, raw_df in (data or {}).items():
        symbol = str(raw_symbol).strip().upper()
        df = _normalize_ohlcv(raw_df)
        if not symbol or df.empty:
            continue
        dates = sorted({ts.date() for ts in df.index})
        for session_date in dates:
            regular = _regular_session(df, session_date, cfg)
            premarket = _time_slice(df, session_date, cfg.premarket_start, cfg.market_open)
            if regular.empty and premarket.empty:
                continue
            rows.append(evaluate_gap_session(symbol, df, session_date, cfg, catalyst_lookup))
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    return out.sort_values(["session_date", "status", "rank_score", "symbol"], ascending=[True, False, False, True]).reset_index(drop=True)


def scan_dataframe(
    symbol: str,
    intraday: pd.DataFrame,
    daily: pd.DataFrame | None = None,
    config: GapScanConfig | None = None,
    catalyst_lookup: CatalystLookup | None = None,
    **kwargs,
) -> dict[str, Any] | None:
    """Compatibility scanner entry point; returns only a qualified latest session."""
    df = _normalize_ohlcv(intraday)
    if df.empty:
        return None
    result = evaluate_gap_session(symbol, df, df.index[-1].date(), config, catalyst_lookup)
    if result.get("status") != "QUALIFIED":
        return None
    return {
        **result,
        "Signal": result.get("signal"),
        "Score": result.get("score"),
        "Grade": result.get("grade"),
        "Price": result.get("entry_price"),
        "RVOL": result.get("premarket_rvol"),
        "Reasons": result.get("reasons"),
    }
