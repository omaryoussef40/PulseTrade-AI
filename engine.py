# engine.py
# Headless runner for AutoTrader. Keep this running on the VPS.

from __future__ import annotations

import time
import traceback
from datetime import datetime, time as dtime

import pandas as pd

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
    scan_symbol_ib,
    send_telegram_message,
    save_trade_replay,
    write_health,
)


def run_cycle() -> None:
    cfg = load_config()
    mode = cfg.get("account_mode", "Simulation")
    ibs = cfg.get("ib", {})
    strategy = cfg.get("strategy", {})
    risk = cfg.get("risk", {})
    order = cfg.get("order", {})
    telegram = cfg.get("telegram", {})
    automation = cfg.get("automation", {})
    watchlist = [s.strip().upper() for s in cfg.get("watchlist", []) if str(s).strip()]

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
    write_health(ib_connected=bool(ib.isConnected()), last_scan_start=datetime.now(EASTERN).isoformat(), last_status="Connected")
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
    max_daily_loss = -abs(float(risk.get("account_size", 1000)) * float(risk.get("max_daily_drawdown_pct", 5.0)) / 100)
    if consecutive_losses >= int(risk.get("max_consecutive_losses", 2)) or realized_pnl_today <= max_daily_loss:
        app_log(f"Risk lock active | consecutive_losses={consecutive_losses} | pnl={realized_pnl_today}", "WARN")
        can_trade = False

    candidates: list[dict] = []
    for symbol in watchlist:
        try:
            result = scan_symbol_ib(ib, symbol, bool(strategy.get("use_rvol_score", False)))
            if not result:
                continue
            if not is_top_candidate(
                result,
                float(strategy.get("min_score", 70)),
                float(strategy.get("min_confidence", 75)),
                float(strategy.get("min_rvol", 1.5)),
                float(strategy.get("min_atr", 0.3)),
                bool(strategy.get("use_rvol_filter", False)),
            ):
                continue

            opt = recommend_option_ib(ib, symbol, result["Signal"], result["Price"], int(strategy.get("option_dte", 7)))
            qty = calculate_contract_quantity(float(opt["Mid"]), float(risk.get("max_spend_per_trade", 250)), int(risk.get("max_contracts", 2))) if opt else 0
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
        return

    df = pd.DataFrame(candidates).sort_values(
        ["Rank Score", "Confidence", "Score", "RVOL", "Option Score"],
        ascending=[False, False, False, False, False],
    )
    selected = df.head(int(strategy.get("top_n_tickers", 2)))

    current_trade_count, current_deployed = get_today_trade_stats()
    remaining_trades = max(0, int(risk.get("max_trades_per_day", 2)) - current_trade_count)
    remaining_capital = max(0.0, float(risk.get("max_daily_capital", 500)) - current_deployed)
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
            log_alert({"timestamp": datetime.now(EASTERN).isoformat(), "symbol": symbol, "signal": row["Signal"], "score": row["Score"], "confidence": row["Confidence"], "telegram_sent": ok})

        if can_trade:
            limit_price = option_full["Mid"] if order.get("type", "LIMIT") == "LIMIT" else None
            trade = place_option_order(ib, option_full["Contract"], "BUY", qty, order.get("type", "LIMIT"), limit_price, ib_cfg.account)
            status = str(trade.orderStatus.status)
            entry_price = float(limit_price if limit_price else option_full["Mid"])
            log_trade({
                "timestamp": datetime.now(EASTERN).isoformat(),
                "event": "ENTRY",
                "account_mode": mode,
                "symbol": symbol,
                "signal": row["Signal"],
                "option": option_full["Option"],
                "quantity": qty,
                "order_type": order.get("type", "LIMIT"),
                "limit_price": entry_price,
                "estimated_cost": estimated_cost,
                "status": status,
                "score": row["Score"],
                "confidence": row["Confidence"],
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
                quantity=qty,
                entry_price=entry_price,
                estimated_cost=estimated_cost,
                notes="Auto trader entry submitted",
            )
            add_active_position_from_entry(row, option_full, qty, entry_price, status)
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
            app_log(f"{symbol}: signal only | {row['Signal']} | score={row['Score']} | confidence={row['Confidence']}")

    write_health(last_scan_finish=datetime.now(EASTERN).isoformat(), last_status="Cycle complete", candidates=len(candidates), submitted_orders=submitted, auto_trading=can_trade)


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
