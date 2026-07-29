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
from datetime import datetime, time as dtime
from html import escape
from pathlib import Path

import pandas as pd
import requests

from modules.premarket_watchlist import build_premarket_watchlist, combined_watchlist, should_auto_build
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
    reconcile_active_positions_with_broker,
    recommend_option_ib,
    reconstruct_option_contract,
    scan_symbol_ib,
    send_position_closed_telegram_message,
    send_telegram_message,
    save_trade_replay,
    sync_active_positions_from_broker,
    sync_today_executions_to_trade_log,
    trade_fill_details,
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


def notify_position_close_events(tg_cfg: TelegramConfig, events: list[dict] | None) -> int:
    sent = 0
    for event in events or []:
        try:
            if send_position_closed_telegram_message(tg_cfg, event):
                sent += 1
        except Exception as exc:
            app_log(f"Telegram close alert failed for {event.get('Symbol')}: {exc}", "WARN")
    return sent


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


def approval_message(order: dict) -> str:
    reasons = str(order.get("reasons") or "")
    if len(reasons) > 500:
        reasons = reasons[:500] + "..."
    limit_price = order.get("limit_price")
    limit_line = f"Limit: ${float(limit_price):.2f}" if limit_price not in [None, "", 0] else "Limit: N/A"
    return (
        "🚨 <b>PulseTrade Order Approval</b>\n\n"
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
        f"Estimated cost: ${float(order.get('estimated_cost') or 0):,.2f}\n"
        f"Order: {escape(str(order.get('order_type', 'LIMIT')))}\n"
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


def submit_approved_order(ib, ib_cfg: IBConfig, order: dict) -> str:
    contract = reconstruct_option_contract(order)
    qualified = ib.qualifyContracts(contract)
    if qualified:
        contract = qualified[0]
    qty = int(order.get("quantity") or 0)
    order_type = str(order.get("order_type") or "LIMIT")
    limit_price = float(order.get("limit_price") or order.get("mid") or 0) if order_type == "LIMIT" else None
    trade = place_option_order(ib, contract, "BUY", qty, order_type, limit_price, ib_cfg.account)
    fallback_entry_price = float(limit_price if limit_price else order.get("mid") or 0)
    fill = trade_fill_details(trade, qty, fallback_entry_price)
    status = str(fill["status"])
    filled_qty = int(fill.get("filled_qty") or 0)
    entry_price = float(fill.get("avg_fill_price") or fallback_entry_price)
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
    })
    save_trade_replay(
        order.get("raw_signal") or {},
        order.get("option_data") or {},
        event="ENTRY",
        order_status=status,
        quantity=filled_qty or qty,
        entry_price=entry_price,
        estimated_cost=order.get("estimated_cost"),
        notes=f"Telegram approval submitted: {order.get('id')}",
    )
    if filled_qty > 0:
        cfg = load_config()
        risk = cfg.get("risk", {})
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


def run_cycle() -> None:
    cfg = load_config()
    mode = cfg.get("account_mode", "Simulation")
    ibs = cfg.get("ib", {})
    strategy = cfg.get("strategy", {})
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
            submit_protection=orders_unlocked_from_config(cfg),
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
        allow_live_orders=can_trade,
    )
    if management_events:
        app_log(f"Managed open positions | events={len(management_events)}")
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

    entry_cutoff_time = dtime(int(risk.get("entry_cutoff_hour", 11)), int(risk.get("entry_cutoff_minute", 0)))
    if datetime.now(EASTERN).time() >= entry_cutoff_time:
        if can_trade:
            app_log(f"Entry cutoff active after {entry_cutoff_time.strftime('%H:%M')} ET. New orders disabled for this cycle.", "WARN")
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

        try:
            option_full = recommend_option_ib(
                ib,
                symbol,
                row["Signal"],
                float(row["Price"]),
                int(strategy.get("option_dte", 7)),
                cfg.get("option_filters", {}),
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
        spend_limit = min(max_spend_per_trade, remaining_capital)
        if liquidity_available and liquidity_available > 0:
            spend_limit = min(spend_limit, liquidity_available)
        if reserve_capital and remaining_trades > 0:
            spend_limit = min(spend_limit, remaining_capital / remaining_trades)
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

    if automation.get("scan_only_market_hours", True) and not is_market_open_now(cfg):
        write_health(engine_running=True, market_open=False, mode=mode, last_status="Waiting for market hours")
        return

    ib_cfg = IBConfig(
        host=ibs.get("host", "127.0.0.1"),
        port=ib_port_from_config(cfg),
        client_id=int(ibs.get("client_id", 11)) + 7,
        account=ibs.get("account") or None,
        readonly=bool(ibs.get("readonly", False)),
    )
    ib = connect_ib(ib_cfg)
    write_health(ib_connected=bool(ib.isConnected()), mode=mode, last_status="IBKR sync connected", last_error="")
    try:
        imported, message = sync_today_executions_to_trade_log(ib, account=ib_cfg.account)
        if imported:
            app_log(f"IBKR execution sync | {message}")

        added_events = sync_active_positions_from_broker(
            ib,
            account=ib_cfg.account,
            stop_loss_pct=float(risk.get("stop_loss_pct", 20.0)),
            take_profit_pct=float(risk.get("take_profit_pct", 30.0)),
            submit_protection=orders_unlocked_from_config(cfg),
        )
        reconcile_events = reconcile_active_positions_with_broker(ib, account=ib_cfg.account, log_closures=True)
        sync_events = list(added_events or []) + list(reconcile_events or [])
        if sync_events:
            app_log(f"IBKR live trade sync updated positions | events={len(sync_events)}")
            close_alerts = notify_position_close_events(
                TelegramConfig(
                    bot_token=cfg.get("telegram", {}).get("bot_token", ""),
                    chat_id=cfg.get("telegram", {}).get("chat_id", ""),
                ),
                sync_events,
            )
            if close_alerts:
                app_log(f"Telegram position close alerts sent | count={close_alerts}")
        write_health(
            engine_running=True,
            market_open=is_market_open_now(cfg),
            ib_connected=True,
            last_broker_sync=datetime.now(EASTERN).isoformat(),
            last_status="IBKR live trade sync complete",
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
    while True:
        try:
            cfg = load_config()
            automation = cfg.get("automation", {})
            scan_interval = max(10, int(automation.get("scan_interval_seconds", 60)))
            sync_interval = max(5, int(automation.get("live_sync_interval_seconds", 15)))
            now = time.monotonic()

            if now - last_sync >= sync_interval:
                run_broker_sync_cycle()
                last_sync = time.monotonic()

            if now - last_scan >= scan_interval:
                run_cycle()
                last_scan = time.monotonic()
                last_sync = last_scan

            next_sync = max(1.0, sync_interval - (time.monotonic() - last_sync))
            next_scan = max(1.0, scan_interval - (time.monotonic() - last_scan))
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
