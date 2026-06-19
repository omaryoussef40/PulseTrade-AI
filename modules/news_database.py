"""
PulseTrade AI - News Database
SQLite storage layer for Market Intelligence / Benzinga news ingestion.

Place this file in:
    modules/news_database.py

Creates/uses:
    data/news.db

This module is intentionally standalone and does not touch the trading engine.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


# Resolve project root from modules/news_database.py -> project root
BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
DEFAULT_DB_PATH = DATA_DIR / "news.db"


@dataclass(frozen=True)
class NewsArticle:
    """Normalized market-intelligence article/event row."""

    external_id: str
    source: str
    published_at: str
    headline: str
    summary: str = ""
    url: str = ""
    author: str = ""
    tickers: list[str] | None = None
    categories: list[str] | None = None
    sentiment: str = "neutral"
    impact_score: int = 0
    watchlist_hit: bool = False
    raw_json: dict[str, Any] | None = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value if value is not None else [], ensure_ascii=False, default=str)
    except Exception:
        return "[]"


def _json_loads(value: str | None, fallback: Any = None) -> Any:
    if fallback is None:
        fallback = []
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def normalize_tickers(tickers: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for ticker in tickers or []:
        t = str(ticker).strip().upper().replace("$", "")
        if t and t not in out:
            out.append(t)
    return out


class NewsDatabase:
    """SQLite helper for PulseTrade AI Market Intelligence."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    external_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    headline TEXT NOT NULL,
                    summary TEXT,
                    url TEXT,
                    author TEXT,
                    tickers_json TEXT NOT NULL DEFAULT '[]',
                    categories_json TEXT NOT NULL DEFAULT '[]',
                    sentiment TEXT NOT NULL DEFAULT 'neutral',
                    impact_score INTEGER NOT NULL DEFAULT 0,
                    watchlist_hit INTEGER NOT NULL DEFAULT 0,
                    telegram_sent INTEGER NOT NULL DEFAULT 0,
                    raw_json TEXT,
                    UNIQUE(source, external_id)
                );

                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_id INTEGER NOT NULL,
                    alert_type TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'sent',
                    error TEXT,
                    FOREIGN KEY(article_id) REFERENCES articles(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    provider TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS watchlist_hits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_id INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    hit_type TEXT NOT NULL DEFAULT 'direct',
                    created_at TEXT NOT NULL,
                    UNIQUE(article_id, ticker, hit_type),
                    FOREIGN KEY(article_id) REFERENCES articles(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS engine_health (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    engine_running INTEGER NOT NULL DEFAULT 0,
                    last_poll_started_at TEXT,
                    last_poll_finished_at TEXT,
                    last_status TEXT,
                    last_error TEXT,
                    articles_seen INTEGER NOT NULL DEFAULT 0,
                    articles_inserted INTEGER NOT NULL DEFAULT 0,
                    watchlist_hits INTEGER NOT NULL DEFAULT 0,
                    alerts_sent INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                INSERT OR IGNORE INTO engine_health (id, updated_at)
                VALUES (1, datetime('now'));

                CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at);
                CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source);
                CREATE INDEX IF NOT EXISTS idx_articles_impact ON articles(impact_score);
                CREATE INDEX IF NOT EXISTS idx_articles_watchlist_hit ON articles(watchlist_hit);
                CREATE INDEX IF NOT EXISTS idx_watchlist_hits_ticker ON watchlist_hits(ticker);
                CREATE INDEX IF NOT EXISTS idx_alerts_article_id ON alerts(article_id);
                """
            )

    def upsert_source(self, name: str, provider: str, enabled: bool = True, config: dict[str, Any] | None = None) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sources (name, provider, enabled, config_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    provider=excluded.provider,
                    enabled=excluded.enabled,
                    config_json=excluded.config_json,
                    updated_at=excluded.updated_at
                """,
                (name, provider, int(enabled), _json_dumps(config or {}), now, now),
            )

    def insert_article(self, article: NewsArticle) -> tuple[bool, int | None]:
        """Insert article. Returns (inserted, article_id). Duplicate returns (False, existing_id)."""
        tickers = normalize_tickers(article.tickers)
        categories = [str(c).strip() for c in (article.categories or []) if str(c).strip()]
        raw_json = article.raw_json or {}
        now = utc_now_iso()

        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM articles WHERE source = ? AND external_id = ?",
                (article.source, article.external_id),
            ).fetchone()
            if existing:
                return False, int(existing["id"])

            cur = conn.execute(
                """
                INSERT INTO articles (
                    external_id, source, published_at, received_at, headline, summary, url, author,
                    tickers_json, categories_json, sentiment, impact_score, watchlist_hit,
                    telegram_sent, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    article.external_id,
                    article.source,
                    article.published_at,
                    now,
                    article.headline,
                    article.summary,
                    article.url,
                    article.author,
                    _json_dumps(tickers),
                    _json_dumps(categories),
                    article.sentiment,
                    int(article.impact_score or 0),
                    int(bool(article.watchlist_hit)),
                    0,
                    _json_dumps(raw_json),
                ),
            )
            article_id = int(cur.lastrowid)

            for ticker in tickers:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO watchlist_hits (article_id, ticker, hit_type, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (article_id, ticker, "direct" if article.watchlist_hit else "detected", now),
                )

            return True, article_id

    def mark_telegram_sent(self, article_id: int, destination: str = "telegram", status: str = "sent", error: str = "") -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute("UPDATE articles SET telegram_sent = 1 WHERE id = ?", (article_id,))
            conn.execute(
                """
                INSERT INTO alerts (article_id, alert_type, destination, sent_at, status, error)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (article_id, "news", destination, now, status, error),
            )

    def get_recent_articles(
        self,
        limit: int = 100,
        ticker: str | None = None,
        source: str | None = None,
        watchlist_only: bool = False,
        min_impact: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM articles WHERE 1=1"
        params: list[Any] = []

        if source:
            sql += " AND source = ?"
            params.append(source)
        if watchlist_only:
            sql += " AND watchlist_hit = 1"
        if min_impact is not None:
            sql += " AND impact_score >= ?"
            params.append(int(min_impact))
        if ticker:
            sql += " AND id IN (SELECT article_id FROM watchlist_hits WHERE ticker = ?)"
            params.append(ticker.strip().upper().replace("$", ""))

        sql += " ORDER BY published_at DESC LIMIT ?"
        params.append(int(limit))

        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_article_dict(row) for row in rows]

    def search_articles(self, query: str, limit: int = 100) -> list[dict[str, Any]]:
        q = f"%{query.strip()}%"
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM articles
                WHERE headline LIKE ? OR summary LIKE ? OR tickers_json LIKE ? OR categories_json LIKE ?
                ORDER BY published_at DESC
                LIMIT ?
                """,
                (q, q, q, q, int(limit)),
            ).fetchall()
            return [self._row_to_article_dict(row) for row in rows]

    def get_article_by_id(self, article_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM articles WHERE id = ?", (int(article_id),)).fetchone()
            return self._row_to_article_dict(row) if row else None

    def get_stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM articles").fetchone()["c"]
            watchlist_hits = conn.execute("SELECT COUNT(*) AS c FROM articles WHERE watchlist_hit = 1").fetchone()["c"]
            high_impact = conn.execute("SELECT COUNT(*) AS c FROM articles WHERE impact_score >= 80").fetchone()["c"]
            alerts_sent = conn.execute("SELECT COUNT(*) AS c FROM alerts").fetchone()["c"]
            latest = conn.execute("SELECT MAX(published_at) AS latest FROM articles").fetchone()["latest"]
            return {
                "total_articles": int(total or 0),
                "watchlist_hits": int(watchlist_hits or 0),
                "high_impact": int(high_impact or 0),
                "alerts_sent": int(alerts_sent or 0),
                "latest_article_at": latest,
            }

    def update_health(self, **kwargs: Any) -> None:
        allowed = {
            "engine_running",
            "last_poll_started_at",
            "last_poll_finished_at",
            "last_status",
            "last_error",
            "articles_seen",
            "articles_inserted",
            "watchlist_hits",
            "alerts_sent",
        }
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        fields["updated_at"] = utc_now_iso()
        set_clause = ", ".join([f"{k} = ?" for k in fields.keys()])
        values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
        values.append(1)
        with self.connect() as conn:
            conn.execute(f"UPDATE engine_health SET {set_clause} WHERE id = ?", values)

    def get_health(self) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM engine_health WHERE id = 1").fetchone()
            return dict(row) if row else {}

    def _row_to_article_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["tickers"] = _json_loads(data.pop("tickers_json", "[]"), [])
        data["categories"] = _json_loads(data.pop("categories_json", "[]"), [])
        data["raw_json"] = _json_loads(data.get("raw_json"), {})
        data["watchlist_hit"] = bool(data.get("watchlist_hit"))
        data["telegram_sent"] = bool(data.get("telegram_sent"))
        return data


def get_news_db(db_path: str | Path = DEFAULT_DB_PATH) -> NewsDatabase:
    return NewsDatabase(db_path)


def initialize_news_database(db_path: str | Path = DEFAULT_DB_PATH) -> NewsDatabase:
    db = NewsDatabase(db_path)
    db.upsert_source("benzinga", "benzinga", enabled=True, config={"type": "rest"})
    return db


if __name__ == "__main__":
    db = initialize_news_database()
    print(f"News database initialized: {db.db_path}")
    print(db.get_stats())
