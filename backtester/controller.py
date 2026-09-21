from __future__ import annotations

"""Strategy Lab controller for PulseTrade AI.

This module keeps Streamlit UI out of the actual backtest workflow. It is the
single orchestration layer for:
- provider-backed historical data loading
- candle-by-candle market replay
- shared scanner signal generation
- option-style trade simulation
- durable export/reload of results
"""

from dataclasses import dataclass, asdict, replace
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
from .gap_data import load_ibkr_gap_candles
from .gap_simulator import GapStockSimulationConfig, simulate_gap_stock_trades_with_decisions
from .gap_universe import YahooGapUniverseConfig, YahooGapUniverseScanner

from strategies.gap.news import HistoricalCatalystLookup
from strategies.gap.strategy import GapScanConfig, scan_gap_sessions

try:
    from bot_core import opportunity_rank_score, setup_room_check
except Exception:  # pragma: no cover
    opportunity_rank_score = None
    setup_room_check = None


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
    require_break_retest: bool = False
    retest_tolerance_pct: float = 0.10
    retest_max_minutes: int = 45
    use_staged_timeline: bool = False
    min_score: float = 70.0
    min_confidence: float = 75.0
    min_rvol: float = 1.5
    min_atr: float = 0.3
    use_rvol_filter: bool = False
    use_rvol_score: bool = False
    use_rvol_ranking: bool = False
    use_sr_filter: bool = True
    min_sr_room_pct: float = 0.75
    top_n_tickers: int = 2
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
    gap_price_min: float = 3.0
    gap_price_max: float = 15.0
    gap_min_abs_gap_pct: float = 8.0
    gap_min_premarket_volume: int = 500_000
    gap_min_premarket_rvol: float = 3.0
    gap_min_avg_daily_volume: int = 1_000_000
    gap_allow_shorts: bool = True
    gap_risk_per_trade: float = 50.0
    gap_max_capital_per_trade: float = 1_500.0
    gap_max_daily_capital: float = 3_000.0
    gap_max_trades_per_day: int = 2
    gap_stop_buffer_pct: float = 0.25
    gap_min_stop_distance_pct: float = 0.5
    gap_max_stop_distance_pct: float = 6.0
    gap_target_r: float = 2.0
    gap_force_exit_hour: int = 11
    gap_force_exit_minute: int = 30
    gap_slippage_pct: float = 0.10
    gap_commission_per_share: float = 0.005
    gap_minimum_order_commission: float = 1.0
    gap_universe_max_symbols: int = 2_000
    gap_ibkr_request_delay_seconds: float = 0.25
    gap_ibkr_max_retries: int = 2


