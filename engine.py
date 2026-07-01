# engine.py
# Headless runner for AutoTrader. Keep this running on the VPS.

from __future__ import annotations

import json
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
    send_telegram_message,
    save_trade_replay,
    trade_fill_details,
    write_health,
)


BASE_DIR = Path(__file__).resolve().parent
EXPORT_DIR = BASE_DIR / "exports"
PENDING_APPROVALS_FILE = EXPORT_DIR / "pending_order_approvals.json"
TELEGRAM_APPROVAL_STATE_FILE = EXPORT_DIR / "telegram_approval_state.json"


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


def read_pending_approvals() -> list[dict]:
    data = read_json_file(PENDING_APPROVALS_FILE, [])
    return data if isinstance(data, list) else []


def write_pending_approvals(orders: list[dict]) -> None:
    write_json_file(PENDING_APPROVALS_FILE, orders)


def make_approval_id(symbol: str, signal: str) -> str:
    stamp = datetime.now(EASTERN).strftime("%Y%m%d%H%M%S%f")
    return f"{symbol}_{signal}_{stamp}".replace(" ", "").upper()


def create_pending_approval(row: pd.Series, option_clean: dict, option_full: dict, qty: int, estimated_cost: float, order_type: str, limit_price: float | None, mode: str) -> dict:
    order = {
        "id": make_approval_id(str(row["Symbol"]), str(row["Signal"])),
        "status": "pending",
        "created_at": datetime.now(EASTERN).isoformat(),
        "symbol": row["Symbol"],
        "signal": row["Signal"],
        "option": option_full["Option"],
        "expiry": option_full["Expiry"],
        "strike": option_full["Strike"],
        "type": option_full["Type"],
        "quantity": int(qty),
        "mid": option_full.get("Mid"),
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
    row = {"Symbol": order.get("symbol"), "Signal": order.get("signal")}
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
        add_active_position_from_entry(row, option_full, filled_qty, entry_price, status)
    update_pending_approval(order["id"], status="submitted", submitted_at=datetime.now(EASTERN).isoformat(), broker_status=status)
    return status


def process_telegram_order_callbacks(ib, ib_cfg: IBConfig, tg_cfg: TelegramConfig, can_trade: bool) -> int:
    if not tg_cfg.bot_token or not tg_cfg.chat_id:
        return 0
    state = read_json_file(TELEGRAM_APPROVAL_STATE_FILE, {})
    payload = {"timeout": 1}
    if state.get("offset") is not None:
        payload["offset"] = state["offset"]
    data = telegram_post(tg_cfg.bot_token, "getUpdates", payload, timeout=5)
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
        allow_live_orders=can_trade,
    )
    if management_events:
        app_log(f"Managed open positions | events={len(management_events)}")

    consecutive_losses, realized_pnl_today = get_today_loss_stats()
    account_size = max(float(risk.get("account_size", 1000) or 1000), 1.0)
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

    if automation.get("require_trade_approval", False):
        try:
            processed_callbacks = process_telegram_order_callbacks(ib, ib_cfg, tg_cfg, can_trade)
            if processed_callbacks:
                app_log(f"Processed {processed_callbacks} Telegram order approval callback(s)")
        except Exception as exc:
            app_log(f"Telegram order approval callback check failed: {exc}", "WARN")

    candidates: list[dict] = []
    for symbol in watchlist:
        try:
            result = scan_symbol_ib(
                ib,
                symbol,
                bool(strategy.get("use_rvol_score", False)),
                str(strategy.get("active_strategy", "pmb")),
                int(strategy.get("orb_minutes", 15)),
            )
            if not result:
                continue
            if not is_top_candidate(
                result,
                float(strategy.get("min_score", 70)),
                0.0,
                float(strategy.get("min_rvol", 1.5)),
                float(strategy.get("min_atr", 0.3)),
                bool(strategy.get("use_rvol_filter", False)),
            ):
                continue

            opt = recommend_option_ib(ib, symbol, result["Signal"], result["Price"], int(strategy.get("option_dte", 7)))
            qty = calculate_contract_quantity(float(opt["Mid"]), max_spend_per_trade, int(risk.get("max_contracts", 2))) if opt else 0
            estimated_cost = round(qty * float(opt["Mid"]) * 100, 2) if opt and qty else 0.0
            option_clean = {k: v for k, v in opt.items() if k != "Contract"} if opt else None

            candidates.append({
                "Rank Score": opportunity_rank_score(result, opt, bool(strategy.get("use_rvol_ranking", False))),
                **clean_for_table(result),
                "Option": option_clean["Option"] if option_clean else "No clean contract",
                "Mid": option_clean["Mid"] if option_clean else None,
                "Option Score": option_clean["Option Score"] if option_clean else 0,
                "Qty": qty,
                "Estimated Cost": estimated_cost,
                "_option_full": opt,
                "_option_clean": option_clean,
            })
        except Exception as exc:
            app_log(f"{symbol} scan error: {exc}", "ERROR")

    if not candidates:
        write_health(last_scan_finish=datetime.now(EASTERN).isoformat(), last_status="No candidates", candidates=0)
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
    selected = df.head(int(strategy.get("top_n_tickers", 2)))

    current_trade_count, current_deployed = get_today_trade_stats()
    remaining_trades = max(0, int(risk.get("max_trades_per_day", 2)) - current_trade_count)
    remaining_capital = max(0.0, max_daily_capital - current_deployed)
    active_symbols = {p.get("symbol") for p in read_active_positions()}

    submitted = 0
    for _, row in selected.iterrows():
        symbol = row["Symbol"]
        option_full = row["_option_full"]
        option_clean = row["_option_clean"]
        qty = int(row["Qty"] or 0)
        estimated_cost = float(row["Estimated Cost"] or 0)

        if qty <= 0 or option_full is None:
            app_log(f"{symbol}: skipped no affordable clean option")
            continue
        if remaining_trades <= 0:
            app_log(f"{symbol}: skipped max trades reached")
            continue
        if estimated_cost > remaining_capital:
            app_log(f"{symbol}: skipped max daily capital reached")
            continue
        if symbol in active_symbols:
            app_log(f"{symbol}: skipped active position already exists")
            continue

        if telegram.get("send_alerts", False):
            ok = send_telegram_message(tg_cfg, make_alert_message(row.to_dict(), option_clean))
            log_alert({"timestamp": datetime.now(EASTERN).isoformat(), "symbol": symbol, "signal": row["Signal"], "score": row["Score"], "grade": row.get("Grade"), "telegram_sent": ok})

        if can_trade:
            limit_price = option_full["Mid"] if order.get("type", "LIMIT") == "LIMIT" else None
            if automation.get("require_trade_approval", False):
                existing = [
                    pending for pending in read_pending_approvals()
                    if str(pending.get("status", "")).lower() in ["pending", "sent"]
                    and pending.get("symbol") == symbol
                    and pending.get("option") == option_full["Option"]
                ]
                if existing:
                    app_log(f"{symbol}: approval already pending | {option_full['Option']}")
                    continue
                pending = create_pending_approval(
                    row,
                    option_clean,
                    option_full,
                    qty,
                    estimated_cost,
                    order.get("type", "LIMIT"),
                    limit_price,
                    mode,
                )
                try:
                    sent = send_order_approval_message(tg_cfg, pending)
                except Exception as exc:
                    sent = False
                    update_pending_approval(pending["id"], status="approval_send_failed", error=str(exc))
                    app_log(f"{symbol}: Telegram order approval send failed | id={pending.get('id')} | {exc}", "WARN")
                save_trade_replay(
                    row.to_dict(),
                    option_clean,
                    event="PENDING_APPROVAL",
                    order_status="Telegram approval sent" if sent else "Telegram approval pending",
                    quantity=qty,
                    entry_price=float(option_full["Mid"]) if option_full else None,
                    estimated_cost=estimated_cost,
                    notes=f"Pending Telegram approval: {pending.get('id')}",
                )
                app_log(f"{symbol}: order approval requested | sent={sent} | id={pending.get('id')}")
                continue
            trade = place_option_order(ib, option_full["Contract"], "BUY", qty, order.get("type", "LIMIT"), limit_price, ib_cfg.account)
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
                "order_type": order.get("type", "LIMIT"),
                "limit_price": entry_price,
                "estimated_cost": estimated_cost,
                "status": status,
                "broker_status": fill.get("raw_status"),
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
                add_active_position_from_entry(row, option_full, filled_qty, entry_price, status)
            submitted += 1
            remaining_trades -= 1
            remaining_capital -= estimated_cost
            active_symbols.add(symbol)
            app_log(f"{symbol}: order submitted | qty={qty} | status={status}")
        else:
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

    write_health(last_scan_finish=datetime.now(EASTERN).isoformat(), last_status="Cycle complete", candidates=len(candidates), submitted_orders=submitted, auto_trading=can_trade, last_error="")
    try:
        ib.disconnect()
    except Exception:
        pass


def main() -> None:
    app_log("Engine started.")
    write_health(engine_running=True, started_at=datetime.now(EASTERN).isoformat(), last_status="Engine started")
    while True:
        try:
            cfg = load_config()
            interval = int(cfg.get("automation", {}).get("scan_interval_seconds", 60))
            run_cycle()
            time.sleep(max(10, interval))
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
