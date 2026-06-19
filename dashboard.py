# dashboard.py
# Streamlit dashboard for AutoTrader. The dashboard edits config.json; engine.py reads the same config.

from __future__ import annotations

import os
import traceback
from datetime import datetime, timedelta, time as dtime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from bot_core import *
from engine import run_cycle

st.set_page_config(page_title=APP_NAME, layout="wide")
st.title(APP_NAME)
st.caption("Production dashboard + shared config + independent engine")

st.markdown("""
<style>
    div[data-testid="stMetricValue"] { font-size: 1.35rem !important; white-space: nowrap !important; }
    div[data-testid="stMetricLabel"] { font-size: 0.82rem !important; }
    .status-card, .setup-card {
        border: 1px solid rgba(250,250,250,0.14);
        border-radius: 14px;
        padding: 14px 16px;
        background: rgba(255,255,255,0.035);
        min-height: 96px;
        margin-bottom: 10px;
    }
    .setup-card-call { border-left: 5px solid #22c55e; }
    .setup-card-put { border-left: 5px solid #ef4444; }
    .status-label, .setup-label { font-size: 0.78rem; color: rgba(250,250,250,0.68); margin-bottom: 6px; }
    .status-value { font-size: 1.22rem; font-weight: 700; line-height: 1.15; }
    .setup-symbol { font-size: 1.40rem; font-weight: 800; margin-bottom: 2px; }
    .setup-badge { font-size: 0.82rem; font-weight: 700; border-radius: 999px; padding: 4px 9px; display: inline-block; margin-bottom: 8px; }
    .badge-call { color: #22c55e; background: rgba(34,197,94,0.12); }
    .badge-put { color: #ef4444; background: rgba(239,68,68,0.12); }
    .small-muted { color: rgba(250,250,250,0.65); font-size: 0.82rem; }
</style>
""", unsafe_allow_html=True)


cfg = load_config()


def status_card(label: str, value: str):
    st.markdown(f"""
    <div class='status-card'>
        <div class='status-label'>{label}</div>
        <div class='status-value'>{value}</div>
    </div>
    """, unsafe_allow_html=True)


