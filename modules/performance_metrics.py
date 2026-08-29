from __future__ import annotations

"""Pure performance calculations shared by the dashboard and tests."""

import re
from typing import Any

import pandas as pd


def _number(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        number = float(value)
        return number if pd.notna(number) else None
    except Exception:
        return None


def _text(value: Any) -> str:
    try:
        if value is None or pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip().upper()


def _contract_id(row: pd.Series) -> int:
    value = _number(row.get("con_id"))
    return int(value) if value and value > 0 else 0


def _option_key(row: pd.Series) -> str:
    return re.sub(r"\s+", " ", _text(row.get("option"))).strip()


def _matching_entry(exit_row: pd.Series, entries: pd.DataFrame) -> pd.Series | None:
    if entries is None or entries.empty:
        return None
    candidates = entries.copy()
    if "timestamp" in candidates.columns and "timestamp" in exit_row.index:
        exit_time = exit_row.get("timestamp")
        if pd.notna(exit_time):
            candidates = candidates[candidates["timestamp"] <= exit_time]
    if candidates.empty:
        return None

    con_id = _contract_id(exit_row)
    if con_id and "con_id" in candidates.columns:
        entry_ids = pd.to_numeric(candidates["con_id"], errors="coerce").fillna(0).astype(int)
        matched = candidates[entry_ids == con_id]
        if not matched.empty:
            candidates = matched
        else:
            candidates = pd.DataFrame()

    if candidates.empty or not con_id:
        pool = entries.copy()
        if "timestamp" in pool.columns and "timestamp" in exit_row.index:
            exit_time = exit_row.get("timestamp")
            if pd.notna(exit_time):
                pool = pool[pool["timestamp"] <= exit_time]
        option_key = _option_key(exit_row)
        if option_key and "option" in pool.columns:
            option_values = pool["option"].apply(lambda value: re.sub(r"\s+", " ", _text(value)).strip())
            matched = pool[option_values == option_key]
        else:
            symbol = _text(exit_row.get("symbol"))
            signal = _text(exit_row.get("signal"))
            symbol_values = pool.get("symbol", pd.Series("", index=pool.index)).apply(_text)
            signal_values = pool.get("signal", pd.Series("", index=pool.index)).apply(_text)
            matched = pool[(symbol_values == symbol) & (signal_values == signal)]
        if matched.empty:
            return None
        candidates = matched

    if "timestamp" in candidates.columns:
        candidates = candidates.sort_values("timestamp")
    return candidates.iloc[-1] if not candidates.empty else None


def _multiplier(exit_row: pd.Series, entry_row: pd.Series | None) -> float:
    for row in [exit_row, entry_row]:
        if row is None:
            continue
        value = _number(row.get("multiplier"))
        if value and value > 0:
            return abs(value)
    sec_type = _text(exit_row.get("sec_type"))
    signal = _text(exit_row.get("signal"))
    if entry_row is not None:
        sec_type = sec_type or _text(entry_row.get("sec_type"))
        signal = signal or _text(entry_row.get("signal"))
    return 100.0 if sec_type in {"OPT", "OPTION"} or signal in {"CALL", "PUT"} else 1.0


def realized_r_multiple(
    exits: pd.DataFrame,
    stop_loss_pct: float,
    entries: pd.DataFrame | None = None,
) -> float:
    """Return the sum of realized trade-level R for the supplied closed trades."""
    if exits is None or exits.empty or float(stop_loss_pct) <= 0:
        return 0.0

    total_r = 0.0
    stop_fraction = float(stop_loss_pct) / 100.0
    entry_rows = entries if isinstance(entries, pd.DataFrame) else pd.DataFrame()
    for _, exit_row in exits.iterrows():
        entry_row = _matching_entry(exit_row, entry_rows)
        entry_price = _number(exit_row.get("entry_price"))
        if not entry_price or entry_price <= 0:
            entry_price = _number(entry_row.get("entry_price")) if entry_row is not None else None

        quantity = _number(exit_row.get("quantity"))
        if not quantity or quantity <= 0:
            quantity = _number(entry_row.get("quantity")) if entry_row is not None else None
        pnl = _number(exit_row.get("realized_pnl"))
        if entry_price is None or quantity is None or pnl is None:
            continue

        initial_risk = abs(entry_price) * abs(quantity) * _multiplier(exit_row, entry_row) * stop_fraction
        if initial_risk > 0:
            total_r += pnl / initial_risk
    return round(total_r, 2)
