# engine.py
# Headless runner for AutoTrader. Keep this running on the VPS.

from __future__ import annotations

import atexit
import csv
import json
import os
import subprocess
import time
import traceback
from datetime import datetime, time as dtime, timedelta
from html import escape
from pathlib import Path

import pandas as pd
import requests

from modules.premarket_watchlist import build_premarket_watchlist, combined_watchlist, dynamic_config, normalize_symbols, should_auto_build
from market_data import create_market_data_provider
from backtester.controller import StrategyLabController, StrategyLabSettings
from bot_core import (
    EASTERN,
    IBConfig,
    TelegramConfig,
    add_active_position_from_entry,
    app_log,
    calculate_contract_quantity,
    clean_for_table,
    connect_ib,
    broker_open_option_symbols,
    get_open_position_deployed,
    get_today_loss_stats,
    get_today_trade_stats,
    ib_port_from_config,
    is_market_open_now,
    is_top_candidate,
    load_config,
    log_alert,
    log_trade,
    make_alert_message,
    manage_open_positions,
    opportunity_rank_score,
    orders_unlocked_from_config,
    place_option_order,
    read_active_positions,
    recommend_option_ib,
    reconstruct_option_contract,
    scan_symbol_ib,
    send_position_closed_telegram_message,
    send_telegram_message,
    save_trade_replay,
    sync_active_positions_from_broker,
    sync_today_executions_to_trade_log,
    trade_fill_details,
    write_active_positions,
    write_health,
)


BASE_DIR = Path(__file__).resolve().parent
EXPORT_DIR = BASE_DIR / "exports"
DATA_DIR = BASE_DIR / "data"
ENGINE_PID_FILE = DATA_DIR / "trading_engine.pid"
PENDING_APPROVALS_FILE = EXPORT_DIR / "pending_order_approvals.json"
TELEGRAM_APPROVAL_STATE_FILE = EXPORT_DIR / "telegram_approval_state.json"
ENGINE_DECISIONS_FILE = EXPORT_DIR / "engine_decisions.csv"
CURRENT_SCAN_CANDIDATES_FILE = EXPORT_DIR / "current_scan_candidates.json"
BACKTESTER_CACHE_WARMUP_STATE_FILE = DATA_DIR / "backtester_cache_warmup_state.json"
OPENING_ORB_TRADE_STATE_FILE = DATA_DIR / "opening_orb_trade_state.json"
OPENING_ORB_SOURCE = "opening_orb"
OPENING_ORB_ACTIVE_STATUSES = {"PENDING_APPROVAL", "ORDER_SUBMITTED", "OPEN"}
OPENING_ORB_TERMINAL_STATUSES = {
    "COMPLETE",
    "DECLINED",
    "FAILED",
    "NOT_FILLED",
    "SIGNAL_ONLY",
    "SKIPPED_ACTIVE_POSITION",
    "SKIPPED_DAILY_LIMIT",
    "SKIPPED_EXISTING_TRADE",
    "WINDOW_EXPIRED",
}


def notify_position_close_events(tg_cfg: TelegramConfig, events: list[dict] | None) -> int:
    sent = 0
    for event in events or []:
        try:
            if send_position_closed_telegram_message(tg_cfg, event):
                sent += 1
        except Exception as exc:
            app_log(f"Telegram close alert failed for {event.get('Symbol')}: {exc}", "WARN")
    return sent


