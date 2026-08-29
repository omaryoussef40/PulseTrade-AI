from __future__ import annotations

"""Stock-only simulator for the research GAP strategy."""

from dataclasses import dataclass
from datetime import time as dtime
from math import floor
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class GapStockSimulationConfig:
    starting_capital: float = 10_000.0
    risk_per_trade: float = 50.0
    max_capital_per_trade: float = 1_500.0
    max_daily_capital: float = 3_000.0
    max_trades_per_day: int = 2
    stop_buffer_pct: float = 0.25
    min_stop_distance_pct: float = 0.5
    max_stop_distance_pct: float = 6.0
    target_r: float = 2.0
    force_exit_time: dtime = dtime(11, 30)
    slippage_pct: float = 0.10
    commission_per_share: float = 0.005
    minimum_order_commission: float = 1.0
    allow_same_symbol_same_day: bool = False


def _decision(signal: pd.Series, status: str, stage: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "timestamp": signal.get("timestamp"),
        "strategy": "GAP",
        "instrument": "STOCK",
        "symbol": signal.get("symbol"),
        "signal": signal.get("signal"),
        "score": signal.get("score"),
        "grade": signal.get("grade"),
        "status": status,
        "stage": stage,
        "reason": reason,
        **extra,
    }


def _normalize_timestamp(value: Any, reference_index: pd.DatetimeIndex) -> pd.Timestamp | None:
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return None
    timezone = getattr(reference_index, "tz", None)
    if timezone is not None:
        if ts.tzinfo is None:
            ts = ts.tz_localize(timezone)
        else:
            ts = ts.tz_convert(timezone)
    return ts


def _commission(shares: int, config: GapStockSimulationConfig) -> float:
    return round(max(float(config.minimum_order_commission), int(shares) * float(config.commission_per_share)), 2)


def _apply_slippage(price: float, action: str, config: GapStockSimulationConfig) -> float:
    slip = max(float(config.slippage_pct), 0.0) / 100.0
    return float(price) * (1.0 + slip if action.upper() == "BUY" else 1.0 - slip)


def _exit_fill(side: str, bars: pd.DataFrame, stop_price: float, target_price: float, config: GapStockSimulationConfig) -> tuple[pd.Timestamp, float, str]:
    side = str(side).upper()
    last_timestamp = bars.index[-1]
    last_price = float(bars["Close"].iloc[-1])
    last_reason = "Session data ended"

    for timestamp, bar in bars.iterrows():
        open_price = float(bar["Open"])
        high = float(bar["High"])
        low = float(bar["Low"])
        if timestamp.time() >= config.force_exit_time:
            return timestamp, open_price, "Timed exit"

        if side == "LONG":
            if open_price <= stop_price:
                return timestamp, open_price, "Stop loss gap-through"
            if open_price >= target_price:
                return timestamp, target_price, "Profit target"
            stop_hit = low <= stop_price
            target_hit = high >= target_price
        else:
            if open_price >= stop_price:
                return timestamp, open_price, "Stop loss gap-through"
            if open_price <= target_price:
                return timestamp, target_price, "Profit target"
            stop_hit = high >= stop_price
            target_hit = low <= target_price

        if stop_hit:
            reason = "Stop loss (same-bar conservative)" if target_hit else "Stop loss"
            return timestamp, stop_price, reason
        if target_hit:
            return timestamp, target_price, "Profit target"
        last_timestamp = timestamp
        last_price = float(bar["Close"])

    return last_timestamp, last_price, last_reason


