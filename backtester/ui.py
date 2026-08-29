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


def _result_dte_values(comparison: pd.DataFrame, trades: pd.DataFrame) -> list[int]:
    values: list[int] = []
    sources = []
    if isinstance(comparison, pd.DataFrame) and not comparison.empty and "DTE" in comparison.columns:
        sources.append(comparison["DTE"])
    if isinstance(trades, pd.DataFrame) and not trades.empty and "option_dte" in trades.columns:
        sources.append(trades["option_dte"])
    for source in sources:
        for value in source.dropna().tolist():
            try:
                dte = int(float(value))
            except Exception:
                continue
            if dte not in values:
                values.append(dte)
    return values


def _render_dte_result(dte: int, comparison: pd.DataFrame, trades: pd.DataFrame) -> None:
    dte_trades = trades[pd.to_numeric(trades["option_dte"], errors="coerce") == int(dte)].copy() if isinstance(trades, pd.DataFrame) and not trades.empty and "option_dte" in trades.columns else pd.DataFrame()
    dte_row = pd.DataFrame()
    if isinstance(comparison, pd.DataFrame) and not comparison.empty and "DTE" in comparison.columns:
        dte_row = comparison[pd.to_numeric(comparison["DTE"], errors="coerce") == int(dte)].head(1)

    st.markdown(f"#### {dte} DTE")
    if not dte_row.empty:
        row = dte_row.iloc[0]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Trades", int(row.get("Trades", 0)))
        c2.metric("Win Rate", f"{float(row.get('Win %', 0)):,.1f}%")
        c3.metric("Net P/L", f"${float(row.get('Net P/L', 0)):,.2f}")
        c4.metric("Profit Factor", row.get("Profit Factor", 0))
    elif not dte_trades.empty:
        c1, c2 = st.columns(2)
        c1.metric("Trades", len(dte_trades))
        c2.metric("Net P/L", f"${pd.to_numeric(dte_trades.get('realized_pnl'), errors='coerce').sum():,.2f}")

    if dte_trades.empty:
        st.info(f"No simulated trades for {dte} DTE.")
        return

    st.plotly_chart(_chart_equity(dte_trades), use_container_width=True)
    daily = daily_pnl(dte_trades)
    fig = go.Figure()
    fig.add_trace(go.Bar(x=daily["date"], y=daily["realized_pnl"], name=f"{dte} DTE Daily P/L"))
    fig.update_layout(height=260, title="Daily P/L", xaxis_title="Date", yaxis_title="P/L USD")
    st.plotly_chart(fig, use_container_width=True)


def _save_strategy_lab_settings(config: dict, symbols: list[str], selected_symbols: list[str], selected_strategies: list[str], period: str, interval: str, max_symbols: int, force_refresh: bool, data_source: str, orb_minutes: int, first_signal_minutes: int, min_session_bars: int, visual_updates: bool, option_dte_values: list[int], premium_pct_ui: float, slippage_pct: float, allow_same_symbol: bool, risk_overrides: dict | None = None, gap_settings: dict | None = None) -> None:
    config.setdefault("strategy_lab", {})
    if "PMB" in {str(name).upper() for name in selected_strategies}:
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
        "option_dte_values": [int(v) for v in option_dte_values],
        "premium_pct_ui": float(premium_pct_ui),
        "slippage_pct": float(slippage_pct),
        "allow_same_symbol": bool(allow_same_symbol),
        "gap": dict(gap_settings or {}),
    }
    if risk_overrides:
        config["strategy_lab"].update(risk_overrides)
    if save_config is not None:
        save_config(config)


def _apply_strategy_lab_to_engine(config: dict, settings: StrategyLabSettings, selected_symbols: list[str]) -> dict[str, object]:
    selected_names = {str(name).strip().upper() for name in settings.selected_strategies}
    if selected_names != {"PMB"}:
        raise ValueError("Only the implemented PMB strategy can be applied to the live trading engine.")

    strategy = config.setdefault("strategy", {})
    risk = config.setdefault("risk", {})
    lab_cfg = config.setdefault("strategy_lab", {})

    selected_strategy = "PMB"
    option_dte = int(settings.option_dte_values[0]) if settings.option_dte_values else int(strategy.get("option_dte", 7))
    account_size = max(float(settings.starting_capital or risk.get("account_size", 1000) or 1000), 1.0)
    max_spend = float(settings.max_spend_per_trade)
    max_daily_capital = float(settings.max_daily_capital)
    if settings.sizing_method == "percent_equity":
        max_spend = round(account_size * float(settings.position_allocation_pct) / 100.0, 2)
        max_daily_capital = round(account_size * float(settings.max_daily_exposure_pct) / 100.0, 2)
        risk["max_spend_per_trade_pct"] = float(settings.position_allocation_pct)
        risk["max_daily_capital_pct"] = float(settings.max_daily_exposure_pct)
    else:
        risk["max_spend_per_trade_pct"] = round(max_spend / account_size * 100.0, 2)
        risk["max_daily_capital_pct"] = round(max_daily_capital / account_size * 100.0, 2)

    config["watchlist"] = list(selected_symbols)
    lab_cfg["applied_to_engine_at"] = datetime.now().isoformat(timespec="seconds")
    lab_cfg["last_applied_strategy"] = str(selected_strategy).upper()

    strategy.update({
        "active_strategy": str(selected_strategy).lower(),
        "option_dte": option_dte,
        "orb_minutes": int(settings.orb_minutes),
        "first_signal_minutes": int(settings.first_signal_minutes),
        "min_session_bars": int(settings.min_session_bars),
    })
    risk.update({
        "account_size": round(account_size, 2),
        "max_trades_per_day": int(settings.max_trades_per_day),
        "max_spend_per_trade": round(max_spend, 2),
        "max_daily_capital": round(max_daily_capital, 2),
        "recycle_capital_after_exit": bool(settings.recycle_capital_after_exit),
        "reserve_capital_for_remaining_trades": bool(settings.reserve_capital_for_remaining_trades),
        "max_contracts": max(1, int(settings.max_contracts or risk.get("max_contracts", 1) or 1)),
        "stop_loss_pct": float(settings.stop_loss_pct),
        "take_profit_pct": float(settings.take_profit_pct),
        "breakeven_trigger_pct": float(settings.breakeven_trigger_pct),
        "trailing_trigger_pct": float(settings.trailing_trigger_pct),
        "trailing_stop_pct": float(settings.trailing_stop_pct),
        "entry_cutoff_hour": int(settings.entry_cutoff_hour),
        "entry_cutoff_minute": int(settings.entry_cutoff_minute),
        "force_exit_enabled": bool(settings.force_exit_enabled),
        "force_exit_hour": int(settings.force_exit_hour),
        "force_exit_minute": int(settings.force_exit_minute),
        "max_consecutive_losses": int(settings.max_consecutive_losses),
        "max_daily_drawdown_pct": float(settings.max_daily_drawdown_pct),
    })
    if save_config is not None:
        save_config(config)
    return {
        "Strategy": str(selected_strategy).upper(),
        "Watchlist": len(selected_symbols),
        "Option DTE": option_dte,
        "ORB": int(settings.orb_minutes),
        "Max trades/day": int(settings.max_trades_per_day),
        "Max spend/trade": round(max_spend, 2),
        "Max daily capital": round(max_daily_capital, 2),
        "Stop %": float(settings.stop_loss_pct),
        "Target %": float(settings.take_profit_pct),
    }


