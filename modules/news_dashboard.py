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


# -----------------------------------------------------------------------------
# Styling
# -----------------------------------------------------------------------------

MARKET_INTELLIGENCE_CSS = """
<style>
.market-card {
    border: 1px solid rgba(250,250,250,0.14);
    border-radius: 16px;
    padding: 15px 16px;
    background: rgba(255,255,255,0.035);
    margin-bottom: 12px;
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
    font-size: 0.78rem;
    color: rgba(250,250,250,0.62);
    margin-bottom: 6px;
}
.news-headline {
    font-size: 1.02rem;
    font-weight: 750;
    line-height: 1.32;
    margin-bottom: 8px;
}
.news-summary {
    font-size: 0.86rem;
    color: rgba(250,250,250,0.72);
    line-height: 1.45;
    margin-bottom: 9px;
}
.news-pill {
    display: inline-block;
    border-radius: 999px;
    padding: 3px 8px;
    margin: 2px 4px 2px 0;
    font-size: 0.73rem;
    font-weight: 700;
    border: 1px solid rgba(250,250,250,0.12);
    background: rgba(255,255,255,0.055);
}
.news-pill-green { color: #22c55e; background: rgba(34,197,94,0.12); }
.news-pill-red { color: #ef4444; background: rgba(239,68,68,0.12); }
.news-pill-yellow { color: #f59e0b; background: rgba(245,158,11,0.12); }
.news-pill-blue { color: #38bdf8; background: rgba(56,189,248,0.12); }
.news-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
    margin-bottom: 14px;
}
.news-stat-card {
    border: 1px solid rgba(250,250,250,0.12);
    border-radius: 14px;
    padding: 12px 13px;
    background: rgba(255,255,255,0.035);
}
.news-stat-label {
    color: rgba(250,250,250,0.62);
    font-size: 0.75rem;
    margin-bottom: 5px;
}
.news-stat-value {
    font-size: 1.25rem;
    font-weight: 800;
}
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
    fig.update_layout(height=330, title="News Impact Timeline", xaxis_title="Time", yaxis_title="Impact Score")
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
    fig.update_layout(height=330, title="Category Breakdown", xaxis_title="Articles", yaxis_title="Category")
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
    fig.update_layout(height=330, title="Top Ticker Mentions", xaxis_title="Mentions", yaxis_title="Ticker")
    return fig


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

    with st.expander("News engine health", expanded=False):
        st.json(health or {"status": "No news engine heartbeat yet."})

    action_cols = st.columns(4)
    with action_cols[0]:
        if st.button("Initialize DB", use_container_width=True):
            initialize_news_database(db_path)
            st.success("News database initialized.")
    with action_cols[1]:
        if st.button("Analyze Recent", use_container_width=True):
            if NewsAnalyzer is None:
                st.error("news_analyzer.py could not be imported.")
            else:
                analyzer = NewsAnalyzer(db_path)
                result = analyzer.analyze_recent(limit=500)
                st.success(f"Analyzed {result.get('articles_analyzed', 0)} articles.")
    with action_cols[2]:
        st.button("Poll Benzinga", disabled=True, use_container_width=True, help="This will be connected after your Benzinga token is added.")
    with action_cols[3]:
        st.button("Send Test Alert", disabled=True, use_container_width=True, help="Telegram news alerts are the next integration step.")

    raw_articles = db.get_recent_articles(limit=1000)
    df = _articles_to_df(raw_articles)

    if df.empty:
        st.info("No news articles stored yet. Once Benzinga is connected, articles will appear here automatically.")
        chart_cols = st.columns(2)
        chart_cols[0].plotly_chart(_empty_chart("News Impact Timeline"), use_container_width=True)
        chart_cols[1].plotly_chart(_empty_chart("Top Ticker Mentions"), use_container_width=True)
        return

    all_tickers = sorted({t for xs in df.get("tickers", pd.Series(dtype=object)).tolist() for t in _safe_list(xs)})
    all_categories = sorted({c for xs in df.get("categories", pd.Series(dtype=object)).tolist() for c in _safe_list(xs)})
    all_sources = sorted(set(df.get("source", pd.Series(dtype=str)).dropna().astype(str).tolist()))

    st.markdown("### Filters")
    f1, f2, f3, f4 = st.columns(4)
    with f1:
        lookback_label = st.selectbox("Time window", ["All", "1 hour", "4 hours", "Today", "7 days"], index=0)
        lookback_hours = None
        if lookback_label == "1 hour":
            lookback_hours = 1
        elif lookback_label == "4 hours":
            lookback_hours = 4
        elif lookback_label == "Today":
            lookback_hours = 24
        elif lookback_label == "7 days":
            lookback_hours = 24 * 7
    with f2:
        ticker = st.selectbox("Ticker", ["All"] + all_tickers)
    with f3:
        sentiment = st.selectbox("Sentiment", ["All", "bullish", "bearish", "neutral"])
    with f4:
        min_impact = st.slider("Minimum impact", min_value=0, max_value=100, value=0, step=5)

    f5, f6, f7 = st.columns(3)
    with f5:
        category = st.selectbox("Category", ["All"] + all_categories)
    with f6:
        source = st.selectbox("Source", ["All"] + all_sources)
    with f7:
        watchlist_only = st.checkbox("Watchlist only", value=False)

    query = st.text_input("Search news", value="", placeholder="Search headline, summary, ticker, category...")

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
        fig.update_layout(height=330, title="Sentiment Breakdown", xaxis_title="Sentiment", yaxis_title="Articles")
        c4.plotly_chart(fig, use_container_width=True)
    else:
        c4.plotly_chart(_empty_chart("Sentiment Breakdown"), use_container_width=True)

    st.markdown("### Live Feed")
    st.caption(f"Showing {len(filtered):,} of {len(df):,} stored articles.")

    view_mode = st.radio("View", ["Cards", "Table"], horizontal=True)
    if view_mode == "Cards":
        max_cards = st.slider("Cards to show", 5, 100, 25, 5)
        for _, row in filtered.head(max_cards).iterrows():
            _render_article_card(row.to_dict())
    else:
        table_cols = [c for c in [
            "published_at", "source", "headline", "tickers", "sentiment",
            "impact_score", "watchlist_hit", "categories", "url"
        ] if c in filtered.columns]
        table = filtered[table_cols].copy()
        if "published_at" in table.columns:
            table["published_at"] = table["published_at"].astype(str)
        st.dataframe(table.head(500), use_container_width=True, hide_index=True)
        st.download_button(
            "Download filtered news CSV",
            table.to_csv(index=False),
            "market_intelligence_news.csv",
            "text/csv",
        )

    with st.expander("Raw latest article", expanded=False):
        if not filtered.empty:
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
