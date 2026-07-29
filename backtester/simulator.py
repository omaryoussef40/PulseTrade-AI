from __future__ import annotations

"""Broker-style option simulator for Yahoo replay signals.

Yahoo/yfinance does not provide reliable historical option chains, so this module
still uses an approximate option-pricing model. The execution model, however, is
broker-style: it tracks cash, buying power, daily capital, commissions, position
size, and every signal decision.

No signal is silently ignored. Every signal becomes TRADED, SKIPPED, or REJECTED
with a reason and sizing/account context.
"""

from dataclasses import dataclass
from datetime import time as dtime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class OptionSimulationConfig:
    starting_capital: float = 1000.0
    max_trades_per_day: int = 2
    # Position sizing. The Strategy Lab can compound by sizing each trade as
    # a percentage of current account equity. Fixed-dollar fields remain for
    # backward compatibility with older saved settings.
    sizing_method: str = "percent_equity"  # percent_equity or fixed_dollar
    position_allocation_pct: float = 20.0
    max_daily_exposure_pct: float = 40.0
    max_spend_per_trade: float = 250.0
    max_daily_capital: float = 500.0
    recycle_capital_after_exit: bool = False
    reserve_capital_for_remaining_trades: bool = True
    # 0 = no fixed contract cap; size by budget/buying power.
    max_contracts: int = 0
    option_dte: int = 7
    option_bars_provider: Callable[..., tuple[dict[str, Any], pd.DataFrame]] | None = None
    target_delta: float = 0.50
    premium_pct: float = 0.0025
    min_premium: float = 0.35
    max_premium: float = 25.0
    stop_loss_pct: float = 20.0
    take_profit_pct: float = 30.0
    breakeven_trigger_pct: float = 15.0
    trailing_trigger_pct: float = 25.0
    trailing_stop_pct: float = 10.0
    entry_cutoff_time: dtime = dtime(11, 0)
    force_exit_enabled: bool = True
    force_exit_time: dtime = dtime(15, 55)
    max_consecutive_losses: int = 2
    max_daily_drawdown_pct: float = 5.0
    commission_per_contract: float = 0.65
    slippage_pct: float = 2.0
    theta_decay_pct_per_day: float = 5.0
    allow_same_symbol_same_day: bool = False
    timezone: ZoneInfo = EASTERN


@dataclass
class SimulatedAccount:
    starting_balance: float
    cash: float | None = None
    realized_pnl: float = 0.0
    commissions_paid: float = 0.0
    peak_equity: float | None = None
    max_drawdown: float = 0.0

    def __post_init__(self):
        if self.cash is None:
            self.cash = float(self.starting_balance)
        if self.peak_equity is None:
            self.peak_equity = float(self.cash)

    @property
    def equity(self) -> float:
        return float(self.cash)

    @property
    def buying_power(self) -> float:
        # Long options are fully paid in cash in this simulator.
        return max(0.0, float(self.cash))

    def reserve_entry_cost(self, amount: float) -> None:
        if amount > self.cash + 1e-9:
            raise ValueError("Insufficient buying power")
        self.cash = round(float(self.cash) - float(amount), 2)
        self._mark_equity()

    def close_position(self, exit_credit: float, realized_pnl: float, commissions: float) -> None:
        self.cash = round(float(self.cash) + float(exit_credit), 2)
        self.realized_pnl = round(float(self.realized_pnl) + float(realized_pnl), 2)
        self.commissions_paid = round(float(self.commissions_paid) + float(commissions), 2)
        self._mark_equity()

    def _mark_equity(self) -> None:
        self.peak_equity = max(float(self.peak_equity), self.equity)
        dd = self.equity - float(self.peak_equity)
        self.max_drawdown = min(float(self.max_drawdown), dd)


@dataclass(frozen=True)
class PositionSize:
    quantity: int
    entry_cost: float
    option_notional: float
    entry_commission: float
    buying_power_before: float
    buying_power_after_entry: float
    allowed_budget: float
    contract_cost_with_commission: float
    reason: str = ""


# ---------- Normalization helpers ----------

def _normalize_timestamp(value, timezone: ZoneInfo = EASTERN) -> pd.Timestamp | None:
    try:
        ts = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts):
            return None
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.tz_localize(timezone)
        else:
            ts = ts.tz_convert(timezone)
        return ts
    except Exception:
        return None


def _clean_data(data: dict[str, pd.DataFrame], timezone: ZoneInfo = EASTERN) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for symbol, df in (data or {}).items():
        if df is None or df.empty:
            continue
        sym = str(symbol).strip().upper()
        tmp = df.copy()
        if isinstance(tmp.columns, pd.MultiIndex):
            tmp.columns = tmp.columns.get_level_values(0)
        tmp = tmp.rename(columns={str(c): str(c).strip().title() for c in tmp.columns})
        cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in tmp.columns]
        if "Close" not in cols:
            continue
        tmp = tmp[cols].copy()
        for col in cols:
            tmp[col] = pd.to_numeric(tmp[col], errors="coerce")
        tmp = tmp.dropna(subset=["Close"])
        idx = pd.to_datetime(tmp.index, errors="coerce")
        valid = ~pd.isna(idx)
        tmp = tmp.loc[valid].copy()
        idx = idx[valid]
        if getattr(idx, "tz", None) is None:
            idx = idx.tz_localize(timezone)
        else:
            idx = idx.tz_convert(timezone)
        tmp.index = idx
        tmp = tmp[~tmp.index.duplicated(keep="last")].sort_index()
        if not tmp.empty:
            out[sym] = tmp
    return out


