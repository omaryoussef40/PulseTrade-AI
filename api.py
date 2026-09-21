from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from bot_core import load_config, read_active_positions, read_health, trading_status_from_config
from engine import read_current_scan_candidates, read_pending_approvals, update_pending_approval


BASE_DIR = Path(__file__).resolve().parent
EXPORT_DIR = BASE_DIR / "exports"
LOG_DIR = BASE_DIR / "logs"

load_dotenv(BASE_DIR / ".env")

API_KEY = os.getenv("PULSETRADE_API_KEY", "").strip()
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("PULSETRADE_CORS_ORIGINS", "*").split(",")
    if origin.strip()
]

app = FastAPI(title="PulseTrade API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class EngineModeReq(BaseModel):
    mode: str


class PlaceOrderReq(BaseModel):
    symbol: str
    side: str
    qty: int
    type: str = "MKT"
    limit_price: float | None = None


class CancelOrderReq(BaseModel):
    order_id: str


class ClosePositionReq(BaseModel):
    symbol: str


class AccountSettingsReq(BaseModel):
    mode: str = "paper"
    max_position_size: float = 10000
    daily_loss_limit: float = 2500
    max_open_positions: int = 8
    allow_shorts: bool = True


class BacktestReq(BaseModel):
    strategy_id: str = "pmb"
    symbols: list[str] | None = None
    start_date: str | None = None
    end_date: str | None = None
    initial_capital: float = 100000
    risk_per_trade: float = 0.01


class ScannerReq(BaseModel):
    min_price: float = 5.0
    max_price: float = 1000.0
    min_rvol: float = 1.5
    min_score: int = 60
    setups: list[str] | None = None


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not API_KEY:
        raise HTTPException(status_code=500, detail="PULSETRADE_API_KEY is not configured")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def read_csv_rows(path: Path, limit: int = 100) -> list[dict[str, Any]]:
    try:
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        return rows[-limit:]
    except Exception:
        return []


def number(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def first_present(row: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


def trade_id(row: dict[str, Any], index: int) -> str:
    raw = first_present(row, "external_id", "timestamp", "symbol", default=str(index))
    return str(raw) or str(index)


def trade_pnl(row: dict[str, Any]) -> float:
    return number(first_present(row, "realized_pnl", "pnl", default=0))


def map_trade(row: dict[str, Any], index: int) -> dict[str, Any]:
    event = str(row.get("event") or "").upper()
    side = "SELL" if event in {"EXIT", "EXTERNAL_CLOSE"} else "BUY"
    return {
        "id": trade_id(row, index),
        "time": first_present(row, "timestamp", "time", default=iso_now()),
        "symbol": str(first_present(row, "symbol", default="")).upper(),
        "side": side,
        "qty": int(number(first_present(row, "quantity", "filled_quantity", default=0))),
        "price": number(first_present(row, "entry_price", "exit_price", "limit_price", default=0)),
        "pnl": trade_pnl(row),
        "strategy": first_present(row, "source", "grade", default="PulseTrade"),
        "status": first_present(row, "status", "broker_status", default=""),
    }


def map_position(position: dict[str, Any]) -> dict[str, Any]:
    qty = int(number(first_present(position, "quantity", "qty", default=0)))
    avg = number(first_present(position, "entry_price", "avg_price", "avg_cost", default=0))
    last = number(first_present(position, "current_price", "last", "mark", default=0))
    explicit_unrealized = first_present(position, "unrealized_pnl", "unrealized", "pnl", default=None)
    unrealized = number(explicit_unrealized, (last - avg) * qty * 100 if last and avg else 0)
    explicit_unrealized_pct = first_present(position, "unrealized_pct", "pnl_pct", default=None)
    unrealized_pct = number(
        explicit_unrealized_pct,
        ((last - avg) / avg * 100) if last and avg else 0,
    )
    side = str(first_present(position, "signal", "side", default="LONG")).upper()
    if side in {"CALL", "BUY"}:
        side = "LONG"
    elif side in {"PUT", "SELL"}:
        side = "SHORT"
    return {
        "symbol": str(first_present(position, "symbol", "Symbol", default="")).upper(),
        "side": side,
        "qty": qty,
        "avg_price": avg,
        "last": last,
        "unrealized": unrealized,
        "unrealized_pct": unrealized_pct,
        "strategy": first_present(position, "strategy", "setup_quality", default="PulseTrade"),
        "option": first_present(position, "option", default=""),
        "expiry": first_present(position, "expiry", default=""),
        "strike": number(first_present(position, "strike", default=0)),
        "option_type": str(first_present(position, "signal", default="")).upper(),
        "entry_time": first_present(position, "entry_time", default=""),
        "entry_status": first_present(position, "entry_status", default=""),
        "cost_basis": number(first_present(position, "cost_basis", default=avg * qty * 100)),
        "market_value": number(first_present(position, "market_value", default=last * qty * 100)),
        "stop_price": number(first_present(position, "current_stop_price", default=0)),
        "take_profit_price": number(first_present(position, "take_profit_price", default=0)),
        "premium_change_pct": number(first_present(position, "premium_change_pct", default=unrealized_pct)),
        "premium_health": first_present(position, "premium_health", default="Not checked"),
        "premium_health_detail": first_present(position, "premium_health_detail", default=""),
        "premium_health_checked_at": first_present(position, "premium_health_checked_at", default=""),
        "market_data_status": first_present(position, "market_data_status", default="Not checked"),
        "market_data_source": first_present(position, "market_data_source", default=""),
        "market_data_checked_at": first_present(position, "market_data_checked_at", default=""),
        "bid": number(first_present(position, "current_bid", default=0)),
        "ask": number(first_present(position, "current_ask", default=0)),
        "spread_pct": number(first_present(position, "current_spread_pct", default=0)),
        "delta": number(first_present(position, "current_delta", default=0)),
        "theta": number(first_present(position, "current_theta", default=0)),
        "implied_vol": number(first_present(position, "current_implied_vol", default=0)),
        "underlying_entry_price": number(first_present(position, "underlying_entry_price", default=0)),
        "underlying_current_price": number(first_present(position, "underlying_current_price", default=0)),
        "underlying_move_with_position_pct": number(
            first_present(position, "underlying_move_with_position_pct", default=0)
        ),
        "breakeven_active": bool(position.get("breakeven_active", False)),
        "trailing_active": bool(position.get("trailing_active", False)),
        "protective_orders_status": first_present(position, "protective_orders_status", default=""),
        "management_mode": first_present(position, "management_mode", default="automated"),
        "software_control_enabled": position.get("software_control_enabled", True) is not False,
    }


def get_recent_trades(limit: int = 100) -> list[dict[str, Any]]:
    rows = read_csv_rows(EXPORT_DIR / "trade_log.csv", limit=limit)
    return [map_trade(row, index) for index, row in enumerate(rows)]


def get_alerts(limit: int = 20) -> list[dict[str, Any]]:
    rows = read_csv_rows(EXPORT_DIR / "alert_log.csv", limit=limit)
    alerts = []
    for index, row in enumerate(rows):
        symbol = str(row.get("symbol") or "").upper()
        signal = str(row.get("signal") or "")
        grade = str(row.get("grade") or "")
        alerts.append(
            {
                "id": f"alert-{index}",
                "level": "info",
                "message": f"{symbol} {signal} signal {grade}".strip(),
                "time": row.get("timestamp") or iso_now(),
            }
        )
    return alerts


def equity_from_trades(trades: list[dict[str, Any]], starting_equity: float) -> list[dict[str, Any]]:
    equity = starting_equity
    curve = []
    for trade in trades[-96:]:
        equity += number(trade.get("pnl"))
        curve.append({"t": trade.get("time") or iso_now(), "v": round(equity, 2)})
    if not curve:
        curve.append({"t": iso_now(), "v": round(starting_equity, 2)})
    return curve


def public_config_summary(config: dict[str, Any]) -> dict[str, Any]:
    automation = config.get("automation", {}) if isinstance(config.get("automation"), dict) else {}
    risk = config.get("risk", {}) if isinstance(config.get("risk"), dict) else {}
    strategy = config.get("strategy", {}) if isinstance(config.get("strategy"), dict) else {}
    return {
        "account_mode": config.get("account_mode"),
        "trading_status": trading_status_from_config(config),
        "automation": {
            "enabled": bool(automation.get("enabled", False)),
            "place_orders": bool(automation.get("place_orders", False)),
            "approval_mode": automation.get("approval_mode"),
            "scan_interval_seconds": automation.get("scan_interval_seconds"),
        },
        "risk": {
            "account_size": risk.get("account_size"),
            "max_trades_per_day": risk.get("max_trades_per_day"),
            "allow_same_symbol_same_day": bool(risk.get("allow_same_symbol_same_day", False)),
            "max_spend_per_trade": risk.get("max_spend_per_trade"),
            "max_daily_capital": risk.get("max_daily_capital"),
        },
        "strategy": {
            "active_strategy": strategy.get("active_strategy"),
            "min_score": strategy.get("min_score"),
            "top_n_tickers": strategy.get("top_n_tickers"),
        },
        "watchlist": config.get("watchlist", []),
    }


def legacy_status_fields(
    config: dict[str, Any],
    health_data: dict[str, Any],
    positions: list[dict[str, Any]],
    pending_approvals: list[dict[str, Any]],
    recent_trades: list[dict[str, Any]],
) -> dict[str, Any]:
    mode = str(health_data.get("mode") or config.get("account_mode") or "Unknown")
    engine_running = bool(health_data.get("engine_running", False))
    broker_connected = bool(health_data.get("ib_connected", False))
    return {
        "engine": "running" if engine_running else "stopped",
        "engine_running": engine_running,
        "mode": mode.lower(),
        "account_mode": mode,
        "broker_connected": broker_connected,
        "ib_connected": broker_connected,
        "market_open": bool(health_data.get("market_open", False)),
        "auto_trading": bool(health_data.get("auto_trading", False)),
        "last_status": health_data.get("last_status", ""),
        "last_error": health_data.get("last_error", ""),
        "last_scan_start": health_data.get("last_scan_start"),
        "last_scan_finish": health_data.get("last_scan_finish"),
        "updated_at": health_data.get("updated_at"),
        "positions_count": len(positions),
        "pending_approvals_count": len(pending_approvals),
        "recent_trades_count": len(recent_trades),
        "trading_status": trading_status_from_config(config),
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "pulsetrade-api",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/status", dependencies=[Depends(require_api_key)])
def status() -> dict[str, Any]:
    config = load_config()
    health_data = read_health()
    positions = read_active_positions()
    approvals = read_pending_approvals()
    pending_approvals = [
        order
        for order in approvals
        if str(order.get("status", "")).lower() in {"pending", "sent", "approval_send_failed"}
    ]
    recent_trades = read_csv_rows(EXPORT_DIR / "trade_log.csv", limit=50)
    recent_alerts = read_csv_rows(EXPORT_DIR / "alert_log.csv", limit=50)
    return {
        "ok": True,
        **legacy_status_fields(config, health_data, positions, pending_approvals, recent_trades),
        "health": health_data,
        "config": public_config_summary(config),
        "positions": positions,
        "positions_count": len(positions),
        "pending_approvals": pending_approvals,
        "pending_approvals_count": len(pending_approvals),
        "scan_candidates": read_current_scan_candidates(),
        "recent_trades": recent_trades,
        "recent_alerts": recent_alerts,
    }


@app.get("/positions", dependencies=[Depends(require_api_key)])
def positions() -> list[dict[str, Any]]:
    return [map_position(position) for position in read_active_positions()]


@app.get("/overview", dependencies=[Depends(require_api_key)])
def overview() -> dict[str, Any]:
    config = load_config()
    health_data = read_health()
    trades = get_recent_trades(limit=250)
    positions_data = [map_position(position) for position in read_active_positions()]
    starting_equity = number(config.get("risk", {}).get("account_size"), 0)
    equity_curve = equity_from_trades(trades, starting_equity)
    day_pnl = sum(number(trade.get("pnl")) for trade in trades if str(trade.get("time", ""))[:10] == datetime.now().strftime("%Y-%m-%d"))
    wins = sum(1 for trade in trades if number(trade.get("pnl")) > 0)
    closed = sum(1 for trade in trades if number(trade.get("pnl")) != 0)
    equity = equity_curve[-1]["v"]
    return {
        "kpis": {
            "equity": equity,
            "day_pnl": round(day_pnl, 2),
            "day_pnl_pct": round((day_pnl / equity * 100), 2) if equity else 0,
            "open_positions": len(positions_data),
            "win_rate": round((wins / closed * 100), 1) if closed else 0,
            "exposure_pct": 0,
        },
        "equity_curve": equity_curve,
        "recent_trades": trades[-8:],
        "alerts": get_alerts(limit=8) or [
            {
                "id": "health",
                "level": "info" if not health_data.get("last_error") else "warn",
                "message": health_data.get("last_status") or "PulseTrade API connected.",
                "time": health_data.get("updated_at") or iso_now(),
            }
        ],
    }


@app.get("/orders", dependencies=[Depends(require_api_key)])
def orders() -> list[dict[str, Any]]:
    mapped = []
    for index, order in enumerate(read_pending_approvals()):
        mapped.append(
            {
                "id": order.get("id") or f"approval-{index}",
                "time": order.get("created_at") or iso_now(),
                "symbol": order.get("symbol") or "",
                "side": "BUY",
                "qty": int(number(order.get("quantity"), 0)),
                "type": order.get("order_type") or "MKT",
                "limit_price": order.get("limit_price"),
                "status": order.get("status") or "pending",
            }
        )
    return mapped


@app.get("/trades", dependencies=[Depends(require_api_key)])
def trades() -> list[dict[str, Any]]:
    return get_recent_trades(limit=100)


@app.get("/scan-candidates", dependencies=[Depends(require_api_key)])
def scan_candidates() -> dict[str, Any]:
    return {"ok": True, "scan_candidates": read_current_scan_candidates()}


@app.get("/ai-audit", dependencies=[Depends(require_api_key)])
def ai_audit() -> dict[str, Any]:
    health_data = read_health()
    candidates = read_current_scan_candidates()
    rows = candidates.get("candidates", []) if isinstance(candidates, dict) else []
    data_fresh = bool(health_data.get("updated_at"))
    return {
        "score": 90 if data_fresh and not health_data.get("last_error") else 65,
        "model": {
            "name": "PulseTrade scanner",
            "version": "local",
            "trained_at": health_data.get("updated_at") or iso_now(),
            "accuracy": None,
        },
        "checks": [
            {"id": "engine", "name": "Engine heartbeat", "status": "pass" if health_data.get("engine_running") else "warn", "detail": health_data.get("last_status") or "No heartbeat yet.", "weight": 25},
            {"id": "broker", "name": "Broker connection", "status": "pass" if health_data.get("ib_connected") else "warn", "detail": "IBKR connected" if health_data.get("ib_connected") else "IBKR not connected", "weight": 25},
            {"id": "data", "name": "Data freshness", "status": "pass" if data_fresh else "warn", "detail": health_data.get("updated_at") or "No health timestamp.", "weight": 25},
            {"id": "risk", "name": "Risk gate", "status": "pass" if health_data.get("auto_trading") else "warn", "detail": "Auto trading enabled" if health_data.get("auto_trading") else "Auto trading disabled", "weight": 25},
        ],
        "signals": [
            {
                "symbol": row.get("Symbol") or row.get("symbol") or "",
                "action": row.get("Signal") or row.get("signal") or "WATCH",
                "confidence": number(row.get("Confidence"), 0) / 100,
                "rationale": row.get("Reasons") or row.get("reasons") or row.get("Setup Quality") or "",
                "time": candidates.get("generated_at") or iso_now(),
            }
            for row in rows[:10]
        ],
    }


@app.get("/backtest/strategies", dependencies=[Depends(require_api_key)])
def strategies() -> list[dict[str, Any]]:
    return [
        {"id": "pmb", "name": "PMB", "description": "PulseTrade momentum breakout strategy."},
        {"id": "brt", "name": "BRT", "description": "Breakout/retest strategy."},
    ]


@app.get("/market-intel", dependencies=[Depends(require_api_key)])
def market_intel() -> dict[str, Any]:
    alerts = get_alerts(limit=6)
    return {
        "indices": [],
        "regime": {"label": "Live PulseTrade feed", "detail": read_health().get("last_status", ""), "confidence": 0.0},
        "sentiment": {"score": 0, "label": "Neutral"},
        "news": [
            {
                "id": alert["id"],
                "time": alert["time"],
                "headline": alert["message"],
                "source": "PulseTrade",
                "sentiment": 0,
                "symbols": [],
            }
            for alert in alerts
        ],
    }


@app.get("/account", dependencies=[Depends(require_api_key)])
def account() -> dict[str, Any]:
    config = load_config()
    risk = config.get("risk", {}) if isinstance(config.get("risk"), dict) else {}
    mode = str(config.get("account_mode") or "Unknown")
    return {
        "broker": "Interactive Brokers",
        "account_id": config.get("ib", {}).get("account") or "",
        "cash": number(risk.get("account_size"), 0),
        "equity": number(risk.get("account_size"), 0),
        "buying_power": number(risk.get("account_size"), 0),
        "margin_used": 0,
        "positions_value": 0,
        "day_trades_left": risk.get("max_trades_per_day", 0),
        "settings": {
            "mode": mode.lower(),
            "max_position_size": risk.get("max_spend_per_trade", 0),
            "daily_loss_limit": risk.get("max_daily_capital", 0),
            "max_open_positions": risk.get("max_trades_per_day", 0),
            "allow_shorts": True,
        },
    }


@app.get("/approvals", dependencies=[Depends(require_api_key)])
def approvals() -> dict[str, Any]:
    data = read_pending_approvals()
    return {"ok": True, "approvals": data, "count": len(data)}


@app.post("/approvals/{order_id}/reject", dependencies=[Depends(require_api_key)])
def reject_approval(order_id: str) -> dict[str, Any]:
    updated = update_pending_approval(
        order_id,
        status="rejected",
        decision_at=datetime.now(timezone.utc).isoformat(),
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Approval not found")
    return {"ok": True, "approval": updated}


@app.get("/journal", dependencies=[Depends(require_api_key)])
def journal(limit: int = 100) -> dict[str, Any]:
    rows = read_csv_rows(EXPORT_DIR / "trade_log.csv", limit=max(1, min(limit, 500)))
    return {"ok": True, "trades": rows, "count": len(rows)}


@app.get("/logs", dependencies=[Depends(require_api_key)])
def logs(lines: int = 100) -> list[dict[str, Any]]:
    today = datetime.now().strftime("%Y-%m-%d")
    path = LOG_DIR / f"{today}.log"
    try:
        if not path.exists():
            return []
        data = path.read_text(encoding="utf-8").splitlines()
        out = []
        for index, line in enumerate(data[-max(1, min(lines, 500)) :]):
            parts = [part.strip() for part in line.split("|", 2)]
            out.append(
                {
                    "id": f"log-{index}",
                    "ts": parts[0] if len(parts) > 0 else iso_now(),
                    "level": parts[1].lower() if len(parts) > 1 else "info",
                    "source": "engine",
                    "message": parts[2] if len(parts) > 2 else line,
                }
            )
        return out
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/engine/start", dependencies=[Depends(require_api_key)])
def engine_start() -> dict[str, Any]:
    return {"ok": False, "engine": "unchanged", "detail": "Start the local engine from the Mac dashboard or engine.py."}


@app.post("/engine/stop", dependencies=[Depends(require_api_key)])
def engine_stop() -> dict[str, Any]:
    return {"ok": False, "engine": "unchanged", "detail": "Stop the local engine from the Mac dashboard or terminal."}


@app.post("/engine/mode", dependencies=[Depends(require_api_key)])
def engine_mode(req: EngineModeReq) -> dict[str, Any]:
    return {"ok": False, "mode": req.mode, "detail": "Mode changes are intentionally not exposed through the public API."}


@app.post("/orders/place", dependencies=[Depends(require_api_key)])
def place_order(req: PlaceOrderReq) -> dict[str, Any]:
    return {"ok": False, "symbol": req.symbol, "status": "rejected", "detail": "Public API order placement is disabled."}


@app.post("/orders/cancel", dependencies=[Depends(require_api_key)])
def cancel_order(req: CancelOrderReq) -> dict[str, Any]:
    return {"ok": False, "order_id": req.order_id, "status": "unchanged", "detail": "Order cancellation is disabled through the public API."}


@app.post("/positions/close", dependencies=[Depends(require_api_key)])
def close_position(req: ClosePositionReq) -> dict[str, Any]:
    return {"ok": False, "symbol": req.symbol, "status": "unchanged", "detail": "Position closing is disabled through the public API."}


@app.post("/positions/close-all", dependencies=[Depends(require_api_key)])
def close_all_positions() -> dict[str, Any]:
    return {"ok": False, "closed": 0, "detail": "Position closing is disabled through the public API."}


@app.post("/account/settings", dependencies=[Depends(require_api_key)])
def update_account_settings(req: AccountSettingsReq) -> dict[str, Any]:
    return {"ok": False, "settings": req.model_dump(), "detail": "Settings updates are disabled through the public API."}


@app.post("/backtest/run", dependencies=[Depends(require_api_key)])
def run_backtest(req: BacktestReq) -> dict[str, Any]:
    return {"equity_curve": equity_from_trades(get_recent_trades(limit=250), req.initial_capital), "metrics": {}, "trades": get_recent_trades(limit=12)}


@app.post("/scanner/run", dependencies=[Depends(require_api_key)])
def run_scanner(req: ScannerReq) -> dict[str, Any]:
    candidates = read_current_scan_candidates()
    rows = candidates.get("candidates", []) if isinstance(candidates, dict) else []
    results = []
    for row in rows:
        score = number(first_present(row, "Score", "score", default=0))
        price = number(first_present(row, "Price", "price", default=0))
        rvol = number(first_present(row, "RVOL", "rvol", default=0))
        if price and (price < req.min_price or price > req.max_price):
            continue
        if rvol and rvol < req.min_rvol:
            continue
        if score < req.min_score:
            continue
        results.append(
            {
                "symbol": first_present(row, "Symbol", "symbol", default=""),
                "price": price,
                "change_pct": number(first_present(row, "Change %", "change_pct", default=0)),
                "volume": int(number(first_present(row, "Volume", "volume", default=0))),
                "rvol": rvol,
                "score": int(score),
                "setup": first_present(row, "Setup Quality", "setup", default="PulseTrade"),
            }
        )
    return {"results": results}


@app.exception_handler(Exception)
async def global_exc(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"error": "internal_error", "detail": str(exc)})
