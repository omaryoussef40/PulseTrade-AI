from __future__ import annotations

"""Strategy Lab controller for PulseTrade AI.

This module keeps Streamlit UI out of the actual backtest workflow. It is the
single orchestration layer for:
- Yahoo historical data loading/caching
- candle-by-candle market replay
- shared scanner signal generation
- option-style trade simulation
- durable export/reload of results
"""

from dataclasses import dataclass, asdict
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any
import json

import pandas as pd

from .data import YahooDataClient

try:
    from market_data import create_market_data_provider
except Exception:  # pragma: no cover
    create_market_data_provider = None
from .replay import MarketReplayEngine, ReplayConfig
from .strategy import scan_replay_history, clean_signal_row
from .simulator import OptionSimulationConfig, simulate_option_trades_with_decisions, summarize_trades


EXPORT_DIR = Path(__file__).resolve().parent / "exports"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class StrategyLabSettings:
    symbols: list[str]
    period: str = "30d"
    interval: str = "5m"
    force_refresh: bool = False
    orb_minutes: int = 15
    first_signal_minutes: int = 20
    min_session_bars: int = 7
    min_score: float = 70.0
    min_confidence: float = 75.0
    min_rvol: float = 1.5
    min_atr: float = 0.3
    use_rvol_filter: bool = False
    use_rvol_score: bool = False
    starting_capital: float = 1000.0
    max_trades_per_day: int = 2
    sizing_method: str = "percent_equity"
    position_allocation_pct: float = 20.0
    max_daily_exposure_pct: float = 40.0
    # Backward-compatible fixed-dollar fields. Used only when sizing_method is fixed_dollar.
    max_spend_per_trade: float = 250.0
    max_daily_capital: float = 500.0
    recycle_capital_after_exit: bool = False
    reserve_capital_for_remaining_trades: bool = True
    # 0 = no fixed contract cap; Strategy Lab sizes by budget.
    max_contracts: int = 0
    option_dte_values: tuple[int, ...] = (7,)
    stop_loss_pct: float = 20.0
    take_profit_pct: float = 30.0
    breakeven_trigger_pct: float = 15.0
    trailing_trigger_pct: float = 25.0
    trailing_stop_pct: float = 10.0
    entry_cutoff_hour: int = 11
    entry_cutoff_minute: int = 0
    force_exit_enabled: bool = True
    force_exit_hour: int = 15
    force_exit_minute: int = 55
    max_consecutive_losses: int = 2
    max_daily_drawdown_pct: float = 5.0
    premium_pct: float = 0.0025
    slippage_pct: float = 2.0
    allow_same_symbol_same_day: bool = False
    selected_strategies: tuple[str, ...] = ("PMB",)
    data_source: str = "IBKR"


def is_top_candidate_replay(
    result: dict,
    min_score: float,
    min_confidence: float,
    min_rvol: float,
    min_atr: float,
    use_rvol_filter: bool,
) -> bool:
    """Return whether a replay signal is tradable.

    PMB v2 no longer uses Confidence as a filter. The min_confidence argument is
    kept only so older UI/config calls do not break.
    """
    if not result or result.get("Signal") not in ["CALL", "PUT"]:
        return False
    try:
        if float(result.get("Score", 0)) < float(min_score):
            return False
        if bool(use_rvol_filter) and float(result.get("RVOL", 0)) < float(min_rvol):
            return False
        if float(result.get("ATR %", 0)) < float(min_atr):
            return False
    except Exception:
        return False
    return True


def _normalize_strategy_names(values) -> list[str]:
    allowed = {"PMB", "BRT", "PULLBACK", "GAP"}
    out: list[str] = []
    for value in values or ["PMB"]:
        name = str(value).strip().upper().replace(" ", "_")
        if name in {"PULLBACK_REVERSAL", "PBR"}:
            name = "PULLBACK"
        if name in {"GAP_CONTINUATION", "GCS"}:
            name = "GAP"
        if name in allowed and name not in out:
            out.append(name)
    return out or ["PMB"]


