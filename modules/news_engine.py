"""
PulseTrade AI - News Engine
Benzinga REST news ingestion for Market Intelligence.

Place this file in:
    modules/news_engine.py

Requires:
    modules/news_database.py

Creates/uses:
    data/news.db

Environment variables supported:
    BENZINGA_API_KEY
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

Config.json support:
    {
      "news": {
        "enabled": true,
        "provider": "benzinga",
        "benzinga_api_key": "YOUR_KEY",
        "poll_interval_seconds": 30,
        "lookback_minutes": 60,
        "only_watchlist_alerts": true,
        "min_impact_for_alert": 70,
        "send_telegram_alerts": true,
        "channels": [],
        "tickers": []
      }
    }

Run one poll:
    python modules/news_engine.py --once

Run continuous engine:
    python modules/news_engine.py

Dry run without saving:
    python modules/news_engine.py --once --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

# Allow this module to run both as "python modules/news_engine.py" and as an import.
THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = THIS_FILE.parents[1]
MODULES_DIR = THIS_FILE.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(MODULES_DIR) not in sys.path:
    sys.path.insert(0, str(MODULES_DIR))

try:
    from news_database import NewsArticle, NewsDatabase, initialize_news_database, utc_now_iso
except Exception as exc:  # pragma: no cover
    raise RuntimeError("Could not import modules/news_database.py. Make sure it exists and compiles.") from exc


CONFIG_PATH = PROJECT_ROOT / "config.json"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
NEWS_LOG_FILE = LOG_DIR / "news_engine.log"

BENZINGA_NEWS_URL = "https://api.benzinga.com/api/v2/news"
SOURCE_NAME = "benzinga"

DEFAULT_WATCHLIST = [
    "SPY", "QQQ", "IWM",
    "NVDA", "AAPL", "MSFT", "META", "AMZN", "GOOGL",
    "TSLA", "AMD", "PLTR", "COIN", "MSTR",
    "AVGO", "SMCI", "MU", "ARM", "TSM", "MRVL",
    "JPM", "GS", "BAC", "NFLX", "UBER", "XOM", "COST",
    "RBLX", "HOOD", "SOFI", "RKLB", "HIMS", "CRWD",
]

COMPANY_ALIASES: dict[str, list[str]] = {
    "TSLA": ["tesla", "robotaxi", "cybertruck", "model y", "model 3", "elon musk"],
    "NVDA": ["nvidia", "jensen huang", "gpu", "blackwell", "cuda"],
    "AMD": ["advanced micro devices", "amd", "radeon", "epyc", "ryzen"],
    "AAPL": ["apple", "iphone", "ipad", "mac", "vision pro", "tim cook"],
    "MSFT": ["microsoft", "azure", "satya nadella"],
    "GOOGL": ["google", "alphabet", "gemini", "youtube"],
    "META": ["meta", "facebook", "instagram", "zuckerberg", "threads"],
    "AMZN": ["amazon", "aws", "andy jassy"],
    "PLTR": ["palantir", "alex karp"],
    "COIN": ["coinbase", "crypto exchange"],
    "MSTR": ["microstrategy", "strategy", "michael saylor", "bitcoin treasury"],
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
    "breaking": ["breaking", "exclusive", "urgent", "just in"],
    "analyst_upgrade": ["upgrade", "upgrades", "raises price target", "price target raised", "initiates buy", "bullish rating"],
    "analyst_downgrade": ["downgrade", "downgrades", "cuts price target", "price target cut", "initiates sell", "bearish rating"],
    "earnings": ["earnings", "eps", "revenue", "guidance", "quarter", "q1", "q2", "q3", "q4"],
    "government": ["white house", "trump", "biden", "tariff", "tariffs", "sanction", "sanctions", "congress", "senate"],
    "fed": ["fed", "federal reserve", "powell", "fomc", "rate cut", "rate hike", "interest rates"],
    "ai": ["artificial intelligence", " ai ", "generative ai", "openai", "anthropic", "chatgpt", "data center"],
    "semiconductors": ["semiconductor", "semiconductors", "chips", "chipmaker", "gpu", "foundry"],
    "ma": ["acquire", "acquisition", "merger", "takeover", "buyout", "deal"],
    "sec": ["sec", "8-k", "10-k", "10-q", "s-1", "filing", "investigation"],
    "lawsuit": ["lawsuit", "sues", "sued", "settlement", "antitrust", "probe"],
    "social": ["tweet", "posts on x", "truth social", "elon musk", "donald trump", "posted", "says on x"],
    "options": ["option", "options", "call sweep", "put sweep", "unusual options", "uoa"],
}

BULLISH_WORDS = [
    "beats", "beat", "surges", "jumps", "rises", "gains", "upgrade", "raises", "buy rating",
    "strong", "record", "approval", "partnership", "contract", "launch", "bullish", "outperform",
]
BEARISH_WORDS = [
    "misses", "falls", "drops", "plunges", "cuts", "downgrade", "sell rating", "weak",
    "lawsuit", "probe", "investigation", "recall", "delay", "tariff", "bearish", "underperform",
]
HIGH_IMPACT_WORDS = [
    "breaking", "exclusive", "halts", "halted", "sec", "fda", "fomc", "powell", "trump", "tariff",
    "earnings", "guidance", "upgrade", "downgrade", "acquisition", "merger", "bankruptcy", "offering",
]


@dataclass
class NewsEngineConfig:
    enabled: bool = True
    api_key: str = ""
    poll_interval_seconds: int = 30
    lookback_minutes: int = 60
    page_size: int = 50
    channels: list[str] = field(default_factory=list)
    tickers: list[str] = field(default_factory=list)
    watchlist: list[str] = field(default_factory=list)
    only_watchlist_alerts: bool = True
    min_impact_for_alert: int = 70
    send_telegram_alerts: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    request_timeout_seconds: int = 15


def log(message: str, level: str = "INFO") -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} | {level} | {message}"
    print(line, flush=True)
    try:
        with NEWS_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_config() -> NewsEngineConfig:
    cfg = load_json_file(CONFIG_PATH)
    news_cfg = cfg.get("news", {}) if isinstance(cfg.get("news", {}), dict) else {}
    telegram_cfg = cfg.get("telegram", {}) if isinstance(cfg.get("telegram", {}), dict) else {}
    watchlist = cfg.get("watchlist", DEFAULT_WATCHLIST)

    if not isinstance(watchlist, list):
        watchlist = DEFAULT_WATCHLIST

    configured_tickers = news_cfg.get("tickers", [])
    if not isinstance(configured_tickers, list):
        configured_tickers = []

    channels = news_cfg.get("channels", [])
    if not isinstance(channels, list):
        channels = []

    return NewsEngineConfig(
        enabled=bool(news_cfg.get("enabled", True)),
        api_key=str(
            news_cfg.get("benzinga_api_key")
            or news_cfg.get("api_key")
            or os.getenv("BENZINGA_API_KEY", "")
        ).strip(),
        poll_interval_seconds=int(news_cfg.get("poll_interval_seconds", 30)),
        lookback_minutes=int(news_cfg.get("lookback_minutes", 60)),
        page_size=int(news_cfg.get("page_size", 50)),
        channels=[str(x).strip() for x in channels if str(x).strip()],
        tickers=[str(x).strip().upper().replace("$", "") for x in configured_tickers if str(x).strip()],
        watchlist=[str(x).strip().upper().replace("$", "") for x in watchlist if str(x).strip()],
        only_watchlist_alerts=bool(news_cfg.get("only_watchlist_alerts", True)),
        min_impact_for_alert=int(news_cfg.get("min_impact_for_alert", 70)),
        send_telegram_alerts=bool(news_cfg.get("send_telegram_alerts", telegram_cfg.get("send_alerts", False))),
        telegram_bot_token=str(telegram_cfg.get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip(),
        telegram_chat_id=str(telegram_cfg.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID", "")).strip(),
        request_timeout_seconds=int(news_cfg.get("request_timeout_seconds", 15)),
    )


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


def parse_datetime(value: Any) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()

    if isinstance(value, (int, float)):
        try:
            # Benzinga values may be Unix seconds or milliseconds depending on feed/provider.
            ts = float(value)
            if ts > 10_000_000_000:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except Exception:
            return datetime.now(timezone.utc).isoformat()

    raw = str(value).strip()
    if not raw:
        return datetime.now(timezone.utc).isoformat()

    # Common API timestamp formats.
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z",
        "%a, %d %b %Y %H:%M:%S %z",
    ):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            pass

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def article_external_id(item: dict[str, Any]) -> str:
    for key in ("id", "uuid", "news_id", "story_id", "article_id"):
        if item.get(key):
            return str(item[key])
    raw = "|".join([
        safe_text(item.get("created") or item.get("updated") or item.get("published") or item.get("date")),
        safe_text(item.get("title") or item.get("headline")),
        safe_text(item.get("url")),
    ])
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:32]


def extract_text_fields(item: dict[str, Any]) -> tuple[str, str, str]:
    headline = safe_text(item.get("title") or item.get("headline") or item.get("name"))
    summary = safe_text(
        item.get("teaser")
        or item.get("summary")
        or item.get("body")
        or item.get("content")
        or item.get("description")
    )
    summary = strip_html(summary)
    url = safe_text(item.get("url") or item.get("link") or item.get("share_url"))
    return headline, summary, url


def extract_api_tickers(item: dict[str, Any]) -> list[str]:
    # Benzinga fields may include stocks, securities, tickers, symbols depending on package/partner.
    candidates: list[Any] = []
    for key in ("stocks", "securities", "tickers", "symbols"):
        val = item.get(key)
        if isinstance(val, list):
            candidates.extend(val)
        elif isinstance(val, str):
            candidates.extend([x.strip() for x in re.split(r"[,;\s]+", val) if x.strip()])

    # Some APIs return channels with tickers embedded.
    channels = item.get("channels")
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


def extract_alias_tickers(text: str, watchlist: list[str]) -> list[str]:
    lower = f" {text.lower()} "
    found: list[str] = []
    watch_set = set(watchlist)
    for ticker, aliases in COMPANY_ALIASES.items():
        if ticker not in watch_set:
            continue
        for alias in aliases:
            if f" {alias.lower()} " in lower or alias.lower() in lower:
                if ticker not in found:
                    found.append(ticker)
                break
    return found


def extract_all_tickers(item: dict[str, Any], watchlist: list[str]) -> list[str]:
    headline, summary, _ = extract_text_fields(item)
    text = f"{headline} {summary}"
    tickers = []
    for symbol in extract_api_tickers(item) + extract_dollar_tickers(text) + extract_alias_tickers(text, watchlist):
        if symbol and symbol not in tickers:
            tickers.append(symbol)
    return tickers


def detect_categories(headline: str, summary: str) -> list[str]:
    text = f" {headline} {summary} ".lower()
    categories: list[str] = []
    for category, words in CATEGORY_KEYWORDS.items():
        if any(word.lower() in text for word in words):
            categories.append(category)
    if not categories:
        categories.append("general")
    return categories


def detect_sentiment(headline: str, summary: str, categories: list[str]) -> str:
    text = f" {headline} {summary} ".lower()
    bullish = sum(1 for word in BULLISH_WORDS if word in text)
    bearish = sum(1 for word in BEARISH_WORDS if word in text)

    if "analyst_upgrade" in categories:
        bullish += 2
    if "analyst_downgrade" in categories:
        bearish += 2
    if "lawsuit" in categories or "sec" in categories:
        bearish += 1

    if bullish > bearish:
        return "bullish"
    if bearish > bullish:
        return "bearish"
    return "neutral"


def calculate_impact_score(headline: str, summary: str, tickers: list[str], categories: list[str], watchlist_hit: bool) -> int:
    text = f" {headline} {summary} ".lower()
    score = 20

    if tickers:
        score += 15
    if watchlist_hit:
        score += 15

    category_weights = {
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
    }
    for category in categories:
        score += category_weights.get(category, 0)

    score += min(20, sum(4 for word in HIGH_IMPACT_WORDS if word in text))

    if len(tickers) >= 3:
        score += 5
    if len(headline) <= 100 and any(w in text for w in ["breaking", "halts", "surges", "plunges"]):
        score += 8

    return max(0, min(100, int(score)))


def normalize_benzinga_item(item: dict[str, Any], watchlist: list[str]) -> NewsArticle | None:
    headline, summary, url = extract_text_fields(item)
    if not headline:
        return None

    tickers = extract_all_tickers(item, watchlist)
    watchlist_hit = bool(set(tickers) & set(watchlist))
    categories = detect_categories(headline, summary)
    sentiment = detect_sentiment(headline, summary, categories)
    impact = calculate_impact_score(headline, summary, tickers, categories, watchlist_hit)

    published_raw = (
        item.get("created")
        or item.get("published")
        or item.get("date")
        or item.get("updated")
        or item.get("created_at")
        or item.get("published_at")
    )

    author = safe_text(item.get("author") or item.get("created_by") or item.get("source") or "Benzinga")

    return NewsArticle(
        external_id=article_external_id(item),
        source=SOURCE_NAME,
        published_at=parse_datetime(published_raw),
        headline=headline,
        summary=summary,
        url=url,
        author=author,
        tickers=tickers,
        categories=categories,
        sentiment=sentiment,
        impact_score=impact,
        watchlist_hit=watchlist_hit,
        raw_json=item,
    )


class BenzingaClient:
    def __init__(self, api_key: str, timeout_seconds: int = 15):
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "PulseTradeAI/0.6"})

    def fetch_news(
        self,
        *,
        page_size: int = 50,
        tickers: list[str] | None = None,
        channels: list[str] | None = None,
        updated_since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if not self.api_key:
            raise RuntimeError("Missing Benzinga API key. Set BENZINGA_API_KEY or config.json news.benzinga_api_key.")

        params: dict[str, Any] = {
            "token": self.api_key,
            "pageSize": int(page_size),
            "displayOutput": "full",
        }
        if tickers:
            params["tickers"] = ",".join(tickers)
        if channels:
            params["channels"] = ",".join(channels)
        if updated_since:
            # Benzinga News API supports updatedSince for delta-style queries.
            params["updatedSince"] = updated_since.replace(microsecond=0).isoformat().replace("+00:00", "Z")

        response = self.session.get(BENZINGA_NEWS_URL, params=params, timeout=self.timeout_seconds)
        response.raise_for_status()
        data = response.json()

        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("data", "news", "articles", "items", "results"):
                value = data.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
            # Some endpoints return a single article object.
            if any(k in data for k in ["title", "headline"]):
                return [data]
        return []


def telegram_escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def make_telegram_message(article: NewsArticle) -> str:
    tickers = ", ".join(article.tickers or []) or "None"
    categories = ", ".join(article.categories or []) or "general"
    icon = "🚨" if article.impact_score >= 80 else "⚡" if article.impact_score >= 60 else "📰"
    headline = telegram_escape(article.headline)
    summary = telegram_escape(article.summary[:350])
    url = telegram_escape(article.url)

    msg = (
        f"{icon} <b>PulseTrade AI Market Intelligence</b>\n\n"
        f"<b>{headline}</b>\n\n"
        f"Tickers: <b>{telegram_escape(tickers)}</b>\n"
        f"Sentiment: <b>{telegram_escape(article.sentiment.upper())}</b>\n"
        f"Impact: <b>{article.impact_score}/100</b>\n"
        f"Category: {telegram_escape(categories)}\n"
        f"Source: Benzinga\n"
    )
    if summary:
        msg += f"\n{summary}\n"
    if url:
        msg += f"\n{url}"
    return msg


def send_telegram(bot_token: str, chat_id: str, message: str) -> tuple[bool, str]:
    if not bot_token or not chat_id:
        return False, "Telegram token/chat_id missing"
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": False}
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            return True, "sent"
        return False, f"HTTP {r.status_code}: {r.text[:300]}"
    except Exception as exc:
        return False, str(exc)


@dataclass
class PollResult:
    seen: int = 0
    inserted: int = 0
    duplicates: int = 0
    watchlist_hits: int = 0
    alerts_sent: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seen": self.seen,
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "watchlist_hits": self.watchlist_hits,
            "alerts_sent": self.alerts_sent,
            "errors": self.errors,
        }


class NewsEngine:
    def __init__(self, config: NewsEngineConfig | None = None, db: NewsDatabase | None = None):
        self.config = config or load_config()
        self.db = db or initialize_news_database()
        self.client = BenzingaClient(self.config.api_key, self.config.request_timeout_seconds)
        self.db.upsert_source(SOURCE_NAME, "benzinga", enabled=self.config.enabled, config={
            "page_size": self.config.page_size,
            "poll_interval_seconds": self.config.poll_interval_seconds,
            "channels": self.config.channels,
            "tickers": self.config.tickers,
        })

    def get_query_tickers(self) -> list[str]:
        # If specific Benzinga query tickers are configured, use those. Otherwise pull general news.
        return self.config.tickers

    def should_alert(self, article: NewsArticle) -> bool:
        if not self.config.send_telegram_alerts:
            return False
        if article.impact_score < self.config.min_impact_for_alert:
            return False
        if self.config.only_watchlist_alerts and not article.watchlist_hit:
            return False
        return True

    def poll_once(self, dry_run: bool = False) -> PollResult:
        result = PollResult()
        started = utc_now_iso()
        self.db.update_health(engine_running=True, last_poll_started_at=started, last_status="Polling Benzinga", last_error="")

        try:
            since = datetime.now(timezone.utc) - timedelta(minutes=max(1, int(self.config.lookback_minutes)))
            raw_items = self.client.fetch_news(
                page_size=self.config.page_size,
                tickers=self.get_query_tickers(),
                channels=self.config.channels,
                updated_since=since,
            )
            result.seen = len(raw_items)

            for item in raw_items:
                try:
                    article = normalize_benzinga_item(item, self.config.watchlist)
                    if article is None:
                        continue

                    if dry_run:
                        result.inserted += 1
                        if article.watchlist_hit:
                            result.watchlist_hits += 1
                        continue

                    inserted, article_id = self.db.insert_article(article)
                    if not inserted:
                        result.duplicates += 1
                        continue

                    result.inserted += 1
                    if article.watchlist_hit:
                        result.watchlist_hits += 1

                    if article_id and self.should_alert(article):
                        ok, status = send_telegram(
                            self.config.telegram_bot_token,
                            self.config.telegram_chat_id,
                            make_telegram_message(article),
                        )
                        if ok:
                            result.alerts_sent += 1
                            self.db.mark_telegram_sent(article_id, destination="telegram", status="sent")
                        else:
                            self.db.mark_telegram_sent(article_id, destination="telegram", status="failed", error=status)
                            result.errors.append(f"Telegram failed for article {article_id}: {status}")

                except Exception as item_exc:
                    result.errors.append(f"Item error: {item_exc}")

            status = f"Poll complete | seen={result.seen} inserted={result.inserted} duplicates={result.duplicates} hits={result.watchlist_hits} alerts={result.alerts_sent}"
            self.db.update_health(
                engine_running=True,
                last_poll_finished_at=utc_now_iso(),
                last_status=status,
                articles_seen=result.seen,
                articles_inserted=result.inserted,
                watchlist_hits=result.watchlist_hits,
                alerts_sent=result.alerts_sent,
                last_error="; ".join(result.errors[-3:]) if result.errors else "",
            )
            log(status)
            return result

        except Exception as exc:
            err = traceback.format_exc()
            result.errors.append(str(exc))
            self.db.update_health(
                engine_running=True,
                last_poll_finished_at=utc_now_iso(),
                last_status="Poll failed",
                last_error=err,
            )
            log("News poll failed:\n" + err, "ERROR")
            return result

    def run_forever(self) -> None:
        log("News engine started.")
        self.db.update_health(engine_running=True, last_status="News engine started")
        while True:
            cfg = load_config()
            self.config = cfg
            if not cfg.enabled:
                self.db.update_health(engine_running=True, last_status="News engine disabled")
                log("News engine disabled in config. Heartbeat only.")
                time.sleep(max(30, cfg.poll_interval_seconds))
                continue

            self.poll_once(dry_run=False)
            time.sleep(max(5, int(cfg.poll_interval_seconds)))


def main() -> None:
    parser = argparse.ArgumentParser(description="PulseTrade AI Benzinga News Engine")
    parser.add_argument("--once", action="store_true", help="Run a single Benzinga poll and exit")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and analyze but do not save or send alerts")
    parser.add_argument("--test-db", action="store_true", help="Initialize database and print stats")
    parser.add_argument("--print-config", action="store_true", help="Print loaded news configuration without API key")
    args = parser.parse_args()

    if args.test_db:
        db = initialize_news_database()
        print(f"Database: {db.db_path}")
        print(json.dumps(db.get_stats(), indent=2, default=str))
        return

    cfg = load_config()
    if args.print_config:
        safe = cfg.__dict__.copy()
        safe["api_key"] = "SET" if cfg.api_key else "MISSING"
        safe["telegram_bot_token"] = "SET" if cfg.telegram_bot_token else "MISSING"
        print(json.dumps(safe, indent=2, default=str))
        return

    engine = NewsEngine(cfg)
    if args.once:
        result = engine.poll_once(dry_run=args.dry_run)
        print(json.dumps(result.as_dict(), indent=2, default=str))
    else:
        engine.run_forever()


if __name__ == "__main__":
    main()
