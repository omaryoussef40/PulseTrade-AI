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
    from bot_core import WATCHLIST, save_config
except Exception:  # pragma: no cover
    WATCHLIST = [
        "SPY", "QQQ", "IWM", "NVDA", "AAPL", "MSFT", "META", "AMZN", "GOOGL", "TSLA", "AMD", "PLTR", "COIN", "MSTR", "AVGO", "SMCI", "MU", "ARM", "TSM", "MRVL", "JPM", "GS", "BAC", "NFLX", "UBER", "XOM", "COST", "RBLX", "HOOD", "SOFI", "RKLB", "HIMS", "CRWD"
    ]
    save_config = None

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


def _parse_extra_symbols(text: str) -> list[str]:
    return [s.strip().upper() for s in str(text).replace("\n", ",").split(",") if s.strip()]


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


def _save_strategy_lab_settings(config: dict, symbols: list[str], selected_symbols: list[str], selected_strategies: list[str], period: str, interval: str, max_symbols: int, force_refresh: bool, data_source: str, orb_minutes: int, first_signal_minutes: int, min_session_bars: int, visual_updates: bool, premium_pct_ui: float, slippage_pct: float, allow_same_symbol: bool) -> None:
    config.setdefault("strategy_lab", {})
    config["watchlist"] = list(selected_symbols)
    config["strategy_lab"] = {
        "symbols": list(selected_symbols),
        "selected_strategies": list(selected_strategies),
        "period": str(period),
        "interval": str(interval),
        "max_symbols": int(max_symbols),
        "force_refresh": bool(force_refresh),
        "data_source": str(data_source),
        "orb_minutes": int(orb_minutes),
        "first_signal_minutes": int(first_signal_minutes),
        "min_session_bars": int(min_session_bars),
        "visual_updates": bool(visual_updates),
        "premium_pct_ui": float(premium_pct_ui),
        "slippage_pct": float(slippage_pct),
        "allow_same_symbol": bool(allow_same_symbol),
    }
    if save_config is not None:
        save_config(config)