def read_backtester_cache_warmup_state() -> dict:
    try:
        if BACKTESTER_CACHE_WARMUP_STATE_FILE.exists():
            with BACKTESTER_CACHE_WARMUP_STATE_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def write_backtester_cache_warmup_state(state: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = BACKTESTER_CACHE_WARMUP_STATE_FILE.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
        tmp.replace(BACKTESTER_CACHE_WARMUP_STATE_FILE)
    except Exception as exc:
        app_log(f"Backtester cache warmup state write failed: {exc}", "WARN")


def backtester_cache_warmup_symbols(cfg: dict) -> list[str]:
    automation = cfg.get("automation", {}) if isinstance(cfg.get("automation", {}), dict) else {}
    lab_cfg = cfg.get("strategy_lab", {}) if isinstance(cfg.get("strategy_lab", {}), dict) else {}
    dyn_cfg = dynamic_config(cfg)
    max_symbols = max(1, int(automation.get("backtester_cache_warmup_max_symbols", 30) or 30))
    symbols = normalize_symbols(
        list(cfg.get("watchlist", []) or [])
        + list(lab_cfg.get("symbols", []) or [])
        + list(dyn_cfg.get("source_universe", []) or [])
    )
    return symbols[:max_symbols]


def build_backtester_cache_warmup_settings(cfg: dict, symbols: list[str]) -> StrategyLabSettings:
    strategy = cfg.get("strategy", {}) if isinstance(cfg.get("strategy", {}), dict) else {}
    risk = cfg.get("risk", {}) if isinstance(cfg.get("risk", {}), dict) else {}
    lab_cfg = cfg.get("strategy_lab", {}) if isinstance(cfg.get("strategy_lab", {}), dict) else {}
    automation = cfg.get("automation", {}) if isinstance(cfg.get("automation", {}), dict) else {}
    raw_dtes = automation.get("backtester_cache_warmup_option_dte_values")
    if not raw_dtes:
        raw_dtes = list(lab_cfg.get("option_dte_values") or []) + [strategy.get("option_dte", 7), 7, 14]
    dte_set: set[int] = set()
    for raw_dte in raw_dtes:
        try:
            dte = int(raw_dte)
        except Exception:
            continue
        if dte > 0:
            dte_set.add(dte)
    dtes = sorted(dte_set) or [int(strategy.get("option_dte", 7) or 7)]
    return StrategyLabSettings(
        symbols=symbols,
        period=str(automation.get("backtester_cache_warmup_period", "60d") or "60d"),
        interval=str(lab_cfg.get("interval", automation.get("backtester_cache_warmup_interval", "5m")) or "5m"),
        force_refresh=bool(automation.get("backtester_cache_warmup_force_refresh", False)),
        orb_minutes=int(lab_cfg.get("orb_minutes", strategy.get("orb_minutes", 15))),
        first_signal_minutes=int(lab_cfg.get("first_signal_minutes", strategy.get("first_signal_minutes", 20))),
        min_session_bars=int(lab_cfg.get("min_session_bars", strategy.get("min_session_bars", 7))),
        min_score=float(lab_cfg.get("min_score", strategy.get("min_score", 70))),
        min_confidence=float(lab_cfg.get("min_confidence", strategy.get("min_confidence", 70))),
        min_rvol=float(lab_cfg.get("min_rvol", strategy.get("min_rvol", 1.5))),
        min_atr=float(lab_cfg.get("min_atr", strategy.get("min_atr", 0.3))),
        use_rvol_filter=bool(lab_cfg.get("use_rvol_filter", strategy.get("use_rvol_filter", False))),
        use_rvol_score=bool(lab_cfg.get("use_rvol_score", strategy.get("use_rvol_score", False))),
        use_rvol_ranking=bool(lab_cfg.get("use_rvol_ranking", strategy.get("use_rvol_ranking", False))),
        use_sr_filter=bool(lab_cfg.get("use_sr_filter", strategy.get("use_sr_filter", True))),
        min_sr_room_pct=float(lab_cfg.get("min_sr_room_pct", strategy.get("min_sr_room_pct", 0.75))),
        top_n_tickers=len(symbols),
        starting_capital=max(float(lab_cfg.get("account_size", risk.get("account_size", 1000)) or 1000), 1_000_000.0),
        max_trades_per_day=max(len(symbols) * max(len(dtes), 1), int(lab_cfg.get("max_trades_per_day", 2))),
        sizing_method="percent_equity",
        position_allocation_pct=1.0,
        max_daily_exposure_pct=100.0,
        max_spend_per_trade=1_000_000.0,
        max_daily_capital=1_000_000.0,
        recycle_capital_after_exit=True,
        reserve_capital_for_remaining_trades=False,
        max_contracts=0,
        option_dte_values=tuple(dtes),
        stop_loss_pct=float(lab_cfg.get("stop_loss_pct", risk.get("stop_loss_pct", 20.0))),
        take_profit_pct=float(lab_cfg.get("take_profit_pct", risk.get("take_profit_pct", 30.0))),
        breakeven_trigger_pct=float(lab_cfg.get("breakeven_trigger_pct", risk.get("breakeven_trigger_pct", 15.0))),
        trailing_trigger_pct=float(lab_cfg.get("trailing_trigger_pct", risk.get("trailing_trigger_pct", 25.0))),
        trailing_stop_pct=float(lab_cfg.get("trailing_stop_pct", risk.get("trailing_stop_pct", 10.0))),
        entry_cutoff_hour=int(lab_cfg.get("entry_cutoff_hour", risk.get("entry_cutoff_hour", 11))),
        entry_cutoff_minute=int(lab_cfg.get("entry_cutoff_minute", risk.get("entry_cutoff_minute", 0))),
        force_exit_enabled=bool(lab_cfg.get("force_exit_enabled", risk.get("force_exit_enabled", True))),
        force_exit_hour=int(lab_cfg.get("force_exit_hour", risk.get("force_exit_hour", 15))),
        force_exit_minute=int(lab_cfg.get("force_exit_minute", risk.get("force_exit_minute", 55))),
        max_consecutive_losses=999,
        max_daily_drawdown_pct=100.0,
        premium_pct=float(lab_cfg.get("premium_pct_ui", 0.25)) / 100.0,
        slippage_pct=float(lab_cfg.get("slippage_pct", 2.0)),
        allow_same_symbol_same_day=True,
        selected_strategies=("PMB",),
        data_source="IBKR",
    )


def run_backtester_cache_warmup_if_due() -> None:
    cfg = load_config()
    automation = cfg.get("automation", {}) if isinstance(cfg.get("automation", {}), dict) else {}
    if not bool(automation.get("enabled", False)):
        return
    if not bool(automation.get("backtester_cache_warmup_enabled", True)):
        return

    now_et = datetime.now(EASTERN)
    if now_et.weekday() >= 5:
        return
    warmup_time = dtime(
        int(automation.get("backtester_cache_warmup_hour", 16)),
        int(automation.get("backtester_cache_warmup_minute", 10)),
    )
    if now_et.time() < warmup_time:
        return

    state = read_backtester_cache_warmup_state()
    today_key = now_et.date().isoformat()
    if state.get("date") == today_key and state.get("status") == "COMPLETE":
        return
    last_attempt = pd.to_datetime(state.get("last_attempt_at"), errors="coerce")
    if pd.notna(last_attempt):
        if last_attempt.tzinfo is None:
            last_attempt = last_attempt.tz_localize(EASTERN)
        else:
            last_attempt = last_attempt.tz_convert(EASTERN)
        retry_minutes = max(5, int(automation.get("backtester_cache_warmup_retry_minutes", 30)))
        if state.get("date") == today_key and now_et < last_attempt.to_pydatetime() + timedelta(minutes=retry_minutes):
            return

    symbols = backtester_cache_warmup_symbols(cfg)
    if not symbols:
        return

    write_backtester_cache_warmup_state({
        "date": today_key,
        "status": "RUNNING",
        "last_attempt_at": now_et.isoformat(),
        "symbols": symbols,
    })
    app_log(f"Backtester IBKR cache warmup started | symbols={len(symbols)} | first={symbols[:5]}")
    provider = None
    try:
        provider = create_market_data_provider("IBKR", cfg, client_id_offset=260, readonly_override=True)
        controller = StrategyLabController(data_provider=provider)
        settings = build_backtester_cache_warmup_settings(cfg, symbols)
        result = controller.run(settings, save=False)
        meta = result.get("meta", {}) if isinstance(result, dict) else {}
        errors = result.get("errors", pd.DataFrame()) if isinstance(result, dict) else pd.DataFrame()
        state = {
            "date": today_key,
            "status": "COMPLETE",
            "completed_at": datetime.now(EASTERN).isoformat(),
            "symbols": symbols,
            "period": settings.period,
            "interval": settings.interval,
            "option_dte_values": list(settings.option_dte_values),
            "candles": int(meta.get("candles", 0) or 0),
            "signals": int(meta.get("signals", 0) or 0),
            "decisions": int(meta.get("decisions", 0) or 0),
            "data_errors": int(len(errors)) if isinstance(errors, pd.DataFrame) else int(meta.get("data_errors", 0) or 0),
        }
        write_backtester_cache_warmup_state(state)
        app_log(f"Backtester IBKR cache warmup complete | candles={state['candles']} | signals={state['signals']} | decisions={state['decisions']} | errors={state['data_errors']}")
    except Exception as exc:
        write_backtester_cache_warmup_state({
            "date": today_key,
            "status": "FAILED",
            "last_attempt_at": now_et.isoformat(),
            "error": str(exc),
            "symbols": symbols,
        })
        app_log(f"Backtester IBKR cache warmup failed: {exc}", "WARN")
    finally:
        disconnect = getattr(provider, "disconnect", None)
        if callable(disconnect):
            try:
                disconnect()
            except Exception:
                pass


def request_eod_exit_approvals(tg_cfg: TelegramConfig, events: list[dict] | None, mode: str) -> list[dict]:
    requested = []
    for event in events or []:
        if str(event.get("Action") or "").upper() != "EXIT_APPROVAL_REQUIRED":
            continue
        position = event.get("Position") if isinstance(event.get("Position"), dict) else {}
        if not position:
            continue
        order, created = create_pending_exit_approval(
            position,
            event.get("Current"),
            str(event.get("Reason") or "End-of-day close approval"),
            mode,
            approval_mode="Telegram",
        )
        sent = False
        if created:
            try:
                sent = send_order_approval_message(tg_cfg, order)
            except Exception as exc:
                update_pending_approval(order["id"], status="approval_send_failed", error=str(exc))
                app_log(f"{position.get('symbol')}: EOD exit approval send failed | id={order.get('id')} | {exc}", "WARN")
        requested.append({
            "Symbol": position.get("symbol"),
            "Option": position.get("option"),
            "Action": "EXIT_APPROVAL_SENT" if sent else "EXIT_APPROVAL_PENDING",
            "Approval ID": order.get("id"),
            "Created": created,
        })
    return requested


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
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


def claim_single_engine_instance() -> bool:
    DATA_DIR.mkdir(exist_ok=True)
    current_pid = os.getpid()
    try:
        existing_pid = int(ENGINE_PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        existing_pid = 0

    if existing_pid and existing_pid != current_pid and _is_pid_running(existing_pid) and _is_engine_process(existing_pid):
        message = f"Engine already running as PID {existing_pid}; refusing duplicate PID {current_pid}."
        app_log(message, "WARN")
        write_health(engine_running=True, last_status="Duplicate engine refused", last_error=message)
        return False

    ENGINE_PID_FILE.write_text(str(current_pid), encoding="utf-8")

    def _cleanup_pid_file() -> None:
        try:
            if ENGINE_PID_FILE.read_text(encoding="utf-8").strip() == str(current_pid):
                ENGINE_PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    atexit.register(_cleanup_pid_file)
    return True


def next_aligned_scan_time(
    now_dt: datetime,
    scan_interval_seconds: int,
    first_scan_time: dtime | None = None,
) -> datetime:
    """Return the next wall-clock scan boundary for completed-bar style scans."""
    if scan_interval_seconds < 60 or scan_interval_seconds % 60 != 0:
        return now_dt

    if first_scan_time is not None:
        first_scan_dt = now_dt.replace(
            hour=first_scan_time.hour,
            minute=first_scan_time.minute,
            second=0,
            microsecond=0,
        )
        if now_dt <= first_scan_dt or 0 <= (now_dt - first_scan_dt).total_seconds() <= 90:
            return first_scan_dt

    interval_minutes = max(1, scan_interval_seconds // 60)
    current_block_minute = (now_dt.minute // interval_minutes) * interval_minutes
    current_boundary = now_dt.replace(minute=current_block_minute, second=0, microsecond=0)

    # If the engine starts right after a boundary, still allow that scan.
    if 0 <= (now_dt - current_boundary).total_seconds() <= 90:
        return current_boundary

    next_boundary = current_boundary + timedelta(minutes=interval_minutes)
    while next_boundary <= now_dt:
        next_boundary += timedelta(minutes=interval_minutes)
    return next_boundary


def scan_result_session_date(result: dict):
    raw = str(result.get("ORB Confirmation Time", "")).strip()
    if len(raw) < 10:
        return None
    try:
        return datetime.fromisoformat(raw[:10]).date()
    except Exception:
        return None


def read_json_file(path: Path, default):
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json_file(path: Path, data) -> None:
    EXPORT_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    tmp.replace(path)


def staged_trading_timeline_config(cfg: dict) -> dict:
    raw = cfg.get("staged_trading_timeline", {})
    return raw if isinstance(raw, dict) else {}


def staged_trading_timeline_stage(cfg: dict, now_et: datetime | None = None) -> dict:
    """Return the active automatic strategy stage for the current ET time."""
    now_et = now_et or datetime.now(EASTERN)
    timeline = staged_trading_timeline_config(cfg)
    if not bool(timeline.get("enabled", False)):
        return {"name": "manual", "entries_allowed": True}

    def boundary(hour_key: str, minute_key: str, default_hour: int, default_minute: int) -> datetime:
        return now_et.replace(
            hour=int(timeline.get(hour_key, default_hour)),
            minute=int(timeline.get(minute_key, default_minute)),
            second=0,
            microsecond=0,
        )

    opening_start = boundary("opening_start_hour", "opening_start_minute", 9, 35)
    opening_end = boundary("opening_end_hour", "opening_end_minute", 9, 45)
    midday_end = boundary("midday_end_hour", "midday_end_minute", 11, 30)
    retest_end = boundary("retest_end_hour", "retest_end_minute", 13, 30)
    if now_et < opening_start:
        return {"name": "before", "entries_allowed": False}
    if now_et < opening_end:
        return {
            "name": "opening_orb",
            "entries_allowed": True,
            "orb_minutes": 5,
            "require_retest": False,
            "scan_interval_seconds": max(10, int(timeline.get("opening_scan_interval_seconds", 60))),
            "first_scan_time": opening_start.time(),
        }
    if now_et < midday_end:
        return {
            "name": "orb_15m",
            "entries_allowed": True,
            "orb_minutes": 15,
            "require_retest": False,
            "scan_interval_seconds": max(60, int(timeline.get("orb_scan_interval_seconds", 300))),
            "first_scan_time": opening_end.time(),
        }
    if now_et < retest_end + timedelta(minutes=1):
        return {
            "name": "break_retest",
            "entries_allowed": True,
            "orb_minutes": 15,
            "require_retest": True,
            "scan_interval_seconds": max(60, int(timeline.get("retest_scan_interval_seconds", 300))),
            "first_scan_time": midday_end.time(),
        }
    return {"name": "after", "entries_allowed": False}


def opening_orb_trade_config(cfg: dict) -> dict:
    raw = cfg.get("opening_orb_trade", {})
    opening_cfg = dict(raw) if isinstance(raw, dict) else {}
    timeline = staged_trading_timeline_config(cfg)
    if bool(timeline.get("enabled", False)):
        opening_cfg.update(
            enabled=True,
            start_hour=int(timeline.get("opening_start_hour", 9)),
            start_minute=int(timeline.get("opening_start_minute", 35)),
            end_hour=int(timeline.get("opening_end_hour", 9)),
            end_minute=int(timeline.get("opening_end_minute", 45)),
            orb_minutes=5,
            scan_interval_seconds=max(10, int(timeline.get("opening_scan_interval_seconds", 60))),
        )
    return opening_cfg


def opening_orb_trade_window(cfg: dict, now_et: datetime | None = None) -> tuple[datetime, datetime]:
    now_et = now_et or datetime.now(EASTERN)
    opening_cfg = opening_orb_trade_config(cfg)
    start = now_et.replace(
        hour=int(opening_cfg.get("start_hour", 9)),
        minute=int(opening_cfg.get("start_minute", 35)),
        second=0,
        microsecond=0,
    )
    end = now_et.replace(
        hour=int(opening_cfg.get("end_hour", 9)),
        minute=int(opening_cfg.get("end_minute", 45)),
        second=0,
        microsecond=0,
    )
    return start, end


def opening_orb_trade_window_phase(cfg: dict, now_et: datetime | None = None) -> str:
    now_et = now_et or datetime.now(EASTERN)
    start, end = opening_orb_trade_window(cfg, now_et)
    if now_et < start:
        return "before"
    if now_et < end:
        return "active"
    return "after"


def _new_opening_orb_trade_state(now_et: datetime | None = None) -> dict:
    now_et = now_et or datetime.now(EASTERN)
    return {
        "session_date": now_et.date().isoformat(),
        "status": "WAITING",
        "updated_at": now_et.isoformat(),
    }


def read_opening_orb_trade_state(now_et: datetime | None = None) -> dict:
    now_et = now_et or datetime.now(EASTERN)
    state = read_json_file(OPENING_ORB_TRADE_STATE_FILE, {})
    if not isinstance(state, dict) or state.get("session_date") != now_et.date().isoformat():
        return _new_opening_orb_trade_state(now_et)
    return state


def write_opening_orb_trade_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OPENING_ORB_TRADE_STATE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)
    tmp.replace(OPENING_ORB_TRADE_STATE_FILE)


def update_opening_orb_trade_state(now_et: datetime | None = None, **updates) -> dict:
    now_et = now_et or datetime.now(EASTERN)
    state = read_opening_orb_trade_state(now_et)
    state.update(updates)
    state["session_date"] = now_et.date().isoformat()
    state["updated_at"] = now_et.isoformat()
    write_opening_orb_trade_state(state)
    return state


def _opening_orb_position_matches(state: dict, position: dict) -> bool:
    position_id = str(state.get("position_id") or "")
    if position_id and str(position.get("id") or "") == position_id:
        return True
    state_con_id = str(state.get("con_id") or "")
    position_con_id = str(position.get("con_id") or "")
    if state_con_id and position_con_id and state_con_id == position_con_id:
        return True
    state_option = str(state.get("option") or "")
    position_option = str(position.get("option") or "")
    if state_option and position_option and state_option == position_option:
        return True
    return (
        str(position.get("entry_source") or "").lower() == OPENING_ORB_SOURCE
        and str(position.get("symbol") or "").upper() == str(state.get("symbol") or "").upper()
    )


def _opening_orb_order_is_active(ib, state: dict) -> bool:
    if ib is None:
        return False
    try:
        ib.reqAllOpenOrders()
        ib.sleep(0.2)
    except Exception:
        pass
    wanted_order_id = str(state.get("broker_order_id") or "")
    wanted_con_id = str(state.get("con_id") or "")
    for trade in ib.openTrades() or []:
        order = getattr(trade, "order", None)
        contract = getattr(trade, "contract", None)
        status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "").lower()
        if status in {"filled", "cancelled", "canceled", "apicancelled", "inactive"}:
            continue
        order_id = str(getattr(order, "orderId", "") or "")
        con_id = str(getattr(contract, "conId", "") or "")
        if (wanted_order_id and order_id == wanted_order_id) or (wanted_con_id and con_id == wanted_con_id):
            return True
    return False


def refresh_opening_orb_trade_state(cfg: dict, ib=None, now_et: datetime | None = None) -> dict:
    now_et = now_et or datetime.now(EASTERN)
    state = read_opening_orb_trade_state(now_et)
    status = str(state.get("status") or "WAITING").upper()

    if status == "WAITING" and opening_orb_trade_window_phase(cfg, now_et) == "after":
        return update_opening_orb_trade_state(now_et, status="WINDOW_EXPIRED", completed_at=now_et.isoformat())

    if status == "PENDING_APPROVAL" and state.get("approval_id"):
        if opening_orb_trade_window_phase(cfg, now_et) == "after":
            update_pending_approval(
                str(state.get("approval_id")),
                status="expired",
                decision_at=now_et.isoformat(),
                reason="Opening ORB approval window ended at 09:45 ET.",
            )
            return update_opening_orb_trade_state(
                now_et,
                status="WINDOW_EXPIRED",
                completed_at=now_et.isoformat(),
                reason="Opening ORB approval was not completed by 09:45 ET.",
            )
        approval = next(
            (item for item in read_pending_approvals() if str(item.get("id") or "") == str(state.get("approval_id") or "")),
            None,
        )
        approval_status = str((approval or {}).get("status") or "").lower()
        if approval_status == "rejected":
            return update_opening_orb_trade_state(now_et, status="DECLINED", completed_at=now_et.isoformat())
        if approval_status in {"failed", "expired", "cancelled", "canceled"}:
            return update_opening_orb_trade_state(now_et, status="FAILED", completed_at=now_et.isoformat())

    matching_position = next(
        (pos for pos in read_active_positions() if _opening_orb_position_matches(state, pos)),
        None,
    )
    if matching_position:
        if status != "OPEN" or state.get("position_id") != matching_position.get("id"):
            return update_opening_orb_trade_state(
                now_et,
                status="OPEN",
                position_id=matching_position.get("id"),
                con_id=matching_position.get("con_id") or state.get("con_id"),
                option=matching_position.get("option") or state.get("option"),
                opened_at=state.get("opened_at") or matching_position.get("entry_time") or now_et.isoformat(),
            )
        return state

    if status == "OPEN" and _opening_orb_order_is_active(ib, state):
        return update_opening_orb_trade_state(now_et, status="ORDER_SUBMITTED")
    if status == "OPEN":
        return update_opening_orb_trade_state(now_et, status="COMPLETE", completed_at=now_et.isoformat())
    if status == "ORDER_SUBMITTED" and ib is not None and not _opening_orb_order_is_active(ib, state):
        terminal_status = "COMPLETE" if state.get("opened_at") else "NOT_FILLED"
        return update_opening_orb_trade_state(now_et, status=terminal_status, completed_at=now_et.isoformat())
    return state


def opening_orb_trade_needs_scan(cfg: dict, now_et: datetime | None = None) -> bool:
    now_et = now_et or datetime.now(EASTERN)
    if not bool(opening_orb_trade_config(cfg).get("enabled", False)):
        return False
    state = read_opening_orb_trade_state(now_et)
    return str(state.get("status") or "WAITING").upper() == "WAITING" and opening_orb_trade_window_phase(cfg, now_et) == "active"


def opening_orb_trade_pauses_normal_entries(cfg: dict, state: dict | None = None, now_et: datetime | None = None) -> bool:
    now_et = now_et or datetime.now(EASTERN)
    state = state or read_opening_orb_trade_state(now_et)
    status = str(state.get("status") or "WAITING").upper()
    if status in OPENING_ORB_ACTIVE_STATUSES:
        return True
    if not bool(opening_orb_trade_config(cfg).get("enabled", False)) or status in OPENING_ORB_TERMINAL_STATUSES:
        return False
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    return now_et >= market_open and opening_orb_trade_window_phase(cfg, now_et) in {"before", "active"}


def opening_orb_spend_limit(
    account_size: float,
    remaining_daily_capital: float,
    capital_pct: float = 50.0,
    liquidity_available: float | None = None,
) -> float:
    limit = min(
        max(float(account_size), 0.0) * max(float(capital_pct), 0.0) / 100.0,
        max(float(remaining_daily_capital), 0.0),
    )
    if liquidity_available is not None and float(liquidity_available) > 0:
        limit = min(limit, float(liquidity_available))
    return max(0.0, float(limit))


def _decision_cell(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str)
    return value


def _decision_row_data(row=None) -> dict:
    if row is None:
        return {}
    if isinstance(row, pd.Series):
        return clean_for_table(row.to_dict())
    if isinstance(row, dict):
        return clean_for_table(row)
    return {}


def log_engine_decision(
    *,
    symbol: str = "",
    decision: str,
    reason: str = "",
    row=None,
    option: dict | None = None,
    qty: int | None = None,
    estimated_cost: float | None = None,
    remaining_capital: float | None = None,
    remaining_trades: int | None = None,
    spend_limit: float | None = None,
    max_spend_per_trade: float | None = None,
    max_daily_capital: float | None = None,
) -> None:
    EXPORT_DIR.mkdir(exist_ok=True)
    data = _decision_row_data(row)
    option = option or {}
    symbol = str(symbol or data.get("Symbol") or option.get("Underlying") or "").upper()
    record = {
        "timestamp": datetime.now(EASTERN).isoformat(),
        "symbol": symbol,
        "decision": decision,
        "reason": reason,
        "signal": data.get("Signal"),
        "score": data.get("Score"),
        "grade": data.get("Grade"),
        "setup_quality": data.get("Setup Quality"),
        "rank_score": data.get("Rank Score"),
        "price": data.get("Price"),
        "rvol": data.get("RVOL"),
        "atr_pct": data.get("ATR %"),
        "orb_confirmation_time": data.get("ORB Confirmation Time"),
        "pdh": data.get("PDH"),
        "pdl": data.get("PDL"),
        "nearest_support": data.get("Nearest Support"),
        "support_distance_pct": data.get("Support Distance %"),
        "nearest_resistance": data.get("Nearest Resistance"),
        "resistance_distance_pct": data.get("Resistance Distance %"),
        "opening_exhaustion_block": data.get("Opening Exhaustion Block"),
        "midday_volume_block": data.get("Midday Volume Block"),
        "midday_volume_ratio": data.get("Midday Volume Ratio"),
        "second_candle_volume_ratio": data.get("Second Candle Volume Ratio"),
        "reasons": data.get("Reasons"),
        "option": option.get("Option"),
        "expiry": option.get("Expiry"),
        "strike": option.get("Strike"),
        "type": option.get("Type"),
        "bid": option.get("Bid"),
        "ask": option.get("Ask"),
        "mid": option.get("Mid"),
        "spread_pct": option.get("Spread %"),
        "spread_dollars": option.get("Spread $"),
        "delta": option.get("Delta"),
        "gamma": option.get("Gamma"),
        "theta": option.get("Theta"),
        "theta_pct_mid": option.get("Theta % Mid"),
        "vega": option.get("Vega"),
        "implied_vol": option.get("Implied Vol"),
        "greek_source": option.get("Greek Source"),
        "option_score": option.get("Option Score"),
        "option_score_notes": option.get("Option Score Notes"),
        "qty": qty,
        "estimated_cost": estimated_cost,
        "remaining_capital": remaining_capital,
        "remaining_trades": remaining_trades,
        "spend_limit": spend_limit,
        "max_spend_per_trade": max_spend_per_trade,
        "max_daily_capital": max_daily_capital,
    }
    fieldnames = list(record.keys())
    write_header = not ENGINE_DECISIONS_FILE.exists()
    with ENGINE_DECISIONS_FILE.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow({k: _decision_cell(v) for k, v in record.items()})


def top_candidate_reject_reason(result: dict, strategy: dict) -> str:
    signal = str(result.get("Signal") or "")
    if signal not in {"CALL", "PUT"}:
        if result.get("Retest Required") and not result.get("Retest Confirmed"):
            return str(result.get("Retest Reason") or "break-and-retest not confirmed")
        if result.get("Opening Exhaustion Block"):
            return "opening exhaustion block"
        if result.get("Midday Volume Block"):
            return "midday volume block"
        return f"non-trade signal: {signal or 'N/A'}"
    score = float(result.get("Score") or 0)
    min_score = float(strategy.get("min_score", 70))
    if score < min_score:
        return f"score below minimum ({score:.1f} < {min_score:.1f})"
    atr_pct = float(result.get("ATR %") or 0)
    min_atr = float(strategy.get("min_atr", 0.3))
    if atr_pct < min_atr:
        return f"ATR% below minimum ({atr_pct:.2f} < {min_atr:.2f})"
    if bool(strategy.get("use_rvol_filter", False)):
        rvol = float(result.get("RVOL") or 0)
        min_rvol = float(strategy.get("min_rvol", 1.5))
        if rvol < min_rvol:
            return f"RVOL below minimum ({rvol:.2f} < {min_rvol:.2f})"
    if str(result.get("Room Check") or "").lower() == "blocked":
        return str(result.get("Room Reason") or "support/resistance room blocked")
    return "scanner filter rejected"


def opening_orb_directional_reject_reason(result: dict, require_retest: bool = False) -> str:
    signal = str(result.get("Signal") or "").upper()
    required_by_signal = {
        "CALL": (
            ("ORB Up", "5-minute ORB break"),
            ("PDH Break", "PDH break"),
            ("Above VWAP", "price above VWAP"),
            ("EMA Bullish", "EMA9 above EMA21"),
        ),
        "PUT": (
            ("ORB Down", "5-minute ORB breakdown"),
            ("PDL Break", "PDL break"),
            ("Below VWAP", "price below VWAP"),
            ("EMA Bearish", "EMA9 below EMA21"),
        ),
    }
    if signal not in required_by_signal:
        return f"normal PMB signal not ready: {signal or 'N/A'}"
    required = required_by_signal[signal]
    if require_retest:
        required = (("Retest Confirmed", "break-and-retest confirmation"),) + required
    missing = [label for key, label in required if result.get(key) is not True]
    return f"missing opening conditions: {', '.join(missing)}" if missing else ""


def read_pending_approvals() -> list[dict]:
    data = read_json_file(PENDING_APPROVALS_FILE, [])
    return data if isinstance(data, list) else []


def write_pending_approvals(orders: list[dict]) -> None:
    write_json_file(PENDING_APPROVALS_FILE, orders)


def read_current_scan_candidates() -> dict:
    data = read_json_file(CURRENT_SCAN_CANDIDATES_FILE, {})
    return data if isinstance(data, dict) else {}


def write_current_scan_candidates(payload: dict) -> None:
    write_json_file(CURRENT_SCAN_CANDIDATES_FILE, payload)


def approval_mode_from_config(config: dict) -> str:
    automation = config.get("automation", {}) if isinstance(config.get("automation", {}), dict) else {}
    mode = str(automation.get("approval_mode") or "").strip().lower()
    if mode in {"automatic", "auto", "none"}:
        return "Automatic"
    if mode in {"telegram", "tg"} or bool(automation.get("require_trade_approval", False)):
        return "Telegram"
    return "Dashboard"


def expire_open_scanner_approvals(scan_id: str, reason: str = "Replaced by a newer scan") -> int:
    orders = read_pending_approvals()
    expired = 0
    now = datetime.now(EASTERN).isoformat()
    for order in orders:
        if str(order.get("status", "")).lower() not in ["pending", "sent"]:
            continue
        if order.get("source") != "scanner":
            continue
        if str(order.get("scan_id", "")) == str(scan_id):
            continue
        order["status"] = "expired"
        order["expired_at"] = now
        order["expiration_reason"] = reason
        expired += 1
    if expired:
        write_pending_approvals(orders)
    return expired


def scan_candidate_display_row(
    row,
    *,
    scan_id: str,
    approval_mode: str,
    tradable: bool,
    trade_status: str,
    block_reason: str = "",
    option_clean: dict | None = None,
    qty: int = 0,
    estimated_cost: float = 0.0,
    spend_limit: float | None = None,
    remaining_capital: float | None = None,
    remaining_trades: int | None = None,
    approval_id: str | None = None,
) -> dict:
    data = _decision_row_data(row)
    option_clean = option_clean or {}
    return {
        "scan_id": scan_id,
        "approval_mode": approval_mode,
        "approval_id": approval_id,
        "tradable": bool(tradable),
        "trade_status": trade_status,
        "block_reason": block_reason,
        "symbol": data.get("Symbol"),
        "signal": data.get("Signal"),
        "score": data.get("Score"),
        "confidence": data.get("Confidence"),
        "grade": data.get("Grade"),
        "setup_quality": data.get("Setup Quality"),
        "rank_score": data.get("Rank Score"),
        "price": data.get("Price"),
        "rvol": data.get("RVOL"),
        "atr_pct": data.get("ATR %"),
        "retest_confirmed": data.get("Retest Confirmed"),
        "retest_trigger_level": data.get("Retest Trigger Level"),
        "breakout_time": data.get("Breakout Time"),
        "retest_time": data.get("Retest Time"),
        "retest_reason": data.get("Retest Reason"),
        "reasons": data.get("Reasons"),
        "option": option_clean.get("Option") or data.get("Option") or "Not priced",
        "expiry": option_clean.get("Expiry"),
        "strike": option_clean.get("Strike"),
        "type": option_clean.get("Type"),
        "quantity": int(qty or 0),
        "mid": option_clean.get("Mid") if option_clean else data.get("Mid"),
        "bid": option_clean.get("Bid"),
        "ask": option_clean.get("Ask"),
        "spread_pct": option_clean.get("Spread %"),
        "delta": option_clean.get("Delta"),
        "option_score": option_clean.get("Option Score"),
        "estimated_cost": float(estimated_cost or 0.0),
        "spend_limit": spend_limit,
        "remaining_capital": remaining_capital,
        "remaining_trades": remaining_trades,
    }


def sort_scan_display_rows(rows: list[dict]) -> list[dict]:
    def _num(value) -> float:
        try:
            return float(value)
        except Exception:
            return -1.0

    return sorted(
        rows,
        key=lambda item: (
            bool(item.get("tradable")),
            _num(item.get("rank_score")),
            _num(item.get("score")),
        ),
        reverse=True,
    )


def make_approval_id(symbol: str, signal: str) -> str:
    stamp = datetime.now(EASTERN).strftime("%Y%m%d%H%M%S%f")
    return f"{symbol}_{signal}_{stamp}".replace(" ", "").upper()


def create_pending_approval(row: pd.Series, option_clean: dict, option_full: dict, qty: int, estimated_cost: float, order_type: str, limit_price: float | None, mode: str, *, source: str = "scanner", scan_id: str | None = None, approval_mode: str = "Dashboard") -> dict:
    order = {
        "id": make_approval_id(str(row["Symbol"]), str(row["Signal"])),
        "status": "pending",
        "created_at": datetime.now(EASTERN).isoformat(),
        "source": source,
        "scan_id": scan_id,
        "approval_mode": approval_mode,
        "symbol": row["Symbol"],
        "signal": row["Signal"],
        "option": option_full["Option"],
        "expiry": option_full["Expiry"],
        "strike": option_full["Strike"],
        "type": option_full["Type"],
        "quantity": int(qty),
        "mid": option_full.get("Mid"),
        "bid": option_full.get("Bid"),
        "ask": option_full.get("Ask"),
        "spread_pct": option_full.get("Spread %"),
        "spread_dollars": option_full.get("Spread $"),
        "delta": option_full.get("Delta"),
        "gamma": option_full.get("Gamma"),
        "theta": option_full.get("Theta"),
        "theta_pct_mid": option_full.get("Theta % Mid"),
        "vega": option_full.get("Vega"),
        "implied_vol": option_full.get("Implied Vol"),
        "option_score": option_full.get("Option Score"),
        "option_score_notes": option_full.get("Option Score Notes"),
        "estimated_cost": float(estimated_cost),
        "order_type": order_type,
        "limit_price": limit_price,
        "account_mode": mode,
        "score": row["Score"],
        "confidence": row.get("Confidence"),
        "grade": row.get("Grade"),
        "setup_quality": row.get("Setup Quality"),
        "rank_score": row["Rank Score"],
        "reasons": row.get("Reasons"),
        "raw_signal": clean_for_table(row.to_dict()),
        "option_data": option_clean,
    }
    orders = read_pending_approvals()
    orders.append(order)
    write_pending_approvals(orders)
    return order


def create_pending_exit_approval(position: dict, current_price: float | None, reason: str, mode: str, *, approval_mode: str = "Telegram") -> tuple[dict, bool]:
    position_id = str(position.get("id") or position.get("con_id") or position.get("option") or "")
    orders = read_pending_approvals()
    for order in orders:
        if (
            str(order.get("status", "")).lower() in ["pending", "sent", "approval_send_failed"]
            and str(order.get("source") or "") == "position_exit"
            and str(order.get("position_id") or "") == position_id
        ):
            return order, False

    qty = int(float(position.get("quantity") or 0))
    mid = float(current_price or position.get("current_price") or position.get("entry_price") or 0)
    order = {
        "id": make_approval_id(str(position.get("symbol") or ""), "EXIT"),
        "status": "pending",
        "created_at": datetime.now(EASTERN).isoformat(),
        "source": "position_exit",
        "approval_mode": approval_mode,
        "action": "SELL",
        "position_id": position_id,
        "symbol": position.get("symbol"),
        "signal": position.get("signal"),
        "option": position.get("option"),
        "expiry": position.get("expiry"),
        "strike": position.get("strike"),
        "type": position.get("signal"),
        "con_id": position.get("con_id"),
        "quantity": qty,
        "mid": round(mid, 2) if mid else None,
        "entry_price": position.get("entry_price"),
        "estimated_cost": round(mid * qty * 100, 2) if mid and qty else 0.0,
        "order_type": "MARKET",
        "limit_price": None,
        "account_mode": mode,
        "score": "EOD",
        "grade": "Exit",
        "setup_quality": "End-of-day close approval",
        "rank_score": 0,
        "reasons": reason,
        "raw_signal": {
            "Symbol": position.get("symbol"),
            "Signal": position.get("signal"),
            "Price": position.get("underlying_current_price") or position.get("underlying_entry_price"),
        },
        "option_data": {
            "Option": position.get("option"),
            "Expiry": position.get("expiry"),
            "Strike": position.get("strike"),
            "Type": position.get("signal"),
            "Mid": round(mid, 2) if mid else None,
        },
    }
    orders.append(order)
    write_pending_approvals(orders)
    return order, True


def approval_message(order: dict) -> str:
    reasons = str(order.get("reasons") or "")
    if len(reasons) > 500:
        reasons = reasons[:500] + "..."
    limit_price = order.get("limit_price")
    limit_line = f"Limit: ${float(limit_price):.2f}" if limit_price not in [None, "", 0] else "Limit: N/A"
    action = str(order.get("action") or "BUY").upper()
    title = "PulseTrade Exit Approval" if action == "SELL" else "PulseTrade Order Approval"
    cost_label = "Estimated proceeds" if action == "SELL" else "Estimated cost"
    return (
        f"🚨 <b>{title}</b>\n\n"
        f"<b>{escape(str(order.get('symbol', 'N/A')))} {escape(str(order.get('signal', 'N/A')))}</b>\n"
        f"Mode: <b>{escape(str(order.get('account_mode', 'N/A')))}</b>\n"
        f"Score: <b>{escape(str(order.get('score', 'N/A')))}</b> | Grade: <b>{escape(str(order.get('grade', 'N/A')))}</b>\n"
        f"Quality: <b>{escape(str(order.get('setup_quality') or 'N/A'))}</b>\n\n"
        "<b>Option</b>\n"
        f"{escape(str(order.get('option', 'N/A')))}\n"
        f"Qty: <b>{escape(str(order.get('quantity', 0)))}</b>\n"
        f"Mid: ${float(order.get('mid') or 0):.2f}\n"
        f"Delta: <b>{escape(str(order.get('delta', 'N/A')))}</b> | Spread: <b>{escape(str(order.get('spread_pct', 'N/A')))}%</b>\n"
        f"Theta: <b>{escape(str(order.get('theta', 'N/A')))}</b> | IV: <b>{escape(str(order.get('implied_vol', 'N/A')))}</b>\n"
        f"{cost_label}: ${float(order.get('estimated_cost') or 0):,.2f}\n"
        f"Order: {escape(action)} {escape(str(order.get('order_type', 'LIMIT')))}\n"
        f"{limit_line}\n\n"
        "<b>Why</b>\n"
        f"{escape(reasons)}\n\n"
        f"Order ID:\n<code>{escape(str(order.get('id')))}</code>"
    )


def telegram_post(bot_token: str, method: str, payload: dict, timeout: int = 10) -> dict:
    response = requests.post(f"https://api.telegram.org/bot{bot_token}/{method}", json=payload, timeout=timeout)
    data = response.json() if response.content else {}
    if response.status_code != 200 or not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data}")
    return data


def is_telegram_polling_noise(exc: Exception) -> bool:
    if isinstance(exc, (requests.exceptions.ReadTimeout, requests.exceptions.Timeout)):
        return True
    message = str(exc)
    return (
        ("Telegram getUpdates failed" in message or "api.telegram.org" in message)
        and (
            "error_code': 409" in message
            or '"error_code": 409' in message
            or "terminated by other getUpdates request" in message
            or "Read timed out" in message
            or "read timeout" in message.lower()
        )
    )


def telegram_try_post(bot_token: str, method: str, payload: dict, timeout: int = 10) -> bool:
    try:
        telegram_post(bot_token, method, payload, timeout=timeout)
        return True
    except Exception as exc:
        app_log(f"Telegram {method} warning: {exc}", "WARN")
        return False


def send_order_approval_message(tg_cfg: TelegramConfig, order: dict) -> bool:
    if not tg_cfg.bot_token or not tg_cfg.chat_id:
        return False
    payload = {
        "chat_id": tg_cfg.chat_id,
        "text": approval_message(order),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ Confirm", "callback_data": f"confirm:{order['id']}"},
                {"text": "❌ Reject", "callback_data": f"reject:{order['id']}"},
            ]]
        },
    }
    data = telegram_post(tg_cfg.bot_token, "sendMessage", payload)
    message = data.get("result", {})
    orders = read_pending_approvals()
    for existing in orders:
        if existing.get("id") == order.get("id"):
            existing["status"] = "sent"
            existing["telegram_message_id"] = message.get("message_id")
            existing["telegram_sent_at"] = datetime.now(EASTERN).isoformat()
            break
    write_pending_approvals(orders)
    return True


