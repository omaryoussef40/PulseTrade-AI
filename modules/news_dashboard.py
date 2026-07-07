"""
PulseTrade AI - Market Intelligence Dashboard
Streamlit UI module for Benzinga / Market Intelligence news stored in SQLite.

Place this file in:
    modules/news_dashboard.py

Requires:
    modules/news_database.py
    modules/news_analyzer.py

Usage inside dashboard.py:
    from modules.news_dashboard import render_market_intelligence_tab

    # Add a Streamlit tab, then call:
    with tab_market:
        render_market_intelligence_tab()

This module is intentionally read-only by default. It does not touch the trading
engine, IBKR orders, positions, or risk logic.
"""

from __future__ import annotations

import html
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go

try:
    import streamlit as st
except Exception:  # Allows py_compile in non-Streamlit environments.
    st = None  # type: ignore


THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = THIS_FILE.parents[1]
MODULES_DIR = THIS_FILE.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(MODULES_DIR) not in sys.path:
    sys.path.insert(0, str(MODULES_DIR))

try:
    from news_database import DEFAULT_DB_PATH, NewsDatabase, initialize_news_database
except Exception as exc:  # pragma: no cover
    raise RuntimeError("Could not import modules/news_database.py") from exc

try:
    from news_analyzer import NewsAnalyzer
except Exception:
    NewsAnalyzer = None  # type: ignore

try:
    from news_control import (
        get_news_engine_status,
        start_news_engine,
        stop_news_engine,
        run_news_poll_once,
        tail_news_process_log,
    )
except Exception:
    get_news_engine_status = None  # type: ignore
    start_news_engine = None  # type: ignore
    stop_news_engine = None  # type: ignore
    run_news_poll_once = None  # type: ignore
    tail_news_process_log = None  # type: ignore


# -----------------------------------------------------------------------------
# Styling
# -----------------------------------------------------------------------------

MARKET_INTELLIGENCE_CSS = """
<style>
.market-card {
    border: 1px solid rgba(31,41,55,0.16);
    border-radius: 8px;
    padding: 9px 10px;
    background: #ffffff;
    color: #111827;
    margin-bottom: 8px;
}
.market-card-high {
    border-left: 5px solid #f59e0b;
}
.market-card-watchlist {
    border-left: 5px solid #22c55e;
}
.market-card-bearish {
    border-left: 5px solid #ef4444;
}
.news-meta {
    font-size: 0.68rem;
    color: #6b7280;
    margin-bottom: 4px;
}
.news-headline {
    font-size: 0.86rem;
    font-weight: 750;
    line-height: 1.24;
    margin-bottom: 5px;
    color: #111827;
}
.news-summary {
    font-size: 0.74rem;
    color: #374151;
    line-height: 1.34;
    margin-bottom: 6px;
}
.news-pill {
    display: inline-block;
    border-radius: 999px;
    padding: 2px 6px;
    margin: 2px 4px 2px 0;
    font-size: 0.64rem;
    font-weight: 700;
    border: 1px solid rgba(31,41,55,0.14);
    background: rgba(31,41,55,0.055);
    color: #374151;
}
.news-pill-green { color: #166534; background: rgba(34,197,94,0.14); }
.news-pill-red { color: #991b1b; background: rgba(239,68,68,0.14); }
.news-pill-yellow { color: #92400e; background: rgba(245,158,11,0.18); }
.news-pill-blue { color: #075985; background: rgba(56,189,248,0.16); }
.news-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
    margin-bottom: 14px;
}
.news-stat-card {
    border: 1px solid rgba(31,41,55,0.16);
    border-radius: 8px;
    padding: 12px 13px;
    background: #ffffff;
    color: #111827;
}
.news-stat-label {
    color: #6b7280;
    font-size: 0.75rem;
    margin-bottom: 5px;
}
.news-stat-value {
    font-size: 1.25rem;
    font-weight: 800;
    color: #111827;
}
.market-card a { color: #075985; font-weight: 700; }
</style>
"""


# -----------------------------------------------------------------------------
# Data helpers
# -----------------------------------------------------------------------------

