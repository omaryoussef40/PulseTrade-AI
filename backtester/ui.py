from __future__ import annotations

"""Streamlit UI for PulseTrade AI Strategy Lab.

The dashboard imports only render_strategy_lab_tab(). All workflow logic lives in
backtester.controller so Streamlit reruns do not own the state of the backtest.
"""

from datetime import datetime
import time as _time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .data import YFINANCE_AVAILABLE, YahooDataClient

try:
    from market_data import create_market_data_provider, available_market_data_sources
except Exception:  # pragma: no cover
    create_market_data_provider = None
    def available_market_data_sources():
        return ["Yahoo"]
from .controller import StrategyLabController, StrategyLabSettings
from .metrics import daily_pnl, symbol_pnl, monthly_pnl


def _parse_symbols(text: str, max_symbols: int) -> list[str]:
    return [s.strip().upper() for s in str(text).replace("\n", ",").split(",") if s.strip()][: int(max_symbols)]


def _chart_equity(trades: pd.DataFrame):
    fig = go.Figure()
    if trades is not None and not trades.empty and "equity" in trades.columns:
        fig.add_trace(go.Scatter(
            x=pd.to_datetime(trades.get("exit_time", trades.index), errors="coerce"),
            y=pd.to_numeric(trades["equity"], errors="coerce"),
            mode="lines+markers",
            name="Equity",
        ))
    fig.update_layout(height=390, title="Strategy Lab Equity Curve", xaxis_title="Exit Time", yaxis_title="Equity USD")
    return fig