def update_pending_approval(order_id: str, **updates) -> dict | None:
    orders = read_pending_approvals()
    updated = None
    for order in orders:
        if order.get("id") == order_id:
            order.update(updates)
            updated = order
            break
    write_pending_approvals(orders)
    return updated


def submit_approved_order(ib, ib_cfg: IBConfig, order: dict, max_wait_seconds: int = 60) -> str:
    if str(order.get("source") or "").lower() == OPENING_ORB_SOURCE:
        approval_cfg = load_config()
        approval_now = datetime.now(EASTERN)
        approval_state = refresh_opening_orb_trade_state(approval_cfg, ib=ib, now_et=approval_now)
        if (
            not bool(opening_orb_trade_config(approval_cfg).get("enabled", False))
            or opening_orb_trade_window_phase(approval_cfg, approval_now) != "active"
            or str(approval_state.get("status") or "").upper() != "PENDING_APPROVAL"
        ):
            raise RuntimeError("The 09:35-09:45 ET opening ORB approval window is closed.")
    contract = reconstruct_option_contract(order)
    qualified = ib.qualifyContracts(contract)
    if qualified:
        contract = qualified[0]
    qty = int(order.get("quantity") or 0)
    order_type = str(order.get("order_type") or "LIMIT")
    limit_price = float(order.get("limit_price") or order.get("mid") or 0) if order_type == "LIMIT" else None
    action = str(order.get("action") or "BUY").upper()
    trade = place_option_order(ib, contract, action, qty, order_type, limit_price, ib_cfg.account, max_wait_seconds=max_wait_seconds)
    fallback_entry_price = float(limit_price if limit_price else order.get("mid") or order.get("entry_price") or 0)
    fill = trade_fill_details(trade, qty, fallback_entry_price)
    status = str(fill["status"])
    filled_qty = int(fill.get("filled_qty") or 0)
    entry_price = float(fill.get("avg_fill_price") or fallback_entry_price)
    if action == "SELL":
        original_entry = float(order.get("entry_price") or 0)
        realized = round((entry_price - original_entry) * filled_qty * 100, 2) if filled_qty and original_entry else 0.0
        log_trade({
            "timestamp": datetime.now(EASTERN).isoformat(),
            "event": "EXIT",
            "account_mode": order.get("account_mode"),
            "symbol": order.get("symbol"),
            "signal": order.get("signal"),
            "option": order.get("option"),
            "quantity": qty,
            "filled_quantity": filled_qty,
            "remaining_quantity": fill.get("remaining_qty"),
            "order_type": order_type,
            "limit_price": entry_price,
            "entry_price": original_entry or order.get("entry_price"),
            "exit_price": entry_price,
            "realized_pnl": realized,
            "status": status,
            "broker_status": fill.get("raw_status"),
            "exit_reason": order.get("reasons") or "Approved exit",
            "source": "APPROVED_EXIT_ORDER",
            "broker_order_ids": str(getattr(getattr(trade, "order", None), "orderId", "") or ""),
            "broker_perm_ids": str(getattr(getattr(trade, "order", None), "permId", "") or ""),
            "broker_client_ids": str(getattr(getattr(trade, "order", None), "clientId", "") or ""),
            "close_classification": "approved_exit_order",
        })
        if filled_qty > 0:
            remaining_positions = []
            position_id = str(order.get("position_id") or "")
            for pos in read_active_positions():
                same_position = position_id and str(pos.get("id") or "") == position_id
                same_con_id = str(pos.get("con_id") or "") and str(pos.get("con_id") or "") == str(order.get("con_id") or "")
                if same_position or same_con_id:
                    current_qty = int(float(pos.get("quantity") or 0))
                    left_qty = current_qty - filled_qty
                    if left_qty > 0:
                        pos["quantity"] = left_qty
                        remaining_positions.append(pos)
                    continue
                remaining_positions.append(pos)
            write_active_positions(remaining_positions)
        update_pending_approval(order["id"], status="submitted", submitted_at=datetime.now(EASTERN).isoformat(), broker_status=status)
        return status

    option_full = {
        "Contract": contract,
        "Option": order.get("option"),
        "Expiry": order.get("expiry"),
        "Strike": order.get("strike"),
        "Type": order.get("type"),
    }
    raw_signal = order.get("raw_signal") if isinstance(order.get("raw_signal"), dict) else {}
    row = {
        "Symbol": order.get("symbol"),
        "Signal": order.get("signal"),
        "Price": raw_signal.get("Price") or raw_signal.get("price"),
    }
    log_trade({
        "timestamp": datetime.now(EASTERN).isoformat(),
        "event": "ENTRY",
        "account_mode": order.get("account_mode"),
        "symbol": order.get("symbol"),
        "signal": order.get("signal"),
        "option": order.get("option"),
        "quantity": qty,
        "filled_quantity": filled_qty,
        "remaining_quantity": fill.get("remaining_qty"),
        "order_type": order_type,
        "limit_price": entry_price,
        "estimated_cost": order.get("estimated_cost"),
        "status": status,
        "broker_status": fill.get("raw_status"),
        "score": order.get("score"),
        "confidence": order.get("confidence"),
        "grade": order.get("grade"),
        "setup_quality": order.get("setup_quality"),
        "rank_score": order.get("rank_score"),
        "reasons": order.get("reasons"),
        "source": order.get("source"),
    })
    approval_label = str(order.get("approval_mode") or "Order").strip() or "Order"
    save_trade_replay(
        order.get("raw_signal") or {},
        order.get("option_data") or {},
        event="ENTRY",
        order_status=status,
        quantity=filled_qty or qty,
        entry_price=entry_price,
        estimated_cost=order.get("estimated_cost"),
        notes=f"{approval_label} approval submitted: {order.get('id')}",
    )
    active_position = None
    if filled_qty > 0:
        cfg = load_config()
        risk = cfg.get("risk", {})
        take_profit_pct = float(risk.get("take_profit_pct", 30.0))
        active_position = add_active_position_from_entry(
            row,
            option_full,
            filled_qty,
            entry_price,
            status,
            ib=ib,
            account=ib_cfg.account,
            stop_loss_pct=float(risk.get("stop_loss_pct", 20.0)),
            take_profit_pct=take_profit_pct,
            trailing_stop_pct=float(risk.get("trailing_stop_pct", 10.0)),
            trailing_from_entry=bool(risk.get("trailing_from_entry", True)),
            fixed_take_profit_enabled=bool(risk.get("use_take_profit_with_trailing", False)),
            entry_source=str(order.get("source") or "") or None,
        )
    if str(order.get("source") or "").lower() == OPENING_ORB_SOURCE:
        broker_order_id = getattr(getattr(trade, "order", None), "orderId", None)
        update_opening_orb_trade_state(
            status="OPEN" if filled_qty > 0 else "ORDER_SUBMITTED",
            symbol=order.get("symbol"),
            signal=order.get("signal"),
            option=order.get("option"),
            con_id=getattr(contract, "conId", None),
            position_id=(active_position or {}).get("id"),
            approval_id=order.get("id"),
            broker_order_id=broker_order_id,
            opened_at=datetime.now(EASTERN).isoformat() if filled_qty > 0 else None,
            order_submitted_at=datetime.now(EASTERN).isoformat(),
        )
    update_pending_approval(order["id"], status="submitted", submitted_at=datetime.now(EASTERN).isoformat(), broker_status=status)
    return status


