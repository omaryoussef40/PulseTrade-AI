from __future__ import annotations

"""Dynamic premarket watchlist builder.

This module is intentionally separate from the live strategy and order engine.
It builds a small daily suggested symbol list from premarket volume and
headline-confirmed catalysts, saves it to exports, and exposes a helper that
combines it with the user's manual watchlist when configured.
"""

import json
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from market_data import provider_from_ib

EASTERN = ZoneInfo("America/New_York")
BASE_DIR = Path(__file__).resolve().parents[1]
EXPORT_DIR = BASE_DIR / "exports"
DYNAMIC_WATCHLIST_FILE = EXPORT_DIR / "dynamic_watchlist.json"
PREMARKET_START = dtime(4, 0)
MARKET_OPEN = dtime(9, 30)

DEFAULT_SOURCE_UNIVERSE = [
    "SPY", "QQQ", "IWM",
    "NVDA", "AAPL", "MSFT", "META", "AMZN", "GOOGL",
    "TSLA", "AMD", "PLTR", "COIN", "MSTR",
    "AVGO", "SMCI", "MU", "ARM", "TSM", "MRVL",
    "JPM", "GS", "BAC",
    "NFLX", "UBER", "XOM", "COST",
    "RBLX", "HOOD", "SOFI", "RKLB", "HIMS", "CRWD",
]

CATALYST_CATEGORY_LABELS = {
    "analyst_upgrade": "upgrade",
    "analyst_downgrade": "downgrade",
    "earnings": "earnings",
    "breaking": "breaking news",
    "ma": "M&A",
    "sec": "SEC filing",
    "lawsuit": "legal catalyst",
    "options": "options flow",
}


def dynamic_config(config: dict) -> dict:
    raw = config.get("dynamic_watchlist", {}) if isinstance(config, dict) else {}
    cfg = raw if isinstance(raw, dict) else {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "mode": str(cfg.get("mode", "Manual")),
        "suggestive_only": bool(cfg.get("suggestive_only", True)),
        "max_symbols": int(cfg.get("max_symbols", 5)),
        "refresh_hour": int(cfg.get("refresh_hour", 9)),
        "refresh_minute": int(cfg.get("refresh_minute", 30)),
        "source_universe": normalize_symbols(cfg.get("source_universe") or DEFAULT_SOURCE_UNIVERSE),
    }


def normalize_symbols(values: Any) -> list[str]:
    if isinstance(values, str):
        values = values.replace("\n", ",").split(",")
    out: list[str] = []
    for value in values or []:
        symbol = str(value or "").strip().upper().replace("$", "")
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def today_key(now: datetime | None = None) -> str:
    now = now or datetime.now(EASTERN)
    if now.tzinfo is None:
        now = now.replace(tzinfo=EASTERN)
    else:
        now = now.astimezone(EASTERN)
    return now.date().isoformat()


def load_dynamic_payload(path: Path = DYNAMIC_WATCHLIST_FILE) -> dict:
    try:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_dynamic_payload(payload: dict, path: Path = DYNAMIC_WATCHLIST_FILE) -> None:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    tmp.replace(path)


def is_today_payload(payload: dict, now: datetime | None = None) -> bool:
    return bool(payload and payload.get("date") == today_key(now))


def today_dynamic_symbols(config: dict, now: datetime | None = None) -> list[str]:
    cfg = dynamic_config(config)
    if not cfg["enabled"] or cfg["suggestive_only"]:
        return []
    payload = load_dynamic_payload()
    if not is_today_payload(payload, now):
        return []
    return normalize_symbols(payload.get("symbols", []))[: int(cfg["max_symbols"])]


def combined_watchlist(config: dict, now: datetime | None = None) -> list[str]:
    manual = normalize_symbols(config.get("watchlist", []) if isinstance(config, dict) else [])
    dynamic = today_dynamic_symbols(config, now=now)
    return normalize_symbols(manual + dynamic)


def should_auto_build(config: dict, now: datetime | None = None) -> bool:
    cfg = dynamic_config(config)
    if not cfg["enabled"] or cfg["mode"].lower() != "automatic":
        return False
    now = now or datetime.now(EASTERN)
    if now.tzinfo is None:
        now = now.replace(tzinfo=EASTERN)
    else:
        now = now.astimezone(EASTERN)
    if now.time() < dtime(int(cfg["refresh_hour"]), int(cfg["refresh_minute"])):
        return False
    return not is_today_payload(load_dynamic_payload(), now=now)


