"""
PulseTrade AI - News Bridge

Small read-only bridge between the Market Intelligence database and the scanner UI.
It lets dashboard.py ask: "does this ticker have a fresh catalyst?"

Place this file in:
    modules/news_bridge.py

Requires:
    data/news.db created by modules/news_engine.py / modules/news_database.py
"""

from __future__ import annotations

import html
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = THIS_FILE.parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "news.db"


@dataclass(frozen=True)
class TickerCatalyst:
    ticker: str
    headline: str
    sentiment: str
    impact_score: int
    published_at: str
    source: str = "benzinga"
    url: str = ""
    categories: tuple[str, ...] = ()
    age_label: str = ""


def _json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass
        return [x.strip() for x in value.split(",") if x.strip()]
    return []


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        raw = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _age_label(value: Any) -> str:
    dt = _parse_dt(value)
    if not dt:
        return "unknown"
    seconds = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _clean_text(value: Any, max_chars: int = 120) -> str:
    text = html.unescape(str(value or ""))
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def _connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def get_latest_catalyst(
    ticker: str,
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    min_impact: int = 70,
    lookback_hours: int = 24,
) -> TickerCatalyst | None:
    """Return the latest/highest-impact catalyst for one ticker."""
    ticker = str(ticker or "").strip().upper().replace("$", "")
    if not ticker or not Path(db_path).exists():
        return None

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()

    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT
                    a.headline,
                    a.source,
                    a.published_at,
                    a.url,
                    a.tickers_json,
                    a.categories_json,
                    a.sentiment,
                    a.impact_score,
                    a.watchlist_hit
                FROM articles a
                JOIN watchlist_hits h ON h.article_id = a.id
                WHERE h.ticker = ?
                  AND a.impact_score >= ?
                  AND a.published_at >= ?
                ORDER BY a.impact_score DESC, a.published_at DESC
                LIMIT 1
                """,
                (ticker, int(min_impact), cutoff),
            ).fetchall()

            # Fallback for DBs where watchlist_hits has not been populated properly.
            if not rows:
                rows = conn.execute(
                    """
                    SELECT
                        headline, source, published_at, url, tickers_json,
                        categories_json, sentiment, impact_score, watchlist_hit
                    FROM articles
                    WHERE impact_score >= ?
                      AND published_at >= ?
                    ORDER BY impact_score DESC, published_at DESC
                    LIMIT 300
                    """,
                    (int(min_impact), cutoff),
                ).fetchall()

            for row in rows:
                tickers = [x.upper() for x in _json_list(row["tickers_json"])]
                if ticker not in tickers:
                    continue
                return TickerCatalyst(
                    ticker=ticker,
                    headline=_clean_text(row["headline"], 110),
                    source=str(row["source"] or "benzinga"),
                    published_at=str(row["published_at"] or ""),
                    url=str(row["url"] or ""),
                    categories=tuple(_json_list(row["categories_json"])[:4]),
                    sentiment=str(row["sentiment"] or "neutral").lower(),
                    impact_score=int(row["impact_score"] or 0),
                    age_label=_age_label(row["published_at"]),
                )
    except Exception:
        return None

    return None


def get_catalyst_map(
    tickers: Iterable[str],
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    min_impact: int = 70,
    lookback_hours: int = 24,
) -> dict[str, TickerCatalyst]:
    """Return latest catalysts for many tickers."""
    out: dict[str, TickerCatalyst] = {}
    for ticker in tickers or []:
        symbol = str(ticker or "").strip().upper().replace("$", "")
        if not symbol or symbol in out:
            continue
        catalyst = get_latest_catalyst(symbol, db_path=db_path, min_impact=min_impact, lookback_hours=lookback_hours)
        if catalyst:
            out[symbol] = catalyst
    return out


def catalyst_direction(catalyst: TickerCatalyst | None) -> str:
    if not catalyst:
        return "neutral"
    sentiment = catalyst.sentiment.lower()
    if sentiment in {"bullish", "bearish"}:
        return sentiment
    cats = {c.lower() for c in catalyst.categories}
    if "analyst_upgrade" in cats:
        return "bullish"
    if "analyst_downgrade" in cats or "lawsuit" in cats or "sec" in cats:
        return "bearish"
    return "neutral"


def catalyst_score(catalyst: TickerCatalyst | None) -> int:
    """Return a simple 0-100 catalyst score usable later in ranking."""
    if not catalyst:
        return 0
    score = int(catalyst.impact_score)
    # Freshness boost/decay.
    dt = _parse_dt(catalyst.published_at)
    if dt:
        minutes = (datetime.now(timezone.utc) - dt).total_seconds() / 60
        if minutes <= 15:
            score += 8
        elif minutes <= 60:
            score += 4
        elif minutes >= 8 * 60:
            score -= 8
    return max(0, min(100, score))


def render_catalyst_html(catalyst: TickerCatalyst | None) -> str:
    """Return a compact HTML badge block for scanner cards."""
    if not catalyst:
        return ""

    direction = catalyst_direction(catalyst)
    if direction == "bullish":
        border = "#22c55e"
        label = "Bullish catalyst"
    elif direction == "bearish":
        border = "#ef4444"
        label = "Bearish catalyst"
    else:
        border = "#f59e0b"
        label = "Catalyst"

    headline = html.escape(catalyst.headline)
    age = html.escape(catalyst.age_label)
    cats = ", ".join(catalyst.categories[:2]) if catalyst.categories else "news"
    cats = html.escape(cats)
    url = html.escape(catalyst.url, quote=True)
    open_link = f"<a href='{url}' target='_blank' style='color:rgba(250,250,250,.75); text-decoration:none;'>Open</a>" if url else ""

    return (
        f"<div style='margin-top:10px; padding:10px 11px; border-left:4px solid {border}; "
        f"background:rgba(255,255,255,0.045); border-radius:12px;'>"
        f"<div style='font-size:.76rem; color:rgba(250,250,250,.70); font-weight:700;'>🔥 {label} • Impact {int(catalyst.impact_score)} • {age}</div>"
        f"<div style='font-size:.82rem; line-height:1.25; margin-top:4px; color:rgba(250,250,250,.88);'>{headline}</div>"
        f"<div style='font-size:.72rem; color:rgba(250,250,250,.55); margin-top:5px;'>{cats} {open_link}</div>"
        f"</div>"
    )


if __name__ == "__main__":
    print("news_bridge.py OK")
    sample = get_catalyst_map(["NVDA", "TSLA", "AMD"])
    for k, v in sample.items():
        print(k, v)