def process_telegram_order_callbacks(ib, ib_cfg: IBConfig, tg_cfg: TelegramConfig, can_trade: bool) -> int:
    if not tg_cfg.bot_token or not tg_cfg.chat_id:
        return 0
    state = read_json_file(TELEGRAM_APPROVAL_STATE_FILE, {})
    payload = {"timeout": 3}
    if state.get("offset") is not None:
        payload["offset"] = state["offset"]
    data = telegram_post(tg_cfg.bot_token, "getUpdates", payload, timeout=12)
    processed = 0
    orders_by_id = {order.get("id"): order for order in read_pending_approvals()}
    for update in data.get("result", []):
        state["offset"] = int(update["update_id"]) + 1
        callback = update.get("callback_query") or {}
        callback_id = callback.get("id")
        callback_data = str(callback.get("data") or "")
        message = callback.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        message_id = message.get("message_id")
        if not callback_id or ":" not in callback_data:
            continue
        action, order_id = callback_data.split(":", 1)
        if chat_id != str(tg_cfg.chat_id):
            telegram_try_post(tg_cfg.bot_token, "answerCallbackQuery", {"callback_query_id": callback_id, "text": "Unauthorized chat"})
            continue
        pending = orders_by_id.get(order_id)
        if not pending or str(pending.get("status", "")).lower() not in ["pending", "sent"]:
            telegram_try_post(tg_cfg.bot_token, "answerCallbackQuery", {"callback_query_id": callback_id, "text": "Order is no longer pending"})
            continue
        if action == "reject":
            update_pending_approval(order_id, status="rejected", decision_at=datetime.now(EASTERN).isoformat())
            telegram_try_post(tg_cfg.bot_token, "answerCallbackQuery", {"callback_query_id": callback_id, "text": "Rejected"})
            if message_id:
                telegram_try_post(tg_cfg.bot_token, "editMessageText", {"chat_id": tg_cfg.chat_id, "message_id": message_id, "text": approval_message(pending) + "\n\n❌ <b>Status: REJECTED</b>", "parse_mode": "HTML"})
            processed += 1
            continue
        if action == "confirm":
            if pending.get("test_order"):
                update_pending_approval(order_id, status="test_confirmed", decision_at=datetime.now(EASTERN).isoformat())
                telegram_try_post(tg_cfg.bot_token, "answerCallbackQuery", {"callback_query_id": callback_id, "text": "Test confirmed"})
                if message_id:
                    telegram_try_post(tg_cfg.bot_token, "editMessageText", {"chat_id": tg_cfg.chat_id, "message_id": message_id, "text": approval_message(pending) + "\n\n✅ <b>Status: TEST CONFIRMED</b>\nNo IBKR order was submitted.", "parse_mode": "HTML"})
                processed += 1
                continue
            if not can_trade:
                telegram_try_post(tg_cfg.bot_token, "answerCallbackQuery", {"callback_query_id": callback_id, "text": "Trading is not armed"})
                continue
            try:
                status = submit_approved_order(ib, ib_cfg, pending)
                telegram_try_post(tg_cfg.bot_token, "answerCallbackQuery", {"callback_query_id": callback_id, "text": f"Submitted: {status}"})
                if message_id:
                    telegram_try_post(tg_cfg.bot_token, "editMessageText", {"chat_id": tg_cfg.chat_id, "message_id": message_id, "text": approval_message(pending) + f"\n\n✅ <b>Status: SUBMITTED</b>\nBroker status: <b>{escape(status)}</b>", "parse_mode": "HTML"})
                app_log(f"{pending.get('symbol')}: approved order submitted | id={order_id} | status={status}")
            except Exception as exc:
                update_pending_approval(order_id, status="failed", failed_at=datetime.now(EASTERN).isoformat(), error=str(exc))
                telegram_try_post(tg_cfg.bot_token, "answerCallbackQuery", {"callback_query_id": callback_id, "text": "Submit failed"})
                app_log(f"{pending.get('symbol')}: approved order submit failed | id={order_id} | {exc}", "ERROR")
            processed += 1
    write_json_file(TELEGRAM_APPROVAL_STATE_FILE, state)
    return processed


