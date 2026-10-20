from __future__ import annotations

"""Historical long-straddle research with combined two-leg accounting."""

from dataclasses import dataclass
import json
import math
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
    entry_hour: int = 9
    entry_minute: int = 30
    breakout_buffer_pct: float = 0.0
    require_vwap: bool = False
    require_volume: bool = False
    min_confirmation_volume_ratio: float = 1.0
    allowed_direction: str = "Both"
    no_break_action: str = "Close both"
    winner_stop_enabled: bool = True
    winner_target_enabled: bool = True
    trailing_enabled: bool = True
    breakeven_enabled: bool = False
    breakeven_trigger_pct: float = 20.0
    contracts_per_leg: int = 1

    def __post_init__(self):
        for name in ('orb_minutes','confirmation_minutes','entry_hour','entry_minute','force_exit_hour','force_exit_minute','option_dte','contracts_per_leg'):
            value=getattr(self,name)
            if not isinstance(value,(int,float)) or not math.isfinite(value) or int(value)!=value:
                raise ValueError(f'{name} must be a whole number.')
        if not 5<=self.orb_minutes<=90 or self.orb_minutes%5 or not 5<=self.confirmation_minutes<=60 or self.confirmation_minutes%5:
            raise ValueError('ORB and confirmation windows must be multiples of five minutes.')
        if not 0<=self.entry_hour<=23 or not 0<=self.force_exit_hour<=23 or not 0<=self.entry_minute<=59 or not 0<=self.force_exit_minute<=59:
            raise ValueError('Invalid entry or exit time.')
        confirmation=570+self.orb_minutes+self.confirmation_minutes
        if not 570<=self.entry_hour*60+self.entry_minute<confirmation or self.entry_minute%5:
            raise ValueError('Entry must be on a five-minute boundary from 09:30 and before confirmation.')
        if not confirmation<self.force_exit_hour*60+self.force_exit_minute<=960 or self.force_exit_minute%5:
            raise ValueError('Timed exit must be on a five-minute boundary after confirmation and by 16:00.')
        if not 0<=self.option_dte<=45 or not 1<=self.contracts_per_leg<=1000:
            raise ValueError('Invalid option DTE or contract quantity.')
        for name in ('winner_stop_loss_pct','winner_target_pct','trailing_trigger_pct','trailing_stop_pct','breakeven_trigger_pct','min_confirmation_volume_ratio'):
            value=getattr(self,name)
            if not math.isfinite(value) or value<=0:raise ValueError(f'Invalid {name}.')
        if self.winner_stop_loss_pct>100 or self.trailing_stop_pct>100:
            raise ValueError('Stop percentages cannot exceed 100%.')
        for name in ('slippage_pct','commission_per_contract','breakout_buffer_pct'):
            value=getattr(self,name)
            if not math.isfinite(value) or value<0:raise ValueError(f'Invalid {name}.')
        if self.slippage_pct>=100 or self.allowed_direction not in ('Both','CALL','PUT') or self.no_break_action not in ('Close both','Hold both to timed exit'):
            raise ValueError('Invalid slippage, direction or no-break rule.')