def render_strategy_lab_tab(config: dict, default_symbols: list[str]):
    st.subheader("Research Lab")
    st.caption("Run selected strategies on historical candles and compare the results before paper/live trading.")

    strategy = config.get("strategy", {})
    risk = config.get("risk", {})
    lab_cfg = config.get("strategy_lab", {})

    default_max_symbols = min(5, max(1, len(default_symbols))) if default_symbols else 5

    current_max_symbols = int(st.session_state.get("sl_max_symbols", int(lab_cfg.get("max_symbols", default_max_symbols))))
    saved_symbols = lab_cfg.get("symbols") or default_symbols
    default_text = ", ".join(saved_symbols) if saved_symbols else "SPY, QQQ, NVDA, TSLA, AMD"
    source_options = available_market_data_sources() if callable(available_market_data_sources) else ["IBKR", "Yahoo"]
    default_source = str(lab_cfg.get("data_source", "IBKR"))
    default_source_index = source_options.index(default_source) if default_source in source_options else 0
    saved_strategies = set(lab_cfg.get("selected_strategies", ["PMB"]))

    setting_cols = st.columns(5)
    with setting_cols[0]:
        with st.expander("🎯 Strategy", expanded=False):
            use_pmb = st.checkbox("PMB", value=("PMB" in saved_strategies), key="sl_strategy_pmb")
            use_brt = st.checkbox("BRT", value=("BRT" in saved_strategies), key="sl_strategy_brt")
            use_pullback = st.checkbox("Pullback", value=("PULLBACK" in saved_strategies), key="sl_strategy_pullback")
            use_gap = st.checkbox("Gap", value=("GAP" in saved_strategies), key="sl_strategy_gap")
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
                st.warning("Select at least one strategy.")
                selected_strategies = ["PMB"]
            if any(x in selected_strategies for x in ["BRT", "PULLBACK", "GAP"]):
                st.caption("PMB is implemented now. BRT, Pullback, and Gap are placeholders until their exact rules are coded.")
            st.caption("PMB v2 uses Score + Grade. Confidence is kept only as a compatibility column and no longer blocks trades.")

    with setting_cols[1]:
        with st.expander("📈 Symbols", expanded=False):
            saved_symbol_set = set(_parse_extra_symbols(default_text))
            selected_presets = st.multiselect(
                "Preset tickers",
                WATCHLIST,
                default=[ticker for ticker in WATCHLIST if ticker in saved_symbol_set],
                key="sl_symbol_presets",
            )
            extra_default = ", ".join([s for s in saved_symbol_set if s not in set(WATCHLIST)])
            extra_symbols_text = st.text_input("Add tickers", value=extra_default, key="sl_extra_symbols")
            selected_all = selected_presets + [s for s in _parse_extra_symbols(extra_symbols_text) if s not in selected_presets]
            st.session_state["sl_selected_all_symbols"] = selected_all
            symbols = selected_all[:current_max_symbols]
            st.caption(f"Using {len(symbols)} of {current_max_symbols}")

    with setting_cols[2]:
        with st.expander("⚙️ Backtest", expanded=False):
            period_options = ["1d", "7d", "30d", "60d", "90d"]
            interval_options = ["5m", "15m", "30m", "60m"]
            period = st.selectbox("Backtest Period", period_options, index=period_options.index(str(lab_cfg.get("period", "30d"))) if str(lab_cfg.get("period", "30d")) in period_options else 0, key="sl_period")
            interval = st.selectbox("Candle interval", interval_options, index=interval_options.index(str(lab_cfg.get("interval", "5m"))) if str(lab_cfg.get("interval", "5m")) in interval_options else 0, key="sl_interval")
            max_symbols = st.number_input("Max symbols", min_value=1, max_value=20, value=current_max_symbols, step=1, key="sl_max_symbols")
            force_refresh = st.checkbox("Force data refresh", value=bool(lab_cfg.get("force_refresh", False)), key="sl_force_refresh")
            data_source = st.selectbox("Data source", source_options, index=default_source_index, key="sl_data_source")

    # Re-apply Max Symbols after Backtest Setup renders. If Max Symbols was changed,
    # Streamlit will rerun and the Symbols accordion will reflect the new value.
    max_symbols = int(st.session_state.get("sl_max_symbols", default_max_symbols))
    selected_presets_state = list(st.session_state.get("sl_symbol_presets", []))
    selected_all_symbols = selected_presets_state + [s for s in _parse_extra_symbols(st.session_state.get("sl_extra_symbols", "")) if s not in selected_presets_state]
    symbols = selected_all_symbols[:max_symbols]
    if str(data_source).upper() == "IBKR":
        st.caption("Strategy Lab uses IBKR historical candles. Keep TWS/IB Gateway open.")
    elif not YFINANCE_AVAILABLE:
        st.error("Yahoo selected but yfinance is not installed.")
        st.code("pip install yfinance pyarrow", language="bash")
        return
    if str(data_source).upper() == "YAHOO" and period == "90d" and interval == "5m":
        st.caption("Note: Yahoo may limit 5-minute history. If 90d returns no data, switch to 15m or use 60d.")

    try:
        provider = create_market_data_provider(data_source, config) if create_market_data_provider else YahooDataClient()
    except Exception as exc:
        st.error(f"Could not initialize {data_source} market-data provider: {exc}")
        return
    controller = StrategyLabController(data_provider=provider)

    with setting_cols[3]:
        with st.expander("🔁 Scanner", expanded=False):
            orb_minutes = st.number_input("ORB window minutes", min_value=5, max_value=90, value=int(lab_cfg.get("orb_minutes", strategy.get("orb_minutes", 15))), step=5, key="sl_orb")
            first_signal_minutes = st.number_input("First scanner minute", min_value=5, max_value=120, value=int(lab_cfg.get("first_signal_minutes", strategy.get("first_signal_minutes", 20))), step=5, key="sl_first_signal")
            min_session_bars = st.number_input("Minimum session bars", min_value=2, max_value=30, value=int(lab_cfg.get("min_session_bars", 7)), step=1, key="sl_min_bars")
            visual_updates = st.checkbox("Show live replay progress", value=bool(lab_cfg.get("visual_updates", True)), key="sl_visual")

    with setting_cols[4]:
        with st.expander("💰 Options", expanded=False):
            starting_capital = st.number_input("Capital USD", min_value=100.0, value=float(risk.get("account_size", 1000)), step=100.0, key="sl_capital")
            max_trades_per_day = st.number_input("Max trades/day", min_value=1, max_value=10, value=int(risk.get("max_trades_per_day", 2)), step=1, key="sl_max_trades")
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
            stop_loss = st.number_input("Stop loss %", min_value=1.0, max_value=90.0, value=float(risk.get("stop_loss_pct", 20.0)), step=1.0, key="sl_stop")
            take_profit = st.number_input("Take profit %", min_value=1.0, max_value=300.0, value=float(risk.get("take_profit_pct", 30.0)), step=1.0, key="sl_tp")
            premium_pct_ui = st.number_input("Entry premium % of stock", min_value=0.1, max_value=10.0, value=float(lab_cfg.get("premium_pct_ui", 0.25)), step=0.05, key="sl_premium_v092")
            slippage_pct = st.number_input("Slippage %", min_value=0.0, max_value=20.0, value=float(lab_cfg.get("slippage_pct", 2.0)), step=0.5, key="sl_slippage")
            allow_same_symbol = st.checkbox("Allow same symbol more than once per day", value=bool(lab_cfg.get("allow_same_symbol", False)), key="sl_same_symbol")

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
    _save_strategy_lab_settings(config, symbols, selected_all_symbols, selected_strategies, period, interval, max_symbols, force_refresh, data_source, orb_minutes, first_signal_minutes, min_session_bars, visual_updates, premium_pct_ui, slippage_pct, allow_same_symbol)

    if st.button("Run Backtest", use_container_width=True, key="sl_run"):
        if not symbols:
            st.warning("Add at least one symbol.")
        else:
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
        st.markdown("### Last Backtest Result")
        st.caption(f"Completed: {meta.get('completed_at', 'N/A')} | Source: {meta.get('data_source', meta.get('provider', 'N/A'))} | Symbols: {', '.join(meta.get('symbols', []))} | Period: {meta.get('period', 'N/A')} | Interval: {meta.get('interval', 'N/A')}")
        m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
        m1.metric("Replay Events", f"{len(replay):,}")
        m2.metric("Signals", f"{len(signals):,}" if isinstance(signals, pd.DataFrame) else "0")
        m3.metric("Trades", int(metrics.get("total_trades", len(trades) if isinstance(trades, pd.DataFrame) else 0)))
        m4.metric("Rejected", int(metrics.get("rejected_signals", 0)))
        m5.metric("Net P/L", f"${float(metrics.get('net_pnl', 0)):,.2f}")
        m6.metric("Win Rate", f"{float(metrics.get('win_rate', 0)):,.1f}%")
        m7.metric("Profit Factor", metrics.get("profit_factor", 0))

        if isinstance(comparison, pd.DataFrame) and not comparison.empty:
            st.markdown("### Strategy Comparison")
            st.dataframe(comparison, use_container_width=True, hide_index=True)

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
        st.info("Run a backtest to generate Strategy Lab results.")