def run_opening_orb_trade_cycle(
    *,
    ib,
    ib_cfg: IBConfig,
    cfg: dict,
    can_trade: bool,
    account_size: float,
    liquidity_available: float | None,
    max_daily_capital: float,
    approval_mode: str,
    tg_cfg: TelegramConfig,
) -> dict:
    now_et = datetime.now(EASTERN)
    opening_cfg = opening_orb_trade_config(cfg)
    state = refresh_opening_orb_trade_state(cfg, ib=ib, now_et=now_et)
    status = str(state.get("status") or "WAITING").upper()
    if not bool(opening_cfg.get("enabled", False)) or status != "WAITING":
        return state
    if opening_orb_trade_window_phase(cfg, now_et) != "active":
        return refresh_opening_orb_trade_state(cfg, ib=ib, now_et=now_et)

    strategy = dict(cfg.get("strategy", {}))
    risk = cfg.get("risk", {})
    order_cfg = cfg.get("order", {})
    telegram = cfg.get("telegram", {})
    watchlist = combined_watchlist(cfg)
    scan_id = f"OPENING_{now_et.strftime('%Y%m%d%H%M%S')}"
    current_trade_count, daily_deployed = get_today_trade_stats()
    if current_trade_count > 0:
        return update_opening_orb_trade_state(
            now_et,
            status="SKIPPED_EXISTING_TRADE",
            completed_at=now_et.isoformat(),
            reason="A trade was already entered today before the opening ORB trade.",
        )
    if current_trade_count >= int(risk.get("max_trades_per_day", 2)):
        return update_opening_orb_trade_state(
            now_et,
            status="SKIPPED_DAILY_LIMIT",
            completed_at=now_et.isoformat(),
            reason="The daily trade limit was already reached.",
        )

    active_positions = read_active_positions()
    if active_positions:
        return update_opening_orb_trade_state(
            now_et,
            status="SKIPPED_ACTIVE_POSITION",
            completed_at=now_et.isoformat(),
            reason="An option position was already open during the opening window.",
        )

    recycle_capital = bool(risk.get("recycle_capital_after_exit", False))
    current_deployed = get_open_position_deployed() if recycle_capital else daily_deployed
    remaining_capital = max(0.0, float(max_daily_capital) - float(current_deployed))
    spend_limit = opening_orb_spend_limit(
        account_size,
        remaining_capital,
        float(opening_cfg.get("capital_pct", 50.0)),
        liquidity_available,
    )
    if spend_limit <= 0:
        return update_opening_orb_trade_state(
            now_et,
            status="SKIPPED_DAILY_LIMIT",
            completed_at=now_et.isoformat(),
            reason="No capital was available for the opening ORB trade.",
        )

    candidates: list[dict] = []
    scan_display_rows: list[dict] = []
    opening_strategy = dict(strategy)
    opening_min_score = float(strategy.get("min_score", 90.0))
    opening_strategy["min_score"] = opening_min_score
    orb_minutes = int(opening_cfg.get("orb_minutes", 5))
    opening_requires_retest = bool(strategy.get("require_break_retest", False))
    if bool(staged_trading_timeline_config(cfg).get("enabled", False)):
        opening_requires_retest = False
    for symbol in watchlist:
        try:
            result = scan_symbol_ib(
                ib,
                symbol,
                bool(strategy.get("use_rvol_score", False)),
                "pmb",
                orb_minutes,
                2,
                min_score=70.0,
                require_retest=opening_requires_retest,
                retest_tolerance_pct=float(strategy.get("retest_tolerance_pct", 0.10)),
                retest_max_minutes=int(strategy.get("retest_max_minutes", 45)),
                intraday_duration="2 D",
                intraday_bar_size="1 min",
                analysis_bar_minutes=5,
            )
            if not result:
                log_engine_decision(
                    symbol=symbol,
                    decision="OPENING_SCAN_NO_RESULT",
                    reason="Opening ORB data was not ready",
                )
                continue
            if scan_result_session_date(result) != now_et.date():
                log_engine_decision(
                    symbol=symbol,
                    row=result,
                    decision="OPENING_SKIP_STALE_DATA",
                    reason="Opening ORB scan did not use today's session",
                )
                continue
            directional_reject_reason = opening_orb_directional_reject_reason(
                result,
                require_retest=opening_requires_retest,
            )
            passed_filters = not directional_reject_reason and is_top_candidate(
                result,
                opening_min_score,
                float(strategy.get("min_confidence", 70.0)),
                float(strategy.get("min_rvol", 1.5)),
                float(strategy.get("min_atr", 0.3)),
                bool(strategy.get("use_rvol_filter", False)),
                bool(strategy.get("use_sr_filter", True)),
                float(strategy.get("min_sr_room_pct", 0.75)),
            )
            rank_score = opportunity_rank_score(result, None, bool(strategy.get("use_rvol_ranking", False)))
            if not passed_filters:
                scan_display_rows.append(scan_candidate_display_row(
                    {"Rank Score": rank_score, **clean_for_table(result), "Option": "Not priced"},
                    scan_id=scan_id,
                    approval_mode=approval_mode,
                    tradable=False,
                    trade_status="Opening ORB waiting",
                    block_reason=directional_reject_reason or top_candidate_reject_reason(result, opening_strategy),
                    spend_limit=spend_limit,
                    remaining_capital=remaining_capital,
                ))
                continue
            candidates.append({
                "Rank Score": rank_score,
                **clean_for_table(result),
                "Option": "Not priced",
                "Mid": None,
                "Option Score": 0,
                "Qty": 0,
                "Estimated Cost": 0.0,
            })
            log_engine_decision(
                symbol=symbol,
                row=result,
                decision="OPENING_CANDIDATE",
                reason="Score and all opening directional conditions passed",
                spend_limit=spend_limit,
                max_daily_capital=max_daily_capital,
            )
        except Exception as exc:
            app_log(f"{symbol}: opening ORB scan error: {exc}", "ERROR")
            log_engine_decision(symbol=symbol, decision="OPENING_SCAN_ERROR", reason=str(exc))

    if not candidates:
        finished_at = datetime.now(EASTERN)
        state = read_opening_orb_trade_state(finished_at)
        if opening_orb_trade_window_phase(cfg, finished_at) == "after":
            state = update_opening_orb_trade_state(
                finished_at,
                status="WINDOW_EXPIRED",
                completed_at=finished_at.isoformat(),
                reason="No eligible 5-minute ORB breakout was found by 09:45 ET.",
            )
        write_current_scan_candidates({
            "scan_id": scan_id,
            "generated_at": finished_at.isoformat(),
            "approval_mode": approval_mode,
            "entry_mode": OPENING_ORB_SOURCE,
            "candidates": sort_scan_display_rows(scan_display_rows),
            "message": "Opening ORB window active; no eligible breakout yet.",
        })
        write_health(
            last_scan_finish=finished_at.isoformat(),
            last_status="Opening ORB window: waiting for breakout",
            opening_orb_trade_status=state.get("status"),
            candidates=0,
            ib_connected=True,
            last_error="",
        )
        return state

    df = pd.DataFrame(candidates).sort_values(
        ["Rank Score", "Score", "RVOL"],
        ascending=[False, False, False],
    )
    selected = False
    for _, row in df.iterrows():
        symbol = str(row["Symbol"])
        try:
            option_filters = dict(cfg.get("option_filters", {}) or {})
            option_filters["_max_contract_cost"] = spend_limit
            option_full = recommend_option_ib(
                ib,
                symbol,
                row["Signal"],
                float(row["Price"]),
                int(strategy.get("option_dte", 7)),
                option_filters,
            )
        except Exception as exc:
            app_log(f"{symbol}: opening ORB option pricing error: {exc}", "ERROR")
            option_full = None
        option_clean = {k: v for k, v in option_full.items() if k != "Contract"} if option_full else None
        qty = calculate_contract_quantity(
            float(option_full["Mid"]),
            spend_limit,
            int(risk.get("max_contracts", 2)),
        ) if option_full else 0
        estimated_cost = round(qty * float(option_full["Mid"]) * 100, 2) if option_full and qty else 0.0
        row["Option"] = option_clean["Option"] if option_clean else "No clean contract"
        row["Mid"] = option_clean["Mid"] if option_clean else None
        row["Option Score"] = option_clean["Option Score"] if option_clean else 0
        row["Qty"] = qty
        row["Estimated Cost"] = estimated_cost

        if qty <= 0 or option_full is None or estimated_cost > spend_limit:
            scan_display_rows.append(scan_candidate_display_row(
                row,
                scan_id=scan_id,
                approval_mode=approval_mode,
                tradable=False,
                trade_status="Opening ORB blocked",
                block_reason="No clean option contract fit inside the 50% opening-trade cap.",
                option_clean=option_clean,
                qty=qty,
                estimated_cost=estimated_cost,
                spend_limit=spend_limit,
                remaining_capital=remaining_capital,
            ))
            continue

        submission_now = datetime.now(EASTERN)
        if opening_orb_trade_window_phase(cfg, submission_now) != "active":
            selected = True
            state = update_opening_orb_trade_state(
                submission_now,
                status="WINDOW_EXPIRED",
                completed_at=submission_now.isoformat(),
                reason="The opening ORB scan finished after 09:45 ET; no order was submitted.",
            )
            scan_display_rows.append(scan_candidate_display_row(
                row,
                scan_id=scan_id,
                approval_mode=approval_mode,
                tradable=False,
                trade_status="Opening ORB window closed",
                block_reason="The 09:35-09:45 ET entry window closed before submission.",
                option_clean=option_clean,
                qty=qty,
                estimated_cost=estimated_cost,
                spend_limit=spend_limit,
                remaining_capital=remaining_capital,
            ))
            break

        selected = True
        if telegram.get("send_alerts", False):
            ok = send_telegram_message(tg_cfg, make_alert_message(row.to_dict(), option_clean))
            log_alert({
                "timestamp": datetime.now(EASTERN).isoformat(),
                "symbol": symbol,
                "signal": row["Signal"],
                "score": row["Score"],
                "grade": row.get("Grade"),
                "entry_mode": OPENING_ORB_SOURCE,
                "telegram_sent": ok,
            })

        if not can_trade:
            scan_display_rows.append(scan_candidate_display_row(
                row,
                scan_id=scan_id,
                approval_mode=approval_mode,
                tradable=False,
                trade_status="Opening ORB signal only",
                block_reason="Order placement is not armed; the opening window will keep monitoring.",
                option_clean=option_clean,
                qty=qty,
                estimated_cost=estimated_cost,
                spend_limit=spend_limit,
                remaining_capital=remaining_capital,
            ))
            break

        entry_order_type = str(order_cfg.get("type", "LIMIT") or "LIMIT").upper()
        limit_price = option_full["Mid"] if entry_order_type == "LIMIT" else None
        if approval_mode != "Automatic":
            pending = create_pending_approval(
                row,
                option_clean,
                option_full,
                qty,
                estimated_cost,
                entry_order_type,
                limit_price,
                str(cfg.get("account_mode", "Simulation")),
                source=OPENING_ORB_SOURCE,
                scan_id=scan_id,
                approval_mode=approval_mode,
            )
            sent = False
            if approval_mode == "Telegram":
                try:
                    sent = send_order_approval_message(tg_cfg, pending)
                except Exception as exc:
                    app_log(f"{symbol}: opening ORB approval send failed: {exc}", "WARN")
                update_pending_approval(pending["id"], status="sent" if sent else "approval_send_failed")
            state = update_opening_orb_trade_state(
                status="PENDING_APPROVAL",
                symbol=symbol,
                signal=row["Signal"],
                score=row["Score"],
                rank_score=row["Rank Score"],
                option=option_full["Option"],
                con_id=getattr(option_full["Contract"], "conId", None),
                approval_id=pending["id"],
                selected_at=datetime.now(EASTERN).isoformat(),
                spend_limit=spend_limit,
                estimated_cost=estimated_cost,
            )
            scan_display_rows.append(scan_candidate_display_row(
                row,
                scan_id=scan_id,
                approval_mode=approval_mode,
                tradable=True,
                trade_status="Opening ORB approval pending",
                block_reason=f"{approval_mode} approval required for the opening ORB trade.",
                option_clean=option_clean,
                qty=qty,
                estimated_cost=estimated_cost,
                spend_limit=spend_limit,
                remaining_capital=remaining_capital,
                approval_id=pending["id"],
            ))
            break

        try:
            trade = place_option_order(
                ib,
                option_full["Contract"],
                "BUY",
                qty,
                entry_order_type,
                limit_price,
                ib_cfg.account,
            )
            fallback_entry_price = float(limit_price if limit_price else option_full["Mid"])
            fill = trade_fill_details(trade, qty, fallback_entry_price)
            trade_status = str(fill["status"])
            filled_qty = int(fill.get("filled_qty") or 0)
            entry_price = float(fill.get("avg_fill_price") or fallback_entry_price)
            log_trade({
                "timestamp": datetime.now(EASTERN).isoformat(),
                "event": "ENTRY",
                "account_mode": cfg.get("account_mode"),
                "symbol": symbol,
                "signal": row["Signal"],
                "option": option_full["Option"],
                "quantity": qty,
                "filled_quantity": filled_qty,
                "remaining_quantity": fill.get("remaining_qty"),
                "order_type": entry_order_type,
                "limit_price": entry_price,
                "estimated_cost": estimated_cost,
                "status": trade_status,
                "broker_status": fill.get("raw_status"),
                "score": row["Score"],
                "confidence": row.get("Confidence"),
                "grade": row.get("Grade"),
                "setup_quality": row.get("Setup Quality"),
                "rank_score": row["Rank Score"],
                "orb_high": row.get("ORB High"),
                "orb_low": row.get("ORB Low"),
                "retest_confirmed": row.get("Retest Confirmed"),
                "retest_trigger_level": row.get("Retest Trigger Level"),
                "breakout_time": row.get("Breakout Time"),
                "retest_time": row.get("Retest Time"),
                "retest_minutes_after_breakout": row.get("Retest Minutes After Breakout"),
                "retest_reason": row.get("Retest Reason"),
                "rvol": row.get("RVOL"),
                "atr_pct": row.get("ATR %"),
                "reasons": row.get("Reasons"),
                "source": OPENING_ORB_SOURCE,
            })
            save_trade_replay(
                row.to_dict(),
                option_clean,
                event="ENTRY",
                order_status=trade_status,
                quantity=filled_qty or qty,
                entry_price=entry_price,
                estimated_cost=estimated_cost,
                notes="09:35-09:45 opening ORB entry",
            )
            active_position = None
            if filled_qty > 0:
                active_position = add_active_position_from_entry(
                    row,
                    option_full,
                    filled_qty,
                    entry_price,
                    trade_status,
                    ib=ib,
                    account=ib_cfg.account,
                    stop_loss_pct=float(risk.get("stop_loss_pct", 20.0)),
                    take_profit_pct=float(risk.get("take_profit_pct", 30.0)),
                    trailing_stop_pct=float(risk.get("trailing_stop_pct", 10.0)),
                    trailing_from_entry=bool(risk.get("trailing_from_entry", True)),
                    fixed_take_profit_enabled=bool(risk.get("use_take_profit_with_trailing", False)),
                    entry_source=OPENING_ORB_SOURCE,
                )
            state = update_opening_orb_trade_state(
                status="OPEN" if filled_qty > 0 else "ORDER_SUBMITTED",
                symbol=symbol,
                signal=row["Signal"],
                score=row["Score"],
                rank_score=row["Rank Score"],
                option=option_full["Option"],
                con_id=getattr(option_full["Contract"], "conId", None),
                position_id=(active_position or {}).get("id"),
                broker_order_id=getattr(getattr(trade, "order", None), "orderId", None),
                opened_at=datetime.now(EASTERN).isoformat() if filled_qty > 0 else None,
                order_submitted_at=datetime.now(EASTERN).isoformat(),
                spend_limit=spend_limit,
                estimated_cost=estimated_cost,
            )
            scan_display_rows.append(scan_candidate_display_row(
                row,
                scan_id=scan_id,
                approval_mode=approval_mode,
                tradable=False,
                trade_status="Opening ORB submitted",
                block_reason=f"Opening ORB order submitted; broker status {trade_status}.",
                option_clean=option_clean,
                qty=qty,
                estimated_cost=estimated_cost,
                spend_limit=spend_limit,
                remaining_capital=max(0.0, remaining_capital - estimated_cost),
            ))
        except Exception as exc:
            state = update_opening_orb_trade_state(
                status="ORDER_SUBMITTED",
                symbol=symbol,
                signal=row["Signal"],
                option=option_full["Option"],
                con_id=getattr(option_full["Contract"], "conId", None),
                order_submitted_at=datetime.now(EASTERN).isoformat(),
                reason=f"Opening ORB submission outcome requires broker reconciliation: {exc}",
            )
            app_log(f"{symbol}: opening ORB order submission failed: {exc}", "ERROR")
        break

    finished_at = datetime.now(EASTERN)
    if not selected and opening_orb_trade_window_phase(cfg, finished_at) == "after":
        state = update_opening_orb_trade_state(
            finished_at,
            status="WINDOW_EXPIRED",
            completed_at=finished_at.isoformat(),
            reason="No affordable clean option was found by 09:45 ET.",
        )
    write_current_scan_candidates({
        "scan_id": scan_id,
        "generated_at": finished_at.isoformat(),
        "approval_mode": approval_mode,
        "entry_mode": OPENING_ORB_SOURCE,
        "candidates": sort_scan_display_rows(scan_display_rows),
        "message": "Opening ORB cycle complete",
    })
    write_health(
        last_scan_finish=finished_at.isoformat(),
        last_status="Opening ORB cycle complete",
        opening_orb_trade_status=state.get("status"),
        candidates=len(candidates),
        ib_connected=True,
        last_error="",
    )
    return state