def is_top_candidate_replay(
    result: dict,
    min_score: float,
    min_confidence: float,
    min_rvol: float,
    min_atr: float,
    use_rvol_filter: bool,
    use_sr_filter: bool = False,
    min_sr_room_pct: float = 0.75,
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
        if bool(use_sr_filter) and setup_room_check is not None:
            room_ok, room_pct, room_note = setup_room_check(result, float(min_sr_room_pct))
            result["Room To Move %"] = round(room_pct, 2) if room_pct is not None else None
            result["Room Check"] = room_note
            if not room_ok:
                return False
    except Exception:
        return False
    return True


def _rank_score_replay(result: dict, use_rvol_ranking: bool) -> float:
    if opportunity_rank_score is not None:
        return float(opportunity_rank_score(result, None, bool(use_rvol_ranking)))
    rvol_component = min(float(result.get("RVOL", 0) or 0) * 10, 30) if use_rvol_ranking else 0
    atr_component = min(float(result.get("ATR %", 0) or 0) * 10, 15)
    return round(float(result.get("Score", 0) or 0) * 0.60 + atr_component + rvol_component, 2)


def _select_live_style_signals(raw_signals: list[dict[str, Any]], settings: StrategyLabSettings) -> pd.DataFrame:
    if not raw_signals:
        return pd.DataFrame()

    df = pd.DataFrame(raw_signals)
    if df.empty:
        return df

    selected_frames: list[pd.DataFrame] = []
    group_cols = ["strategy", "timestamp"] if "strategy" in df.columns else ["timestamp"]

    for _, group in df.groupby(group_cols, dropna=False, sort=True):
        strategy_name = str(group["strategy"].iloc[0]).upper() if "strategy" in group.columns and not group.empty else "PMB"
        opening_stage = "timeline_stage" in group.columns and bool((group["timeline_stage"].astype(str) == "opening_orb_5m").all())
        max_selected = 1 if opening_stage else max(1, int(settings.gap_max_trades_per_day if strategy_name == "GAP" else settings.top_n_tickers or 1))
        ranked = group.copy().sort_values(
            ["rank_score", "score", "rvol", "option_score"],
            ascending=[False, False, False, False],
        )
        ranked["selection_rank"] = range(1, len(ranked) + 1)
        ranked["selection_status"] = ranked["selection_rank"].apply(lambda value: "SELECTED" if int(value) <= max_selected else "NOT_SELECTED")
        selected_frames.append(ranked[ranked["selection_rank"] <= max_selected])

    selected = pd.concat(selected_frames, ignore_index=True) if selected_frames else pd.DataFrame()
    if not selected.empty:
        selected = selected.sort_values(["timestamp", "selection_rank", "symbol"]).reset_index(drop=True)
    return selected


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


def _gap_scan_config(settings: StrategyLabSettings) -> GapScanConfig:
    return GapScanConfig(
        price_min=float(settings.gap_price_min),
        price_max=float(settings.gap_price_max),
        min_abs_gap_pct=float(settings.gap_min_abs_gap_pct),
        min_premarket_volume=int(settings.gap_min_premarket_volume),
        min_premarket_rvol=float(settings.gap_min_premarket_rvol),
        min_avg_daily_volume=int(settings.gap_min_avg_daily_volume),
        allow_gap_up_shorts=bool(settings.gap_allow_shorts),
        allow_gap_down_longs=True,
        require_no_catalyst=True,
        # Unknown news coverage is audited and rejected, never treated as clear.
        allow_unverified_catalyst=False,
    )


def _gap_signal_rows(scanner: pd.DataFrame) -> list[dict[str, Any]]:
    if scanner is None or scanner.empty:
        return []
    qualified = scanner[scanner["status"].astype(str).str.upper() == "QUALIFIED"]
    rows: list[dict[str, Any]] = []
    for _, item in qualified.iterrows():
        rows.append({
            "strategy": "GAP",
            "strategy_name": "Gap",
            "timestamp": item.get("timestamp"),
            "session_date": item.get("session_date"),
            "symbol": item.get("symbol"),
            "signal": item.get("signal"),
            "score": item.get("score"),
            "grade": item.get("grade"),
            "confidence": None,
            "price": item.get("entry_price"),
            "entry_price": item.get("entry_price"),
            "rvol": item.get("premarket_rvol"),
            "premarket_rvol": item.get("premarket_rvol"),
            "premarket_volume": item.get("premarket_volume"),
            "avg_daily_volume": item.get("avg_daily_volume"),
            "gap_pct": item.get("gap_pct"),
            "atr_pct": None,
            "rank_score": item.get("rank_score"),
            "option_score": 0.0,
            "vwap": item.get("opening_vwap"),
            "opening_range_high": item.get("opening_range_high"),
            "opening_range_low": item.get("opening_range_low"),
            "catalyst_status": item.get("catalyst_status"),
            "catalyst_verified": item.get("catalyst_verified"),
            "catalyst_headline": item.get("catalyst_headline"),
            "reasons": item.get("reasons"),
        })
    return rows


def _scan_strategy_replay(strategy_name: str, symbol: str, history: pd.DataFrame, settings: StrategyLabSettings) -> dict | None:
    """Run one strategy against replay history.

    PMB is evaluated candle-by-candle here. GAP is evaluated separately from
    extended-hours data before the regular-hours replay starts. BRT and Pullback
    remain placeholders and intentionally return no trades.
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
            min_session_bars=int(settings.min_session_bars),
            require_retest=bool(settings.require_break_retest),
            retest_tolerance_pct=float(settings.retest_tolerance_pct),
            retest_max_minutes=int(settings.retest_max_minutes),
        )
        if result:
            result["Strategy"] = "PMB"
        return result
    return None


def _staged_replay_settings(timestamp: pd.Timestamp, settings: StrategyLabSettings) -> tuple[str, StrategyLabSettings | None]:
    """Return the live-style PMB stage and its effective replay settings."""
    clock = timestamp.time()
    if clock < dtime(9, 35):
        return "before_opening_orb", None
    if clock < dtime(9, 45):
        return "opening_orb_5m", replace(
            settings,
            orb_minutes=5,
            first_signal_minutes=10,
            min_session_bars=2,
            min_score=94.0,
            min_rvol=1.5,
            use_rvol_filter=True,
            require_break_retest=False,
        )
    if clock < dtime(11, 30):
        return "orb_15m", replace(
            settings,
            orb_minutes=15,
            first_signal_minutes=20,
            min_session_bars=4,
            require_break_retest=False,
        )
    if clock <= dtime(13, 30):
        return "break_retest_15m", replace(
            settings,
            orb_minutes=15,
            first_signal_minutes=20,
            min_session_bars=4,
            require_break_retest=True,
        )
    return "after_retest_window", None


def _staged_event_scanner_allowed(event, stage: str) -> bool:
    if stage == "opening_orb_5m":
        return event.bar_number >= 2 and event.timestamp.time() >= dtime(9, 40)
    if stage in {"orb_15m", "break_retest_15m"}:
        return event.bar_number >= 4 and event.timestamp.time() >= dtime(9, 50)
    return False


def _opening_stage_directional_passes(result: dict) -> bool:
    signal = str(result.get("Signal") or "").upper()
    required = {
        "CALL": ("ORB Up", "PDH Break", "Above VWAP", "EMA Bullish"),
        "PUT": ("ORB Down", "PDL Break", "Below VWAP", "EMA Bearish"),
    }.get(signal)
    return bool(required and all(result.get(key) is True for key in required))


class StrategyLabController:
    def __init__(
        self,
        export_dir: str | Path | None = None,
        data_client: YahooDataClient | None = None,
        data_provider=None,
        gap_universe_scanner=None,
        gap_data_provider=None,
        gap_data_loader=None,
    ):
        self.export_dir = Path(export_dir) if export_dir else EXPORT_DIR
        self.export_dir.mkdir(parents=True, exist_ok=True)
        # data_client is kept for backward compatibility. New code should pass data_provider.
        self.client = data_provider or data_client or YahooDataClient()
        self.gap_universe_scanner = gap_universe_scanner or YahooGapUniverseScanner()
        self.gap_data_provider = gap_data_provider or self.client
        self.gap_data_loader = gap_data_loader or load_ibkr_gap_candles

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

    def disconnect_providers(self) -> None:
        """Release research-only provider connections without double-disconnecting."""
        seen: set[int] = set()
        for provider in [self.gap_data_provider, self.client]:
            if provider is None or id(provider) in seen:
                continue
            seen.add(id(provider))
            disconnect = getattr(provider, "disconnect", None)
            if callable(disconnect):
                try:
                    disconnect()
                except Exception:
                    pass

    @property
    def paths(self) -> dict[str, Path]:
        return {
            "replay": self.export_dir / "strategy_lab_replay.csv",
            "gap_universe": self.export_dir / "strategy_lab_gap_universe.csv",
            "gap_scanner": self.export_dir / "strategy_lab_gap_scanner.csv",
            "errors": self.export_dir / "strategy_lab_data_errors.csv",
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

    def save_no_data_result(self, result: dict[str, Any]) -> None:
        """Record a failed data load without erasing the last useful CSV exports."""
        paths = self.paths
        meta = dict(result.get("meta", {}) or {})
        errors = result.get("errors", pd.DataFrame())
        if isinstance(errors, pd.DataFrame) and not errors.empty:
            meta["errors"] = errors.to_dict(orient="records")
            errors.to_csv(paths["errors"], index=False)
        with paths["meta"].open("w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, default=str)
        with paths["metrics"].open("w", encoding="utf-8") as f:
            json.dump({}, f, indent=2, default=str)

    def load_data(self, settings: StrategyLabSettings, progress_callback=None) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
        data: dict[str, pd.DataFrame] = {}
        errors = []
        total_symbols = max(len(settings.symbols), 1)
        for idx, raw_symbol in enumerate(settings.symbols, start=1):
            symbol = str(raw_symbol).strip().upper()
            if not symbol:
                continue
            if progress_callback:
                progress_callback(
                    idx,
                    total_symbols,
                    {
                        "stage": "Checking candle cache",
                        "symbol": symbol,
                        "timestamp": "",
                    },
                    0,
                )
            try:
                df = self.client.load(
                    symbol=symbol,
                    period=settings.period,
                    interval=settings.interval,
                    regular_hours_only=True,
                    force_refresh=settings.force_refresh,
                )
                if df is None or df.empty:
                    errors.append({"symbol": symbol, "error": f"{self.provider_name} returned no candles for {settings.period} {settings.interval}. Check IBKR/TWS connection, historical data permissions, and the selected port."})
                else:
                    data[symbol] = df
                    if progress_callback:
                        source_getter = getattr(self.client, "last_load_source", None)
                        load_source = source_getter(symbol) if callable(source_getter) else "unknown"
                        stage = {
                            "cache": "Loaded cached candles",
                            "derived_cache": "Built candles from cached 5-minute data",
                            "download": "Downloaded and cached candles",
                            "cache_fallback": "Loaded cached candles after refresh failed",
                        }.get(str(load_source), "Loaded candles")
                        progress_callback(
                            idx,
                            total_symbols,
                            {"stage": stage, "symbol": symbol, "timestamp": ""},
                            0,
                        )
            except Exception as exc:
                message = str(exc).strip() or repr(exc)
                errors.append({"symbol": symbol, "error": message})
        return data, pd.DataFrame(errors)

    def load_gap_market(self, settings: StrategyLabSettings, progress_callback=None) -> tuple[pd.DataFrame, dict[str, pd.DataFrame], pd.DataFrame]:
        universe_config = YahooGapUniverseConfig(
            price_min=float(settings.gap_price_min),
            price_max=float(settings.gap_price_max),
            min_avg_daily_volume=int(settings.gap_min_avg_daily_volume),
            max_symbols=int(settings.gap_universe_max_symbols),
        )
        universe = self.gap_universe_scanner.scan(universe_config)
        symbols = universe.get("symbol", pd.Series(dtype=str)).astype(str).tolist() if not universe.empty else []
        if not symbols:
            return universe, {}, pd.DataFrame([{
                "symbol": "MARKET",
                "error": "Yahoo screener returned no eligible stocks",
                "source": "Yahoo universe",
            }])
        data, errors = self.gap_data_loader(
            self.gap_data_provider,
            symbols,
            period=settings.period,
            interval=settings.interval,
            request_delay_seconds=float(settings.gap_ibkr_request_delay_seconds),
            max_retries=int(settings.gap_ibkr_max_retries),
            force_refresh=True,
            progress_callback=progress_callback,
        )
        return universe, data, errors

    def scan_gap_data(self, data: dict[str, pd.DataFrame], settings: StrategyLabSettings) -> pd.DataFrame:
        if "GAP" not in _normalize_strategy_names(settings.selected_strategies):
            return pd.DataFrame()
        catalyst_lookup = HistoricalCatalystLookup(min_impact=0, min_window_articles=5)
        return scan_gap_sessions(data, config=_gap_scan_config(settings), catalyst_lookup=catalyst_lookup)

    def replay_and_scan(self, data: dict[str, pd.DataFrame], settings: StrategyLabSettings, progress_callback=None, gap_scanner: pd.DataFrame | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        if settings.use_staged_timeline and str(settings.interval).lower() != "5m":
            raise ValueError("Live staged timeline requires 5-minute candles.")
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
        raw_signal_rows: list[dict[str, Any]] = []
        entry_cutoff_time = dtime(int(settings.entry_cutoff_hour), int(settings.entry_cutoff_minute))

        for idx, event in enumerate(events, start=1):
            timeline_stage = "fixed"
            event_settings = settings
            scanner_allowed = bool(event.scanner_allowed and event.timestamp.time() < entry_cutoff_time)
            if settings.use_staged_timeline:
                timeline_stage, staged_settings = _staged_replay_settings(event.timestamp, settings)
                event_settings = staged_settings or settings
                scanner_allowed = staged_settings is not None and _staged_event_scanner_allowed(event, timeline_stage)
            row = {
                "#": idx,
                "symbol": event.symbol,
                "timestamp": event.timestamp,
                "session_date": event.session_date,
                "bar_number": event.bar_number,
                "close": round(event.close, 2),
                "scanner_allowed": scanner_allowed,
                "timeline_stage": timeline_stage,
                "new_session": event.is_new_session,
                "session_close": event.is_session_close,
                "strategies": ", ".join(selected_strategies),
            }

            if scanner_allowed:
                for strategy_name in [name for name in selected_strategies if name != "GAP"]:
                    scan_result = _scan_strategy_replay(strategy_name, event.symbol, event.history, event_settings)
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
                        event_settings.min_score,
                        event_settings.min_confidence,
                        event_settings.min_rvol,
                        event_settings.min_atr,
                        event_settings.use_rvol_filter,
                        event_settings.use_sr_filter,
                        event_settings.min_sr_room_pct,
                    )
                    if timeline_stage == "opening_orb_5m":
                        qualifies = bool(qualifies and _opening_stage_directional_passes(clean_scan))
                    if qualifies:
                        raw_signal_rows.append({
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
                            "rank_score": _rank_score_replay(clean_scan, settings.use_rvol_ranking),
                            "option_score": 0.0,
                            "timeline_stage": timeline_stage,
                            "stop_loss_pct": 5.0 if timeline_stage == "opening_orb_5m" else float(settings.stop_loss_pct),
                            "vwap": clean_scan.get("VWAP"),
                            "orb_high": clean_scan.get("ORB High"),
                            "orb_low": clean_scan.get("ORB Low"),
                            "pdh": clean_scan.get("PDH"),
                            "pdl": clean_scan.get("PDL"),
                            "pdh_method": clean_scan.get("PDH Method"),
                            "room_to_move_pct": clean_scan.get("Room To Move %"),
                            "room_check": clean_scan.get("Room Check"),
                            "score_components": clean_scan.get("Score Components"),
                            "reasons": clean_scan.get("Reasons"),
                        })
            replay_rows.append(row)
            if progress_callback and (idx == 1 or idx == total_events or idx % max(1, total_events // 100) == 0):
                progress_callback(idx, total_events, row, len(raw_signal_rows))

        raw_signal_rows.extend(_gap_signal_rows(gap_scanner if gap_scanner is not None else pd.DataFrame()))
        selected_signals = _select_live_style_signals(raw_signal_rows, settings)
        return pd.DataFrame(replay_rows), selected_signals, sessions_df

    def simulate(
        self,
        signals: pd.DataFrame,
        data: dict[str, pd.DataFrame],
        settings: StrategyLabSettings,
        gap_data: dict[str, pd.DataFrame] | None = None,
    ) -> tuple[pd.DataFrame, dict, pd.DataFrame, pd.DataFrame]:
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
                    "Instrument": "Stock" if strategy_name == "GAP" else "Option",
                    "DTE": "Stock" if strategy_name == "GAP" else ", ".join(str(v) for v in option_dte_values),
                    "Status": "No qualifying gaps" if strategy_name == "GAP" else "No signals" if strategy_name == "PMB" else "Placeholder only",
                    "Trades": 0,
                    "Win %": 0.0,
                    "Avg R": 0.0,
                    "Max DD": 0.0,
                    "Profit Factor": 0.0,
                    "Net P/L": 0.0,
                    "Return %": 0.0,
                })
            comparison = pd.DataFrame(comparison_rows)
            return pd.DataFrame(), summarize_trades(pd.DataFrame(), float(settings.starting_capital)), pd.DataFrame(), comparison

        if "PMB" in selected_strategies:
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
                    entry_cutoff_time=(
                        dtime(13, 30)
                        if settings.use_staged_timeline
                        else dtime(int(settings.entry_cutoff_hour), int(settings.entry_cutoff_minute))
                    ),
                    force_exit_enabled=bool(settings.force_exit_enabled),
                    force_exit_time=dtime(int(settings.force_exit_hour), int(settings.force_exit_minute)),
                    max_consecutive_losses=int(settings.max_consecutive_losses),
                    max_daily_drawdown_pct=float(settings.max_daily_drawdown_pct),
                    premium_pct=float(settings.premium_pct),
                    slippage_pct=float(settings.slippage_pct),
                    allow_same_symbol_same_day=bool(settings.allow_same_symbol_same_day),
                )
                strategy_signals = signals[signals["strategy"].astype(str).str.upper() == "PMB"].copy() if "strategy" in signals.columns else signals.copy()
                trades, decisions = simulate_option_trades_with_decisions(strategy_signals, data, sim_config)
                if not trades.empty:
                    trades["strategy"] = "PMB"
                    trades["strategy_name"] = "PMB"
                    trades["instrument"] = "OPTION"
                    trades["option_dte"] = int(option_dte)
                    all_trades.append(trades)
                if not decisions.empty:
                    decisions["strategy"] = "PMB"
                    decisions["strategy_name"] = "PMB"
                    decisions["instrument"] = "OPTION"
                    decisions["option_dte"] = int(option_dte)
                    all_decisions.append(decisions)
                metrics = summarize_trades(trades, starting_capital=float(settings.starting_capital))
                comparison_rows.append({
                    "Strategy": "PMB",
                    "Instrument": "Option",
                    "DTE": int(option_dte),
                    "Status": "Implemented",
                    "Trades": int(metrics.get("total_trades", 0)),
                    "Win %": float(metrics.get("win_rate", 0)),
                    "Avg R": round(float(metrics.get("avg_trade", 0)) / max(abs(float(settings.max_spend_per_trade or 1)), 1.0), 2),
                    "Max DD": float(metrics.get("max_drawdown", 0)),
                    "Profit Factor": metrics.get("profit_factor", 0),
                    "Net P/L": float(metrics.get("net_pnl", 0)),
                    "Return %": float(metrics.get("return_pct", 0)),
                })

        if "GAP" in selected_strategies:
            gap_signals = signals[signals["strategy"].astype(str).str.upper() == "GAP"].copy() if "strategy" in signals.columns else pd.DataFrame()
            gap_config = GapStockSimulationConfig(
                starting_capital=float(settings.starting_capital),
                risk_per_trade=float(settings.gap_risk_per_trade),
                max_capital_per_trade=float(settings.gap_max_capital_per_trade),
                max_daily_capital=float(settings.gap_max_daily_capital),
                max_trades_per_day=int(settings.gap_max_trades_per_day),
                stop_buffer_pct=float(settings.gap_stop_buffer_pct),
                min_stop_distance_pct=float(settings.gap_min_stop_distance_pct),
                max_stop_distance_pct=float(settings.gap_max_stop_distance_pct),
                target_r=float(settings.gap_target_r),
                force_exit_time=dtime(int(settings.gap_force_exit_hour), int(settings.gap_force_exit_minute)),
                slippage_pct=float(settings.gap_slippage_pct),
                commission_per_share=float(settings.gap_commission_per_share),
                minimum_order_commission=float(settings.gap_minimum_order_commission),
                allow_same_symbol_same_day=bool(settings.allow_same_symbol_same_day),
            )
            gap_trades, gap_decisions = simulate_gap_stock_trades_with_decisions(gap_signals, gap_data or data, gap_config)
            if not gap_trades.empty:
                all_trades.append(gap_trades)
            if not gap_decisions.empty:
                all_decisions.append(gap_decisions)
            gap_metrics = summarize_trades(gap_trades, starting_capital=float(settings.starting_capital))
            avg_r = float(pd.to_numeric(gap_trades.get("r_multiple"), errors="coerce").mean()) if not gap_trades.empty else 0.0
            comparison_rows.append({
                "Strategy": "Gap",
                "Instrument": "Stock",
                "DTE": "Stock",
                "Status": "Research only",
                "Trades": int(gap_metrics.get("total_trades", 0)),
                "Win %": float(gap_metrics.get("win_rate", 0)),
                "Avg R": round(avg_r, 2),
                "Max DD": float(gap_metrics.get("max_drawdown", 0)),
                "Profit Factor": gap_metrics.get("profit_factor", 0),
                "Net P/L": float(gap_metrics.get("net_pnl", 0)),
                "Return %": float(gap_metrics.get("return_pct", 0)),
            })

        for strategy_name in [name for name in selected_strategies if name in {"BRT", "PULLBACK"}]:
            comparison_rows.append({
                "Strategy": _strategy_display_name(strategy_name),
                "Instrument": "Option",
                "DTE": ", ".join(str(v) for v in option_dte_values),
                "Status": "Placeholder only",
                "Trades": 0,
                "Win %": 0.0,
                "Avg R": 0.0,
                "Max DD": 0.0,
                "Profit Factor": 0.0,
                "Net P/L": 0.0,
                "Return %": 0.0,
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

    def run(self, settings: StrategyLabSettings, progress_callback=None, save: bool = True) -> dict[str, Any]:
        started_at = datetime.now().isoformat(timespec="seconds")
        selected_strategies = _normalize_strategy_names(settings.selected_strategies)
        needs_base_data = any(name != "GAP" for name in selected_strategies)
        base_data: dict[str, pd.DataFrame] = {}
        base_errors = pd.DataFrame()
        if needs_base_data:
            base_data, base_errors = self.load_data(settings, progress_callback=progress_callback)

        gap_universe = pd.DataFrame()
        gap_data: dict[str, pd.DataFrame] = {}
        gap_errors = pd.DataFrame()
        if "GAP" in selected_strategies:
            try:
                gap_universe, gap_data, gap_errors = self.load_gap_market(settings, progress_callback=progress_callback)
            except Exception as exc:
                gap_errors = pd.DataFrame([{
                    "symbol": "MARKET",
                    "error": f"GAP market load failed: {exc}",
                    "source": "GAP market",
                }])

        error_frames = []
        if not base_errors.empty:
            tagged = base_errors.copy()
            tagged["source"] = str(settings.data_source)
            error_frames.append(tagged)
        if not gap_errors.empty:
            tagged = gap_errors.copy()
            if "source" not in tagged.columns:
                tagged["source"] = "IBKR GAP candles"
            else:
                tagged["source"] = tagged["source"].fillna("IBKR GAP candles")
            error_frames.append(tagged)
        errors = pd.concat(error_frames, ignore_index=True) if error_frames else pd.DataFrame()

        if not base_data and not gap_data:
            result = {
                "data": {},
                "errors": errors,
                "replay": pd.DataFrame(),
                "gap_universe": gap_universe,
                "gap_scanner": pd.DataFrame(),
                "signals": pd.DataFrame(),
                "sessions": pd.DataFrame(),
                "trades": pd.DataFrame(),
                "decisions": pd.DataFrame(),
                "comparison": pd.DataFrame(),
                "metrics": {},
                "meta": {"started_at": started_at, "completed_at": datetime.now().isoformat(timespec="seconds"), "status": "NO_DATA"},
            }
            if save:
                self.save_no_data_result(result)
            return result

        gap_scanner = self.scan_gap_data(gap_data, settings)
        if base_data:
            replay, signals, sessions = self.replay_and_scan(
                base_data,
                settings,
                progress_callback=progress_callback,
                gap_scanner=gap_scanner,
            )
        else:
            signals = _select_live_style_signals(_gap_signal_rows(gap_scanner), settings)
            replay = gap_scanner.copy()
            if not replay.empty:
                replay["close"] = replay.get("entry_price")
                replay["scanner_allowed"] = replay.get("status", "").astype(str) == "QUALIFIED"
                replay["strategies"] = "GAP"
            session_cols = [col for col in ["symbol", "session_date", "status", "gap_pct", "premarket_volume", "premarket_rvol", "catalyst_status"] if col in gap_scanner.columns]
            sessions = gap_scanner[session_cols].copy() if session_cols else pd.DataFrame()

        simulation_data = base_data or gap_data
        trades, metrics, decisions, comparison = self.simulate(signals, simulation_data, settings, gap_data=gap_data)
        result_data = dict(base_data)
        result_data.update(gap_data)
        populated_frames = [df for df in result_data.values() if isinstance(df, pd.DataFrame) and not df.empty]
        data_start = min(df.index.min() for df in populated_frames).isoformat() if populated_frames else None
        data_end = max(df.index.max() for df in populated_frames).isoformat() if populated_frames else None
        meta = {
            "version": "v0.10.3-close-time-replay",
            "started_at": started_at,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "status": "COMPLETE_WITH_DATA_ERRORS" if not errors.empty else "COMPLETE",
            "symbols": list(base_data.keys()),
            "gap_universe_count": int(len(gap_universe)),
            "gap_candle_symbols": int(len(gap_data)),
            "gap_candle_coverage_pct": round(100.0 * len(gap_data) / max(len(gap_universe), 1), 2),
            "period": settings.period,
            "interval": settings.interval,
            "data_source": settings.data_source,
            "gap_universe_source": "Yahoo",
            "gap_data_source": "IBKR",
            "data_errors": int(len(errors)),
            "provider": self.provider_name,
            "candles": int(sum(len(df) for df in result_data.values())),
            "data_start": data_start,
            "data_end": data_end,
            "replay_events": int(len(replay)),
            "bar_timestamp_semantics": "completed_at_close",
            "gap_scanner_rows": int(len(gap_scanner)),
            "gap_qualified": int((gap_scanner.get("status", pd.Series(dtype=str)).astype(str) == "QUALIFIED").sum()) if not gap_scanner.empty else 0,
            "signals": int(len(signals)),
            "trades": int(len(trades)),
            "decisions": int(len(decisions)),
            "strategies": selected_strategies,
            "settings": asdict(settings),
        }
        result = {"data": result_data, "errors": errors, "replay": replay, "gap_universe": gap_universe, "gap_scanner": gap_scanner, "signals": signals, "sessions": sessions, "trades": trades, "decisions": decisions, "comparison": comparison, "metrics": metrics, "meta": meta}
        if save:
            self.save_result(result)
        return result

    def save_result(self, result: dict[str, Any]) -> None:
        paths = self.paths
        frames = {
            "replay": result.get("replay", pd.DataFrame()),
            "gap_universe": result.get("gap_universe", pd.DataFrame()),
            "gap_scanner": result.get("gap_scanner", pd.DataFrame()),
            "errors": result.get("errors", pd.DataFrame()),
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
        for key in ["replay", "gap_universe", "gap_scanner", "errors", "signals", "trades", "decisions", "comparison", "sessions"]:
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
        if str(out.get("meta", {}).get("status", "")).upper() == "NO_DATA":
            for key in ["replay", "gap_universe", "gap_scanner", "signals", "trades", "decisions", "comparison", "sessions"]:
                out[key] = pd.DataFrame()
        return out
