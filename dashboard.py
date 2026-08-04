# dashboard.py
# Streamlit dashboard for AutoTrader. The dashboard edits config.json; engine.py reads the same config.

from __future__ import annotations

import os
import asyncio
import calendar
import html
import json
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, time as dtime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from bot_core import *
from engine import (
    approval_mode_from_config,
    is_telegram_polling_noise,
    make_approval_id,
    process_telegram_order_callbacks,
    read_current_scan_candidates,
    read_pending_approvals,
    run_cycle,
    send_order_approval_message,
    submit_approved_order,
    update_pending_approval,
    write_pending_approvals,
)

try:
    from modules.news_dashboard import render_market_intelligence_tab
except Exception:
    render_market_intelligence_tab = None

try:
    from modules.news_control import get_news_engine_status, start_news_engine
except Exception:
    get_news_engine_status = None
    start_news_engine = None

try:
    from modules.news_bridge import get_catalyst_map, render_catalyst_html
except Exception:
    get_catalyst_map = None
    render_catalyst_html = None

try:
    from modules.premarket_watchlist import (
        build_premarket_watchlist,
        combined_watchlist,
        dynamic_config,
        load_dynamic_payload,
        normalize_symbols,
    )
except Exception:
    build_premarket_watchlist = None
    combined_watchlist = None
    dynamic_config = None
    load_dynamic_payload = None
    normalize_symbols = None

try:
    from modules.ibkr_flex import sync_flex_trades_to_trade_log
except Exception:
    sync_flex_trades_to_trade_log = None

try:
    from backtester.data import YahooDataClient, YFINANCE_AVAILABLE
    from backtester.replay import MarketReplayEngine, ReplayConfig, make_replay_log
    from backtester.strategy import scan_replay_history, clean_signal_row
    from backtester.simulator import OptionSimulationConfig, simulate_option_trades, summarize_trades
except Exception:
    YahooDataClient = None
    YFINANCE_AVAILABLE = False
    MarketReplayEngine = None
    ReplayConfig = None
    make_replay_log = None
    scan_replay_history = None
    clean_signal_row = None
    OptionSimulationConfig = None
    simulate_option_trades = None
    summarize_trades = None


try:
    from backtester.ui import render_strategy_lab_tab
except Exception:
    render_strategy_lab_tab = None

APP_DISPLAY_NAME = "PulseTrade AI"
st.set_page_config(page_title=APP_DISPLAY_NAME, layout="wide")
getattr(st, "html", lambda body: st.markdown(body, unsafe_allow_html=True))("""
<style>
    .block-container { padding-top: 2.75rem !important; padding-bottom: 7.25rem !important; }
    div[data-testid="stMetricValue"] { font-size: 1.24rem !important; line-height: 1.16 !important; white-space: nowrap !important; }
    div[data-testid="stMetricLabel"] { font-size: 0.78rem !important; line-height: 1.12 !important; }
    div[data-testid="stMetric"] { min-width: 0 !important; }
    .compact-metric { min-width: 0; padding-top: 0.05rem; }
    .compact-metric-label { font-size: 0.78rem; line-height: 1.12; color: rgba(49, 51, 63, 0.82); margin-bottom: 0.28rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .compact-metric-value { display: flex; align-items: baseline; gap: 0.18rem; font-size: 1.08rem; line-height: 1.16; white-space: nowrap; color: rgb(49, 51, 63); }
    .metric-positive { color: #16833a; font-weight: 700; }
    .metric-negative { color: #c2410c; font-weight: 700; }
    .metric-divider { color: rgba(49, 51, 63, 0.55); font-weight: 600; }
    .status-card {
        border: 1px solid rgba(250,250,250,0.14);
        border-radius: 14px;
        padding: 14px 16px;
        background: rgba(255,255,255,0.035);
        min-height: 96px;
        margin-bottom: 10px;
    }
    .setup-card {
        border: 1px solid rgba(31,41,55,0.16);
        border-radius: 8px;
        padding: 8px 10px;
        background: #ffffff;
        color: #111827;
        min-height: 0;
        margin-bottom: 6px;
    }
    .setup-card-call { border-left: 5px solid #16a34a; background: rgba(22,163,74,0.08); }
    .setup-card-put { border-left: 5px solid #dc2626; background: rgba(220,38,38,0.08); }
    .setup-card-wait { border-left: 5px solid #f59e0b; background: rgba(245,158,11,0.12); }
    .status-label, .setup-label { font-size: 0.78rem; color: rgba(250,250,250,0.68); margin-bottom: 6px; }
    .setup-label { color: #4b5563; }
    .status-value { font-size: 1.22rem; font-weight: 700; line-height: 1.15; }
    .setup-symbol { font-size: 1.04rem; font-weight: 800; margin-bottom: 1px; color: #111827; }
    .setup-badge { font-size: 0.70rem; font-weight: 800; border-radius: 999px; padding: 2px 7px; display: inline-block; margin-bottom: 5px; }
    .badge-call { color: #166534; background: rgba(22,163,74,0.16); }
    .badge-put { color: #991b1b; background: rgba(220,38,38,0.16); }
    .badge-wait { color: #92400e; background: rgba(245,158,11,0.20); }
    .setup-card b { color: #111827; }
    .small-muted { color: #4b5563; font-size: 0.74rem; }

    /* Clean light sidebar navigation */
    section[data-testid="stSidebar"] {
        background: #ffffff !important;
        border-right: 1px solid #e5e7eb;
    }
    section[data-testid="stSidebar"] > div {
        background: #ffffff !important;
        padding-top: 1.6rem;
    }
    .pt-sidebar-brand {
        display: flex;
        align-items: center;
        gap: 0.62rem;
        color: #0f172a;
        font-weight: 850;
        font-size: 1.12rem;
        letter-spacing: 0;
        margin: 0.35rem 0 1.35rem;
    }
    .pt-sidebar-brand-mark {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 1.55rem;
        height: 1.55rem;
        border-radius: 8px;
        color: #0b82ff;
        background: #eaf3ff;
        font-size: 1rem;
        line-height: 1;
        font-weight: 900;
    }
    .pt-sidebar-status {
        border: 1px solid #e5e7eb;
        border-radius: 8px;
        padding: 0.82rem 0.9rem;
        margin-top: 1.8rem;
        color: #0f172a;
        background: #ffffff;
    }
    .pt-sidebar-status-row {
        display: flex;
        align-items: center;
        gap: 0.45rem;
        font-size: 0.86rem;
        font-weight: 700;
    }
    .pt-sidebar-dot {
        width: 0.55rem;
        height: 0.55rem;
        border-radius: 999px;
        display: inline-block;
        background: #16a34a;
    }
    .pt-sidebar-version {
        color: #64748b;
        font-size: 0.78rem;
        margin-top: 0.62rem;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] {
        gap: 0.34rem;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] label > div:first-child {
        display: none !important;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] label {
        border: 1px solid transparent !important;
        border-radius: 8px;
        width: 100%;
        min-width: 100%;
        box-sizing: border-box;
        padding: 0.64rem 0.72rem;
        margin-bottom: 0.2rem;
        background: transparent !important;
        transition: all 0.15s ease;
        color: #334155 !important;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] label:hover {
        border-color: #dbeafe !important;
        background: #eff6ff !important;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) {
        border-color: #dbeafe !important;
        background: #eaf3ff !important;
        color: #0969da !important;
        box-shadow: none !important;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] label p {
        color: inherit !important;
        font-size: 0.9rem !important;
        font-weight: 760 !important;
        line-height: 1.1 !important;
    }

    /* Cleaner action buttons */
    div.stButton > button {
        border-radius: 12px !important;
        border: 1px solid rgba(250,250,250,0.16) !important;
        background: linear-gradient(180deg, rgba(255,255,255,0.075), rgba(255,255,255,0.035)) !important;
        padding: 0.72rem 1.05rem !important;
        font-weight: 700 !important;
        min-height: 44px !important;
        transition: all 0.15s ease !important;
    }
    div.stButton > button:hover {
        border-color: rgba(239,68,68,0.8) !important;
        background: rgba(239,68,68,0.14) !important;
        transform: translateY(-1px);
    }
    div.stButton > button:active {
        transform: translateY(0);
    }
    div.stButton > button,
    div[data-testid="stTextInput"] input,
    div[data-testid="stNumberInput"] input,
    div[data-testid="stTextArea"] textarea,
    div[data-testid="stSelectbox"] div[data-baseweb="select"] > div,
    div[data-testid="stMultiSelect"] div[data-baseweb="select"] > div,
    div[data-testid="stDateInput"] input,
    div[data-testid="stTimeInput"] input,
    div[data-testid="stFileUploader"] section,
    div[data-testid="stExpander"] details {
        border: 1px solid rgba(31,41,55,0.24) !important;
        box-shadow: 0 1px 2px rgba(15,23,42,0.06) !important;
    }
    div.stButton > button:hover,
    div[data-testid="stTextInput"] input:hover,
    div[data-testid="stNumberInput"] input:hover,
    div[data-testid="stTextArea"] textarea:hover,
    div[data-testid="stSelectbox"] div[data-baseweb="select"] > div:hover,
    div[data-testid="stMultiSelect"] div[data-baseweb="select"] > div:hover,
    div[data-testid="stDateInput"] input:hover,
    div[data-testid="stTimeInput"] input:hover,
    div[data-testid="stExpander"] details:hover {
        border-color: rgba(239,68,68,0.55) !important;
    }
    div[data-testid="stTextInput"] input:focus,
    div[data-testid="stNumberInput"] input:focus,
    div[data-testid="stTextArea"] textarea:focus,
    div[data-testid="stDateInput"] input:focus,
    div[data-testid="stTimeInput"] input:focus {
        border-color: rgba(239,68,68,0.72) !important;
        box-shadow: 0 0 0 1px rgba(239,68,68,0.22) !important;
    }

    /* Compact global status ribbon */
    .app-title {
        font-size: 2.35rem !important;
        line-height: 1.05 !important;
        margin: 0.55rem 0 0.15rem 0 !important;
        font-weight: 900 !important;
    }
    .app-subtitle {
        color: rgba(250,250,250,0.62);
        font-size: 0.86rem;
        margin-bottom: 1.05rem;
    }
    .fixed-status-dock {
        position: fixed;
        left: 0;
        right: 0;
        bottom: 0;
        z-index: 999;
        background: rgba(11,15,22,0.96);
        border-top: 1px solid rgba(250,250,250,0.12);
        box-shadow: 0 -10px 30px rgba(0,0,0,0.32);
    }
    .fixed-status-inner {
        margin-left: 22rem;
        padding: 10px 2.4rem 12px 2.4rem;
        display: grid;
        grid-template-columns: repeat(6, minmax(0, 1fr));
        gap: 10px;
    }
    .compact-status-card {
        border: 1px solid rgba(250,250,250,0.16);
        border-radius: 10px;
        padding: 8px 10px;
        background: rgba(255,255,255,0.038);
        min-height: 50px;
        overflow: visible;
    }
    .compact-status-label {
        display: block !important;
        font-size: 0.68rem !important;
        letter-spacing: 0.03em;
        text-transform: uppercase;
        color: rgba(250,250,250,0.76) !important;
        margin-bottom: 4px !important;
        line-height: 1.15 !important;
        white-space: nowrap;
        overflow: visible !important;
        visibility: visible !important;
        opacity: 1 !important;
    }
    .compact-status-value {
        display: block !important;
        font-size: 0.86rem !important;
        font-weight: 800;
        line-height: 1.15 !important;
        color: rgba(250,250,250,0.98) !important;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    @media (max-width: 900px) {
        .fixed-status-inner {
            margin-left: 0;
            padding: 8px 10px 10px 10px;
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }
        .block-container { padding-bottom: 14rem !important; }
    }
    @media (max-width: 1100px) {
        .compact-status-wrap { grid-template-columns: repeat(3, minmax(120px, 1fr)); }
    }

    /* Hard override: keep compact status titles visible in Streamlit containers */
    [class*="compact-status-card"] [class*="compact-status-label"] {
        display: block !important;
        visibility: visible !important;
        opacity: 1 !important;
        height: auto !important;
        max-height: none !important;
        color: rgba(250,250,250,0.76) !important;
        font-size: 0.72rem !important;
    }

    /* Inline status ribbon: prevents Streamlit from clipping the title line */
    .compact-status-inline {
        display: flex !important;
        align-items: center !important;
        gap: 8px !important;
        min-height: 42px !important;
        padding: 8px 10px !important;
        overflow: visible !important;
    }
    .compact-status-label-inline {
        display: inline-block !important;
        visibility: visible !important;
        opacity: 1 !important;
        font-size: 0.66rem !important;
        letter-spacing: 0.045em !important;
        text-transform: uppercase !important;
        color: rgba(250,250,250,0.62) !important;
        line-height: 1 !important;
        white-space: nowrap !important;
        flex: 0 0 auto !important;
    }
    .compact-status-value-inline {
        display: inline-block !important;
        visibility: visible !important;
        opacity: 1 !important;
        font-size: 0.92rem !important;
        font-weight: 800 !important;
        color: rgba(250,250,250,0.98) !important;
        line-height: 1 !important;
        white-space: nowrap !important;
        overflow: hidden !important;
        text-overflow: ellipsis !important;
    }

    /* Native compact status bar spacing */
    div[data-testid="stVerticalBlockBorderWrapper"] {
        background: rgba(255,255,255,0.028);
        border-color: rgba(250,250,250,0.16) !important;
        border-radius: 12px !important;
        overflow: visible !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] > div {
        padding-top: 0.72rem !important;
        padding-bottom: 0.72rem !important;
        overflow: visible !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] p {
        margin-top: 0 !important;
        margin-bottom: 0.35rem !important;
        line-height: 1.25 !important;
        overflow: visible !important;
    }
    .pt-safe-metric {
        border: 1px solid rgba(31,41,55,0.16);
        border-radius: 8px;
        background: #ffffff;
        padding: 0.82rem 0.95rem;
        min-height: 82px;
        overflow: hidden;
        margin-bottom: 0.45rem;
    }
    .pt-safe-metric-label {
        color: #6b7280;
        font-size: 0.82rem;
        font-weight: 700;
        line-height: 1.14;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .pt-safe-metric-value {
        color: #111827;
        font-size: 1.08rem;
        font-weight: 800;
        line-height: 1.18;
        margin-top: 0.42rem;
        overflow-wrap: anywhere;
    }
    .pt-safe-metric-delta {
        color: #6b7280;
        font-size: 0.78rem;
        font-weight: 700;
        margin-top: 0.25rem;
    }

</style>
""")


def _install_safe_metric_renderer() -> None:
    def _safe_metric(self, label, value, delta=None, *args, **kwargs):
        label_text = "" if label is None else str(label)
        value_text = "" if value is None else str(value)
        delta_html = ""
        if delta not in [None, ""]:
            delta_html = f"<div class='pt-safe-metric-delta'>{html.escape(str(delta))}</div>"
        return self.markdown(
            "<div class='pt-safe-metric'>"
            f"<div class='pt-safe-metric-label'>{html.escape(label_text)}</div>"
            f"<div class='pt-safe-metric-value'>{html.escape(value_text)}</div>"
            f"{delta_html}"
            "</div>",
            unsafe_allow_html=True,
        )

    try:
        from streamlit.delta_generator import DeltaGenerator

        if getattr(DeltaGenerator.metric, "__name__", "") != "_safe_metric":
            DeltaGenerator.metric = _safe_metric
    except Exception:
        pass


_install_safe_metric_renderer()


cfg = load_config()
NEWS_CATALYSTS = {}


def status_card(label: str, value: str):
    st.markdown(f"""
    <div class='status-card'>
        <div class='status-label'>{label}</div>
        <div class='status-value'>{value}</div>
    </div>
    """, unsafe_allow_html=True)


def clean_card_text(value, limit: int | None = None) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]*$", " ", text)
    text = re.sub(r"<[^>]*>", " ", text)
    text = " ".join(text.replace(" | ", " • ").split())
    if limit:
        text = text[:limit]
    return html.escape(text)


def setup_card(row: dict):
    signal = str(row.get("Signal", "WAIT")).upper()
    symbol = str(row.get("Symbol", "")).upper()
    card_cls = "setup-card-call" if signal == "CALL" else "setup-card-put" if signal == "PUT" else "setup-card-wait"
    badge_cls = "badge-call" if signal == "CALL" else "badge-put" if signal == "PUT" else "badge-wait"
    quality = setup_quality_label(row.get("Score"), row.get("Confidence")) if "setup_quality_label" in globals() else "Setup"
    quality = clean_card_text(quality)
    catalyst_html = ""
    try:
        catalyst = NEWS_CATALYSTS.get(symbol) if isinstance(NEWS_CATALYSTS, dict) else None
        catalyst_html = render_catalyst_html(catalyst) if catalyst and render_catalyst_html else ""
    except Exception:
        catalyst_html = ""
    st.markdown(f"""
    <div class='setup-card {card_cls}'>
        <div class='setup-symbol'>{html.escape(symbol)}</div>
        <span class='setup-badge {badge_cls}'>{html.escape(signal)}</span>
        <div class='small-muted'>{quality}</div>
        <div style='margin-top:8px; display:grid; grid-template-columns:1fr 1fr; gap:6px;'>
            <div><div class='setup-label'>Score</div><b>{clean_card_text(row.get('Score', 'N/A'))}</b></div>
            <div><div class='setup-label'>Confidence</div><b>{clean_card_text(row.get('Confidence', 'N/A'))}</b></div>
            <div><div class='setup-label'>RVOL</div><b>{clean_card_text(row.get('RVOL', 'N/A'))}</b></div>
            <div><div class='setup-label'>ATR %</div><b>{clean_card_text(row.get('ATR %', 'N/A'))}</b></div>
        </div>
        {catalyst_html}
    </div>
    """, unsafe_allow_html=True)


def clean_ui_error(prefix: str, exc: Exception):
    st.error(f"{prefix}: {exc}")
    st.caption("Technical details are hidden in the dashboard. Check the terminal/log files if needed.")





def _empty_fig(title: str, y_title: str = "USD"):
    """Visual placeholder chart used before the first trade is logged."""
    x = pd.date_range(end=datetime.now(), periods=8, freq="D")
    y = [0, 0, 0, 0, 0, 0, 0, 0]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x, y=y, mode="lines+markers", name="Waiting for trades"))
    fig.update_layout(
        height=330,
        title=title,
        xaxis_title="Date",
        yaxis_title=y_title,
        margin=dict(l=20, r=20, t=50, b=30),
        annotations=[dict(
            text="No trade data yet",
            xref="paper", yref="paper", x=0.5, y=0.55,
            showarrow=False,
            font=dict(size=18, color="rgba(250,250,250,0.45)")
        )]
    )
    fig.update_yaxes(zeroline=True)
    return fig


def _format_pnl_chart(fig: go.Figure, height: int = 360) -> go.Figure:
    fig.update_layout(
        height=height,
        title_text=None,
        showlegend=False,
        hovermode="x unified",
        plot_bgcolor="#ffffff",
        paper_bgcolor="#ffffff",
        margin=dict(l=16, r=18, t=12, b=34),
        font=dict(color="#374151", size=13),
    )
    fig.update_xaxes(
        showgrid=False,
        title_text=None,
        tickfont=dict(color="#6b7280"),
        linecolor="#e5e7eb",
        zeroline=False,
    )
    fig.update_yaxes(
        tickprefix="$",
        title_text=None,
        separatethousands=True,
        gridcolor="#e5e7eb",
        zeroline=True,
        zerolinecolor="#e5e7eb",
        tickfont=dict(color="#6b7280"),
    )
    return fig


def _chart_card_title(title: str) -> None:
    st.markdown(
        f"<div style='font-size:1.05rem;font-weight:800;margin:0.1rem 0 0.45rem;color:#111827;'>{html.escape(title)} <span style='color:#6b7280;'>ⓘ</span></div>",
        unsafe_allow_html=True,
    )


def _render_perf_kpis(items: list[dict]) -> None:
    cards = []
    for item in items:
        label = html.escape(str(item.get("label", "")))
        value = str(item.get("value", ""))
        tone = str(item.get("tone", "neutral"))
        value_class = {
            "positive": "pt-kpi-positive",
            "negative": "pt-kpi-negative",
        }.get(tone, "")
        cards.append(
            "<div class='pt-kpi-card'>"
            f"<div class='pt-kpi-label'>{label}</div>"
            f"<div class='pt-kpi-value {value_class}'>{value}</div>"
            "</div>"
        )
    st.markdown(
        f"""
        <style>
        .pt-kpi-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
            gap: 0.75rem;
            margin: 0.85rem 0 1.15rem;
        }}
        .pt-kpi-card {{
            min-height: 86px;
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            background: #ffffff;
            padding: 0.82rem 0.95rem;
            overflow: hidden;
        }}
        .pt-kpi-label {{
            color: #6b7280;
            font-size: 0.86rem;
            font-weight: 700;
            line-height: 1.15;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        .pt-kpi-value {{
            color: #111827;
            font-size: 1.08rem;
            font-weight: 800;
            line-height: 1.2;
            margin-top: 0.45rem;
            overflow-wrap: anywhere;
        }}
        .pt-kpi-positive {{ color: #16833a; }}
        .pt-kpi-negative {{ color: #c2410c; }}
        </style>
        <div class="pt-kpi-grid">{''.join(cards)}</div>
        """,
        unsafe_allow_html=True,
    )


def render_empty_performance_dashboard():
    """Keep the Performance tab useful and visually complete before any trades exist."""
    st.markdown("### Performance Overview")
    st.caption("No trades have been logged yet. These panels will populate automatically after signals, entries, and exits are recorded.")

    _render_perf_kpis([
        {"label": "Net P/L", "value": "$0.00"},
        {"label": "Win Rate", "value": "0%"},
        {"label": "Entries", "value": "0"},
        {"label": "Closed", "value": "0"},
        {"label": "Avg Win / Loss", "value": "$0 / $0"},
        {"label": "Profit Factor", "value": "0.00"},
    ])

    f1, f2, f3 = st.columns(3)
    with f1:
        st.selectbox("Period", ["Today", "This Week", "This Month", "All Time", "Custom Range"], index=0, disabled=True)
    with f2:
        st.selectbox("Direction", ["All", "CALL", "PUT"], index=0, disabled=True)
    with f3:
        st.selectbox("Outcome", ["All", "Winners", "Losers", "Breakeven"], index=0, disabled=True)

    st.plotly_chart(_empty_fig("Equity Curve Preview", "Realized P/L USD"), use_container_width=True)

    chart_cols = st.columns(2)
    with chart_cols[0]:
        st.plotly_chart(_empty_fig("Daily P/L Preview", "Daily P/L USD"), use_container_width=True)
    with chart_cols[1]:
        st.plotly_chart(_empty_fig("Symbol P/L Preview", "P/L USD"), use_container_width=True)

    st.markdown("### Trade Replay Journal")
    placeholder = pd.DataFrame([{
        "timestamp": "Waiting for first setup",
        "event": "No data yet",
        "symbol": "—",
        "signal": "—",
        "score": "—",
        "confidence": "—",
        "option": "—",
        "status": "Replay snapshots will appear here"
    }])
    st.dataframe(placeholder, use_container_width=True, hide_index=True)

    action_cols = st.columns(4)
    action_cols[0].button("Download Journal", disabled=True)
    action_cols[1].button("Export Report", disabled=True)
    action_cols[2].button("Open Replay", disabled=True)
    action_cols[3].button("Reset Filters", disabled=True)




def _money_value(value, currency: str = "USD") -> str:
    """Format IBKR account values safely for dashboard display."""
    try:
        return f"{currency} {float(value):,.2f}"
    except Exception:
        return f"{currency} {value}" if value not in [None, ""] else "N/A"


def _number_or_none(value) -> float | None:
    try:
        if value in [None, ""]:
            return None
        return float(str(value).replace(",", ""))
    except Exception:
        return None


def fetch_ibkr_account_summary(ib_cfg: IBConfig) -> dict:
    """Fetch key account fields directly from IBKR/TWS."""
    ib = connect_ib(ib_cfg)
    summary = ib.accountSummary()
    accounts = sorted({item.account for item in summary if getattr(item, "account", None)})
    account_id = ib_cfg.account or (accounts[0] if accounts else "N/A")

    wanted = {
        "NetLiquidation": None,
        "TotalCashValue": None,
        "AvailableFunds": None,
        "BuyingPower": None,
        "MaintMarginReq": None,
        "UnrealizedPnL": None,
        "RealizedPnL": None,
    }

    currency = "USD"
    for item in summary:
        if item.tag in wanted and (account_id == "N/A" or not ib_cfg.account or item.account == account_id):
            wanted[item.tag] = item.value
            currency = item.currency or currency

    ib.disconnect()
    return {
        "account_id": account_id,
        "currency": currency,
        "connected": True,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        **wanted,
    }


def render_ibkr_account_summary(summary: dict):
    """Render professional account overview cards."""
    summary = summary or {}

    st.markdown("### IBKR Account Summary")
    status = "Connected" if summary.get("connected") else "Disconnected"
    st.caption(f"Status: {status} | Last synced: {summary.get('fetched_at', 'N/A')} | Account: {summary.get('account_id', 'N/A')}")
    if summary.get("error"):
        st.error(str(summary.get("error")))

    cur = summary.get("currency", "USD")
    a1, a2, a3, a4 = st.columns(4)
    a1.metric("Net Liquidation", _money_value(summary.get("NetLiquidation"), cur))
    a2.metric("Cash Balance", _money_value(summary.get("TotalCashValue"), cur))
    a3.metric("Available Funds", _money_value(summary.get("AvailableFunds"), cur))
    a4.metric("Buying Power", _money_value(summary.get("BuyingPower"), cur))

    b1, b2, b3, b4 = st.columns(4)
    b1.metric("Maintenance Margin", _money_value(summary.get("MaintMarginReq"), cur))
    b2.metric("Unrealized P/L", _money_value(summary.get("UnrealizedPnL", 0), cur))
    b3.metric("Realized P/L", _money_value(summary.get("RealizedPnL", 0), cur))
    b4.metric("Connection", "🟢 Connected" if summary.get("connected") else "🔴 Disconnected")


def save_and_rerun(new_cfg: dict):
    save_config(new_cfg)


def dashboard_auto_start_enabled(config: dict) -> bool:
    dashboard_cfg = config.get("dashboard", {}) if isinstance(config.get("dashboard", {}), dict) else {}
    return bool(dashboard_cfg.get("auto_start_engines", False))


TRADING_STYLE_PRESETS = {
    "Scalping": {
        "description": "Fast entries with tighter targets, stricter volume/liquidity gates, and smaller size.",
        "risk": {
            "max_trades_per_day": 3,
            "max_daily_capital_pct": 35.0,
            "max_spend_per_trade_pct": 15.0,
            "max_contracts": 2,
            "stop_loss_pct": 15.0,
            "take_profit_pct": 20.0,
            "breakeven_trigger_pct": 12.0,
            "trailing_trigger_pct": 18.0,
            "trailing_stop_pct": 8.0,
            "max_consecutive_losses": 2,
            "max_daily_drawdown_pct": 4.0,
            "entry_cutoff_hour": 11,
            "entry_cutoff_minute": 0,
        },
        "strategy": {
            "min_score": 92,
            "use_rvol_filter": False,
            "min_rvol": 0.8,
            "use_rvol_score": True,
            "use_rvol_ranking": True,
            "min_atr": 0.3,
            "top_n_tickers": 3,
            "orb_minutes": 5,
            "first_signal_minutes": 15,
            "min_session_bars": 2,
        },
        "option_filters": {
            "max_spread_pct": 6.0,
            "excellent_spread_pct": 3.0,
            "min_volume": 20,
        },
    },
    "Intraday": {
        "description": "Fewer trades with wider breathing room for clean momentum continuation.",
        "risk": {
            "max_trades_per_day": 2,
            "max_daily_capital_pct": 45.0,
            "max_spend_per_trade_pct": 22.0,
            "max_contracts": 3,
            "stop_loss_pct": 20.0,
            "take_profit_pct": 35.0,
            "breakeven_trigger_pct": 18.0,
            "trailing_trigger_pct": 28.0,
            "trailing_stop_pct": 12.0,
            "max_consecutive_losses": 2,
            "max_daily_drawdown_pct": 6.0,
            "entry_cutoff_hour": 11,
            "entry_cutoff_minute": 0,
        },
        "strategy": {
            "min_score": 90,
            "use_rvol_filter": False,
            "min_rvol": 0.5,
            "use_rvol_score": True,
            "use_rvol_ranking": True,
            "min_atr": 0.3,
            "top_n_tickers": 2,
            "orb_minutes": 15,
            "first_signal_minutes": 20,
            "min_session_bars": 7,
        },
        "option_filters": {
            "max_spread_pct": 8.0,
            "excellent_spread_pct": 4.0,
            "min_volume": 10,
        },
    },
}


def apply_trading_style_preset(config: dict, preset_name: str) -> dict:
    preset = TRADING_STYLE_PRESETS[preset_name]
    for section in ("risk", "strategy", "option_filters"):
        target = config.setdefault(section, {})
        target.update(preset.get(section, {}))

    risk = config.setdefault("risk", {})
    account_size = max(float(risk.get("account_size", 1000.0) or 1000.0), 1.0)
    risk["max_spend_per_trade"] = round(account_size * float(risk.get("max_spend_per_trade_pct", 0.0)) / 100.0, 2)
    risk["max_daily_capital"] = round(account_size * float(risk.get("max_daily_capital_pct", 0.0)) / 100.0, 2)
    config.setdefault("dashboard", {})["trading_style_preset"] = preset_name
    config.setdefault("dashboard", {})["trading_style_mode"] = preset_name
    return config


