from __future__ import annotations

"""Historical long-straddle research with combined two-leg accounting."""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .orb_results import EASTERN, _normalize_bars


@dataclass(frozen=True)
class StraddleResultsConfig:
    period: str = "30d"
    option_dte: int = 7
    orb_minutes: int = 15
    confirmation_minutes: int = 15
    winner_stop_loss_pct: float = 30.0
    winner_target_pct: float = 100.0
    trailing_trigger_pct: float = 25.0
    trailing_stop_pct: float = 10.0
    slippage_pct: float = 2.0
    commission_per_contract: float = 0.65
    force_exit_hour: int = 15
    force_exit_minute: int = 55


def scan_straddle_sessions(
    data: dict[str, pd.DataFrame],
    config: StraddleResultsConfig | None = None,
) -> pd.DataFrame:
    settings = config or StraddleResultsConfig()
    orb_bars_required = max(1, int(settings.orb_minutes) // 5)
    confirmation_bars_required = max(1, int(settings.confirmation_minutes) // 5)
    rows: list[dict[str, Any]] = []

    for raw_symbol, raw_frame in data.items():
        symbol = str(raw_symbol).strip().upper()
        if not symbol:
            continue
        frame = _normalize_bars(raw_frame)
        for session_date, session in frame.groupby(frame.index.date):
            session = session.between_time("09:30", "16:00", inclusive="left").sort_index()
            if session.empty:
                continue
            session_start = session.index[0].normalize() + pd.Timedelta(hours=9, minutes=30)
            orb_end = session_start + pd.Timedelta(minutes=int(settings.orb_minutes))
            confirmation_time = orb_end + pd.Timedelta(minutes=int(settings.confirmation_minutes))
            opening = session[(session.index >= session_start) & (session.index < orb_end)]
            confirmation = session[(session.index >= orb_end) & (session.index < confirmation_time)]
            if len(opening) < orb_bars_required or len(confirmation) < confirmation_bars_required:
                continue

            orb_high = float(opening["High"].max())
            orb_low = float(opening["Low"].min())
            confirmation_close = float(confirmation.iloc[-1]["Close"])
            direction = "CALL" if confirmation_close > orb_high else "PUT" if confirmation_close < orb_low else ""
            rows.append({
                "session_date": session_date.isoformat(),
                "symbol": symbol,
                "entry_time": session_start,
                "confirmation_time": confirmation_time,
                "entry_underlying": round(float(session.iloc[0]["Open"]), 4),
                "orb_high": round(orb_high, 4),
                "orb_low": round(orb_low, 4),
                "confirmation_close": round(confirmation_close, 4),
                "broke_orb": bool(direction),
                "direction": direction,
                "option_status": "PENDING",
            })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["session_date", "symbol"]).reset_index(drop=True)


def _normalize_option_bars(frame: pd.DataFrame | None, timezone) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    bars = frame.copy()
    index = pd.DatetimeIndex(pd.to_datetime(bars.index, errors="coerce"))
    valid = ~pd.isna(index)
    bars = bars.loc[valid].copy()
    index = index[valid]
    if index.tz is None:
        index = index.tz_localize(timezone or EASTERN)
    else:
        index = index.tz_convert(timezone or EASTERN)
    bars.index = index
    for column in ["Open", "High", "Low", "Close"]:
        if column in bars.columns:
            bars[column] = pd.to_numeric(bars[column], errors="coerce")
    return bars.sort_index()


def _open_at_or_after(bars: pd.DataFrame, timestamp: pd.Timestamp) -> tuple[pd.Timestamp, float]:
    available = bars[bars.index >= timestamp]
    if available.empty:
        raise RuntimeError(f"No option opening price at or after {timestamp.strftime('%H:%M')}")
    for index, row in available.iterrows():
        for column in ["Open", "Close"]:
            value = row.get(column)
            if pd.notna(value) and float(value) > 0:
                return pd.Timestamp(index), float(value)
    raise RuntimeError("Option bars contain no positive opening price")


def _winner_exit(
    bars: pd.DataFrame,
    confirmation_time: pd.Timestamp,
    entry_paid: float,
    settings: StraddleResultsConfig,
) -> tuple[pd.Timestamp, float, str, float]:
    force_exit = confirmation_time.normalize() + pd.Timedelta(
        hours=int(settings.force_exit_hour), minutes=int(settings.force_exit_minute)
    )
    observations: list[tuple[pd.Timestamp, float]] = []
    confirmation_index, confirmation_open = _open_at_or_after(bars, confirmation_time)
    observations.append((confirmation_index, confirmation_open))
    future = bars[(bars.index >= confirmation_index) & (bars.index < force_exit)]
    for index, row in future.iterrows():
        close = row.get("Close")
        if pd.notna(close) and float(close) > 0:
            observations.append((pd.Timestamp(index) + pd.Timedelta(minutes=5), float(close)))
    if not observations:
        raise RuntimeError("No winner prices after ORB confirmation")

    initial_stop = entry_paid * (1.0 - float(settings.winner_stop_loss_pct) / 100.0)
    target = entry_paid * (1.0 + float(settings.winner_target_pct) / 100.0)
    trailing_trigger = entry_paid * (1.0 + float(settings.trailing_trigger_pct) / 100.0)
    peak = max(entry_paid, observations[0][1])
    trailing_active = peak >= trailing_trigger
    trailing_level = peak * (1.0 - float(settings.trailing_stop_pct) / 100.0) if trailing_active else 0.0

    for timestamp, mark in observations:
        peak = max(peak, mark)
        if peak >= trailing_trigger:
            trailing_active = True
            trailing_level = max(trailing_level, peak * (1.0 - float(settings.trailing_stop_pct) / 100.0))
        if mark <= initial_stop:
            return timestamp, initial_stop, "Winner stop loss", peak
        if trailing_active and mark <= trailing_level:
            return timestamp, trailing_level, "Winner trailing stop", peak
        if mark >= target:
            return timestamp, target, "Winner target", peak

    final_rows = bars[bars.index <= force_exit]
    if final_rows.empty:
        raise RuntimeError("No option price available for the end-of-day exit")
    final_row = final_rows.iloc[-1]
    final_mid = float(final_row.get("Close") or final_row.get("Open"))
    return force_exit, final_mid, "End-of-day exit", max(peak, final_mid)


def simulate_straddles(
    sessions: pd.DataFrame,
    option_pair_provider: Callable[..., tuple[dict[str, Any], pd.DataFrame, dict[str, Any], pd.DataFrame]],
    config: StraddleResultsConfig | None = None,
    progress_callback: Callable[[int, int, dict[str, Any]], None] | None = None,
) -> pd.DataFrame:
    settings = config or StraddleResultsConfig()
    if sessions is None or sessions.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    total = len(sessions)
    slip = float(settings.slippage_pct) / 100.0
    commission = float(settings.commission_per_contract)

    for number, (_, session) in enumerate(sessions.iterrows(), start=1):
        base = session.to_dict()
        if progress_callback:
            progress_callback(number, total, base)
        try:
            call_info, raw_call_bars, put_info, raw_put_bars = option_pair_provider(
                symbol=str(session["symbol"]),
                underlying_price=float(session["entry_underlying"]),
                option_dte=int(settings.option_dte),
                reference_time=pd.Timestamp(session["entry_time"]),
            )
            call_bars = _normalize_option_bars(raw_call_bars, pd.Timestamp(session["entry_time"]).tz)
            put_bars = _normalize_option_bars(raw_put_bars, pd.Timestamp(session["entry_time"]).tz)
            if call_bars.empty or put_bars.empty:
                raise RuntimeError("CALL or PUT historical bars are empty")
            call_strike = float(call_info.get("strike"))
            put_strike = float(put_info.get("strike"))
            call_expiry = str(call_info.get("expiry") or "")
            put_expiry = str(put_info.get("expiry") or "")
            if abs(call_strike - put_strike) > 1e-6 or call_expiry != put_expiry:
                raise RuntimeError(
                    f"IBKR did not return a matched pair: CALL {call_expiry} {call_strike:g}, "
                    f"PUT {put_expiry} {put_strike:g}"
                )

            entry_time = pd.Timestamp(session["entry_time"])
            confirmation_time = pd.Timestamp(session["confirmation_time"])
            _, call_entry_mid = _open_at_or_after(call_bars, entry_time)
            _, put_entry_mid = _open_at_or_after(put_bars, entry_time)
            call_entry = call_entry_mid * (1.0 + slip)
            put_entry = put_entry_mid * (1.0 + slip)
            entry_debit = call_entry + put_entry
            entry_cost = entry_debit * 100.0 + 2.0 * commission

            direction = str(session.get("direction") or "")
            if direction:
                winner_bars = call_bars if direction == "CALL" else put_bars
                loser_bars = put_bars if direction == "CALL" else call_bars
                _, loser_mid = _open_at_or_after(loser_bars, confirmation_time)
                loser_exit = loser_mid * (1.0 - slip)
                winner_entry = call_entry if direction == "CALL" else put_entry
                winner_exit_time, winner_exit_mid, exit_reason, winner_peak = _winner_exit(
                    winner_bars, confirmation_time, winner_entry, settings
                )
                winner_exit = winner_exit_mid * (1.0 - slip)
                exit_credit = (loser_exit + winner_exit) * 100.0 - 2.0 * commission
                max_exit_credit = (loser_exit + winner_peak * (1.0 - slip)) * 100.0 - 2.0 * commission
                call_exit = winner_exit if direction == "CALL" else loser_exit
                put_exit = winner_exit if direction == "PUT" else loser_exit
                loser_return = ((loser_exit / (put_entry if direction == "CALL" else call_entry)) - 1.0) * 100.0
                winner_return = ((winner_exit / winner_entry) - 1.0) * 100.0
            else:
                _, call_exit_mid = _open_at_or_after(call_bars, confirmation_time)
                _, put_exit_mid = _open_at_or_after(put_bars, confirmation_time)
                call_exit = call_exit_mid * (1.0 - slip)
                put_exit = put_exit_mid * (1.0 - slip)
                exit_credit = (call_exit + put_exit) * 100.0 - 2.0 * commission
                max_exit_credit = exit_credit
                winner_exit_time = confirmation_time
                exit_reason = "No ORB break; both legs closed"
                loser_return = None
                winner_return = None

            realized_pnl = exit_credit - entry_cost
            rows.append({
                **base,
                "option_status": "AVAILABLE",
                "option_expiry": call_expiry,
                "option_strike": call_strike,
                "call_local_symbol": call_info.get("localSymbol"),
                "put_local_symbol": put_info.get("localSymbol"),
                "call_entry": round(call_entry, 4),
                "put_entry": round(put_entry, 4),
                "combined_entry_debit": round(entry_debit, 4),
                "entry_cost": round(entry_cost, 2),
                "call_exit": round(call_exit, 4),
                "put_exit": round(put_exit, 4),
                "exit_time": winner_exit_time,
                "exit_reason": exit_reason,
                "winner_return_pct": round(winner_return, 2) if winner_return is not None else None,
                "loser_return_pct": round(loser_return, 2) if loser_return is not None else None,
                "realized_pnl": round(realized_pnl, 2),
                "combined_return_pct": round(realized_pnl / entry_cost * 100.0, 2) if entry_cost else 0.0,
                "max_combined_return_pct": round((max_exit_credit - entry_cost) / entry_cost * 100.0, 2) if entry_cost else 0.0,
                "option_error": None,
            })
        except Exception as exc:
            rows.append({
                **base,
                "option_status": "UNAVAILABLE",
                "option_error": str(exc).strip() or repr(exc),
            })
    return pd.DataFrame(rows)


def summarize_straddles(results: pd.DataFrame) -> pd.DataFrame:
    if results is None or results.empty:
        return pd.DataFrame()

    def summarize(symbol: str, group: pd.DataFrame) -> dict[str, Any]:
        trades = group[group["option_status"].astype(str) == "AVAILABLE"]
        pnl = pd.to_numeric(trades.get("realized_pnl"), errors="coerce").fillna(0.0)
        returns = pd.to_numeric(trades.get("combined_return_pct"), errors="coerce")
        gains = pnl[pnl > 0].sum()
        losses = abs(pnl[pnl < 0].sum())
        return {
            "Ticker": symbol,
            "Sessions": len(group),
            "Completed Pairs": len(trades),
            "Option Coverage %": round(len(trades) / len(group) * 100.0, 1) if len(group) else 0.0,
            "ORB Breaks": int(trades["broke_orb"].fillna(False).sum()) if len(trades) else 0,
            "Win Rate %": round((pnl > 0).mean() * 100.0, 1) if len(trades) else 0.0,
            "Net P/L": round(float(pnl.sum()), 2),
            "Average Return %": round(float(returns.mean()), 2) if len(trades) else 0.0,
            "Median Return %": round(float(returns.median()), 2) if len(trades) else 0.0,
            "Profit Factor": round(float(gains / losses), 2) if losses > 0 else ("inf" if gains > 0 else 0.0),
        }

    ticker_rows = [summarize(symbol, group) for symbol, group in results.groupby("symbol", sort=True)]
    return pd.DataFrame([summarize("ALL", results), *ticker_rows])


def save_straddle_results(
    results: pd.DataFrame,
    summary: pd.DataFrame,
    meta: dict[str, Any],
    export_dir: str | Path,
) -> None:
    directory = Path(export_dir)
    directory.mkdir(parents=True, exist_ok=True)
    results.to_csv(directory / "straddle_results_details.csv", index=False)
    summary.to_csv(directory / "straddle_results_summary.csv", index=False)
    with (directory / "straddle_results_meta.json").open("w", encoding="utf-8") as file:
        json.dump(meta, file, indent=2, default=str)