def run_cycle(*, opening_trade_only: bool = False) -> None:
    cfg = load_config()
    mode = cfg.get("account_mode", "Simulation")
    ibs = cfg.get("ib", {})
    strategy = dict(cfg.get("strategy", {}))
    risk = cfg.get("risk", {})
    order = cfg.get("order", {})
    telegram = cfg.get("telegram", {})
    automation = cfg.get("automation", {})
    watchlist = combined_watchlist(cfg)
    approval_mode = approval_mode_from_config(cfg)
    scan_id = datetime.now(EASTERN).strftime("%Y%m%d%H%M%S")

    # Safety gate: the engine can run 24/7, but it will not connect, scan, alert,
    # or trade unless automation is enabled in config.json/dashboard.
    if not automation.get("enabled", False):
        write_health(
            engine_running=True,
            market_open=is_market_open_now(cfg),
            ib_connected=False,
            mode=mode,
            auto_trading=False,
            last_status="Automation disabled",
        )
        app_log("Automation disabled. Engine heartbeat only.")
        return

    market_open = is_market_open_now(cfg)
    if automation.get("scan_only_market_hours", True) and not market_open:
        write_health(engine_running=True, market_open=False, last_status="Waiting for market hours", mode=mode)
        app_log("Market closed. Waiting.")
        return

    ib_cfg = IBConfig(
        host=ibs.get("host", "127.0.0.1"),
        port=ib_port_from_config(cfg),
        client_id=int(ibs.get("client_id", 11)),
        account=ibs.get("account") or None,
        readonly=bool(ibs.get("readonly", False)),
    )
    tg_cfg = TelegramConfig(bot_token=telegram.get("bot_token", ""), chat_id=telegram.get("chat_id", ""))

    write_health(engine_running=True, market_open=market_open, ib_connected=False, mode=mode, last_status="Connecting to IBKR")
    app_log(f"Cycle started | mode={mode} | port={ib_cfg.port} | symbols={len(watchlist)}")

    ib = connect_ib(ib_cfg)
    write_health(ib_connected=bool(ib.isConnected()), last_scan_start=datetime.now(EASTERN).isoformat(), last_status="Connected", last_error="")
    try:
        imported, message = sync_today_executions_to_trade_log(ib, account=ib_cfg.account)
        if imported:
            app_log(f"IBKR execution sync before risk checks | {message}")
    except Exception as exc:
        app_log(f"IBKR execution sync before risk checks failed: {exc}", "WARN")

    try:
        sync_events = sync_active_positions_from_broker(
            ib,
            account=ib_cfg.account,
            stop_loss_pct=float(risk.get("stop_loss_pct", 20.0)),
            take_profit_pct=float(risk.get("take_profit_pct", 30.0)),
            trailing_stop_pct=float(risk.get("trailing_stop_pct", 10.0)),
            trailing_from_entry=bool(risk.get("trailing_from_entry", True)),
            fixed_take_profit_enabled=bool(risk.get("use_take_profit_with_trailing", False)),
            submit_protection=orders_unlocked_from_config(cfg) and market_open,
        )
        if sync_events:
            app_log(f"IBKR position sync restored active positions | events={len(sync_events)}")
    except Exception as exc:
        app_log(f"IBKR position sync failed: {exc}", "WARN")

    if should_auto_build(cfg):
        try:
            payload = build_premarket_watchlist(cfg, ib)
            watchlist = combined_watchlist(cfg)
            app_log(f"Dynamic watchlist built | symbols={payload.get('symbols', [])}")
        except Exception as exc:
            app_log(f"Dynamic watchlist build failed: {exc}", "WARN")
    can_trade = orders_unlocked_from_config(cfg)
    if can_trade:
        app_log("Order placement is ARMED for this cycle.", "WARN")
    else:
        app_log("Order placement disabled for this cycle. Signals/management still evaluated.")

    force_exit_time = dtime(int(risk.get("force_exit_hour", 15)), int(risk.get("force_exit_minute", 55)))
    management_events = manage_open_positions(
        ib=ib,
        account=ib_cfg.account,
        stop_loss_pct=float(risk.get("stop_loss_pct", 20.0)),
        take_profit_pct=float(risk.get("take_profit_pct", 30.0)),
        breakeven_trigger_pct=float(risk.get("breakeven_trigger_pct", 15.0)),
        trailing_trigger_pct=float(risk.get("trailing_trigger_pct", 25.0)),
        trailing_stop_pct=float(risk.get("trailing_stop_pct", 10.0)),
        force_exit_time=force_exit_time,
        force_exit_enabled=bool(risk.get("force_exit_enabled", True)),
        require_eod_exit_approval=bool(risk.get("require_eod_exit_approval", True)),
        allow_live_orders=can_trade,
        trailing_from_entry=bool(risk.get("trailing_from_entry", True)),
        fixed_take_profit_enabled=bool(risk.get("use_take_profit_with_trailing", False)),
    )
    if management_events:
        app_log(f"Managed open positions | events={len(management_events)}")
        exit_approval_events = request_eod_exit_approvals(tg_cfg, management_events, mode)
        if exit_approval_events:
            sent_count = sum(1 for event in exit_approval_events if event.get("Action") == "EXIT_APPROVAL_SENT")
            app_log(f"EOD exit approval requested | events={len(exit_approval_events)} | sent={sent_count}")
        close_alerts = notify_position_close_events(tg_cfg, management_events)
        if close_alerts:
            app_log(f"Telegram position close alerts sent | count={close_alerts}")

    consecutive_losses, realized_pnl_today = get_today_loss_stats()
    account_size = max(float(risk.get("account_size", 1000) or 1000), 1.0)
    liquidity_available: float | None = None
    if bool(risk.get("use_ibkr_buying_power", False)):
        try:
            summary_rows = ib.accountSummary()
            buying_power = None
            available_funds = None
            net_liquidation = None
            total_cash_value = None
            for item in summary_rows:
                tag = str(getattr(item, "tag", "") or "")
                value = getattr(item, "value", None)
                try:
                    numeric_value = float(value)
                except (TypeError, ValueError):
                    continue
                if tag == "BuyingPower":
                    buying_power = numeric_value
                elif tag == "AvailableFunds":
                    available_funds = numeric_value
                elif tag == "NetLiquidation":
                    net_liquidation = numeric_value
                elif tag == "TotalCashValue":
                    total_cash_value = numeric_value
            liquidity_available = available_funds if available_funds and available_funds > 0 else buying_power
            live_account_size = (
                net_liquidation
                if net_liquidation and net_liquidation > 0
                else total_cash_value
                if total_cash_value and total_cash_value > 0
                else liquidity_available
            )
            if live_account_size and live_account_size > 0:
                account_size = float(live_account_size)
                liquidity_note = f" | available_funds={liquidity_available:.2f}" if liquidity_available else ""
                app_log(f"Using live IBKR account value for option sizing | account_size={account_size:.2f}{liquidity_note}")
            else:
                app_log("IBKR account value unavailable; using saved account size for sizing.", "WARN")
        except Exception as exc:
            app_log(f"IBKR account value refresh failed; using saved account size: {exc}", "WARN")
    max_spend_per_trade = float(risk.get("max_spend_per_trade", 250))
    max_daily_capital = float(risk.get("max_daily_capital", 500))
    max_spend_pct = float(risk.get("max_spend_per_trade_pct", 0) or 0)
    max_daily_capital_pct = float(risk.get("max_daily_capital_pct", 0) or 0)
    if max_spend_pct > 0:
        max_spend_per_trade = account_size * max_spend_pct / 100.0
    if max_daily_capital_pct > 0:
        max_daily_capital = account_size * max_daily_capital_pct / 100.0

    max_daily_loss = -abs(account_size * float(risk.get("max_daily_drawdown_pct", 5.0)) / 100)
    if consecutive_losses >= int(risk.get("max_consecutive_losses", 2)) or realized_pnl_today <= max_daily_loss:
        app_log(f"Risk lock active | consecutive_losses={consecutive_losses} | pnl={realized_pnl_today}", "WARN")
        can_trade = False

    timeline_stage = staged_trading_timeline_stage(cfg)
    if timeline_stage.get("name") in {"orb_15m", "break_retest"}:
        strategy["orb_minutes"] = int(timeline_stage["orb_minutes"])
        strategy["require_break_retest"] = bool(timeline_stage["require_retest"])
    timeline_enabled = bool(staged_trading_timeline_config(cfg).get("enabled", False))
    if timeline_enabled:
        timeline = staged_trading_timeline_config(cfg)
        entry_cutoff_time = dtime(
            int(timeline.get("retest_end_hour", 13)),
            int(timeline.get("retest_end_minute", 30)),
        )
        cutoff_active = not bool(timeline_stage.get("entries_allowed", False))
    else:
        entry_cutoff_time = dtime(int(risk.get("entry_cutoff_hour", 11)), int(risk.get("entry_cutoff_minute", 0)))
        cutoff_active = datetime.now(EASTERN).time() >= entry_cutoff_time
    if cutoff_active:
        if can_trade:
            app_log(f"Entry cutoff active for {timeline_stage.get('name', 'manual')} at {entry_cutoff_time.strftime('%H:%M')} ET. New orders disabled for this cycle.", "WARN")
        can_trade = False

    if approval_mode == "Telegram":
        try:
            processed_callbacks = process_telegram_order_callbacks(ib, ib_cfg, tg_cfg, can_trade)
            if processed_callbacks:
                app_log(f"Processed {processed_callbacks} Telegram order approval callback(s)")
        except Exception as exc:
            if not is_telegram_polling_noise(exc):
                app_log(f"Telegram order approval callback check failed: {exc}", "WARN")

    expired = expire_open_scanner_approvals(scan_id)
    if expired:
        app_log(f"Expired {expired} open scanner approval candidate(s) for the new scan.")

    opening_state = refresh_opening_orb_trade_state(cfg, ib=ib)
    if opening_trade_only:
        run_opening_orb_trade_cycle(
            ib=ib,
            ib_cfg=ib_cfg,
            cfg=cfg,
            can_trade=can_trade,
            account_size=account_size,
            liquidity_available=liquidity_available,
            max_daily_capital=max_daily_capital,
            approval_mode=approval_mode,
            tg_cfg=tg_cfg,
        )
        try:
            ib.disconnect()
        except Exception:
            pass
        return
    if opening_orb_trade_pauses_normal_entries(cfg, opening_state):
        status = str(opening_state.get("status") or "WAITING")
        app_log(f"Normal scanner entries paused for opening ORB trade | status={status}")
        write_health(
            last_scan_finish=datetime.now(EASTERN).isoformat(),
            last_status=f"Normal entries paused for opening ORB trade ({status})",
            opening_orb_trade_status=status,
            ib_connected=True,
            last_error="",
        )
        try:
            ib.disconnect()
        except Exception:
            pass
        return

    candidates: list[dict] = []
    scan_display_rows: list[dict] = []
    fresh_scan_seen = False
    for symbol in watchlist:
        try:
            result = scan_symbol_ib(
                ib,
                symbol,
                bool(strategy.get("use_rvol_score", False)),
                str(strategy.get("active_strategy", "pmb")),
                int(strategy.get("orb_minutes", 15)),
                int(strategy.get("min_session_bars", 7)),
                require_retest=bool(strategy.get("require_break_retest", False)),
                retest_tolerance_pct=float(strategy.get("retest_tolerance_pct", 0.10)),
                retest_max_minutes=int(strategy.get("retest_max_minutes", 45)),
                intraday_bar_size="5 mins",
                analysis_bar_minutes=5,
            )
            if not result:
                log_engine_decision(symbol=symbol, decision="SCAN_NO_RESULT", reason="IBKR scan returned no PMB result")
                continue
            session_date = scan_result_session_date(result)
            today_et = datetime.now(EASTERN).date()
            if session_date != today_et:
                app_log(f"{symbol}: skipped stale scan result | session={session_date} | today={today_et}", "WARN")
                log_engine_decision(
                    symbol=symbol,
                    row=result,
                    decision="SKIP_STALE_DATA",
                    reason=f"scan session {session_date} did not match today {today_et}",
                )
                continue
            fresh_scan_seen = True
            passed_filters = is_top_candidate(
                result,
                float(strategy.get("min_score", 70)),
                0.0,
                float(strategy.get("min_rvol", 1.5)),
                float(strategy.get("min_atr", 0.3)),
                bool(strategy.get("use_rvol_filter", False)),
                bool(strategy.get("use_sr_filter", True)),
                float(strategy.get("min_sr_room_pct", 0.75)),
            )
            if not passed_filters:
                reject_reason = top_candidate_reject_reason(result, strategy)
                scan_display_rows.append(scan_candidate_display_row(
                    {
                        "Rank Score": opportunity_rank_score(result, None, bool(strategy.get("use_rvol_ranking", False))),
                        **clean_for_table(result),
                        "Option": "Not priced",
                    },
                    scan_id=scan_id,
                    approval_mode=approval_mode,
                    tradable=False,
                    trade_status="Scanner rejected",
                    block_reason=reject_reason,
                ))
                log_engine_decision(
                    symbol=symbol,
                    row=result,
                    decision="FILTER_REJECT",
                    reason=reject_reason,
                )
                continue

            candidate = {
                "Rank Score": opportunity_rank_score(result, None, bool(strategy.get("use_rvol_ranking", False))),
                **clean_for_table(result),
                "Option": "Not priced",
                "Mid": None,
                "Option Score": 0,
                "Qty": 0,
                "Estimated Cost": 0.0,
                "_option_full": None,
                "_option_clean": None,
            }
            candidates.append(candidate)
            log_engine_decision(symbol=symbol, row=candidate, decision="CANDIDATE", reason="passed stock setup filters")
        except Exception as exc:
            app_log(f"{symbol} scan error: {exc}", "ERROR")
            scan_display_rows.append({
                "scan_id": scan_id,
                "approval_mode": approval_mode,
                "approval_id": None,
                "tradable": False,
                "trade_status": "Scan error",
                "block_reason": str(exc),
                "symbol": symbol,
                "signal": None,
                "score": None,
                "confidence": None,
                "grade": None,
                "setup_quality": None,
                "rank_score": None,
                "price": None,
                "rvol": None,
                "atr_pct": None,
                "reasons": None,
                "option": "Not priced",
                "quantity": 0,
                "mid": None,
                "estimated_cost": 0.0,
            })
            log_engine_decision(symbol=symbol, decision="SCAN_ERROR", reason=str(exc))

    if can_trade and not fresh_scan_seen:
        app_log("Fresh same-day scan required before live entries. New orders disabled for this cycle.", "WARN")
        can_trade = False

    if not candidates:
        write_current_scan_candidates({
            "scan_id": scan_id,
            "generated_at": datetime.now(EASTERN).isoformat(),
            "approval_mode": approval_mode,
            "candidates": sort_scan_display_rows(scan_display_rows),
            "message": "No candidates passed scanner setup filters.",
        })
        write_health(last_scan_finish=datetime.now(EASTERN).isoformat(), last_status="No candidates", candidates=0, ib_connected=True, last_error="")
        app_log("No candidates passed filters.")
        try:
            ib.disconnect()
        except Exception:
            pass
        return

    df = pd.DataFrame(candidates).sort_values(
        ["Rank Score", "Score", "RVOL", "Option Score"],
        ascending=[False, False, False, False],
    )
    max_selected = int(strategy.get("top_n_tickers", 2))

    current_trade_count, daily_deployed = get_today_trade_stats()
    recycle_capital = bool(risk.get("recycle_capital_after_exit", False))
    current_deployed = get_open_position_deployed() if recycle_capital else daily_deployed
    remaining_trades = max(0, int(risk.get("max_trades_per_day", 2)) - current_trade_count)
    remaining_capital = max(0.0, max_daily_capital - current_deployed)
    reserve_capital = bool(risk.get("reserve_capital_for_remaining_trades", True))
    active_symbols = {p.get("symbol") for p in read_active_positions() if p.get("symbol")}
    try:
        active_symbols |= broker_open_option_symbols(ib, account=ib_cfg.account)
    except Exception as exc:
        app_log(f"IBKR active-symbol check failed; using local active positions only: {exc}", "WARN")

    submitted = 0
    selected_count = 0
    for _, row in df.iterrows():
        symbol = row["Symbol"]
        if selected_count >= max_selected:
            scan_display_rows.append(scan_candidate_display_row(
                row,
                scan_id=scan_id,
                approval_mode=approval_mode,
                tradable=False,
                trade_status="Not selected",
                block_reason=f"Outside top {max_selected} trade selection for this scan.",
                remaining_capital=remaining_capital,
                remaining_trades=remaining_trades,
            ))
            continue
        if remaining_trades <= 0:
            app_log(f"{symbol}: skipped max trades reached")
            scan_display_rows.append(scan_candidate_display_row(
                row,
                scan_id=scan_id,
                approval_mode=approval_mode,
                tradable=False,
                trade_status="Blocked",
                block_reason="Max trades per day reached.",
                remaining_capital=remaining_capital,
                remaining_trades=remaining_trades,
            ))
            log_engine_decision(
                symbol=symbol,
                row=row,
                decision="SKIP_MAX_TRADES",
                reason="max trades per day reached",
                remaining_capital=remaining_capital,
                remaining_trades=remaining_trades,
                max_spend_per_trade=max_spend_per_trade,
                max_daily_capital=max_daily_capital,
            )
            continue
        if symbol in active_symbols:
            app_log(f"{symbol}: skipped active position already exists")
            scan_display_rows.append(scan_candidate_display_row(
                row,
                scan_id=scan_id,
                approval_mode=approval_mode,
                tradable=False,
                trade_status="Blocked",
                block_reason="Active option position already exists.",
                remaining_capital=remaining_capital,
                remaining_trades=remaining_trades,
            ))
            log_engine_decision(
                symbol=symbol,
                row=row,
                decision="SKIP_ACTIVE_POSITION",
                reason="active option position already exists",
                remaining_capital=remaining_capital,
                remaining_trades=remaining_trades,
                max_spend_per_trade=max_spend_per_trade,
                max_daily_capital=max_daily_capital,
            )
            continue

        spend_limit = min(max_spend_per_trade, remaining_capital)
        if liquidity_available and liquidity_available > 0:
            spend_limit = min(spend_limit, liquidity_available)
        if reserve_capital and remaining_trades > 0:
            spend_limit = min(spend_limit, remaining_capital / remaining_trades)

        try:
            option_filters = dict(cfg.get("option_filters", {}) or {})
            option_filters["_max_contract_cost"] = spend_limit
            option_full = recommend_option_ib(
                ib,
                symbol,
                row["Signal"],
                float(row["Price"]),
                int(strategy.get("option_dte", 7)),
                option_filters,
            )
        except Exception as exc:
            app_log(f"{symbol}: option pricing error: {exc}", "ERROR")
            scan_display_rows.append(scan_candidate_display_row(
                row,
                scan_id=scan_id,
                approval_mode=approval_mode,
                tradable=False,
                trade_status="Blocked",
                block_reason=f"Option pricing error: {exc}",
                remaining_capital=remaining_capital,
                remaining_trades=remaining_trades,
            ))
            log_engine_decision(
                symbol=symbol,
                row=row,
                decision="SKIP_OPTION_ERROR",
                reason=str(exc),
                remaining_capital=remaining_capital,
                remaining_trades=remaining_trades,
                max_spend_per_trade=max_spend_per_trade,
                max_daily_capital=max_daily_capital,
            )
            continue
        option_clean = {k: v for k, v in option_full.items() if k != "Contract"} if option_full else None
        qty = calculate_contract_quantity(float(option_full["Mid"]), spend_limit, int(risk.get("max_contracts", 2))) if option_full else 0
        estimated_cost = round(qty * float(option_full["Mid"]) * 100, 2) if option_full and qty else 0.0
        row["Option"] = option_clean["Option"] if option_clean else "No clean contract"
        row["Mid"] = option_clean["Mid"] if option_clean else None
        row["Option Score"] = option_clean["Option Score"] if option_clean else 0
        row["Qty"] = qty
        row["Estimated Cost"] = estimated_cost
        row["_option_full"] = option_full
        row["_option_clean"] = option_clean

        if qty <= 0 or option_full is None:
            mid = row.get("Mid")
            contract_cost = float(mid) * 100 if mid else 0.0
            if option_full and contract_cost > max_spend_per_trade:
                block_reason = f"One contract costs about ${contract_cost:,.2f}; per-trade limit is ${max_spend_per_trade:,.2f}."
            elif option_full and contract_cost > remaining_capital:
                block_reason = f"One contract costs about ${contract_cost:,.2f}; remaining daily capital is ${remaining_capital:,.2f}."
            elif option_full and liquidity_available and liquidity_available > 0 and contract_cost > liquidity_available:
                block_reason = f"One contract costs about ${contract_cost:,.2f}; IBKR available funds are ${liquidity_available:,.2f}."
            else:
                block_reason = f"No affordable clean option. One contract costs about ${contract_cost:,.2f}; spend limit is ${spend_limit:,.2f}."
            app_log(f"{symbol}: skipped no affordable clean option | mid={mid} | contract_cost={contract_cost:.2f} | spend_limit={spend_limit:.2f}")
            scan_display_rows.append(scan_candidate_display_row(
                row,
                scan_id=scan_id,
                approval_mode=approval_mode,
                tradable=False,
                trade_status="Blocked",
                block_reason=block_reason,
                option_clean=option_clean,
                qty=qty,
                estimated_cost=estimated_cost,
                spend_limit=spend_limit,
                remaining_capital=remaining_capital,
                remaining_trades=remaining_trades,
            ))
            log_engine_decision(
                symbol=symbol,
                row=row,
                option=option_clean,
                qty=qty,
                estimated_cost=estimated_cost,
                decision="SKIP_NO_CLEAN_OPTION",
                reason=f"no clean option or one contract exceeded spend limit {spend_limit:.2f}",
                remaining_capital=remaining_capital,
                remaining_trades=remaining_trades,
                spend_limit=spend_limit,
                max_spend_per_trade=max_spend_per_trade,
                max_daily_capital=max_daily_capital,
            )
            continue
        if estimated_cost > remaining_capital:
            app_log(f"{symbol}: skipped max daily capital reached")
            scan_display_rows.append(scan_candidate_display_row(
                row,
                scan_id=scan_id,
                approval_mode=approval_mode,
                tradable=False,
                trade_status="Blocked",
                block_reason=f"Estimated cost ${estimated_cost:,.2f} exceeds remaining daily capital ${remaining_capital:,.2f}.",
                option_clean=option_clean,
                qty=qty,
                estimated_cost=estimated_cost,
                spend_limit=spend_limit,
                remaining_capital=remaining_capital,
                remaining_trades=remaining_trades,
            ))
            log_engine_decision(
                symbol=symbol,
                row=row,
                option=option_clean,
                qty=qty,
                estimated_cost=estimated_cost,
                decision="SKIP_DAILY_CAPITAL",
                reason="estimated cost exceeds remaining daily capital",
                remaining_capital=remaining_capital,
                remaining_trades=remaining_trades,
                spend_limit=spend_limit,
                max_spend_per_trade=max_spend_per_trade,
                max_daily_capital=max_daily_capital,
            )
            continue
        if estimated_cost > spend_limit:
            app_log(f"{symbol}: skipped per-trade cap exceeded | estimated_cost={estimated_cost:.2f} | spend_limit={spend_limit:.2f}", "WARN")
            scan_display_rows.append(scan_candidate_display_row(
                row,
                scan_id=scan_id,
                approval_mode=approval_mode,
                tradable=False,
                trade_status="Blocked",
                block_reason=f"Estimated cost ${estimated_cost:,.2f} exceeds per-trade spend limit ${spend_limit:,.2f}.",
                option_clean=option_clean,
                qty=qty,
                estimated_cost=estimated_cost,
                spend_limit=spend_limit,
                remaining_capital=remaining_capital,
                remaining_trades=remaining_trades,
            ))
            log_engine_decision(
                symbol=symbol,
                row=row,
                option=option_clean,
                qty=qty,
                estimated_cost=estimated_cost,
                decision="SKIP_TRADE_BUDGET",
                reason="estimated cost exceeds reserved trade budget",
                remaining_capital=remaining_capital,
                remaining_trades=remaining_trades,
                spend_limit=spend_limit,
                max_spend_per_trade=max_spend_per_trade,
                max_daily_capital=max_daily_capital,
            )
            continue
        selected_count += 1

        if telegram.get("send_alerts", False):
            ok = send_telegram_message(tg_cfg, make_alert_message(row.to_dict(), option_clean))
            log_alert({"timestamp": datetime.now(EASTERN).isoformat(), "symbol": symbol, "signal": row["Signal"], "score": row["Score"], "grade": row.get("Grade"), "telegram_sent": ok})

        if can_trade:
            entry_order_type = str(order.get("type", "LIMIT") or "LIMIT").upper()
            limit_price = option_full["Mid"] if entry_order_type == "LIMIT" else None
            if approval_mode != "Automatic":
                existing = [
                    pending for pending in read_pending_approvals()
                    if str(pending.get("status", "")).lower() in ["pending", "sent"]
                    and pending.get("symbol") == symbol
                    and pending.get("option") == option_full["Option"]
                ]
                if existing:
                    app_log(f"{symbol}: approval already pending | {option_full['Option']}")
                    scan_display_rows.append(scan_candidate_display_row(
                        row,
                        scan_id=scan_id,
                        approval_mode=approval_mode,
                        tradable=True,
                        trade_status="Approval already pending",
                        block_reason="This option already has a pending approval.",
                        option_clean=option_clean,
                        qty=qty,
                        estimated_cost=estimated_cost,
                        spend_limit=spend_limit,
                        remaining_capital=remaining_capital,
                        remaining_trades=remaining_trades,
                        approval_id=str(existing[0].get("id") or ""),
                    ))
                    log_engine_decision(
                        symbol=symbol,
                        row=row,
                        option=option_clean,
                        qty=qty,
                        estimated_cost=estimated_cost,
                        decision="SKIP_APPROVAL_PENDING",
                        reason=f"approval already pending for {option_full['Option']}",
                        remaining_capital=remaining_capital,
                        remaining_trades=remaining_trades,
                        spend_limit=spend_limit,
                        max_spend_per_trade=max_spend_per_trade,
                        max_daily_capital=max_daily_capital,
                    )
                    continue
                pending = create_pending_approval(
                    row,
                    option_clean,
                    option_full,
                    qty,
                    estimated_cost,
                    entry_order_type,
                    limit_price,
                    mode,
                    source="scanner",
                    scan_id=scan_id,
                    approval_mode=approval_mode,
                )
                sent = False
                approval_status = "Dashboard approval pending"
                if approval_mode == "Telegram":
                    try:
                        sent = send_order_approval_message(tg_cfg, pending)
                        approval_status = "Telegram approval sent" if sent else "Telegram approval pending"
                    except Exception as exc:
                        sent = False
                        update_pending_approval(pending["id"], status="approval_send_failed", error=str(exc))
                        app_log(f"{symbol}: Telegram order approval send failed | id={pending.get('id')} | {exc}", "WARN")
                scan_display_rows.append(scan_candidate_display_row(
                    row,
                    scan_id=scan_id,
                    approval_mode=approval_mode,
                    tradable=True,
                    trade_status=approval_status,
                    option_clean=option_clean,
                    qty=qty,
                    estimated_cost=estimated_cost,
                    spend_limit=spend_limit,
                    remaining_capital=remaining_capital,
                    remaining_trades=remaining_trades,
                    approval_id=str(pending.get("id") or ""),
                ))
                save_trade_replay(
                    row.to_dict(),
                    option_clean,
                    event="PENDING_APPROVAL",
                    order_status=approval_status,
                    quantity=qty,
                    entry_price=float(option_full["Mid"]) if option_full else None,
                    estimated_cost=estimated_cost,
                    notes=f"Pending {approval_mode} approval: {pending.get('id')}",
                )
                app_log(f"{symbol}: order approval requested | mode={approval_mode} | sent={sent} | id={pending.get('id')}")
                log_engine_decision(
                    symbol=symbol,
                    row=row,
                    option=option_clean,
                    qty=qty,
                    estimated_cost=estimated_cost,
                    decision="PENDING_APPROVAL",
                    reason=f"{approval_mode} approval requested; sent={sent}; id={pending.get('id')}",
                    remaining_capital=remaining_capital,
                    remaining_trades=remaining_trades,
                    spend_limit=spend_limit,
                    max_spend_per_trade=max_spend_per_trade,
                    max_daily_capital=max_daily_capital,
                )
                continue
            trade = place_option_order(ib, option_full["Contract"], "BUY", qty, entry_order_type, limit_price, ib_cfg.account)
            fallback_entry_price = float(limit_price if limit_price else option_full["Mid"])
            fill = trade_fill_details(trade, qty, fallback_entry_price)
            status = str(fill["status"])
            filled_qty = int(fill.get("filled_qty") or 0)
            entry_price = float(fill.get("avg_fill_price") or fallback_entry_price)
            log_trade({
                "timestamp": datetime.now(EASTERN).isoformat(),
                "event": "ENTRY",
                "account_mode": mode,
                "symbol": symbol,
                "signal": row["Signal"],
                "option": option_full["Option"],
                "quantity": qty,
                "filled_quantity": filled_qty,
                "remaining_quantity": fill.get("remaining_qty"),
                "order_type": entry_order_type,
                "limit_price": entry_price,
                "estimated_cost": estimated_cost,
                "status": status,
                "broker_status": fill.get("raw_status"),
                "delta": option_full.get("Delta"),
                "estimated_delta": option_full.get("Estimated Delta"),
                "gamma": option_full.get("Gamma"),
                "theta": option_full.get("Theta"),
                "theta_pct_mid": option_full.get("Theta % Mid"),
                "vega": option_full.get("Vega"),
                "implied_vol": option_full.get("Implied Vol"),
                "greek_source": option_full.get("Greek Source"),
                "bid": option_full.get("Bid"),
                "ask": option_full.get("Ask"),
                "mid": option_full.get("Mid"),
                "spread_pct": option_full.get("Spread %"),
                "spread_dollars": option_full.get("Spread $"),
                "option_score": option_full.get("Option Score"),
                "option_score_notes": option_full.get("Option Score Notes"),
                "score": row["Score"],
                "confidence": row.get("Confidence"),
                "grade": row.get("Grade"),
                "setup_quality": row.get("Setup Quality"),
                "rank_score": row["Rank Score"],
                "orb_high": row.get("ORB High"),
                "orb_low": row.get("ORB Low"),
                "retest_confirmed": row.get("Retest Confirmed"),
                "retest_trigger_level": row.get("Retest Trigger Level"),
                "breakout_time": row.get("Breakout Time"),
                "retest_time": row.get("Retest Time"),
                "retest_minutes_after_breakout": row.get("Retest Minutes After Breakout"),
                "retest_reason": row.get("Retest Reason"),
                "pdh": row.get("PDH"),
                "pdl": row.get("PDL"),
                "vwap": row.get("VWAP"),
                "rvol": row.get("RVOL"),
                "atr_pct": row.get("ATR %"),
                "reasons": row.get("Reasons"),
            })
            save_trade_replay(
                row.to_dict(),
                option_clean,
                event="ENTRY",
                order_status=status,
                quantity=filled_qty or qty,
                entry_price=entry_price,
                estimated_cost=estimated_cost,
                notes="Auto trader entry submitted",
            )
            if filled_qty > 0:
                take_profit_pct = float(risk.get("take_profit_pct", 30.0))
                add_active_position_from_entry(
                    row,
                    option_full,
                    filled_qty,
                    entry_price,
                    status,
                    ib=ib,
                    account=ib_cfg.account,
                    stop_loss_pct=float(risk.get("stop_loss_pct", 20.0)),
                    take_profit_pct=take_profit_pct,
                    trailing_stop_pct=float(risk.get("trailing_stop_pct", 10.0)),
                    trailing_from_entry=bool(risk.get("trailing_from_entry", True)),
                    fixed_take_profit_enabled=bool(risk.get("use_take_profit_with_trailing", False)),
                )
            submitted += 1
            remaining_trades -= 1
            remaining_capital -= estimated_cost
            active_symbols.add(symbol)
            scan_display_rows.append(scan_candidate_display_row(
                row,
                scan_id=scan_id,
                approval_mode=approval_mode,
                tradable=False,
                trade_status="Submitted",
                block_reason=f"Automatic order submitted; broker status {status}.",
                option_clean=option_clean,
                qty=qty,
                estimated_cost=estimated_cost,
                spend_limit=spend_limit,
                remaining_capital=remaining_capital,
                remaining_trades=remaining_trades,
            ))
            app_log(f"{symbol}: order submitted | qty={qty} | status={status}")
            log_engine_decision(
                symbol=symbol,
                row=row,
                option=option_clean,
                qty=qty,
                estimated_cost=estimated_cost,
                decision="ORDER_SUBMITTED",
                reason=f"broker status {status}",
                remaining_capital=remaining_capital,
                remaining_trades=remaining_trades,
                spend_limit=spend_limit,
                max_spend_per_trade=max_spend_per_trade,
                max_daily_capital=max_daily_capital,
            )
        else:
            scan_display_rows.append(scan_candidate_display_row(
                row,
                scan_id=scan_id,
                approval_mode=approval_mode,
                tradable=False,
                trade_status="Blocked",
                block_reason="Order placement is disabled by mode, safety gates, risk lock, cutoff time, or settings.",
                option_clean=option_clean,
                qty=qty,
                estimated_cost=estimated_cost,
                spend_limit=spend_limit,
                remaining_capital=remaining_capital,
                remaining_trades=remaining_trades,
            ))
            save_trade_replay(
                row.to_dict(),
                option_clean,
                event="SIGNAL_ONLY",
                order_status="Trading disabled",
                quantity=qty,
                entry_price=float(option_full["Mid"]) if option_full else None,
                estimated_cost=estimated_cost,
                notes="Qualified setup recorded without order placement",
            )
            app_log(f"{symbol}: signal only | {row['Signal']} | score={row['Score']} | grade={row.get('Grade', 'N/A')}")
            log_engine_decision(
                symbol=symbol,
                row=row,
                option=option_clean,
                qty=qty,
                estimated_cost=estimated_cost,
                decision="SIGNAL_ONLY",
                reason="order placement disabled by mode/risk/settings",
                remaining_capital=remaining_capital,
                remaining_trades=remaining_trades,
                spend_limit=spend_limit,
                max_spend_per_trade=max_spend_per_trade,
                max_daily_capital=max_daily_capital,
            )

    write_current_scan_candidates({
        "scan_id": scan_id,
        "generated_at": datetime.now(EASTERN).isoformat(),
        "approval_mode": approval_mode,
        "candidates": sort_scan_display_rows(scan_display_rows),
        "message": "Cycle complete",
    })
    write_health(last_scan_finish=datetime.now(EASTERN).isoformat(), last_status="Cycle complete", candidates=len(candidates), submitted_orders=submitted, auto_trading=can_trade, ib_connected=True, last_error="")
    try:
        ib.disconnect()
    except Exception:
        pass