def render_platform_settings():
    st.markdown("### Platform Settings")
    st.header("Control Panel")
    approval_label = approval_mode_from_config(cfg)
    style_label = cfg.get("dashboard", {}).get("trading_style_mode", cfg.get("dashboard", {}).get("trading_style_preset", "Manual"))
    order_label = trading_status_from_config(cfg)
    max_trade_pct = float(cfg.get("risk", {}).get("max_spend_per_trade_pct", 0) or 0)
    max_daily_pct = float(cfg.get("risk", {}).get("max_daily_capital_pct", 0) or 0)
    scan_seconds = int(cfg.get("automation", {}).get("scan_interval_seconds", 60))
    st.markdown(
        f"""
        <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr)); gap:.65rem; margin:.4rem 0 1rem;">
            <div class="pt-safe-metric"><div class="pt-safe-metric-label">Mode</div><div class="pt-safe-metric-value">{html.escape(str(cfg.get("account_mode", "Simulation")))}</div></div>
            <div class="pt-safe-metric"><div class="pt-safe-metric-label">Approval</div><div class="pt-safe-metric-value">{html.escape(str(approval_label))}</div></div>
            <div class="pt-safe-metric"><div class="pt-safe-metric-label">Orders</div><div class="pt-safe-metric-value">{html.escape(str(order_label).replace(" / ", " "))}</div></div>
            <div class="pt-safe-metric"><div class="pt-safe-metric-label">Style</div><div class="pt-safe-metric-value">{html.escape(str(style_label))}</div></div>
            <div class="pt-safe-metric"><div class="pt-safe-metric-label">Trade / Day</div><div class="pt-safe-metric-value">{max_trade_pct:.1f}% / {max_daily_pct:.1f}%</div></div>
            <div class="pt-safe-metric"><div class="pt-safe-metric-label">Scan</div><div class="pt-safe-metric-value">{scan_seconds}s</div></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    tab_trading, tab_risk, tab_strategy, tab_watchlists, tab_advanced = st.tabs([
        "Trading",
        "Risk & Size",
        "Strategy",
        "Watchlists",
        "Advanced",
    ])

    with tab_trading:
        st.markdown("#### Trading Mode")
        dashboard_cfg = cfg.setdefault("dashboard", {})
        cfg["account_mode"] = st.radio("Trading account mode", ["Simulation", "Paper", "Live"], index=["Simulation", "Paper", "Live"].index(cfg.get("account_mode", "Simulation")), horizontal=True)
        dashboard_cfg["auto_start_engines"] = st.checkbox("Auto-start engine/news from dashboard", value=dashboard_auto_start_enabled(cfg))
        cfg["automation"]["enabled"] = st.checkbox("Enable engine automation", value=bool(cfg["automation"].get("enabled", False)))
        cfg["automation"]["place_orders"] = st.checkbox("Allow engine to place orders", value=bool(cfg["automation"].get("place_orders", False)))
        cfg["automation"]["confirm_order_risk"] = st.checkbox("I understand this can place IBKR orders", value=bool(cfg["automation"].get("confirm_order_risk", False)))
        current_approval_mode = approval_mode_from_config(cfg)
        cfg["automation"]["approval_mode"] = st.selectbox(
            "Entry approval mode",
            ["Dashboard", "Telegram", "Automatic"],
            index=["Dashboard", "Telegram", "Automatic"].index(current_approval_mode),
            help="Dashboard and Telegram pause scanner entries for approval. Automatic submits scanner trades immediately when all safety gates are armed.",
        )
        cfg["automation"]["require_trade_approval"] = cfg["automation"]["approval_mode"] == "Telegram"
        if cfg["account_mode"] == "Live":
            st.error("LIVE mode selected. Orders can use real money if all confirmations are enabled.")
            cfg["automation"]["live_confirm_text"] = st.text_input("Type TRADE LIVE to unlock live orders", value=str(cfg["automation"].get("live_confirm_text", "")))
        else:
            cfg["automation"]["live_confirm_text"] = ""
        cfg["automation"]["scan_interval_seconds"] = st.number_input("Engine scan interval seconds", value=int(cfg["automation"].get("scan_interval_seconds", 60)), min_value=10, max_value=3600, step=10)
        cfg["automation"]["live_sync_interval_seconds"] = st.number_input("IBKR live trade sync seconds", value=int(cfg["automation"].get("live_sync_interval_seconds", 15)), min_value=5, max_value=300, step=5)
        cfg["automation"]["scan_only_market_hours"] = st.checkbox("Scan only during market hours", value=bool(cfg["automation"].get("scan_only_market_hours", True)))
        cfg["order"]["type"] = st.selectbox("Order type", ["LIMIT", "MARKET"], index=0 if cfg["order"].get("type", "LIMIT") == "LIMIT" else 1)
        st.caption(f"Trading status: {trading_status_from_config(cfg)}")

    with tab_strategy:
        st.markdown("#### Strategy Preset")
        dashboard_cfg = cfg.setdefault("dashboard", {})
        style_options = ["Scalping", "Intraday", "Manual"]
        current_style = str(dashboard_cfg.get("trading_style_mode", dashboard_cfg.get("trading_style_preset", "Manual")))
        if current_style not in style_options:
            current_style = "Manual"
        selected_style = st.radio(
            "Trading style",
            style_options,
            index=style_options.index(current_style),
            horizontal=True,
            help="Preset modes overwrite and lock the preset-owned fields below. Manual unlocks them.",
        )
        dashboard_cfg["trading_style_mode"] = selected_style
        if selected_style in TRADING_STYLE_PRESETS:
            preserve_position_sizing = {}
            if selected_style == current_style:
                risk_cfg = cfg.get("risk", {})
                preserve_position_sizing = {
                    key: risk_cfg.get(key)
                    for key in ("max_spend_per_trade_pct", "max_daily_capital_pct")
                    if risk_cfg.get(key) is not None
                }
            apply_trading_style_preset(cfg, selected_style)
            if preserve_position_sizing:
                cfg.setdefault("risk", {}).update(preserve_position_sizing)
            preset = TRADING_STYLE_PRESETS[selected_style]
            st.caption(preset["description"])
            st.markdown(
                " | ".join(
                    [
                        f"ORB: {preset['strategy']['orb_minutes']}m",
                        f"Trades/day: {preset['risk']['max_trades_per_day']}",
                        f"Stop: {preset['risk']['stop_loss_pct']:.0f}%",
                        f"Target: {preset['risk']['take_profit_pct']:.0f}%",
                        f"Trail: +{preset['risk']['trailing_trigger_pct']:.0f}% / {preset['risk']['trailing_stop_pct']:.0f}%",
                        f"RVOL: {preset['strategy']['min_rvol']}",
                        f"Max spread: {preset['option_filters']['max_spread_pct']:.0f}%",
                        f"Min option volume: {preset['option_filters']['min_volume']}",
                    ]
                )
            )
            st.info("Preset mode is active. Position sizing percentages remain editable; switch to Manual to edit the other preset-owned fields.")
        else:
            st.caption("Manual mode is active. The controls below can be edited directly.")
            dashboard_cfg["trading_style_preset"] = "Manual"
        preset_locked = selected_style != "Manual"

    with tab_risk:
        st.markdown("#### Position Size")
        r = cfg["risk"]
        s = cfg["strategy"]
        buying_power = _number_or_none((st.session_state.get("ibkr_account_summary") or {}).get("AvailableFunds"))
        use_buying_power = st.checkbox("Use IBKR available funds as account size", value=bool(r.get("use_ibkr_buying_power", False)))
        r["use_ibkr_buying_power"] = bool(use_buying_power)
        if use_buying_power and buying_power is None:
            try:
                summary = cached_account_summary(
                    ib_cfg.host,
                    int(ib_cfg.port),
                    int(ib_cfg.client_id) + 305,
                    ib_cfg.account,
                    True,
                )
                summary["connected"] = True
                summary.setdefault("fetched_at", datetime.now().isoformat(timespec="seconds"))
                st.session_state["ibkr_account_summary"] = summary
                buying_power = _number_or_none(summary.get("AvailableFunds"))
            except Exception as exc:
                st.caption(f"IBKR available funds are not available yet, using saved account size. {exc}")

        if use_buying_power and buying_power is not None:
            r["account_size"] = round(float(buying_power), 2)
            st.number_input("Account size USD", value=float(r["account_size"]), min_value=0.0, step=100.0, disabled=True)
            st.caption(f"Using IBKR AvailableFunds: ${float(buying_power):,.2f}")
        else:
            r["account_size"] = st.number_input("Account size USD", value=float(r.get("account_size", 1000)), min_value=100.0, step=100.0)

        account_size = max(float(r.get("account_size", 1000) or 1000), 1.0)
        r["max_trades_per_day"] = st.number_input("Max trades per day", value=int(r.get("max_trades_per_day", 2)), min_value=1, max_value=10, step=1, disabled=preset_locked)
        s["top_n_tickers"] = st.number_input("Trade only top N tickers", value=int(s.get("top_n_tickers", 2)), min_value=1, max_value=10, step=1, disabled=preset_locked)
        default_trade_pct = float(r.get("max_spend_per_trade_pct", 0) or 0)
        if default_trade_pct <= 0:
            default_trade_pct = round(float(r.get("max_spend_per_trade", 250)) / account_size * 100, 2)
        default_daily_pct = float(r.get("max_daily_capital_pct", 0) or 0)
        if default_daily_pct <= 0:
            default_daily_pct = round(float(r.get("max_daily_capital", 500)) / account_size * 100, 2)

        r["max_spend_per_trade_pct"] = st.number_input("Max amount per trade % of account", value=float(default_trade_pct), min_value=0.1, max_value=100.0, step=0.5)
        r["max_daily_capital_pct"] = st.number_input("Max daily capital % of account", value=float(default_daily_pct), min_value=0.1, max_value=100.0, step=0.5)
        r["max_spend_per_trade"] = round(account_size * float(r["max_spend_per_trade_pct"]) / 100.0, 2)
        r["max_daily_capital"] = round(account_size * float(r["max_daily_capital_pct"]) / 100.0, 2)
        st.caption(f"Calculated limits: ${r['max_spend_per_trade']:,.2f} per trade | ${r['max_daily_capital']:,.2f} max daily capital")
        r["recycle_capital_after_exit"] = st.checkbox(
            "Recycle capital after closed trades",
            value=bool(r.get("recycle_capital_after_exit", False)),
            help="When enabled, closed trades free daily capital for later entries. Max trades per day still counts every entry.",
        )
        r["reserve_capital_for_remaining_trades"] = st.checkbox(
            "Reserve daily capital across remaining trades",
            value=bool(r.get("reserve_capital_for_remaining_trades", True)),
        )
        if r["reserve_capital_for_remaining_trades"]:
            reserved_budget = min(
                float(r["max_spend_per_trade"]),
                float(r["max_daily_capital"]) / max(int(r["max_trades_per_day"]), 1),
            )
            st.caption(f"First-entry reserved budget: about ${reserved_budget:,.2f} when no trades are open today.")
        r["max_contracts"] = st.number_input("Max contracts per trade", value=int(r.get("max_contracts", 2)), min_value=1, step=1, disabled=preset_locked)
        today_trade_count, today_deployed_capital = get_today_trade_stats()
        if r["recycle_capital_after_exit"]:
            from bot_core import get_open_position_deployed
            open_deployed_capital = get_open_position_deployed()
            st.caption(f"Today: {today_trade_count} trades | ${open_deployed_capital:,.2f} currently open | ${today_deployed_capital:,.2f} gross entries")
        else:
            st.caption(f"Today: {today_trade_count} trades | ${today_deployed_capital:,.2f} deployed")

    with tab_advanced:
        st.markdown("#### Performance Capital")
        perf = cfg.setdefault("performance", {})
        perf["original_deposited_capital"] = st.number_input(
            "Original deposited from bank transfer USD",
            value=float(perf.get("original_deposited_capital", 2300.0) or 2300.0),
            min_value=0.0,
            step=100.0,
        )
        st.caption("Used only for performance return math. Broker trade P/L still comes from synced IBKR executions.")

    with tab_risk:
        st.markdown("#### Trade Management")
        r = cfg["risk"]
        s = cfg["strategy"]
        orb_window_options = [5, 15, 30]
        current_orb_minutes = int(s.get("orb_minutes", 15))
        if current_orb_minutes not in orb_window_options:
            current_orb_minutes = 15
        s["orb_minutes"] = st.selectbox(
            "ORB window",
            orb_window_options,
            index=orb_window_options.index(current_orb_minutes),
            format_func=lambda minutes: f"{minutes} minutes",
            disabled=preset_locked,
        )
        s["min_session_bars"] = st.number_input("Minimum session bars", value=int(s.get("min_session_bars", 7)), min_value=2, max_value=30, step=1, disabled=preset_locked)
        r["stop_loss_pct"] = st.number_input("Option stop loss %", value=float(r.get("stop_loss_pct", 20.0)), min_value=1.0, max_value=90.0, step=1.0, disabled=preset_locked)
        r["take_profit_pct"] = st.number_input("Option take profit %", value=float(r.get("take_profit_pct", 30.0)), min_value=1.0, max_value=300.0, step=1.0, disabled=preset_locked)
        r["breakeven_trigger_pct"] = st.number_input("Move stop to breakeven at +%", value=float(r.get("breakeven_trigger_pct", 15.0)), min_value=1.0, max_value=200.0, step=1.0, disabled=preset_locked)
        r["trailing_trigger_pct"] = st.number_input("Activate trailing stop at +%", value=float(r.get("trailing_trigger_pct", 25.0)), min_value=1.0, max_value=300.0, step=1.0, disabled=preset_locked)
        r["trailing_stop_pct"] = st.number_input("Trailing stop distance %", value=float(r.get("trailing_stop_pct", 10.0)), min_value=1.0, max_value=90.0, step=1.0, disabled=preset_locked)
        r["entry_cutoff_hour"] = st.number_input("No new entries after hour ET", value=int(r.get("entry_cutoff_hour", 11)), min_value=9, max_value=15, step=1, disabled=preset_locked)
        r["entry_cutoff_minute"] = st.number_input("No new entries after minute ET", value=int(r.get("entry_cutoff_minute", 0)), min_value=0, max_value=59, step=1, disabled=preset_locked)
        r["force_exit_enabled"] = st.checkbox("Force exit open trades near end of day", value=bool(r.get("force_exit_enabled", True)))
        r["force_exit_hour"] = st.number_input("Force exit hour ET", value=int(r.get("force_exit_hour", 15)), min_value=9, max_value=15, step=1)
        r["force_exit_minute"] = st.number_input("Force exit minute ET", value=int(r.get("force_exit_minute", 55)), min_value=0, max_value=59, step=1)
        r["max_consecutive_losses"] = st.number_input("Stop after consecutive losses", value=int(r.get("max_consecutive_losses", 2)), min_value=1, max_value=10, step=1, disabled=preset_locked)
        r["max_daily_drawdown_pct"] = st.number_input("Max daily drawdown % of account", value=float(r.get("max_daily_drawdown_pct", 5.0)), min_value=1.0, max_value=50.0, step=1.0, disabled=preset_locked)

    with tab_strategy:
        st.markdown("#### Signal Filters")
        s = cfg["strategy"]
        s["option_dte"] = st.number_input("Target option DTE", value=int(s.get("option_dte", 7)), min_value=0, max_value=45, step=1)
        s["min_score"] = st.number_input("Minimum score", value=int(s.get("min_score", 70)), min_value=0, max_value=100, step=5, disabled=preset_locked)
        s["min_confidence"] = st.number_input("Minimum confidence", value=int(s.get("min_confidence", 75)), min_value=0, max_value=100, step=5)
        s["use_rvol_filter"] = st.checkbox("Use RVOL as required filter", value=bool(s.get("use_rvol_filter", False)), disabled=preset_locked)
        s["min_rvol"] = st.number_input("Minimum RVOL", value=float(s.get("min_rvol", 1.5)), min_value=0.0, max_value=10.0, step=0.1, disabled=preset_locked)
        s["use_rvol_score"] = st.checkbox("Use RVOL bonus in technical score", value=bool(s.get("use_rvol_score", False)), disabled=preset_locked)
        s["use_rvol_ranking"] = st.checkbox("Use RVOL bonus in ranking", value=bool(s.get("use_rvol_ranking", False)), disabled=preset_locked)
        s["min_atr"] = st.number_input("Minimum ATR %", value=float(s.get("min_atr", 0.3)), min_value=0.0, max_value=10.0, step=0.1, disabled=preset_locked)
        s["use_sr_filter"] = st.checkbox("Require room to nearest support/resistance", value=bool(s.get("use_sr_filter", True)))
        s["min_sr_room_pct"] = st.number_input("Minimum room to opposing level %", value=float(s.get("min_sr_room_pct", 0.75)), min_value=0.0, max_value=10.0, step=0.1)
        if not s["use_rvol_filter"]:
            st.caption("RVOL is informational only and will not block trades.")
        if s["use_sr_filter"]:
            st.caption("CALLs need room before nearest resistance; PUTs need room before nearest support.")

    with tab_strategy:
        st.markdown("#### Option Contract Filters")
        option_filters = cfg.setdefault("option_filters", {})
        option_filters["require_live_greeks"] = st.checkbox(
            "Require live IBKR Greeks",
            value=bool(option_filters.get("require_live_greeks", True)),
        )
        c1, c2 = st.columns(2)
        option_filters["max_spread_pct"] = c1.number_input(
            "Maximum spread %",
            value=float(option_filters.get("max_spread_pct", 10.0)),
            min_value=1.0,
            max_value=50.0,
            step=0.5,
            disabled=preset_locked,
        )
        option_filters["excellent_spread_pct"] = c2.number_input(
            "Excellent spread %",
            value=float(option_filters.get("excellent_spread_pct", 5.0)),
            min_value=0.5,
            max_value=20.0,
            step=0.5,
            disabled=preset_locked,
        )
        c3, c4 = st.columns(2)
        option_filters["min_abs_delta"] = c3.number_input(
            "Minimum absolute delta",
            value=float(option_filters.get("min_abs_delta", 0.45)),
            min_value=0.05,
            max_value=0.95,
            step=0.01,
        )
        option_filters["target_abs_delta"] = c4.number_input(
            "Target absolute delta",
            value=float(option_filters.get("target_abs_delta", 0.55)),
            min_value=0.05,
            max_value=0.95,
            step=0.01,
        )
        c5, c6 = st.columns(2)
        option_filters["max_abs_delta"] = c5.number_input(
            "Maximum absolute delta",
            value=float(option_filters.get("max_abs_delta", 0.80)),
            min_value=0.05,
            max_value=1.0,
            step=0.01,
        )
        option_filters["max_spread_dollars"] = c6.number_input(
            "Maximum spread $",
            value=float(option_filters.get("max_spread_dollars", 0.75)),
            min_value=0.0,
            max_value=10.0,
            step=0.05,
        )
        c7, c8 = st.columns(2)
        option_filters["max_theta_pct_of_mid"] = c7.number_input(
            "Maximum theta % of mid",
            value=float(option_filters.get("max_theta_pct_of_mid", 12.0)),
            min_value=1.0,
            max_value=100.0,
            step=0.5,
        )
        option_filters["min_bid"] = c8.number_input(
            "Minimum bid",
            value=float(option_filters.get("min_bid", 0.05)),
            min_value=0.0,
            max_value=10.0,
            step=0.05,
        )
        option_filters["min_volume"] = st.number_input(
            "Minimum option volume",
            value=int(option_filters.get("min_volume", 0)),
            min_value=0,
            max_value=100000,
            step=10,
            disabled=preset_locked,
        )
        st.caption("These are hard gates before live order placement. Spread and real delta now decide whether an option is tradeable.")

    with tab_watchlists:
        st.markdown("#### Manual Watchlist")
        current_watchlist = [str(x).strip().upper() for x in cfg.get("watchlist", WATCHLIST) if str(x).strip()]
        current_watchlist_set = set(current_watchlist)
        preset_watchlist_default = [ticker for ticker in WATCHLIST if ticker in current_watchlist_set]
        extra_watchlist_default = ", ".join([s for s in current_watchlist if s not in set(WATCHLIST)])
        if "platform_watchlist_presets" not in st.session_state:
            st.session_state["platform_watchlist_presets"] = preset_watchlist_default
        if "platform_watchlist_extra" not in st.session_state:
            st.session_state["platform_watchlist_extra"] = extra_watchlist_default
        selected_watchlist = st.multiselect(
            "Preset tickers",
            WATCHLIST,
            key="platform_watchlist_presets",
        )
        extra_watchlist_text = st.text_input("Add tickers", key="platform_watchlist_extra")
        extra_watchlist = [x.strip().upper() for x in extra_watchlist_text.replace("\n", ",").split(",") if x.strip()]
        cfg["watchlist"] = selected_watchlist + [x for x in extra_watchlist if x not in selected_watchlist]
        cfg.setdefault("strategy_lab", {})["symbols"] = list(cfg["watchlist"])

        st.divider()
        st.markdown("#### Dynamic Premarket Watchlist")
        dynamic = cfg.setdefault("dynamic_watchlist", {})
        dynamic["enabled"] = st.checkbox("Enable dynamic premarket watchlist", value=bool(dynamic.get("enabled", False)))
        dynamic["suggestive_only"] = st.checkbox(
            "Suggestive only",
            value=bool(dynamic.get("suggestive_only", True)),
            help="Show the premarket watchlist marquee without automatically adding symbols to the scanner watchlist.",
        )
        dynamic["mode"] = st.selectbox(
            "Dynamic loading mode",
            ["Manual", "Automatic"],
            index=1 if str(dynamic.get("mode", "Manual")).lower() == "automatic" else 0,
        )
        dynamic["max_symbols"] = int(st.number_input("Dynamic symbols to add", value=int(dynamic.get("max_symbols", 5)), min_value=1, max_value=20, step=1))
        d1, d2 = st.columns(2)
        dynamic["refresh_hour"] = int(d1.number_input("Auto build hour ET", value=int(dynamic.get("refresh_hour", 9)), min_value=4, max_value=15, step=1))
        dynamic["refresh_minute"] = int(d2.number_input("Auto build minute ET", value=int(dynamic.get("refresh_minute", 30)), min_value=0, max_value=59, step=1))
        default_universe = dynamic.get("source_universe") or cfg.get("watchlist", WATCHLIST)
        universe_text = st.text_area(
            "Premarket source universe",
            value=", ".join(default_universe),
            height=90,
            help="Ranks this list by premarket volume, headline-confirmed news, upgrades/downgrades, and earnings.",
        )
        dynamic["source_universe"] = [x.strip().upper() for x in universe_text.replace("\n", ",").split(",") if x.strip()]

        payload = load_dynamic_payload() if load_dynamic_payload else {}
        if payload:
            st.caption(f"Last dynamic build: {payload.get('generated_at', 'N/A')} | Symbols: {', '.join(payload.get('symbols', [])) or 'None'}")
        if st.button("Build Dynamic Watchlist Now", use_container_width=True):
            if build_premarket_watchlist is None:
                st.error("Dynamic watchlist module could not be loaded.")
            else:
                dynamic_ib = None
                try:
                    dynamic_ib_cfg = IBConfig(
                        host=ib_cfg.host,
                        port=ib_cfg.port,
                        client_id=ib_cfg.client_id + 105,
                        account=ib_cfg.account,
                        readonly=True,
                    )
                    dynamic_ib = connect_ib(dynamic_ib_cfg)
                    payload = build_premarket_watchlist(cfg, dynamic_ib)
                    st.success(f"Dynamic watchlist built: {', '.join(payload.get('symbols', [])) or 'No symbols selected'}")
                    if payload.get("rows"):
                        st.dataframe(pd.DataFrame(payload["rows"]).head(int(dynamic.get("max_symbols", 5))), use_container_width=True)
                except Exception as exc:
                    st.error(f"Dynamic watchlist build failed: {exc}")
                finally:
                    try:
                        if dynamic_ib and dynamic_ib.isConnected():
                            dynamic_ib.disconnect()
                    except Exception:
                        pass

        st.divider()
        st.markdown("#### Automatic IBKR Scanner")
        scanner_cfg = cfg.setdefault("scanner", {})
        scanner_cfg["auto_run_enabled"] = st.checkbox(
            "Run scanner automatically each morning",
            value=bool(scanner_cfg.get("auto_run_enabled", True)),
        )
        s1, s2 = st.columns(2)
        scanner_cfg["auto_run_hour"] = int(s1.number_input(
            "Auto scanner hour ET",
            value=int(scanner_cfg.get("auto_run_hour", 9)),
            min_value=4,
            max_value=15,
            step=1,
        ))
        scanner_cfg["auto_run_minute"] = int(s2.number_input(
            "Auto scanner minute ET",
            value=int(scanner_cfg.get("auto_run_minute", 40)),
            min_value=0,
            max_value=59,
            step=1,
        ))

    with tab_advanced:
        st.markdown("#### IBKR Connection")
        ibs = cfg["ib"]
        ibs["host"] = st.text_input("Host", value=ibs.get("host", "127.0.0.1"))
        if cfg.get("account_mode") == "Live":
            st.text_input("Active Port", value=str(ibs.get("live_port", 7496)), disabled=True)
        else:
            st.text_input("Active Port", value=str(ibs.get("paper_port", 7497)), disabled=True)
        ibs["paper_port"] = int(st.number_input("Paper port", value=int(ibs.get("paper_port", 7497)), step=1))
        ibs["live_port"] = int(st.number_input("Live port", value=int(ibs.get("live_port", 7496)), step=1))
        ibs["client_id"] = int(st.number_input("Client ID", value=int(ibs.get("client_id", 11)), step=1))
        ibs["account"] = st.text_input("Account ID optional", value=ibs.get("account", ""))
        ibs["readonly"] = st.checkbox("Read-only connection", value=bool(ibs.get("readonly", False)))

    with tab_advanced:
        st.markdown("#### IBKR Flex Historical Sync")
        flex = cfg.setdefault("ibkr_flex", {})
        flex["token"] = st.text_input(
            "Flex Web Service token",
            value=flex.get("token", os.getenv("IBKR_FLEX_TOKEN", "")),
            type="password",
        )
        flex["trade_query_id"] = st.text_input(
            "Flex trade query ID",
            value=flex.get("trade_query_id", os.getenv("IBKR_FLEX_TRADE_QUERY_ID", "")),
            type="password",
        )
        flex["base_url"] = st.text_input(
            "Flex base URL optional",
            value=flex.get("base_url", ""),
            placeholder="Leave blank for IBKR default",
        )
        st.caption("Used only for direct historical trade/P&L sync. This does not place orders.")

    with tab_advanced:
        st.markdown("#### Telegram")
        tg = cfg["telegram"]
        tg["bot_token"] = st.text_input("Bot token", value=tg.get("bot_token", os.getenv("TELEGRAM_BOT_TOKEN", "")), type="password")
        tg["chat_id"] = st.text_input("Chat ID", value=tg.get("chat_id", os.getenv("TELEGRAM_CHAT_ID", "")))
        tg["send_alerts"] = st.checkbox("Send Telegram alerts", value=bool(tg.get("send_alerts", False)))

    save_config(cfg)
    st.caption("Settings auto-saved to config.json")


with st.sidebar:
    st.markdown(
        """
        <div class="pt-sidebar-brand">
            <span class="pt-sidebar-brand-mark">〽</span>
            <span>PulseTrade AI</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    sidebar_page_labels = {
        "📊 Performance & Trade Journal": "📊  Overview",
        "🤖 AI AUDIT": "🤖  AI AUDIT",
        "💼 Positions": "💼  Live Trading",
        "📈 Strategy Lab": "🎯  Backtesting",
        "🧠 Market Intelligence": "🧠  Market Intel",
        "📈 Scanner & Breakdown": "📡  Scanner",
        "🏦 Account Status": "💳  Account",
        "📝 Logs": "📝  Logs",
    }
    sidebar_pages = [
        "📊 Performance & Trade Journal",
        "🤖 AI AUDIT",
        "💼 Positions",
        "📈 Strategy Lab",
        "🧠 Market Intelligence",
        "📈 Scanner & Breakdown",
        "🏦 Account Status",
        "📝 Logs",
    ]
    sidebar_page_slugs = {
        "📊 Performance & Trade Journal": "overview",
        "🤖 AI AUDIT": "ai-audit",
        "💼 Positions": "live-trading",
        "📈 Strategy Lab": "backtesting",
        "🧠 Market Intelligence": "market-intel",
        "📈 Scanner & Breakdown": "scanner",
        "🏦 Account Status": "account",
        "📝 Logs": "logs",
    }
    slug_to_sidebar_page = {slug: page for page, slug in sidebar_page_slugs.items()}
    slug_to_sidebar_page.update({
        "positions": "💼 Positions",
        "strategy": "📈 Strategy Lab",
        "price-action": "📈 Scanner & Breakdown",
    })
    query_page = st.query_params.get("page", "overview")
    default_sidebar_page = slug_to_sidebar_page.get(str(query_page), sidebar_pages[0])
    selected_page = st.radio(
        "Menu",
        sidebar_pages,
        index=sidebar_pages.index(default_sidebar_page),
        format_func=lambda page: sidebar_page_labels.get(page, page),
        label_visibility="collapsed",
        key="sidebar_selected_page",
    )
    selected_page_slug = sidebar_page_slugs.get(selected_page, "overview")
    if st.query_params.get("page") != selected_page_slug:
        st.query_params["page"] = selected_page_slug
    sidebar_health = read_health()
    engine_label = "Engine Running" if bool(sidebar_health.get("engine_running", False)) else "Engine Stopped"
    dot_color = "#16a34a" if bool(sidebar_health.get("engine_running", False)) else "#dc2626"
    st.markdown(
        f"""
        <div class="pt-sidebar-status">
            <div class="pt-sidebar-status-row"><span class="pt-sidebar-dot" style="background:{dot_color};"></span>{html.escape(engine_label)}</div>
            <div class="pt-sidebar-version">Version 1.0.0</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

# Light dashboard refresh so health updates while engine is running.
# Auto-refresh disabled while the Yahoo Backtester is active.
# Manual refresh is safer for long replay/simulation jobs.
# if not bool(st.session_state.get("bt_job_running", False)):
# Auto-refresh disabled to preserve scanner/research state while navigating.
# Use manual refresh/reconnect buttons when needed.
# st_autorefresh(interval=30_000, key="dashboard_refresh")

# Shared objects from saved config.
cfg = load_config()
ib_cfg = IBConfig(
    host=cfg["ib"].get("host", "127.0.0.1"),
    port=ib_port_from_config(cfg),
    client_id=int(cfg["ib"].get("client_id", 11)),
    account=cfg["ib"].get("account") or None,
    readonly=bool(cfg["ib"].get("readonly", False)),
)
tg_cfg = TelegramConfig(bot_token=cfg["telegram"].get("bot_token", ""), chat_id=cfg["telegram"].get("chat_id", ""))
symbols = cfg.get("watchlist", WATCHLIST)


def dashboard_ib_cfg(offset: int = 300, readonly: bool | None = None) -> IBConfig:
    return IBConfig(
        host=ib_cfg.host,
        port=ib_cfg.port,
        client_id=int(ib_cfg.client_id) + int(offset),
        account=ib_cfg.account,
        readonly=ib_cfg.readonly if readonly is None else bool(readonly),
    )

@st.cache_data(ttl=180, show_spinner=False)
def cached_catalyst_map(symbols_key: tuple[str, ...], min_impact: int = 70, lookback_hours: int = 24) -> dict:
    if get_catalyst_map is None:
        return {}
    try:
        return get_catalyst_map(list(symbols_key), min_impact=min_impact, lookback_hours=lookback_hours)
    except Exception:
        return {}


@st.cache_data(ttl=5, show_spinner=False)
def cached_socket_ping(host: str, port: int, timeout_seconds: float) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout_seconds):
            return True
    except Exception:
        return False


def render_premarket_watchlist_marquee(config: dict) -> None:
    if load_dynamic_payload is None:
        return
    dyn = config.get("dynamic_watchlist", {}) if isinstance(config.get("dynamic_watchlist", {}), dict) else {}
    if not bool(dyn.get("enabled", False)):
        return
    now_et = datetime.now(EASTERN)
    show_time = dtime(int(dyn.get("refresh_hour", 9)), int(dyn.get("refresh_minute", 35)))
    if now_et.time() < show_time:
        return
    payload = load_dynamic_payload() or {}
    if payload.get("date") != now_et.date().isoformat():
        return
    selected_symbols = set(normalize_symbols(payload.get("symbols", []))) if normalize_symbols else set(payload.get("symbols", []))
    rows = [
        row for row in payload.get("rows", [])
        if str(row.get("symbol", "")).upper() in selected_symbols
    ]
    if not rows:
        return
    rows = sorted(rows, key=lambda row: float(row.get("score", 0) or 0), reverse=True)
    items = []
    for row in rows[: int(dyn.get("max_symbols", 8) or 8)]:
        symbol = html.escape(str(row.get("symbol", "")))
        score = float(row.get("score", 0) or 0)
        reason = html.escape(str(row.get("reason") or "premarket activity"))
        items.append(f"<span class='pt-pm-item'><b>{symbol}</b> {score:.0f} - {reason}</span>")
    if not items:
        return
    content = "<span class='pt-pm-label'>Suggested Premarket Watchlist</span>" + "".join(items)
    st.markdown(
        f"""
        <style>
        .pt-pm-marquee {{
            border: 1px solid #d1d5db;
            border-radius: 8px;
            background: #ffffff;
            overflow: hidden;
            margin: 0.55rem 0 1.1rem;
            height: 42px;
            display: flex;
            align-items: center;
        }}
        .pt-pm-track {{
            display: inline-flex;
            align-items: center;
            gap: 1rem;
            white-space: nowrap;
            animation: ptPmScroll 72s linear infinite;
            padding-left: 100%;
            color: #111827;
            font-size: 0.94rem;
        }}
        .pt-pm-label {{
            font-weight: 800;
            color: #2563eb;
            margin-right: 0.4rem;
        }}
        .pt-pm-item {{
            display: inline-flex;
            gap: 0.35rem;
            align-items: center;
        }}
        .pt-pm-item b {{
            color: #111827;
        }}
        @keyframes ptPmScroll {{
            0% {{ transform: translateX(0); }}
            100% {{ transform: translateX(-100%); }}
        }}
        </style>
        <div class="pt-pm-marquee"><div class="pt-pm-track">{content}</div></div>
        """,
        unsafe_allow_html=True,
    )


def fetch_account_summary_dashboard_safe(summary_cfg: IBConfig) -> dict:
    payload = {
        "host": summary_cfg.host,
        "port": int(summary_cfg.port),
        "client_id": int(summary_cfg.client_id),
        "account": summary_cfg.account,
        "readonly": bool(summary_cfg.readonly),
    }
    code = (
        "import json, sys; "
        "from bot_core import IBConfig, fetch_ibkr_account_summary_dict; "
        "payload=json.loads(sys.argv[1]); "
        "cfg=IBConfig(payload['host'], int(payload['port']), int(payload['client_id']), payload.get('account') or None, bool(payload.get('readonly', True))); "
        "print(json.dumps(fetch_ibkr_account_summary_dict(cfg), default=str))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, json.dumps(payload)],
        cwd=str(Path(__file__).resolve().parent),
        text=True,
        capture_output=True,
        timeout=25,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "Account summary subprocess failed").strip()
        raise RuntimeError(message.splitlines()[-1] if message else "Account summary subprocess failed")
    output_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return json.loads(output_lines[-1] if output_lines else "{}")


@st.cache_data(ttl=30, show_spinner=False)
def cached_account_summary(host: str, port: int, client_id: int, account: str | None, readonly: bool) -> dict:
    summary_cfg = IBConfig(host=host, port=port, client_id=client_id, account=account, readonly=readonly)
    return fetch_account_summary_dashboard_safe(summary_cfg)


@st.cache_data(ttl=5, show_spinner=False)
def cached_ibkr_positions(host: str, port: int, client_id: int, account: str | None) -> list[dict]:
    positions_cfg = IBConfig(host=host, port=port, client_id=client_id, account=account, readonly=True)
    return fetch_ibkr_positions_list(positions_cfg)


def _portfolio_contract_keys(contract) -> set[str]:
    symbol = str(getattr(contract, "symbol", "") or "").upper()
    expiry = str(getattr(contract, "lastTradeDateOrContractMonth", "") or "")
    right = str(getattr(contract, "right", "") or "").upper()
    strike_value = _number_or_none(getattr(contract, "strike", None))
    strike = f"{strike_value:.4f}" if strike_value is not None else ""
    keys = set()
    con_id = getattr(contract, "conId", None)
    if con_id not in [None, "", 0]:
        keys.add(f"conid:{con_id}")
    local_symbol = str(getattr(contract, "localSymbol", "") or "").replace(" ", "").upper()
    if local_symbol:
        keys.add(f"local:{local_symbol}")
    if symbol and expiry and strike and right:
        keys.add(f"opt:{symbol}:{expiry}:{strike}:{right[:1]}")
    return keys


def _active_position_keys(position: dict) -> set[str]:
    symbol = str(position.get("symbol") or "").upper()
    expiry = str(position.get("expiry") or "")
    signal = str(position.get("signal") or position.get("type") or "").upper()
    right = "C" if signal.startswith("C") else "P" if signal.startswith("P") else signal[:1]
    strike_value = _number_or_none(position.get("strike"))
    strike = f"{strike_value:.4f}" if strike_value is not None else ""
    keys = set()
    con_id = position.get("con_id") or position.get("conId")
    if con_id not in [None, "", 0]:
        keys.add(f"conid:{con_id}")
    option_text = str(position.get("option") or "").replace(" ", "").upper()
    if option_text:
        keys.add(f"local:{option_text}")
    if symbol and expiry and strike and right:
        keys.add(f"opt:{symbol}:{expiry}:{strike}:{right[:1]}")
    return keys


@st.cache_data(ttl=2, show_spinner=False)
def cached_ibkr_portfolio_pnl(host: str, port: int, client_id: int, account: str | None) -> dict[str, dict]:
    portfolio_cfg = IBConfig(host=host, port=port, client_id=client_id, account=account, readonly=True)
    ib = connect_ib(portfolio_cfg)
    try:
        ib.sleep(2)
        by_key: dict[str, dict] = {}
        for item in ib.portfolio():
            contract = getattr(item, "contract", None)
            if contract is None:
                continue
            if account and getattr(item, "account", None) not in [None, "", account]:
                continue
            data = {
                "market_price": _number_or_none(getattr(item, "marketPrice", None)),
                "market_value": _number_or_none(getattr(item, "marketValue", None)),
                "average_cost": _number_or_none(getattr(item, "averageCost", None)),
                "unrealized_pnl": _number_or_none(getattr(item, "unrealizedPNL", None)),
                "realized_pnl": _number_or_none(getattr(item, "realizedPNL", None)),
                "position": _number_or_none(getattr(item, "position", None)),
                "local_symbol": str(getattr(contract, "localSymbol", "") or ""),
            }
            for key in _portfolio_contract_keys(contract):
                by_key[key] = data
        return by_key
    finally:
        try:
            if ib and ib.isConnected():
                ib.disconnect()
        except Exception:
            pass


# News catalysts are used only on scanner cards. Loading them for every page
# caused repeated SQLite work on normal navigation.
NEWS_CATALYSTS = {}
if selected_page == "📈 Scanner & Breakdown":
    symbols_key = tuple(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip())
    NEWS_CATALYSTS = cached_catalyst_map(symbols_key, min_impact=70, lookback_hours=24)

health = read_health()

def live_ibkr_ping(ib_cfg: IBConfig, timeout_seconds: float = 0.4) -> bool:
    """Fast dashboard-safe IBKR socket check.

    This intentionally does not call ib_insync/connect_ib from Streamlit, because
    ib_insync can raise "Timeout should be used inside a task" inside the
    Streamlit runtime. A successful socket connection means TWS/IB Gateway is
    listening on the configured host/port now.
    """
    return cached_socket_ping(ib_cfg.host, int(ib_cfg.port), float(timeout_seconds))

health["ib_connected"] = live_ibkr_ping(ib_cfg)
if not health["ib_connected"]:
    st.session_state["ibkr_connected"] = False
    if st.session_state.get("ibkr_account_summary", {}).get("connected"):
        st.session_state["ibkr_account_summary"] = {
            **st.session_state["ibkr_account_summary"],
            "connected": False,
            "error": f"IBKR is not reachable at {ib_cfg.host}:{ib_cfg.port}.",
        }


def sync_ibkr_account_status(force: bool = False) -> dict:
    connected_now = live_ibkr_ping(ib_cfg)
    if not connected_now:
        cached = st.session_state.get("ibkr_account_summary") or {}
        summary = {
            **cached,
            "account_id": cached.get("account_id") or ib_cfg.account or "Auto / All",
            "currency": cached.get("currency") or "USD",
            "connected": False,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"IBKR is not reachable at {ib_cfg.host}:{ib_cfg.port}. Open IB Gateway/TWS and confirm the API port.",
        }
        st.session_state["ibkr_account_summary"] = summary
        st.session_state["ibkr_connected"] = False
        health["ib_connected"] = False
        write_health(ib_connected=False, last_status="IBKR disconnected", last_error=summary["error"])
        return summary

    if not force and st.session_state.get("ibkr_account_summary", {}).get("connected"):
        health["ib_connected"] = True
        st.session_state["ibkr_connected"] = True
        return st.session_state["ibkr_account_summary"]

    try:
        if force:
            summary = fetch_account_summary_dashboard_safe(dashboard_ib_cfg(300, readonly=True))
        else:
            summary = cached_account_summary(
                ib_cfg.host,
                int(ib_cfg.port),
                int(ib_cfg.client_id) + 300,
                ib_cfg.account,
                True,
            )
        summary["connected"] = True
        summary.setdefault("fetched_at", datetime.now().isoformat(timespec="seconds"))
        st.session_state["ibkr_account_summary"] = summary
        st.session_state["ibkr_connected"] = True
        st.session_state.pop("ibkr_auto_connect_error", None)
        health["ib_connected"] = True
        write_health(ib_connected=True, last_status="Account summary synced")
        return summary
    except Exception as e:
        summary = {
            "account_id": ib_cfg.account or "Auto / All",
            "currency": "USD",
            "connected": False,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"IBKR account summary failed: {e}",
        }
        st.session_state["ibkr_account_summary"] = summary
        st.session_state["ibkr_connected"] = False
        st.session_state["ibkr_auto_connect_error"] = str(e)
        health["ib_connected"] = False
        write_health(ib_connected=False, last_status="Account summary failed", last_error=str(e))
        return summary


def enrich_active_positions_with_live_pnl(positions: list[dict]) -> tuple[pd.DataFrame, str | None]:
    if not positions:
        return pd.DataFrame(), None

    df = pd.DataFrame(positions).copy()
    for column in ["Live Price", "Live P/L", "Live P/L %", "Live Premium %", "Live Stock With Trade %"]:
        df[column] = pd.NA

    if not live_ibkr_ping(ib_cfg):
        return df, f"IBKR is not reachable at {ib_cfg.host}:{ib_cfg.port}."

    try:
        portfolio_by_key = cached_ibkr_portfolio_pnl(
            ib_cfg.host,
            int(ib_cfg.port),
            int(ib_cfg.client_id) + 209,
            ib_cfg.account,
        )
    except Exception:
        portfolio_by_key = {}

    live_ib = None
    try:
        live_ib = connect_ib(dashboard_ib_cfg(208, readonly=True))
        stock_price_cache: dict[str, float | None] = {}

        def set_live_display_metrics(row_idx: int, position: dict, live_price: float | None) -> None:
            entry = _number_or_none(position.get("entry_price"))
            if live_price is not None and live_price > 0 and entry is not None and entry > 0:
                df.at[row_idx, "Live Premium %"] = ((float(live_price) - float(entry)) / float(entry)) * 100.0

            underlying_entry = _number_or_none(position.get("underlying_entry_price"))
            symbol = str(position.get("symbol") or "").upper()
            if not symbol or underlying_entry is None or underlying_entry <= 0:
                return
            if symbol not in stock_price_cache:
                stock_price_cache[symbol] = get_stock_snapshot_price(live_ib, symbol)
            stock_price = _number_or_none(stock_price_cache.get(symbol))
            if stock_price is None or stock_price <= 0:
                return
            raw_move_pct = ((float(stock_price) - float(underlying_entry)) / float(underlying_entry)) * 100.0
            signal = str(position.get("signal") or "").upper()
            df.at[row_idx, "Live Stock With Trade %"] = -raw_move_pct if signal == "PUT" else raw_move_pct

        for idx, position in enumerate(positions):
            entry_price = _number_or_none(position.get("entry_price"))
            qty = _number_or_none(position.get("quantity"))
            if entry_price is None or entry_price <= 0 or qty is None or qty <= 0:
                continue

            portfolio_item = None
            for key in _active_position_keys(position):
                portfolio_item = portfolio_by_key.get(key)
                if portfolio_item:
                    break
            if portfolio_item:
                live_price = _number_or_none(portfolio_item.get("market_price"))
                pnl = _number_or_none(portfolio_item.get("unrealized_pnl"))
                average_cost = _number_or_none(portfolio_item.get("average_cost"))
                broker_qty = abs(float(_number_or_none(portfolio_item.get("position")) or qty))
                cost_basis = None
                if average_cost is not None and broker_qty > 0:
                    cost_basis = abs(average_cost * broker_qty)
                if cost_basis in [None, 0]:
                    cost_basis = abs(entry_price * float(qty) * 100.0)
                if live_price is not None and live_price > 100:
                    live_price = live_price / 100.0
                pnl_pct = (float(pnl) / float(cost_basis)) * 100.0 if pnl is not None and cost_basis else pd.NA
                if live_price is not None and live_price > 0:
                    df.at[idx, "Live Price"] = live_price
                    set_live_display_metrics(idx, position, live_price)
                if pnl is not None:
                    df.at[idx, "Live P/L"] = pnl
                    df.at[idx, "Live P/L %"] = pnl_pct
                    continue

            contract = reconstruct_option_contract(position)
            qualified = live_ib.qualifyContracts(contract)
            if qualified:
                contract = qualified[0]
            market = get_snapshot_mid(live_ib, contract)
            live_price = _number_or_none(market.get("Mid"))
            if live_price is None or live_price <= 0:
                continue
            pnl = (live_price - entry_price) * float(qty) * 100.0
            pnl_pct = ((live_price - entry_price) / entry_price) * 100.0
            df.at[idx, "Live Price"] = live_price
            df.at[idx, "Live P/L"] = pnl
            df.at[idx, "Live P/L %"] = pnl_pct
            set_live_display_metrics(idx, position, live_price)
        return df, None
    except Exception as exc:
        return df, display_exception_message(exc)
    finally:
        try:
            if live_ib and live_ib.isConnected():
                live_ib.disconnect()
        except Exception:
            pass


def _fmt_money_cell(value) -> str:
    number = _number_or_none(value)
    return "N/A" if number is None else f"${number:,.2f}"


def _fmt_pct_cell(value) -> str:
    number = _number_or_none(value)
    return "N/A" if number is None else f"{number:,.2f}%"


@st.fragment(run_every="5s")
def render_live_positions_fragment(cfg_snapshot: dict, ib_cfg_snapshot: IBConfig, health_snapshot: dict) -> None:
    st.markdown("### Live IBKR Positions")
    try:
        broker_positions = cached_ibkr_positions(
            ib_cfg_snapshot.host,
            int(ib_cfg_snapshot.port),
            int(ib_cfg_snapshot.client_id) + 205,
            ib_cfg_snapshot.account,
        )
        if broker_positions:
            broker_df = pd.DataFrame(broker_positions)
            preferred_cols = [
                "account",
                "symbol",
                "localSymbol",
                "secType",
                "position",
                "avgCost",
                "lastTradeDateOrContractMonth",
                "strike",
                "right",
                "currency",
            ]
            shown_cols = [col for col in preferred_cols if col in broker_df.columns]
            st.dataframe(broker_df[shown_cols] if shown_cols else broker_df, use_container_width=True, hide_index=True)
        else:
            st.info("No live positions found in IBKR.")
    except Exception as exc:
        st.warning(f"Could not fetch live IBKR positions: {display_exception_message(exc)}")

    st.markdown("### Bot-Managed Positions")
    active_positions = read_active_positions()
    if active_positions:
        active_df, live_pnl_error = enrich_active_positions_with_live_pnl(active_positions)
        if "premium_health" in active_df.columns:
            active_df["Premium Health"] = active_df["premium_health"].fillna("Not checked")
        else:
            active_df["Premium Health"] = "Not checked"
        if live_pnl_error:
            st.caption(f"Live P/L unavailable: {live_pnl_error}")

        live_rows = active_df.to_dict("records")
        for idx, position in enumerate(active_positions):
            live_row = live_rows[idx] if idx < len(live_rows) else position
            health_label = str(position.get("premium_health") or "Not checked")
            health_detail = str(position.get("premium_health_detail") or "")
            pnl_value = _number_or_none(live_row.get("Live P/L"))
            pnl_style = "color:#16833a;" if pnl_value and pnl_value > 0 else "color:#c2410c;" if pnl_value and pnl_value < 0 else ""
            live_premium_pct = _number_or_none(live_row.get("Live Premium %"))
            if live_premium_pct is None:
                live_premium_pct = _number_or_none(position.get("premium_change_pct"))
            live_stock_with_trade_pct = _number_or_none(live_row.get("Live Stock With Trade %"))
            if live_stock_with_trade_pct is None:
                live_stock_with_trade_pct = _number_or_none(position.get("underlying_move_with_position_pct"))
            display_health_label = health_label
            display_health_detail = health_detail
            if live_premium_pct is not None and live_stock_with_trade_pct is not None:
                display_health_detail = (
                    f"Live premium {live_premium_pct:.1f}%; stock is {live_stock_with_trade_pct:.2f}% with trade."
                )
                if live_premium_pct <= PREMIUM_HEALTH_WEAK_DROP_PCT:
                    display_health_label = (
                        "Weak - stop check pending"
                        if live_stock_with_trade_pct >= PREMIUM_HEALTH_UNDERLYING_TOLERANCE_PCT
                        else "Weak but stock against"
                    )
                else:
                    display_health_label = "Healthy"
            health_is_weak = "weak" in display_health_label.lower()
            with st.container(border=True):
                action_cols = st.columns([2.2, 2.2, 2.4])
                with action_cols[0]:
                    st.markdown(f"**{position.get('symbol', 'N/A')} {position.get('signal', '')}**")
                    st.caption(str(position.get("option") or ""))
                    st.caption(f"Qty {position.get('quantity', 'N/A')} | Entry {_fmt_money_cell(position.get('entry_price'))}")
                with action_cols[1]:
                    st.markdown(
                        f"""
                        <div style="display:grid; grid-template-columns: 1fr 1fr; gap:.35rem .7rem;">
                            <div><div style="color:#6b7280;font-weight:700;font-size:.78rem;">Live Price</div><div style="font-weight:800;">{_fmt_money_cell(live_row.get("Live Price"))}</div></div>
                            <div><div style="color:#6b7280;font-weight:700;font-size:.78rem;">Live P/L</div><div style="font-weight:800;{pnl_style}">{_fmt_money_cell(live_row.get("Live P/L"))}</div></div>
                            <div><div style="color:#6b7280;font-weight:700;font-size:.78rem;">Premium</div><div style="font-weight:800;">{_fmt_pct_cell(live_premium_pct)}</div></div>
                            <div><div style="color:#6b7280;font-weight:700;font-size:.78rem;">Stock With Trade</div><div style="font-weight:800;">{_fmt_pct_cell(live_stock_with_trade_pct)}</div></div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                with action_cols[2]:
                    if health_is_weak:
                        st.warning(f"{display_health_label}: {display_health_detail}" if display_health_detail else display_health_label)
                    else:
                        st.info(f"{display_health_label}: {display_health_detail}" if display_health_detail else display_health_label)
                    st.caption(
                        f"Stop {_fmt_money_cell(position.get('current_stop_price'))} | "
                        f"TP {_fmt_money_cell(position.get('take_profit_price'))}"
                    )
                st.caption("Close controls are below this live P/L area so they do not refresh every 5 seconds.")
    else:
        st.info("No bot-managed positions. Engine is waiting for a valid signal.")

    if st.button("Sync Bot Positions With IBKR", icon=":material/sync:", use_container_width=True):
        sync_ib = None
        try:
            sync_ib_cfg = IBConfig(
                host=ib_cfg_snapshot.host,
                port=ib_cfg_snapshot.port,
                client_id=ib_cfg_snapshot.client_id + 206,
                account=ib_cfg_snapshot.account,
                readonly=True,
            )
            sync_ib = connect_ib(sync_ib_cfg)
            restored_events = sync_active_positions_from_broker(sync_ib, account=ib_cfg_snapshot.account)
            reconciled_events = reconcile_active_positions_with_broker(sync_ib, account=ib_cfg_snapshot.account, log_closures=True)
            sync_events = restored_events + reconciled_events
            if sync_events:
                st.success(f"Synced {len(sync_events)} bot-managed position record(s).")
                st.dataframe(pd.DataFrame(sync_events), use_container_width=True, hide_index=True)
                st.rerun(scope="fragment")
            else:
                st.info("Bot-managed positions already match IBKR.")
        except Exception as exc:
            st.error(f"Position sync failed: {display_exception_message(exc)}")
        finally:
            try:
                if sync_ib and sync_ib.isConnected():
                    sync_ib.disconnect()
            except Exception:
                pass


def render_position_close_controls(cfg_snapshot: dict, ib_cfg_snapshot: IBConfig) -> None:
    orders_unlocked = orders_unlocked_from_config(cfg_snapshot)
    readonly = bool(cfg_snapshot.get("ib", {}).get("readonly", False))
    active_positions = read_active_positions()

    st.markdown("### Position Actions")
    if not active_positions:
        st.info("No bot-managed positions available to close.")
        return

    labels = []
    for idx, position in enumerate(active_positions):
        option = str(position.get("option") or "").strip()
        label = (
            f"{position.get('symbol', 'N/A')} {position.get('signal', '')} | "
            f"Qty {position.get('quantity', 'N/A')} | Entry {_fmt_money_cell(position.get('entry_price'))}"
        )
        labels.append(f"{label} | {option}" if option else label)

    selected_label = st.selectbox("Position to close", labels, key="close_position_selector")
    selected_idx = labels.index(selected_label)
    position = active_positions[selected_idx]
    position_id = str(position.get("id") or f"{position.get('symbol')}_{selected_idx}")
    estimated_cost = (
        float(_number_or_none(position.get("entry_price")) or 0)
        * float(_number_or_none(position.get("quantity")) or 0)
        * 100.0
    )

    with st.container(border=True):
        st.markdown(f"**Close Brief: {position.get('symbol', 'N/A')} {position.get('signal', '')}**")
        st.caption(str(position.get("option") or ""))
        brief_cols = st.columns(4)
        brief_cols[0].metric("Qty", position.get("quantity", "N/A"))
        brief_cols[1].metric("Entry", _fmt_money_cell(position.get("entry_price")))
        brief_cols[2].metric("Position Cost", f"${estimated_cost:,.2f}")
        brief_cols[3].metric("Order", "Market Sell")
        st.caption(
            f"Stop {_fmt_money_cell(position.get('current_stop_price'))} | "
            f"TP {_fmt_money_cell(position.get('take_profit_price'))}"
        )

        close_disabled = not orders_unlocked or readonly
        close_cols = st.columns([1, 2])
        with close_cols[0]:
            if st.button(
                "Close Position",
                key=f"close_market_static_{position_id}",
                type="primary",
                icon=":material/close:",
                use_container_width=True,
                disabled=close_disabled,
                help="Submit a market sell order for the selected option position.",
            ):
                close_ib = None
                try:
                    close_ib = connect_ib(dashboard_ib_cfg(309, readonly=False))
                    contract = reconstruct_option_contract(position)
                    qualified = close_ib.qualifyContracts(contract)
                    if qualified:
                        contract = qualified[0]
                    market = get_snapshot_mid(close_ib, contract)
                    exit_price = _number_or_none(market.get("Mid")) or _number_or_none(position.get("entry_price"))
                    trade, realized = submit_exit_order(
                        close_ib,
                        position,
                        exit_price,
                        "Dashboard market close",
                        account=ib_cfg_snapshot.account,
                        use_market=True,
                    )
                    closed_keys = _active_position_keys(position)
                    remaining_positions = []
                    for pos in read_active_positions():
                        same_id = bool(position.get("id")) and str(pos.get("id") or "") == position_id
                        same_contract = bool(closed_keys) and bool(_active_position_keys(pos) & closed_keys)
                        if same_id or same_contract:
                            continue
                        remaining_positions.append(pos)
                    write_active_positions(remaining_positions)
                    st.success(
                        f"Market close submitted for {position.get('symbol')} | "
                        f"status {getattr(trade.orderStatus, 'status', 'Submitted')} | est P/L ${realized:,.2f}"
                    )
                    send_position_closed_telegram_message(tg_cfg, {
                        "Symbol": position.get("symbol"),
                        "Option": position.get("option"),
                        "Action": "EXIT",
                        "Reason": "Dashboard market close",
                        "Quantity": position.get("quantity"),
                        "Entry": position.get("entry_price"),
                        "Current": exit_price,
                        "P/L $": realized,
                        "Status": str(getattr(trade.orderStatus, "status", "Submitted")),
                    })
                    st.rerun()
                except Exception as exc:
                    st.error(f"Market close failed: {display_exception_message(exc)}")
                finally:
                    try:
                        if close_ib and close_ib.isConnected():
                            close_ib.disconnect()
                    except Exception:
                        pass
        with close_cols[1]:
            if close_disabled:
                st.caption("Order safety gates required before this button is enabled.")
            else:
                st.caption("This action block is outside the live P/L refresh fragment.")


def style_live_pnl_table(df: pd.DataFrame):
    def color_pnl(value):
        number = _number_or_none(value)
        if number is None:
            return ""
        if number > 0:
            return "color: #16833a; font-weight: 700;"
        if number < 0:
            return "color: #c2410c; font-weight: 700;"
        return ""

    formatters = {
        "entry_price": lambda value: "" if _number_or_none(value) is None else f"${float(value):,.2f}",
        "underlying_entry_price": lambda value: "" if _number_or_none(value) is None else f"${float(value):,.2f}",
        "current_stop_price": lambda value: "" if _number_or_none(value) is None else f"${float(value):,.2f}",
        "take_profit_price": lambda value: "" if _number_or_none(value) is None else f"${float(value):,.2f}",
        "Live Price": lambda value: "" if _number_or_none(value) is None else f"${float(value):,.2f}",
        "Live P/L": lambda value: "" if _number_or_none(value) is None else f"${float(value):,.2f}",
        "Live P/L %": lambda value: "" if _number_or_none(value) is None else f"{float(value):,.2f}%",
        "premium_change_pct": lambda value: "" if _number_or_none(value) is None else f"{float(value):,.1f}%",
        "underlying_move_with_position_pct": lambda value: "" if _number_or_none(value) is None else f"{float(value):,.2f}%",
    }
    return df.style.format({key: value for key, value in formatters.items() if key in df.columns}).map(
        color_pnl,
        subset=[column for column in ["Live P/L", "Live P/L %"] if column in df.columns],
    )


def schedule_ibkr_reconnect_refresh() -> None:
    if not health.get("ib_connected"):
        st_autorefresh(interval=10_000, key="ibkr_reconnect_refresh")


PROJECT_ROOT = Path(__file__).resolve().parent
ENGINE_PID_FILE = PROJECT_ROOT / "data" / "trading_engine.pid"
ENGINE_LOG_FILE = PROJECT_ROOT / "logs" / "engine_stdout.log"
ENGINE_FILE = PROJECT_ROOT / "engine.py"
STATUS_ALERT_STATE_FILE = PROJECT_ROOT / "data" / "status_alert_state.json"
STATUS_ALERT_COOLDOWN_SECONDS = 300


def project_python() -> str:
    candidates = (
        PROJECT_ROOT / ".venv" / "bin" / "python",
        PROJECT_ROOT / ".venv" / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _pid_command_line(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        if os.name == "nt":
            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine",
            ]
        else:
            cmd = ["ps", "-p", str(pid), "-o", "command="]
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=2).strip()
    except Exception:
        return ""


def _is_engine_process(pid: int) -> bool:
    cmdline = _pid_command_line(pid).lower()
    return "engine.py" in cmdline and "python" in cmdline


def _read_engine_pid() -> int | None:
    try:
        if not ENGINE_PID_FILE.exists():
            return None
        raw = ENGINE_PID_FILE.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except Exception:
        return None


def get_trading_engine_process_status() -> tuple[bool, str]:
    pid = _read_engine_pid()
    if pid is None:
        return False, "No trading engine PID file."
    if _is_pid_running(pid) and _is_engine_process(pid):
        return True, f"Trading engine running as PID {pid}."
    try:
        ENGINE_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return False, "Stale trading engine PID removed."


def _read_status_alert_state() -> dict:
    try:
        if not STATUS_ALERT_STATE_FILE.exists():
            return {}
        with STATUS_ALERT_STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_status_alert_state(state: dict) -> None:
    try:
        STATUS_ALERT_STATE_FILE.parent.mkdir(exist_ok=True)
        with STATUS_ALERT_STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception:
        pass


def maybe_send_status_alerts(status_cfg: dict, status_health: dict, engine_running: bool) -> None:
    tg = status_cfg.get("telegram", {}) if isinstance(status_cfg.get("telegram", {}), dict) else {}
    tg_cfg_local = TelegramConfig(bot_token=tg.get("bot_token", ""), chat_id=tg.get("chat_id", ""))
    if not tg_cfg_local.bot_token or not tg_cfg_local.chat_id:
        return

    now = datetime.now(EASTERN)
    state = _read_status_alert_state()
    checks = {
        "ibkr_disconnected": {
            "bad": not bool(status_health.get("ib_connected")),
            "alert": "🚨 <b>PulseTrade Urgent</b>\n\nIBKR is disconnected.",
            "recovery": "✅ <b>PulseTrade Status</b>\n\nIBKR connection restored.",
        },
        "engine_not_running": {
            "bad": not bool(engine_running),
            "alert": "🚨 <b>PulseTrade Urgent</b>\n\nTrading engine is not running.",
            "recovery": "✅ <b>PulseTrade Status</b>\n\nTrading engine is running again.",
        },
    }

    changed = False
    for key, check in checks.items():
        item = state.get(key, {}) if isinstance(state.get(key), dict) else {}
        was_bad = bool(item.get("active", False))
        last_alert_at = item.get("last_alert_at")
        seconds_since_alert = STATUS_ALERT_COOLDOWN_SECONDS + 1
        try:
            seconds_since_alert = (now - datetime.fromisoformat(str(last_alert_at))).total_seconds()
        except Exception:
            pass

        if check["bad"]:
            if not was_bad or seconds_since_alert >= STATUS_ALERT_COOLDOWN_SECONDS:
                send_telegram_message(tg_cfg_local, check["alert"])
                item["last_alert_at"] = now.isoformat()
            item["active"] = True
            changed = True
        elif was_bad:
            send_telegram_message(tg_cfg_local, check["recovery"])
            item["active"] = False
            item["recovered_at"] = now.isoformat()
            changed = True

        state[key] = item

    if changed:
        _write_status_alert_state(state)


def start_trading_engine_once() -> None:
    running, message = get_trading_engine_process_status()
    if running:
        st.session_state["trading_engine_auto_start_status"] = message
        return
    if st.session_state.get("trading_engine_auto_start_checked") and "Stale" not in message and "No trading engine" not in message:
        return
    st.session_state["trading_engine_auto_start_checked"] = True
    if not ENGINE_FILE.exists():
        st.session_state["trading_engine_auto_start_status"] = f"Missing file: {ENGINE_FILE}"
        return

    try:
        ENGINE_PID_FILE.parent.mkdir(exist_ok=True)
        ENGINE_LOG_FILE.parent.mkdir(exist_ok=True)
        log_handle = ENGINE_LOG_FILE.open("a", encoding="utf-8")
        log_handle.write("\n--- Starting PulseTrade AI trading engine from dashboard ---\n")
        log_handle.flush()
        kwargs = {
            "cwd": str(PROJECT_ROOT),
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True
        elif os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        process = subprocess.Popen([project_python(), str(ENGINE_FILE)], **kwargs)
        ENGINE_PID_FILE.write_text(str(process.pid), encoding="utf-8")
        st.session_state["trading_engine_auto_start_status"] = f"Trading engine started as PID {process.pid}."
    except Exception as exc:
        st.session_state["trading_engine_auto_start_status"] = f"Trading engine auto-start failed: {exc}"


def auto_start_news_engine_once() -> None:
    if st.session_state.get("news_engine_auto_start_checked"):
        return
    st.session_state["news_engine_auto_start_checked"] = True

    news_cfg = cfg.get("news", {}) if isinstance(cfg.get("news", {}), dict) else {}
    if not bool(news_cfg.get("enabled", True)):
        st.session_state["news_engine_auto_start_status"] = "News engine auto-start disabled in config."
        return
    if get_news_engine_status is None or start_news_engine is None:
        st.session_state["news_engine_auto_start_status"] = "News engine controls unavailable."
        return

    try:
        status = get_news_engine_status()
        if not status.running:
            status = start_news_engine()
        st.session_state["news_engine_auto_start_status"] = status.message
    except Exception as exc:
        st.session_state["news_engine_auto_start_status"] = f"News engine auto-start failed: {exc}"


def start_telegram_decision_worker() -> None:
    if st.session_state.get("telegram_decision_worker_started"):
        return
    st.session_state["telegram_decision_worker_started"] = True

    def worker() -> None:
        while True:
            try:
                current_cfg = load_config()
                if bool(current_cfg.get("automation", {}).get("require_trade_approval", False)):
                    time.sleep(2)
                    continue
                current_ib_cfg = IBConfig(
                    host=current_cfg["ib"].get("host", "127.0.0.1"),
                    port=ib_port_from_config(current_cfg),
                    client_id=int(current_cfg["ib"].get("client_id", 11)) + 301,
                    account=current_cfg["ib"].get("account") or None,
                    readonly=bool(current_cfg["ib"].get("readonly", False)),
                )
                current_tg_cfg = TelegramConfig(
                    bot_token=current_cfg["telegram"].get("bot_token", ""),
                    chat_id=current_cfg["telegram"].get("chat_id", ""),
                )
                pending = [o for o in read_pending_approvals() if str(o.get("status", "")).lower() in ["pending", "sent"]]
                if pending:
                    has_live_order = any(not o.get("test_order") for o in pending)
                    ib_connected_now = live_ibkr_ping(current_ib_cfg)
                    ib = connect_ib(current_ib_cfg) if has_live_order and ib_connected_now else None
                    try:
                        can_trade = orders_unlocked_from_config(current_cfg) and ib_connected_now
                        process_telegram_order_callbacks(ib, current_ib_cfg, current_tg_cfg, can_trade)
                    finally:
                        try:
                            if ib:
                                ib.disconnect()
                        except Exception:
                            pass
            except Exception as exc:
                if not is_telegram_polling_noise(exc):
                    app_log(f"Telegram decision worker error: {exc}", "WARN")
            time.sleep(2)

    threading.Thread(target=worker, daemon=True, name="telegram-decision-worker").start()


def load_manual_option_defaults(symbol: str, signal: str, dte_target: int) -> dict:
    ib = connect_ib(dashboard_ib_cfg(302, readonly=True))
    try:
        stock = qualify_stock(ib, symbol)
        stock_market = get_snapshot_mid(ib, stock)
        stock_price = stock_market.get("Mid")
        if stock_price is None or pd.isna(stock_price) or float(stock_price) <= 0:
            stock_price = stock_market.get("Last")
        if stock_price is None or pd.isna(stock_price) or float(stock_price) <= 0:
            raise ValueError(f"No live stock price available for {symbol}.")

        expiry, strikes = get_option_expiry_and_strikes(ib, symbol, dte_target)
        if not expiry or not strikes:
            raise ValueError(f"No option chain found for {symbol}.")

        strike = min(strikes, key=lambda value: abs(float(value) - float(stock_price)))
        right = "C" if signal == "CALL" else "P"
        contract = Option(symbol, expiry, float(strike), right, "SMART", currency="USD", multiplier="100")
        qualified = ib.qualifyContracts(contract)
        if qualified:
            contract = qualified[0]
        option_market = get_snapshot_mid(ib, contract)
        mid = option_market.get("Mid")
        quote_warning = ""
        if mid is None or pd.isna(mid) or float(mid) <= 0:
            mid = 0.0
            quote_warning = (
                f"No live option quote returned for {symbol} {expiry} {strike:g} {signal}. "
                "This is common outside market hours or on illiquid contracts."
            )

        return {
            "expiry": expiry,
            "strike": float(strike),
            "mid": round(float(mid), 2),
            "limit": round(float(mid), 2),
            "underlying": round(float(stock_price), 2),
            "bid": None if pd.isna(option_market.get("Bid")) else round(float(option_market.get("Bid")), 2),
            "ask": None if pd.isna(option_market.get("Ask")) else round(float(option_market.get("Ask")), 2),
            "warning": quote_warning,
        }
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


def operational_order_label(config: dict, health_state: dict) -> str:
    mode = config.get("account_mode", "Simulation")
    automation = config.get("automation", {})
    if mode == "Simulation":
        return "🔵 Simulation"
    if not automation.get("enabled", False):
        return "🔒 Locked"
    if orders_unlocked_from_config(config):
        return "🟢 Armed"
    return "🟡 Safe"


def next_action_label(config: dict, health_state: dict) -> str:
    automation = config.get("automation", {})
    if not health_state.get("engine_running"):
        return "Start Engine"
    if not automation.get("enabled", False):
        return "Automation Off"
    if automation.get("scan_only_market_hours", True) and not is_market_open_now(config):
        return "Waiting Market"
    if not health_state.get("ib_connected"):
        return "Connect IBKR"
    status = str(health_state.get("last_status", "Monitoring"))
    if len(status) > 18:
        status = status[:18] + "…"
    return status

def render_status_overview():
    header_cols = st.columns(6)
    with header_cols[0]:
        status_card("ENGINE", "🟢 Running" if health.get("engine_running") else "⚪ Unknown")
    with header_cols[1]:
        status_card("IBKR", "🟢 Connected" if health.get("ib_connected") else "🔴 Disconnected")
    with header_cols[2]:
        status_card("MARKET", "🟢 Open" if is_market_open_now(cfg) else "🔴 Closed")
    with header_cols[3]:
        status_card("MODE", cfg.get("account_mode", "Simulation"))
    with header_cols[4]:
        status_card("ORDERS", operational_order_label(cfg, health))
    with header_cols[5]:
        status_card("NEXT ACTION", next_action_label(cfg, health))
    st.caption(f"Operational status: {operational_order_label(cfg, health).replace('🔵 ', '').replace('🟢 ', '').replace('🟡 ', '').replace('🔒 ', '')} | Config status: {trading_status_from_config(cfg)} | Last update: {health.get('updated_at', 'N/A')}")

def compact_status_card(label: str, value: str):
    """Render one compact health/status item using native Streamlit elements.

    This avoids the HTML clipping issue that hid the status titles at the top
    of the page.
    """
    with st.container(border=True):
        st.caption(label)
        st.markdown(f"**{value}**")


def render_compact_status_bar():
    status_cfg = load_config()
    status_health = read_health()
    engine_process_running, _engine_process_message = get_trading_engine_process_status()
    status_health["engine_running"] = engine_process_running
    status_health["ib_connected"] = live_ibkr_ping(ib_cfg)
    maybe_send_status_alerts(status_cfg, status_health, engine_process_running)
    items = [
        ("Engine Status", "🟢 Running" if status_health.get("engine_running") else "⚪ Unknown"),
        ("IBKR Status", "🟢 Connected" if status_health.get("ib_connected") else "🔴 Disconnected"),
        ("Market Condition", "🟢 Open" if is_market_open_now(status_cfg) else "🔴 Closed"),
        ("Account Mode", str(status_cfg.get("account_mode", "Simulation"))),
        ("Order Status", operational_order_label(status_cfg, status_health)),
        ("Next Action", next_action_label(status_cfg, status_health)),
    ]
    cards = "".join(
        "<div class='compact-status-card'>"
        f"<span class='compact-status-label'>{html.escape(label)}</span>"
        f"<span class='compact-status-value'>{html.escape(value)}</span>"
        "</div>"
        for label, value in items
    )
    st.markdown(
        f"<div class='fixed-status-dock'><div class='fixed-status-inner'>{cards}</div></div>",
        unsafe_allow_html=True,
    )


_status_fragment = getattr(st, "fragment", None) or getattr(st, "experimental_fragment", None)


if _status_fragment is not None:
    @_status_fragment(run_every="5s")
    def render_app_header():
        render_compact_status_bar()
else:
    def render_app_header():
        render_compact_status_bar()


def render_account_status_tab():
    st.subheader("IBKR Account Status")
    st.caption("Auto-connects on dashboard startup. Use these controls only when TWS/IB Gateway was restarted or account data needs a manual refresh.")
    account_summary = sync_ibkr_account_status()

    with st.expander("⚙️ Platform Settings", expanded=False):
        render_platform_settings()

    st.divider()

    control_cols = st.columns(4)
    with control_cols[0]:
        if st.button("Reconnect IBKR", use_container_width=True):
            account_summary = sync_ibkr_account_status(force=True)
            if account_summary.get("connected"):
                st.success("IBKR connected and account details synced.")
            else:
                st.error(account_summary.get("error", "IBKR connection failed."))
            st.rerun()

    with control_cols[1]:
        if st.button("Refresh Account", use_container_width=True):
            account_summary = sync_ibkr_account_status(force=True)
            if account_summary.get("connected"):
                st.success("Account summary synced.")
            else:
                st.error(account_summary.get("error", "Account summary failed."))
            st.rerun()

    with control_cols[2]:
        if st.button("Test Telegram", use_container_width=True):
            ok = send_telegram_message(tg_cfg, "AutoTrader Telegram test message.")
            if ok:
                st.success("Telegram sent")
            else:
                st.error("Telegram failed")

    with control_cols[3]:
        if st.button("Run One Engine Cycle Now", use_container_width=True):
            try:
                run_cycle()
                st.success("Engine cycle completed. Check logs/health below.")
            except Exception as e:
                st.error(f"Engine cycle failed: {e}")
                st.caption("Technical details are hidden in the dashboard. Check the terminal/log files if needed.")

    if not account_summary.get("connected") and st.session_state.get("ibkr_auto_connect_error"):
        st.warning(f"IBKR auto-connect pending: {st.session_state.get('ibkr_auto_connect_error')}")

    render_ibkr_account_summary(account_summary)

    st.markdown("### Connection Details")
    details = {
        "Host": ib_cfg.host,
        "Port": ib_cfg.port,
        "Client ID": ib_cfg.client_id,
        "Configured Account": ib_cfg.account or "Auto / All",
        "Read-only": ib_cfg.readonly,
        "Account Mode": cfg.get("account_mode", "Simulation"),
        "Trading Status": trading_status_from_config(cfg),
        "Engine Status": "Running" if health.get("engine_running") else "Unknown",
        "IBKR Status": "Connected" if health.get("ib_connected") else "Disconnected",
        "Last Health Update": health.get("updated_at", "N/A"),
    }
    st.json(details)



def render_yahoo_backtester_tab(config: dict, default_symbols: list[str]):
    st.subheader("Yahoo Historical Replay")
    st.caption("Phase 3: Yahoo/yfinance historical replay connected to the shared scanner strategy. This does not place trades and does not use IBKR.")

    if YahooDataClient is None or MarketReplayEngine is None:
        st.error("Backtester package could not be loaded.")
        st.caption("Make sure TradingBot/backtester/ contains __init__.py, data.py, replay.py, and strategy.py.")
        return

    if not YFINANCE_AVAILABLE:
        st.error("yfinance is not installed.")
        st.code("pip install yfinance pyarrow", language="bash")
        return

    strategy = config.get("strategy", {})
    risk = config.get("risk", {})

    st.markdown("### Setup")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        period = st.selectbox("Historical period", ["5d", "10d", "30d", "60d"], index=2, key="bt_period")
    with c2:
        interval = st.selectbox("Candle interval", ["5m", "15m", "30m", "60m", "1d"], index=0, key="bt_interval")
    with c3:
        max_symbols = st.number_input("Max symbols", min_value=1, max_value=20, value=min(5, max(1, len(default_symbols))), step=1, key="bt_max_symbols")
    with c4:
        force_refresh = st.checkbox("Force Yahoo refresh", value=False, key="bt_force_refresh")

    default_text = ", ".join(default_symbols[: int(max_symbols)]) if default_symbols else "SPY, QQQ, NVDA"
    symbols_text = st.text_area("Symbols to replay", value=default_text, height=80, key="bt_symbols_text")
    selected_symbols = [s.strip().upper() for s in symbols_text.replace("\n", ",").split(",") if s.strip()][: int(max_symbols)]

    with st.expander("Replay rules", expanded=False):
        r1, r2, r3, r4 = st.columns(4)
        with r1:
            orb_minutes = st.number_input("ORB window minutes", min_value=5, max_value=90, value=30, step=5, key="bt_orb_minutes")
        with r2:
            first_signal_minutes = st.number_input("First scanner minute", min_value=5, max_value=120, value=35, step=5, key="bt_first_signal_minutes")
        with r3:
            min_session_bars = st.number_input("Minimum bars before scanner", min_value=2, max_value=30, value=7, step=1, key="bt_min_session_bars")
        with r4:
            replay_mode = st.selectbox("Replay mode", ["Instant", "10x visual", "5x visual", "1x visual"], index=0, key="bt_replay_mode")

        st.caption(
            f"Current live filters shown for reference: score ≥ {strategy.get('min_score', 70)}, "
            f"confidence ≥ {strategy.get('min_confidence', 75)}, ATR% ≥ {strategy.get('min_atr', 0.3)}, "
            f"max trades/day {risk.get('max_trades_per_day', 2)}. Phase 3 applies scanner filters and records historical CALL/PUT signals."
        )

    st.markdown("### Step 1 — Historical Data")
    download_col, cache_col = st.columns([2, 1])
    with download_col:
        download_clicked = st.button("⬇ Download Historical Data", use_container_width=True, key="bt_download_data")
    with cache_col:
        clear_clicked = st.button("Clear Yahoo Cache", use_container_width=True, key="bt_clear_cache")

    client = YahooDataClient()

    # Persist Phase 3 replay output to disk as well as Streamlit session state.
    # Streamlit can rerun after button/progress updates; disk persistence makes results durable.
    bt_export_dir = os.path.join(os.path.dirname(__file__), "backtester", "exports")
    os.makedirs(bt_export_dir, exist_ok=True)
    replay_csv_path = os.path.join(bt_export_dir, "last_replay_log.csv")
    signals_csv_path = os.path.join(bt_export_dir, "last_scanner_signals.csv")
    sessions_csv_path = os.path.join(bt_export_dir, "last_sessions.csv")
    sim_trades_csv_path = os.path.join(bt_export_dir, "last_simulated_trades.csv")
    sim_metrics_json_path = os.path.join(bt_export_dir, "last_simulation_metrics.json")
    meta_json_path = os.path.join(bt_export_dir, "last_replay_meta.json")

    def _save_backtest_result(replay_df: pd.DataFrame, sessions_df: pd.DataFrame, signals_df: pd.DataFrame, meta: dict) -> None:
        st.session_state["bt_last_replay_log"] = replay_df
        st.session_state["bt_last_sessions"] = sessions_df
        st.session_state["bt_last_signals"] = signals_df
        st.session_state["bt_last_replay_meta"] = meta
        # New replay invalidates old simulated trades.
        st.session_state.pop("bt_last_sim_trades", None)
        st.session_state.pop("bt_last_sim_metrics", None)
        try:
            for _path in [sim_trades_csv_path, sim_metrics_json_path]:
                if os.path.exists(_path):
                    os.remove(_path)
        except Exception:
            pass
        try:
            replay_df.to_csv(replay_csv_path, index=False)
            sessions_df.to_csv(sessions_csv_path, index=False)
            signals_df.to_csv(signals_csv_path, index=False)
            with open(meta_json_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, default=str)
        except Exception as exc:
            st.warning(f"Replay finished, but disk save failed: {exc}")

    def _load_backtest_result_from_disk(force: bool = False) -> None:
        """Reload the last replay result from disk when Streamlit reruns.

        Streamlit reruns can leave the session key present but empty. In that case
        the old loader skipped disk reload, so the table disappeared. This loader
        only skips disk reload when a real non-empty replay DataFrame is already
        available in session state.
        """
        current = st.session_state.get("bt_last_replay_log")
        if not force and isinstance(current, pd.DataFrame) and not current.empty:
            return
        if not os.path.exists(replay_csv_path):
            return
        try:
            replay_df = pd.read_csv(replay_csv_path)
            if replay_df.empty:
                return
            sessions_df = pd.read_csv(sessions_csv_path) if os.path.exists(sessions_csv_path) else pd.DataFrame()
            signals_df = pd.read_csv(signals_csv_path) if os.path.exists(signals_csv_path) else pd.DataFrame()
            meta = {}
            if os.path.exists(meta_json_path):
                with open(meta_json_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            st.session_state["bt_last_replay_log"] = replay_df
            st.session_state["bt_last_sessions"] = sessions_df
            st.session_state["bt_last_signals"] = signals_df
            st.session_state["bt_last_replay_meta"] = meta
            if os.path.exists(sim_trades_csv_path):
                sim_trades_df = pd.read_csv(sim_trades_csv_path)
                st.session_state["bt_last_sim_trades"] = sim_trades_df
            if os.path.exists(sim_metrics_json_path):
                with open(sim_metrics_json_path, "r", encoding="utf-8") as f:
                    st.session_state["bt_last_sim_metrics"] = json.load(f)
        except Exception as exc:
            st.caption(f"Could not reload saved replay result: {exc}")

    _load_backtest_result_from_disk()

    if clear_clicked:
        removed = 0
        for symbol in selected_symbols:
            removed += client.clear_cache(symbol)
        st.session_state.pop("bt_loaded_data", None)
        st.session_state.pop("bt_loaded_meta", None)
        st.session_state.pop("bt_last_replay_log", None)
        st.session_state.pop("bt_last_sessions", None)
        st.session_state.pop("bt_last_replay_meta", None)
        st.session_state.pop("bt_last_signals", None)
        st.session_state.pop("bt_last_sim_trades", None)
        st.session_state.pop("bt_last_sim_metrics", None)
        for _path in [replay_csv_path, signals_csv_path, sessions_csv_path, sim_trades_csv_path, sim_metrics_json_path, meta_json_path]:
            try:
                if os.path.exists(_path):
                    os.remove(_path)
            except Exception:
                pass
        st.success(f"Removed {removed} cached file(s) and cleared last replay result.")

    if download_clicked:
        if not selected_symbols:
            st.warning("Add at least one symbol.")
            return

        progress = st.progress(0)
        status = st.empty()
        data = {}
        errors = []

        for i, symbol in enumerate(selected_symbols):
            try:
                status.info(f"Downloading {symbol} from Yahoo...")
                df = client.load(symbol=symbol, period=period, interval=interval, force_refresh=force_refresh)
                if df.empty:
                    errors.append({"symbol": symbol, "error": "Yahoo returned no candles"})
                else:
                    data[symbol] = df
            except Exception as exc:
                errors.append({"symbol": symbol, "error": str(exc)})
            progress.progress((i + 1) / max(len(selected_symbols), 1))

        if data:
            st.session_state["bt_loaded_data"] = data
            st.session_state["bt_loaded_meta"] = {
                "symbols": list(data.keys()),
                "period": period,
                "interval": interval,
                "loaded_at": datetime.now().isoformat(timespec="seconds"),
            }
            status.success("Historical data downloaded and cached.")
        else:
            status.error("No usable historical data was loaded.")

        if errors:
            with st.expander("Download warnings", expanded=True):
                st.dataframe(pd.DataFrame(errors), use_container_width=True, hide_index=True)

    loaded_data = st.session_state.get("bt_loaded_data", {})
    loaded_meta = st.session_state.get("bt_loaded_meta", {})

    if loaded_data:
        total_bars = sum(len(df) for df in loaded_data.values())
        d1, d2, d3, d4 = st.columns(4)
        d1.metric("Loaded Symbols", len(loaded_data))
        d2.metric("Loaded Candles", f"{total_bars:,}")
        d3.metric("Period", loaded_meta.get("period", period))
        d4.metric("Interval", loaded_meta.get("interval", interval))
        st.caption(f"Loaded at: {loaded_meta.get('loaded_at', 'N/A')} | Symbols: {', '.join(loaded_data.keys())}")
    else:
        st.info("Download historical data first. After that, the replay step can run from memory/cache without another Yahoo request.")

    with st.expander("Yahoo cache files", expanded=False):
        cache_info = client.cache_info()
        if cache_info.empty:
            st.info("No cache files found yet.")
        else:
            st.dataframe(cache_info, use_container_width=True, hide_index=True)

    st.markdown("### Step 2 — Replay Market")
    run_col, preview_col = st.columns([2, 1])
    with run_col:
        replay_clicked = st.button("▶ Replay Market", use_container_width=True, key="bt_replay_market")
    with preview_col:
        scanner_only = st.checkbox("Scanner-ready events only", value=False, key="bt_scanner_only")

    if replay_clicked:
        if not loaded_data:
            st.warning("Download historical data first, then run replay.")
            return

        try:
            replay_config = ReplayConfig(
                interval=interval,
                orb_minutes=int(orb_minutes),
                first_signal_minutes=int(first_signal_minutes),
                min_session_bars=int(min_session_bars),
            )
            engine = MarketReplayEngine(loaded_data, config=replay_config)
            sessions_df = engine.sessions()
            events = list(engine.events(only_scanner_allowed=bool(scanner_only)))
            total_events = len(events)

            if total_events == 0:
                st.warning("No replay events found for this data/rule combination.")
                return

            progress = st.progress(0)
            status = st.empty()
            live_box = st.empty()
            recent_rows = []
            log_rows = []
            signal_rows = []

            min_score = float(strategy.get("min_score", 70))
            min_confidence = float(strategy.get("min_confidence", 75))
            min_rvol = float(strategy.get("min_rvol", 1.5))
            min_atr = float(strategy.get("min_atr", 0.3))
            use_rvol_filter = bool(strategy.get("use_rvol_filter", False))
            use_rvol_score = bool(strategy.get("use_rvol_score", False))

            delay_map = {
                "Instant": 0.0,
                "10x visual": 0.001,
                "5x visual": 0.004,
                "1x visual": 0.015,
            }
            delay = delay_map.get(replay_mode, 0.0)
            update_every = max(1, total_events // 200)

            import time as _time

            for idx, event in enumerate(events, start=1):
                row = {
                    "#": idx,
                    "symbol": event.symbol,
                    "timestamp": event.timestamp,
                    "session_date": event.session_date,
                    "bar_number": event.bar_number,
                    "close": round(event.close, 2),
                    "scanner_allowed": event.scanner_allowed,
                    "new_session": event.is_new_session,
                    "session_close": event.is_session_close,
                }

                if event.scanner_allowed and scan_replay_history is not None:
                    scan_result = scan_replay_history(
                        symbol=event.symbol,
                        history=event.history,
                        daily=None,
                        use_rvol_score=use_rvol_score,
                        min_score=min_score,
                    )
                    clean_scan = clean_signal_row(scan_result) if clean_signal_row else None
                    if clean_scan:
                        row.update({
                            "signal": clean_scan.get("Signal"),
                            "score": clean_scan.get("Score"),
                            "confidence": clean_scan.get("Confidence"),
                            "rvol": clean_scan.get("RVOL"),
                            "atr_pct": clean_scan.get("ATR %"),
                        })

                        qualifies = is_top_candidate(
                            clean_scan,
                            min_score,
                            min_confidence,
                            min_rvol,
                            min_atr,
                            use_rvol_filter,
                            bool(strategy.get("use_sr_filter", True)),
                            float(strategy.get("min_sr_room_pct", 0.75)),
                        )

                        if qualifies:
                            signal_rows.append({
                                "timestamp": event.timestamp,
                                "symbol": event.symbol,
                                "signal": clean_scan.get("Signal"),
                                "score": clean_scan.get("Score"),
                                "confidence": clean_scan.get("Confidence"),
                                "price": clean_scan.get("Price"),
                                "rvol": clean_scan.get("RVOL"),
                                "atr_pct": clean_scan.get("ATR %"),
                                "vwap": clean_scan.get("VWAP"),
                                "orb_high": clean_scan.get("ORB High"),
                                "orb_low": clean_scan.get("ORB Low"),
                                "pdh": clean_scan.get("PDH"),
                                "pdl": clean_scan.get("PDL"),
                                "pdh_method": clean_scan.get("PDH Method"),
                                "reasons": clean_scan.get("Reasons"),
                            })

                log_rows.append(row)
                recent_rows.append(row)
                if len(recent_rows) > 15:
                    recent_rows = recent_rows[-15:]

                if idx == 1 or idx == total_events or idx % update_every == 0:
                    progress.progress(idx / total_events)
                    status.info(
                        f"Replaying {idx:,} / {total_events:,} candles | "
                        f"Signals: {len(signal_rows):,} | {event.symbol} | {event.timestamp.strftime('%Y-%m-%d %H:%M')}"
                    )
                    live_box.dataframe(pd.DataFrame(recent_rows), use_container_width=True, hide_index=True)

                if delay:
                    _time.sleep(delay)

            replay_log = pd.DataFrame(log_rows)
            signals_df = pd.DataFrame(signal_rows)
            scanner_events = replay_log[replay_log["scanner_allowed"] == True].copy() if not replay_log.empty else pd.DataFrame()

            replay_meta = {
                "completed_at": datetime.now().isoformat(timespec="seconds"),
                "symbols": list(loaded_data.keys()),
                "events": int(len(replay_log)),
                "scanner_events": int(len(scanner_events)),
                "signals": int(len(signals_df)),
                "sessions": int(len(sessions_df)) if not sessions_df.empty else 0,
                "scanner_only": bool(scanner_only),
                "interval": interval,
                "period": loaded_meta.get("period", period),
            }
            _save_backtest_result(replay_log, sessions_df, signals_df, replay_meta)
            status.success("Replay + scanner pass complete. Result saved under Last Replay Result.")

            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Replay Events", f"{len(replay_log):,}")
            m2.metric("Scanner Events", f"{len(scanner_events):,}")
            m3.metric("Signals", f"{len(signals_df):,}")
            m4.metric("Sessions", len(sessions_df) if not sessions_df.empty else 0)
            m5.metric("Symbols", len(loaded_data))

            if not signals_df.empty:
                st.markdown("### Historical Scanner Signals")
                st.dataframe(signals_df.tail(500), use_container_width=True, hide_index=True)
                st.download_button(
                    "Download scanner signals",
                    signals_df.to_csv(index=False),
                    "yahoo_phase3_scanner_signals.csv",
                    "text/csv",
                )
            else:
                st.info("No CALL/PUT signals passed the current scanner filters for this replay.")

            st.markdown("### Replay Timeline")
            st.dataframe(replay_log.tail(500), use_container_width=True, hide_index=True)
            st.download_button(
                "Download full replay log",
                replay_log.to_csv(index=False),
                "yahoo_phase3_replay_log.csv",
                "text/csv",
            )

            st.markdown("### Sessions")
            if sessions_df.empty:
                st.info("No sessions found.")
            else:
                st.dataframe(sessions_df, use_container_width=True, hide_index=True)
                st.download_button(
                    "Download sessions CSV",
                    sessions_df.to_csv(index=False),
                    "yahoo_replay_sessions.csv",
                    "text/csv",
                )

            st.session_state["bt_last_replay_log"] = replay_log
            st.session_state["bt_last_sessions"] = sessions_df
            st.session_state["bt_last_signals"] = signals_df
            st.session_state["bt_last_replay_meta"] = {
                "completed_at": datetime.now().isoformat(timespec="seconds"),
                "symbols": list(loaded_data.keys()),
                "events": int(len(replay_log)),
                "scanner_events": int(len(scanner_events)),
                "signals": int(len(signals_df)),
                "sessions": int(len(sessions_df)) if not sessions_df.empty else 0,
                "scanner_only": bool(scanner_only),
                "interval": interval,
                "period": loaded_meta.get("period", period),
            }

            first_symbol = next(iter(loaded_data.keys()))
            preview = loaded_data[first_symbol].tail(120).copy()
            fig = go.Figure()
            fig.add_trace(go.Candlestick(
                x=preview.index,
                open=preview["Open"],
                high=preview["High"],
                low=preview["Low"],
                close=preview["Close"],
                name=first_symbol,
            ))
            fig.update_layout(height=520, title=f"{first_symbol} Yahoo Data Preview", xaxis_rangeslider_visible=False)
            fig.update_xaxes(rangebreaks=[dict(bounds=[16, 9.5], pattern="hour"), dict(bounds=["sat", "mon"])])
            st.plotly_chart(fig, use_container_width=True)

        except Exception as exc:
            st.error(f"Replay failed: {exc}")
            st.caption("The dashboard is safe. This error is isolated to the Yahoo Backtester tab.")

    # Persist replay output across Streamlit reruns/autorefresh.
    # Without this block, the replay appears briefly and disappears on the next rerun.
    # Rerun-safe display: before rendering the result area, reload from disk
    # if the in-memory replay object is missing or empty.
    _memory_replay = st.session_state.get("bt_last_replay_log")
    if not isinstance(_memory_replay, pd.DataFrame) or _memory_replay.empty:
        _load_backtest_result_from_disk(force=True)

    saved_replay = st.session_state.get("bt_last_replay_log")
    saved_sessions = st.session_state.get("bt_last_sessions")
    saved_signals = st.session_state.get("bt_last_signals")
    saved_meta = st.session_state.get("bt_last_replay_meta", {})

    if isinstance(saved_replay, pd.DataFrame) and not saved_replay.empty:
        st.markdown("### Last Replay Result")
        st.caption(
            f"Completed at: {saved_meta.get('completed_at', 'N/A')} | "
            f"Symbols: {', '.join(saved_meta.get('symbols', []))} | "
            f"Period: {saved_meta.get('period', 'N/A')} | "
            f"Interval: {saved_meta.get('interval', 'N/A')}"
        )

        scanner_events_saved = saved_replay[saved_replay["scanner_allowed"] == True].copy() if "scanner_allowed" in saved_replay.columns else pd.DataFrame()
        rr1, rr2, rr3, rr4, rr5 = st.columns(5)
        rr1.metric("Replay Events", f"{len(saved_replay):,}")
        rr2.metric("Scanner Events", f"{len(scanner_events_saved):,}")
        rr3.metric("Signals", saved_meta.get("signals", 0))
        rr4.metric("Sessions", saved_meta.get("sessions", 0))
        rr5.metric("Symbols", len(saved_meta.get("symbols", [])))

        if isinstance(saved_signals, pd.DataFrame) and not saved_signals.empty:
            st.markdown("### Historical Scanner Signals")
            st.dataframe(saved_signals.tail(500), use_container_width=True, hide_index=True)
            st.download_button(
                "Download scanner signals",
                saved_signals.to_csv(index=False),
                "yahoo_phase3_scanner_signals.csv",
                "text/csv",
                key="bt_download_saved_signals",
            )

        st.markdown("### Replay Timeline")
        display_df = scanner_events_saved if bool(st.session_state.get("bt_scanner_only", False)) and not scanner_events_saved.empty else saved_replay
        st.dataframe(display_df.tail(500), use_container_width=True, hide_index=True)
        st.download_button(
            "Download full replay log",
            saved_replay.to_csv(index=False),
            "yahoo_phase2_replay_log.csv",
            "text/csv",
            key="bt_download_saved_replay",
        )

        st.markdown("### Sessions")
        if isinstance(saved_sessions, pd.DataFrame) and not saved_sessions.empty:
            st.dataframe(saved_sessions, use_container_width=True, hide_index=True)
            st.download_button(
                "Download sessions CSV",
                saved_sessions.to_csv(index=False),
                "yahoo_replay_sessions.csv",
                "text/csv",
                key="bt_download_saved_sessions",
            )
        else:
            st.info("No sessions found for the last replay.")



    st.markdown("### Step 3 — Simulate Option Trades")
    st.caption("Uses the historical CALL/PUT scanner signals and simulates approximate ~0.50-delta option trades. This is Yahoo-based approximation, not real historical IBKR option-chain pricing.")

    sim_col1, sim_col2, sim_col3, sim_col4 = st.columns(4)
    with sim_col1:
        sim_starting_capital = st.number_input("Backtest capital USD", min_value=100.0, value=float(risk.get("account_size", 1000)), step=100.0, key="bt_sim_capital")
        sim_max_trades = st.number_input("Max trades/day", min_value=1, max_value=10, value=int(risk.get("max_trades_per_day", 2)), step=1, key="bt_sim_max_trades")
        sim_max_contracts = st.number_input("Max contracts/trade", min_value=0, max_value=100, value=int(risk.get("max_contracts", 0) or 0), step=1, key="bt_sim_max_contracts", help="0 means no fixed contract cap.")
    with sim_col2:
        sim_spend = st.number_input("Max spend/trade USD", min_value=50.0, value=float(risk.get("max_spend_per_trade", 250)), step=50.0, key="bt_sim_spend")
        sim_daily_cap = st.number_input("Max daily capital USD", min_value=50.0, value=float(risk.get("max_daily_capital", 500)), step=50.0, key="bt_sim_daily_cap")
        sim_reserve_capital = st.checkbox("Reserve capital for remaining trades", value=bool(risk.get("reserve_capital_for_remaining_trades", True)), key="bt_sim_reserve_capital")
        sim_recycle_capital = st.checkbox("Recycle capital after exits", value=bool(risk.get("recycle_capital_after_exit", False)), key="bt_sim_recycle_capital")
    with sim_col3:
        sim_stop = st.number_input("Stop loss %", min_value=1.0, max_value=90.0, value=float(risk.get("stop_loss_pct", 20.0)), step=1.0, key="bt_sim_stop")
        sim_tp = st.number_input("Take profit %", min_value=1.0, max_value=300.0, value=float(risk.get("take_profit_pct", 30.0)), step=1.0, key="bt_sim_tp")
        sim_breakeven = st.number_input("Move stop to breakeven at +%", min_value=1.0, max_value=200.0, value=float(risk.get("breakeven_trigger_pct", 15.0)), step=1.0, key="bt_sim_breakeven")
        sim_trailing_trigger = st.number_input("Activate trailing stop at +%", min_value=1.0, max_value=300.0, value=float(risk.get("trailing_trigger_pct", 25.0)), step=1.0, key="bt_sim_trailing_trigger")
        sim_trailing_stop = st.number_input("Trailing stop distance %", min_value=1.0, max_value=90.0, value=float(risk.get("trailing_stop_pct", 10.0)), step=1.0, key="bt_sim_trailing_stop")
    with sim_col4:
        sim_premium_pct = st.number_input("Entry premium % of stock", min_value=0.5, max_value=10.0, value=2.5, step=0.1, key="bt_sim_premium_pct")
        sim_slippage = st.number_input("Slippage %", min_value=0.0, max_value=20.0, value=2.0, step=0.5, key="bt_sim_slippage")
        sim_entry_cutoff_hour = st.number_input("No entries after hour ET", min_value=9, max_value=15, value=int(risk.get("entry_cutoff_hour", 11)), step=1, key="bt_sim_entry_cutoff_hour")
        sim_entry_cutoff_minute = st.number_input("No entries after minute ET", min_value=0, max_value=59, value=int(risk.get("entry_cutoff_minute", 0)), step=1, key="bt_sim_entry_cutoff_minute")
        sim_force_exit_enabled = st.checkbox("Force exit near end of day", value=bool(risk.get("force_exit_enabled", True)), key="bt_sim_force_exit_enabled")
        sim_force_exit_hour = st.number_input("Force exit hour ET", min_value=9, max_value=15, value=int(risk.get("force_exit_hour", 15)), step=1, key="bt_sim_force_exit_hour")
        sim_force_exit_minute = st.number_input("Force exit minute ET", min_value=0, max_value=59, value=int(risk.get("force_exit_minute", 55)), step=1, key="bt_sim_force_exit_minute")
        sim_max_losses = st.number_input("Stop after consecutive losses", min_value=1, max_value=10, value=int(risk.get("max_consecutive_losses", 2)), step=1, key="bt_sim_max_losses")
        sim_max_daily_dd = st.number_input("Max daily drawdown %", min_value=1.0, max_value=50.0, value=float(risk.get("max_daily_drawdown_pct", 5.0)), step=1.0, key="bt_sim_max_daily_dd")

    sim_run_col, sim_note_col = st.columns([2, 1])
    with sim_run_col:
        simulate_clicked = st.button("▶ Simulate Option Trades", use_container_width=True, key="bt_simulate_options")
    with sim_note_col:
        allow_same_symbol = st.checkbox("Allow same symbol same day", value=False, key="bt_sim_same_symbol")

    if simulate_clicked:
        if simulate_option_trades is None or OptionSimulationConfig is None:
            st.error("Option simulator module could not be loaded. Make sure backtester/simulator.py exists.")
        elif not isinstance(saved_signals, pd.DataFrame) or saved_signals.empty:
            st.warning("Run Replay Market first and generate scanner signals before simulating trades.")
        else:
            try:
                sim_data = loaded_data if isinstance(loaded_data, dict) and loaded_data else {}
                if not sim_data:
                    sim_symbols = saved_meta.get("symbols", []) or sorted(saved_signals.get("symbol", pd.Series(dtype=str)).dropna().astype(str).unique().tolist())
                    sim_period = saved_meta.get("period", period)
                    sim_interval = saved_meta.get("interval", interval)
                    for _sym in sim_symbols:
                        try:
                            sim_data[_sym] = client.load(symbol=_sym, period=sim_period, interval=sim_interval, force_refresh=False)
                        except Exception:
                            pass

                sim_cfg = OptionSimulationConfig(
                    starting_capital=float(sim_starting_capital),
                    max_trades_per_day=int(sim_max_trades),
                    max_spend_per_trade=float(sim_spend),
                    max_daily_capital=float(sim_daily_cap),
                    recycle_capital_after_exit=bool(sim_recycle_capital),
                    reserve_capital_for_remaining_trades=bool(sim_reserve_capital),
                    max_contracts=int(sim_max_contracts),
                    stop_loss_pct=float(sim_stop),
                    take_profit_pct=float(sim_tp),
                    breakeven_trigger_pct=float(sim_breakeven),
                    trailing_trigger_pct=float(sim_trailing_trigger),
                    trailing_stop_pct=float(sim_trailing_stop),
                    entry_cutoff_time=dtime(int(sim_entry_cutoff_hour), int(sim_entry_cutoff_minute)),
                    force_exit_enabled=bool(sim_force_exit_enabled),
                    force_exit_time=dtime(int(sim_force_exit_hour), int(sim_force_exit_minute)),
                    max_consecutive_losses=int(sim_max_losses),
                    max_daily_drawdown_pct=float(sim_max_daily_dd),
                    premium_pct=float(sim_premium_pct) / 100.0,
                    slippage_pct=float(sim_slippage),
                    allow_same_symbol_same_day=bool(allow_same_symbol),
                )
                trades_df = simulate_option_trades(saved_signals, sim_data, sim_cfg)
                metrics = summarize_trades(trades_df, starting_capital=float(sim_starting_capital)) if summarize_trades else {}
                st.session_state["bt_last_sim_trades"] = trades_df
                st.session_state["bt_last_sim_metrics"] = metrics
                try:
                    trades_df.to_csv(sim_trades_csv_path, index=False)
                    with open(sim_metrics_json_path, "w", encoding="utf-8") as f:
                        json.dump(metrics, f, indent=2, default=str)
                except Exception as exc:
                    st.warning(f"Simulation completed, but save failed: {exc}")
                st.success(f"Simulated {len(trades_df):,} option trade(s).")
            except Exception as exc:
                st.error(f"Simulation failed: {exc}")
                st.caption("The error is isolated to the Yahoo Backtester simulator.")

    saved_sim_trades = st.session_state.get("bt_last_sim_trades")
    saved_sim_metrics = st.session_state.get("bt_last_sim_metrics", {})
    if (not isinstance(saved_sim_trades, pd.DataFrame) or saved_sim_trades.empty) and os.path.exists(sim_trades_csv_path):
        try:
            saved_sim_trades = pd.read_csv(sim_trades_csv_path)
            st.session_state["bt_last_sim_trades"] = saved_sim_trades
            if os.path.exists(sim_metrics_json_path):
                with open(sim_metrics_json_path, "r", encoding="utf-8") as f:
                    saved_sim_metrics = json.load(f)
                    st.session_state["bt_last_sim_metrics"] = saved_sim_metrics
        except Exception:
            saved_sim_trades = pd.DataFrame()

    if isinstance(saved_sim_trades, pd.DataFrame) and not saved_sim_trades.empty:
        st.markdown("### Simulated Option Performance")
        sm1, sm2, sm3, sm4, sm5, sm6 = st.columns(6)
        sm1.metric("Net P/L", f"${float(saved_sim_metrics.get('net_pnl', 0)):,.2f}")
        sm2.metric("Return", f"{float(saved_sim_metrics.get('return_pct', 0)):,.2f}%")
        sm3.metric("Win Rate", f"{float(saved_sim_metrics.get('win_rate', 0)):,.1f}%")
        sm4.metric("Profit Factor", saved_sim_metrics.get("profit_factor", 0))
        sm5.metric("Max DD", f"${float(saved_sim_metrics.get('max_drawdown', 0)):,.2f}")
        sm6.metric("Trades", int(saved_sim_metrics.get("total_trades", len(saved_sim_trades))))

        if "equity" in saved_sim_trades.columns:
            equity_fig = go.Figure()
            equity_fig.add_trace(go.Scatter(
                x=pd.to_datetime(saved_sim_trades.get("exit_time", saved_sim_trades.index), errors="coerce"),
                y=pd.to_numeric(saved_sim_trades["equity"], errors="coerce"),
                mode="lines+markers",
                name="Simulated Equity",
            ))
            equity_fig.update_layout(height=380, title="Simulated Equity Curve", xaxis_title="Exit Time", yaxis_title="Equity USD")
            st.plotly_chart(equity_fig, use_container_width=True)

        st.markdown("### Simulated Trades")
        key_cols = [c for c in ["trade_no", "entry_time", "exit_time", "date", "symbol", "signal", "score", "confidence", "entry_underlying", "exit_underlying", "entry_premium", "exit_premium", "quantity", "realized_pnl", "return_pct", "hold_minutes", "exit_reason", "reasons"] if c in saved_sim_trades.columns]
        st.dataframe(saved_sim_trades[key_cols].tail(500), use_container_width=True, hide_index=True)
        st.download_button(
            "Download simulated trades",
            saved_sim_trades.to_csv(index=False),
            "yahoo_simulated_option_trades.csv",
            "text/csv",
            key="bt_download_sim_trades",
        )

    st.info("Current phase: Yahoo replay simulates approximate option entries/exits and P/L. Accurate 7/14-DTE testing requires IBKR historical option bars.")


def scanner_result_paths() -> tuple[Path, Path, Path]:
    root = Path(__file__).resolve().parent
    data_dir = root / "data"
    export_dir = root / "exports"
    data_dir.mkdir(parents=True, exist_ok=True)
    export_dir.mkdir(parents=True, exist_ok=True)
    return (
        data_dir / "scanner_job_status.json",
        export_dir / "scanner_stock_results.csv",
        export_dir / "scanner_option_ideas.csv",
    )


def read_scanner_job_status() -> dict:
    status_file, _stock_file, _option_file = scanner_result_paths()
    try:
        if status_file.exists():
            status = json.loads(status_file.read_text(encoding="utf-8"))
            if status.get("status") == "running":
                timestamp = status.get("updated_at") or status.get("started_at")
                try:
                    started = datetime.fromisoformat(str(timestamp))
                    age_minutes = (datetime.now() - started).total_seconds() / 60.0
                    if age_minutes > 15:
                        return {
                            **status,
                            "status": "error",
                            "message": "Scanner job timed out while waiting for IBKR option data. Run it again.",
                        }
                except Exception:
                    pass
            return status
    except Exception:
        pass
    return {}


def write_scanner_job_status(status: dict) -> None:
    status_file, _stock_file, _option_file = scanner_result_paths()
    try:
        status_file.write_text(json.dumps(status, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


def display_exception_message(exc: Exception) -> str:
    return str(exc) or type(exc).__name__


def scanner_results_session_date(df: pd.DataFrame):
    if not isinstance(df, pd.DataFrame) or df.empty or "ORB Confirmation Time" not in df.columns:
        return None
    dates = pd.to_datetime(df["ORB Confirmation Time"].astype(str).str.slice(0, 10), errors="coerce")
    dates = dates.dropna()
    if dates.empty:
        return None
    return dates.max().date()


def load_saved_scanner_results() -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    status_file, stock_file, option_file = scanner_result_paths()
    status = read_scanner_job_status()
    stock_df = pd.DataFrame()
    option_df = pd.DataFrame()
    try:
        if stock_file.exists():
            stock_df = pd.read_csv(stock_file)
    except Exception:
        stock_df = pd.DataFrame()
    try:
        if option_file.exists():
            option_df = pd.read_csv(option_file)
    except Exception:
        option_df = pd.DataFrame()
    session_date = scanner_results_session_date(stock_df)
    today_et = datetime.now(EASTERN).date()
    if session_date and session_date != today_et:
        status = {
            **status,
            "status": "stale",
            "message": f"Scanner results are stale ({session_date}); run the IBKR scanner for today's data.",
        }
        stock_df = pd.DataFrame()
        option_df = pd.DataFrame()
    return stock_df, option_df, status


def run_ibkr_scanner_job(scan_cfg: dict, scan_symbols: list[str], source: str = "manual") -> None:
    _status_file, stock_file, option_file = scanner_result_paths()
    asyncio.set_event_loop(asyncio.new_event_loop())
    for path in (stock_file, option_file):
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
    write_scanner_job_status({
        "status": "running",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "run_date": datetime.now(EASTERN).date().isoformat(),
        "symbols": list(scan_symbols),
        "message": "Scanner running",
    })
    rows, option_rows, option_candidates = [], [], []
    ib = None
    try:
        scan_ib_cfg = IBConfig(
            host=scan_cfg["ib"].get("host", "127.0.0.1"),
            port=ib_port_from_config(scan_cfg),
            client_id=int(scan_cfg["ib"].get("client_id", 11)) + 100,
            account=scan_cfg["ib"].get("account") or None,
            readonly=bool(scan_cfg["ib"].get("readonly", False)),
        )
        ib = connect_ib(scan_ib_cfg)
        ib.RequestTimeout = 8
        for i, symbol in enumerate(scan_symbols):
            try:
                result = scan_symbol_ib(
                    ib,
                    symbol,
                    bool(scan_cfg["strategy"].get("use_rvol_score", False)),
                    str(scan_cfg["strategy"].get("active_strategy", "pmb")),
                    int(scan_cfg["strategy"].get("orb_minutes", 15)),
                    int(scan_cfg["strategy"].get("min_session_bars", 7)),
                )
                if result:
                    rows.append(clean_for_table(result))
                    if is_top_candidate(
                        result,
                        float(scan_cfg["strategy"].get("min_score", 70)),
                        float(scan_cfg["strategy"].get("min_confidence", 75)),
                        float(scan_cfg["strategy"].get("min_rvol", 1.5)),
                        float(scan_cfg["strategy"].get("min_atr", 0.3)),
                        bool(scan_cfg["strategy"].get("use_rvol_filter", False)),
                        bool(scan_cfg["strategy"].get("use_sr_filter", True)),
                        float(scan_cfg["strategy"].get("min_sr_room_pct", 0.75)),
                    ):
                        option_candidates.append(clean_for_table(result))
            except Exception as e:
                rows.append({"Symbol": symbol, "Signal": "ERROR", "Score": 0, "Confidence": 0, "RVOL": 0, "ATR %": 0, "Reasons": display_exception_message(e)})
            write_scanner_job_status({
                "status": "running",
                "started_at": read_scanner_job_status().get("started_at"),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "symbols": list(scan_symbols),
                "completed": i + 1,
                "total": len(scan_symbols),
                "message": f"Scanning {symbol}",
            })

        stock_df = pd.DataFrame(rows)
        if not stock_df.empty:
            stock_df = stock_df.sort_values(["Score", "Confidence", "RVOL", "ATR %"], ascending=[False, False, False, False])
        stock_df.to_csv(stock_file, index=False)

        option_candidate_df = pd.DataFrame(option_candidates)
        if not option_candidate_df.empty:
            option_candidate_df = option_candidate_df.sort_values(["Score", "Confidence", "RVOL", "ATR %"], ascending=[False, False, False, False])
            option_lookup_limit = max(1, int(scan_cfg["strategy"].get("top_n_tickers", 4)))
            priced_candidates = option_candidate_df.head(option_lookup_limit)
            for j, (_, row) in enumerate(priced_candidates.iterrows()):
                symbol = str(row["Symbol"])
                write_scanner_job_status({
                    "status": "running",
                    "started_at": read_scanner_job_status().get("started_at"),
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "symbols": list(scan_symbols),
                    "completed": len(scan_symbols),
                    "total": len(scan_symbols),
                    "message": f"Pricing options for {symbol} ({j + 1}/{len(priced_candidates)})",
                })
                try:
                    option = recommend_option_ib(
                        ib,
                        symbol,
                        row["Signal"],
                        float(row["Price"]),
                        int(scan_cfg["strategy"].get("option_dte", 7)),
                        scan_cfg.get("option_filters", {}),
                    )
                    if option:
                        option_clean = {k: v for k, v in option.items() if k != "Contract"}
                        option_rows.append({"Symbol": symbol, "Signal": row["Signal"], "Score": row["Score"], "Confidence": row["Confidence"], **option_clean})
                except Exception as e:
                    option_rows.append({"Symbol": symbol, "Signal": row["Signal"], "Score": row["Score"], "Confidence": row["Confidence"], "Option": "ERROR", "Option Score": 0, "Reasons": display_exception_message(e)})

        option_df = pd.DataFrame(option_rows)
        if not option_df.empty:
            option_df = option_df.sort_values(["Score", "Option Score"], ascending=[False, False])

        option_df.to_csv(option_file, index=False)
        final_status = "complete"
        final_message = "Scanner complete"
        if stock_df.empty:
            final_status = "no_data"
            final_message = "Scanner completed, but IBKR did not return today's completed ORB session bars yet. Try again after 09:50 ET."

        write_scanner_job_status({
            "status": final_status,
            "started_at": read_scanner_job_status().get("started_at"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "run_date": datetime.now(EASTERN).date().isoformat(),
            "symbols": list(scan_symbols),
            "completed": len(scan_symbols),
            "total": len(scan_symbols),
            "stock_rows": len(stock_df),
            "option_rows": len(option_df),
            "message": final_message,
        })
    except Exception as e:
        write_scanner_job_status({
            "status": "error",
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "symbols": list(scan_symbols),
            "message": display_exception_message(e),
        })
    finally:
        try:
            if ib and ib.isConnected():
                ib.disconnect()
        except Exception:
            pass


def run_ibkr_scanner_ui(scan_cfg: dict, scan_symbols: list[str]):
    status = read_scanner_job_status()
    if status.get("status") == "running":
        st.info(f"Scanner is already running: {status.get('completed', 0)} / {status.get('total', len(scan_symbols))} symbols.")
        st_autorefresh(interval=2_000, key="scanner_job_refresh")
        return
    threading.Thread(target=run_ibkr_scanner_job, args=(json.loads(json.dumps(scan_cfg, default=str)), list(scan_symbols), "manual"), daemon=True, name="ibkr-scanner-job").start()
    st.success("Scanner started in the background. You can switch tabs and come back for results.")
    st_autorefresh(interval=2_000, key="scanner_job_refresh_started")


def maybe_start_auto_ibkr_scanner(scan_cfg: dict, scan_symbols: list[str]) -> None:
    scanner_cfg = scan_cfg.get("scanner", {}) if isinstance(scan_cfg.get("scanner", {}), dict) else {}
    if not bool(scanner_cfg.get("auto_run_enabled", True)):
        return
    now_et = datetime.now(EASTERN)
    if now_et.weekday() >= 5:
        return
    run_time = dtime(int(scanner_cfg.get("auto_run_hour", 9)), int(scanner_cfg.get("auto_run_minute", 40)))
    if now_et.time() < run_time:
        return
    if now_et.time() >= dtime(16, 0):
        return

    status = read_scanner_job_status()
    today = now_et.date().isoformat()
    if status.get("status") == "running" or status.get("run_date") == today:
        return

    stock_df, _option_df, saved_status = load_saved_scanner_results()
    if saved_status.get("run_date") == today:
        return
    if scanner_results_session_date(stock_df) == now_et.date():
        return

    session_key = f"auto_ibkr_scanner_started_{today}"
    if st.session_state.get(session_key):
        return
    st.session_state[session_key] = True
    threading.Thread(
        target=run_ibkr_scanner_job,
        args=(json.loads(json.dumps(scan_cfg, default=str)), list(scan_symbols), "auto"),
        daemon=True,
        name="auto-ibkr-scanner-job",
    ).start()
    app_log(f"Auto IBKR scanner started for {today} at {run_time.strftime('%H:%M')} ET")


PRICE_ACTION_EXPORT = Path(__file__).resolve().parent / "backtester" / "exports" / "price_action_lab.csv"


def _price_action_top_candidate(result: dict, min_score: float, min_atr: float, use_rvol_filter: bool, min_rvol: float) -> bool:
    if not result or str(result.get("Signal", "")).upper() not in {"CALL", "PUT"}:
        return False
    try:
        if float(result.get("Score") or 0) < float(min_score):
            return False
        if float(result.get("ATR %") or 0) < float(min_atr):
            return False
        if bool(use_rvol_filter) and float(result.get("RVOL") or 0) < float(min_rvol):
            return False
    except Exception:
        return False
    return True


def _simulate_price_action_signal(
    session: pd.DataFrame,
    signal_time,
    direction: str,
    entry_price: float,
    stop_pct: float,
    target_r: float,
) -> dict:
    direction = str(direction).upper()
    entry_price = float(entry_price or 0)
    stop_pct = max(float(stop_pct or 1.0), 0.05)
    risk_dollars = max(entry_price * stop_pct / 100.0, 0.01)
    future = session[session.index > signal_time].copy()
    if future.empty or entry_price <= 0:
        return {"exit_reason": "No future bars", "r": 0.0, "false_breakout": True, "bars_held": 0}

    if direction == "CALL":
        stop_price = entry_price - risk_dollars
        target_price = entry_price + risk_dollars * float(target_r)
        max_favorable = 0.0
        for ts, bar in future.iterrows():
            high = float(bar["High"])
            low = float(bar["Low"])
            max_favorable = max(max_favorable, (high - entry_price) / risk_dollars)
            if low <= stop_price:
                return {"exit_time": ts, "exit_reason": "Stop", "r": -1.0, "false_breakout": max_favorable < 0.5, "bars_held": int(len(future[future.index <= ts]))}
            if high >= target_price:
                return {"exit_time": ts, "exit_reason": "Target", "r": float(target_r), "false_breakout": False, "bars_held": int(len(future[future.index <= ts]))}
        exit_price = float(future.iloc[-1]["Close"])
        r_value = (exit_price - entry_price) / risk_dollars
    else:
        stop_price = entry_price + risk_dollars
        target_price = entry_price - risk_dollars * float(target_r)
        max_favorable = 0.0
        for ts, bar in future.iterrows():
            high = float(bar["High"])
            low = float(bar["Low"])
            max_favorable = max(max_favorable, (entry_price - low) / risk_dollars)
            if high >= stop_price:
                return {"exit_time": ts, "exit_reason": "Stop", "r": -1.0, "false_breakout": max_favorable < 0.5, "bars_held": int(len(future[future.index <= ts]))}
            if low <= target_price:
                return {"exit_time": ts, "exit_reason": "Target", "r": float(target_r), "false_breakout": False, "bars_held": int(len(future[future.index <= ts]))}
        exit_price = float(future.iloc[-1]["Close"])
        r_value = (entry_price - exit_price) / risk_dollars

    r_value = round(float(r_value), 3)
    return {
        "exit_time": future.index[-1],
        "exit_reason": "Session close",
        "r": r_value,
        "false_breakout": bool(max_favorable < 0.5 and r_value <= 0),
        "bars_held": int(len(future)),
    }


def _max_drawdown_r(values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in values:
        equity += float(value)
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return round(abs(max_dd), 3)


def _summarize_price_action_rows(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    summary_rows = []
    for (symbol, orb), group in df.groupby(["symbol", "orb_minutes"]):
        r_values = [float(v) for v in group["r"].fillna(0).tolist()]
        wins = [v for v in r_values if v > 0]
        losses = [abs(v) for v in r_values if v < 0]
        trades = len(r_values)
        win_rate = (len(wins) / trades * 100.0) if trades else 0.0
        net_r = sum(r_values)
        avg_r = net_r / trades if trades else 0.0
        gross_win = sum(wins)
        gross_loss = sum(losses)
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (gross_win if gross_win > 0 else 0.0)
        false_breakouts = int(group["false_breakout"].fillna(False).astype(bool).sum())
        max_dd = _max_drawdown_r(r_values)
        daily = group.groupby("session_date")["r"].sum()
        consistency = (float((daily > 0).sum()) / max(len(daily), 1)) * 100.0
        outlier_penalty = 0.0
        if trades >= 3 and max(r_values) > max(net_r - max(r_values), 0) + 1.5:
            outlier_penalty = 8.0
        score = (
            net_r * 10.0
            + avg_r * 25.0
            + win_rate * 0.20
            + min(profit_factor, 4.0) * 6.0
            + consistency * 0.12
            + min(trades, 7) * 2.0
            - false_breakouts * 4.0
            - max_dd * 4.0
            - outlier_penalty
        )
        summary_rows.append({
            "Symbol": symbol,
            "ORB": f"{int(orb)}m",
            "ORB Minutes": int(orb),
            "Trades": trades,
            "Win Rate": round(win_rate, 1),
            "Avg R": round(avg_r, 2),
            "Net R": round(net_r, 2),
            "Profit Factor": round(profit_factor, 2),
            "Max Drawdown R": round(max_dd, 2),
            "False Breakouts": false_breakouts,
            "Consistency": round(consistency, 1),
            "Score": round(score, 1),
        })
    return pd.DataFrame(summary_rows).sort_values(["Symbol", "Score"], ascending=[True, False])


def run_price_action_lab(
    lab_symbols: list[str],
    period: str,
    force_refresh: bool,
    min_score: float,
    min_atr: float,
    use_rvol_filter: bool,
    min_rvol: float,
    stop_pct: float,
    target_r: float,
    max_symbols: int,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if YahooDataClient is None or MarketReplayEngine is None or ReplayConfig is None or scan_replay_history is None:
        raise RuntimeError("Strategy Lab dependencies are unavailable.")
    client = YahooDataClient()
    symbols_to_run = [str(s).strip().upper() for s in lab_symbols if str(s).strip()][:max_symbols]
    data = client.load_many(symbols_to_run, period=period, interval="5m", force_refresh=force_refresh)
    rows: list[dict] = []
    errors: list[dict] = []
    orb_values = [5, 15, 30]
    progress = st.progress(0)
    status = st.empty()
    total_steps = max(len(orb_values) * max(len(data), 1), 1)
    step = 0

    for orb_minutes in orb_values:
        first_signal_minutes = int(orb_minutes) + 5
        min_session_bars = max(2, int(round(orb_minutes / 5)) + 1)
        engine = MarketReplayEngine(data, config=ReplayConfig(interval="5m", orb_minutes=orb_minutes, first_signal_minutes=first_signal_minutes, min_session_bars=min_session_bars))
        seen: set[tuple[str, object]] = set()
        for symbol in data.keys():
            step += 1
            status.caption(f"Testing {symbol} with {orb_minutes}m ORB")
            progress.progress(min(step / total_steps, 1.0))
            try:
                for event in engine.events(symbols=[symbol], only_scanner_allowed=True):
                    key = (event.symbol, event.session_date)
                    if key in seen:
                        continue
                    result = scan_replay_history(
                        symbol=event.symbol,
                        history=event.history,
                        daily=None,
                        use_rvol_score=bool(cfg.get("strategy", {}).get("use_rvol_score", False)),
                        min_score=float(min_score),
                        orb_minutes=int(orb_minutes),
                    )
                    clean = clean_signal_row(result) if clean_signal_row else result
                    if not _price_action_top_candidate(clean or {}, min_score, min_atr, use_rvol_filter, min_rvol):
                        continue
                    session = engine.data[event.symbol][engine.data[event.symbol].index.date == event.session_date]
                    entry_price = float((clean or {}).get("Price") or event.close)
                    sim = _simulate_price_action_signal(session, event.timestamp, str((clean or {}).get("Signal")), entry_price, stop_pct, target_r)
                    rows.append({
                        "symbol": event.symbol,
                        "session_date": event.session_date,
                        "orb_minutes": int(orb_minutes),
                        "timestamp": event.timestamp,
                        "signal": (clean or {}).get("Signal"),
                        "score": (clean or {}).get("Score"),
                        "grade": (clean or {}).get("Grade"),
                        "entry_price": round(entry_price, 2),
                        "r": sim.get("r"),
                        "exit_reason": sim.get("exit_reason"),
                        "false_breakout": sim.get("false_breakout"),
                        "bars_held": sim.get("bars_held"),
                        "reasons": (clean or {}).get("Reasons"),
                    })
                    seen.add(key)
            except Exception as exc:
                errors.append({"symbol": symbol, "orb_minutes": orb_minutes, "error": str(exc)})
    progress.empty()
    status.empty()

    trades_df = pd.DataFrame(rows)
    summary_df = _summarize_price_action_rows(rows)
    recommendation_df = pd.DataFrame()
    if not summary_df.empty:
        recommendation_df = summary_df.sort_values(["Symbol", "Score"], ascending=[True, False]).groupby("Symbol", as_index=False).head(1)
        recommendation_df = recommendation_df.rename(columns={"ORB": "Recommended ORB", "Score": "Recommendation Score"})
    if not trades_df.empty:
        PRICE_ACTION_EXPORT.parent.mkdir(parents=True, exist_ok=True)
        trades_df.to_csv(PRICE_ACTION_EXPORT, index=False)
    return summary_df, recommendation_df, pd.DataFrame(errors)


def render_price_action_lab_tab(cfg: dict, symbols: list[str]) -> None:
    st.subheader("Price Action Lab")
    st.caption("Compare 5m, 15m, and 30m ORB behaviour per ticker. This is research only and does not change live trading.")
    lab_cfg = cfg.setdefault("price_action_lab", {})
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        period = st.selectbox("Lookback", ["7d", "10d", "30d", "60d"], index=["7d", "10d", "30d", "60d"].index(str(lab_cfg.get("period", "10d"))) if str(lab_cfg.get("period", "10d")) in ["7d", "10d", "30d", "60d"] else 1, key="pal_period")
    with c2:
        max_symbols = int(st.number_input("Max symbols", min_value=1, max_value=max(len(symbols), 1), value=min(int(lab_cfg.get("max_symbols", 12)), max(len(symbols), 1)), step=1, key="pal_max_symbols"))
    with c3:
        min_score = float(st.number_input("Minimum score", min_value=50.0, max_value=100.0, value=float(lab_cfg.get("min_score", cfg.get("strategy", {}).get("min_score", 90))), step=1.0, key="pal_min_score"))
    with c4:
        min_atr = float(st.number_input("Minimum ATR %", min_value=0.0, max_value=5.0, value=float(lab_cfg.get("min_atr", cfg.get("strategy", {}).get("min_atr", 0.3))), step=0.05, key="pal_min_atr"))

    c5, c6, c7, c8 = st.columns(4)
    with c5:
        stop_pct = float(st.number_input("Underlying stop %", min_value=0.1, max_value=10.0, value=float(lab_cfg.get("stop_pct", 0.75)), step=0.05, key="pal_stop_pct"))
    with c6:
        target_r = float(st.number_input("Target R", min_value=0.25, max_value=5.0, value=float(lab_cfg.get("target_r", 1.5)), step=0.25, key="pal_target_r"))
    with c7:
        use_rvol_filter = st.checkbox("Use RVOL filter", value=bool(lab_cfg.get("use_rvol_filter", cfg.get("strategy", {}).get("use_rvol_filter", False))), key="pal_use_rvol")
    with c8:
        min_rvol = float(st.number_input("Minimum RVOL", min_value=0.0, max_value=10.0, value=float(lab_cfg.get("min_rvol", cfg.get("strategy", {}).get("min_rvol", 1.5))), step=0.1, key="pal_min_rvol"))

    selected_symbols = st.multiselect("Symbols", options=symbols, default=list(symbols[:max_symbols]), key="pal_symbols")
    force_refresh = st.checkbox("Force data refresh", value=False, key="pal_force_refresh")
    lab_cfg.update({
        "period": period,
        "max_symbols": max_symbols,
        "min_score": min_score,
        "min_atr": min_atr,
        "stop_pct": stop_pct,
        "target_r": target_r,
        "use_rvol_filter": bool(use_rvol_filter),
        "min_rvol": min_rvol,
    })
    save_config(cfg)

    if st.button("Run Price Action Lab", type="primary", use_container_width=True):
        try:
            summary_df, recommendation_df, errors_df = run_price_action_lab(
                selected_symbols or symbols,
                period,
                force_refresh,
                min_score,
                min_atr,
                bool(use_rvol_filter),
                min_rvol,
                stop_pct,
                target_r,
                max_symbols,
            )
            st.session_state["price_action_summary"] = summary_df
            st.session_state["price_action_recommendations"] = recommendation_df
            st.session_state["price_action_errors"] = errors_df
            st.success("Price Action Lab complete.")
        except Exception as exc:
            st.error(f"Price Action Lab failed: {display_exception_message(exc)}")

    summary_df = st.session_state.get("price_action_summary", pd.DataFrame())
    recommendation_df = st.session_state.get("price_action_recommendations", pd.DataFrame())
    errors_df = st.session_state.get("price_action_errors", pd.DataFrame())

    if isinstance(recommendation_df, pd.DataFrame) and not recommendation_df.empty:
        st.markdown("### Recommendations")
        st.dataframe(recommendation_df, use_container_width=True, hide_index=True)
    if isinstance(summary_df, pd.DataFrame) and not summary_df.empty:
        st.markdown("### ORB Comparison")
        st.dataframe(summary_df, use_container_width=True, hide_index=True)
        st.download_button("Download Price Action Lab CSV", summary_df.to_csv(index=False), "price_action_lab_summary.csv", "text/csv", key="download_price_action_summary")
    elif PRICE_ACTION_EXPORT.exists():
        st.info("No current in-session results. Run the lab to generate recommendations.")
    else:
        st.info("Run the lab to compare ORB behaviour across your selected tickers.")
    if isinstance(errors_df, pd.DataFrame) and not errors_df.empty:
        with st.expander("Data errors", expanded=False):
            st.dataframe(errors_df, use_container_width=True, hide_index=True)


# Global compact terminal header shown on every page.
if dashboard_auto_start_enabled(cfg):
    start_trading_engine_once()
    auto_start_news_engine_once()
maybe_start_auto_ibkr_scanner(cfg, symbols)
if selected_page == "🏦 Account Status":
    sync_ibkr_account_status(force=False)
    schedule_ibkr_reconnect_refresh()
render_app_header()
render_premarket_watchlist_marquee(cfg)

# Page routing from sidebar navigation

if selected_page == "🏦 Account Status":
    render_account_status_tab()

elif selected_page == "📈 Scanner & Breakdown":
    scan_col, breakdown_col = st.columns([1, 1])
    with scan_col:
        st.subheader("Scanner")
        st.caption("Scan the full watchlist, rank the best setups, and review option ideas.")
        run_scanner_clicked = st.button("▶ Run IBKR Scanner", use_container_width=True)
        if run_scanner_clicked:
            run_ibkr_scanner_ui(cfg, symbols)

        stock_df, option_df, scanner_job_status = load_saved_scanner_results()
        if scanner_job_status.get("status") == "running":
            st.info(f"Scanner running in background: {scanner_job_status.get('completed', 0)} / {scanner_job_status.get('total', len(symbols))} symbols.")
            st_autorefresh(interval=2_000, key="scanner_job_refresh_display")
        elif scanner_job_status.get("status") == "error":
            st.error(f"Scanner failed: {scanner_job_status.get('message', 'Unknown error')}")
        elif scanner_job_status.get("status") == "stale":
            st.warning(scanner_job_status.get("message", "Scanner results are stale. Run the IBKR scanner for today's data."))
        elif scanner_job_status.get("status") == "no_data":
            st.warning(scanner_job_status.get("message", "Scanner completed, but no fresh scanner rows were available yet."))

        if (isinstance(stock_df, pd.DataFrame) and not stock_df.empty) or (isinstance(option_df, pd.DataFrame) and not option_df.empty):
            st.markdown("### Last Scanner Results")
            last_run = scanner_job_status.get("finished_at") or scanner_job_status.get("started_at")
            if last_run:
                st.caption(f"Last scan: {last_run}")
            card_source = option_df if isinstance(option_df, pd.DataFrame) and not option_df.empty else stock_df
            if isinstance(card_source, pd.DataFrame) and not card_source.empty:
                card_cols = st.columns(min(2, len(card_source)))
                for idx, (_, row) in enumerate(card_source.head(4).iterrows()):
                    with card_cols[idx % len(card_cols)]:
                        setup_card(row.to_dict())
            with st.expander("Stock Results", expanded=True):
                st.dataframe(stock_df, use_container_width=True)
            with st.expander("Option Ideas", expanded=isinstance(option_df, pd.DataFrame) and not option_df.empty):
                if isinstance(option_df, pd.DataFrame) and not option_df.empty:
                    st.dataframe(option_df, use_container_width=True)
                    st.download_button("Download option ideas", option_df.to_csv(index=False), "option_ideas.csv", "text/csv", key="download_persisted_option_ideas")
                else:
                    st.info("No clean option contracts found for the filtered setups.")
        elif scanner_job_status.get("status") not in {"running", "stale", "no_data"}:
            st.info("No scanner results yet. Run the IBKR scanner once and the results will stay here while you navigate.")

    with breakdown_col:
        st.subheader("Ticker Breakdown")
        st.caption("Analyze one ticker using the same scanner logic.")
        ticker = st.text_input("Ticker", value="", label_visibility="collapsed").strip().upper()
        if st.button("Analyze Ticker", use_container_width=True):
            if not ticker:
                st.warning("Enter a ticker first.")
                st.stop()
            ib = None
            try:
                breakdown_ib_cfg = IBConfig(
                    host=ib_cfg.host,
                    port=ib_cfg.port,
                    client_id=ib_cfg.client_id + 101,
                    account=ib_cfg.account,
                    readonly=ib_cfg.readonly,
                )
                ib = connect_ib(breakdown_ib_cfg)
                ib.RequestTimeout = 8
                breakdown = scan_symbol_ib(
                    ib,
                    ticker,
                    bool(cfg["strategy"].get("use_rvol_score", False)),
                    str(cfg["strategy"].get("active_strategy", "pmb")),
                    int(cfg["strategy"].get("orb_minutes", 15)),
                    int(cfg["strategy"].get("min_session_bars", 7)),
                )
                if not breakdown:
                    st.error("No breakdown available.")
                else:
                    st.plotly_chart(make_chart(ticker, breakdown), use_container_width=True)
                    b1, b2, b3, b4 = st.columns(4)
                    b1.metric("Signal", signal_badge(breakdown["Signal"]))
                    b2.metric("Score", breakdown["Score"])
                    b3.metric("Confidence", breakdown["Confidence"])
                    b4.metric("RVOL", breakdown["RVOL"])
                    st.write("Why:")
                    for reason in breakdown["Reasons"].split(" | "):
                        st.write(f"✓ {reason}")

                    option_details = None
                    if str(breakdown.get("Signal", "")).upper() in {"CALL", "PUT"}:
                        with st.spinner("Fetching live option Greeks and spread from IBKR..."):
                            option_details = recommend_option_ib(
                                ib,
                                ticker,
                                breakdown["Signal"],
                                float(breakdown["Price"]),
                                int(cfg["strategy"].get("option_dte", 7)),
                                cfg.get("option_filters", {}),
                            )
                        if option_details:
                            st.markdown("#### Option Contract")
                            o1, o2, o3, o4 = st.columns(4)
                            o1.metric("Option Score", option_details.get("Option Score", "N/A"))
                            o2.metric("Delta", option_details.get("Delta", "N/A"))
                            o3.metric("Spread", f"{option_details.get('Spread %', 'N/A')}%")
                            o4.metric("Mid", f"${float(option_details.get('Mid') or 0):.2f}")
                            o5, o6, o7, o8 = st.columns(4)
                            o5.metric("Bid / Ask", f"{option_details.get('Bid', 'N/A')} / {option_details.get('Ask', 'N/A')}")
                            o6.metric("Theta", option_details.get("Theta", "N/A"))
                            o7.metric("Gamma", option_details.get("Gamma", "N/A"))
                            o8.metric("IV", option_details.get("Implied Vol", "N/A"))
                            notes = option_details.get("Option Score Notes")
                            if notes:
                                st.caption(f"Option quality: {notes}")
                            option_display = {k: v for k, v in option_details.items() if k != "Contract"}
                            st.dataframe(pd.DataFrame([option_display]), use_container_width=True)
                        else:
                            filters = option_filters_from_config(cfg)
                            st.warning(
                                "No clean option contract passed the live filters "
                                f"(spread <= {filters.get('max_spread_pct')}%, "
                                f"|delta| >= {filters.get('min_abs_delta')}, "
                                "live Greeks required)."
                            )
                    else:
                        st.info("No option contract priced because the scanner signal is WAIT.")
                    st.dataframe(pd.DataFrame([clean_for_table(breakdown)]), use_container_width=True)
            except Exception as e:
                st.error(f"Analysis failed: {display_exception_message(e)}")
                st.caption("Technical details are hidden in the dashboard. Check the terminal/log files if needed.")
            finally:
                try:
                    if ib and ib.isConnected():
                        ib.disconnect()
                except Exception:
                    pass

elif selected_page == "🧪 Price Action Lab":
    render_price_action_lab_tab(cfg, symbols)

elif selected_page == "💼 Positions":
    start_telegram_decision_worker()
    st.subheader("Positions")
    st.caption("The dashboard starts the trading engine automatically. Order placement still follows the automation and safety settings.")
    positions_refresh_seconds = 5

    mode = cfg.get("account_mode", "Simulation")
    trading_status = trading_status_from_config(cfg)
    orders_unlocked = orders_unlocked_from_config(cfg)
    readonly = bool(cfg.get("ib", {}).get("readonly", False))
    place_orders = bool(cfg.get("automation", {}).get("place_orders", False))
    risk_confirmed = bool(cfg.get("automation", {}).get("confirm_order_risk", False))
    automation_enabled = bool(cfg.get("automation", {}).get("enabled", False))

    st.markdown("### Status")
    health_now = read_health()
    engine_process_running, _engine_message = get_trading_engine_process_status()
    compact_cols = st.columns(8)
    compact_cols[0].metric("Engine", "Running" if engine_process_running else "Stopped")
    compact_cols[1].metric("IBKR", "Connected" if live_ibkr_ping(ib_cfg) else "Disconnected")
    compact_cols[2].metric("Mode", mode)
    compact_cols[3].metric("Orders", operational_order_label(cfg, health))
    compact_cols[4].metric("Automation", "On" if automation_enabled else "Off")
    compact_cols[5].metric("Place", "Allowed" if place_orders else "Disabled")
    compact_cols[6].metric("Risk", "Confirmed" if risk_confirmed else "No")
    compact_cols[7].metric("Read-only", "On" if readonly else "Off")

    with st.expander("Status details", expanded=False):
        st.json({
            "Trading Status": trading_status,
            "IB Port": ib_cfg.port,
            "Last Engine Status": health_now.get("last_status", "N/A") if health_now else "N/A",
            "Candidates": health_now.get("candidates", 0) if health_now else 0,
            "Health": health_now or {},
        })

    render_live_positions_fragment(cfg, ib_cfg, health)
    render_position_close_controls(cfg, ib_cfg)

    approval_mode = approval_mode_from_config(cfg)
    configured_scan_interval = max(10, int(cfg.get("automation", {}).get("scan_interval_seconds", 60)))
    candidate_refresh_seconds = positions_refresh_seconds

    approvals = read_pending_approvals()
    approvals_by_id = {str(order.get("id") or ""): order for order in approvals}
    scan_snapshot = read_current_scan_candidates()
    all_scan_rows = list(scan_snapshot.get("candidates") or [])
    current_scan_candidates = [
        row for row in all_scan_rows
        if str(row.get("trade_status") or "").lower() != "scanner rejected"
    ]
    latest_scan_id = str(scan_snapshot.get("scan_id") or "")

    with st.expander("Current Scan Trade Candidates", expanded=True):
        st.caption(
            f"Approval mode: {approval_mode} | Engine scan interval: {configured_scan_interval} seconds. "
            f"Positions refreshes every {candidate_refresh_seconds} seconds to pick up newly completed scans."
        )
        if current_scan_candidates:
            candidate_rows = []
            for order_item in current_scan_candidates:
                candidate_rows.append({
                    "symbol": order_item.get("symbol"),
                    "side": order_item.get("signal"),
                    "tradable": "Yes" if order_item.get("tradable") else "No",
                    "status": order_item.get("trade_status") or order_item.get("status"),
                    "why_not_tradable": order_item.get("block_reason"),
                    "score": order_item.get("score"),
                    "grade": order_item.get("grade"),
                    "rank": order_item.get("rank_score"),
                    "option": order_item.get("option"),
                    "qty": order_item.get("quantity"),
                    "mid": order_item.get("mid"),
                    "limit": order_item.get("limit_price"),
                    "est_cost": order_item.get("estimated_cost"),
                    "spend_limit": order_item.get("spend_limit"),
                    "remaining_capital": order_item.get("remaining_capital"),
                    "scan": order_item.get("scan_id"),
                })

            for order_item in current_scan_candidates:
                with st.container(border=True):
                    left, mid, right = st.columns([3, 2, 2])
                    with left:
                        st.markdown(f"**{order_item.get('symbol', 'N/A')} {order_item.get('signal', 'N/A')}**")
                        st.caption(str(order_item.get("option", "N/A")))
                        block_reason = str(order_item.get("block_reason") or "")
                        if block_reason:
                            st.warning(block_reason)
                        reasons = str(order_item.get("reasons") or "")
                        if reasons:
                            st.caption(reasons[:420] + ("..." if len(reasons) > 420 else ""))
                    with mid:
                        st.metric("Score", order_item.get("score", "N/A"))
                        st.caption(
                            f"Qty {order_item.get('quantity', 0)} | "
                            f"Mid ${float(order_item.get('mid') or 0):.2f} | "
                            f"Cost ${float(order_item.get('estimated_cost') or 0):,.2f}"
                        )
                    with right:
                        approval_id = str(order_item.get("approval_id") or "")
                        pending_order = approvals_by_id.get(approval_id, {})
                        approve_disabled = (
                            not orders_unlocked
                            or not bool(order_item.get("tradable"))
                            or not pending_order
                            or str(pending_order.get("status", "")).lower() not in ["pending", "sent"]
                        )
                        button_key = approval_id or f"{order_item.get('scan_id')}_{order_item.get('symbol')}_{order_item.get('signal')}"
                        if st.button(
                            "Confirm Trade",
                            key=f"approve_scan_{button_key}",
                            icon=":material/check_circle:",
                            use_container_width=True,
                            disabled=approve_disabled,
                        ):
                            approval_ib = None
                            try:
                                approval_ib = connect_ib(dashboard_ib_cfg(307, readonly=False))
                                status = submit_approved_order(approval_ib, dashboard_ib_cfg(307, readonly=False), pending_order)
                                update_pending_approval(
                                    approval_id,
                                    decision_at=datetime.now(EASTERN).isoformat(),
                                    decision_source="dashboard",
                                )
                                st.success(f"Submitted {order_item.get('symbol')} {order_item.get('signal')}: {status}")
                                st.rerun()
                            except Exception as exc:
                                update_pending_approval(
                                    approval_id,
                                    status="failed",
                                    failed_at=datetime.now(EASTERN).isoformat(),
                                    decision_source="dashboard",
                                    error=str(exc),
                                )
                                st.error(f"Order submit failed: {display_exception_message(exc)}")
                            finally:
                                try:
                                    if approval_ib and approval_ib.isConnected():
                                        approval_ib.disconnect()
                                except Exception:
                                    pass
                        reject_disabled = not pending_order or str(pending_order.get("status", "")).lower() not in ["pending", "sent"]
                        if st.button(
                            "Refuse Trade",
                            key=f"reject_scan_{button_key}",
                            icon=":material/cancel:",
                            use_container_width=True,
                            disabled=reject_disabled,
                        ):
                            if approval_id:
                                update_pending_approval(
                                    approval_id,
                                    status="rejected",
                                    decision_at=datetime.now(EASTERN).isoformat(),
                                    decision_source="dashboard",
                                )
                                st.info(f"Rejected {order_item.get('symbol')} {order_item.get('signal')}.")
                                st.rerun()
                        if approve_disabled:
                            st.caption("Confirm is enabled only for tradable rows with a live pending approval.")

            st.markdown("#### Candidate Table")
            st.dataframe(pd.DataFrame(candidate_rows), use_container_width=True, hide_index=True)
        else:
            st.info(scan_snapshot.get("message") or "No trade candidates found for the latest scan.")
            hidden_count = max(0, len(all_scan_rows) - len(current_scan_candidates))
            if hidden_count:
                st.caption(f"{hidden_count} scanner-rejected ticker(s) hidden.")

    def is_recent_pending_approval(order: dict) -> bool:
        if str(order.get("status", "")).lower() not in ["pending", "sent"]:
            return False
        try:
            created_at = datetime.fromisoformat(str(order.get("created_at")))
            return (datetime.now(EASTERN) - created_at).total_seconds() <= 120
        except Exception:
            return False

    pending_telegram_orders = [o for o in read_pending_approvals() if is_recent_pending_approval(o)]
    if pending_telegram_orders:
        st_autorefresh(interval=1_000, key="telegram_order_decision_refresh")
        st.caption("Waiting for Telegram decision. The background worker is checking for replies.")

    st.markdown("### Manual Order Approval")
    with st.container(border=True):
        st.session_state.setdefault("manual_order_expiry", "")
        st.session_state.setdefault("manual_order_strike", 0.0)
        st.session_state.setdefault("manual_order_limit", 0.0)
        st.session_state.setdefault("manual_order_mid", 0.0)

        m1, m2, m3, m4 = st.columns(4)
        manual_symbol = m1.text_input("Symbol", value="SPY", key="manual_order_symbol").strip().upper()
        manual_signal = m2.selectbox("Option side", ["CALL", "PUT"], key="manual_order_signal")
        manual_dte = m3.selectbox("Expiry target", [7, 14, 30, 45], format_func=lambda days: f"{days} DTE", key="manual_order_dte")
        m4.markdown("<div style='height:1.72rem'></div>", unsafe_allow_html=True)
        if m4.button("Load IBKR Defaults", use_container_width=True):
            try:
                defaults = load_manual_option_defaults(manual_symbol, manual_signal, int(manual_dte))
                st.session_state["manual_order_expiry"] = defaults["expiry"]
                st.session_state["manual_order_strike"] = defaults["strike"]
                st.session_state["manual_order_mid"] = defaults["mid"]
                st.session_state["manual_order_limit"] = defaults["limit"]
                st.success(
                    f"Loaded {manual_symbol} {manual_signal}: "
                    f"{defaults['expiry']} {defaults['strike']:g} | mid ${defaults['mid']:.2f}"
                )
                if defaults.get("warning"):
                    st.warning(defaults["warning"])
            except Exception as e:
                st.error(f"Could not load IBKR option defaults: {e}")

        m5, m6, m7, m8 = st.columns(4)
        manual_expiry = m5.text_input("Resolved expiry", key="manual_order_expiry", disabled=True).strip()
        manual_strike = m6.number_input("ATM strike", min_value=0.0, step=0.5, key="manual_order_strike")
        manual_mid = m7.number_input("Estimated mid", min_value=0.0, step=0.05, key="manual_order_mid")
        manual_limit = m8.number_input("Limit price", min_value=0.0, step=0.05, key="manual_order_limit")

        m9, m10 = st.columns(2)
        manual_qty = m9.number_input("Quantity", value=1, min_value=1, step=1, key="manual_order_qty")
        manual_order_type = m10.selectbox("Order type", ["LIMIT", "MARKET"], key="manual_order_type")

        def build_manual_order_payload() -> dict:
            option_label = f"{manual_symbol} {manual_expiry} {manual_strike:g} {manual_signal}"
            mid_or_limit = float(manual_mid or manual_limit or 0)
            return {
                "id": make_approval_id(manual_symbol, manual_signal),
                "status": "pending",
                "created_at": datetime.now(EASTERN).isoformat(),
                "approval_mode": "Dashboard",
                "symbol": manual_symbol,
                "signal": manual_signal,
                "option": option_label,
                "expiry": manual_expiry,
                "strike": float(manual_strike),
                "type": manual_signal,
                "quantity": int(manual_qty),
                "mid": mid_or_limit,
                "estimated_cost": round(float(manual_qty) * mid_or_limit * 100, 2),
                "order_type": manual_order_type,
                "limit_price": float(manual_limit) if manual_order_type == "LIMIT" else None,
                "account_mode": mode,
                "score": "MANUAL",
                "grade": "Manual",
                "setup_quality": "Manual order",
                "rank_score": 0,
                "reasons": "Manual order entered and approved from Positions tab.",
                "raw_signal": {"Symbol": manual_symbol, "Signal": manual_signal},
                "option_data": {
                    "Option": option_label,
                    "Expiry": manual_expiry,
                    "Strike": float(manual_strike),
                    "Type": manual_signal,
                    "Mid": mid_or_limit,
                },
            }

        def submit_manual_popup_order(order: dict) -> None:
            orders = read_pending_approvals()
            if not any(str(existing.get("id")) == str(order.get("id")) for existing in orders):
                orders.append(order)
                write_pending_approvals(orders)
            approval_ib = None
            try:
                approval_ib = connect_ib(dashboard_ib_cfg(308, readonly=False))
                status = submit_approved_order(approval_ib, dashboard_ib_cfg(308, readonly=False), order)
                update_pending_approval(
                    order["id"],
                    decision_at=datetime.now(EASTERN).isoformat(),
                    decision_source="dashboard_popup",
                )
                st.session_state["manual_order_last_status"] = f"Submitted {order.get('symbol')} {order.get('signal')}: {status}"
                st.session_state.pop("manual_order_pending_popup", None)
                st.rerun()
            except Exception as exc:
                update_pending_approval(
                    order.get("id", ""),
                    status="failed",
                    failed_at=datetime.now(EASTERN).isoformat(),
                    decision_source="dashboard_popup",
                    error=str(exc),
                )
                st.error(f"Order submit failed: {display_exception_message(exc)}")
            finally:
                try:
                    if approval_ib and approval_ib.isConnected():
                        approval_ib.disconnect()
                except Exception:
                    pass

        def render_manual_order_confirmation(order: dict) -> None:
            order_type_label = str(order.get("order_type", "LIMIT"))
            limit_label = f"${float(order.get('limit_price') or 0):.2f}" if order.get("limit_price") else "Market"
            position_cost = float(order.get("estimated_cost") or 0)
            st.markdown(f"**Order Brief: {order.get('symbol')} {order.get('signal')}**")
            st.caption(str(order.get("option", "")))
            st.markdown(
                f"""
                <div style="border:1px solid #e5e7eb;border-radius:8px;padding:0.85rem;margin:0.75rem 0;background:#f8fafc;">
                    <div style="font-size:0.82rem;color:#64748b;font-weight:800;margin-bottom:0.35rem;">Position Cost</div>
                    <div style="font-size:1.6rem;font-weight:800;color:#111827;">${position_cost:,.2f}</div>
                    <div style="font-size:0.86rem;color:#475569;margin-top:0.35rem;">
                        {int(order.get("quantity") or 0)} contract(s) x ${float(order.get("mid") or order.get("limit_price") or 0):.2f} x 100 multiplier
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            review_cols = st.columns(4)
            review_cols[0].metric("Qty", order.get("quantity", 0))
            review_cols[1].metric("Side", order.get("signal", "N/A"))
            review_cols[2].metric("Order", order_type_label)
            review_cols[3].metric("Price", limit_label)
            st.caption(f"Account mode: {order.get('account_mode', 'N/A')} | Expiry: {order.get('expiry', 'N/A')} | Strike: {order.get('strike', 'N/A')}")
            if not orders_unlocked:
                st.warning("Order placement is locked by the current automation/safety settings.")
            confirm_cols = st.columns(2)
            with confirm_cols[0]:
                if st.button("Place Order", key="manual_popup_confirm", use_container_width=True, disabled=not orders_unlocked):
                    submit_manual_popup_order(order)
            with confirm_cols[1]:
                if st.button("Cancel", key="manual_popup_cancel", use_container_width=True):
                    st.session_state.pop("manual_order_pending_popup", None)
                    st.rerun()

        dialog = getattr(st, "dialog", None) or getattr(st, "experimental_dialog", None)
        if dialog is not None:
            @dialog("Confirm Manual Order")
            def manual_order_confirmation_dialog(order: dict) -> None:
                render_manual_order_confirmation(order)

        if st.button("Review Manual Order", use_container_width=True):
            if not manual_symbol or not manual_expiry or manual_strike <= 0:
                st.error("Enter symbol, expiry, and strike before reviewing the order.")
            elif manual_order_type == "LIMIT" and manual_limit <= 0:
                st.error("Limit orders need a limit price greater than zero.")
            else:
                st.session_state["manual_order_pending_popup"] = build_manual_order_payload()

        if st.session_state.get("manual_order_last_status"):
            st.success(st.session_state["manual_order_last_status"])
        pending_popup_order = st.session_state.get("manual_order_pending_popup")
        if isinstance(pending_popup_order, dict):
            if dialog is not None:
                manual_order_confirmation_dialog(pending_popup_order)
            else:
                with st.container(border=True):
                    st.markdown("#### Confirm Manual Order")
                    render_manual_order_confirmation(pending_popup_order)

    if st.button("Manage Open Positions Now", use_container_width=True):
        try:
            if not read_active_positions():
                st.info("No active positions to manage.")
            else:
                ib = connect_ib(dashboard_ib_cfg(304))
                try:
                    r = cfg["risk"]
                    events = manage_open_positions(
                        ib=ib,
                        account=ib_cfg.account,
                        stop_loss_pct=float(r.get("stop_loss_pct", 20.0)),
                        take_profit_pct=float(r.get("take_profit_pct", 30.0)),
                        breakeven_trigger_pct=float(r.get("breakeven_trigger_pct", 15.0)),
                        trailing_trigger_pct=float(r.get("trailing_trigger_pct", 25.0)),
                        trailing_stop_pct=float(r.get("trailing_stop_pct", 10.0)),
                        force_exit_time=dtime(int(r.get("force_exit_hour", 15)), int(r.get("force_exit_minute", 55))),
                        force_exit_enabled=bool(r.get("force_exit_enabled", True)),
                        allow_live_orders=orders_unlocked_from_config(cfg),
                    )
                finally:
                    try:
                        ib.disconnect()
                    except Exception:
                        pass
                st.dataframe(pd.DataFrame(events), use_container_width=True) if events else st.info("No active positions to manage.")
        except Exception as e:
            clean_ui_error("Position management failed", e)

elif selected_page in ("📊 Performance & Trade Journal", "🤖 AI AUDIT"):
    if selected_page == "🤖 AI AUDIT":
        st.subheader("AI AUDIT")
    else:
        st.subheader("Performance & Trade Journal")

    def _parse_dashboard_timestamps(values):
        try:
            return pd.to_datetime(values, errors="coerce", utc=True, format="mixed").dt.tz_convert(EASTERN)
        except TypeError:
            return pd.to_datetime(values, errors="coerce", utc=True).dt.tz_convert(EASTERN)

    def _load_trade_log_df() -> pd.DataFrame:
        if not os.path.exists(TRADE_LOG_FILE):
            return pd.DataFrame()
        try:
            df = pd.read_csv(TRADE_LOG_FILE)
            if df.empty or "timestamp" not in df.columns:
                return pd.DataFrame()
            df["timestamp"] = _parse_dashboard_timestamps(df["timestamp"])
            df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
            if "realized_pnl" in df.columns:
                df["realized_pnl"] = pd.to_numeric(df["realized_pnl"], errors="coerce").fillna(0.0)
            else:
                df["realized_pnl"] = 0.0
            if {"source", "event", "con_id"}.issubset(df.columns):
                con_key = pd.to_numeric(df["con_id"], errors="coerce").fillna(0).astype(int).astype(str)
                trade_key = (
                    df["timestamp"].dt.date.astype(str)
                    + "|"
                    + df["event"].astype(str).str.upper()
                    + "|"
                    + con_key
                )
                flex_keys = set(trade_key[df["source"].astype(str).eq("IBKR_FLEX") & con_key.ne("0")])
                duplicate_live_rows = df["source"].astype(str).eq("IBKR_EXECUTION") & trade_key.isin(flex_keys)
                df = df[~duplicate_live_rows].copy()
            if {"source", "event", "symbol", "signal", "quantity"}.issubset(df.columns):
                source_norm = df["source"].fillna("").astype(str).str.upper()
                event_norm = df["event"].fillna("").astype(str).str.upper()
                qty_key = pd.to_numeric(df["quantity"], errors="coerce").fillna(0).astype(float).round(6).astype(str)
                trade_key = (
                    df["timestamp"].dt.date.astype(str)
                    + "|"
                    + event_norm
                    + "|"
                    + df["symbol"].fillna("").astype(str).str.upper()
                    + "|"
                    + df["signal"].fillna("").astype(str).str.upper()
                    + "|"
                    + qty_key
                )
                broker_sources = source_norm.isin(["IBKR_EXECUTION", "IBKR_FLEX"])
                dedupe_events = event_norm.isin(["ENTRY", "EXIT"])
                broker_event_keys = set(trade_key[dedupe_events & broker_sources])
                duplicate_local_events = dedupe_events & source_norm.eq("") & trade_key.isin(broker_event_keys)
                df = df[~duplicate_local_events].copy()
                source_norm = df["source"].fillna("").astype(str).str.upper()
                event_norm = df["event"].fillna("").astype(str).str.upper()
                broker_sources = source_norm.isin(["IBKR_EXECUTION", "IBKR_FLEX"])
                if broker_sources.any():
                    qty = pd.to_numeric(df["quantity"], errors="coerce").fillna(0).abs()
                    broad_key = (
                        df["timestamp"].dt.date.astype(str)
                        + "|"
                        + event_norm
                        + "|"
                        + df["symbol"].fillna("").astype(str).str.upper()
                        + "|"
                        + df["signal"].fillna("").astype(str).str.upper()
                    )
                    broker_event_mask = event_norm.isin(["ENTRY", "EXIT"]) & broker_sources
                    broker_event_qty = qty[broker_event_mask].groupby(broad_key[broker_event_mask]).sum()
                    local_event = event_norm.isin(["ENTRY", "EXIT"]) & source_norm.eq("")
                    split_fill_matches = pd.Series(
                        [
                            float(qty.iloc[pos]) > 0
                            and float(broker_event_qty.get(broad_key.iloc[pos], 0.0)) >= float(qty.iloc[pos])
                            for pos in range(len(df))
                        ],
                        index=df.index,
                    )
                    duplicate_split_local = local_event & split_fill_matches
                    df = df[~duplicate_split_local].copy()
            return df
        except Exception:
            return pd.DataFrame()

    def _render_monthly_pnl_calendar(exits_df: pd.DataFrame, month_anchor: datetime.date) -> None:
        month_start = month_anchor.replace(day=1)
        month_label = month_start.strftime("%B %Y")
        day_stats = {}
        if isinstance(exits_df, pd.DataFrame) and not exits_df.empty and "timestamp" in exits_df.columns:
            month_exits = exits_df[
                (exits_df["timestamp"].dt.year == month_start.year)
                & (exits_df["timestamp"].dt.month == month_start.month)
            ].copy()
            if not month_exits.empty:
                month_exits["trade_date"] = month_exits["timestamp"].dt.date
                grouped = month_exits.groupby("trade_date").agg(
                    pnl=("realized_pnl", "sum"),
                    trades=("realized_pnl", "size"),
                )
                day_stats = grouped.to_dict("index")

        weeks = calendar.Calendar(firstweekday=6).monthdatescalendar(month_start.year, month_start.month)
        today_et = datetime.now(EASTERN).date()
        cells = []
        for week in weeks:
            for day in week:
                in_month = day.month == month_start.month
                stats = day_stats.get(day, {"pnl": 0.0, "trades": 0})
                pnl = float(stats.get("pnl", 0.0) or 0.0)
                trades = int(stats.get("trades", 0) or 0)
                tone = "pt-cal-empty"
                if trades and pnl > 0:
                    tone = "pt-cal-win"
                elif trades and pnl < 0:
                    tone = "pt-cal-loss"
                elif trades:
                    tone = "pt-cal-flat"
                today_class = " pt-cal-today" if day == today_et else ""
                muted_class = " pt-cal-muted" if not in_month else ""
                trade_label = "trade" if trades == 1 else "trades"
                pnl_html = f"<strong>${pnl:,.0f}</strong><span>{trades} {trade_label}</span>" if trades else ""
                cells.append(
                    f"<div class='pt-cal-cell {tone}{today_class}{muted_class}'>"
                    f"<div class='pt-cal-day'>{day.day if in_month else ''}</div>"
                    f"<div class='pt-cal-pnl'>{pnl_html}</div>"
                    "</div>"
                )

        st.markdown(
            f"""
            <style>
            .pt-cal-wrap {{
                border: 1px solid #e5e7eb;
                border-radius: 8px;
                overflow: hidden;
                margin: 0.75rem 0 1.25rem;
                background: #ffffff;
            }}
            .pt-cal-head {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                padding: 0.85rem 1rem;
                border-bottom: 1px solid #e5e7eb;
            }}
            .pt-cal-title {{
                font-size: 1.35rem;
                font-weight: 700;
            }}
            .pt-cal-weekdays, .pt-cal-grid {{
                display: grid;
                grid-template-columns: repeat(7, minmax(0, 1fr));
                gap: 6px;
                padding: 6px 1rem;
            }}
            .pt-cal-weekdays div {{
                border: 1px solid #e5e7eb;
                border-radius: 8px;
                padding: 0.55rem;
                text-align: center;
                font-weight: 700;
                color: #111827;
            }}
            .pt-cal-grid {{
                padding-bottom: 1rem;
            }}
            .pt-cal-cell {{
                min-height: 112px;
                border-radius: 6px;
                border: 1px solid #e5e7eb;
                background: #f3f4f6;
                padding: 0.55rem;
                position: relative;
            }}
            .pt-cal-day {{
                text-align: right;
                font-size: 0.95rem;
                color: #111827;
            }}
            .pt-cal-pnl {{
                margin-top: 0.85rem;
                text-align: center;
                color: #111827;
            }}
            .pt-cal-pnl strong {{
                display: block;
                font-size: 1.25rem;
            }}
            .pt-cal-pnl span {{
                color: #6b7280;
                font-size: 0.95rem;
            }}
            .pt-cal-win {{
                background: #dcfce7;
                border-color: #10b981;
            }}
            .pt-cal-loss {{
                background: #fee2e2;
                border-color: #ef4444;
            }}
            .pt-cal-flat {{
                background: #eef2ff;
                border-color: #6366f1;
            }}
            .pt-cal-muted {{
                background: #ffffff;
            }}
            .pt-cal-today .pt-cal-day {{
                display: inline-flex;
                align-items: center;
                justify-content: center;
                float: right;
                width: 1.75rem;
                height: 1.75rem;
                border-radius: 999px;
                background: #6554b8;
                color: #ffffff;
            }}
            </style>
            <div class="pt-cal-wrap">
                <div class="pt-cal-head">
                    <div class="pt-cal-title">{html.escape(month_label)}</div>
                    <div>{len(day_stats)} trading day{"s" if len(day_stats) != 1 else ""}</div>
                </div>
                <div class="pt-cal-weekdays">
                    <div>Sun</div><div>Mon</div><div>Tue</div><div>Wed</div><div>Thu</div><div>Fri</div><div>Sat</div>
                </div>
                <div class="pt-cal-grid">{''.join(cells)}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    def _shift_month(month_anchor, months: int):
        year = int(month_anchor.year) + ((int(month_anchor.month) - 1 + int(months)) // 12)
        month = ((int(month_anchor.month) - 1 + int(months)) % 12) + 1
        return month_anchor.replace(year=year, month=month, day=1)

    def _calendar_available_years(exits_df: pd.DataFrame, fallback_year: int) -> list[int]:
        years = {int(fallback_year)}
        if isinstance(exits_df, pd.DataFrame) and not exits_df.empty and "timestamp" in exits_df.columns:
            parsed_years = pd.to_numeric(exits_df["timestamp"].dt.year, errors="coerce").dropna().astype(int).tolist()
            years.update(parsed_years)
        min_year = min(years)
        max_year = max(years)
        years.update(range(min_year - 1, max_year + 2))
        return sorted(years)

    def _render_calendar_controls(default_anchor, exits_df: pd.DataFrame):
        key = "performance_calendar_anchor"
        default_month_start = default_anchor.replace(day=1)
        current = st.session_state.get(key)
        if not current:
            current = default_month_start
        if hasattr(current, "date"):
            current = current.date()
        current = current.replace(day=1)

        nav_cols = st.columns(5)
        with nav_cols[0]:
            previous_year_clicked = st.button("Prev Year", key="perf_cal_prev_year", use_container_width=True)
        with nav_cols[1]:
            previous_month_clicked = st.button("Prev Month", key="perf_cal_prev_month", use_container_width=True)
        with nav_cols[2]:
            current_clicked = st.button("Current", key="perf_cal_current", use_container_width=True)
        with nav_cols[3]:
            next_month_clicked = st.button("Next Month", key="perf_cal_next_month", use_container_width=True)
        with nav_cols[4]:
            next_year_clicked = st.button("Next Year", key="perf_cal_next_year", use_container_width=True)

        if previous_year_clicked:
            current = _shift_month(current, -12)
        elif previous_month_clicked:
            current = _shift_month(current, -1)
        elif next_month_clicked:
            current = _shift_month(current, 1)
        elif next_year_clicked:
            current = _shift_month(current, 12)
        elif current_clicked:
            current = datetime.now(EASTERN).date().replace(day=1)

        current = current.replace(year=int(current.year), month=int(current.month), day=1)
        st.session_state[key] = current
        return current

    def _format_signed_money(value) -> str:
        try:
            amount = float(value)
        except Exception:
            amount = 0.0
        sign = "-" if amount < 0 else ""
        return f"{sign}${abs(amount):,.2f}"

    def _render_performance_table(rows: list[dict], columns: list[tuple[str, str]], empty_message: str) -> None:
        if not rows:
            st.info(empty_message)
            return
        header_html = "".join(f"<th>{html.escape(label)}</th>" for _key, label in columns)
        body = []
        for row in rows:
            cells = []
            for key, _label in columns:
                value = row.get(key, "")
                cell_class = ""
                if key in {"net_pnl", "unrealized_pnl"}:
                    try:
                        amount = float(row.get(f"{key}_raw", value))
                    except Exception:
                        amount = 0.0
                    cell_class = " pt-perf-positive" if amount >= 0 else " pt-perf-negative"
                cells.append(f"<td class='{cell_class}'>{html.escape(str(value))}</td>")
            body.append(f"<tr>{''.join(cells)}</tr>")
        st.markdown(
            f"""
            <style>
            .pt-perf-table-wrap {{
                border: 1px solid #e5e7eb;
                border-radius: 8px;
                overflow: hidden;
                background: #ffffff;
            }}
            .pt-perf-table {{
                width: 100%;
                border-collapse: collapse;
                font-size: 1rem;
            }}
            .pt-perf-table th {{
                background: #f4f2fb;
                color: #111827;
                font-size: 1.05rem;
                font-weight: 800;
                padding: 1rem;
                text-align: center;
                border-bottom: 1px solid #e5e7eb;
            }}
            .pt-perf-table td {{
                padding: 1rem;
                text-align: center;
                color: #111827;
                border-bottom: 1px solid #f3f4f6;
            }}
            .pt-perf-table tr:last-child td {{
                border-bottom: 0;
            }}
            .pt-perf-positive {{
                color: #10b981 !important;
                font-weight: 800;
            }}
            .pt-perf-negative {{
                color: #ef4444 !important;
                font-weight: 800;
            }}
            </style>
            <div class="pt-perf-table-wrap">
                <table class="pt-perf-table">
                    <thead><tr>{header_html}</tr></thead>
                    <tbody>{''.join(body)}</tbody>
                </table>
            </div>
            """,
            unsafe_allow_html=True,
        )

    def _safe_trade_text(value, fallback: str = "") -> str:
        if value is None:
            return fallback
        try:
            if pd.isna(value):
                return fallback
        except Exception:
            pass
        text = str(value).strip()
        return text if text else fallback

    def _top_value_counts(df: pd.DataFrame, column: str, limit: int = 3) -> list[str]:
        if df.empty or column not in df.columns:
            return []
        values = df[column].fillna("").astype(str).str.strip()
        values = values[values.ne("")]
        if values.empty:
            return []
        return [f"{idx} ({count})" for idx, count in values.value_counts().head(limit).items()]

    def _normalized_trade_key_parts(df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
        fallback = pd.Series([""] * len(df), index=df.index, dtype="object")
        symbol = df.get("symbol", fallback).fillna("").astype(str).str.upper().str.strip()
        signal = df.get("signal", fallback).fillna("").astype(str).str.upper().str.strip()
        option = df.get("option", fallback).fillna("").astype(str).str.upper().str.replace(r"\s+", " ", regex=True).str.strip()
        con_id = pd.to_numeric(df.get("con_id", pd.Series([0] * len(df), index=df.index)), errors="coerce").fillna(0).astype(int).astype(str)
        return symbol, signal, option, con_id

    def _collapse_logical_trade_rows(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or "timestamp" not in df.columns:
            return df.copy()
        out = df.copy().sort_values("timestamp")
        event = out.get("event", pd.Series([""] * len(out), index=out.index)).fillna("").astype(str).str.upper().str.strip()
        symbol, signal, option, con_id = _normalized_trade_key_parts(out)
        date_key = out["timestamp"].dt.date.astype(str)
        minute_key = out["timestamp"].dt.floor("min").astype(str)
        contract_key = con_id.where(con_id.ne("0"), symbol + "|" + signal + "|" + option)
        out["_logical_trade_key"] = date_key + "|" + event + "|" + contract_key + "|" + minute_key

        numeric_sum_cols = [c for c in ["quantity", "filled_quantity", "realized_pnl", "estimated_cost", "commission"] if c in out.columns]
        weighted_price_cols = [c for c in ["entry_price", "exit_price", "limit_price"] if c in out.columns]
        first_cols = [c for c in out.columns if c not in set(numeric_sum_cols + weighted_price_cols + ["_logical_trade_key"])]
        rows = []
        for _, group in out.groupby("_logical_trade_key", sort=False):
            row = group.iloc[0][first_cols].to_dict()
            qty = pd.to_numeric(group.get("quantity", pd.Series(dtype=float)), errors="coerce").abs().fillna(0.0)
            for col in numeric_sum_cols:
                row[col] = float(pd.to_numeric(group[col], errors="coerce").fillna(0.0).sum())
            for col in weighted_price_cols:
                prices = pd.to_numeric(group[col], errors="coerce")
                valid = prices.notna()
                if valid.any() and qty[valid].sum() > 0:
                    row[col] = float((prices[valid] * qty[valid]).sum() / qty[valid].sum())
                elif valid.any():
                    row[col] = float(prices[valid].iloc[-1])
                else:
                    row[col] = pd.NA
            source_values = group.get("source", pd.Series(dtype=str)).dropna().astype(str).str.strip()
            if not source_values.empty:
                row["source"] = ", ".join(source_values.drop_duplicates().tolist())
            external_values = group.get("external_id", pd.Series(dtype=str)).dropna().astype(str).str.strip()
            if not external_values.empty:
                row["external_id"] = ",".join(external_values.tolist())
            if "timestamp" in row:
                row["timestamp"] = group["timestamp"].max()
            rows.append(row)
        collapsed = pd.DataFrame(rows)
        for col in ["quantity", "filled_quantity"]:
            if col in collapsed.columns:
                values = pd.to_numeric(collapsed[col], errors="coerce")
                collapsed[col] = values.apply(lambda v: int(v) if pd.notna(v) and float(v).is_integer() else v)
        return collapsed.sort_values("timestamp") if "timestamp" in collapsed.columns else collapsed

    def _trade_audit_contract_key(row: pd.Series) -> str:
        con_id = pd.to_numeric(pd.Series([row.get("con_id")]), errors="coerce").fillna(0).iloc[0]
        if con_id:
            return f"conid:{int(con_id)}"
        symbol = _safe_trade_text(row.get("symbol")).upper()
        signal = _safe_trade_text(row.get("signal")).upper()
        option = re.sub(r"\s+", " ", _safe_trade_text(row.get("option")).upper()).strip()
        return f"{symbol}|{signal}|{option}"

    def _match_entry_for_exit(exit_row: pd.Series, entries_df: pd.DataFrame) -> pd.Series | None:
        if entries_df.empty or "timestamp" not in entries_df.columns:
            return None
        exit_time = exit_row.get("timestamp")
        if pd.isna(exit_time):
            return None
        entries = entries_df[entries_df["timestamp"] <= exit_time].copy()
        if entries.empty:
            return None
        key = _trade_audit_contract_key(exit_row)
        matches = entries[entries.apply(_trade_audit_contract_key, axis=1) == key]
        if matches.empty:
            symbol = _safe_trade_text(exit_row.get("symbol")).upper()
            signal = _safe_trade_text(exit_row.get("signal")).upper()
            matches = entries[
                entries.get("symbol", pd.Series(dtype=str)).fillna("").astype(str).str.upper().eq(symbol)
                & entries.get("signal", pd.Series(dtype=str)).fillna("").astype(str).str.upper().eq(signal)
            ]
        if matches.empty:
            return None
        return matches.sort_values("timestamp").iloc[-1]

    def _load_audit_intraday(symbols: list[str], start_date, end_date) -> tuple[dict[str, pd.DataFrame], list[str]]:
        errors = []
        data: dict[str, pd.DataFrame] = {}
        if YahooDataClient is None:
            return data, ["YahooDataClient is unavailable, so post-trade price tracking cannot run."]
        client = YahooDataClient()
        today_et = datetime.now(EASTERN).date()
        for symbol in sorted({str(s).upper().strip() for s in symbols if str(s).strip()}):
            loaded = pd.DataFrame()
            try:
                if (today_et - end_date).days <= 7:
                    loaded = client.load(symbol=symbol, period="10d", interval="1m", force_refresh=False)
                if loaded.empty:
                    loaded = client.load(
                        symbol=symbol,
                        period=None,
                        start=start_date - timedelta(days=2),
                        end=end_date + timedelta(days=2),
                        interval="5m",
                        force_refresh=False,
                    )
            except Exception as exc:
                errors.append(f"{symbol}: {display_exception_message(exc)}")
                loaded = pd.DataFrame()
            if loaded is not None and not loaded.empty:
                data[symbol] = loaded.sort_index()
            else:
                errors.append(f"{symbol}: no intraday bars returned.")
        return data, errors

    def _price_at_or_before(bars: pd.DataFrame, timestamp) -> float | None:
        if bars.empty or pd.isna(timestamp):
            return None
        prior = bars[bars.index <= timestamp]
        if prior.empty:
            later = bars[bars.index >= timestamp]
            prior = later.head(1)
        if prior.empty:
            return None
        return _number_or_none(prior.iloc[-1].get("Close"))

    def _orb_read(bars: pd.DataFrame, trade_time, direction: str, entry_underlying: float | None) -> str:
        if bars.empty or pd.isna(trade_time) or entry_underlying is None:
            return "ORB unavailable"
        session_day = trade_time.date()
        session = bars[bars.index.date == session_day]
        if session.empty:
            return "ORB unavailable"
        open_ts = pd.Timestamp(datetime.combine(session_day, dtime(9, 30)), tz=EASTERN)
        reads = []
        for minutes in [5, 15]:
            orb = session[(session.index >= open_ts) & (session.index < open_ts + timedelta(minutes=minutes))]
            if orb.empty:
                continue
            high = float(orb["High"].max())
            low = float(orb["Low"].min())
            if direction == "PUT":
                passed = entry_underlying < low
                reads.append(f"{minutes}m {'confirmed' if passed else 'not confirmed'}")
            else:
                passed = entry_underlying > high
                reads.append(f"{minutes}m {'confirmed' if passed else 'not confirmed'}")
        return ", ".join(reads) if reads else "ORB unavailable"

    def _directional_pct(start: float | None, end: float | None, direction: str) -> float | None:
        if start is None or end is None or start <= 0:
            return None
        raw = (float(end) - float(start)) / float(start) * 100.0
        return -raw if direction == "PUT" else raw

    def _build_deep_trade_audit(exits_df: pd.DataFrame, entries_df: pd.DataFrame, start_date, end_date, config: dict) -> dict:
        if exits_df.empty:
            return {"rows": pd.DataFrame(), "summary": ["No closed trades are selected."], "errors": []}
        symbols = exits_df.get("symbol", pd.Series(dtype=str)).dropna().astype(str).str.upper().tolist()
        bars_by_symbol, errors = _load_audit_intraday(symbols, start_date, end_date)
        rows = []
        risk = config.get("risk", {}) if isinstance(config, dict) else {}
        strategy = config.get("strategy", {}) if isinstance(config, dict) else {}
        stop_loss = float(risk.get("stop_loss_pct", 20.0) or 20.0)
        take_profit = float(risk.get("take_profit_pct", 30.0) or 30.0)
        trailing_trigger = float(risk.get("trailing_trigger_pct", 25.0) or 25.0)
        trailing_stop = float(risk.get("trailing_stop_pct", 10.0) or 10.0)
        configured_orb = int(strategy.get("orb_minutes", 15) or 15)

        for _, exit_row in exits_df.sort_values("timestamp").iterrows():
            symbol = _safe_trade_text(exit_row.get("symbol")).upper()
            direction = _safe_trade_text(exit_row.get("signal"), "CALL").upper()
            bars = bars_by_symbol.get(symbol, pd.DataFrame())
            exit_time = exit_row.get("timestamp")
            entry_row = _match_entry_for_exit(exit_row, entries_df)
            entry_time = entry_row.get("timestamp") if entry_row is not None else pd.NaT
            entry_px = _price_at_or_before(bars, entry_time)
            exit_px = _price_at_or_before(bars, exit_time)
            post_30 = bars[(bars.index > exit_time) & (bars.index <= exit_time + timedelta(minutes=30))] if not bars.empty and pd.notna(exit_time) else pd.DataFrame()
            post_60 = bars[(bars.index > exit_time) & (bars.index <= exit_time + timedelta(minutes=60))] if not bars.empty and pd.notna(exit_time) else pd.DataFrame()
            post_day = bars[(bars.index > exit_time) & (bars.index.date == exit_time.date())] if not bars.empty and pd.notna(exit_time) else pd.DataFrame()
            during = bars[(bars.index >= entry_time) & (bars.index <= exit_time)] if not bars.empty and pd.notna(entry_time) and pd.notna(exit_time) else pd.DataFrame()

            after_30_px = _number_or_none(post_30.iloc[-1].get("Close")) if not post_30.empty else None
            after_60_px = _number_or_none(post_60.iloc[-1].get("Close")) if not post_60.empty else None
            day_close_px = _number_or_none(post_day.iloc[-1].get("Close")) if not post_day.empty else None
            move_trade = _directional_pct(entry_px, exit_px, direction)
            move_30 = _directional_pct(exit_px, after_30_px, direction)
            move_60 = _directional_pct(exit_px, after_60_px, direction)
            move_day = _directional_pct(exit_px, day_close_px, direction)
            if direction == "PUT":
                best_after_day = float(post_day["Low"].min()) if not post_day.empty else None
                worst_during = float(during["High"].max()) if not during.empty else None
            else:
                best_after_day = float(post_day["High"].max()) if not post_day.empty else None
                worst_during = float(during["Low"].min()) if not during.empty else None
            best_after_move = _directional_pct(exit_px, best_after_day, direction)
            adverse_during = _directional_pct(entry_px, worst_during, "PUT" if direction == "CALL" else "CALL")

            pnl = float(exit_row.get("realized_pnl", 0.0) or 0.0)
            if move_day is None:
                exit_verdict = "Need more same-day bars"
            elif pnl > 0 and move_day > 0.35:
                exit_verdict = "Early exit: stock kept moving into day close"
            elif pnl < 0 and move_day > 0.35:
                exit_verdict = "Possible stop too tight: stock recovered into day close"
            elif pnl < 0 and move_day <= -0.25:
                exit_verdict = "Exit likely protected capital into day close"
            elif pnl > 0 and move_day <= -0.25:
                exit_verdict = "Exit looked well timed into day close"
            else:
                exit_verdict = "Exit was reasonable"

            rows.append({
                "Symbol": symbol,
                "Side": direction,
                "Close Time": exit_time.strftime("%m/%d %H:%M") if pd.notna(exit_time) else "",
                "P/L": round(pnl, 2),
                "Stock Move In Trade": f"{move_trade:+.2f}%" if move_trade is not None else "N/A",
                "Next 30m": f"{move_30:+.2f}%" if move_30 is not None else "N/A",
                "Next 60m": f"{move_60:+.2f}%" if move_60 is not None else "N/A",
                "To Day Close": f"{move_day:+.2f}%" if move_day is not None else "N/A",
                "Best To Day Close": f"{best_after_move:+.2f}%" if best_after_move is not None else "N/A",
                "Adverse During": f"{adverse_during:+.2f}%" if adverse_during is not None else "N/A",
                "ORB Check": _orb_read(bars, entry_time if pd.notna(entry_time) else exit_time, direction, entry_px),
                "Exit Read": exit_verdict,
            })

        audit_df = pd.DataFrame(rows)
        summary = []
        if not audit_df.empty:
            early = int(audit_df["Exit Read"].astype(str).str.contains("Early exit|recovered", case=False, regex=True).sum())
            protected = int(audit_df["Exit Read"].astype(str).str.contains("protected|well timed", case=False, regex=True).sum())
            orb_5 = int(audit_df["ORB Check"].astype(str).str.contains("5m confirmed").sum())
            orb_15 = int(audit_df["ORB Check"].astype(str).str.contains("15m confirmed").sum())
            summary.append(f"Post-exit audit: {early} trade(s) had meaningful favorable movement from exit to the last candle of the day; {protected} exit(s) looked protective or well timed.")
            summary.append(f"ORB audit: 5m confirmed {orb_5}/{len(audit_df)} selected trades; 15m confirmed {orb_15}/{len(audit_df)}. Current engine ORB is {configured_orb}m.")
            if early >= max(1, len(audit_df) // 2):
                summary.append(f"Consider testing a wider trailing stop than {trailing_stop:.0f}% or delaying take-profit exits beyond {take_profit:.0f}% in Strategy Lab before changing live settings.")
            if protected >= max(1, len(audit_df) // 2):
                summary.append(f"Your current stop/trailing framework protected several trades; avoid loosening the {stop_loss:.0f}% stop unless the backtester confirms it.")
            if orb_5 > orb_15:
                summary.append("5m ORB confirmed more trades than 15m in this sample; test 5m ORB for earlier entries, but watch false breakouts.")
            elif orb_15 >= orb_5 and orb_15:
                summary.append("15m ORB held up as a cleaner confirmation in this sample.")
            summary.append(f"Settings reviewed: stop {stop_loss:.0f}%, target {take_profit:.0f}%, trailing trigger +{trailing_trigger:.0f}%, trailing distance {trailing_stop:.0f}%, ORB {configured_orb}m.")
        return {"rows": audit_df, "summary": summary, "errors": errors[:5]}

    def _render_deep_trade_audit(exits_df: pd.DataFrame, entries_df: pd.DataFrame, start_date, end_date) -> None:
        st.markdown(
            """
            <style>
            .pt-audit-wrap {
                border: 1px solid #d1d5db;
                border-radius: 8px;
                background: #ffffff;
                padding: 1rem;
                margin: 0.5rem 0 1.25rem;
            }
            .pt-audit-head {
                display: flex;
                align-items: flex-start;
                justify-content: space-between;
                gap: 0.75rem;
                margin-bottom: 0.85rem;
            }
            .pt-audit-head h3 {
                margin: 0;
                color: #111827;
                font-size: 1.02rem;
                line-height: 1.25;
            }
            .pt-audit-head p {
                margin: 0.22rem 0 0;
                color: #64748b;
                font-size: 0.84rem;
                line-height: 1.35;
            }
            .pt-audit-range {
                color: #475569;
                font-size: 0.78rem;
                font-weight: 800;
                white-space: nowrap;
            }
            .pt-audit-summary {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                gap: 0.65rem;
                margin-top: 0.85rem;
            }
            .pt-audit-card {
                border: 1px solid #e5e7eb;
                border-left: 4px solid #0b82ff;
                border-radius: 8px;
                background: #f8fafc;
                padding: 0.75rem 0.85rem;
            }
            .pt-audit-card strong {
                display: block;
                color: #0f172a;
                font-size: 0.82rem;
                margin-bottom: 0.25rem;
            }
            .pt-audit-card span {
                color: #374151;
                font-size: 0.86rem;
                line-height: 1.38;
            }
            .pt-audit-note {
                border: 1px solid #fde68a;
                border-radius: 8px;
                background: #fffbeb;
                color: #713f12;
                padding: 0.7rem 0.85rem;
                margin: 0.75rem 0;
                font-size: 0.84rem;
                line-height: 1.35;
            }
            @media (max-width: 900px) {
                .pt-audit-head { display: block; }
                .pt-audit-range { display: block; margin-top: 0.25rem; white-space: normal; }
            }
            </style>
            """,
            unsafe_allow_html=True,
        )
        st.markdown(
            f"""
            <div class="pt-audit-wrap">
                <div class="pt-audit-head">
                    <div>
                        <h3>Deep Exit & Settings Audit</h3>
                        <p>Tracks the underlying stock during each trade, after 30/60 minutes, and through the last available candle of the same trading day. Option P/L is inferred from stock direction, not recalculated with Greeks.</p>
                    </div>
                    <div class="pt-audit-range">{html.escape(str(start_date))} to {html.escape(str(end_date))}</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        audit_key = f"deep_trade_audit_v2_{start_date}_{end_date}_{len(exits_df)}"
        if st.button("Run Deep Trade Audit", key="run_deep_trade_audit", use_container_width=True):
            with st.spinner("Checking stock movement after your exits..."):
                st.session_state[audit_key] = _build_deep_trade_audit(exits_df, entries_df, start_date, end_date, cfg)
        audit = st.session_state.get(audit_key)
        if not audit:
            return

        summary_cards = []
        for item in audit.get("summary", []):
            text = str(item)
            if ":" in text:
                title, body = text.split(":", 1)
            else:
                title, body = "Recommendation", text
            summary_cards.append(
                "<div class='pt-audit-card'>"
                f"<strong>{html.escape(title.strip())}</strong>"
                f"<span>{html.escape(body.strip())}</span>"
                "</div>"
            )
        if summary_cards:
            st.markdown(
                f"<div class='pt-audit-summary'>{''.join(summary_cards)}</div>",
                unsafe_allow_html=True,
            )
        if audit.get("errors"):
            with st.expander("Market data notes"):
                for err in audit["errors"]:
                    st.markdown(
                        f"<div class='pt-audit-note'>{html.escape(str(err))}</div>",
                        unsafe_allow_html=True,
                    )
        rows = audit.get("rows")
        if isinstance(rows, pd.DataFrame) and not rows.empty:
            display_rows = rows.copy()
            if "P/L" in display_rows.columns:
                display_rows["P/L"] = pd.to_numeric(display_rows["P/L"], errors="coerce").map(_format_signed_money)
            st.dataframe(display_rows, use_container_width=True, hide_index=True)

    def _build_trade_ai_summary(exits_df: pd.DataFrame, entries_df: pd.DataFrame, period_label: str, start_date, end_date) -> dict:
        if exits_df.empty:
            return {
                "headline": f"No closed trades to analyze for {period_label.lower()}.",
                "right": ["No completed exits matched the current filters, so there is not enough closed-trade evidence yet."],
                "wrong": ["The review needs closed trades with realized P/L before it can judge execution quality."],
                "suggestions": ["Review again after positions close, or widen the date/symbol/outcome filters."],
                "stats": [],
                "worst": pd.DataFrame(),
            }

        exits = exits_df.copy().sort_values("timestamp")
        entries = entries_df.copy()
        pnl = pd.to_numeric(exits.get("realized_pnl", 0), errors="coerce").fillna(0.0)
        trade_count = int(len(exits))
        wins_df = exits[pnl > 0].copy()
        losses_df = exits[pnl < 0].copy()
        wins = int(len(wins_df))
        losses = int(len(losses_df))
        net = float(pnl.sum())
        gross_profit = float(pnl[pnl > 0].sum()) if wins else 0.0
        gross_loss = abs(float(pnl[pnl < 0].sum())) if losses else 0.0
        win_rate_local = (wins / trade_count * 100.0) if trade_count else 0.0
        avg_win_local = float(pnl[pnl > 0].mean()) if wins else 0.0
        avg_loss_local = float(pnl[pnl < 0].mean()) if losses else 0.0
        profit_factor_local = gross_profit / gross_loss if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)
        best_trade = exits.loc[pnl.idxmax()] if not pnl.empty else None
        worst_trade = exits.loc[pnl.idxmin()] if not pnl.empty else None
        symbol_pnl = pd.DataFrame()
        if "symbol" in exits.columns:
            symbol_pnl = (
                exits.assign(_pnl=pnl)
                .groupby("symbol", as_index=False)
                .agg(pnl=("_pnl", "sum"), trades=("_pnl", "size"))
            )
            symbol_pnl = symbol_pnl.sort_values("pnl", ascending=False)

        right = []
        wrong = []
        suggestions = []

        if net > 0:
            right.append(f"You finished positive with net realized P/L of {_format_signed_money(net)} across {trade_count} closed trade{'s' if trade_count != 1 else ''}.")
        elif net < 0:
            wrong.append(f"The selected period finished negative at {_format_signed_money(net)} across {trade_count} closed trade{'s' if trade_count != 1 else ''}.")
        else:
            right.append(f"You kept the selected period flat across {trade_count} closed trade{'s' if trade_count != 1 else ''}.")

        if win_rate_local >= 55:
            right.append(f"Your win rate was solid at {win_rate_local:.1f}%, which means entries were often moving in the intended direction.")
        elif trade_count >= 3:
            wrong.append(f"Win rate was only {win_rate_local:.1f}%, so too many setups failed before producing realized gains.")

        if profit_factor_local >= 1.5:
            right.append(f"Profit factor was {profit_factor_local:.2f}, so winners outweighed losers by a healthy margin.")
        elif gross_loss > 0:
            wrong.append(f"Profit factor was {profit_factor_local:.2f}; losses are absorbing too much of the winning trade P/L.")

        if wins and losses and abs(avg_loss_local) > avg_win_local:
            wrong.append(f"Average loss ({_format_signed_money(avg_loss_local)}) was larger than average win ({_format_signed_money(avg_win_local)}).")
            suggestions.append("Tighten loss exits or let the strongest winning trades reach a larger target before taking profit.")
        elif wins and avg_win_local > abs(avg_loss_local):
            right.append(f"Average winner ({_format_signed_money(avg_win_local)}) was larger than average loser ({_format_signed_money(avg_loss_local)}).")

        if not symbol_pnl.empty:
            best_symbol = symbol_pnl.iloc[0]
            worst_symbol = symbol_pnl.iloc[-1]
            if float(best_symbol["pnl"]) > 0:
                right.append(f"Best symbol was {best_symbol['symbol']} with {_format_signed_money(best_symbol['pnl'])} over {int(best_symbol['trades'])} closed trade{'s' if int(best_symbol['trades']) != 1 else ''}.")
            if float(worst_symbol["pnl"]) < 0:
                wrong.append(f"Weakest symbol was {worst_symbol['symbol']} with {_format_signed_money(worst_symbol['pnl'])}; be more selective there until the setup quality improves.")

        exit_reasons = _top_value_counts(exits, "exit_reason")
        if exit_reasons:
            right.append("Most common exit reason(s): " + ", ".join(exit_reasons) + ".")
        elif "broker_status" in exits.columns:
            broker_statuses = _top_value_counts(exits, "broker_status")
            if broker_statuses:
                right.append("Most common broker status on exits: " + ", ".join(broker_statuses) + ".")

        if trade_count >= 6:
            losing_streak = 0
            max_losing_streak = 0
            for value in pnl.tolist():
                if value < 0:
                    losing_streak += 1
                    max_losing_streak = max(max_losing_streak, losing_streak)
                else:
                    losing_streak = 0
            if max_losing_streak >= 3:
                wrong.append(f"There was a {max_losing_streak}-trade losing streak. That is a good place to pause or reduce size.")
                suggestions.append("After two consecutive losses, consider forcing the next signal to meet a higher score/clean-contract threshold.")

        if not entries.empty and trade_count and len(entries) > trade_count * 1.5:
            suggestions.append("There were noticeably more entries than exits in this filtered view; check whether positions are being split, duplicated, or left open longer than intended.")

        if not suggestions:
            if net >= 0:
                suggestions.append("Keep using the setups that produced positive P/L, but track whether the same symbols and exit rules keep working over the next few sessions.")
            else:
                suggestions.append("Reduce size until the selected setup/filter combination shows a positive profit factor over several closed trades.")
        if trade_count < 3:
            suggestions.append("Treat this as a light read: fewer than three closed trades is too small for a reliable pattern.")

        if best_trade is not None and worst_trade is not None:
            headline = (
                f"{period_label}: {trade_count} closed, {wins} win{'s' if wins != 1 else ''}, "
                f"{losses} loss{'es' if losses != 1 else ''}, net {_format_signed_money(net)}. "
                f"Best: {_safe_trade_text(best_trade.get('symbol'), 'N/A')} {_format_signed_money(best_trade.get('realized_pnl', 0))}; "
                f"worst: {_safe_trade_text(worst_trade.get('symbol'), 'N/A')} {_format_signed_money(worst_trade.get('realized_pnl', 0))}."
            )
        else:
            headline = f"{period_label}: net {_format_signed_money(net)} from {trade_count} closed trades."

        stats = [
            ("Closed", trade_count),
            ("Net P/L", _format_signed_money(net)),
            ("Win Rate", f"{win_rate_local:.1f}%"),
            ("Profit Factor", f"{profit_factor_local:.2f}"),
            ("Avg Win", _format_signed_money(avg_win_local)),
            ("Avg Loss", _format_signed_money(avg_loss_local)),
        ]
        worst_cols = [c for c in ["timestamp", "symbol", "signal", "option", "quantity", "exit_price", "realized_pnl", "exit_reason", "broker_status"] if c in exits.columns]
        worst = exits.assign(_pnl=pnl).sort_values("_pnl").head(5)
        worst = worst[worst_cols].copy() if worst_cols else pd.DataFrame()
        return {
            "headline": headline,
            "right": right[:5],
            "wrong": wrong[:5] or ["No obvious repeated mistake stood out in the selected closed trades."],
            "suggestions": suggestions[:5],
            "stats": stats,
            "worst": worst,
        }

    def _render_trade_ai_summary(exits_df: pd.DataFrame, entries_df: pd.DataFrame, period_label: str, start_date, end_date) -> None:
        review = _build_trade_ai_summary(exits_df, entries_df, period_label, start_date, end_date)
        stat_html = "".join(
            f"<div class='pt-ai-stat'><span>{html.escape(str(label))}</span><strong>{html.escape(str(value))}</strong></div>"
            for label, value in review.get("stats", [])
        )

        def section(title: str, items: list[str], tone: str) -> str:
            bullet_html = "".join(f"<li>{html.escape(str(item))}</li>" for item in items)
            return f"<div class='pt-ai-section pt-ai-{tone}'><h4>{html.escape(title)}</h4><ul>{bullet_html}</ul></div>"

        st.markdown(
            f"""
            <style>
            .pt-ai-review {{
                border: 1px solid #dbeafe;
                border-radius: 8px;
                background: linear-gradient(180deg, #f8fbff 0%, #ffffff 100%);
                padding: 1rem;
                margin: 0.25rem 0 1.25rem;
            }}
            .pt-ai-title {{
                display: flex;
                align-items: baseline;
                justify-content: space-between;
                gap: 0.75rem;
                margin-bottom: 0.75rem;
            }}
            .pt-ai-title h3 {{
                margin: 0;
                color: #0f172a;
                font-size: 1.1rem;
                line-height: 1.25;
            }}
            .pt-ai-title span {{
                color: #64748b;
                font-size: 0.84rem;
                font-weight: 700;
                white-space: nowrap;
            }}
            .pt-ai-headline {{
                color: #111827;
                font-size: 0.98rem;
                line-height: 1.45;
                margin: 0.2rem 0 0.9rem;
            }}
            .pt-ai-stats {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(128px, 1fr));
                gap: 0.55rem;
                margin-bottom: 0.9rem;
            }}
            .pt-ai-stat {{
                border: 1px solid #e5e7eb;
                border-radius: 8px;
                background: #ffffff;
                padding: 0.58rem 0.7rem;
            }}
            .pt-ai-stat span {{
                display: block;
                color: #64748b;
                font-size: 0.75rem;
                font-weight: 800;
            }}
            .pt-ai-stat strong {{
                display: block;
                color: #111827;
                font-size: 0.98rem;
                margin-top: 0.18rem;
            }}
            .pt-ai-grid {{
                display: grid;
                grid-template-columns: repeat(3, minmax(0, 1fr));
                gap: 0.75rem;
            }}
            .pt-ai-section {{
                border: 1px solid #e5e7eb;
                border-radius: 8px;
                background: #ffffff;
                padding: 0.82rem;
                min-height: 160px;
            }}
            .pt-ai-section h4 {{
                margin: 0 0 0.55rem;
                color: #111827;
                font-size: 0.92rem;
            }}
            .pt-ai-section ul {{
                margin: 0;
                padding-left: 1rem;
                color: #374151;
                font-size: 0.88rem;
                line-height: 1.38;
            }}
            .pt-ai-section li {{ margin-bottom: 0.45rem; }}
            .pt-ai-right {{ border-top: 4px solid #10b981; }}
            .pt-ai-wrong {{ border-top: 4px solid #ef4444; }}
            .pt-ai-improve {{ border-top: 4px solid #0b82ff; }}
            @media (max-width: 900px) {{
                .pt-ai-grid {{ grid-template-columns: 1fr; }}
                .pt-ai-title {{ display: block; }}
                .pt-ai-title span {{ display: block; margin-top: 0.25rem; white-space: normal; }}
            }}
            </style>
            <div class="pt-ai-review">
                <div class="pt-ai-title">
                    <h3>AI Trade Review</h3>
                    <span>{html.escape(str(start_date))} to {html.escape(str(end_date))}</span>
                </div>
                <div class="pt-ai-headline">{html.escape(review["headline"])}</div>
                <div class="pt-ai-stats">{stat_html}</div>
                <div class="pt-ai-grid">
                    {section("What You Did Right", review["right"], "right")}
                    {section("What Went Wrong", review["wrong"], "wrong")}
                    {section("How To Improve", review["suggestions"], "improve")}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        _render_deep_trade_audit(exits_df, entries_df, start_date, end_date)

    flex_cfg = cfg.setdefault("ibkr_flex", {})
    token_ready = bool(os.getenv("IBKR_FLEX_TOKEN") or flex_cfg.get("token"))
    query_ready = bool(os.getenv("IBKR_FLEX_TRADE_QUERY_ID") or flex_cfg.get("trade_query_id"))
    if st.button("Sync All IBKR Trades", use_container_width=True):
        total_imported = 0
        messages = []
        if sync_flex_trades_to_trade_log is not None and token_ready and query_ready:
            try:
                imported, message = sync_flex_trades_to_trade_log(cfg, TRADE_LOG_FILE)
                total_imported += int(imported or 0)
                messages.append(f"Flex: {message}")
            except Exception as exc:
                messages.append(f"Flex failed: {exc}")
        else:
            messages.append("Flex skipped: token/query ID missing.")

        sync_ib = None
        try:
            sync_ib_cfg = IBConfig(
                host=ib_cfg.host,
                port=ib_cfg.port,
                client_id=ib_cfg.client_id + 207,
                account=ib_cfg.account,
                readonly=True,
            )
            sync_ib = connect_ib(sync_ib_cfg)
            recent_imported = 0
            today_et = datetime.now(EASTERN).date()
            for day_offset in range(0, 10):
                imported, message = sync_today_executions_to_trade_log(
                    sync_ib,
                    account=ib_cfg.account,
                    target_date=today_et - timedelta(days=day_offset),
                )
                recent_imported += int(imported or 0)
            total_imported += recent_imported
            messages.append(f"Recent live executions: imported={recent_imported}")
        except Exception as exc:
            messages.append(f"Recent live execution sync failed: {display_exception_message(exc)}")
        finally:
            try:
                if sync_ib and sync_ib.isConnected():
                    sync_ib.disconnect()
            except Exception:
                pass

        if total_imported:
            st.success(f"Imported {total_imported} new trade row(s).")
        else:
            st.info("No new IBKR trade rows imported.")
        for message in messages:
            st.caption(message)

    trade_log = _load_trade_log_df()
    replay_df = load_trade_replay() if "load_trade_replay" in globals() else pd.DataFrame()

    if trade_log.empty and replay_df.empty:
        render_empty_performance_dashboard()
    else:
        now_et = datetime.now(EASTERN)
        today = now_et.date()
        base_dates = trade_log["timestamp"].dt.date if not trade_log.empty else replay_df["timestamp"].dt.date

        if selected_page == "🤖 AI AUDIT":
            st.markdown("### Audit Filters")
            audit_cols = st.columns([1.45, 1, 1, 1])
            with audit_cols[0]:
                audit_date_range = st.date_input(
                    "Date range",
                    value=(base_dates.max(), base_dates.max()),
                    key="ai_audit_date_range",
                )
            if isinstance(audit_date_range, tuple):
                audit_start_date = audit_date_range[0] if audit_date_range else base_dates.max()
                audit_end_date = audit_date_range[-1] if len(audit_date_range) > 1 else audit_start_date
            else:
                audit_start_date = audit_date_range
                audit_end_date = audit_date_range

            audit_source = trade_log.copy()
            if not audit_source.empty:
                audit_mask = (audit_source["timestamp"].dt.date >= audit_start_date) & (audit_source["timestamp"].dt.date <= audit_end_date)
                audit_source = audit_source[audit_mask].copy()

            audit_symbols = sorted(audit_source.get("symbol", pd.Series(dtype=str)).dropna().astype(str).unique().tolist())
            with audit_cols[1]:
                audit_selected_symbols = st.multiselect("Symbol", audit_symbols, default=[], key="ai_audit_symbols")
            with audit_cols[2]:
                audit_direction = st.selectbox("Direction", ["All", "CALL", "PUT"], key="ai_audit_direction")
            with audit_cols[3]:
                audit_outcome = st.selectbox("Outcome", ["All", "Winners", "Losers", "Breakeven"], key="ai_audit_outcome")

            def _set_ai_audit_date_range(start_value, end_value) -> None:
                st.session_state["ai_audit_date_range"] = (start_value, end_value)

            last_month_end = today.replace(day=1) - timedelta(days=1)
            last_month_start = last_month_end.replace(day=1)
            preset_cols = st.columns(5)
            preset_options = [
                ("Today", today, today),
                ("Yesterday", today - timedelta(days=1), today - timedelta(days=1)),
                ("This Month", today.replace(day=1), today),
                ("Last Month", last_month_start, last_month_end),
                ("All Time", base_dates.min(), base_dates.max()),
            ]
            for preset_col, (label, preset_start, preset_end) in zip(preset_cols, preset_options):
                with preset_col:
                    st.button(
                        label,
                        key=f"ai_audit_preset_{label.lower().replace(' ', '_')}",
                        use_container_width=True,
                        on_click=_set_ai_audit_date_range,
                        args=(preset_start, preset_end),
                    )

            if audit_selected_symbols and "symbol" in audit_source.columns:
                audit_source = audit_source[audit_source["symbol"].astype(str).isin(audit_selected_symbols)].copy()
            if audit_direction != "All" and "signal" in audit_source.columns:
                audit_source = audit_source[audit_source["signal"].astype(str).str.upper() == audit_direction].copy()

            audit_exits = audit_source[audit_source.get("event", pd.Series(dtype=str)).astype(str) == "EXIT"].copy() if not audit_source.empty else pd.DataFrame()
            if not audit_exits.empty:
                if audit_outcome == "Winners":
                    audit_exits = audit_exits[audit_exits["realized_pnl"] > 0]
                elif audit_outcome == "Losers":
                    audit_exits = audit_exits[audit_exits["realized_pnl"] < 0]
                elif audit_outcome == "Breakeven":
                    audit_exits = audit_exits[audit_exits["realized_pnl"] == 0]
            audit_exits = _collapse_logical_trade_rows(audit_exits) if not audit_exits.empty else audit_exits

            audit_entries = audit_source[audit_source.get("event", pd.Series(dtype=str)).astype(str) == "ENTRY"].copy() if not audit_source.empty else pd.DataFrame()
            if not audit_entries.empty and "status" in audit_entries.columns:
                inactive_statuses = {"cancelled", "canceled", "apicancelled", "inactive", "rejected"}
                audit_entries = audit_entries[~audit_entries["status"].fillna("").astype(str).str.lower().isin(inactive_statuses)].copy()
            audit_entries = _collapse_logical_trade_rows(audit_entries) if not audit_entries.empty else audit_entries

            if audit_start_date > audit_end_date:
                st.warning("Start date must be before or equal to end date.")
            else:
                st.caption(f"Auditing {len(audit_exits)} closed trade(s) from {audit_start_date} to {audit_end_date}.")
                _render_trade_ai_summary(audit_exits, audit_entries, "AI Audit", audit_start_date, audit_end_date)
            st.stop()

        period = st.radio("Performance Period", ["Today", "This Week", "This Month", "All Time", "Custom Range"], index=2, horizontal=True)
        if period == "Today":
            start_date, end_date = today, today
        elif period == "This Week":
            start_date, end_date = today - timedelta(days=today.weekday()), today
        elif period == "This Month":
            start_date, end_date = today.replace(day=1), today
        elif period == "Custom Range":
            c1, c2 = st.columns(2)
            with c1:
                start_date = st.date_input("Start date", value=today - timedelta(days=30))
            with c2:
                end_date = st.date_input("End date", value=today)
        else:
            start_date, end_date = base_dates.min(), base_dates.max()

        filtered = trade_log.copy()
        if not filtered.empty:
            mask = (filtered["timestamp"].dt.date >= start_date) & (filtered["timestamp"].dt.date <= end_date)
            filtered = filtered[mask].copy()

        replay_filtered = replay_df.copy()
        if not replay_filtered.empty and "timestamp" in replay_filtered.columns:
            mask = (replay_filtered["timestamp"].dt.date >= start_date) & (replay_filtered["timestamp"].dt.date <= end_date)
            replay_filtered = replay_filtered[mask].copy()

        filter_cols = st.columns(3)
        symbols_available = sorted(set(filtered.get("symbol", pd.Series(dtype=str)).dropna().astype(str).tolist() + replay_filtered.get("symbol", pd.Series(dtype=str)).dropna().astype(str).tolist()))
        with filter_cols[0]:
            selected_symbols = st.multiselect("Symbol filter", symbols_available, default=[])
        with filter_cols[1]:
            direction_filter = st.selectbox("Direction", ["All", "CALL", "PUT"])
        with filter_cols[2]:
            outcome_filter = st.selectbox("Outcome", ["All", "Winners", "Losers", "Breakeven"])

        def _apply_common_filters(df: pd.DataFrame) -> pd.DataFrame:
            out = df.copy()
            if selected_symbols and "symbol" in out.columns:
                out = out[out["symbol"].astype(str).isin(selected_symbols)]
            if direction_filter != "All" and "signal" in out.columns:
                out = out[out["signal"].astype(str).str.upper() == direction_filter]
            return out

        filtered = _apply_common_filters(filtered)
        replay_filtered = _apply_common_filters(replay_filtered)

        exits = filtered[filtered.get("event", pd.Series(dtype=str)).astype(str) == "EXIT"].copy() if not filtered.empty else pd.DataFrame()
        if not exits.empty:
            if outcome_filter == "Winners":
                exits = exits[exits["realized_pnl"] > 0]
            elif outcome_filter == "Losers":
                exits = exits[exits["realized_pnl"] < 0]
            elif outcome_filter == "Breakeven":
                exits = exits[exits["realized_pnl"] == 0]
        raw_exit_count = int(len(exits))
        exits = _collapse_logical_trade_rows(exits) if not exits.empty else exits

        calendar_source = _apply_common_filters(trade_log.copy()) if not trade_log.empty else pd.DataFrame()
        calendar_exits = calendar_source[calendar_source.get("event", pd.Series(dtype=str)).astype(str) == "EXIT"].copy() if not calendar_source.empty else pd.DataFrame()
        if not calendar_exits.empty:
            if outcome_filter == "Winners":
                calendar_exits = calendar_exits[calendar_exits["realized_pnl"] > 0]
            elif outcome_filter == "Losers":
                calendar_exits = calendar_exits[calendar_exits["realized_pnl"] < 0]
            elif outcome_filter == "Breakeven":
                calendar_exits = calendar_exits[calendar_exits["realized_pnl"] == 0]
        calendar_exits = _collapse_logical_trade_rows(calendar_exits) if not calendar_exits.empty else calendar_exits

        entries = filtered[filtered.get("event", pd.Series(dtype=str)).astype(str) == "ENTRY"].copy() if not filtered.empty else pd.DataFrame()
        if not entries.empty and "status" in entries.columns:
            inactive_statuses = {"cancelled", "canceled", "apicancelled", "inactive", "rejected"}
            entries = entries[~entries["status"].fillna("").astype(str).str.lower().isin(inactive_statuses)].copy()
        raw_entry_count = int(len(entries))
        entries = _collapse_logical_trade_rows(entries) if not entries.empty else entries
        total_entries = int(len(entries))
        total_exits = int(len(exits))
        realized_pnl = float(exits["realized_pnl"].sum()) if not exits.empty else 0.0
        wins = int((exits["realized_pnl"] > 0).sum()) if not exits.empty else 0
        losses = int((exits["realized_pnl"] < 0).sum()) if not exits.empty else 0
        win_rate = round((wins / total_exits * 100), 1) if total_exits else 0.0
        avg_win = float(exits.loc[exits["realized_pnl"] > 0, "realized_pnl"].mean()) if wins else 0.0
        avg_loss = float(exits.loc[exits["realized_pnl"] < 0, "realized_pnl"].mean()) if losses else 0.0
        gross_profit = float(exits.loc[exits["realized_pnl"] > 0, "realized_pnl"].sum()) if wins else 0.0
        gross_loss = abs(float(exits.loc[exits["realized_pnl"] < 0, "realized_pnl"].sum())) if losses else 0.0
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (round(gross_profit, 2) if gross_profit > 0 else 0.0)
        original_deposited = float(cfg.get("performance", {}).get("original_deposited_capital", 2300.0) or 0.0)
        pct_up = (realized_pnl / original_deposited * 100.0) if original_deposited > 0 else 0.0
        account_summary = st.session_state.get("ibkr_account_summary") or {}
        if _number_or_none(account_summary.get("AvailableFunds")) is None and live_ibkr_ping(ib_cfg):
            account_summary = sync_ibkr_account_status(force=False)
        available_funds = _number_or_none(account_summary.get("AvailableFunds"))
        available_funds_label = f"${available_funds:,.2f}" if available_funds is not None else "N/A"
        avg_loss_label = f"-${abs(avg_loss):,.0f}" if avg_loss < 0 else f"${avg_loss:,.0f}"

        _render_perf_kpis([
            {"label": "Net P/L", "value": f"${realized_pnl:,.2f}", "tone": "positive" if realized_pnl >= 0 else "negative"},
            {"label": "Win Rate", "value": f"{win_rate}%"},
            {"label": "Entries", "value": total_entries},
            {"label": "Closed", "value": total_exits},
            {
                "label": "Avg W/L",
                "value": (
                    f"<span class='pt-kpi-positive'>${avg_win:,.0f}</span>"
                    f"<span style='color:#6b7280;'> / </span>"
                    f"<span class='pt-kpi-negative'>{avg_loss_label}</span>"
                ),
            },
            {"label": "Profit Factor", "value": profit_factor},
            {"label": "% Return", "value": f"{pct_up:.2f}%", "tone": "positive" if pct_up >= 0 else "negative"},
            {"label": "Deposited", "value": f"${original_deposited:,.2f}"},
            {"label": "Avail. Funds", "value": available_funds_label},
        ])
        if raw_entry_count != total_entries or raw_exit_count != total_exits:
            st.caption(
                f"Performance counts logical trades. Raw broker fills in this view: "
                f"{raw_entry_count} entry row(s), {raw_exit_count} exit row(s)."
            )

        calendar_anchor = today
        if period == "All Time" and not calendar_exits.empty:
            calendar_anchor = calendar_exits["timestamp"].max().date()
        elif period == "Custom Range":
            calendar_anchor = start_date
        st.markdown("### Monthly P/L Calendar")
        calendar_anchor = _render_calendar_controls(calendar_anchor, calendar_exits)
        _render_monthly_pnl_calendar(calendar_exits, calendar_anchor)

        if not exits.empty:
            exits = exits.sort_values("timestamp")
            daily = exits.copy()
            daily["date"] = daily["timestamp"].dt.date
            daily_pnl = daily.groupby("date", as_index=False)["realized_pnl"].sum()
            daily_pnl["cumulative_pnl"] = daily_pnl["realized_pnl"].cumsum()

            baseline = pd.DataFrame([{
                "date": daily_pnl["date"].min() - timedelta(days=1),
                "realized_pnl": 0.0,
                "cumulative_pnl": 0.0,
            }])
            equity_daily = pd.concat([baseline, daily_pnl], ignore_index=True)
            eq_fig = go.Figure()
            eq_fig.add_trace(go.Scatter(
                x=equity_daily["date"],
                y=equity_daily["cumulative_pnl"],
                mode="lines",
                fill="tozeroy",
                line=dict(color="#6554d9", width=2),
                fillcolor="rgba(16, 185, 129, 0.22)",
                hovertemplate="%{x|%m/%d/%y}<br>$%{y:,.2f}<extra></extra>",
                name="Cumulative P/L",
            ))
            eq_fig = _format_pnl_chart(eq_fig, height=260)
            eq_fig.update_xaxes(tickformat="%m/%d/%y")

            chart_cols = st.columns(3)
            daily_fig = go.Figure()
            daily_colors = ["#10b981" if float(v) >= 0 else "#ef4444" for v in daily_pnl["realized_pnl"]]
            daily_fig.add_trace(go.Bar(
                x=daily_pnl["date"],
                y=daily_pnl["realized_pnl"],
                marker_color=daily_colors,
                hovertemplate="%{x|%m/%d/%y}<br>$%{y:,.2f}<extra></extra>",
                name="Daily P/L",
            ))
            daily_fig = _format_pnl_chart(daily_fig, height=260)
            daily_fig.update_xaxes(tickformat="%m/%d/%y")

            with chart_cols[0]:
                with st.container(border=True):
                    _chart_card_title("Daily net cumulative P&L")
                    st.plotly_chart(eq_fig, use_container_width=True)
            with chart_cols[1]:
                with st.container(border=True):
                    _chart_card_title("Net daily P&L")
                    st.plotly_chart(daily_fig, use_container_width=True)

            if "symbol" in exits.columns:
                sym_pnl = exits.groupby("symbol", as_index=False)["realized_pnl"].sum().sort_values("realized_pnl", ascending=False)
                sym_fig = go.Figure()
                sym_colors = ["#10b981" if float(v) >= 0 else "#ef4444" for v in sym_pnl["realized_pnl"]]
                sym_fig.add_trace(go.Bar(
                    x=sym_pnl["symbol"].astype(str),
                    y=sym_pnl["realized_pnl"],
                    marker_color=sym_colors,
                    hovertemplate="%{x}<br>$%{y:,.2f}<extra></extra>",
                    name="Symbol P/L",
                ))
                sym_fig = _format_pnl_chart(sym_fig, height=260)
                with chart_cols[2]:
                    with st.container(border=True):
                        _chart_card_title("Net P&L by symbol")
                        st.plotly_chart(sym_fig, use_container_width=True)
            else:
                with chart_cols[2]:
                    with st.container(border=True):
                        _chart_card_title("Net P&L by symbol")
                        st.plotly_chart(_format_pnl_chart(_empty_fig("Net P&L by symbol", "P/L USD"), height=260), use_container_width=True)
        else:
            st.info("No closed trades match the selected filters yet.")
            chart_cols = st.columns(3)
            with chart_cols[0]:
                with st.container(border=True):
                    _chart_card_title("Daily net cumulative P&L")
                    st.plotly_chart(_format_pnl_chart(_empty_fig("Daily net cumulative P&L", "Realized P/L USD"), height=260), use_container_width=True)
            with chart_cols[1]:
                with st.container(border=True):
                    _chart_card_title("Net daily P&L")
                    st.plotly_chart(_format_pnl_chart(_empty_fig("Net daily P&L", "Daily P/L USD"), height=260), use_container_width=True)
            with chart_cols[2]:
                with st.container(border=True):
                    _chart_card_title("Net P&L by symbol")
                    st.plotly_chart(_format_pnl_chart(_empty_fig("Net P&L by symbol", "P/L USD"), height=260), use_container_width=True)

        st.markdown("### Executed Trade Journal")
        if filtered.empty:
            st.info("No executed trades match the selected filters.")
        else:
            executed_cols = [
                c for c in [
                    "timestamp",
                    "event",
                    "symbol",
                    "signal",
                    "option",
                    "quantity",
                    "filled_quantity",
                    "entry_price",
                    "exit_price",
                    "realized_pnl",
                    "status",
                    "broker_status",
                    "source",
                    "external_id",
                ] if c in filtered.columns
            ]
            executed_journal = filtered[executed_cols].sort_values("timestamp", ascending=False).copy()
            recent_rows = []
            if not exits.empty:
                for _, row in exits.sort_values("timestamp", ascending=False).head(25).iterrows():
                    close_date = row["timestamp"].strftime("%m/%d/%Y") if pd.notna(row.get("timestamp")) else ""
                    pnl = float(row.get("realized_pnl", 0.0) or 0.0)
                    recent_rows.append({
                        "close_date": close_date,
                        "symbol": row.get("symbol", ""),
                        "net_pnl": _format_signed_money(pnl),
                        "net_pnl_raw": pnl,
                    })

            open_rows = []
            active_positions = read_active_positions()
            if active_positions:
                for position in active_positions[:25]:
                    opened = position.get("entry_time") or position.get("timestamp") or position.get("opened_at") or ""
                    try:
                        opened_label = pd.to_datetime(opened, errors="coerce").strftime("%m/%d/%Y")
                    except Exception:
                        opened_label = str(opened)[:10]
                    entry_price = position.get("entry_price", position.get("avgCost", ""))
                    try:
                        entry_label = f"${float(entry_price):,.2f}"
                    except Exception:
                        entry_label = str(entry_price)
                    open_rows.append({
                        "open_date": opened_label,
                        "symbol": position.get("symbol", ""),
                        "entry": entry_label,
                    })

            tab_recent, tab_open = st.tabs(["Recent trades", "Open positions"])
            with tab_recent:
                _render_performance_table(
                    recent_rows,
                    [("close_date", "Close Date"), ("symbol", "Symbol"), ("net_pnl", "Net P&L")],
                    "No closed trades match the selected filters.",
                )
            with tab_open:
                _render_performance_table(
                    open_rows,
                    [("open_date", "Open Date"), ("symbol", "Symbol"), ("entry", "Entry")],
                    "No open bot-managed positions.",
                )

            with st.expander("Detailed executed trade journal"):
                st.dataframe(executed_journal.head(250), use_container_width=True)
            st.download_button("Download executed trade journal", executed_journal.to_csv(index=False), "executed_trade_journal.csv", "text/csv")

        st.markdown("### Trade Replay Journal")
        st.caption("Replay rows are scanner snapshots and signal/order attempts. Executed broker trades are shown above.")
        if replay_filtered.empty:
            st.info("No replay snapshots yet. The engine records them for qualified signals and entries.")
        else:
            key_cols = [c for c in ["timestamp", "event", "symbol", "signal", "score", "confidence", "rank_score", "price", "option", "mid", "quantity", "estimated_cost", "order_status", "reasons"] if c in replay_filtered.columns]
            journal = replay_filtered[key_cols].copy()
            st.dataframe(journal.head(250), use_container_width=True)
            st.download_button("Download replay journal", journal.to_csv(index=False), "trade_replay_journal.csv", "text/csv")

            with st.expander("Replay detail"):
                labels = journal.apply(lambda r: f"{r.get('timestamp', '')} | {r.get('symbol', '')} | {r.get('signal', '')} | {r.get('event', '')}", axis=1).tolist()
                if labels:
                    selected_label = st.selectbox("Select replay snapshot", labels)
                    selected_idx = labels.index(selected_label)
                    st.json(replay_filtered.iloc[selected_idx].dropna().to_dict())

        if not filtered.empty:
            with st.expander("Raw trade history"):
                st.dataframe(filtered.tail(250), use_container_width=True)
                st.download_button("Download filtered trade log", filtered.to_csv(index=False), "filtered_trade_log.csv", "text/csv")

elif selected_page == "📝 Logs":
    st.subheader("Logs")
    col_a, col_b = st.columns(2)
    with col_a:
        st.write("Trade Log")
        if os.path.exists(TRADE_LOG_FILE):
            trade_log = pd.read_csv(TRADE_LOG_FILE)
            st.dataframe(trade_log.tail(100), use_container_width=True)
            st.download_button("Download trade log", trade_log.to_csv(index=False), "trade_log.csv", "text/csv")
        else:
            st.info("No trades logged yet.")
    with col_b:
        st.write("Alert Log")
        if os.path.exists(ALERT_LOG_FILE):
            alert_log = pd.read_csv(ALERT_LOG_FILE)
            st.dataframe(alert_log.tail(100), use_container_width=True)
        else:
            st.info("No alerts logged yet.")
    latest_logs = sorted(LOG_DIR.glob("*.log"), reverse=True)
    if latest_logs:
        st.write(f"Latest App Log: {latest_logs[0].name}")
        st.code(latest_logs[0].read_text(encoding="utf-8")[-5000:])


elif selected_page == "🧠 Market Intelligence":
    if render_market_intelligence_tab is None:
        st.error("Market Intelligence module could not be loaded.")
        st.caption("Make sure modules/news_dashboard.py, modules/news_database.py, and modules/news_analyzer.py exist and compile.")
    else:
        try:
            render_market_intelligence_tab()
        except Exception as e:
            st.error(f"Market Intelligence failed: {e}")
            st.caption("Check logs/news_engine.log and confirm data/news.db exists.")

elif selected_page == "📈 Strategy Lab":
    if render_strategy_lab_tab is None:
        st.error("Strategy Lab module could not be loaded.")
        st.caption("Make sure backtester/ui.py, controller.py, data.py, replay.py, strategy.py, simulator.py, and metrics.py exist and compile.")
    else:
        render_strategy_lab_tab(cfg, symbols)
