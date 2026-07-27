"""
PulseTrade AI - News Analyzer
Rule-based enrichment layer for Market Intelligence articles.

Place this file in:
    modules/news_analyzer.py

Purpose:
    - Extract/normalize tickers from Benzinga or other news rows
    - Detect category
    - Detect sentiment
    - Calculate impact score
    - Mark watchlist hits
    - Update data/news.db articles in place

Run tests / examples:
    python -m py_compile modules/news_analyzer.py
    python modules/news_analyzer.py --demo
    python modules/news_analyzer.py --analyze-recent 100
    python modules/news_analyzer.py --analyze-all

This file is intentionally independent from the trading engine.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Allow running as: python modules/news_analyzer.py
THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = THIS_FILE.parents[1]
MODULES_DIR = THIS_FILE.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(MODULES_DIR) not in sys.path:
    sys.path.insert(0, str(MODULES_DIR))

try:
    from news_database import NewsDatabase, DEFAULT_DB_PATH, normalize_tickers, _json_loads, _json_dumps
except Exception as exc:  # pragma: no cover
    raise RuntimeError("Could not import modules/news_database.py. Make sure it exists and compiles.") from exc

CONFIG_PATH = PROJECT_ROOT / "config.json"

DEFAULT_WATCHLIST = [
    "SPY", "QQQ", "IWM",
    "NVDA", "AAPL", "MSFT", "META", "AMZN", "GOOGL",
    "TSLA", "AMD", "PLTR", "COIN", "MSTR",
    "AVGO", "SMCI", "MU", "ARM", "TSM", "MRVL",
    "JPM", "GS", "BAC", "NFLX", "UBER", "XOM", "COST",
    "RBLX", "HOOD", "SOFI", "RKLB", "HIMS", "CRWD",
]

# Alias detection is deliberately watchlist-focused. This avoids false positives from common words.
COMPANY_ALIASES: dict[str, list[str]] = {
    "TSLA": ["tesla", "robotaxi", "cybertruck", "model y", "model 3", "elon musk"],
    "NVDA": ["nvidia", "jensen huang", "gpu", "blackwell", "cuda"],
    "AMD": ["advanced micro devices", "radeon", "epyc", "ryzen"],
    "AAPL": ["apple", "iphone", "ipad", "mac", "vision pro", "tim cook"],
    "MSFT": ["microsoft", "azure", "satya nadella", "openai"],
    "GOOGL": ["google", "alphabet", "gemini", "youtube"],
    "META": ["meta platforms", "facebook", "instagram", "zuckerberg", "threads"],
    "AMZN": ["amazon", "aws", "andy jassy"],
    "PLTR": ["palantir", "alex karp"],
    "COIN": ["coinbase"],
    "MSTR": ["microstrategy", "michael saylor", "bitcoin treasury"],
    "TSM": ["taiwan semiconductor", "tsmc"],
    "AVGO": ["broadcom"],
    "SMCI": ["super micro", "supermicro"],
    "MU": ["micron"],
    "ARM": ["arm holdings"],
    "MRVL": ["marvell"],
    "JPM": ["jpmorgan", "jp morgan", "jamie dimon"],
    "GS": ["goldman sachs"],
    "BAC": ["bank of america"],
    "NFLX": ["netflix"],
    "UBER": ["uber"],
    "XOM": ["exxon", "exxon mobil"],
    "COST": ["costco"],
    "RBLX": ["roblox"],
    "HOOD": ["robinhood"],
    "SOFI": ["sofi"],
    "RKLB": ["rocket lab"],
    "HIMS": ["hims", "hers health"],
    "CRWD": ["crowdstrike"],
}

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "breaking": ["breaking", "exclusive", "urgent", "just in", "developing"],
    "analyst_upgrade": ["upgrade", "upgrades", "upgraded", "raises price target", "price target raised", "initiates buy", "outperform rating", "buy rating"],
    "analyst_downgrade": ["downgrade", "downgrades", "downgraded", "cuts price target", "price target cut", "initiates sell", "underperform rating", "sell rating"],
    "earnings": ["earnings", "eps", "revenue", "sales", "guidance", "quarter", "q1", "q2", "q3", "q4", "fiscal"],
    "government": ["white house", "trump", "biden", "tariff", "tariffs", "sanction", "sanctions", "congress", "senate", "administration"],
    "fed": ["fed", "federal reserve", "powell", "fomc", "rate cut", "rate hike", "interest rates", "cpi", "inflation", "jobs report", "payrolls"],
    "ai": ["artificial intelligence", " ai ", "generative ai", "openai", "anthropic", "chatgpt", "data center", "data centers"],
    "semiconductors": ["semiconductor", "semiconductors", "chips", "chipmaker", "gpu", "foundry", "wafer"],
    "ma": ["acquire", "acquires", "acquisition", "merger", "takeover", "buyout", "deal talks", "strategic alternatives"],
    "sec": ["sec", "8-k", "10-k", "10-q", "s-1", "filing", "subpoena", "investigation"],
    "lawsuit": ["lawsuit", "sues", "sued", "settlement", "antitrust", "probe", "class action"],
    "social": ["tweet", "tweets", "posts on x", "truth social", "elon musk", "donald trump", "posted", "says on x"],
    "options": ["option", "options", "call sweep", "put sweep", "unusual options", "options activity"],
    "fda": ["fda", "phase 1", "phase 2", "phase 3", "clinical trial", "approval", "pdufa", "drug application"],
    "crypto": ["bitcoin", "ethereum", "crypto", "coinbase", "sec crypto", "etf approval"],
    "energy": ["oil", "crude", "opec", "natural gas", "energy prices"],
}

BULLISH_WORDS = [
    "beats", "beat", "surges", "jumps", "rises", "gains", "upgrade", "upgraded", "raises", "raised",
    "buy rating", "strong", "record", "approval", "approved", "partnership", "contract", "launch",
    "bullish", "outperform", "profit rises", "guidance raised", "raises guidance", "wins", "awarded",
]
BEARISH_WORDS = [
    "misses", "falls", "drops", "plunges", "cuts", "cut", "downgrade", "downgraded", "sell rating",
    "weak", "lawsuit", "probe", "investigation", "recall", "delay", "delayed", "tariff", "bearish",
    "underperform", "bankruptcy", "offering", "secondary offering", "guidance cut", "cuts guidance",
]
HIGH_IMPACT_WORDS = [
    "breaking", "exclusive", "halts", "halted", "sec", "fda", "fomc", "powell", "trump", "tariff",
    "earnings", "guidance", "upgrade", "downgrade", "acquisition", "merger", "bankruptcy", "offering",
    "approval", "denied", "investigation", "lawsuit", "record high", "record low",
]

CATEGORY_WEIGHTS = {
    "breaking": 25,
    "analyst_upgrade": 25,
    "analyst_downgrade": 25,
    "earnings": 25,
    "government": 30,
    "fed": 35,
    "ai": 12,
    "semiconductors": 12,
    "ma": 30,
    "sec": 20,
    "lawsuit": 18,
    "social": 20,
    "options": 16,
    "fda": 25,
    "crypto": 12,
    "energy": 10,
}


@dataclass
class AnalysisResult:
    tickers: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    sentiment: str = "neutral"
    impact_score: int = 0
    watchlist_hit: bool = False
    priority: str = "normal"
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tickers": self.tickers,
            "categories": self.categories,
            "sentiment": self.sentiment,
            "impact_score": self.impact_score,
            "watchlist_hit": self.watchlist_hit,
            "priority": self.priority,
            "reason": self.reason,
        }


def load_project_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_watchlist() -> list[str]:
    cfg = load_project_config()
    watchlist = cfg.get("watchlist", DEFAULT_WATCHLIST)
    if not isinstance(watchlist, list):
        watchlist = DEFAULT_WATCHLIST
    return normalize_tickers(watchlist)


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_symbol(value: str) -> str:
    return str(value or "").strip().upper().replace("$", "")


def normalize_symbols(values: Iterable[Any] | None) -> list[str]:
    symbols: list[str] = []
    for item in values or []:
        if isinstance(item, dict):
            raw = item.get("name") or item.get("ticker") or item.get("symbol") or item.get("security") or ""
        else:
            raw = str(item)
        symbol = normalize_symbol(raw)
        if symbol and re.match(r"^[A-Z][A-Z0-9.]{0,9}$", symbol) and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def extract_api_tickers_from_raw(raw_json: dict[str, Any] | None) -> list[str]:
    if not isinstance(raw_json, dict):
        return []
    candidates: list[Any] = []
    for key in ("stocks", "securities", "tickers", "symbols"):
        val = raw_json.get(key)
        if isinstance(val, list):
            candidates.extend(val)
        elif isinstance(val, str):
            candidates.extend([x.strip() for x in re.split(r"[,;\s]+", val) if x.strip()])
    channels = raw_json.get("channels")
    if isinstance(channels, list):
        for channel in channels:
            if isinstance(channel, dict):
                name = channel.get("name") or channel.get("label") or ""
                if re.match(r"^[A-Z]{1,5}$", str(name)):
                    candidates.append(name)
    return normalize_symbols(candidates)


def extract_dollar_tickers(text: str) -> list[str]:
    tickers = re.findall(r"\$([A-Za-z][A-Za-z0-9.]{0,9})\b", text or "")
    return normalize_symbols(tickers)


def extract_uppercase_ticker_mentions(text: str, watchlist: list[str]) -> list[str]:
    """Detect standalone uppercase watchlist tickers only. Avoid broad all-caps extraction."""
    found: list[str] = []
    watch_set = set(watchlist)
    for ticker in watchlist:
        if len(ticker) < 2:
            continue
        if re.search(rf"(?<![A-Za-z0-9$]){re.escape(ticker)}(?![A-Za-z0-9])", text or ""):
            if ticker in watch_set and ticker not in found:
                found.append(ticker)
    return found


def extract_alias_tickers(text: str, watchlist: list[str]) -> list[str]:
    lower = f" {text.lower()} "
    found: list[str] = []
    watch_set = set(watchlist)
    for ticker, aliases in COMPANY_ALIASES.items():
        if ticker not in watch_set:
            continue
        for alias in aliases:
            alias_l = alias.lower()
            if " " in alias_l:
                hit = alias_l in lower
            else:
                hit = re.search(rf"\b{re.escape(alias_l)}\b", lower) is not None
            if hit:
                if ticker not in found:
                    found.append(ticker)
                break
    return found


def extract_tickers(headline: str, summary: str = "", raw_json: dict[str, Any] | None = None, watchlist: list[str] | None = None) -> list[str]:
    watchlist = normalize_tickers(watchlist or load_watchlist())
    headline_text = headline or ""
    tickers: list[str] = []
    for symbol in (
        extract_dollar_tickers(headline_text)
        + extract_uppercase_ticker_mentions(headline_text, watchlist)
        + extract_alias_tickers(headline_text, watchlist)
    ):
        if symbol and symbol not in tickers:
            tickers.append(symbol)
    return tickers


def detect_categories(headline: str, summary: str = "") -> list[str]:
    text = f" {headline} {strip_html(summary)} ".lower()
    categories: list[str] = []
    for category, words in CATEGORY_KEYWORDS.items():
        if any(word.lower() in text for word in words):
            categories.append(category)
    if not categories:
        categories.append("general")
    return categories


def detect_sentiment(headline: str, summary: str = "", categories: list[str] | None = None) -> str:
    categories = categories or []
    text = f" {headline} {strip_html(summary)} ".lower()
    bullish = sum(1 for word in BULLISH_WORDS if word in text)
    bearish = sum(1 for word in BEARISH_WORDS if word in text)

    if "analyst_upgrade" in categories:
        bullish += 2
    if "analyst_downgrade" in categories:
        bearish += 2
    if "lawsuit" in categories or "sec" in categories:
        bearish += 1
    if "fda" in categories and any(w in text for w in ["approval", "approved", "accepts", "clearance"]):
        bullish += 2
    if "fda" in categories and any(w in text for w in ["reject", "rejected", "complete response", "delay"]):
        bearish += 2

    if bullish > bearish:
        return "bullish"
    if bearish > bullish:
        return "bearish"
    return "neutral"


def calculate_impact_score(
    headline: str,
    summary: str = "",
    tickers: list[str] | None = None,
    categories: list[str] | None = None,
    watchlist_hit: bool = False,
) -> int:
    tickers = tickers or []
    categories = categories or []
    text = f" {headline} {strip_html(summary)} ".lower()
    score = 20

    if tickers:
        score += 15
    if watchlist_hit:
        score += 15

    for category in categories:
        score += CATEGORY_WEIGHTS.get(category, 0)

    score += min(20, sum(4 for word in HIGH_IMPACT_WORDS if word in text))

    if len(tickers) >= 3:
        score += 5
    if len(tickers) >= 5:
        score += 5
    if len(headline) <= 110 and any(w in text for w in ["breaking", "halts", "surges", "plunges"]):
        score += 8

    return max(0, min(100, int(score)))


def impact_priority(score: int, watchlist_hit: bool = False) -> str:
    if score >= 85 and watchlist_hit:
        return "critical"
    if score >= 80:
        return "high"
    if score >= 60:
        return "medium"
    return "normal"


def build_reason(categories: list[str], sentiment: str, impact: int, watchlist_hit: bool) -> str:
    main = categories[0] if categories else "general"
    bits = [main.replace("_", " ").title(), sentiment.title(), f"Impact {impact}"]
    if watchlist_hit:
        bits.append("Watchlist hit")
    return " | ".join(bits)


def analyze_text(
    headline: str,
    summary: str = "",
    raw_json: dict[str, Any] | None = None,
    watchlist: list[str] | None = None,
) -> AnalysisResult:
    watchlist = normalize_tickers(watchlist or load_watchlist())
    tickers = extract_tickers(headline, summary, raw_json, watchlist)
    categories = detect_categories(headline, summary)
    sentiment = detect_sentiment(headline, summary, categories)
    watchlist_hit = bool(set(tickers) & set(watchlist))
    impact = calculate_impact_score(headline, summary, tickers, categories, watchlist_hit)
    priority = impact_priority(impact, watchlist_hit)
    reason = build_reason(categories, sentiment, impact, watchlist_hit)
    return AnalysisResult(
        tickers=tickers,
        categories=categories,
        sentiment=sentiment,
        impact_score=impact,
        watchlist_hit=watchlist_hit,
        priority=priority,
        reason=reason,
    )


def analyze_article_row(article: dict[str, Any], watchlist: list[str] | None = None) -> AnalysisResult:
    raw_json = article.get("raw_json")
    if isinstance(raw_json, str):
        raw_json = _json_loads(raw_json, {})
    return analyze_text(
        headline=safe_text(article.get("headline")),
        summary=safe_text(article.get("summary")),
        raw_json=raw_json if isinstance(raw_json, dict) else {},
        watchlist=watchlist,
    )


class NewsAnalyzer:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH, watchlist: list[str] | None = None):
        self.db = NewsDatabase(db_path)
        self.watchlist = normalize_tickers(watchlist or load_watchlist())

    def analyze_recent(self, limit: int = 250, min_impact: int | None = None) -> dict[str, Any]:
        articles = self.db.get_recent_articles(limit=limit, min_impact=min_impact)
        return self._analyze_and_update(articles)

    def analyze_all(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM articles ORDER BY published_at DESC").fetchall()
            articles = [self.db._row_to_article_dict(row) for row in rows]
        return self._analyze_and_update(articles)

    def analyze_unscored(self, limit: int = 500) -> dict[str, Any]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM articles
                WHERE impact_score IS NULL OR impact_score = 0 OR categories_json = '[]'
                ORDER BY published_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            articles = [self.db._row_to_article_dict(row) for row in rows]
        return self._analyze_and_update(articles)

    def _analyze_and_update(self, articles: list[dict[str, Any]]) -> dict[str, Any]:
        updated = 0
        watchlist_hits = 0
        high_impact = 0
        for article in articles:
            article_id = int(article["id"])
            result = analyze_article_row(article, self.watchlist)
            self.update_article_analysis(article_id, result)
            updated += 1
            if result.watchlist_hit:
                watchlist_hits += 1
            if result.impact_score >= 80:
                high_impact += 1
        return {
            "articles_analyzed": updated,
            "watchlist_hits": watchlist_hits,
            "high_impact": high_impact,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def update_article_analysis(self, article_id: int, result: AnalysisResult) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE articles
                SET tickers_json = ?,
                    categories_json = ?,
                    sentiment = ?,
                    impact_score = ?,
                    watchlist_hit = ?
                WHERE id = ?
                """,
                (
                    _json_dumps(result.tickers),
                    _json_dumps(result.categories),
                    result.sentiment,
                    int(result.impact_score),
                    int(result.watchlist_hit),
                    int(article_id),
                ),
            )
            # Refresh watchlist_hits table for this article.
            conn.execute("DELETE FROM watchlist_hits WHERE article_id = ?", (int(article_id),))
            for ticker in result.tickers:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO watchlist_hits (article_id, ticker, hit_type, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (int(article_id), ticker, "direct" if ticker in self.watchlist else "detected", now),
                )

    def preview(self, limit: int = 20) -> list[dict[str, Any]]:
        articles = self.db.get_recent_articles(limit=limit)
        out: list[dict[str, Any]] = []
        for article in articles:
            result = analyze_article_row(article, self.watchlist)
            out.append({
                "id": article.get("id"),
                "published_at": article.get("published_at"),
                "headline": article.get("headline"),
                **result.as_dict(),
            })
        return out