# ---------- Pricing / sizing ----------

def estimate_entry_premium(underlying_price: float, config: OptionSimulationConfig) -> float:
    premium = float(underlying_price) * float(config.premium_pct)
    premium = max(float(config.min_premium), min(float(config.max_premium), premium))
    return round(premium, 2)


def _price_model_components(
    signal: str,
    entry_underlying: float,
    current_underlying: float,
    entry_premium_mid: float,
    minutes_held: float,
    config: OptionSimulationConfig,
) -> dict:
    """Conservative Yahoo option-pricing approximation.

    The older simulator added delta * absolute stock dollars directly to the
    premium. That made cheap options explode too easily. This model still uses
    the same Yahoo stock candles, but converts the stock move into a capped
    percentage return on the option premium.

    It is intentionally conservative until IBKR historical option quotes replace
    this approximate pricing engine.
    """
    entry_underlying = max(float(entry_underlying), 0.01)
    entry_premium_mid = max(float(entry_premium_mid), 0.01)
    signed_underlying_move = float(current_underlying) - float(entry_underlying)
    if str(signal).upper() == "PUT":
        signed_underlying_move *= -1

    underlying_move_pct = signed_underlying_move / entry_underlying

    # Raw delta return is economically intuitive but can be too aggressive when
    # the entry premium is tiny. We cap it using the underlying % move.
    raw_delta_return_pct = (float(config.target_delta) * signed_underlying_move) / entry_premium_mid

    abs_underlying_pct = abs(underlying_move_pct)
    if raw_delta_return_pct >= 0:
        # Rough cap table:
        #   0.5% stock move -> about 35% option gain max
        #   1.0% stock move -> about 70% option gain max
        #   2.0% stock move -> about 140% option gain max
        #   3.0% stock move -> about 210% option gain max
        # This still allows strong winners, but prevents tiny stock moves from
        # turning into unrealistic 300-500% option returns.
        gain_cap_pct = min(2.50, max(0.12, abs_underlying_pct * 70.0))
        option_return_before_theta_pct = min(raw_delta_return_pct, gain_cap_pct)
    else:
        # Losing options can decay quickly, but not instantly to zero from one
        # small adverse candle. Stops handle the intended risk limit.
        loss_cap_pct = -min(0.85, max(0.12, abs_underlying_pct * 55.0))
        option_return_before_theta_pct = max(raw_delta_return_pct, loss_cap_pct)

    trading_day_minutes = 390.0
    theta_decay_pct = (float(config.theta_decay_pct_per_day) / 100.0) * max(float(minutes_held), 0.0) / trading_day_minutes

    option_return_after_theta_pct = option_return_before_theta_pct - theta_decay_pct
    mark = entry_premium_mid * (1.0 + option_return_after_theta_pct)

    return {
        "mark": round(max(0.01, float(mark)), 2),
        "underlying_move": round(signed_underlying_move, 4),
        "underlying_move_pct": round(underlying_move_pct * 100.0, 4),
        "raw_delta_return_pct": round(raw_delta_return_pct * 100.0, 2),
        "option_return_before_theta_pct": round(option_return_before_theta_pct * 100.0, 2),
        "theta_decay_pct": round(theta_decay_pct * 100.0, 2),
        "option_return_after_theta_pct": round(option_return_after_theta_pct * 100.0, 2),
        "pricing_model": "yahoo_conservative_pct_delta_cap_v1",
    }


def _mark_option_price(
    signal: str,
    entry_underlying: float,
    current_underlying: float,
    entry_premium_mid: float,
    minutes_held: float,
    config: OptionSimulationConfig,
) -> float:
    return float(_price_model_components(signal, entry_underlying, current_underlying, entry_premium_mid, minutes_held, config)["mark"])