def scan_straddle_sessions(
    data: dict[str, pd.DataFrame],
    config: StraddleResultsConfig | None = None,
) -> pd.DataFrame:
    settings = config or StraddleResultsConfig()
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
            expected_opening=pd.date_range(session_start,orb_end-pd.Timedelta(minutes=5),freq='5min')
            expected_confirmation=pd.date_range(orb_end,confirmation_time-pd.Timedelta(minutes=5),freq='5min')
            entry_time=session_start.normalize()+pd.Timedelta(hours=settings.entry_hour,minutes=settings.entry_minute)
            if not opening.index.equals(expected_opening) or not confirmation.index.equals(expected_confirmation) or entry_time not in session.index:
                continue

            orb_high = float(opening["High"].max())
            orb_low = float(opening["Low"].min())
            confirmation_close = float(confirmation.iloc[-1]["Close"])
            buffer=float(settings.breakout_buffer_pct)/100
            direction = "CALL" if confirmation_close > orb_high*(1+buffer) else "PUT" if confirmation_close < orb_low*(1-buffer) else ""
            decision_reason='ORB breakout' if direction else 'No ORB break'
            completed=session.loc[session.index<confirmation_time]
            volume=completed['Volume']
            vwap=float((((completed.High+completed.Low+completed.Close)/3)*volume).sum()/volume.sum()) if volume.notna().all() and (volume>=0).all() and volume.sum()>0 else None
            average_opening_volume=float(opening.Volume.mean())
            confirmation_volume_ratio=float(confirmation.Volume.mean())/average_opening_volume if average_opening_volume>0 and opening.Volume.notna().all() and confirmation.Volume.notna().all() and (opening.Volume>=0).all() and (confirmation.Volume>=0).all() else None
            if direction and settings.allowed_direction not in ('Both',direction):
                direction='';decision_reason='Breakout direction excluded'
            if direction and settings.require_vwap and (vwap is None or not (confirmation_close>vwap if direction=='CALL' else confirmation_close<vwap)):
                direction='';decision_reason='VWAP filter not met'
            if direction and settings.require_volume and (confirmation_volume_ratio is None or confirmation_volume_ratio<settings.min_confirmation_volume_ratio):
                direction='';decision_reason='Confirmation volume filter not met'
            rows.append({
                "session_date": session_date.isoformat(),
                "symbol": symbol,
                "entry_time": entry_time,
                "confirmation_time": confirmation_time,
                "entry_underlying": round(float(session.loc[entry_time,"Open"]), 4),
                "orb_high": round(orb_high, 4),
                "orb_low": round(orb_low, 4),
                "confirmation_close": round(confirmation_close, 4),
                "broke_orb": bool(direction),
                "direction": direction,
                "decision_reason": decision_reason,
                "confirmation_vwap": vwap,
                "confirmation_volume_ratio": confirmation_volume_ratio,
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
    if confirmation_index!=confirmation_time:
        raise RuntimeError('Winner option price at confirmation is unavailable')
    observations.append((confirmation_index, confirmation_open))
    future = bars[(bars.index >= confirmation_index) & (bars.index < force_exit)]
    for index, row in future.iterrows():
        close = row.get("Close")
        if pd.notna(close) and float(close) > 0 and pd.Timestamp(index)+pd.Timedelta(minutes=5)<=force_exit:
            observations.append((pd.Timestamp(index) + pd.Timedelta(minutes=5), float(close)))
    if not observations:
        raise RuntimeError("No winner prices after ORB confirmation")

    initial_stop = entry_paid * (1.0 - float(settings.winner_stop_loss_pct) / 100.0)
    target = entry_paid * (1.0 + float(settings.winner_target_pct) / 100.0)
    trailing_trigger = entry_paid * (1.0 + float(settings.trailing_trigger_pct) / 100.0)
    peak = max(entry_paid, observations[0][1])
    trailing_active = settings.trailing_enabled and peak >= trailing_trigger
    trailing_level = peak * (1.0 - float(settings.trailing_stop_pct) / 100.0) if trailing_active else 0.0

    for timestamp, mark in observations:
        peak = max(peak, mark)
        if settings.trailing_enabled and peak >= trailing_trigger:
            trailing_active = True
            trailing_level = max(trailing_level, peak * (1.0 - float(settings.trailing_stop_pct) / 100.0))
        breakeven_active=settings.breakeven_enabled and peak>=entry_paid*(1+settings.breakeven_trigger_pct/100)
        stop=entry_paid if breakeven_active else initial_stop
        if (settings.winner_stop_enabled or breakeven_active) and mark<=stop:
            return timestamp, mark, "Winner breakeven stop" if breakeven_active else "Winner stop loss", peak
        if trailing_active and mark <= trailing_level:
            return timestamp, mark, "Winner trailing stop", peak
        if settings.winner_target_enabled and mark >= target:
            return timestamp, target, "Winner target", peak

    final_mid=_timed_exit_price(bars,force_exit)
    return force_exit, final_mid, "End-of-day exit", max(peak, final_mid)


def _timed_exit_price(bars,cutoff):
    """Use the cutoff bar's open, never its still-future closing price."""
    if cutoff in bars.index:
        value=bars.loc[cutoff].get('Open')
        if pd.notna(value) and float(value)>0:return float(value)
    completed=bars.loc[(bars.index.normalize()==cutoff.normalize()) & (bars.index+pd.Timedelta(minutes=5)<=cutoff)]
    valid=completed.loc[pd.to_numeric(completed.Close,errors='coerce')>0]
    if valid.empty:raise RuntimeError('No completed option price available for the timed exit')
    return float(valid.iloc[-1].Close)


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
            call_entry_time, call_entry_mid = _open_at_or_after(call_bars, entry_time)
            put_entry_time, put_entry_mid = _open_at_or_after(put_bars, entry_time)
            if call_entry_time!=entry_time or put_entry_time!=entry_time:
                raise RuntimeError('Matched option prices at the configured entry time are unavailable')
            call_entry = call_entry_mid * (1.0 + slip)
            put_entry = put_entry_mid * (1.0 + slip)
            entry_debit = call_entry + put_entry
            quantity=int(settings.contracts_per_leg)
            entry_cost = quantity*(entry_debit * 100.0 + 2.0 * commission)

            direction = str(session.get("direction") or "")
            if direction:
                winner_bars = call_bars if direction == "CALL" else put_bars
                loser_bars = put_bars if direction == "CALL" else call_bars
                loser_time, loser_mid = _open_at_or_after(loser_bars, confirmation_time)
                if loser_time!=confirmation_time:
                    raise RuntimeError('Opposing-leg price at confirmation is unavailable')
                loser_exit = loser_mid * (1.0 - slip)
                winner_entry = call_entry if direction == "CALL" else put_entry
                winner_exit_time, winner_exit_mid, exit_reason, winner_peak = _winner_exit(
                    winner_bars, confirmation_time, winner_entry, settings
                )
                winner_exit = winner_exit_mid * (1.0 - slip)
                exit_credit = quantity*((loser_exit + winner_exit) * 100.0 - 2.0 * commission)
                max_exit_credit = quantity*((loser_exit + winner_peak * (1.0 - slip)) * 100.0 - 2.0 * commission)
                call_exit = winner_exit if direction == "CALL" else loser_exit
                put_exit = winner_exit if direction == "PUT" else loser_exit
                loser_return = ((loser_exit / (put_entry if direction == "CALL" else call_entry)) - 1.0) * 100.0
                winner_return = ((winner_exit / winner_entry) - 1.0) * 100.0
            else:
                if settings.no_break_action=='Hold both to timed exit':
                    pair_exit_time=confirmation_time.normalize()+pd.Timedelta(hours=settings.force_exit_hour,minutes=settings.force_exit_minute)
                    call_exit_mid=_timed_exit_price(call_bars,pair_exit_time)
                    put_exit_mid=_timed_exit_price(put_bars,pair_exit_time)
                else:
                    pair_exit_time=confirmation_time
                    call_time, call_exit_mid = _open_at_or_after(call_bars, confirmation_time)
                    put_time, put_exit_mid = _open_at_or_after(put_bars, confirmation_time)
                    if call_time!=confirmation_time or put_time!=confirmation_time:
                        raise RuntimeError('Matched option prices at confirmation are unavailable')
                call_exit = call_exit_mid * (1.0 - slip)
                put_exit = put_exit_mid * (1.0 - slip)
                exit_credit = quantity*((call_exit + put_exit) * 100.0 - 2.0 * commission)
                max_exit_credit = exit_credit
                winner_exit_time = pair_exit_time
                exit_reason = "No ORB break; both legs closed" if settings.no_break_action=="Close both" else "No ORB break; both legs held to timed exit"
                loser_return = None
                winner_return = None

            realized_pnl = exit_credit - entry_cost
            rows.append({
                **base,
                "option_status": "AVAILABLE",
                "contracts_per_leg": quantity,
                "actual_entry_time": call_entry_time,
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