def run_broker_sync_cycle() -> None:
    cfg = load_config()
    mode = cfg.get("account_mode", "Simulation")
    ibs = cfg.get("ib", {})
    risk = cfg.get("risk", {})
    automation = cfg.get("automation", {})

    if not automation.get("enabled", False):
        write_health(engine_running=True, mode=mode, last_status="Automation disabled")
        return

    market_open = is_market_open_now(cfg)

    ib_cfg = IBConfig(
        host=ibs.get("host", "127.0.0.1"),
        port=ib_port_from_config(cfg),
        client_id=int(ibs.get("client_id", 11)),
        account=ibs.get("account") or None,
        readonly=bool(ibs.get("readonly", False)),
    )
    ib = connect_ib(ib_cfg)
    write_health(ib_connected=bool(ib.isConnected()), mode=mode, last_status="IBKR sync connected", last_error="")
    try:
        if tg_cfg := TelegramConfig(bot_token=cfg.get("telegram", {}).get("bot_token", ""), chat_id=cfg.get("telegram", {}).get("chat_id", "")):
            try:
                processed_callbacks = process_telegram_order_callbacks(
                    ib,
                    ib_cfg,
                    tg_cfg,
                    can_trade=orders_unlocked_from_config(cfg) and market_open,
                )
                if processed_callbacks:
                    app_log(f"Processed {processed_callbacks} Telegram order approval callback(s)")
            except Exception as exc:
                if is_telegram_polling_noise(exc):
                    app_log(f"Telegram order approval polling skipped: {exc}", "WARN")
                else:
                    app_log(f"Telegram order approval callback check failed: {exc}", "WARN")

        imported, message = sync_today_executions_to_trade_log(ib, account=ib_cfg.account)
        if imported:
            app_log(f"IBKR execution sync | {message}")

        added_events = sync_active_positions_from_broker(
            ib,
            account=ib_cfg.account,
            stop_loss_pct=float(risk.get("stop_loss_pct", 20.0)),
            take_profit_pct=float(risk.get("take_profit_pct", 30.0)),
            trailing_stop_pct=float(risk.get("trailing_stop_pct", 10.0)),
            trailing_from_entry=bool(risk.get("trailing_from_entry", True)),
            fixed_take_profit_enabled=bool(risk.get("use_take_profit_with_trailing", False)),
            submit_protection=orders_unlocked_from_config(cfg) and market_open,
        )
        if automation.get("scan_only_market_hours", True) and not market_open:
            position_refresh_at = datetime.now(EASTERN).isoformat()
            opening_state = refresh_opening_orb_trade_state(cfg, ib=ib)
            write_health(
                engine_running=True,
                market_open=False,
                ib_connected=True,
                mode=mode,
                last_broker_sync=position_refresh_at,
                last_position_refresh=position_refresh_at,
                positions_monitored=len(read_active_positions()),
                opening_orb_trade_status=opening_state.get("status"),
                last_status="IBKR position sync complete outside market hours",
            )
            return
        position_events = manage_open_positions(
            ib=ib,
            account=ib_cfg.account,
            stop_loss_pct=float(risk.get("stop_loss_pct", 20.0)),
            take_profit_pct=float(risk.get("take_profit_pct", 30.0)),
            breakeven_trigger_pct=float(risk.get("breakeven_trigger_pct", 15.0)),
            trailing_trigger_pct=float(risk.get("trailing_trigger_pct", 25.0)),
            trailing_stop_pct=float(risk.get("trailing_stop_pct", 10.0)),
            force_exit_time=dtime(int(risk.get("force_exit_hour", 15)), int(risk.get("force_exit_minute", 55))),
            force_exit_enabled=bool(risk.get("force_exit_enabled", True)),
            require_eod_exit_approval=bool(risk.get("require_eod_exit_approval", True)),
            allow_live_orders=orders_unlocked_from_config(cfg),
            trailing_from_entry=bool(risk.get("trailing_from_entry", True)),
            fixed_take_profit_enabled=bool(risk.get("use_take_profit_with_trailing", False)),
        )
        sync_events = list(added_events or []) + list(position_events or [])
        exit_approval_events = request_eod_exit_approvals(tg_cfg, sync_events, mode)
        if exit_approval_events:
            sent_count = sum(1 for event in exit_approval_events if event.get("Action") == "EXIT_APPROVAL_SENT")
            app_log(f"EOD exit approval requested | events={len(exit_approval_events)} | sent={sent_count}")
        material_events = [
            event for event in sync_events
            if str(event.get("Action") or "").upper() not in {"HOLD", "EXIT_APPROVAL_REQUIRED"}
        ]
        if material_events:
            app_log(f"IBKR live position monitor updated positions | events={len(material_events)}")
            close_alerts = notify_position_close_events(
                TelegramConfig(
                    bot_token=cfg.get("telegram", {}).get("bot_token", ""),
                    chat_id=cfg.get("telegram", {}).get("chat_id", ""),
                ),
                material_events,
            )
            if close_alerts:
                app_log(f"Telegram position close alerts sent | count={close_alerts}")
        opening_state = refresh_opening_orb_trade_state(cfg, ib=ib)
        position_refresh_at = datetime.now(EASTERN).isoformat()
        write_health(
            engine_running=True,
            market_open=market_open,
            ib_connected=True,
            last_broker_sync=position_refresh_at,
            last_position_refresh=position_refresh_at,
            positions_monitored=len(read_active_positions()),
            opening_orb_trade_status=opening_state.get("status"),
            last_status="IBKR live position monitor complete",
        )
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