def calculate_position_size(
    entry_premium: float,
    account: SimulatedAccount,
    config: OptionSimulationConfig,
    daily_capital_used: float,
    day_start_equity: float | None = None,
    remaining_trades: int | None = None,
) -> PositionSize:
    buying_power_before = float(account.buying_power)
    current_equity = float(account.equity)
    day_start_equity = float(day_start_equity if day_start_equity is not None else current_equity)
    per_contract_notional = float(entry_premium) * 100.0
    per_contract_commission = float(config.commission_per_contract)
    per_contract_cost = per_contract_notional + per_contract_commission

    sizing_method = str(getattr(config, "sizing_method", "percent_equity") or "percent_equity").lower()
    if sizing_method in {"percent_equity", "% of equity", "percent of equity"}:
        position_budget = current_equity * max(0.0, float(config.position_allocation_pct)) / 100.0
        max_daily_capital = day_start_equity * max(0.0, float(config.max_daily_exposure_pct)) / 100.0
    else:
        position_budget = float(config.max_spend_per_trade)
        max_daily_capital = float(config.max_daily_capital)
        if max_daily_capital <= 0:
            max_daily_capital = current_equity

    remaining_daily_capital = max(0.0, float(max_daily_capital) - float(daily_capital_used))
    if bool(getattr(config, "reserve_capital_for_remaining_trades", True)) and remaining_trades and int(remaining_trades) > 0:
        remaining_daily_capital = min(remaining_daily_capital, remaining_daily_capital / int(remaining_trades))

    allowed_budget = min(
        position_budget,
        buying_power_before,
        remaining_daily_capital,
    )

    if per_contract_cost <= 0:
        qty = 0
    else:
        qty = int(np.floor(allowed_budget / per_contract_cost))

    fixed_cap = int(getattr(config, "max_contracts", 0) or 0)
    if fixed_cap > 0:
        qty = min(qty, fixed_cap)

    qty = max(0, qty)
    option_notional = round(qty * per_contract_notional, 2)
    entry_commission = round(qty * per_contract_commission, 2)
    entry_cost = round(option_notional + entry_commission, 2)

    reason = ""
    if qty <= 0:
        if buying_power_before < per_contract_cost:
            reason = "Insufficient buying power to afford one contract"
        elif remaining_daily_capital < per_contract_cost:
            reason = "Remaining daily capital cannot afford one contract"
        elif allowed_budget < per_contract_cost:
            reason = "Position budget cannot afford one contract"
        else:
            reason = "Position size calculated as zero"

    return PositionSize(
        quantity=qty,
        entry_cost=entry_cost,
        option_notional=option_notional,
        entry_commission=entry_commission,
        buying_power_before=round(buying_power_before, 2),
        buying_power_after_entry=round(buying_power_before - entry_cost, 2),
        allowed_budget=round(allowed_budget, 2),
        contract_cost_with_commission=round(per_contract_cost, 2),
        reason=reason,
    )


# ---------- Simulation helpers ----------

def _future_session_bars(df: pd.DataFrame, entry_ts: pd.Timestamp) -> pd.DataFrame:
    same_day = df[df.index.date == entry_ts.date()].copy()
    return same_day[same_day.index >= entry_ts].copy()


def _historical_option_session(
    signal_row: dict,
    symbol: str,
    signal: str,
    entry_ts: pd.Timestamp,
    entry_underlying: float,
    config: OptionSimulationConfig,
) -> tuple[dict[str, Any] | None, pd.DataFrame]:
    provider = getattr(config, "option_bars_provider", None)
    if provider is None:
        return None, pd.DataFrame()
    info, bars = provider(
        symbol=symbol,
        signal=signal,
        underlying_price=float(entry_underlying),
        option_dte=int(getattr(config, "option_dte", 7) or 7),
        reference_time=entry_ts,
    )
    if bars is None or bars.empty:
        return info, pd.DataFrame()
    option_bars = bars.copy()
    if getattr(option_bars.index, "tz", None) is None:
        option_bars.index = pd.to_datetime(option_bars.index, errors="coerce").tz_localize(config.timezone)
    else:
        option_bars.index = pd.to_datetime(option_bars.index, errors="coerce").tz_convert(config.timezone)
    option_bars = option_bars[option_bars.index.date == entry_ts.date()].copy()
    option_bars = option_bars[option_bars.index >= entry_ts].copy()
    option_bars = option_bars.dropna(subset=["Close"]) if "Close" in option_bars.columns else pd.DataFrame()
    return info, option_bars


def _base_decision(signal_row: dict, status: str, reason: str, stage: str = "SIMULATOR", **extra) -> dict:
    ts = signal_row.get("timestamp") or signal_row.get("Timestamp")
    symbol = str(signal_row.get("symbol") or signal_row.get("Symbol") or "").upper()
    signal = str(signal_row.get("signal") or signal_row.get("Signal") or "").upper()
    strategy = str(signal_row.get("strategy") or signal_row.get("Strategy") or "PMB").upper()
    out = {
        "timestamp": ts,
        "session_date": signal_row.get("session_date"),
        "strategy": strategy,
        "symbol": symbol,
        "signal": signal,
        "score": signal_row.get("score") or signal_row.get("Score"),
        "grade": signal_row.get("grade") or signal_row.get("Grade") or signal_row.get("Setup Quality"),
        "confidence": signal_row.get("confidence") or signal_row.get("Confidence"),
        "price": signal_row.get("price") or signal_row.get("Price"),
        "status": status,
        "stage": stage,
        "reason": reason,
    }
    out.update(extra)
    return out