def demo() -> None:
    examples = [
        "BREAKING: Tesla shares rise after Elon Musk says robotaxi launch is on track",
        "Morgan Stanley upgrades NVIDIA, raises price target on strong AI demand",
        "Trump says new tariffs on Chinese semiconductor imports are being considered",
        "Apple falls after analyst cuts price target citing iPhone demand weakness",
        "FDA approves new treatment from Hims partner company",
    ]
    watchlist = load_watchlist()
    for headline in examples:
        result = analyze_text(headline, watchlist=watchlist)
        print("\nHEADLINE:", headline)
        print(json.dumps(result.as_dict(), indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="PulseTrade AI News Analyzer")
    parser.add_argument("--demo", action="store_true", help="Run demo analysis examples")
    parser.add_argument("--analyze-recent", type=int, default=0, help="Analyze and update the latest N articles")
    parser.add_argument("--analyze-all", action="store_true", help="Analyze and update all articles")
    parser.add_argument("--analyze-unscored", type=int, default=0, help="Analyze articles with missing/zero scores")
    parser.add_argument("--preview", type=int, default=0, help="Preview latest N analyses without updating")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to news.db")
    args = parser.parse_args()

    if args.demo:
        demo()
        return

    analyzer = NewsAnalyzer(db_path=args.db)

    if args.analyze_all:
        print(json.dumps(analyzer.analyze_all(), indent=2))
        return

    if args.analyze_recent:
        print(json.dumps(analyzer.analyze_recent(args.analyze_recent), indent=2))
        return

    if args.analyze_unscored:
        print(json.dumps(analyzer.analyze_unscored(args.analyze_unscored), indent=2))
        return

    if args.preview:
        print(json.dumps(analyzer.preview(args.preview), indent=2, default=str))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