def main() -> None:
    if not claim_single_engine_instance():
        return
    app_log("Engine started.")
    write_health(engine_running=True, started_at=datetime.now(EASTERN).isoformat(), last_status="Engine started")
    last_scan = 0.0
    last_sync = 0.0
    last_opening_scan = 0.0
    next_scan_at: datetime | None = None
    previous_scan_interval = 0
    previous_schedule_stage = ""
    while True:
        try:
            cfg = load_config()
            automation = cfg.get("automation", {})
            scan_interval = max(10, int(automation.get("scan_interval_seconds", 60)))
            sync_interval = max(5, int(automation.get("live_sync_interval_seconds", 15)))
            align_scans = bool(automation.get("align_scans_to_interval", scan_interval >= 300))
            first_scan_time = dtime(
                int(automation.get("first_scan_hour", 9)),
                int(automation.get("first_scan_minute", 45)),
            )
            now_wall = datetime.now(EASTERN)
            timeline_stage = staged_trading_timeline_stage(cfg, now_wall)
            schedule_stage = str(timeline_stage.get("name") or "manual")
            normal_scanner_enabled = schedule_stage not in {"before", "opening_orb", "after"}
            if schedule_stage in {"orb_15m", "break_retest"}:
                scan_interval = int(timeline_stage["scan_interval_seconds"])
                align_scans = True
                first_scan_time = timeline_stage["first_scan_time"]
            now = time.monotonic()

            if now - last_sync >= sync_interval:
                run_broker_sync_cycle()
                last_sync = time.monotonic()

            run_backtester_cache_warmup_if_due()

            opening_cfg = opening_orb_trade_config(cfg)
            opening_scan_interval = max(10, int(opening_cfg.get("scan_interval_seconds", 30)))
            now_wall = datetime.now(EASTERN)
            if opening_orb_trade_needs_scan(cfg, now_wall) and now - last_opening_scan >= opening_scan_interval:
                run_cycle(opening_trade_only=True)
                last_opening_scan = time.monotonic()
                last_sync = last_opening_scan
                now = last_opening_scan

            if schedule_stage != previous_schedule_stage:
                next_scan_at = None
                previous_schedule_stage = schedule_stage
                app_log(f"Trading timeline stage changed to {schedule_stage}.")

            if not normal_scanner_enabled:
                next_scan_at = None
                previous_scan_interval = scan_interval
                next_scan = 5.0
            elif align_scans and scan_interval >= 60 and scan_interval % 60 == 0:
                now_wall = datetime.now(EASTERN)
                if next_scan_at is None or previous_scan_interval != scan_interval:
                    next_scan_at = next_aligned_scan_time(now_wall, scan_interval, first_scan_time)
                    previous_scan_interval = scan_interval
                    app_log(f"Next aligned scanner cycle scheduled at {next_scan_at.strftime('%H:%M:%S %Z')}.")
                if now_wall >= next_scan_at:
                    run_cycle()
                    last_scan = time.monotonic()
                    last_sync = last_scan
                    next_scan_at = next_aligned_scan_time(
                        datetime.now(EASTERN) + timedelta(seconds=91),
                        scan_interval,
                    )
                    app_log(f"Next aligned scanner cycle scheduled at {next_scan_at.strftime('%H:%M:%S %Z')}.")
                next_scan = max(1.0, (next_scan_at - datetime.now(EASTERN)).total_seconds())
            else:
                next_scan_at = None
                previous_scan_interval = scan_interval
                if now - last_scan >= scan_interval:
                    run_cycle()
                    last_scan = time.monotonic()
                    last_sync = last_scan
                next_scan = max(1.0, scan_interval - (time.monotonic() - last_scan))

            next_sync = max(1.0, sync_interval - (time.monotonic() - last_sync))
            time.sleep(min(next_sync, next_scan, 5.0))
        except KeyboardInterrupt:
            app_log("Engine stopped by user.")
            write_health(engine_running=False, last_status="Stopped by user")
            break
        except Exception:
            err = traceback.format_exc()
            app_log("Engine error:\n" + err, "ERROR")
            write_health(engine_running=True, last_status="Engine error", last_error=err)
            time.sleep(60)


if __name__ == "__main__":
    main()