def render_strategy_lab_tab(config: dict, default_symbols: list[str]):
    st.subheader("Research Lab")
    st.caption("Run selected strategies on historical candles and compare the results before paper/live trading.")

    strategy = config.get("strategy", {})
    risk = config.get("risk", {})
    lab_cfg = config.get("strategy_lab", {})
    gap_cfg = lab_cfg.get("gap", {}) if isinstance(lab_cfg.get("gap", {}), dict) else {}

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
            if "GAP" in selected_strategies:
                st.caption("GAP scanner/backtester is implemented for stock research only. It cannot place live orders.")
            if any(x in selected_strategies for x in ["BRT", "PULLBACK"]):
                st.caption("BRT and Pullback are placeholders and intentionally produce no trades.")
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
            period_options = ["1d", "7d", "14d", "30d", "60d", "90d"]
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
    non_gap_selected = any(name != "GAP" for name in selected_strategies)
    if "GAP" in selected_strategies:
        st.caption("GAP builds its universe with Yahoo's market screener, then loads extended-hours candles from IBKR. The data-source selector applies only to PMB/BRT/Pullback.")
        if not YFINANCE_AVAILABLE:
            st.error("GAP requires yfinance for fresh market-wide universe discovery.")
            return
    if non_gap_selected and str(data_source).upper() == "IBKR":
        st.caption("Strategy Lab uses IBKR historical candles. Keep TWS/IB Gateway open.")
    elif non_gap_selected and not YFINANCE_AVAILABLE:
        st.error("Yahoo selected but yfinance is not installed.")
        st.code("pip install yfinance pyarrow", language="bash")
        return
    if non_gap_selected and str(data_source).upper() == "YAHOO" and period == "90d" and interval == "5m":
        st.caption("Note: Yahoo may limit 5-minute history. If 90d returns no data, switch to 15m or use 60d.")

    try:
        provider = create_market_data_provider(data_source, config) if create_market_data_provider else YahooDataClient()
        gap_provider = (
            create_market_data_provider("IBKR", config, client_id_offset=201, readonly_override=True)
            if "GAP" in selected_strategies and create_market_data_provider
            else provider
        )
    except Exception as exc:
        st.error(f"Could not initialize {data_source} market-data provider: {exc}")
        return
    controller = StrategyLabController(data_provider=provider, gap_data_provider=gap_provider)

    with setting_cols[3]:
        with st.expander("🔁 Scanner", expanded=False):
            orb_minutes = st.number_input("ORB window minutes", min_value=5, max_value=90, value=int(lab_cfg.get("orb_minutes", strategy.get("orb_minutes", 15))), step=5, key="sl_orb")
            first_signal_minutes = st.number_input("First scanner minute", min_value=5, max_value=120, value=int(lab_cfg.get("first_signal_minutes", strategy.get("first_signal_minutes", 20))), step=5, key="sl_first_signal")
            min_session_bars = st.number_input("Minimum session bars", min_value=2, max_value=30, value=int(lab_cfg.get("min_session_bars", 7)), step=1, key="sl_min_bars")
            visual_updates = st.checkbox("Show live replay progress", value=bool(lab_cfg.get("visual_updates", True)), key="sl_visual")

    with setting_cols[4]:
        with st.expander("💰 Options", expanded=False):
            starting_capital = st.number_input("Capital USD", min_value=100.0, value=float(lab_cfg.get("account_size", risk.get("account_size", 1000))), step=100.0, key="sl_capital")
            max_trades_per_day = st.number_input("Max trades/day", min_value=1, max_value=10, value=int(lab_cfg.get("max_trades_per_day", risk.get("max_trades_per_day", 2))), step=1, key="sl_max_trades")
            saved_sizing_method = str(lab_cfg.get("sizing_method", "percent_equity"))
            sizing_model_options = ["% of Equity", "Fixed Dollar"]
            sizing_model_index = 1 if saved_sizing_method == "fixed_dollar" else 0
            sizing_model = st.selectbox("Position sizing", sizing_model_options, index=sizing_model_index, key="sl_sizing_model")
            if sizing_model == "% of Equity":
                allocation_pct = st.number_input("Position allocation %", min_value=1.0, max_value=100.0, value=float(lab_cfg.get("position_allocation_pct", risk.get("max_spend_per_trade_pct", 20.0) or 20.0)), step=1.0, key="sl_alloc_pct")
                daily_exposure_pct = st.number_input("Max daily exposure %", min_value=1.0, max_value=100.0, value=float(lab_cfg.get("max_daily_exposure_pct", risk.get("max_daily_capital_pct", 40.0) or 40.0)), step=1.0, key="sl_daily_exp_pct")
                max_spend = float(starting_capital) * float(allocation_pct) / 100.0
                max_daily_capital = float(starting_capital) * float(daily_exposure_pct) / 100.0
                st.caption("Compounds automatically: each trade uses a % of current equity, not a fixed dollar amount.")
            else:
                allocation_pct = float(lab_cfg.get("position_allocation_pct", risk.get("max_spend_per_trade_pct", 20.0) or 20.0))
                daily_exposure_pct = float(lab_cfg.get("max_daily_exposure_pct", risk.get("max_daily_capital_pct", 40.0) or 40.0))
                max_spend = st.number_input("Max spend/trade USD", min_value=50.0, value=float(lab_cfg.get("max_spend_per_trade", risk.get("max_spend_per_trade", 250))), step=50.0, key="sl_spend")
                default_daily_capital = max(float(lab_cfg.get("max_daily_capital", risk.get("max_daily_capital", 0)) or 0), float(lab_cfg.get("account_size", risk.get("account_size", 1000))), float(max_spend))
                max_daily_capital = st.number_input("Max daily capital USD", min_value=50.0, value=default_daily_capital, step=50.0, key="sl_daily_cap")
            reserve_capital = st.checkbox("Reserve capital for remaining trades", value=bool(lab_cfg.get("reserve_capital_for_remaining_trades", risk.get("reserve_capital_for_remaining_trades", True))), key="sl_reserve_capital")
            recycle_capital = st.checkbox("Recycle capital after exits", value=bool(lab_cfg.get("recycle_capital_after_exit", risk.get("recycle_capital_after_exit", False))), key="sl_recycle_capital")
            max_contracts = st.number_input("Max contracts/trade", min_value=0, max_value=100, value=int(lab_cfg.get("max_contracts", risk.get("max_contracts", 0) or 0)), step=1, key="sl_max_contracts", help="0 means no fixed contract cap.")
            stop_loss = st.number_input("Stop loss %", min_value=1.0, max_value=90.0, value=float(lab_cfg.get("stop_loss_pct", risk.get("stop_loss_pct", 20.0))), step=1.0, key="sl_stop")
            take_profit = st.number_input("Take profit %", min_value=1.0, max_value=300.0, value=float(lab_cfg.get("take_profit_pct", risk.get("take_profit_pct", 30.0))), step=1.0, key="sl_tp")
            breakeven_trigger = st.number_input("Move stop to breakeven at +%", min_value=1.0, max_value=200.0, value=float(lab_cfg.get("breakeven_trigger_pct", risk.get("breakeven_trigger_pct", 15.0))), step=1.0, key="sl_breakeven")
            trailing_trigger = st.number_input("Activate trailing stop at +%", min_value=1.0, max_value=300.0, value=float(lab_cfg.get("trailing_trigger_pct", risk.get("trailing_trigger_pct", 25.0))), step=1.0, key="sl_trailing_trigger")
            trailing_stop = st.number_input("Trailing stop distance %", min_value=1.0, max_value=90.0, value=float(lab_cfg.get("trailing_stop_pct", risk.get("trailing_stop_pct", 10.0))), step=1.0, key="sl_trailing_stop")
            entry_cutoff_hour = st.number_input("No entries after hour ET", min_value=9, max_value=15, value=int(lab_cfg.get("entry_cutoff_hour", risk.get("entry_cutoff_hour", 11))), step=1, key="sl_entry_cutoff_hour")
            entry_cutoff_minute = st.number_input("No entries after minute ET", min_value=0, max_value=59, value=int(lab_cfg.get("entry_cutoff_minute", risk.get("entry_cutoff_minute", 0))), step=1, key="sl_entry_cutoff_minute")
            force_exit_enabled = st.checkbox("Force exit near end of day", value=bool(lab_cfg.get("force_exit_enabled", risk.get("force_exit_enabled", True))), key="sl_force_exit_enabled")
            force_exit_hour = st.number_input("Force exit hour ET", min_value=9, max_value=15, value=int(lab_cfg.get("force_exit_hour", risk.get("force_exit_hour", 15))), step=1, key="sl_force_exit_hour")
            force_exit_minute = st.number_input("Force exit minute ET", min_value=0, max_value=59, value=int(lab_cfg.get("force_exit_minute", risk.get("force_exit_minute", 55))), step=1, key="sl_force_exit_minute")
            max_consecutive_losses = st.number_input("Stop after consecutive losses", min_value=1, max_value=10, value=int(lab_cfg.get("max_consecutive_losses", risk.get("max_consecutive_losses", 2))), step=1, key="sl_max_consecutive_losses")
            max_daily_drawdown_pct = st.number_input("Max daily drawdown %", min_value=1.0, max_value=50.0, value=float(lab_cfg.get("max_daily_drawdown_pct", risk.get("max_daily_drawdown_pct", 5.0))), step=1.0, key="sl_max_daily_drawdown")
            saved_dtes = [int(v) for v in lab_cfg.get("option_dte_values", [7, 14]) if int(v) in [7, 14]]
            option_dte_values = st.multiselect("Compare option DTE", [7, 14], default=saved_dtes or [7, 14], format_func=lambda value: f"{value} DTE", key="sl_option_dtes")
            premium_pct_ui = st.number_input("Entry premium % of stock", min_value=0.1, max_value=10.0, value=float(lab_cfg.get("premium_pct_ui", 0.25)), step=0.05, key="sl_premium_v092")
            slippage_pct = st.number_input("Slippage %", min_value=0.0, max_value=20.0, value=float(lab_cfg.get("slippage_pct", 2.0)), step=0.5, key="sl_slippage")
            allow_same_symbol = st.checkbox("Allow same symbol more than once per day", value=bool(lab_cfg.get("allow_same_symbol", False)), key="sl_same_symbol")

    gap_price_min = float(gap_cfg.get("price_min", 3.0))
    gap_price_max = float(gap_cfg.get("price_max", 15.0))
    gap_min_abs_gap_pct = float(gap_cfg.get("min_abs_gap_pct", 8.0))
    gap_min_premarket_volume = int(gap_cfg.get("min_premarket_volume", 500_000))
    gap_min_premarket_rvol = float(gap_cfg.get("min_premarket_rvol", 3.0))
    gap_min_avg_daily_volume = int(gap_cfg.get("min_avg_daily_volume", 1_000_000))
    gap_allow_shorts = bool(gap_cfg.get("allow_shorts", True))
    gap_risk_per_trade = float(gap_cfg.get("risk_per_trade", max(10.0, float(starting_capital) * 0.005)))
    gap_max_capital_per_trade = float(gap_cfg.get("max_capital_per_trade", max(100.0, float(starting_capital) * 0.20)))
    gap_max_daily_capital = float(gap_cfg.get("max_daily_capital", max(100.0, float(starting_capital) * 0.40)))
    gap_max_trades_per_day = int(gap_cfg.get("max_trades_per_day", 2))
    gap_stop_buffer_pct = float(gap_cfg.get("stop_buffer_pct", 0.25))
    gap_min_stop_distance_pct = float(gap_cfg.get("min_stop_distance_pct", 0.5))
    gap_max_stop_distance_pct = float(gap_cfg.get("max_stop_distance_pct", 6.0))
    gap_target_r = float(gap_cfg.get("target_r", 2.0))
    gap_force_exit_hour = int(gap_cfg.get("force_exit_hour", 11))
    gap_force_exit_minute = int(gap_cfg.get("force_exit_minute", 30))
    gap_slippage_pct = float(gap_cfg.get("slippage_pct", 0.10))
    gap_commission_per_share = float(gap_cfg.get("commission_per_share", 0.005))
    gap_minimum_order_commission = float(gap_cfg.get("minimum_order_commission", 1.0))
    gap_universe_max_symbols = int(gap_cfg.get("universe_max_symbols", 2_000))
    gap_ibkr_request_delay_seconds = float(gap_cfg.get("ibkr_request_delay_seconds", 0.25))
    gap_ibkr_max_retries = int(gap_cfg.get("ibkr_max_retries", 2))

    if "GAP" in selected_strategies:
        with st.expander("Gap Scanner & Stock Backtest", expanded=True):
            st.caption("Research-only. Every run builds a fresh US-stock universe with Yahoo's market screener, then loads IBKR extended-hours trade candles. The ticker selector above is ignored by GAP.")
            st.caption("Keep TWS or IB Gateway open with historical market-data permissions. A full-universe IBKR run can take several minutes because symbols are paced and retried individually.")
            st.caption("Historical runs use the universe generated today because Yahoo does not provide historical screener membership; symbols that moved outside $3-$15 before today can be absent from older sessions.")
            gap_cols = st.columns(5)
            with gap_cols[0]:
                gap_price_min = st.number_input("Minimum stock price", min_value=1.0, max_value=50.0, value=gap_price_min, step=0.5, key="sl_gap_price_min")
                gap_price_max = st.number_input("Maximum stock price", min_value=2.0, max_value=100.0, value=max(gap_price_max, gap_price_min), step=0.5, key="sl_gap_price_max")
                gap_min_abs_gap_pct = st.number_input("Minimum absolute gap %", min_value=1.0, max_value=50.0, value=gap_min_abs_gap_pct, step=0.5, key="sl_gap_min_gap")
            with gap_cols[1]:
                gap_min_premarket_volume = st.number_input("Minimum premarket volume", min_value=0, max_value=100_000_000, value=gap_min_premarket_volume, step=50_000, key="sl_gap_pm_volume")
                gap_min_premarket_rvol = st.number_input("Minimum premarket RVOL", min_value=0.1, max_value=50.0, value=gap_min_premarket_rvol, step=0.25, key="sl_gap_pm_rvol")
                gap_min_avg_daily_volume = st.number_input("Minimum average daily volume", min_value=0, max_value=500_000_000, value=gap_min_avg_daily_volume, step=100_000, key="sl_gap_adv")
            with gap_cols[2]:
                gap_risk_per_trade = st.number_input("Risk per trade USD", min_value=1.0, max_value=float(starting_capital), value=min(gap_risk_per_trade, float(starting_capital)), step=10.0, key="sl_gap_risk")
                gap_max_capital_per_trade = st.number_input("Max stock notional per trade", min_value=10.0, max_value=max(float(starting_capital), 10.0), value=min(gap_max_capital_per_trade, float(starting_capital)), step=100.0, key="sl_gap_capital_trade")
                gap_max_daily_capital = st.number_input("Max daily stock notional", min_value=10.0, max_value=max(float(starting_capital), 10.0), value=min(gap_max_daily_capital, float(starting_capital)), step=100.0, key="sl_gap_daily_capital")
            with gap_cols[3]:
                gap_max_trades_per_day = st.number_input("Max GAP trades/day", min_value=1, max_value=10, value=gap_max_trades_per_day, step=1, key="sl_gap_max_trades")
                gap_stop_buffer_pct = st.number_input("Opening-range stop buffer %", min_value=0.0, max_value=5.0, value=gap_stop_buffer_pct, step=0.05, key="sl_gap_stop_buffer")
                gap_min_stop_distance_pct = st.number_input("Minimum stop distance %", min_value=0.1, max_value=10.0, value=gap_min_stop_distance_pct, step=0.1, key="sl_gap_min_stop")
                gap_max_stop_distance_pct = st.number_input("Maximum stop distance %", min_value=0.5, max_value=30.0, value=max(gap_max_stop_distance_pct, gap_min_stop_distance_pct), step=0.5, key="sl_gap_max_stop")
            with gap_cols[4]:
                gap_target_r = st.number_input("Profit target R", min_value=0.5, max_value=10.0, value=gap_target_r, step=0.25, key="sl_gap_target_r")
                gap_allow_shorts = st.checkbox("Backtest gap-up shorts", value=gap_allow_shorts, key="sl_gap_shorts")
                gap_slippage_pct = st.number_input("Stock slippage % per fill", min_value=0.0, max_value=5.0, value=gap_slippage_pct, step=0.05, key="sl_gap_slippage")
                gap_force_exit_hour = st.number_input("Timed exit hour ET", min_value=9, max_value=15, value=gap_force_exit_hour, step=1, key="sl_gap_exit_hour")
                gap_force_exit_minute = st.number_input("Timed exit minute ET", min_value=0, max_value=59, value=gap_force_exit_minute, step=5, key="sl_gap_exit_minute")
            st.caption("Entry is fixed at 09:45 ET after the first 15-minute candle. News is checked from the previous session close through entry; dates without adequate local news coverage are marked UNVERIFIED and rejected.")
            if gap_allow_shorts:
                st.caption("Gap-up short results are theoretical: historical shortability, borrow fees, SSR restrictions, and trading halts are not modeled yet.")
            if interval not in {"5m", "15m"}:
                st.warning("GAP needs 5-minute or 15-minute candles for a precise 09:45 entry. Wider intervals will be rejected by the scanner audit.")

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
        use_rvol_ranking=bool(strategy.get("use_rvol_ranking", False)),
        use_sr_filter=bool(strategy.get("use_sr_filter", True)),
        min_sr_room_pct=float(strategy.get("min_sr_room_pct", 0.75)),
        top_n_tickers=int(strategy.get("top_n_tickers", 2)),
        starting_capital=float(starting_capital),
        max_trades_per_day=int(max_trades_per_day),
        sizing_method="percent_equity" if sizing_model == "% of Equity" else "fixed_dollar",
        position_allocation_pct=float(allocation_pct),
        max_daily_exposure_pct=float(daily_exposure_pct),
        max_spend_per_trade=float(max_spend),
        max_daily_capital=float(max_daily_capital),
        recycle_capital_after_exit=bool(recycle_capital),
        reserve_capital_for_remaining_trades=bool(reserve_capital),
        max_contracts=int(max_contracts),
        option_dte_values=tuple(int(v) for v in option_dte_values),
        stop_loss_pct=float(stop_loss),
        take_profit_pct=float(take_profit),
        breakeven_trigger_pct=float(breakeven_trigger),
        trailing_trigger_pct=float(trailing_trigger),
        trailing_stop_pct=float(trailing_stop),
        entry_cutoff_hour=int(entry_cutoff_hour),
        entry_cutoff_minute=int(entry_cutoff_minute),
        force_exit_enabled=bool(force_exit_enabled),
        force_exit_hour=int(force_exit_hour),
        force_exit_minute=int(force_exit_minute),
        max_consecutive_losses=int(max_consecutive_losses),
        max_daily_drawdown_pct=float(max_daily_drawdown_pct),
        premium_pct=float(premium_pct_ui) / 100.0,
        slippage_pct=float(slippage_pct),
        allow_same_symbol_same_day=bool(allow_same_symbol),
        selected_strategies=tuple(selected_strategies),
        data_source=str(data_source),
        gap_price_min=float(gap_price_min),
        gap_price_max=float(gap_price_max),
        gap_min_abs_gap_pct=float(gap_min_abs_gap_pct),
        gap_min_premarket_volume=int(gap_min_premarket_volume),
        gap_min_premarket_rvol=float(gap_min_premarket_rvol),
        gap_min_avg_daily_volume=int(gap_min_avg_daily_volume),
        gap_allow_shorts=bool(gap_allow_shorts),
        gap_risk_per_trade=float(gap_risk_per_trade),
        gap_max_capital_per_trade=float(gap_max_capital_per_trade),
        gap_max_daily_capital=float(gap_max_daily_capital),
        gap_max_trades_per_day=int(gap_max_trades_per_day),
        gap_stop_buffer_pct=float(gap_stop_buffer_pct),
        gap_min_stop_distance_pct=float(gap_min_stop_distance_pct),
        gap_max_stop_distance_pct=float(gap_max_stop_distance_pct),
        gap_target_r=float(gap_target_r),
        gap_force_exit_hour=int(gap_force_exit_hour),
        gap_force_exit_minute=int(gap_force_exit_minute),
        gap_slippage_pct=float(gap_slippage_pct),
        gap_commission_per_share=float(gap_commission_per_share),
        gap_minimum_order_commission=float(gap_minimum_order_commission),
        gap_universe_max_symbols=int(gap_universe_max_symbols),
        gap_ibkr_request_delay_seconds=float(gap_ibkr_request_delay_seconds),
        gap_ibkr_max_retries=int(gap_ibkr_max_retries),
    )
    _save_strategy_lab_settings(
        config,
        symbols,
        selected_all_symbols,
        selected_strategies,
        period,
        interval,
        max_symbols,
        force_refresh,
        data_source,
        orb_minutes,
        first_signal_minutes,
        min_session_bars,
        visual_updates,
        list(option_dte_values),
        premium_pct_ui,
        slippage_pct,
        allow_same_symbol,
        {
            "account_size": float(starting_capital),
            "max_trades_per_day": int(max_trades_per_day),
            "sizing_method": "percent_equity" if sizing_model == "% of Equity" else "fixed_dollar",
            "position_allocation_pct": float(allocation_pct),
            "max_daily_exposure_pct": float(daily_exposure_pct),
            "max_spend_per_trade": float(max_spend),
            "max_daily_capital": float(max_daily_capital),
            "max_contracts": int(max_contracts),
            "recycle_capital_after_exit": bool(recycle_capital),
            "reserve_capital_for_remaining_trades": bool(reserve_capital),
            "stop_loss_pct": float(stop_loss),
            "take_profit_pct": float(take_profit),
            "breakeven_trigger_pct": float(breakeven_trigger),
            "trailing_trigger_pct": float(trailing_trigger),
            "trailing_stop_pct": float(trailing_stop),
            "entry_cutoff_hour": int(entry_cutoff_hour),
            "entry_cutoff_minute": int(entry_cutoff_minute),
            "force_exit_enabled": bool(force_exit_enabled),
            "force_exit_hour": int(force_exit_hour),
            "force_exit_minute": int(force_exit_minute),
            "max_consecutive_losses": int(max_consecutive_losses),
            "max_daily_drawdown_pct": float(max_daily_drawdown_pct),
        },
        {
            "price_min": float(gap_price_min),
            "price_max": float(gap_price_max),
            "min_abs_gap_pct": float(gap_min_abs_gap_pct),
            "min_premarket_volume": int(gap_min_premarket_volume),
            "min_premarket_rvol": float(gap_min_premarket_rvol),
            "min_avg_daily_volume": int(gap_min_avg_daily_volume),
            "allow_shorts": bool(gap_allow_shorts),
            "risk_per_trade": float(gap_risk_per_trade),
            "max_capital_per_trade": float(gap_max_capital_per_trade),
            "max_daily_capital": float(gap_max_daily_capital),
            "max_trades_per_day": int(gap_max_trades_per_day),
            "stop_buffer_pct": float(gap_stop_buffer_pct),
            "min_stop_distance_pct": float(gap_min_stop_distance_pct),
            "max_stop_distance_pct": float(gap_max_stop_distance_pct),
            "target_r": float(gap_target_r),
            "force_exit_hour": int(gap_force_exit_hour),
            "force_exit_minute": int(gap_force_exit_minute),
            "slippage_pct": float(gap_slippage_pct),
            "commission_per_share": float(gap_commission_per_share),
            "minimum_order_commission": float(gap_minimum_order_commission),
            "universe_max_symbols": int(gap_universe_max_symbols),
            "ibkr_request_delay_seconds": float(gap_ibkr_request_delay_seconds),
            "ibkr_max_retries": int(gap_ibkr_max_retries),
        },
    )

    action_col, apply_col = st.columns([2, 1])
    research_only_selection = any(name != "PMB" for name in selected_strategies)
    with action_col:
        run_clicked = st.button("Run Backtest", use_container_width=True, key="sl_run")
    with apply_col:
        apply_confirmed = st.checkbox("Confirm apply", key="sl_apply_confirm")
        apply_clicked = st.button("Apply to Trading Engine", use_container_width=True, key="sl_apply_to_engine", disabled=(not apply_confirmed or research_only_selection))
        if research_only_selection:
            st.caption("Live apply is disabled while a research-only strategy is selected.")

    with st.expander(f"{controller.provider_name} data cache", expanded=False):
        cache_info = controller.cache_info()
        cache_cols = st.columns([1, 3])
        with cache_cols[0]:
            clear_data_cache = st.button("Clear Selected Cache", use_container_width=True, key="sl_clear_data_cache")
        if clear_data_cache:
            removed = 0
            for cache_symbol in symbols:
                removed += controller.clear_cache(cache_symbol)
            st.success(f"Removed {removed} cached file(s).")
            cache_info = controller.cache_info()
        if cache_info.empty:
            st.info("No cached files found yet.")
        else:
            st.dataframe(cache_info, use_container_width=True, hide_index=True)

    if apply_clicked:
        if not symbols:
            st.warning("Add at least one symbol before applying to the trading engine.")
        elif save_config is None:
            st.error("Could not save config.json from Strategy Lab.")
        else:
            applied = _apply_strategy_lab_to_engine(config, settings, selected_all_symbols or symbols)
            st.success("Strategy Lab settings applied to the main trading engine config.")
            st.caption("This updates scanner/risk settings only. It does not change account mode or enable order placement.")
            st.dataframe(pd.DataFrame([applied]), use_container_width=True, hide_index=True)

    if run_clicked:
        requires_selected_symbols = any(name != "GAP" for name in selected_strategies)
        if requires_selected_symbols and not symbols:
            st.warning("Add at least one symbol for the selected non-GAP strategy.")
        else:
            st.session_state["bt_job_running"] = True
            progress = st.progress(0)
            status = st.empty()
            live_box = st.empty()

            def progress_callback(idx, total, row, signal_count):
                progress.progress(min(1.0, idx / max(total, 1)))
                stage = str(row.get("stage") or "Replaying")
                if visual_updates:
                    status.info(f"{stage} {idx:,} / {total:,} | Signals: {signal_count:,} | {row.get('symbol')} | {row.get('timestamp')}")
                    if idx % max(1, total // 50) == 0 or idx == total:
                        live_box.dataframe(pd.DataFrame([row]), use_container_width=True, hide_index=True)

            try:
                result = controller.run(settings, progress_callback=progress_callback)
                st.session_state["sl_last_result"] = result
                result_status = str(result.get("meta", {}).get("status", "")).upper()
                if result_status == "COMPLETE":
                    status.success("Research Lab backtest complete. Results saved to backtester/exports.")
                elif result_status == "COMPLETE_WITH_DATA_ERRORS":
                    status.warning("Backtest completed with incomplete market-data coverage. Review Data errors before using the results.")
                else:
                    status.error("Research Lab could not load market data. Open Data errors below for details.")
            except Exception as exc:
                status.error(f"Strategy Lab failed: {exc}")
            finally:
                controller.disconnect_providers()
                st.session_state["bt_job_running"] = False

    session_result = st.session_state.get("sl_last_result")
    if isinstance(session_result, dict):
        result = session_result
    else:
        result = controller.load_last_result()
    replay = result.get("replay", pd.DataFrame())
    gap_universe = result.get("gap_universe", pd.DataFrame())
    gap_scanner = result.get("gap_scanner", pd.DataFrame())
    signals = result.get("signals", pd.DataFrame())
    trades = result.get("trades", pd.DataFrame())
    decisions = result.get("decisions", pd.DataFrame())
    comparison = result.get("comparison", pd.DataFrame())
    sessions = result.get("sessions", pd.DataFrame())
    metrics = result.get("metrics", {}) or {}
    meta = result.get("meta", {}) or {}
    errors = result.get("errors", pd.DataFrame())

    has_result = (
        (isinstance(replay, pd.DataFrame) and not replay.empty)
        or (isinstance(gap_universe, pd.DataFrame) and not gap_universe.empty)
        or (isinstance(errors, pd.DataFrame) and not errors.empty)
    )
    if has_result:
        st.markdown("### Last Backtest Result")
        base_symbols = list(meta.get("symbols", []))
        source_label = str(meta.get("data_source", meta.get("provider", "N/A")))
        if int(meta.get("gap_universe_count", 0) or 0):
            gap_source = f"Yahoo universe + IBKR candles ({int(meta.get('gap_universe_count', 0)):,} stocks)"
            source_label = f"{source_label} + {gap_source}" if base_symbols else gap_source
        symbol_label = ", ".join(base_symbols[:20]) if base_symbols else "Yahoo-generated GAP universe"
        if len(base_symbols) > 20:
            symbol_label += f" + {len(base_symbols) - 20} more"
        st.caption(f"Completed: {meta.get('completed_at', 'N/A')} | Source: {source_label} | Symbols: {symbol_label} | Period: {meta.get('period', 'N/A')} | Interval: {meta.get('interval', 'N/A')}")
        m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
        m1.metric("Replay Events", f"{len(replay):,}")
        m2.metric("Signals", f"{len(signals):,}" if isinstance(signals, pd.DataFrame) else "0")
        m3.metric("Trades", int(metrics.get("total_trades", len(trades) if isinstance(trades, pd.DataFrame) else 0)))
        m4.metric("Rejected", int(metrics.get("rejected_signals", 0)))
        m5.metric("Net P/L", f"${float(metrics.get('net_pnl', 0)):,.2f}")
        m6.metric("Win Rate", f"{float(metrics.get('win_rate', 0)):,.1f}%")
        m7.metric("Profit Factor", metrics.get("profit_factor", 0))

        if isinstance(errors, pd.DataFrame) and not errors.empty:
            with st.expander(f"Data errors ({len(errors):,})", expanded=False):
                st.dataframe(errors.tail(2000), use_container_width=True, hide_index=True)

        if isinstance(gap_universe, pd.DataFrame) and not gap_universe.empty:
            st.markdown("### GAP Discovery Universe")
            reported_total = int(pd.to_numeric(gap_universe.get("reported_total"), errors="coerce").max()) if "reported_total" in gap_universe.columns and pd.to_numeric(gap_universe["reported_total"], errors="coerce").notna().any() else len(gap_universe)
            raw_quotes_received = int(pd.to_numeric(gap_universe.get("raw_quotes_received"), errors="coerce").max()) if "raw_quotes_received" in gap_universe.columns else len(gap_universe)
            u1, u2, u3 = st.columns(3)
            u1.metric("Eligible Stocks", len(gap_universe))
            u2.metric("Yahoo Matches", reported_total)
            candle_symbols = int(meta.get("gap_candle_symbols", 0) or 0)
            u3.metric("IBKR Candles Loaded", candle_symbols)
            if reported_total > raw_quotes_received:
                st.warning(f"Yahoo reported {reported_total:,} screen matches, but the configured safety cap retrieved {raw_quotes_received:,}.")
            if raw_quotes_received > len(gap_universe):
                st.caption(f"Removed {raw_quotes_received - len(gap_universe):,} Yahoo rows after exact equity, price, and liquidity validation.")
            if candle_symbols < len(gap_universe):
                st.warning(f"IBKR candle coverage is incomplete: {candle_symbols:,} of {len(gap_universe):,} universe symbols loaded. Open Data errors before trusting these results.")
            universe_cols = [c for c in ["symbol", "name", "exchange", "price", "change_pct", "day_volume", "avg_daily_volume_3m", "market_cap", "screened_at"] if c in gap_universe.columns]
            st.dataframe(gap_universe[universe_cols], use_container_width=True, hide_index=True)
            st.download_button("Download Yahoo GAP universe", gap_universe.to_csv(index=False), "strategy_lab_gap_universe.csv", "text/csv")

        if isinstance(gap_scanner, pd.DataFrame) and not gap_scanner.empty:
            gap_qualified = int((gap_scanner["status"].astype(str) == "QUALIFIED").sum()) if "status" in gap_scanner.columns else 0
            unverified = int((gap_scanner["catalyst_status"].astype(str) == "UNVERIFIED").sum()) if "catalyst_status" in gap_scanner.columns else 0
            st.markdown("### GAP Scanner Audit")
            g1, g2, g3 = st.columns(3)
            g1.metric("Sessions Checked", len(gap_scanner))
            g2.metric("Qualified at 09:45", gap_qualified)
            g3.metric("News Unverified", unverified)
            if unverified:
                st.warning(f"{unverified:,} symbol-sessions lacked adequate local news coverage. They were rejected, so the backtest does not treat unknown news as no news.")
            gap_cols = [c for c in ["session_date", "symbol", "status", "signal", "gap_pct", "premarket_last", "premarket_volume", "premarket_rvol", "avg_daily_volume", "entry_time", "entry_price", "opening_vwap", "opening_range_high", "opening_range_low", "score", "catalyst_status", "catalyst_headline", "rejection_codes"] if c in gap_scanner.columns]
            st.dataframe(gap_scanner[gap_cols].tail(1000), use_container_width=True, hide_index=True)
            st.download_button("Download GAP scanner audit", gap_scanner.to_csv(index=False), "strategy_lab_gap_scanner.csv", "text/csv")

        if (
            str(meta.get("data_source", meta.get("provider", ""))).upper() == "IBKR"
            and isinstance(decisions, pd.DataFrame)
            and not decisions.empty
            and "stage" in decisions.columns
        ):
            option_data_rejects = int((decisions["stage"].astype(str) == "OPTION_DATA").sum())
            if option_data_rejects:
                coverage_pct = 100.0 * (len(decisions) - option_data_rejects) / max(len(decisions), 1)
                st.warning(
                    f"IBKR option-history coverage is incomplete: {option_data_rejects:,} of {len(decisions):,} signals "
                    f"were rejected before simulation because historical option bars were unavailable. "
                    f"Coverage: {coverage_pct:.1f}%."
                )

        dte_values = _result_dte_values(comparison, trades)
        if dte_values:
            st.markdown("### Option DTE Results")
            if len(dte_values) == 1:
                _render_dte_result(dte_values[0], comparison, trades)
            else:
                columns = st.columns(len(dte_values))
                for column, dte in zip(columns, dte_values):
                    with column:
                        _render_dte_result(dte, comparison, trades)

        if isinstance(comparison, pd.DataFrame) and not comparison.empty:
            st.markdown("### Strategy Comparison")
            st.dataframe(comparison, use_container_width=True, hide_index=True)

        if isinstance(trades, pd.DataFrame) and not trades.empty:
            st.markdown("### Net P/L by Symbol")
            sp = symbol_pnl(trades)
            fig = go.Figure()
            fig.add_trace(go.Bar(x=sp["symbol"], y=sp["realized_pnl"], name="Symbol P/L"))
            fig.update_layout(height=330, xaxis_title="Symbol", yaxis_title="P/L USD")
            st.plotly_chart(fig, use_container_width=True)

        tab_signals, tab_decisions, tab_trades, tab_replay, tab_sessions = st.tabs(["Signals", "Execution Decisions", "Simulated Trades", "Replay Log", "Sessions"])
        with tab_signals:
            st.dataframe(signals.tail(500), use_container_width=True, hide_index=True) if isinstance(signals, pd.DataFrame) and not signals.empty else st.info("No scanner signals passed the current filters.")
            if isinstance(signals, pd.DataFrame) and not signals.empty:
                st.download_button("Download signals", signals.to_csv(index=False), "strategy_lab_signals.csv", "text/csv")
        with tab_decisions:
            if isinstance(decisions, pd.DataFrame) and not decisions.empty:
                st.caption("Every scanner signal ends here as TRADED, SKIPPED, or REJECTED. This is the execution audit trail.")
                dcols = [c for c in ["decision_no", "timestamp", "strategy", "instrument", "option_dte", "symbol", "signal", "score", "grade", "status", "stage", "reason", "option_expiry", "option_strike", "option_local_symbol", "entry_premium", "entry_price", "stop_price", "target_price", "contract_cost_with_commission", "quantity", "initial_risk", "r_multiple", "sizing_method", "position_allocation_pct", "max_daily_exposure_pct", "buying_power_before", "buying_power_after_entry", "buying_power_after_exit", "max_spend_per_trade", "max_daily_capital", "estimated_cost", "exit_credit", "account_equity", "underlying_move_pct", "option_return_pct", "pricing_model", "raw_delta_return_pct", "model_option_return_pct", "theta_decay_pct", "realized_pnl"] if c in decisions.columns]
                st.dataframe(decisions[dcols].tail(500), use_container_width=True, hide_index=True)
                st.download_button("Download execution decisions", decisions.to_csv(index=False), "strategy_lab_signal_decisions.csv", "text/csv")
            else:
                st.info("No execution decision log yet.")
        with tab_trades:
            if isinstance(trades, pd.DataFrame) and not trades.empty:
                key_cols = [c for c in ["trade_no", "entry_time", "exit_time", "date", "strategy", "instrument", "option_dte", "symbol", "signal", "option_expiry", "option_strike", "option_local_symbol", "score", "grade", "gap_pct", "entry_underlying", "exit_underlying", "entry_price", "exit_price", "stop_price", "target_price", "risk_per_share", "initial_risk", "r_multiple", "underlying_move_pct", "entry_premium", "exit_premium", "option_return_pct", "pricing_model", "quantity", "estimated_cost", "exit_credit", "buying_power_before", "buying_power_after_entry", "buying_power_after_exit", "account_equity", "realized_pnl", "return_pct", "hold_minutes", "exit_reason", "catalyst_status", "reasons"] if c in trades.columns]
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