def simulate_gap_stock_trades_with_decisions(
    signals: pd.DataFrame,
    data: dict[str, pd.DataFrame],
    config: GapStockSimulationConfig | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    cfg = config or GapStockSimulationConfig()
    if signals is None or signals.empty:
        return pd.DataFrame(), pd.DataFrame()

    work = signals.copy()
    work["timestamp"] = pd.to_datetime(work["timestamp"], errors="coerce")
    work = work.dropna(subset=["timestamp"]).sort_values(["timestamp", "rank_score", "symbol"], ascending=[True, False, True])
    trades: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    cumulative_pnl = 0.0

    for session_date, day_signals in work.groupby(work["timestamp"].dt.date, sort=True):
        day_start_equity = float(cfg.starting_capital) + cumulative_pnl
        remaining_daily_capital = min(float(cfg.max_daily_capital), max(day_start_equity, 0.0))
        day_trade_count = 0
        traded_symbols: set[str] = set()
        day_trades: list[dict[str, Any]] = []

        for _, signal in day_signals.sort_values(["rank_score", "symbol"], ascending=[False, True]).iterrows():
            symbol = str(signal.get("symbol") or "").strip().upper()
            side = str(signal.get("signal") or "").strip().upper()
            if day_trade_count >= int(cfg.max_trades_per_day):
                decisions.append(_decision(signal, "SKIPPED", "DAILY_LIMIT", "Maximum GAP trades reached for the day"))
                continue
            if symbol in traded_symbols and not cfg.allow_same_symbol_same_day:
                decisions.append(_decision(signal, "SKIPPED", "DUPLICATE_SYMBOL", "Symbol already traded today"))
                continue
            if side not in {"LONG", "SHORT"}:
                decisions.append(_decision(signal, "REJECTED", "SIGNAL", "GAP signal must be LONG or SHORT"))
                continue
            bars = data.get(symbol)
            if bars is None or bars.empty:
                decisions.append(_decision(signal, "REJECTED", "MARKET_DATA", "No stock candles available"))
                continue
            timestamp = _normalize_timestamp(signal.get("timestamp"), bars.index)
            if timestamp is None:
                decisions.append(_decision(signal, "REJECTED", "MARKET_DATA", "Invalid entry timestamp"))
                continue
            session_bars = bars[(bars.index.date == session_date) & (bars.index >= timestamp)].copy()
            if session_bars.empty:
                decisions.append(_decision(signal, "REJECTED", "MARKET_DATA", "No candles at or after the 09:45 entry"))
                continue

            raw_entry = float(signal.get("entry_price") or session_bars["Open"].iloc[0])
            or_high = float(signal.get("opening_range_high") or 0.0)
            or_low = float(signal.get("opening_range_low") or 0.0)
            if raw_entry <= 0 or or_high <= 0 or or_low <= 0:
                decisions.append(_decision(signal, "REJECTED", "STRUCTURE", "Missing entry or opening-range prices"))
                continue

            stop_buffer = max(float(cfg.stop_buffer_pct), 0.0) / 100.0
            stop_price = or_low * (1.0 - stop_buffer) if side == "LONG" else or_high * (1.0 + stop_buffer)
            risk_per_share = raw_entry - stop_price if side == "LONG" else stop_price - raw_entry
            risk_distance_pct = (risk_per_share / raw_entry * 100.0) if raw_entry else 0.0
            if risk_per_share <= 0:
                decisions.append(_decision(signal, "REJECTED", "STRUCTURE", "Opening-range stop is on the wrong side of entry"))
                continue
            if risk_distance_pct < float(cfg.min_stop_distance_pct):
                decisions.append(_decision(signal, "REJECTED", "STRUCTURE", f"Stop distance {risk_distance_pct:.2f}% is too tight"))
                continue
            if risk_distance_pct > float(cfg.max_stop_distance_pct):
                decisions.append(_decision(signal, "REJECTED", "STRUCTURE", f"Stop distance {risk_distance_pct:.2f}% exceeds the limit"))
                continue

            capital_limit = min(float(cfg.max_capital_per_trade), remaining_daily_capital)
            shares_by_risk = floor(max(float(cfg.risk_per_trade), 0.0) / risk_per_share)
            shares_by_capital = floor(max(capital_limit, 0.0) / raw_entry)
            shares = int(min(shares_by_risk, shares_by_capital))
            if shares < 1:
                decisions.append(_decision(
                    signal,
                    "REJECTED",
                    "SIZING",
                    "Risk or capital allocation cannot buy one share",
                    risk_per_share=round(risk_per_share, 4),
                    capital_limit=round(capital_limit, 2),
                ))
                continue

            target_price = raw_entry + float(cfg.target_r) * risk_per_share if side == "LONG" else raw_entry - float(cfg.target_r) * risk_per_share
            if target_price <= 0:
                decisions.append(_decision(signal, "REJECTED", "STRUCTURE", "Calculated target price is invalid"))
                continue

            exit_time, raw_exit, exit_reason = _exit_fill(side, session_bars, stop_price, target_price, cfg)
            entry_action = "BUY" if side == "LONG" else "SELL"
            exit_action = "SELL" if side == "LONG" else "BUY"
            entry_fill = _apply_slippage(raw_entry, entry_action, cfg)
            exit_fill = _apply_slippage(raw_exit, exit_action, cfg)
            entry_commission = _commission(shares, cfg)
            exit_commission = _commission(shares, cfg)
            commissions = entry_commission + exit_commission
            gross_pnl = (exit_fill - entry_fill) * shares if side == "LONG" else (entry_fill - exit_fill) * shares
            realized_pnl = round(gross_pnl - commissions, 2)
            initial_risk = risk_per_share * shares
            r_multiple = round(realized_pnl / initial_risk, 3) if initial_risk > 0 else 0.0
            notional = round(raw_entry * shares, 2)
            hold_minutes = max(0.0, (pd.Timestamp(exit_time) - timestamp).total_seconds() / 60.0)

            trade = {
                "entry_time": timestamp,
                "exit_time": exit_time,
                "date": str(session_date),
                "strategy": "GAP",
                "strategy_name": "Gap",
                "instrument": "STOCK",
                "symbol": symbol,
                "signal": side,
                "side": side,
                "score": signal.get("score"),
                "grade": signal.get("grade"),
                "gap_pct": signal.get("gap_pct"),
                "premarket_volume": signal.get("premarket_volume"),
                "premarket_rvol": signal.get("premarket_rvol"),
                "quantity": shares,
                "entry_price": round(entry_fill, 4),
                "exit_price": round(exit_fill, 4),
                "entry_underlying": round(entry_fill, 4),
                "exit_underlying": round(exit_fill, 4),
                "stop_price": round(stop_price, 4),
                "target_price": round(target_price, 4),
                "risk_per_share": round(risk_per_share, 4),
                "initial_risk": round(initial_risk, 2),
                "r_multiple": r_multiple,
                "estimated_cost": notional,
                "notional": notional,
                "commissions": round(commissions, 2),
                "realized_pnl": realized_pnl,
                "return_pct": round(realized_pnl / notional * 100.0, 2) if notional else 0.0,
                "hold_minutes": round(hold_minutes, 1),
                "exit_reason": exit_reason,
                "catalyst_status": signal.get("catalyst_status"),
                "reasons": signal.get("reasons"),
            }
            day_trades.append(trade)
            decisions.append(_decision(
                signal,
                "TRADED",
                "SIMULATION",
                f"Opened stock {side.lower()} and exited: {exit_reason}",
                quantity=shares,
                entry_price=round(entry_fill, 4),
                stop_price=round(stop_price, 4),
                target_price=round(target_price, 4),
                estimated_cost=notional,
                initial_risk=round(initial_risk, 2),
                realized_pnl=realized_pnl,
                r_multiple=r_multiple,
            ))
            day_trade_count += 1
            traded_symbols.add(symbol)
            remaining_daily_capital = max(0.0, remaining_daily_capital - notional)

        cumulative_pnl += sum(float(trade["realized_pnl"]) for trade in day_trades)
        day_end_equity = round(float(cfg.starting_capital) + cumulative_pnl, 2)
        for trade in day_trades:
            trade["account_equity"] = day_end_equity
        trades.extend(day_trades)

    trades_df = pd.DataFrame(trades)
    if not trades_df.empty:
        trades_df = trades_df.sort_values(["exit_time", "symbol"]).reset_index(drop=True)
        trades_df.insert(0, "trade_no", range(1, len(trades_df) + 1))
        trades_df["cumulative_pnl"] = pd.to_numeric(trades_df["realized_pnl"], errors="coerce").fillna(0.0).cumsum()
        trades_df["equity"] = float(cfg.starting_capital) + trades_df["cumulative_pnl"]
    decisions_df = pd.DataFrame(decisions)
    if not decisions_df.empty:
        decisions_df.insert(0, "decision_no", range(1, len(decisions_df) + 1))
    return trades_df, decisions_df