def _strategy_display_name(strategy: str) -> str:
    return {
        "PMB": "PMB",
        "BRT": "BRT",
        "PULLBACK": "Pullback",
        "GAP": "Gap",
    }.get(str(strategy).upper(), str(strategy).upper())


def _scan_strategy_replay(strategy_name: str, symbol: str, history: pd.DataFrame, settings: StrategyLabSettings) -> dict | None:
    """Run one strategy against replay history.

    PMB is fully implemented and uses the current Pulse Momentum Breakout rules.
    BRT, Pullback, and Gap are structure-ready placeholders until we code their exact
    entry rules. They intentionally return no trades rather than fake performance.
    """
    strategy_name = str(strategy_name).strip().upper()
    if strategy_name == "PMB":
        result = scan_replay_history(
            symbol=symbol,
            history=history,
            daily=None,
            use_rvol_score=bool(settings.use_rvol_score),
            min_score=float(settings.min_score),
            orb_minutes=int(settings.orb_minutes),
        )
        if result:
            result["Strategy"] = "PMB"
        return result
    return None


class StrategyLabController:
    def __init__(self, export_dir: str | Path | None = None, data_client: YahooDataClient | None = None, data_provider=None):
        self.export_dir = Path(export_dir) if export_dir else EXPORT_DIR
        self.export_dir.mkdir(parents=True, exist_ok=True)
        # data_client is kept for backward compatibility. New code should pass data_provider.
        self.client = data_provider or data_client or YahooDataClient()

    @property
    def provider_name(self) -> str:
        return str(getattr(self.client, "name", self.client.__class__.__name__))

    def cache_info(self) -> pd.DataFrame:
        if hasattr(self.client, "cache_info"):
            try:
                return self.client.cache_info()
            except Exception:
                return pd.DataFrame()
        return pd.DataFrame()

    def clear_cache(self, symbol: str | None = None) -> int:
        if hasattr(self.client, "clear_cache"):
            try:
                return int(self.client.clear_cache(symbol))
            except Exception:
                return 0
        return 0

    @property
    def paths(self) -> dict[str, Path]:
        return {
            "replay": self.export_dir / "strategy_lab_replay.csv",
            "signals": self.export_dir / "strategy_lab_signals.csv",
            "trades": self.export_dir / "strategy_lab_trades.csv",
            "decisions": self.export_dir / "strategy_lab_signal_decisions.csv",
            "sessions": self.export_dir / "strategy_lab_sessions.csv",
            "comparison": self.export_dir / "strategy_lab_comparison.csv",
            "metrics": self.export_dir / "strategy_lab_metrics.json",
            "meta": self.export_dir / "strategy_lab_meta.json",
        }

    def clear_results(self) -> None:
        for path in self.paths.values():
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass

    def load_data(self, settings: StrategyLabSettings) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
        data: dict[str, pd.DataFrame] = {}
        errors = []
        for raw_symbol in settings.symbols:
            symbol = str(raw_symbol).strip().upper()
            if not symbol:
                continue
            try:
                df = self.client.load(
                    symbol=symbol,
                    period=settings.period,
                    interval=settings.interval,
                    force_refresh=settings.force_refresh,
                )
                if df is None or df.empty:
                    errors.append({"symbol": symbol, "error": f"{self.provider_name} returned no candles"})
                else:
                    data[symbol] = df
            except Exception as exc:
                errors.append({"symbol": symbol, "error": str(exc)})
        return data, pd.DataFrame(errors)

    def replay_and_scan(self, data: dict[str, pd.DataFrame], settings: StrategyLabSettings, progress_callback=None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        replay_config = ReplayConfig(
            interval=settings.interval,
            orb_minutes=int(settings.orb_minutes),
            first_signal_minutes=int(settings.first_signal_minutes),
            min_session_bars=int(settings.min_session_bars),
        )
        engine = MarketReplayEngine(data, config=replay_config)
        sessions_df = engine.sessions()
        events = list(engine.events(only_scanner_allowed=False))
        total_events = max(len(events), 1)
        selected_strategies = _normalize_strategy_names(settings.selected_strategies)

        replay_rows: list[dict[str, Any]] = []
        signal_rows: list[dict[str, Any]] = []
        seen_signal_keys: set[tuple[str, str, str, str]] = set()

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
                "strategies": ", ".join(selected_strategies),
            }

            if event.scanner_allowed:
                for strategy_name in selected_strategies:
                    scan_result = _scan_strategy_replay(strategy_name, event.symbol, event.history, settings)
                    clean_scan = clean_signal_row(scan_result)
                    if not clean_scan:
                        continue
                    clean_scan["Strategy"] = strategy_name
                    row.update({
                        f"{strategy_name.lower()}_signal": clean_scan.get("Signal"),
                        f"{strategy_name.lower()}_score": clean_scan.get("Score"),
                        f"{strategy_name.lower()}_grade": clean_scan.get("Grade"),
                        "signal": clean_scan.get("Signal"),
                        "score": clean_scan.get("Score"),
                        "grade": clean_scan.get("Grade"),
                        "rvol": clean_scan.get("RVOL"),
                        "atr_pct": clean_scan.get("ATR %"),
                    })
                    qualifies = is_top_candidate_replay(
                        clean_scan,
                        settings.min_score,
                        settings.min_confidence,
                        settings.min_rvol,
                        settings.min_atr,
                        settings.use_rvol_filter,
                    )
                    if qualifies:
                        signal_key = (strategy_name, event.symbol, str(event.session_date), str(clean_scan.get("Signal")))
                        if signal_key not in seen_signal_keys:
                            seen_signal_keys.add(signal_key)
                            signal_rows.append({
                                "strategy": strategy_name,
                                "strategy_name": _strategy_display_name(strategy_name),
                                "timestamp": event.timestamp,
                                "session_date": event.session_date,
                                "symbol": event.symbol,
                                "signal": clean_scan.get("Signal"),
                                "score": clean_scan.get("Score"),
                                "grade": clean_scan.get("Grade"),
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
                                "score_components": clean_scan.get("Score Components"),
                                "reasons": clean_scan.get("Reasons"),
                            })
            replay_rows.append(row)
            if progress_callback and (idx == 1 or idx == total_events or idx % max(1, total_events // 100) == 0):
                progress_callback(idx, total_events, row, len(signal_rows))

        return pd.DataFrame(replay_rows), pd.DataFrame(signal_rows), sessions_df

    def simulate(self, signals: pd.DataFrame, data: dict[str, pd.DataFrame], settings: StrategyLabSettings) -> tuple[pd.DataFrame, dict, pd.DataFrame, pd.DataFrame]:
        all_trades: list[pd.DataFrame] = []
        all_decisions: list[pd.DataFrame] = []
        comparison_rows: list[dict[str, Any]] = []
        selected_strategies = _normalize_strategy_names(settings.selected_strategies)
        option_dte_values = tuple(int(v) for v in (settings.option_dte_values or (7,)) if int(v) > 0) or (7,)
        option_bars_provider = getattr(self.client, "historical_option_bars", None) if str(settings.data_source).upper() == "IBKR" else None

        if signals is None or signals.empty:
            for strategy_name in selected_strategies:
                comparison_rows.append({
                    "Strategy": _strategy_display_name(strategy_name),
                    "DTE": ", ".join(str(v) for v in option_dte_values),
                    "Status": "No signals" if strategy_name == "PMB" else "Placeholder only",
                    "Trades": 0,
                    "Win %": 0.0,
                    "Avg R": 0.0,
                    "Max DD": 0.0,
                    "Profit Factor": 0.0,
                    "Net P/L": 0.0,
                    "Return %": 0.0,
                })
            comparison = pd.DataFrame(comparison_rows)
            return pd.DataFrame(), {}, pd.DataFrame(), comparison

        for option_dte in option_dte_values:
            sim_config = OptionSimulationConfig(
                starting_capital=float(settings.starting_capital),
                max_trades_per_day=int(settings.max_trades_per_day),
                sizing_method=str(settings.sizing_method),
                position_allocation_pct=float(settings.position_allocation_pct),
                max_daily_exposure_pct=float(settings.max_daily_exposure_pct),
                max_spend_per_trade=float(settings.max_spend_per_trade),
                max_daily_capital=float(settings.max_daily_capital),
                recycle_capital_after_exit=bool(settings.recycle_capital_after_exit),
                reserve_capital_for_remaining_trades=bool(settings.reserve_capital_for_remaining_trades),
                max_contracts=int(settings.max_contracts),
                option_dte=int(option_dte),
                option_bars_provider=option_bars_provider,
                stop_loss_pct=float(settings.stop_loss_pct),
                take_profit_pct=float(settings.take_profit_pct),
                breakeven_trigger_pct=float(settings.breakeven_trigger_pct),
                trailing_trigger_pct=float(settings.trailing_trigger_pct),
                trailing_stop_pct=float(settings.trailing_stop_pct),
                entry_cutoff_time=dtime(int(settings.entry_cutoff_hour), int(settings.entry_cutoff_minute)),
                force_exit_enabled=bool(settings.force_exit_enabled),
                force_exit_time=dtime(int(settings.force_exit_hour), int(settings.force_exit_minute)),
                max_consecutive_losses=int(settings.max_consecutive_losses),
                max_daily_drawdown_pct=float(settings.max_daily_drawdown_pct),
                premium_pct=float(settings.premium_pct),
                slippage_pct=float(settings.slippage_pct),
                allow_same_symbol_same_day=bool(settings.allow_same_symbol_same_day),
            )
            for strategy_name in selected_strategies:
                strategy_signals = signals[signals.get("strategy", "PMB").astype(str).str.upper() == strategy_name].copy() if "strategy" in signals.columns else signals.copy()
                trades, decisions = simulate_option_trades_with_decisions(strategy_signals, data, sim_config)
                if not trades.empty:
                    trades["strategy"] = strategy_name
                    trades["strategy_name"] = _strategy_display_name(strategy_name)
                    trades["option_dte"] = int(option_dte)
                    all_trades.append(trades)
                if not decisions.empty:
                    decisions["strategy"] = strategy_name
                    decisions["strategy_name"] = _strategy_display_name(strategy_name)
                    decisions["option_dte"] = int(option_dte)
                    all_decisions.append(decisions)
                metrics = summarize_trades(trades, starting_capital=float(settings.starting_capital))
                comparison_rows.append({
                    "Strategy": _strategy_display_name(strategy_name),
                    "DTE": int(option_dte),
                    "Status": "Implemented" if strategy_name == "PMB" else "Placeholder only",
                    "Trades": int(metrics.get("total_trades", 0)),
                    "Win %": float(metrics.get("win_rate", 0)),
                    "Avg R": round(float(metrics.get("avg_trade", 0)) / max(abs(float(settings.max_spend_per_trade or 1)), 1.0), 2),
                    "Max DD": float(metrics.get("max_drawdown", 0)),
                    "Profit Factor": metrics.get("profit_factor", 0),
                    "Net P/L": float(metrics.get("net_pnl", 0)),
                    "Return %": float(metrics.get("return_pct", 0)),
                })

        combined_trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
        combined_decisions = pd.concat(all_decisions, ignore_index=True) if all_decisions else pd.DataFrame()
        combined_metrics = summarize_trades(combined_trades, starting_capital=float(settings.starting_capital))
        if isinstance(combined_decisions, pd.DataFrame) and not combined_decisions.empty:
            combined_metrics["total_decisions"] = int(len(combined_decisions))
            combined_metrics["traded_signals"] = int((combined_decisions.get("status", pd.Series(dtype=str)).astype(str) == "TRADED").sum())
            combined_metrics["rejected_signals"] = int((combined_decisions.get("status", pd.Series(dtype=str)).astype(str) == "REJECTED").sum())
            combined_metrics["skipped_signals"] = int((combined_decisions.get("status", pd.Series(dtype=str)).astype(str) == "SKIPPED").sum())
        comparison = pd.DataFrame(comparison_rows)
        return combined_trades, combined_metrics, combined_decisions, comparison

    def run(self, settings: StrategyLabSettings, progress_callback=None) -> dict[str, Any]:
        started_at = datetime.now().isoformat(timespec="seconds")
        data, errors = self.load_data(settings)
        if not data:
            result = {
                "data": data,
                "errors": errors,
                "replay": pd.DataFrame(),
                "signals": pd.DataFrame(),
                "sessions": pd.DataFrame(),
                "trades": pd.DataFrame(),
                "decisions": pd.DataFrame(),
                "comparison": pd.DataFrame(),
                "metrics": {},
                "meta": {"started_at": started_at, "completed_at": datetime.now().isoformat(timespec="seconds"), "status": "NO_DATA"},
            }
            self.save_result(result)
            return result

        replay, signals, sessions = self.replay_and_scan(data, settings, progress_callback=progress_callback)
        trades, metrics, decisions, comparison = self.simulate(signals, data, settings)
        meta = {
            "version": "v0.9.6-pmb-v2",
            "started_at": started_at,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "status": "COMPLETE",
            "symbols": list(data.keys()),
            "period": settings.period,
            "interval": settings.interval,
            "data_source": settings.data_source,
            "provider": self.provider_name,
            "candles": int(sum(len(df) for df in data.values())),
            "replay_events": int(len(replay)),
            "signals": int(len(signals)),
            "trades": int(len(trades)),
            "decisions": int(len(decisions)),
            "strategies": _normalize_strategy_names(settings.selected_strategies),
            "settings": asdict(settings),
        }
        result = {"data": data, "errors": errors, "replay": replay, "signals": signals, "sessions": sessions, "trades": trades, "decisions": decisions, "comparison": comparison, "metrics": metrics, "meta": meta}
        self.save_result(result)
        return result

    def save_result(self, result: dict[str, Any]) -> None:
        paths = self.paths
        frames = {
            "replay": result.get("replay", pd.DataFrame()),
            "signals": result.get("signals", pd.DataFrame()),
            "trades": result.get("trades", pd.DataFrame()),
            "decisions": result.get("decisions", pd.DataFrame()),
            "comparison": result.get("comparison", pd.DataFrame()),
            "sessions": result.get("sessions", pd.DataFrame()),
        }
        for key, frame in frames.items():
            try:
                if isinstance(frame, pd.DataFrame):
                    frame.to_csv(paths[key], index=False)
            except Exception:
                pass
        for key in ["metrics", "meta"]:
            try:
                with paths[key].open("w", encoding="utf-8") as f:
                    json.dump(result.get(key, {}), f, indent=2, default=str)
            except Exception:
                pass

    def load_last_result(self) -> dict[str, Any]:
        paths = self.paths
        out: dict[str, Any] = {}
        for key in ["replay", "signals", "trades", "decisions", "comparison", "sessions"]:
            try:
                out[key] = pd.read_csv(paths[key]) if paths[key].exists() else pd.DataFrame()
            except Exception:
                out[key] = pd.DataFrame()
        for key in ["metrics", "meta"]:
            try:
                if paths[key].exists():
                    with paths[key].open("r", encoding="utf-8") as f:
                        out[key] = json.load(f)
                else:
                    out[key] = {}
            except Exception:
                out[key] = {}
        return out