def build_premarket_watchlist(config: dict, ib, now: datetime | None = None) -> dict:
    cfg = dynamic_config(config)
    now = now or datetime.now(EASTERN)
    if now.tzinfo is None:
        now = now.replace(tzinfo=EASTERN)
    else:
        now = now.astimezone(EASTERN)

    provider = provider_from_ib(ib, timezone=EASTERN)
    rows: list[dict] = []
    errors: list[dict] = []

    for symbol in cfg["source_universe"]:
        try:
            row = score_premarket_symbol(provider, symbol, now=now)
            if row is not None:
                rows.append(row)
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})

    rows = sorted(rows, key=lambda item: float(item.get("score", 0)), reverse=True)
    selected = rows[: int(cfg["max_symbols"])]
    payload = {
        "date": today_key(now),
        "generated_at": now.isoformat(),
        "mode": cfg["mode"],
        "symbols": [row["symbol"] for row in selected],
        "rows": rows,
        "errors": errors[:25],
    }
    save_dynamic_payload(payload)
    return payload


def score_premarket_symbol(provider, symbol: str, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(EASTERN)
    today = now.astimezone(EASTERN).date() if now.tzinfo else now.date()

    intraday = provider.intraday_bars(symbol, duration="10 D", bar_size="5 mins", use_rth=False)
    if intraday is None or intraday.empty:
        return None
    idx = pd.to_datetime(intraday.index)
    today_mask = idx.date == today
    time_mask = [(PREMARKET_START <= ts.time() < MARKET_OPEN) for ts in idx]
    premarket = intraday[today_mask & pd.Series(time_mask, index=intraday.index)]
    if premarket.empty:
        return None

    daily = provider.daily_bars(symbol, duration="10 D", use_rth=True)
    previous_levels = _previous_day_levels(daily, today)
    previous_close = previous_levels.get("close")
    if previous_close is None or previous_close <= 0:
        return None

    pm_close = float(premarket["Close"].iloc[-1])
    pm_high = float(premarket["High"].max())
    pm_low = float(premarket["Low"].min())
    pm_volume = float(pd.to_numeric(premarket["Volume"], errors="coerce").fillna(0).sum())
    avg_daily_volume = _avg_daily_volume(daily, today)
    avg_prior_premarket_volume = _avg_prior_premarket_volume(intraday, today)

    gap_pct = ((pm_close - previous_close) / previous_close) * 100.0
    range_pct = ((pm_high - pm_low) / previous_close) * 100.0
    premarket_volume_pct = (pm_volume / avg_daily_volume * 100.0) if avg_daily_volume > 0 else 0.0
    premarket_relative_volume = (pm_volume / avg_prior_premarket_volume) if avg_prior_premarket_volume > 0 else 0.0
    if premarket_relative_volume > 0:
        unusual_volume_score = min(premarket_relative_volume * 18.0, 55.0)
    else:
        unusual_volume_score = min(premarket_volume_pct * 3.0, 55.0)

    liquidity_score = min(premarket_volume_pct * 0.75, 15.0)
    range_score = min(range_pct * 5.0, 10.0)
    proximity = _pmb_level_proximity_score(pm_close, previous_close, previous_levels.get("high"), previous_levels.get("low"))
    catalyst = _latest_catalyst(symbol)
    catalyst_categories = [str(c).lower() for c in getattr(catalyst, "categories", ())] if catalyst else []
    catalyst_score = _catalyst_score(catalyst)
    gap_extension_penalty = min(max(abs(gap_pct) - 3.0, 0.0) * 5.0, 15.0)
    score = round(max(0.0, min(100.0, unusual_volume_score + liquidity_score + range_score + proximity["score"] + catalyst_score - gap_extension_penalty)), 2)

    reasons = []
    if premarket_relative_volume > 0:
        reasons.append(f"premarket RVOL {premarket_relative_volume:.2f}x")
    if pm_volume >= 250_000:
        reasons.append(f"premarket volume {int(pm_volume):,}")
    if abs(gap_pct) >= 1.5:
        reasons.append(f"gap {gap_pct:+.2f}%")
    if proximity["distance_pct"] is not None and proximity["distance_pct"] <= 2.0:
        reasons.append(f"near {proximity['level_name']} ({proximity['distance_pct']:.2f}%)")
    if range_pct >= 1.0:
        reasons.append(f"range {range_pct:.2f}%")
    if catalyst:
        catalyst_labels = [
            CATALYST_CATEGORY_LABELS.get(category, category.replace("_", " "))
            for category in catalyst_categories
            if category in CATALYST_CATEGORY_LABELS
        ]
        if catalyst_labels:
            reasons.append(" / ".join(catalyst_labels[:2]))
        else:
            reasons.append("headline news")
    if abs(gap_pct) >= 3.0:
        reasons.append(f"extended gap {gap_pct:.2f}%")

    return {
        "symbol": symbol,
        "score": score,
        "scoring_model": "unusual_premarket_volume",
        "gap_pct": round(gap_pct, 2),
        "premarket_range_pct": round(range_pct, 2),
        "premarket_volume": int(pm_volume),
        "avg_prior_premarket_volume": int(avg_prior_premarket_volume),
        "premarket_relative_volume": round(premarket_relative_volume, 2),
        "premarket_volume_pct_of_avg_daily": round(premarket_volume_pct, 2),
        "nearest_pmb_level": proximity["level_name"],
        "nearest_pmb_level_distance_pct": round(proximity["distance_pct"], 2) if proximity["distance_pct"] is not None else None,
        "previous_high": round(previous_levels["high"], 2) if previous_levels.get("high") else None,
        "previous_low": round(previous_levels["low"], 2) if previous_levels.get("low") else None,
        "previous_close": round(previous_close, 2),
        "premarket_last": round(pm_close, 2),
        "has_news": bool(catalyst),
        "news_headline": getattr(catalyst, "headline", "") if catalyst else "",
        "news_impact_score": int(getattr(catalyst, "impact_score", 0) or 0) if catalyst else 0,
        "news_categories": catalyst_categories,
        "news_sentiment": str(getattr(catalyst, "sentiment", "") or "") if catalyst else "",
        "unusual_volume_score": round(unusual_volume_score, 2),
        "liquidity_score": round(liquidity_score, 2),
        "range_score": round(range_score, 2),
        "level_proximity_score": round(proximity["score"], 2),
        "catalyst_score": round(catalyst_score, 2),
        "gap_extension_penalty": round(gap_extension_penalty, 2),
        "reason": ", ".join(reasons) if reasons else "premarket activity",
    }


def _previous_day_levels(daily: pd.DataFrame, today) -> dict:
    empty = {"high": None, "low": None, "close": None}
    if daily is None or daily.empty:
        return empty
    df = daily.copy()
    idx = pd.to_datetime(df.index)
    prior = df[idx.date < today]
    if prior.empty:
        prior = df
    try:
        row = prior.dropna(subset=["High", "Low", "Close"]).iloc[-1]
        return {
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
        }
    except Exception:
        return empty


def _pmb_level_proximity_score(price: float, previous_close: float, pdh: float | None, pdl: float | None) -> dict:
    candidates = []
    for name, level in [("PDH", pdh), ("PDL", pdl)]:
        if level and previous_close:
            distance_pct = abs(float(price) - float(level)) / float(previous_close) * 100.0
            candidates.append((distance_pct, name))
    if not candidates:
        return {"score": 0.0, "level_name": None, "distance_pct": None}
    distance_pct, level_name = min(candidates, key=lambda item: item[0])
    if distance_pct > 2.0:
        score = 0.0
    else:
        score = 15.0 * (1.0 - (distance_pct / 2.0))
    return {"score": max(0.0, score), "level_name": level_name, "distance_pct": distance_pct}


def _previous_close(daily: pd.DataFrame, today) -> float | None:
    if daily is None or daily.empty or "Close" not in daily.columns:
        return None
    df = daily.copy()
    idx = pd.to_datetime(df.index)
    prior = df[idx.date < today]
    if prior.empty:
        prior = df
    try:
        return float(prior["Close"].dropna().iloc[-1])
    except Exception:
        return None


def _avg_daily_volume(daily: pd.DataFrame, today) -> float:
    if daily is None or daily.empty or "Volume" not in daily.columns:
        return 0.0
    df = daily.copy()
    idx = pd.to_datetime(df.index)
    prior = df[idx.date < today].tail(5)
    if prior.empty:
        prior = df.tail(5)
    return float(pd.to_numeric(prior["Volume"], errors="coerce").fillna(0).mean() or 0.0)


def _avg_prior_premarket_volume(intraday: pd.DataFrame, today) -> float:
    if intraday is None or intraday.empty or "Volume" not in intraday.columns:
        return 0.0
    df = intraday.copy()
    idx = pd.to_datetime(df.index)
    df = df.assign(_date=idx.date, _time=[ts.time() for ts in idx])
    prior = df[(df["_date"] < today) & (df["_time"] >= PREMARKET_START) & (df["_time"] < MARKET_OPEN)]
    if prior.empty:
        return 0.0
    volumes = (
        pd.to_numeric(prior["Volume"], errors="coerce")
        .fillna(0)
        .groupby(prior["_date"])
        .sum()
    )
    volumes = volumes[volumes > 0].tail(5)
    if volumes.empty:
        return 0.0
    return float(volumes.mean())


def _latest_catalyst(symbol: str):
    try:
        from modules.news_bridge import get_latest_catalyst

        return get_latest_catalyst(symbol, min_impact=60, lookback_hours=24)
    except Exception:
        return None


def _catalyst_score(catalyst) -> float:
    if not catalyst:
        return 0.0
    categories = {str(c).lower() for c in getattr(catalyst, "categories", ())}
    base = min(float(getattr(catalyst, "impact_score", 0) or 0) * 0.22, 18.0)
    if categories & {"analyst_upgrade", "analyst_downgrade"}:
        base += 7.0
    if "earnings" in categories:
        base += 8.0
    if categories & {"breaking", "ma", "sec", "lawsuit"}:
        base += 5.0
    return min(base, 30.0)