def _to_datetime(value: Any) -> pd.Timestamp | None:
    if value is None or value == "":
        return None
    try:
        ts = pd.to_datetime(value, errors="coerce", utc=True)
        if pd.isna(ts):
            return None
        return ts
    except Exception:
        return None


def _relative_time(value: Any) -> str:
    ts = _to_datetime(value)
    if ts is None:
        return "unknown time"
    now = pd.Timestamp.now(tz="UTC")
    diff = now - ts
    seconds = max(0, int(diff.total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _fmt_time(value: Any) -> str:
    ts = _to_datetime(value)
    if ts is None:
        return "N/A"
    return ts.strftime("%Y-%m-%d %H:%M UTC")


def _safe_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value if str(x).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(x) for x in parsed if str(x).strip()]
        except Exception:
            pass
        return [x.strip() for x in value.split(",") if x.strip()]
    return []




def _clean_news_text(value: Any, max_chars: int | None = None) -> str:
    """Clean Benzinga HTML/body text before rendering or summarizing."""
    text = "" if value is None else str(value)
    text = html.unescape(text)
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if max_chars and len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text

def _articles_to_df(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "published_at" in df.columns:
        df["published_at"] = pd.to_datetime(df["published_at"], errors="coerce", utc=True)
    if "impact_score" in df.columns:
        df["impact_score"] = pd.to_numeric(df["impact_score"], errors="coerce").fillna(0).astype(int)
    if "watchlist_hit" in df.columns:
        df["watchlist_hit"] = df["watchlist_hit"].astype(bool)
    return df


def _filter_df(
    df: pd.DataFrame,
    query: str = "",
    ticker: str = "All",
    sentiment: str = "All",
    category: str = "All",
    source: str = "All",
    watchlist_only: bool = False,
    min_impact: int = 0,
    lookback_hours: int | None = None,
) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()

    if lookback_hours is not None and "published_at" in out.columns:
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=int(lookback_hours))
        out = out[out["published_at"] >= cutoff]

    if watchlist_only and "watchlist_hit" in out.columns:
        out = out[out["watchlist_hit"] == True]

    if min_impact and "impact_score" in out.columns:
        out = out[out["impact_score"] >= int(min_impact)]

    if sentiment != "All" and "sentiment" in out.columns:
        out = out[out["sentiment"].astype(str).str.lower() == sentiment.lower()]

    if source != "All" and "source" in out.columns:
        out = out[out["source"].astype(str) == source]

    if ticker != "All":
        t = ticker.upper().strip()
        out = out[out["tickers"].apply(lambda xs: t in [str(x).upper() for x in _safe_list(xs)])]

    if category != "All":
        out = out[out["categories"].apply(lambda xs: category in _safe_list(xs))]

    q = query.strip().lower()
    if q:
        def match(row: pd.Series) -> bool:
            haystack = " ".join([
                str(row.get("headline", "")),
                str(row.get("summary", "")),
                " ".join(_safe_list(row.get("tickers"))),
                " ".join(_safe_list(row.get("categories"))),
            ]).lower()
            return q in haystack
        out = out[out.apply(match, axis=1)]

    return out.sort_values("published_at", ascending=False) if "published_at" in out.columns else out


# -----------------------------------------------------------------------------
# UI helpers
# -----------------------------------------------------------------------------

def _sentiment_pill(sentiment: str) -> str:
    s = str(sentiment or "neutral").lower()
    if s == "bullish":
        return "<span class='news-pill news-pill-green'>Bullish</span>"
    if s == "bearish":
        return "<span class='news-pill news-pill-red'>Bearish</span>"
    return "<span class='news-pill'>Neutral</span>"


def _impact_pill(score: Any) -> str:
    try:
        n = int(score or 0)
    except Exception:
        n = 0
    cls = "news-pill"
    if n >= 85:
        cls = "news-pill news-pill-yellow"
    elif n >= 70:
        cls = "news-pill news-pill-blue"
    return f"<span class='{cls}'>Impact {n}</span>"


def _ticker_pills(tickers: list[str]) -> str:
    if not tickers:
        return "<span class='news-pill'>No ticker</span>"
    return "".join([f"<span class='news-pill news-pill-blue'>{html.escape(str(t))}</span>" for t in tickers[:8]])


def _category_pills(categories: list[str]) -> str:
    return "".join([f"<span class='news-pill'>{html.escape(str(c))}</span>" for c in categories[:6]])


def _stat_card(label: str, value: str) -> None:
    st.markdown(
        f"""
        <div class="news-stat-card">
            <div class="news-stat-label">{html.escape(label)}</div>
            <div class="news-stat-value">{html.escape(value)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_news_table(table: pd.DataFrame, max_rows: int = 500) -> None:
    if table.empty:
        st.info("No articles match the current filters.")
        return

    headers = ["Published", "Source", "Headline", "Tickers", "Sentiment", "Impact", "Watchlist"]
    header_html = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body_rows = []
    for _, row in table.head(max_rows).iterrows():
        published = _fmt_time(row.get("published_at"))
        source = html.escape(_clean_news_text(row.get("source", ""), 24))
        headline_text = html.escape(_clean_news_text(row.get("headline", "Untitled"), 115))
        url = str(row.get("url", "") or "")
        headline = f"<a href='{html.escape(url, quote=True)}' target='_blank'>{headline_text}</a>" if url else headline_text
        tickers = _ticker_pills(_safe_list(row.get("tickers")))
        sentiment = _sentiment_pill(str(row.get("sentiment", "neutral")))
        impact = _impact_pill(row.get("impact_score", 0))
        watchlist = "Yes" if bool(row.get("watchlist_hit", False)) else "No"
        watchlist_class = " pt-news-positive" if watchlist == "Yes" else ""
        body_rows.append(
            "<tr>"
            f"<td>{html.escape(published)}</td>"
            f"<td>{source}</td>"
            f"<td class='pt-news-headline'>{headline}</td>"
            f"<td>{tickers}</td>"
            f"<td>{sentiment}</td>"
            f"<td>{impact}</td>"
            f"<td class='{watchlist_class}'>{watchlist}</td>"
            "</tr>"
        )

    st.markdown(
        f"""
        <style>
        .pt-news-table-wrap {{
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            overflow: hidden;
            background: #ffffff;
        }}
        .pt-news-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.86rem;
        }}
        .pt-news-table th {{
            background: #f4f2fb;
            color: #111827;
            font-size: 0.92rem;
            font-weight: 800;
            padding: 0.8rem 0.7rem;
            text-align: left;
            border-bottom: 1px solid #e5e7eb;
        }}
        .pt-news-table td {{
            padding: 0.75rem 0.7rem;
            color: #111827;
            border-bottom: 1px solid #f3f4f6;
            vertical-align: top;
        }}
        .pt-news-table tr:last-child td {{
            border-bottom: 0;
        }}
        .pt-news-headline {{
            min-width: 320px;
            font-weight: 750;
            line-height: 1.25;
        }}
        .pt-news-table a {{
            color: #4f46e5;
            text-decoration: none;
        }}
        .pt-news-positive {{
            color: #10b981 !important;
            font-weight: 800;
        }}
        </style>
        <div class="pt-news-table-wrap">
            <table class="pt-news-table">
                <thead><tr>{header_html}</tr></thead>
                <tbody>{''.join(body_rows)}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_article_card(article: dict[str, Any]) -> None:
    headline = html.escape(str(article.get("headline", "Untitled")))
    summary = html.escape(str(article.get("summary", "") or ""))
    source = html.escape(str(article.get("source", "unknown")))
    author = html.escape(str(article.get("author", "") or ""))
    published = article.get("published_at")
    url = str(article.get("url", "") or "")
    tickers = _safe_list(article.get("tickers"))
    categories = _safe_list(article.get("categories"))
    sentiment = str(article.get("sentiment", "neutral"))
    impact = int(article.get("impact_score", 0) or 0)
    watchlist_hit = bool(article.get("watchlist_hit"))

    classes = ["market-card"]
    if impact >= 85:
        classes.append("market-card-high")
    elif watchlist_hit:
        classes.append("market-card-watchlist")
    elif sentiment.lower() == "bearish":
        classes.append("market-card-bearish")

    link_html = ""
    if url:
        safe_url = html.escape(url, quote=True)
        link_html = f"<a href='{safe_url}' target='_blank'>Open source</a>"

    meta_parts = [source, _fmt_time(published), _relative_time(published)]
    if author:
        meta_parts.append(author)
    meta = " • ".join([p for p in meta_parts if p])

    summary_html = f"<div class='news-summary'>{summary[:500]}</div>" if summary else ""
    watchlist_html = "<span class='news-pill news-pill-green'>Watchlist hit</span>" if watchlist_hit else ""

    st.markdown(
        f"""
        <div class="{' '.join(classes)}">
            <div class="news-meta">{meta}</div>
            <div class="news-headline">{headline}</div>
            {summary_html}
            <div>
                {_ticker_pills(tickers)}
                {_sentiment_pill(sentiment)}
                {_impact_pill(impact)}
                {watchlist_html}
            </div>
            <div style="margin-top:7px;">
                {_category_pills(categories)}
            </div>
            <div class="news-meta" style="margin-top:8px;">{link_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _empty_chart(title: str) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=[], y=[], mode="lines+markers", name="No data"))
    fig.update_layout(
        title=title,
        height=300,
        margin=dict(l=20, r=20, t=50, b=30),
        paper_bgcolor="white",
        plot_bgcolor="white",
        font=dict(color="#111827"),
        annotations=[dict(text="No news data yet", x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False)],
    )
    return fig


def _impact_timeline(df: pd.DataFrame) -> go.Figure:
    if df.empty or "published_at" not in df.columns:
        return _empty_chart("Impact Timeline")
    chart = df.dropna(subset=["published_at"]).copy().sort_values("published_at")
    if chart.empty:
        return _empty_chart("Impact Timeline")
    text = chart.get("headline", pd.Series([""] * len(chart))).astype(str).str.slice(0, 80)
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=chart["published_at"],
        y=chart["impact_score"],
        mode="markers+lines",
        name="Impact",
        text=text,
        hovertemplate="%{x}<br>Impact %{y}<br>%{text}<extra></extra>",
    ))
    fig.update_layout(height=330, title="News Impact Timeline", xaxis_title="Time", yaxis_title="Impact Score", paper_bgcolor="white", plot_bgcolor="white", font=dict(color="#111827"))
    fig.update_yaxes(range=[0, 105])
    return fig


def _category_chart(df: pd.DataFrame) -> go.Figure:
    if df.empty or "categories" not in df.columns:
        return _empty_chart("Category Breakdown")
    counts: dict[str, int] = {}
    for cats in df["categories"].tolist():
        for cat in _safe_list(cats):
            counts[cat] = counts.get(cat, 0) + 1
    if not counts:
        return _empty_chart("Category Breakdown")
    data = pd.DataFrame([{"category": k, "count": v} for k, v in counts.items()]).sort_values("count", ascending=True).tail(12)
    fig = go.Figure()
    fig.add_trace(go.Bar(x=data["count"], y=data["category"], orientation="h", name="Articles"))
    fig.update_layout(height=330, title="Category Breakdown", xaxis_title="Articles", yaxis_title="Category", paper_bgcolor="white", plot_bgcolor="white", font=dict(color="#111827"))
    return fig


def _ticker_chart(df: pd.DataFrame) -> go.Figure:
    if df.empty or "tickers" not in df.columns:
        return _empty_chart("Ticker Mentions")
    counts: dict[str, int] = {}
    for tickers in df["tickers"].tolist():
        for ticker in _safe_list(tickers):
            counts[ticker] = counts.get(ticker, 0) + 1
    if not counts:
        return _empty_chart("Ticker Mentions")
    data = pd.DataFrame([{"ticker": k, "count": v} for k, v in counts.items()]).sort_values("count", ascending=True).tail(15)
    fig = go.Figure()
    fig.add_trace(go.Bar(x=data["count"], y=data["ticker"], orientation="h", name="Mentions"))
    fig.update_layout(height=330, title="Top Ticker Mentions", xaxis_title="Mentions", yaxis_title="Ticker", paper_bgcolor="white", plot_bgcolor="white", font=dict(color="#111827"))
    return fig



def _build_ticker_sentiment_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate the filtered news into a ticker-level bullish/bearish summary."""
    if df.empty or "tickers" not in df.columns:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        tickers = _safe_list(row.get("tickers"))
        if not tickers:
            continue
        sentiment = str(row.get("sentiment", "neutral") or "neutral").lower()
        impact = int(row.get("impact_score", 0) or 0)
        headline = _clean_news_text(row.get("headline", ""), 140)
        published = row.get("published_at")
        categories = ", ".join(_safe_list(row.get("categories"))[:3])

        for ticker in tickers:
            rows.append({
                "Ticker": str(ticker).upper(),
                "Sentiment": sentiment,
                "Impact": impact,
                "Headline": headline,
                "Published": published,
                "Categories": categories,
            })

    if not rows:
        return pd.DataFrame()

    expanded = pd.DataFrame(rows)

    def last_headline(group: pd.DataFrame) -> str:
        group = group.sort_values("Published", ascending=False)
        return str(group.iloc[0].get("Headline", ""))

    def last_time(group: pd.DataFrame) -> Any:
        group = group.sort_values("Published", ascending=False)
        return group.iloc[0].get("Published")

    summary = (
        expanded.groupby("Ticker")
        .agg(
            Articles=("Ticker", "count"),
            Bullish=("Sentiment", lambda s: int((s == "bullish").sum())),
            Bearish=("Sentiment", lambda s: int((s == "bearish").sum())),
            Neutral=("Sentiment", lambda s: int((s == "neutral").sum())),
            Avg_Impact=("Impact", "mean"),
            Max_Impact=("Impact", "max"),
        )
        .reset_index()
    )

    headline_map: dict[str, str] = {}
    time_map: dict[str, Any] = {}
    for ticker_value, group in expanded.groupby("Ticker"):
        group = group.sort_values("Published", ascending=False)
        headline_map[str(ticker_value)] = str(group.iloc[0].get("Headline", ""))
        time_map[str(ticker_value)] = group.iloc[0].get("Published")

    summary["Latest Headline"] = summary["Ticker"].map(headline_map)
    summary["Latest"] = summary["Ticker"].map(time_map)
    summary["Avg Impact"] = summary["Avg_Impact"].round(0).astype(int)
    summary["Max Impact"] = summary["Max_Impact"].astype(int)
    summary["Bias Score"] = summary["Bullish"] - summary["Bearish"]

    def bias_label(row: pd.Series) -> str:
        if row["Bias Score"] > 0:
            return "Bullish"
        if row["Bias Score"] < 0:
            return "Bearish"
        return "Mixed / Neutral"

    summary["Bias"] = summary.apply(bias_label, axis=1)
    return summary.sort_values(["Max Impact", "Articles"], ascending=[False, False])


def _render_highest_impact_catalysts(filtered: pd.DataFrame) -> None:
    catalyst_cols = [c for c in ["published_at", "headline", "tickers", "sentiment", "impact_score", "categories", "url"] if c in filtered.columns]
    catalysts = filtered.sort_values(["impact_score", "published_at"], ascending=[False, False]).head(10).copy()
    if catalysts.empty:
        st.caption("No catalyst headlines in the current filter.")
    else:
        if "published_at" in catalysts.columns:
            catalysts["published_at"] = catalysts["published_at"].astype(str)
        if "headline" in catalysts.columns:
            catalysts["headline"] = catalysts["headline"].apply(lambda x: _clean_news_text(x, 140))
        st.dataframe(catalysts[catalyst_cols], use_container_width=True, hide_index=True)


def _render_market_summary(filtered: pd.DataFrame) -> None:
    """Render a decision-friendly summary above the raw news feed."""
    st.markdown("### Market Intelligence Summary")
    st.caption("Quick read of the filtered news: which watchlist names look bullish, bearish, or have fresh catalysts.")

    if filtered.empty:
        st.info("No articles match the current filters.")
        return

    ticker_summary = _build_ticker_sentiment_summary(filtered)

    s1, s2, s3, s4 = st.columns(4)
    bullish_articles = int((filtered.get("sentiment", pd.Series(dtype=str)).astype(str).str.lower() == "bullish").sum())
    bearish_articles = int((filtered.get("sentiment", pd.Series(dtype=str)).astype(str).str.lower() == "bearish").sum())
    high_impact_articles = int((filtered.get("impact_score", pd.Series(dtype=int)) >= 85).sum()) if "impact_score" in filtered.columns else 0
    ticker_count = int(len(ticker_summary)) if not ticker_summary.empty else 0
    with s1:
        _stat_card("Bullish Articles", f"{bullish_articles:,}")
    with s2:
        _stat_card("Bearish Articles", f"{bearish_articles:,}")
    with s3:
        _stat_card("High Impact", f"{high_impact_articles:,}")
    with s4:
        _stat_card("Tickers Mentioned", f"{ticker_count:,}")

    if ticker_summary.empty:
        st.info("No ticker-level summary available for the current filters.")
        return

    left, right = st.columns(2)

    bullish = ticker_summary[ticker_summary["Bias"] == "Bullish"].copy()
    bearish = ticker_summary[ticker_summary["Bias"] == "Bearish"].copy()

    bullish = bullish.sort_values(["Bias Score", "Max Impact", "Articles"], ascending=[False, False, False]).head(8)
    bearish = bearish.sort_values(["Bias Score", "Max Impact", "Articles"], ascending=[True, False, False]).head(8)

    display_cols = ["Ticker", "Bias", "Articles", "Bullish", "Bearish", "Avg Impact", "Max Impact", "Latest Headline"]

    with left:
        st.markdown("#### Bullish Watchlist")
        if bullish.empty:
            st.caption("No clear bullish watchlist names in the current filter.")
        else:
            st.dataframe(bullish[display_cols], use_container_width=True, hide_index=True)

    with right:
        st.markdown("#### Bearish Watchlist")
        if bearish.empty:
            st.caption("No clear bearish watchlist names in the current filter.")
        else:
            st.dataframe(bearish[display_cols], use_container_width=True, hide_index=True)

# -----------------------------------------------------------------------------
# Main render function
# -----------------------------------------------------------------------------

def render_market_intelligence_tab(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    """Render the PulseTrade AI Market Intelligence dashboard tab."""
    if st is None:  # pragma: no cover
        raise RuntimeError("Streamlit is required to render this dashboard module.")

    st.markdown(MARKET_INTELLIGENCE_CSS, unsafe_allow_html=True)
    st.subheader("Market Intelligence")
    st.caption("Benzinga news feed, catalyst detection, watchlist hits, impact scoring, and news history.")

    db = initialize_news_database(db_path)
    health = db.get_health()
    stats = db.get_stats()

    top_cols = st.columns(5)
    with top_cols[0]:
        _stat_card("Total Articles", f"{stats.get('total_articles', 0):,}")
    with top_cols[1]:
        _stat_card("Watchlist Hits", f"{stats.get('watchlist_hits', 0):,}")
    with top_cols[2]:
        _stat_card("High Impact", f"{stats.get('high_impact', 0):,}")
    with top_cols[3]:
        _stat_card("Alerts Sent", f"{stats.get('alerts_sent', 0):,}")
    with top_cols[4]:
        _stat_card("News Engine", "Running" if health.get("engine_running") else "Idle")

    process_status = None
    if get_news_engine_status is not None:
        try:
            process_status = get_news_engine_status()
        except Exception:
            process_status = None

    def render_news_engine_controls() -> None:
        with st.expander("News Engine Controls", expanded=False):
            st.caption("Use these controls to run Benzinga polling without opening a separate Terminal window.")

            control_cols = st.columns(5)
            with control_cols[0]:
                if st.button("Start News Engine", use_container_width=True, disabled=start_news_engine is None):
                    status = start_news_engine()
                    if status.running:
                        st.success(status.message)
                    else:
                        st.warning(status.message)
            with control_cols[1]:
                if st.button("Stop News Engine", use_container_width=True, disabled=stop_news_engine is None):
                    status = stop_news_engine()
                    st.info(status.message)
            with control_cols[2]:
                if st.button("Run One Poll", use_container_width=True, disabled=run_news_poll_once is None):
                    with st.spinner("Polling Benzinga..."):
                        result = run_news_poll_once()
                    if result.get("ok"):
                        st.success("One news poll completed.")
                    else:
                        st.error("News poll failed.")
                    st.code((result.get("stdout") or "")[-4000:])
                    if result.get("stderr"):
                        st.code((result.get("stderr") or "")[-4000:])
            with control_cols[3]:
                if st.button("Analyze Recent", use_container_width=True):
                    if NewsAnalyzer is None:
                        st.error("news_analyzer.py could not be imported.")
                    else:
                        analyzer = NewsAnalyzer(db_path)
                        result = analyzer.analyze_recent(limit=500)
                        st.success(f"Analyzed {result.get('articles_analyzed', 0)} articles.")
            with control_cols[4]:
                if st.button("Initialize DB", use_container_width=True):
                    initialize_news_database(db_path)
                    st.success("News database initialized.")

            if tail_news_process_log is not None:
                st.caption("News engine process log")
                log_text = tail_news_process_log()
                st.code(log_text if log_text else "No process log yet.")

    raw_articles = db.get_recent_articles(limit=1000)
    df = _articles_to_df(raw_articles)

    if df.empty:
        st.info("No news articles stored yet. Once Benzinga is connected, articles will appear here automatically.")
        st.markdown("### Intelligence Overview")
        chart_cols = st.columns(2)
        chart_cols[0].plotly_chart(_empty_chart("News Impact Timeline"), use_container_width=True)
        chart_cols[1].plotly_chart(_empty_chart("Top Ticker Mentions"), use_container_width=True)
        render_news_engine_controls()
        return

    all_tickers = sorted({t for xs in df.get("tickers", pd.Series(dtype=object)).tolist() for t in _safe_list(xs)})
    all_categories = sorted({c for xs in df.get("categories", pd.Series(dtype=object)).tolist() for c in _safe_list(xs)})
    all_sources = sorted(set(df.get("source", pd.Series(dtype=str)).dropna().astype(str).tolist()))

    lookback_options = ["All", "1 hour", "4 hours", "Today", "7 days"]
    sentiment_options = ["All", "bullish", "bearish", "neutral"]
    ticker_options = ["All"] + all_tickers
    category_options = ["All"] + all_categories
    source_options = ["All"] + all_sources

    lookback_label = st.session_state.get("news_filter_time_window", "All")
    lookback_label = lookback_label if lookback_label in lookback_options else "All"
    ticker = st.session_state.get("news_filter_ticker", "All")
    ticker = ticker if ticker in ticker_options else "All"
    sentiment = st.session_state.get("news_filter_sentiment", "All")
    sentiment = sentiment if sentiment in sentiment_options else "All"
    min_impact = int(st.session_state.get("news_filter_min_impact", 0))
    category = st.session_state.get("news_filter_category", "All")
    category = category if category in category_options else "All"
    source = st.session_state.get("news_filter_source", "All")
    source = source if source in source_options else "All"
    watchlist_only = bool(st.session_state.get("news_filter_watchlist_only", True))
    query = str(st.session_state.get("news_filter_query", ""))

    with st.expander("Filters", expanded=False):
        st.markdown("### Filters")
        f1, f2, f3, f4 = st.columns(4)
        with f1:
            lookback_label = st.selectbox("Time window", lookback_options, index=lookback_options.index(lookback_label), key="news_filter_time_window")
        with f2:
            ticker = st.selectbox("Ticker", ticker_options, index=ticker_options.index(ticker), key="news_filter_ticker")
        with f3:
            sentiment = st.selectbox("Sentiment", sentiment_options, index=sentiment_options.index(sentiment), key="news_filter_sentiment")
        with f4:
            min_impact = st.slider("Minimum impact", min_value=0, max_value=100, value=min_impact, step=5, key="news_filter_min_impact")

        f5, f6, f7 = st.columns(3)
        with f5:
            category = st.selectbox("Category", category_options, index=category_options.index(category), key="news_filter_category")
        with f6:
            source = st.selectbox("Source", source_options, index=source_options.index(source), key="news_filter_source")
        with f7:
            watchlist_only = st.checkbox("Watchlist only", value=watchlist_only, key="news_filter_watchlist_only")

        query = st.text_input("Search news", value=query, placeholder="Search headline, summary, ticker, category...", key="news_filter_query")
        active_bits = [
            f"Window: {lookback_label}",
            f"Ticker: {ticker}",
            f"Sentiment: {sentiment}",
            f"Min impact: {min_impact}",
            "Watchlist only" if watchlist_only else "All articles",
        ]
        st.caption(" • ".join(active_bits))

    render_news_engine_controls()

    lookback_hours = None
    if lookback_label == "1 hour":
        lookback_hours = 1
    elif lookback_label == "4 hours":
        lookback_hours = 4
    elif lookback_label == "Today":
        lookback_hours = 24
    elif lookback_label == "7 days":
        lookback_hours = 24 * 7

    filtered = _filter_df(
        df,
        query=query,
        ticker=ticker,
        sentiment=sentiment,
        category=category,
        source=source,
        watchlist_only=watchlist_only,
        min_impact=min_impact,
        lookback_hours=lookback_hours,
    )

    feed_tab, overview_tab, diagnostics_tab = st.tabs(["Live Feed", "Overview", "Diagnostics"])
    with overview_tab:
        st.markdown("### Intelligence Overview")
        c1, c2 = st.columns(2)
        c1.plotly_chart(_impact_timeline(filtered), use_container_width=True)
        c2.plotly_chart(_ticker_chart(filtered), use_container_width=True)

        c3, c4 = st.columns(2)
        c3.plotly_chart(_category_chart(filtered), use_container_width=True)
        if not filtered.empty and "sentiment" in filtered.columns:
            sent_counts = filtered["sentiment"].fillna("neutral").astype(str).value_counts().reset_index()
            sent_counts.columns = ["sentiment", "count"]
            fig = go.Figure()
            fig.add_trace(go.Bar(x=sent_counts["sentiment"], y=sent_counts["count"], name="Articles"))
            fig.update_layout(height=330, title="Sentiment Breakdown", xaxis_title="Sentiment", yaxis_title="Articles", paper_bgcolor="white", plot_bgcolor="white", font=dict(color="#111827"))
            c4.plotly_chart(fig, use_container_width=True)
        else:
            c4.plotly_chart(_empty_chart("Sentiment Breakdown"), use_container_width=True)

        with st.expander("Market Intelligence Summary", expanded=False):
            _render_market_summary(filtered)

        with st.expander("Highest Impact Catalysts", expanded=False):
            _render_highest_impact_catalysts(filtered)

    with feed_tab:
        st.markdown("### Live Feed")
        st.caption(f"Showing {len(filtered):,} of {len(df):,} stored articles.")

        view_mode = st.radio("View", ["Cards", "Table"], horizontal=True)
        if view_mode == "Cards":
            max_cards = st.slider("Cards to show", 5, 100, 25, 5)
            card_cols = st.columns(4)
            for idx, (_, row) in enumerate(filtered.head(max_cards).iterrows()):
                with card_cols[idx % 4]:
                    _render_article_card(row.to_dict())
        else:
            table_cols = [c for c in [
                "published_at", "source", "headline", "tickers", "sentiment",
                "impact_score", "watchlist_hit", "categories", "url"
            ] if c in filtered.columns]
            table = filtered[table_cols].copy()
            if "published_at" in table.columns:
                table["published_at"] = table["published_at"].astype(str)
            _render_news_table(table, max_rows=500)
            st.download_button(
                "Download filtered news CSV",
                table.to_csv(index=False),
                "market_intelligence_news.csv",
                "text/csv",
            )

    with diagnostics_tab:
        st.markdown("### Diagnostics")
        st.caption("Technical details for the local news engine and latest filtered article.")
        st.json(health or {"status": "No news engine heartbeat yet."})
        if process_status is not None:
            st.caption("Local process status")
            st.json(process_status.as_dict())
        if not filtered.empty:
            with st.expander("Raw latest article", expanded=False):
                st.json(filtered.iloc[0].dropna().to_dict())


# -----------------------------------------------------------------------------
# Standalone smoke test
# -----------------------------------------------------------------------------

def main() -> None:
    db = initialize_news_database()
    print(f"News dashboard module OK. Database: {db.db_path}")
    print(db.get_stats())


if __name__ == "__main__":
    main()
