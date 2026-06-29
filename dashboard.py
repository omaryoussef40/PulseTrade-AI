# dashboard.py
# Streamlit dashboard for AutoTrader. The dashboard edits config.json; engine.py reads the same config.

from __future__ import annotations

import os
import asyncio
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
    make_approval_id,
    process_telegram_order_callbacks,
    read_pending_approvals,
    run_cycle,
    send_order_approval_message,
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
    div[data-testid="stMetricValue"] { font-size: 1.35rem !important; white-space: nowrap !important; }
    div[data-testid="stMetricLabel"] { font-size: 0.82rem !important; }
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

    /* Cleaner sidebar navigation */
    section[data-testid="stSidebar"] {
        border-right: 1px solid rgba(250,250,250,0.08);
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] label {
        border: 1px solid rgba(250,250,250,0.12);
        border-radius: 12px;
        width: 100%;
        min-width: 100%;
        box-sizing: border-box;
        padding: 10px 12px;
        margin-bottom: 8px;
        background: rgba(255,255,255,0.035);
        transition: all 0.15s ease;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] label:hover {
        border-color: rgba(239,68,68,0.55);
        background: rgba(239,68,68,0.08);
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) {
        border-color: rgba(239,68,68,0.95);
        background: rgba(239,68,68,0.16);
        box-shadow: inset 3px 0 0 rgba(239,68,68,0.95);
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

</style>
""")


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


def render_empty_performance_dashboard():
    """Keep the Performance tab useful and visually complete before any trades exist."""
    st.markdown("### Performance Overview")
    st.caption("No trades have been logged yet. These panels will populate automatically after signals, entries, and exits are recorded.")

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Net P/L", "$0.00")
    c2.metric("Win Rate", "0%")
    c3.metric("Entries", "0")
    c4.metric("Closed", "0")
    c5.metric("Avg Win / Loss", "$0 / $0")
    c6.metric("Profit Factor", "0.00")

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


def render_platform_settings():
    st.markdown("### Platform Settings")
    st.caption("These settings were previously in the left control panel. They now live here so the sidebar can be used only for navigation.")

    st.header("Control Panel")

    with st.expander("Automation Safety", expanded=False):
        dashboard_cfg = cfg.setdefault("dashboard", {})
        cfg["account_mode"] = st.radio("Trading account mode", ["Simulation", "Paper", "Live"], index=["Simulation", "Paper", "Live"].index(cfg.get("account_mode", "Simulation")), horizontal=True)
        dashboard_cfg["auto_start_engines"] = st.checkbox("Auto-start engine/news from dashboard", value=dashboard_auto_start_enabled(cfg))
        cfg["automation"]["enabled"] = st.checkbox("Enable engine automation", value=bool(cfg["automation"].get("enabled", False)))
        cfg["automation"]["place_orders"] = st.checkbox("Allow engine to place orders", value=bool(cfg["automation"].get("place_orders", False)))
        cfg["automation"]["confirm_order_risk"] = st.checkbox("I understand this can place IBKR orders", value=bool(cfg["automation"].get("confirm_order_risk", False)))
        cfg["automation"]["require_trade_approval"] = st.checkbox("Require Telegram approval before entry orders", value=bool(cfg["automation"].get("require_trade_approval", False)))
        if cfg["account_mode"] == "Live":
            st.error("LIVE mode selected. Orders can use real money if all confirmations are enabled.")
            cfg["automation"]["live_confirm_text"] = st.text_input("Type TRADE LIVE to unlock live orders", value=str(cfg["automation"].get("live_confirm_text", "")))
        else:
            cfg["automation"]["live_confirm_text"] = ""
        cfg["automation"]["scan_interval_seconds"] = st.number_input("Engine scan interval seconds", value=int(cfg["automation"].get("scan_interval_seconds", 60)), min_value=10, max_value=3600, step=10)
        cfg["automation"]["scan_only_market_hours"] = st.checkbox("Scan only during market hours", value=bool(cfg["automation"].get("scan_only_market_hours", True)))
        cfg["order"]["type"] = st.selectbox("Order type", ["LIMIT", "MARKET"], index=0 if cfg["order"].get("type", "LIMIT") == "LIMIT" else 1)
        st.caption(f"Trading status: {trading_status_from_config(cfg)}")

    with st.expander("Position Controls", expanded=False):
        r = cfg["risk"]
        s = cfg["strategy"]
        buying_power = _number_or_none((st.session_state.get("ibkr_account_summary") or {}).get("BuyingPower"))
        use_buying_power = st.checkbox("Use full IBKR buying power as account size", value=bool(r.get("use_ibkr_buying_power", False)))
        r["use_ibkr_buying_power"] = bool(use_buying_power)
        if use_buying_power and buying_power is None:
            try:
                summary = cached_account_summary(
                    ib_cfg.host,
                    int(ib_cfg.port),
                    int(ib_cfg.client_id),
                    ib_cfg.account,
                    bool(ib_cfg.readonly),
                )
                summary["connected"] = True
                summary.setdefault("fetched_at", datetime.now().isoformat(timespec="seconds"))
                st.session_state["ibkr_account_summary"] = summary
                buying_power = _number_or_none(summary.get("BuyingPower"))
            except Exception as exc:
                st.caption(f"IBKR buying power is not available yet, using saved account size. {exc}")

        if use_buying_power and buying_power is not None:
            r["account_size"] = round(float(buying_power), 2)
            st.number_input("Account size USD", value=float(r["account_size"]), min_value=0.0, step=100.0, disabled=True)
            st.caption(f"Using IBKR BuyingPower: ${float(buying_power):,.2f}")
        else:
            r["account_size"] = st.number_input("Account size USD", value=float(r.get("account_size", 1000)), min_value=100.0, step=100.0)

        account_size = max(float(r.get("account_size", 1000) or 1000), 1.0)
        r["max_trades_per_day"] = st.number_input("Max trades per day", value=int(r.get("max_trades_per_day", 2)), min_value=1, max_value=10, step=1)
        s["top_n_tickers"] = st.number_input("Trade only top N tickers", value=int(s.get("top_n_tickers", 2)), min_value=1, max_value=10, step=1)
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
        r["max_contracts"] = st.number_input("Max contracts per trade", value=int(r.get("max_contracts", 2)), min_value=1, max_value=20, step=1)
        today_trade_count, today_deployed_capital = get_today_trade_stats()
        st.caption(f"Today: {today_trade_count} trades | ${today_deployed_capital:,.2f} deployed")

    with st.expander("Trade Management", expanded=False):
        r = cfg["risk"]
        s = cfg["strategy"]
        current_orb_minutes = int(s.get("orb_minutes", 15))
        if current_orb_minutes not in [15, 30]:
            current_orb_minutes = 15
        s["orb_minutes"] = st.selectbox(
            "ORB window",
            [15, 30],
            index=[15, 30].index(current_orb_minutes),
            format_func=lambda minutes: f"{minutes} minutes",
        )
        r["stop_loss_pct"] = st.number_input("Option stop loss %", value=float(r.get("stop_loss_pct", 20.0)), min_value=1.0, max_value=90.0, step=1.0)
        r["take_profit_pct"] = st.number_input("Option take profit %", value=float(r.get("take_profit_pct", 30.0)), min_value=1.0, max_value=300.0, step=1.0)
        r["breakeven_trigger_pct"] = st.number_input("Move stop to breakeven at +%", value=float(r.get("breakeven_trigger_pct", 15.0)), min_value=1.0, max_value=200.0, step=1.0)
        r["trailing_trigger_pct"] = st.number_input("Activate trailing stop at +%", value=float(r.get("trailing_trigger_pct", 25.0)), min_value=1.0, max_value=300.0, step=1.0)
        r["trailing_stop_pct"] = st.number_input("Trailing stop distance %", value=float(r.get("trailing_stop_pct", 10.0)), min_value=1.0, max_value=90.0, step=1.0)
        r["force_exit_hour"] = st.number_input("Force exit hour ET", value=int(r.get("force_exit_hour", 15)), min_value=9, max_value=15, step=1)
        r["force_exit_minute"] = st.number_input("Force exit minute ET", value=int(r.get("force_exit_minute", 55)), min_value=0, max_value=59, step=1)
        r["max_consecutive_losses"] = st.number_input("Stop after consecutive losses", value=int(r.get("max_consecutive_losses", 2)), min_value=1, max_value=10, step=1)
        r["max_daily_drawdown_pct"] = st.number_input("Max daily drawdown % of account", value=float(r.get("max_daily_drawdown_pct", 5.0)), min_value=1.0, max_value=50.0, step=1.0)

    with st.expander("Signal Filters", expanded=False):
        s = cfg["strategy"]
        s["option_dte"] = st.number_input("Target option DTE", value=int(s.get("option_dte", 7)), min_value=0, max_value=45, step=1)
        s["min_score"] = st.number_input("Minimum score", value=int(s.get("min_score", 70)), min_value=0, max_value=100, step=5)
        s["min_confidence"] = st.number_input("Minimum confidence", value=int(s.get("min_confidence", 75)), min_value=0, max_value=100, step=5)
        s["use_rvol_filter"] = st.checkbox("Use RVOL as required filter", value=bool(s.get("use_rvol_filter", False)))
        s["min_rvol"] = st.number_input("Minimum RVOL", value=float(s.get("min_rvol", 1.5)), min_value=0.0, max_value=10.0, step=0.1)
        s["use_rvol_score"] = st.checkbox("Use RVOL bonus in technical score", value=bool(s.get("use_rvol_score", False)))
        s["use_rvol_ranking"] = st.checkbox("Use RVOL bonus in ranking", value=bool(s.get("use_rvol_ranking", False)))
        s["min_atr"] = st.number_input("Minimum ATR %", value=float(s.get("min_atr", 0.3)), min_value=0.0, max_value=10.0, step=0.1)
        if not s["use_rvol_filter"]:
            st.caption("RVOL is informational only and will not block trades.")

    with st.expander("Watchlist", expanded=False):
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
    st.caption("Connection Settings")

    with st.expander("IBKR Connection", expanded=False):
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

    with st.expander("Telegram", expanded=False):
        tg = cfg["telegram"]
        tg["bot_token"] = st.text_input("Bot token", value=tg.get("bot_token", os.getenv("TELEGRAM_BOT_TOKEN", "")), type="password")
        tg["chat_id"] = st.text_input("Chat ID", value=tg.get("chat_id", os.getenv("TELEGRAM_CHAT_ID", "")))
        tg["send_alerts"] = st.checkbox("Send Telegram alerts", value=bool(tg.get("send_alerts", False)))

    save_config(cfg)
    st.caption("Settings auto-saved to config.json")


with st.sidebar:
    st.markdown("### PulseTrade AI")
    st.caption("Navigation")
    selected_page = st.radio(
        "Menu",
        [
            "📊 Performance & Trade Journal",
            "💼 Positions",
            "📈 Strategy Lab",
            "🧠 Market Intelligence",
            "📈 Scanner & Breakdown",
            "🏦 Account Status",
            "📝 Logs",
        ],
        label_visibility="collapsed",
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


@st.cache_data(ttl=30, show_spinner=False)
def cached_account_summary(host: str, port: int, client_id: int, account: str | None, readonly: bool) -> dict:
    summary_cfg = IBConfig(host=host, port=port, client_id=client_id, account=account, readonly=readonly)
    return fetch_ibkr_account_summary_dict(summary_cfg)


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
            summary = fetch_ibkr_account_summary_dict(ib_cfg)
        else:
            summary = cached_account_summary(
                ib_cfg.host,
                int(ib_cfg.port),
                int(ib_cfg.client_id),
                ib_cfg.account,
                bool(ib_cfg.readonly),
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


def schedule_ibkr_reconnect_refresh() -> None:
    if not health.get("ib_connected"):
        st_autorefresh(interval=10_000, key="ibkr_reconnect_refresh")


PROJECT_ROOT = Path(__file__).resolve().parent
ENGINE_PID_FILE = PROJECT_ROOT / "data" / "trading_engine.pid"
ENGINE_LOG_FILE = PROJECT_ROOT / "logs" / "engine_stdout.log"
ENGINE_FILE = PROJECT_ROOT / "engine.py"
STATUS_ALERT_STATE_FILE = PROJECT_ROOT / "data" / "status_alert_state.json"
STATUS_ALERT_COOLDOWN_SECONDS = 300


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
    if _is_pid_running(pid):
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
        process = subprocess.Popen([sys.executable, str(ENGINE_FILE)], **kwargs)
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
                current_ib_cfg = IBConfig(
                    host=current_cfg["ib"].get("host", "127.0.0.1"),
                    port=ib_port_from_config(current_cfg),
                    client_id=int(current_cfg["ib"].get("client_id", 11)),
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
                app_log(f"Telegram decision worker error: {exc}", "WARN")
            time.sleep(2)

    threading.Thread(target=worker, daemon=True, name="telegram-decision-worker").start()


def load_manual_option_defaults(symbol: str, signal: str, dte_target: int) -> dict:
    ib = connect_ib(ib_cfg)
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
    st.caption("Uses the historical CALL/PUT scanner signals and simulates 7-DTE, ~0.50-delta option trades. This is still Yahoo-based approximation, not real historical option-chain pricing.")

    sim_col1, sim_col2, sim_col3, sim_col4 = st.columns(4)
    with sim_col1:
        sim_starting_capital = st.number_input("Backtest capital USD", min_value=100.0, value=float(risk.get("account_size", 1000)), step=100.0, key="bt_sim_capital")
        sim_max_trades = st.number_input("Max trades/day", min_value=1, max_value=10, value=int(risk.get("max_trades_per_day", 2)), step=1, key="bt_sim_max_trades")
    with sim_col2:
        sim_spend = st.number_input("Max spend/trade USD", min_value=50.0, value=float(risk.get("max_spend_per_trade", 250)), step=50.0, key="bt_sim_spend")
        sim_daily_cap = st.number_input("Max daily capital USD", min_value=50.0, value=float(risk.get("max_daily_capital", 500)), step=50.0, key="bt_sim_daily_cap")
    with sim_col3:
        sim_stop = st.number_input("Stop loss %", min_value=1.0, max_value=90.0, value=float(risk.get("stop_loss_pct", 20.0)), step=1.0, key="bt_sim_stop")
        sim_tp = st.number_input("Take profit %", min_value=1.0, max_value=300.0, value=float(risk.get("take_profit_pct", 30.0)), step=1.0, key="bt_sim_tp")
    with sim_col4:
        sim_premium_pct = st.number_input("Entry premium % of stock", min_value=0.5, max_value=10.0, value=2.5, step=0.1, key="bt_sim_premium_pct")
        sim_slippage = st.number_input("Slippage %", min_value=0.0, max_value=20.0, value=2.0, step=0.5, key="bt_sim_slippage")

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
                    max_contracts=int(risk.get("max_contracts", 2)),
                    stop_loss_pct=float(sim_stop),
                    take_profit_pct=float(sim_tp),
                    breakeven_trigger_pct=float(risk.get("breakeven_trigger_pct", 15.0)),
                    trailing_trigger_pct=float(risk.get("trailing_trigger_pct", 25.0)),
                    trailing_stop_pct=float(risk.get("trailing_stop_pct", 10.0)),
                    force_exit_time=dtime(int(risk.get("force_exit_hour", 15)), int(risk.get("force_exit_minute", 55))),
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

    st.info("Current phase: Yahoo replay now simulates approximate 7-DTE option entries/exits and P/L. Next phase: improve analytics, trade explorer, and parameter testing.")


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
            return json.loads(status_file.read_text(encoding="utf-8"))
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
    return stock_df, option_df, status


def run_ibkr_scanner_job(scan_cfg: dict, scan_symbols: list[str]) -> None:
    _status_file, stock_file, option_file = scanner_result_paths()
    asyncio.set_event_loop(asyncio.new_event_loop())
    write_scanner_job_status({
        "status": "running",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "symbols": list(scan_symbols),
        "message": "Scanner running",
    })
    rows, option_rows = [], []
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
        for i, symbol in enumerate(scan_symbols):
            try:
                result = scan_symbol_ib(
                    ib,
                    symbol,
                    bool(scan_cfg["strategy"].get("use_rvol_score", False)),
                    str(scan_cfg["strategy"].get("active_strategy", "pmb")),
                    int(scan_cfg["strategy"].get("orb_minutes", 15)),
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
                    ):
                        option = recommend_option_ib(ib, symbol, result["Signal"], result["Price"], int(scan_cfg["strategy"].get("option_dte", 7)))
                        if option:
                            option_clean = {k: v for k, v in option.items() if k != "Contract"}
                            option_rows.append({"Symbol": symbol, "Signal": result["Signal"], "Score": result["Score"], "Confidence": result["Confidence"], **option_clean})
            except Exception as e:
                rows.append({"Symbol": symbol, "Signal": "ERROR", "Score": 0, "Confidence": 0, "RVOL": 0, "ATR %": 0, "Reasons": display_exception_message(e)})
            write_scanner_job_status({
                "status": "running",
                "started_at": read_scanner_job_status().get("started_at"),
                "symbols": list(scan_symbols),
                "completed": i + 1,
                "total": len(scan_symbols),
                "message": f"Scanning {symbol}",
            })

        stock_df = pd.DataFrame(rows)
        if not stock_df.empty:
            stock_df = stock_df.sort_values(["Score", "Confidence", "RVOL", "ATR %"], ascending=[False, False, False, False])

        option_df = pd.DataFrame(option_rows)
        if not option_df.empty:
            option_df = option_df.sort_values(["Score", "Option Score"], ascending=[False, False])

        stock_df.to_csv(stock_file, index=False)
        option_df.to_csv(option_file, index=False)
        write_scanner_job_status({
            "status": "complete",
            "started_at": read_scanner_job_status().get("started_at"),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "symbols": list(scan_symbols),
            "completed": len(scan_symbols),
            "total": len(scan_symbols),
            "stock_rows": len(stock_df),
            "option_rows": len(option_df),
            "message": "Scanner complete",
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
    threading.Thread(target=run_ibkr_scanner_job, args=(json.loads(json.dumps(scan_cfg, default=str)), list(scan_symbols)), daemon=True, name="ibkr-scanner-job").start()
    st.success("Scanner started in the background. You can switch tabs and come back for results.")
    st_autorefresh(interval=2_000, key="scanner_job_refresh_started")


# Global compact terminal header shown on every page.
if dashboard_auto_start_enabled(cfg):
    start_trading_engine_once()
    auto_start_news_engine_once()
if selected_page == "🏦 Account Status":
    sync_ibkr_account_status(force=False)
    schedule_ibkr_reconnect_refresh()
render_app_header()

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
        elif scanner_job_status.get("status") != "running":
            st.info("No scanner results yet. Run the IBKR scanner once and the results will stay here while you navigate.")

    with breakdown_col:
        st.subheader("Ticker Breakdown")
        st.caption("Analyze one ticker using the same scanner logic.")
        ticker = st.text_input("Ticker", value="", label_visibility="collapsed").strip().upper()
        if st.button("Analyze Ticker", use_container_width=True):
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
                breakdown = scan_symbol_ib(
                    ib,
                    ticker,
                    bool(cfg["strategy"].get("use_rvol_score", False)),
                    str(cfg["strategy"].get("active_strategy", "pmb")),
                    int(cfg["strategy"].get("orb_minutes", 15)),
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

elif selected_page == "💼 Positions":
    start_telegram_decision_worker()
    st.subheader("Positions")
    st.caption("The dashboard starts the trading engine automatically. Order placement still follows the automation and safety settings.")

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

    st.markdown("### Active Positions")
    active_positions = read_active_positions()
    if active_positions:
        st.dataframe(pd.DataFrame(active_positions), use_container_width=True)
    else:
        st.info("No open positions. Engine is waiting for a valid signal.")

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
        try:
            ib_connected_now = live_ibkr_ping(ib_cfg)
            ib = connect_ib(ib_cfg) if ib_connected_now else None
            processed = process_telegram_order_callbacks(ib, ib_cfg, tg_cfg, orders_unlocked_from_config(cfg) and ib_connected_now)
            if processed:
                st.success(f"Processed {processed} Telegram decision(s).")
        except Exception as e:
            if "Read timed out" not in str(e):
                st.warning(f"Telegram decision auto-check failed: {e}")

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

        def approval_status_label(order: dict) -> str:
            status = str(order.get("status", "unknown")).lower()
            symbol = order.get("symbol", "N/A")
            signal = order.get("signal", "N/A")
            if status in ["pending", "sent"]:
                return f"⏳ {symbol} {signal} awaiting Telegram decision"
            if status == "test_confirmed":
                return f"✅ {symbol} test approved from Telegram"
            if status == "submitted":
                return f"✅ {symbol} {signal} approved and submitted"
            if status == "rejected":
                return f"❌ {symbol} {signal} rejected from Telegram"
            if status == "failed":
                return f"⚠️ {symbol} {signal} approval failed"
            return f"{symbol} {signal}: {status}"

        action_cols = st.columns(3)
        with action_cols[0]:
            if st.button("Send Manual Order Approval", use_container_width=True):
                if not manual_symbol or not manual_expiry or manual_strike <= 0:
                    st.error("Enter symbol, expiry, and strike before sending approval.")
                elif manual_order_type == "LIMIT" and manual_limit <= 0:
                    st.error("Limit orders need a limit price greater than zero.")
                else:
                    option_label = f"{manual_symbol} {manual_expiry} {manual_strike:g} {manual_signal}"
                    pending = {
                        "id": make_approval_id(manual_symbol, manual_signal),
                        "status": "pending",
                        "created_at": datetime.now(EASTERN).isoformat(),
                        "symbol": manual_symbol,
                        "signal": manual_signal,
                        "option": option_label,
                        "expiry": manual_expiry,
                        "strike": float(manual_strike),
                        "type": manual_signal,
                        "quantity": int(manual_qty),
                        "mid": float(manual_mid or manual_limit or 0),
                        "estimated_cost": round(float(manual_qty) * float(manual_mid or manual_limit or 0) * 100, 2),
                        "order_type": manual_order_type,
                        "limit_price": float(manual_limit) if manual_order_type == "LIMIT" else None,
                        "account_mode": mode,
                        "score": "MANUAL",
                        "grade": "Manual",
                        "setup_quality": "Manual order",
                        "rank_score": 0,
                        "reasons": "Manual order entered from Positions tab.",
                        "raw_signal": {"Symbol": manual_symbol, "Signal": manual_signal},
                        "option_data": {"Option": option_label, "Expiry": manual_expiry, "Strike": float(manual_strike), "Type": manual_signal, "Mid": float(manual_mid or manual_limit or 0)},
                    }
                    orders = read_pending_approvals()
                    orders.append(pending)
                    write_pending_approvals(orders)
                    try:
                        sent = send_order_approval_message(tg_cfg, pending)
                        if sent:
                            st.success(f"Manual approval sent: {pending['id']}")
                        else:
                            st.error("Telegram token/chat ID missing.")
                    except Exception as e:
                        st.error(f"Telegram approval failed: {e}")

        with action_cols[1]:
            if st.button("Send Telegram Test", use_container_width=True):
                test_symbol = manual_symbol or "TEST"
                test_order = {
                    "id": make_approval_id(test_symbol, "TEST"),
                    "status": "pending",
                    "created_at": datetime.now(EASTERN).isoformat(),
                    "symbol": test_symbol,
                    "signal": "TEST",
                    "option": "Telegram approval test",
                    "expiry": manual_expiry or datetime.now(EASTERN).strftime("%Y%m%d"),
                    "strike": float(manual_strike or 1),
                    "type": "CALL",
                    "quantity": 1,
                    "mid": 0,
                    "estimated_cost": 0,
                    "order_type": "TEST",
                    "limit_price": None,
                    "account_mode": mode,
                    "score": "TEST",
                    "grade": "Test",
                    "setup_quality": "Telegram button test",
                    "rank_score": 0,
                    "reasons": "This is a Telegram Confirm/Reject test. Confirm will not submit an IBKR order.",
                    "test_order": True,
                    "raw_signal": {"Symbol": test_symbol, "Signal": "TEST"},
                    "option_data": {},
                }
                orders = read_pending_approvals()
                orders.append(test_order)
                write_pending_approvals(orders)
                try:
                    sent = send_order_approval_message(tg_cfg, test_order)
                    if sent:
                        st.success(f"Telegram test sent: {test_order['id']}")
                    else:
                        st.error("Telegram token/chat ID missing.")
                except Exception as e:
                    st.error(f"Telegram test failed: {e}")

        with action_cols[2]:
            st.caption("Telegram Decision Status")
            approvals = read_pending_approvals()
            latest_approval = approvals[-1] if approvals else {}
            if latest_approval:
                st.markdown(f"**{approval_status_label(latest_approval)}**")
                st.caption(f"Order ID: {latest_approval.get('id', 'N/A')}")
            else:
                st.info("No Telegram approvals sent yet.")

            recent = approvals[-5:]
            if recent:
                st.dataframe(
                    pd.DataFrame([
                        {
                            "symbol": o.get("symbol"),
                            "side": o.get("signal"),
                            "status": o.get("status"),
                            "created": o.get("created_at"),
                        }
                        for o in reversed(recent)
                    ]),
                    use_container_width=True,
                    hide_index=True,
                )

    if st.button("Manage Open Positions Now", use_container_width=True):
        try:
            if not read_active_positions():
                st.info("No active positions to manage.")
            else:
                ib = connect_ib(ib_cfg)
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

elif selected_page == "📊 Performance & Trade Journal":
    st.subheader("Performance & Trade Journal")

    def _load_trade_log_df() -> pd.DataFrame:
        if not os.path.exists(TRADE_LOG_FILE):
            return pd.DataFrame()
        try:
            df = pd.read_csv(TRADE_LOG_FILE)
            if df.empty or "timestamp" not in df.columns:
                return pd.DataFrame()
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
            if "realized_pnl" in df.columns:
                df["realized_pnl"] = pd.to_numeric(df["realized_pnl"], errors="coerce").fillna(0.0)
            else:
                df["realized_pnl"] = 0.0
            return df
        except Exception:
            return pd.DataFrame()

    trade_log = _load_trade_log_df()
    replay_df = load_trade_replay() if "load_trade_replay" in globals() else pd.DataFrame()

    if trade_log.empty and replay_df.empty:
        render_empty_performance_dashboard()
    else:
        now_et = datetime.now(EASTERN)
        today = now_et.date()
        period = st.radio("Performance Period", ["Today", "This Week", "This Month", "All Time", "Custom Range"], horizontal=True)
        base_dates = trade_log["timestamp"].dt.date if not trade_log.empty else replay_df["timestamp"].dt.date
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

        entries = filtered[filtered.get("event", pd.Series(dtype=str)).astype(str) == "ENTRY"].copy() if not filtered.empty else pd.DataFrame()
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

        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("Net P/L", f"${realized_pnl:,.2f}")
        m2.metric("Win Rate", f"{win_rate}%")
        m3.metric("Entries", total_entries)
        m4.metric("Closed", total_exits)
        m5.metric("Avg Win / Loss", f"${avg_win:,.0f} / ${avg_loss:,.0f}")
        m6.metric("Profit Factor", profit_factor)

        if not exits.empty:
            exits = exits.sort_values("timestamp")
            exits["cumulative_pnl"] = exits["realized_pnl"].cumsum()
            eq_fig = go.Figure()
            eq_fig.add_trace(go.Scatter(x=exits["timestamp"], y=exits["cumulative_pnl"], mode="lines+markers", name="Cumulative P/L"))
            eq_fig.update_layout(height=380, title=f"Equity Curve ({period})", xaxis_title="Time", yaxis_title="Realized P/L USD")
            st.plotly_chart(eq_fig, use_container_width=True)

            chart_cols = st.columns(2)
            daily = exits.copy()
            daily["date"] = daily["timestamp"].dt.date
            daily_pnl = daily.groupby("date", as_index=False)["realized_pnl"].sum()
            daily_fig = go.Figure()
            daily_fig.add_trace(go.Bar(x=daily_pnl["date"].astype(str), y=daily_pnl["realized_pnl"], name="Daily P/L"))
            daily_fig.update_layout(height=360, title="Daily P/L", xaxis_title="Date", yaxis_title="P/L USD")
            chart_cols[0].plotly_chart(daily_fig, use_container_width=True)

            if "symbol" in exits.columns:
                sym_pnl = exits.groupby("symbol", as_index=False)["realized_pnl"].sum().sort_values("realized_pnl", ascending=False)
                sym_fig = go.Figure()
                sym_fig.add_trace(go.Bar(x=sym_pnl["symbol"].astype(str), y=sym_pnl["realized_pnl"], name="Symbol P/L"))
                sym_fig.update_layout(height=360, title="P/L by Symbol", xaxis_title="Symbol", yaxis_title="P/L USD")
                chart_cols[1].plotly_chart(sym_fig, use_container_width=True)
        else:
            st.info("No closed trades match the selected filters yet.")

        st.markdown("### Trade Replay Journal")
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