def _simulate_single_trade(
    signal_row: dict,
    data: dict[str, pd.DataFrame],
    config: OptionSimulationConfig,
    account: SimulatedAccount,
    daily_capital_used: float,
    day_start_equity: float | None = None,
    remaining_trades: int | None = None,
) -> tuple[dict | None, dict, float]:
    symbol = str(signal_row.get("symbol") or signal_row.get("Symbol") or "").strip().upper()
    signal = str(signal_row.get("signal") or signal_row.get("Signal") or "").strip().upper()
    entry_ts = _normalize_timestamp(signal_row.get("timestamp"), config.timezone)

    if not symbol:
        return None, _base_decision(signal_row, "REJECTED", "Missing symbol", "VALIDATION"), daily_capital_used
    if signal not in {"CALL", "PUT"}:
        return None, _base_decision(signal_row, "REJECTED", "Signal is not CALL or PUT", "VALIDATION"), daily_capital_used
    if entry_ts is None:
        return None, _base_decision(signal_row, "REJECTED", "Invalid or missing timestamp", "VALIDATION"), daily_capital_used
    if symbol not in data:
        return None, _base_decision(signal_row, "REJECTED", "No historical Yahoo candles available for symbol", "DATA"), daily_capital_used

    df = data[symbol]
    try:
        entry_underlying = float(signal_row.get("price") or signal_row.get("Price"))
    except Exception:
        entry_underlying = np.nan

    bars = _future_session_bars(df, entry_ts)
    if bars.empty:
        return None, _base_decision(signal_row, "REJECTED", "No future same-session candles after signal", "DATA"), daily_capital_used

    if np.isnan(entry_underlying) or entry_underlying <= 0:
        entry_underlying = float(bars.iloc[0]["Close"])
    if entry_underlying <= 0:
        return None, _base_decision(signal_row, "REJECTED", "Invalid entry underlying price", "VALIDATION"), daily_capital_used

    option_info = None
    option_bars = pd.DataFrame()
    uses_real_option_bars = False
    if getattr(config, "option_bars_provider", None) is not None:
        try:
            option_info, option_bars = _historical_option_session(signal_row, symbol, signal, entry_ts, entry_underlying, config)
            uses_real_option_bars = not option_bars.empty
        except Exception as exc:
            return None, _base_decision(
                signal_row,
                "REJECTED",
                f"IBKR historical option bars unavailable: {exc}",
                "OPTION_DATA",
                option_dte=int(getattr(config, "option_dte", 7) or 7),
            ), daily_capital_used
        if not uses_real_option_bars:
            option_decision_info = {k: v for k, v in (option_info or {}).items() if k != "option_dte"}
            return None, _base_decision(
                signal_row,
                "REJECTED",
                "No IBKR historical option bars after signal timestamp",
                "OPTION_DATA",
                option_dte=int(getattr(config, "option_dte", 7) or 7),
                **option_decision_info,
            ), daily_capital_used

    entry_premium_mid = round(float(option_bars.iloc[0]["Close"]), 2) if uses_real_option_bars else estimate_entry_premium(entry_underlying, config)
    entry_premium = round(entry_premium_mid * (1 + float(config.slippage_pct) / 100.0), 2)
    sizing = calculate_position_size(
        entry_premium,
        account,
        config,
        daily_capital_used,
        day_start_equity=day_start_equity,
        remaining_trades=remaining_trades,
    )

    if sizing.quantity <= 0:
        return None, _base_decision(
            signal_row,
            "REJECTED",
            sizing.reason,
            "SIZING",
            entry_underlying=round(entry_underlying, 2),
            entry_premium=entry_premium,
            contract_cost_with_commission=sizing.contract_cost_with_commission,
            buying_power_before=sizing.buying_power_before,
            remaining_daily_capital=round(max(0.0, sizing.allowed_budget), 2),
            allowed_budget=sizing.allowed_budget,
            sizing_method=str(getattr(config, "sizing_method", "percent_equity")),
            position_allocation_pct=float(getattr(config, "position_allocation_pct", 0)),
            max_daily_exposure_pct=float(getattr(config, "max_daily_exposure_pct", 0)),
            max_spend_per_trade=float(config.max_spend_per_trade),
            max_daily_capital=float(config.max_daily_capital),
            suggested_min_spend=sizing.contract_cost_with_commission,
        ), daily_capital_used

    try:
        account.reserve_entry_cost(sizing.entry_cost)
    except Exception:
        return None, _base_decision(
            signal_row,
            "REJECTED",
            "Insufficient buying power at broker entry",
            "BROKER",
            estimated_cost=sizing.entry_cost,
            buying_power_before=sizing.buying_power_before,
        ), daily_capital_used

    initial_stop = entry_premium * (1 - float(config.stop_loss_pct) / 100.0)
    take_profit = entry_premium * (1 + float(config.take_profit_pct) / 100.0)
    current_stop = initial_stop
    highest_price = entry_premium
    breakeven_active = False
    trailing_active = False

    price_bars = option_bars if uses_real_option_bars else bars
    exit_ts = price_bars.index[-1]
    underlying_until_exit = bars[bars.index <= exit_ts]
    exit_underlying = float(underlying_until_exit.iloc[-1]["Close"]) if not underlying_until_exit.empty else float(bars.iloc[-1]["Close"])
    exit_premium_mid = entry_premium_mid
    exit_reason = "End-of-day forced exit"
    exit_pricing_components = (
        {
            "pricing_model": "ibkr_historical_option_bars_v1",
            "option_return_after_theta_pct": 0.0,
            "theta_decay_pct": 0.0,
            "raw_delta_return_pct": 0.0,
        }
        if uses_real_option_bars
        else _price_model_components(signal, entry_underlying, exit_underlying, entry_premium_mid, 0.0, config)
    )

    for ts, bar in price_bars.iterrows():
        stock_until_ts = bars[bars.index <= ts]
        current_underlying = float(stock_until_ts.iloc[-1]["Close"]) if not stock_until_ts.empty else entry_underlying
        minutes_held = max(0.0, (ts - entry_ts).total_seconds() / 60.0)
        if uses_real_option_bars:
            current_premium = round(float(bar["Close"]), 2)
            pricing_components = {
                "pricing_model": "ibkr_historical_option_bars_v1",
                "option_return_after_theta_pct": round(((current_premium - entry_premium_mid) / entry_premium_mid) * 100.0, 2) if entry_premium_mid else 0.0,
                "theta_decay_pct": 0.0,
                "raw_delta_return_pct": 0.0,
            }
        else:
            pricing_components = _price_model_components(signal, entry_underlying, current_underlying, entry_premium_mid, minutes_held, config)
            current_premium = float(pricing_components["mark"])
        highest_price = max(highest_price, current_premium)
        pnl_pct = ((current_premium - entry_premium) / entry_premium) * 100.0 if entry_premium else 0.0

        reason = None
        if bool(getattr(config, "force_exit_enabled", True)) and ts.time() >= config.force_exit_time:
            reason = "End-of-day forced exit"
        elif trailing_active and current_premium <= current_stop:
            reason = "Trailing stop hit"
        elif breakeven_active and current_premium <= current_stop:
            reason = "Breakeven stop hit"
        elif current_premium <= initial_stop:
            reason = "Stop loss hit"
        elif pnl_pct >= float(config.breakeven_trigger_pct) and not breakeven_active:
            current_stop = max(current_stop, entry_premium)
            breakeven_active = True

        if reason is None and pnl_pct >= float(config.trailing_trigger_pct):
            trailing_active = True
            trail_stop = highest_price * (1 - float(config.trailing_stop_pct) / 100.0)
            current_stop = max(current_stop, trail_stop)

        if reason is None and not trailing_active and current_premium >= take_profit:
            reason = "Take profit hit"

        if reason is None and trailing_active and current_premium <= current_stop:
            reason = "Trailing stop hit"

        if reason:
            exit_ts = ts
            exit_underlying = current_underlying
            exit_pricing_components = dict(pricing_components)

            # Fill at the triggered order level, not at the collapsed mark.
            # Example: a 20% stop on a $0.44 option should fill near $0.35,
            # not at $0.01 just because the next candle mark printed through it.
            # This keeps the backtest aligned with stop-market/stop-limit intent
            # and prevents stop-loss trades from incorrectly losing nearly 100%.
            if reason == "Stop loss hit":
                exit_premium_mid = initial_stop
            elif reason == "Breakeven stop hit":
                exit_premium_mid = current_stop
            elif reason == "Trailing stop hit":
                exit_premium_mid = current_stop
            elif reason == "Take profit hit":
                exit_premium_mid = take_profit
            else:
                exit_premium_mid = current_premium

            exit_premium_mid = round(max(0.01, float(exit_premium_mid)), 2)
            exit_reason = reason
            break

    if exit_reason == "End-of-day forced exit" and not bool(getattr(config, "force_exit_enabled", True)):
        exit_reason = "Session close"

    if exit_reason in {"End-of-day forced exit", "Session close"} and not uses_real_option_bars:
        final_minutes_held = max(0.0, (exit_ts - entry_ts).total_seconds() / 60.0)
        exit_pricing_components = _price_model_components(signal, entry_underlying, exit_underlying, entry_premium_mid, final_minutes_held, config)
        exit_premium_mid = float(exit_pricing_components["mark"])
    elif exit_reason in {"End-of-day forced exit", "Session close"} and uses_real_option_bars:
        exit_premium_mid = round(float(price_bars.iloc[-1]["Close"]), 2)

    # Directional consistency guard for the Yahoo approximate pricing model.
    # In real options, a profitable trailing stop can sometimes fill before the
    # underlying closes back below entry. With 5-minute Yahoo stock candles only,
    # we do not have the actual option tape or intrabar stop trigger. To avoid
    # impossible-looking results such as a CALL closing with the stock below the
    # entry price but still booking a large profit, cap favorable fills to
    # breakeven whenever the final underlying move is against the option. This
    # keeps the approximate Yahoo model conservative until IBKR historical option
    # prices are available.
    directional_guard_applied = False
    if not uses_real_option_bars and str(signal).upper() == "CALL" and float(exit_underlying) <= float(entry_underlying) and float(exit_premium_mid) > float(entry_premium):
        exit_premium_mid = float(entry_premium)
        directional_guard_applied = True
    elif not uses_real_option_bars and str(signal).upper() == "PUT" and float(exit_underlying) >= float(entry_underlying) and float(exit_premium_mid) > float(entry_premium):
        exit_premium_mid = float(entry_premium)
        directional_guard_applied = True

    exit_premium = round(exit_premium_mid * (1 - float(config.slippage_pct) / 100.0), 2)
    exit_notional = round(exit_premium * sizing.quantity * 100.0, 2)
    exit_commission = round(sizing.quantity * float(config.commission_per_contract), 2)
    exit_credit = round(max(0.0, exit_notional - exit_commission), 2)
    total_commissions = round(sizing.entry_commission + exit_commission, 2)
    gross_pnl = round(exit_notional - sizing.option_notional, 2)
    realized_pnl = round(exit_credit - sizing.entry_cost, 2)
    return_pct = round((realized_pnl / sizing.entry_cost * 100.0), 2) if sizing.entry_cost else 0.0
    hold_minutes = round(max(0.0, (exit_ts - entry_ts).total_seconds() / 60.0), 1)

    account.close_position(exit_credit=exit_credit, realized_pnl=realized_pnl, commissions=total_commissions)
    daily_capital_used = (
        round(float(daily_capital_used) + sizing.entry_cost, 2)
        if not bool(getattr(config, "recycle_capital_after_exit", False))
        else round(float(daily_capital_used), 2)
    )

    trade = {
        "entry_time": entry_ts,
        "exit_time": exit_ts,
        "date": entry_ts.date().isoformat(),
        "strategy": str(signal_row.get("strategy") or signal_row.get("Strategy") or "PMB").upper(),
        "symbol": symbol,
        "signal": signal,
        "option_dte": int(getattr(config, "option_dte", 7) or 7),
        "option_expiry": (option_info or {}).get("expiry"),
        "option_strike": (option_info or {}).get("strike"),
        "option_right": (option_info or {}).get("right"),
        "option_local_symbol": (option_info or {}).get("localSymbol"),
        "score": signal_row.get("score"),
        "grade": signal_row.get("grade") or signal_row.get("Grade") or signal_row.get("Setup Quality"),
        "confidence": signal_row.get("confidence"),
        "entry_underlying": round(entry_underlying, 2),
        "exit_underlying": round(exit_underlying, 2),
        "entry_premium_mid": entry_premium_mid,
        "entry_premium": entry_premium,
        "exit_premium": exit_premium,
        "quantity": sizing.quantity,
        "contract_cost_with_commission": sizing.contract_cost_with_commission,
        "option_notional": sizing.option_notional,
        "estimated_cost": sizing.entry_cost,
        "exit_credit": exit_credit,
        "gross_pnl": gross_pnl,
        "commissions": total_commissions,
        "realized_pnl": realized_pnl,
        "return_pct": return_pct,
        "underlying_move": round(exit_underlying - entry_underlying, 4),
        "underlying_move_pct": round(((exit_underlying - entry_underlying) / entry_underlying) * 100.0, 4) if entry_underlying else 0.0,
        "option_return_pct": round(((exit_premium - entry_premium) / entry_premium) * 100.0, 2) if entry_premium else 0.0,
        "directional_guard_applied": directional_guard_applied,
        "pricing_model": exit_pricing_components.get("pricing_model"),
        "raw_delta_return_pct": exit_pricing_components.get("raw_delta_return_pct"),
        "model_option_return_pct": exit_pricing_components.get("option_return_after_theta_pct"),
        "theta_decay_pct": exit_pricing_components.get("theta_decay_pct"),
        "hold_minutes": hold_minutes,
        "exit_reason": exit_reason,
        "buying_power_before": sizing.buying_power_before,
        "buying_power_after_entry": sizing.buying_power_after_entry,
        "buying_power_after_exit": round(account.buying_power, 2),
        "account_equity": round(account.equity, 2),
        "daily_capital_used": daily_capital_used,
        "rvol": signal_row.get("rvol"),
        "atr_pct": signal_row.get("atr_pct"),
        "vwap": signal_row.get("vwap"),
        "orb_high": signal_row.get("orb_high"),
        "orb_low": signal_row.get("orb_low"),
        "pdh": signal_row.get("pdh"),
        "pdl": signal_row.get("pdl"),
        "reasons": signal_row.get("reasons"),
    }
    decision = _base_decision(
        signal_row,
        "TRADED",
        f"Opened simulated option trade and exited: {exit_reason}",
        "BROKER",
        entry_underlying=round(entry_underlying, 2),
        option_dte=int(getattr(config, "option_dte", 7) or 7),
        option_expiry=(option_info or {}).get("expiry"),
        option_strike=(option_info or {}).get("strike"),
        option_local_symbol=(option_info or {}).get("localSymbol"),
        entry_premium=entry_premium,
        contract_cost_with_commission=sizing.contract_cost_with_commission,
        quantity=sizing.quantity,
        sizing_method=str(getattr(config, "sizing_method", "percent_equity")),
        position_allocation_pct=float(getattr(config, "position_allocation_pct", 0)),
        max_daily_exposure_pct=float(getattr(config, "max_daily_exposure_pct", 0)),
        max_spend_per_trade=float(config.max_spend_per_trade),
        max_daily_capital=float(config.max_daily_capital),
        max_contracts=(int(config.max_contracts) if int(config.max_contracts or 0) > 0 else "unlimited"),
        buying_power_before=sizing.buying_power_before,
        buying_power_after_entry=sizing.buying_power_after_entry,
        buying_power_after_exit=round(account.buying_power, 2),
        estimated_cost=sizing.entry_cost,
        exit_credit=exit_credit,
        exit_reason=exit_reason,
        underlying_move_pct=round(((exit_underlying - entry_underlying) / entry_underlying) * 100.0, 4) if entry_underlying else 0.0,
        option_return_pct=round(((exit_premium - entry_premium) / entry_premium) * 100.0, 2) if entry_premium else 0.0,
        pricing_model=exit_pricing_components.get("pricing_model"),
        raw_delta_return_pct=exit_pricing_components.get("raw_delta_return_pct"),
        model_option_return_pct=exit_pricing_components.get("option_return_after_theta_pct"),
        theta_decay_pct=exit_pricing_components.get("theta_decay_pct"),
        realized_pnl=realized_pnl,
        directional_guard_applied=directional_guard_applied,
        account_equity=round(account.equity, 2),
        daily_capital_used=daily_capital_used,
    )
    return trade, decision, daily_capital_used