def render_strategy_lab_tab(config: dict, default_symbols: list[str]):
    st.subheader("Research Lab")
    st.caption("Run selected strategies on historical candles and compare the results before paper/live trading.")

    strategy = config.get("strategy", {})
    risk = config.get("risk", {})

    default_max_symbols = min(5, max(1, len(default_symbols))) if default_symbols else 5

    with st.expander("🎯 Strategy Selection", expanded=True):
        s1, s2, s3, s4 = st.columns(4)
        with s1:
            use_pmb = st.checkbox("PMB", value=True, key="sl_strategy_pmb")
        with s2:
            use_brt = st.checkbox("BRT", value=False, key="sl_strategy_brt")
        with s3:
            use_pullback = st.checkbox("Pullback", value=False, key="sl_strategy_pullback")
        with s4:
            use_gap = st.checkbox("Gap", value=False, key="sl_strategy_gap")

        selected_strategies = []
        if use_pmb:
            selected_strategies.append("PMB")
        if use_brt:
            selected_strategies.append("BRT")
        if use_pullback:
            selected_strategies.append("PULLBACK")
        if use_gap:
            selected_strategies.append("GAP")
        if not selected_strategies:
            st.warning("Select at least one strategy. PMB is selected by default for a real backtest.")
            selected_strategies = ["PMB"]
        if any(x in selected_strategies for x in ["BRT", "PULLBACK", "GAP"]):
            st.info("PMB is implemented now. BRT, Pullback, and Gap are clean placeholders and will show 0 trades until their exact rules are coded.")
        st.caption("PMB v2 uses Score + Grade. Confidence is kept only as a compatibility column and no longer blocks trades.")

    with st.expander("📈 Symbols", expanded=True):
        current_max_symbols = int(st.session_state.get("sl_max_symbols", default_max_symbols))
        default_text = ", ".join(default_symbols[:current_max_symbols]) if default_symbols else "SPY, QQQ, NVDA, TSLA, AMD"
        symbols_text = st.text_area("Symbols", value=default_text, height=80, key="sl_symbols")
        symbols = _parse_symbols(symbols_text, current_max_symbols)
        st.caption(f"Using up to {current_max_symbols} symbol(s): {', '.join(symbols) if symbols else 'none'}")

    with st.expander("⚙️ Backtest Setup", expanded=False):
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            period = st.selectbox("Backtest Period", ["30d", "60d", "90d"], index=0, key="sl_period")
        with c2:
            interval = st.selectbox("Candle interval", ["5m", "15m", "30m", "60m"], index=0, key="sl_interval")
        with c3:
            max_symbols = st.number_input("Max symbols", min_value=1, max_value=20, value=current_max_symbols, step=1, key="sl_max_symbols")
        with c4:
            force_refresh = st.checkbox("Force data refresh", value=False, key="sl_force_refresh")

        source_options = available_market_data_sources() if callable(available_market_data_sources) else ["IBKR", "Yahoo"]
        default_source_index = 0 if "IBKR" in source_options else 0
        data_source = st.radio("Historical Data Source", source_options, index=default_source_index, horizontal=True, key="sl_data_source")
        if str(data_source).upper() == "IBKR":
            st.caption("Strategy Lab will use IBKR historical candles through market_data.py. Keep TWS/IB Gateway open.")
        elif not YFINANCE_AVAILABLE:
            st.error("Yahoo selected but yfinance is not installed.")
            st.code("pip install yfinance pyarrow", language="bash")
            return

    # Re-apply Max Symbols after Backtest Setup renders. If Max Symbols was changed,
    # Streamlit will rerun and the Symbols accordion will reflect the new value.
    max_symbols = int(st.session_state.get("sl_max_symbols", default_max_symbols))
    symbols = _parse_symbols(st.session_state.get("sl_symbols", ""), max_symbols)
    if str(data_source).upper() == "YAHOO" and period == "90d" and interval == "5m":
        st.caption("Note: Yahoo may limit 5-minute history. If 90d returns no data, switch to 15m or use 60d.")

    try:
        provider = create_market_data_provider(data_source, config) if create_market_data_provider else YahooDataClient()
    except Exception as exc:
        st.error(f"Could not initialize {data_source} market-data provider: {exc}")
        return
    controller = StrategyLabController(data_provider=provider)

    with st.expander("🔁 Replay & Scanner Rules", expanded=False):
        r1, r2, r3, r4 = st.columns(4)
        with r1:
            orb_minutes = st.number_input("ORB window minutes", min_value=5, max_value=90, value=15, step=5, key="sl_orb")
        with r2:
            first_signal_minutes = st.number_input("First scanner minute", min_value=5, max_value=120, value=20, step=5, key="sl_first_signal")
        with r3:
            min_session_bars = st.number_input("Minimum session bars", min_value=2, max_value=30, value=7, step=1, key="sl_min_bars")
        with r4:
            visual_updates = st.checkbox("Show live replay progress", value=True, key="sl_visual")

    with st.expander("💰 Option Simulation Rules", expanded=False):
        s1, s2, s3, s4 = st.columns(4)
        with s1:
            starting_capital = st.number_input("Capital USD", min_value=100.0, value=float(risk.get("account_size", 1000)), step=100.0, key="sl_capital")
            max_trades_per_day = st.number_input("Max trades/day", min_value=1, max_value=10, value=int(risk.get("max_trades_per_day", 2)), step=1, key="sl_max_trades")
        with s2:
            sizing_model = st.selectbox("Position sizing", ["% of Equity", "Fixed Dollar"], index=0, key="sl_sizing_model")
            if sizing_model == "% of Equity":
                allocation_pct = st.number_input("Position allocation %", min_value=1.0, max_value=100.0, value=20.0, step=1.0, key="sl_alloc_pct")
                daily_exposure_pct = st.number_input("Max daily exposure %", min_value=1.0, max_value=100.0, value=40.0, step=1.0, key="sl_daily_exp_pct")
                max_spend = float(starting_capital) * float(allocation_pct) / 100.0
                max_daily_capital = float(starting_capital) * float(daily_exposure_pct) / 100.0
                st.caption("Compounds automatically: each trade uses a % of current equity, not a fixed dollar amount.")
            else:
                allocation_pct = 0.0
                daily_exposure_pct = 0.0
                max_spend = st.number_input("Max spend/trade USD", min_value=50.0, value=float(risk.get("max_spend_per_trade", 250)), step=50.0, key="sl_spend")
                default_daily_capital = max(float(risk.get("max_daily_capital", 0) or 0), float(risk.get("account_size", 1000)), float(max_spend))
                max_daily_capital = st.number_input("Max daily capital USD", min_value=50.0, value=default_daily_capital, step=50.0, key="sl_daily_cap")
        with s3:
            stop_loss = st.number_input("Stop loss %", min_value=1.0, max_value=90.0, value=float(risk.get("stop_loss_pct", 20.0)), step=1.0, key="sl_stop")
            take_profit = st.number_input("Take profit %", min_value=1.0, max_value=300.0, value=float(risk.get("take_profit_pct", 30.0)), step=1.0, key="sl_tp")
        with s4:
            premium_pct_ui = st.number_input("Entry premium % of stock", min_value=0.1, max_value=10.0, value=0.25, step=0.05, key="sl_premium_v092")
            slippage_pct = st.number_input("Slippage %", min_value=0.0, max_value=20.0, value=2.0, step=0.5, key="sl_slippage")
        allow_same_symbol = st.checkbox("Allow same symbol more than once per day", value=False, key="sl_same_symbol")

    settings = StrategyLabSettings(
        symbols=symbols,
        period=period,
        interval=interval,
        force_refresh=bool(force_refresh),
        orb_minutes=int(orb_minutes),
        first_signal_minutes=int(first_signal_minutes),
        min_session_bars=int(min_session_bars),
        min_score=float(strategy.get("min_score", 70)),
        min_confidence=float(strategy.get("min_confidence", 75)),
        min_rvol=float(strategy.get("min_rvol", 1.5)),
        min_atr=float(strategy.get("min_atr", 0.3)),
        use_rvol_filter=bool(strategy.get("use_rvol_filter", False)),
        use_rvol_score=bool(strategy.get("use_rvol_score", False)),
        starting_capital=float(starting_capital),
        max_trades_per_day=int(max_trades_per_day),
        sizing_method="percent_equity" if sizing_model == "% of Equity" else "fixed_dollar",
        position_allocation_pct=float(allocation_pct),
        max_daily_exposure_pct=float(daily_exposure_pct),
        max_spend_per_trade=float(max_spend),
        max_daily_capital=float(max_daily_capital),
        max_contracts=0,  # 0 = unlimited; Strategy Lab buys as many contracts as budget allows.
        stop_loss_pct=float(stop_loss),
        take_profit_pct=float(take_profit),
        breakeven_trigger_pct=float(risk.get("breakeven_trigger_pct", 15.0)),
        trailing_trigger_pct=float(risk.get("trailing_trigger_pct", 25.0)),
        trailing_stop_pct=float(risk.get("trailing_stop_pct", 10.0)),
        force_exit_hour=int(risk.get("force_exit_hour", 15)),
        force_exit_minute=int(risk.get("force_exit_minute", 55)),
        premium_pct=float(premium_pct_ui) / 100.0,
        slippage_pct=float(slippage_pct),
        allow_same_symbol_same_day=bool(allow_same_symbol),
        selected_strategies=tuple(selected_strategies),
        data_source=str(data_source),
    )

    st.markdown("### Position Sizing Preview")
    preview_underlying = 500.0
    preview_premium = max(0.35, round(preview_underlying * (float(premium_pct_ui) / 100.0) * (1 + float(slippage_pct) / 100.0), 2))
    preview_contract_cost = preview_premium * 100.0 + 0.65
    if sizing_model == "% of Equity":
        preview_budget = min(float(starting_capital) * float(allocation_pct) / 100.0, float(starting_capital) * float(daily_exposure_pct) / 100.0, float(starting_capital))
        preview_label = f"{allocation_pct:.0f}% of equity"
    else:
        preview_budget = min(float(max_spend), float(max_daily_capital), float(starting_capital))
        preview_label = "fixed dollar"
    preview_contracts = int(preview_budget // preview_contract_cost) if preview_contract_cost > 0 else 0
    preview_cost = round(preview_contracts * preview_contract_cost, 2)
    pc1, pc2, pc3, pc4, pc5, pc6 = st.columns(6)
    pc1.metric("Sizing", preview_label)
    pc2.metric("Example Premium", f"${preview_premium:,.2f}")
    pc3.metric("Cost / Contract", f"${preview_contract_cost:,.2f}")
    pc4.metric("Contracts", preview_contracts)
    pc5.metric("Trade Cost", f"${preview_cost:,.2f}")
    pc6.metric("BP After Entry", f"${max(float(starting_capital) - preview_cost, 0):,.2f}")
    if sizing_model == "% of Equity":
        if float(daily_exposure_pct) < float(allocation_pct):
            st.warning("Max Daily Exposure % is lower than Position Allocation %. The simulator will size trades using the smaller daily exposure cap.")
    else:
        if float(max_daily_capital) < float(max_spend):
            st.warning("Max Daily Capital is lower than Max Spend/Trade. The simulator will size trades using the smaller daily-capital amount.")
        if float(max_spend) > float(starting_capital):
            st.warning("Max Spend/Trade is greater than starting capital. Buying power will cap the actual position size.")

    st.markdown("### 2. Run Research Lab")
    a, b, c = st.columns([2, 1, 1])
    run_clicked = a.button("▶ Run Backtest", use_container_width=True, key="sl_run")
    clear_clicked = b.button("Clear Results", use_container_width=True, key="sl_clear")
    show_cache = c.button("Show Cache", use_container_width=True, key="sl_cache")

    if clear_clicked:
        controller.clear_results()
        for k in ["sl_last_result"]:
            st.session_state.pop(k, None)
        st.success("Strategy Lab result files cleared.")

    if show_cache:
        info = controller.cache_info()
        st.dataframe(info, use_container_width=True, hide_index=True) if not info.empty else st.info(f"No cache info available for {data_source}.")

    if run_clicked:
        if not symbols:
            st.warning("Add at least one symbol.")
            return
        st.session_state["bt_job_running"] = True
        progress = st.progress(0)
        status = st.empty()
        live_box = st.empty()

        def progress_callback(idx, total, row, signal_count):
            progress.progress(min(1.0, idx / max(total, 1)))
            if visual_updates:
                status.info(f"Replaying {idx:,} / {total:,} candles | Signals: {signal_count:,} | {row.get('symbol')} | {row.get('timestamp')}")
                if idx % max(1, total // 50) == 0 or idx == total:
                    live_box.dataframe(pd.DataFrame([row]), use_container_width=True, hide_index=True)

        try:
            result = controller.run(settings, progress_callback=progress_callback)
            st.session_state["sl_last_result"] = result
            status.success("Research Lab backtest complete. Results saved to backtester/exports.")
        except Exception as exc:
            status.error(f"Strategy Lab failed: {exc}")
        finally:
            st.session_state["bt_job_running"] = False

    result = st.session_state.get("sl_last_result") or controller.load_last_result()
    replay = result.get("replay", pd.DataFrame())
    signals = result.get("signals", pd.DataFrame())
    trades = result.get("trades", pd.DataFrame())
    decisions = result.get("decisions", pd.DataFrame())
    comparison = result.get("comparison", pd.DataFrame())
    sessions = result.get("sessions", pd.DataFrame())
    metrics = result.get("metrics", {}) or {}
    meta = result.get("meta", {}) or {}

    if isinstance(replay, pd.DataFrame) and not replay.empty:
        st.markdown("### 3. Last Backtest Result")
        st.caption(f"Completed: {meta.get('completed_at', 'N/A')} | Source: {meta.get('data_source', meta.get('provider', 'N/A'))} | Symbols: {', '.join(meta.get('symbols', []))} | Period: {meta.get('period', 'N/A')} | Interval: {meta.get('interval', 'N/A')}")
        m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
        m1.metric("Replay Events", f"{len(replay):,}")
        m2.metric("Signals", f"{len(signals):,}" if isinstance(signals, pd.DataFrame) else "0")
        m3.metric("Trades", int(metrics.get("total_trades", len(trades) if isinstance(trades, pd.DataFrame) else 0)))
        m4.metric("Rejected", int(metrics.get("rejected_signals", 0)))
        m5.metric("Net P/L", f"${float(metrics.get('net_pnl', 0)):,.2f}")
        m6.metric("Win Rate", f"{float(metrics.get('win_rate', 0)):,.1f}%")
        m7.metric("Profit Factor", metrics.get("profit_factor", 0))

        st.markdown("### Strategy Comparison")
        if isinstance(comparison, pd.DataFrame) and not comparison.empty:
            st.dataframe(comparison, use_container_width=True, hide_index=True)
            st.download_button("Download strategy comparison", comparison.to_csv(index=False), "strategy_lab_comparison.csv", "text/csv")
        else:
            st.info("No comparison table saved yet.")

        if isinstance(trades, pd.DataFrame) and not trades.empty:
            st.plotly_chart(_chart_equity(trades), use_container_width=True)
            cc1, cc2 = st.columns(2)
            with cc1:
                d = daily_pnl(trades)
                fig = go.Figure()
                fig.add_trace(go.Bar(x=d["date"], y=d["realized_pnl"], name="Daily P/L"))
                fig.update_layout(height=330, title="Daily P/L", xaxis_title="Date", yaxis_title="P/L USD")
                st.plotly_chart(fig, use_container_width=True)
            with cc2:
                sp = symbol_pnl(trades)
                fig = go.Figure()
                fig.add_trace(go.Bar(x=sp["symbol"], y=sp["realized_pnl"], name="Symbol P/L"))
                fig.update_layout(height=330, title="Symbol P/L", xaxis_title="Symbol", yaxis_title="P/L USD")
                st.plotly_chart(fig, use_container_width=True)

        tab_signals, tab_decisions, tab_trades, tab_replay, tab_sessions = st.tabs(["Signals", "Execution Decisions", "Simulated Trades", "Replay Log", "Sessions"])
        with tab_signals:
            st.dataframe(signals.tail(500), use_container_width=True, hide_index=True) if isinstance(signals, pd.DataFrame) and not signals.empty else st.info("No scanner signals passed the current filters.")
            if isinstance(signals, pd.DataFrame) and not signals.empty:
                st.download_button("Download signals", signals.to_csv(index=False), "strategy_lab_signals.csv", "text/csv")
        with tab_decisions:
            if isinstance(decisions, pd.DataFrame) and not decisions.empty:
                st.caption("Every scanner signal ends here as TRADED, SKIPPED, or REJECTED. This is the execution audit trail.")
                dcols = [c for c in ["decision_no", "timestamp", "strategy", "symbol", "signal", "score", "grade", "status", "stage", "reason", "entry_premium", "contract_cost_with_commission", "quantity", "sizing_method", "position_allocation_pct", "max_daily_exposure_pct", "buying_power_before", "buying_power_after_entry", "buying_power_after_exit", "max_spend_per_trade", "max_daily_capital", "estimated_cost", "exit_credit", "account_equity", "underlying_move_pct", "option_return_pct", "pricing_model", "raw_delta_return_pct", "model_option_return_pct", "theta_decay_pct", "realized_pnl"] if c in decisions.columns]
                st.dataframe(decisions[dcols].tail(500), use_container_width=True, hide_index=True)
                st.download_button("Download execution decisions", decisions.to_csv(index=False), "strategy_lab_signal_decisions.csv", "text/csv")
            else:
                st.info("No execution decision log yet.")

        with tab_trades:
            if isinstance(trades, pd.DataFrame) and not trades.empty:
                key_cols = [c for c in ["trade_no", "entry_time", "exit_time", "date", "strategy", "symbol", "signal", "score", "grade", "entry_underlying", "exit_underlying", "underlying_move_pct", "entry_premium", "exit_premium", "option_return_pct", "pricing_model", "quantity", "estimated_cost", "exit_credit", "buying_power_before", "buying_power_after_entry", "buying_power_after_exit", "account_equity", "realized_pnl", "return_pct", "hold_minutes", "exit_reason", "reasons"] if c in trades.columns]
                st.dataframe(trades[key_cols].tail(500), use_container_width=True, hide_index=True)
                st.download_button("Download trades", trades.to_csv(index=False), "strategy_lab_trades.csv", "text/csv")
            else:
                st.info("No simulated trades. Open Execution Decisions to see exactly why each signal was rejected or skipped.")
        with tab_replay:
            st.dataframe(replay.tail(500), use_container_width=True, hide_index=True)
            st.download_button("Download replay log", replay.to_csv(index=False), "strategy_lab_replay.csv", "text/csv")
        with tab_sessions:
            st.dataframe(sessions, use_container_width=True, hide_index=True) if isinstance(sessions, pd.DataFrame) and not sessions.empty else st.info("No sessions saved.")
    else:
        st.info("Run a full backtest to generate Strategy Lab results.")
