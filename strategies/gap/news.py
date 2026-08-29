from __future__ import annotations

"""Read-only historical catalyst coverage for GAP research."""

import sqlite3
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "news.db"


class HistoricalCatalystLookup:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH, min_impact: int = 50, min_window_articles: int = 5):
        self.db_path = Path(db_path)
        self.min_impact = int(min_impact)
        self.min_window_articles = int(min_window_articles)
        self._cache: dict[tuple[str, date, date | None], dict[str, Any]] = {}

    def __call__(self, symbol: str, session_date: date, previous_session_date: date | None) -> dict[str, Any]:
        key = (str(symbol).strip().upper(), session_date, previous_session_date)
        if key in self._cache:
            return self._cache[key]
        result = self._query(*key)
        self._cache[key] = result
        return result

    def _query(self, symbol: str, session_date: date, previous_session_date: date | None) -> dict[str, Any]:
        if not self.db_path.exists() or previous_session_date is None:
            return {"known": False, "has_catalyst": None, "window_articles": 0}
        start = datetime.combine(previous_session_date, dtime(16, 0), tzinfo=EASTERN).astimezone(timezone.utc)
        end = datetime.combine(session_date, dtime(9, 45), tzinfo=EASTERN).astimezone(timezone.utc)
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                total = int(conn.execute(
                    "SELECT COUNT(*) FROM articles WHERE published_at >= ? AND published_at <= ?",
                    (start.isoformat(), end.isoformat()),
                ).fetchone()[0])
                known = total >= self.min_window_articles
                if not known:
                    return {"known": False, "has_catalyst": None, "window_articles": total}
                row = conn.execute(
                    """
                    SELECT a.headline, a.impact_score, a.published_at
                    FROM articles a
                    JOIN watchlist_hits h ON h.article_id = a.id
                    WHERE h.ticker = ?
                      AND a.impact_score >= ?
                      AND a.published_at >= ?
                      AND a.published_at <= ?
                    ORDER BY a.impact_score DESC, a.published_at DESC
                    LIMIT 1
                    """,
                    (symbol, self.min_impact, start.isoformat(), end.isoformat()),
                ).fetchone()
                return {
                    "known": True,
                    "has_catalyst": bool(row),
                    "headline": str(row["headline"] or "") if row else "",
                    "impact_score": int(row["impact_score"] or 0) if row else 0,
                    "published_at": str(row["published_at"] or "") if row else "",
                    "window_articles": total,
                }
        except Exception:
            return {"known": False, "has_catalyst": None, "window_articles": 0}