# ---------- Public API ----------

def simulate_option_trades_with_decisions(
    signals: pd.DataFrame,
    data: dict[str, pd.DataFrame],
    config: OptionSimulationConfig | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    config = config or OptionSimulationConfig()
    account = SimulatedAccount(float(config.starting_capital))

    if signals is None or signals.empty:
        return pd.DataFrame(), pd.DataFrame()

    clean_data = _clean_data(data, timezone=config.timezone)
    sig = signals.copy()
    if "timestamp" not in sig.columns:
        return pd.DataFrame(), pd.DataFrame([{"status": "REJECTED", "stage": "VALIDATION", "reason": "Signals table has no timestamp column"}])

    sig["_ts"] = pd.to_datetime(sig["timestamp"], errors="coerce", utc=True)
    sort_col = "symbol" if "symbol" in sig.columns else sig.columns[0]
    sig = sig.dropna(subset=["_ts"]).sort_values(["_ts", sort_col])

    trades: list[dict] = []
    decisions: list[dict] = []
    day_trade_counts: dict[str, int] = {}
    day_capital_used: dict[str, float] = {}
    day_start_equity: dict[str, float] = {}
    day_realized_pnl: dict[str, float] = {}
    day_consecutive_losses: dict[str, int] = {}
    traded_symbols_by_day: dict[str, set[str]] = {}

    if not clean_data:
        for _, row in sig.iterrows():
            decisions.append(_base_decision(row.dropna().to_dict(), "REJECTED", "No clean historical data available for simulator", "DATA"))
        return pd.DataFrame(), pd.DataFrame(decisions)

    for _, row in sig.iterrows():
        row_dict = row.dropna().to_dict()
        ts = _normalize_timestamp(row_dict.get("timestamp"), config.timezone)
        symbol = str(row_dict.get("symbol") or row_dict.get("Symbol") or "").upper()
        if ts is None:
            decisions.append(_base_decision(row_dict, "REJECTED", "Invalid timestamp", "VALIDATION"))
            continue
        day_key = ts.date().isoformat()

        day_start_equity.setdefault(day_key, float(account.equity))
        max_daily_loss = -abs(float(day_start_equity[day_key]) * float(getattr(config, "max_daily_drawdown_pct", 5.0)) / 100.0)

        if ts.time() > getattr(config, "entry_cutoff_time", dtime(11, 0)):
            decisions.append(_base_decision(
                row_dict,
                "SKIPPED",
                "Entry cutoff time passed",
                "RISK",
                entry_cutoff_time=str(getattr(config, "entry_cutoff_time", dtime(11, 0))),
                buying_power_before=round(account.buying_power, 2),
            ))
            continue
        if day_consecutive_losses.get(day_key, 0) >= int(getattr(config, "max_consecutive_losses", 2)):
            decisions.append(_base_decision(
                row_dict,
                "SKIPPED",
                "Consecutive loss risk lock active",
                "RISK",
                consecutive_losses=day_consecutive_losses.get(day_key, 0),
                max_consecutive_losses=int(getattr(config, "max_consecutive_losses", 2)),
                buying_power_before=round(account.buying_power, 2),
            ))
            continue
        if float(day_realized_pnl.get(day_key, 0.0)) <= max_daily_loss:
            decisions.append(_base_decision(
                row_dict,
                "SKIPPED",
                "Daily drawdown risk lock active",
                "RISK",
                realized_pnl_today=round(float(day_realized_pnl.get(day_key, 0.0)), 2),
                max_daily_loss=round(float(max_daily_loss), 2),
                buying_power_before=round(account.buying_power, 2),
            ))
            continue
        if day_trade_counts.get(day_key, 0) >= int(config.max_trades_per_day):
            decisions.append(_base_decision(row_dict, "SKIPPED", "Max trades/day reached", "RISK", max_trades_per_day=int(config.max_trades_per_day), buying_power_before=round(account.buying_power, 2)))
            continue
        if not config.allow_same_symbol_same_day and symbol in traded_symbols_by_day.get(day_key, set()):
            decisions.append(_base_decision(row_dict, "SKIPPED", "Same symbol already traded that day", "RISK", buying_power_before=round(account.buying_power, 2)))
            continue

        trade, decision, new_daily_used = _simulate_single_trade(
            row_dict,
            clean_data,
            config,
            account,
            day_capital_used.get(day_key, 0.0),
            day_start_equity.get(day_key, float(account.equity)),
            max(0, int(config.max_trades_per_day) - day_trade_counts.get(day_key, 0)),
        )
        if trade is None:
            decisions.append(decision)
            continue

        trades.append(trade)
        day_trade_counts[day_key] = day_trade_counts.get(day_key, 0) + 1
        day_capital_used[day_key] = new_daily_used
        trade_pnl = float(trade.get("realized_pnl", 0.0) or 0.0)
        day_realized_pnl[day_key] = round(float(day_realized_pnl.get(day_key, 0.0)) + trade_pnl, 2)
        day_consecutive_losses[day_key] = day_consecutive_losses.get(day_key, 0) + 1 if trade_pnl < 0 else 0
        traded_symbols_by_day.setdefault(day_key, set()).add(symbol)
        decision["trade_no"] = len(trades)
        decisions.append(decision)

    trades_df = pd.DataFrame(trades)
    if not trades_df.empty:
        trades_df = trades_df.sort_values("entry_time").reset_index(drop=True)
        trades_df["trade_no"] = trades_df.index + 1
        # Account equity is already recorded after each exit; keep cumulative P/L too.
        trades_df["cumulative_pnl"] = pd.to_numeric(trades_df["realized_pnl"], errors="coerce").fillna(0.0).cumsum()
        trades_df["equity"] = pd.to_numeric(trades_df["account_equity"], errors="coerce").fillna(float(config.starting_capital) + trades_df["cumulative_pnl"])

    decisions_df = pd.DataFrame(decisions)
    if not decisions_df.empty:
        decisions_df = decisions_df.reset_index(drop=True)
        decisions_df["decision_no"] = decisions_df.index + 1
    return trades_df, decisions_df


def simulate_option_trades(
    signals: pd.DataFrame,
    data: dict[str, pd.DataFrame],
    config: OptionSimulationConfig | None = None,
) -> pd.DataFrame:
    trades, _ = simulate_option_trades_with_decisions(signals, data, config)
    return trades


def summarize_trades(trades: pd.DataFrame, starting_capital: float = 1000.0) -> dict:
    if trades is None or trades.empty:
        return {
            "total_trades": 0,
            "net_pnl": 0.0,
            "ending_equity": float(starting_capital),
            "return_pct": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "avg_trade": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "commissions": 0.0,
        }
    pnl = pd.to_numeric(trades["realized_pnl"], errors="coerce").fillna(0.0)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    net = float(pnl.sum())
    if "equity" in trades.columns:
        equity = pd.to_numeric(trades["equity"], errors="coerce").fillna(float(starting_capital) + pnl.cumsum())
    else:
        equity = float(starting_capital) + pnl.cumsum()
    running_max = equity.cummax()
    dd = equity - running_max
    gross_profit = float(wins.sum()) if len(wins) else 0.0
    gross_loss = abs(float(losses.sum())) if len(losses) else 0.0
    commissions = float(pd.to_numeric(trades.get("commissions", 0), errors="coerce").fillna(0).sum()) if hasattr(trades.get("commissions", 0), "sum") else 0.0
    return {
        "total_trades": int(len(trades)),
        "net_pnl": round(net, 2),
        "ending_equity": round(float(equity.iloc[-1]) if len(equity) else float(starting_capital) + net, 2),
        "return_pct": round((float(equity.iloc[-1]) - float(starting_capital)) / float(starting_capital) * 100.0, 2) if starting_capital and len(equity) else 0.0,
        "win_rate": round(len(wins) / len(trades) * 100.0, 1) if len(trades) else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else (round(gross_profit, 2) if gross_profit else 0.0),
        "max_drawdown": round(float(dd.min()), 2) if len(dd) else 0.0,
        "avg_trade": round(float(pnl.mean()), 2) if len(pnl) else 0.0,
        "avg_win": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "largest_win": round(float(pnl.max()), 2) if len(pnl) else 0.0,
        "largest_loss": round(float(pnl.min()), 2) if len(pnl) else 0.0,
        "commissions": round(commissions, 2),
    }