def setup_card(row: dict):
    signal = str(row.get("Signal", "WAIT"))
    card_cls = "setup-card-call" if signal == "CALL" else "setup-card-put" if signal == "PUT" else ""
    badge_cls = "badge-call" if signal == "CALL" else "badge-put" if signal == "PUT" else ""
    reasons = str(row.get("Reasons", "")).replace(" | ", " • ")
    quality = setup_quality_label(row.get("Score"), row.get("Confidence")) if "setup_quality_label" in globals() else "Setup"
    st.markdown(f"""
    <div class='setup-card {card_cls}'>
        <div class='setup-symbol'>{row.get('Symbol', '')}</div>
        <span class='setup-badge {badge_cls}'>{signal}</span>
        <div class='small-muted'>{quality}</div>
        <div style='margin-top:10px; display:grid; grid-template-columns:1fr 1fr; gap:8px;'>
            <div><div class='setup-label'>Score</div><b>{row.get('Score', 'N/A')}</b></div>
            <div><div class='setup-label'>Confidence</div><b>{row.get('Confidence', 'N/A')}</b></div>
            <div><div class='setup-label'>RVOL</div><b>{row.get('RVOL', 'N/A')}</b></div>
            <div><div class='setup-label'>ATR %</div><b>{row.get('ATR %', 'N/A')}</b></div>
        </div>
        <div class='small-muted' style='margin-top:10px;'>{reasons[:160]}</div>
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


def save_and_rerun(new_cfg: dict):
    save_config(new_cfg)


with st.sidebar:
    st.header("Control Panel")

    with st.expander("Automation Safety", expanded=False):
        cfg["account_mode"] = st.radio("Trading account mode", ["Simulation", "Paper", "Live"], index=["Simulation", "Paper", "Live"].index(cfg.get("account_mode", "Simulation")), horizontal=True)
        cfg["automation"]["enabled"] = st.checkbox("Enable engine automation", value=bool(cfg["automation"].get("enabled", False)))
        cfg["automation"]["place_orders"] = st.checkbox("Allow engine to place orders", value=bool(cfg["automation"].get("place_orders", False)))
        cfg["automation"]["confirm_order_risk"] = st.checkbox("I understand this can place IBKR orders", value=bool(cfg["automation"].get("confirm_order_risk", False)))
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
        r["account_size"] = st.number_input("Account size USD", value=int(r.get("account_size", 1000)), min_value=100, step=100)
        r["max_trades_per_day"] = st.number_input("Max trades per day", value=int(r.get("max_trades_per_day", 2)), min_value=1, max_value=10, step=1)
        s["top_n_tickers"] = st.number_input("Trade only top N tickers", value=int(s.get("top_n_tickers", 2)), min_value=1, max_value=10, step=1)
        r["max_spend_per_trade"] = st.number_input("Max amount spent per trade USD", value=int(r.get("max_spend_per_trade", 250)), min_value=50, step=50)
        r["max_daily_capital"] = st.number_input("Max daily capital used USD", value=int(r.get("max_daily_capital", 500)), min_value=50, step=50)
        r["max_contracts"] = st.number_input("Max contracts per trade", value=int(r.get("max_contracts", 2)), min_value=1, max_value=20, step=1)
        today_trade_count, today_deployed_capital = get_today_trade_stats()
        st.caption(f"Today: {today_trade_count} trades | ${today_deployed_capital:,.2f} deployed")

    with st.expander("Trade Management", expanded=False):
        r = cfg["risk"]
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
        wl_text = st.text_area("Symbols", value=", ".join(cfg.get("watchlist", WATCHLIST)), height=120)
        cfg["watchlist"] = [x.strip().upper() for x in wl_text.replace("\n", ",").split(",") if x.strip()]

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

# Light dashboard refresh so health updates while engine is running.
st_autorefresh(interval=30_000, key="dashboard_refresh")

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

health = read_health()

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

header_cols = st.columns(6)
with header_cols[0]:
    status_card("ENGINE", "🟢 Running" if health.get("engine_running") else "⚪ Unknown")
with header_cols[1]:
    status_card("IBKR", "🟢 Connected" if health.get("ib_connected") else "⚪ Unknown")
with header_cols[2]:
    status_card("MARKET", "🟢 Open" if is_market_open_now(cfg) else "🔴 Closed")
with header_cols[3]:
    status_card("MODE", cfg.get("account_mode", "Simulation"))
with header_cols[4]:
    status_card("ORDERS", operational_order_label(cfg, health))
with header_cols[5]:
    status_card("NEXT ACTION", next_action_label(cfg, health))
st.caption(f"Operational status: {operational_order_label(cfg, health).replace('🔵 ', '').replace('🟢 ', '').replace('🟡 ', '').replace('🔒 ', '')} | Config status: {trading_status_from_config(cfg)} | Last update: {health.get('updated_at', 'N/A')}")

connection_col1, connection_col2, connection_col3 = st.columns(3)
with connection_col1:
    if st.button("Connect to IBKR"):
        try:
            ib = connect_ib(ib_cfg)
            st.success(f"Connected: {ib.isConnected()}")
        except Exception as e:
            st.error(f"IBKR connection failed: {e}")
with connection_col2:
    if st.button("Test Telegram"):
        ok = send_telegram_message(tg_cfg, "AutoTrader Telegram test message.")
        st.success("Telegram sent") if ok else st.error("Telegram failed")
with connection_col3:
    if st.button("Run One Engine Cycle Now"):
        try:
            run_cycle()
            st.success("Engine cycle completed. Check logs/health below.")
        except Exception as e:
            st.error(f"Engine cycle failed: {e}")
            st.caption("Technical details are hidden in the dashboard. Check the terminal/log files if needed.")

tab1, tab2, tab3, tab4, tab5 = st.tabs(["📈 Scanner", "🔍 Breakdown", "💼 Positions", "📊 Performance", "📝 Logs"])

with tab1:
    st.subheader("Scanner")
    st.caption("Scan the full watchlist, rank the best setups, and review option ideas. Scanner requires IBKR / ib-insync connection.")
    if st.button("▶ Run IBKR Scanner", use_container_width=True):
        rows, option_rows = [], []
        progress = st.progress(0)
        try:
            ib = connect_ib(ib_cfg)
            for i, symbol in enumerate(symbols):
                try:
                    result = scan_symbol_ib(ib, symbol, bool(cfg["strategy"].get("use_rvol_score", False)))
                    if result:
                        rows.append(clean_for_table(result))
                        if is_top_candidate(
                            result,
                            float(cfg["strategy"].get("min_score", 70)),
                            float(cfg["strategy"].get("min_confidence", 75)),
                            float(cfg["strategy"].get("min_rvol", 1.5)),
                            float(cfg["strategy"].get("min_atr", 0.3)),
                            bool(cfg["strategy"].get("use_rvol_filter", False)),
                        ):
                            option = recommend_option_ib(ib, symbol, result["Signal"], result["Price"], int(cfg["strategy"].get("option_dte", 7)))
                            if option:
                                option_clean = {k: v for k, v in option.items() if k != "Contract"}
                                option_rows.append({"Symbol": symbol, "Signal": result["Signal"], "Score": result["Score"], "Confidence": result["Confidence"], **option_clean})
                except Exception as e:
                    st.warning(f"{symbol}: {e}")
                progress.progress((i + 1) / max(len(symbols), 1))

            stock_df = pd.DataFrame(rows)
            if not stock_df.empty:
                stock_df = stock_df.sort_values(["Score", "Confidence", "RVOL", "ATR %"], ascending=[False, False, False, False])

            option_df = pd.DataFrame(option_rows)
            if not option_df.empty:
                option_df = option_df.sort_values(["Score", "Option Score"], ascending=[False, False])

            st.markdown("### Top Opportunities")
            card_source = option_df if not option_df.empty else stock_df
            if card_source.empty:
                st.info("No candidates passed your filters.")
            else:
                card_cols = st.columns(min(3, len(card_source)))
                for idx, (_, row) in enumerate(card_source.head(3).iterrows()):
                    with card_cols[idx % len(card_cols)]:
                        setup_card(row.to_dict())

            with st.expander("Stock Results", expanded=True):
                st.dataframe(stock_df, use_container_width=True)
            with st.expander("Option Ideas", expanded=not option_df.empty):
                if not option_df.empty:
                    st.dataframe(option_df, use_container_width=True)
                    st.download_button("Download option ideas", option_df.to_csv(index=False), "option_ideas.csv", "text/csv")
                else:
                    st.info("No clean option contracts found for the filtered setups.")
        except Exception as e:
            clean_ui_error("Scanner failed", e)

with tab2:
    st.subheader("Ticker Breakdown")
    ticker = st.text_input("Ticker", value="").strip().upper()
    if st.button("Analyze Ticker"):
        try:
            ib = connect_ib(ib_cfg)
            breakdown = scan_symbol_ib(ib, ticker, bool(cfg["strategy"].get("use_rvol_score", False)))
            if not breakdown:
                st.error("No breakdown available.")
            else:
                left, right = st.columns([2, 1])
                with left:
                    st.plotly_chart(make_chart(ticker, breakdown), use_container_width=True)
                    st.dataframe(pd.DataFrame([clean_for_table(breakdown)]), use_container_width=True)
                with right:
                    st.metric("Signal", signal_badge(breakdown["Signal"]))
                    st.metric("Score", breakdown["Score"])
                    st.metric("Confidence", breakdown["Confidence"])
                    st.metric("RVOL", breakdown["RVOL"])
                    st.write("Why:")
                    for reason in breakdown["Reasons"].split(" | "):
                        st.write(f"✓ {reason}")
        except Exception as e:
            st.error(f"Analysis failed: {e}")
            st.caption("Technical details are hidden in the dashboard. Check the terminal/log files if needed.")

with tab3:
    st.subheader("Positions")
    st.caption("The dashboard can be closed. The engine keeps running only when `python engine.py` is running on the VPS.")

    mode = cfg.get("account_mode", "Simulation")
    trading_status = trading_status_from_config(cfg)
    orders_unlocked = orders_unlocked_from_config(cfg)
    readonly = bool(cfg.get("ib", {}).get("readonly", False))
    place_orders = bool(cfg.get("automation", {}).get("place_orders", False))
    risk_confirmed = bool(cfg.get("automation", {}).get("confirm_order_risk", False))
    automation_enabled = bool(cfg.get("automation", {}).get("enabled", False))

    st.markdown("### Order Safety")
    safety_cols = st.columns(4)
    with safety_cols[0]:
        status_card("Mode", mode)
    with safety_cols[1]:
        status_card("Trading Status", trading_status)
    with safety_cols[2]:
        status_card("Orders", operational_order_label(cfg, health))
    with safety_cols[3]:
        status_card("Automation", "✅ Enabled" if automation_enabled else "⏸ Off")

    safety_cols2 = st.columns(4)
    with safety_cols2[0]:
        status_card("Place Orders", "✅ Allowed" if place_orders else "❌ Disabled")
    with safety_cols2[1]:
        status_card("Risk Confirmed", "✅ Yes" if risk_confirmed else "❌ No")
    with safety_cols2[2]:
        status_card("Read-only", "✅ On" if readonly else "❌ Off")
    with safety_cols2[3]:
        status_card("IB Port", str(ib_cfg.port))

    if st.button("Manage Open Positions Now", use_container_width=True):
        try:
            ib = connect_ib(ib_cfg)
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
            st.dataframe(pd.DataFrame(events), use_container_width=True) if events else st.info("No active positions to manage.")
        except Exception as e:
            clean_ui_error("Position management failed", e)

    st.markdown("### Active Positions")
    active_positions = read_active_positions()
    if active_positions:
        st.dataframe(pd.DataFrame(active_positions), use_container_width=True)
    else:
        st.info("No open positions. Engine is waiting for a valid signal.")

    st.markdown("### Engine Health")
    health_now = read_health()
    if health_now:
        hcols = st.columns(4)
        hcols[0].metric("Engine", "Running" if health_now.get("engine_running") else "Unknown")
        hcols[1].metric("IBKR", "Connected" if health_now.get("ib_connected") else "Unknown")
        hcols[2].metric("Last Status", str(health_now.get("last_status", "N/A"))[:24])
        hcols[3].metric("Candidates", str(health_now.get("candidates", 0)))
        with st.expander("Health details"):
            st.json(health_now)
    else:
        st.info("No engine heartbeat yet. Start `python engine.py` to activate health monitoring.")

with tab4:
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

with tab5:
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
