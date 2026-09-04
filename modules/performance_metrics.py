from __future__ import annotations

"""Pure performance calculations shared by the dashboard and tests."""

import re
from typing import Any

import pandas as pd


def _broker_id_key(value: Any) -> str:
    try:
        if value is None or pd.isna(value):
            return ""
    except Exception:
        pass
    tokens = {token.strip() for token in str(value).split(",") if token.strip()}
    return ",".join(sorted(tokens))


def deduplicate_broker_event_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Keep one authoritative row when local and broker logs describe one fill."""
    if df is None or df.empty:
        return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    required = {"timestamp", "event", "symbol", "quantity"}
    if not required.issubset(df.columns):
        return df.copy()

    out = df.copy().reset_index(drop=True)
    event = out["event"].fillna("").astype(str).str.upper().str.strip()
    date = out["timestamp"].astype(str).str.slice(0, 10)
    symbol = out["symbol"].fillna("").astype(str).str.upper().str.strip()
    signal = out.get("signal", pd.Series("", index=out.index)).fillna("").astype(str).str.upper().str.strip()
    quantity = pd.to_numeric(out["quantity"], errors="coerce").fillna(0).abs().round(6).astype(str)
    order_ids = out.get("broker_order_ids", pd.Series("", index=out.index)).apply(_broker_id_key)
    perm_ids = out.get("broker_perm_ids", pd.Series("", index=out.index)).apply(_broker_id_key)
    broker_ids = order_ids.where(order_ids.ne(""), perm_ids)
    eligible = event.isin(["ENTRY", "EXIT"]) & broker_ids.ne("")
    out["_broker_event_key"] = ""
    out.loc[eligible, "_broker_event_key"] = (
        date[eligible] + "|" + event[eligible] + "|" + symbol[eligible] + "|"
        + signal[eligible] + "|" + quantity[eligible] + "|" + broker_ids[eligible]
    )

    # A local order estimate and its eventual IBKR execution can carry entirely
    # different order/perm IDs. Pair them by trade identity and proximity so the
    # actual broker fill remains authoritative.
    source = out.get("source", pd.Series("", index=out.index)).fillna("").astype(str).str.upper().str.strip()
    broker_source = source.isin(["IBKR_EXECUTION", "IBKR_FLEX"])
    local_source = ~broker_source
    try:
        timestamps = pd.to_datetime(out["timestamp"], errors="coerce", utc=True, format="mixed")
    except TypeError:
        timestamps = pd.to_datetime(out["timestamp"], errors="coerce", utc=True)
    con_ids = pd.to_numeric(out.get("con_id", pd.Series(0, index=out.index)), errors="coerce").fillna(0).astype(int)
    local_indexes = out.index[local_source & event.isin(["ENTRY", "EXIT"])]
    for local_index in local_indexes:
        local_time = timestamps.iloc[local_index]
        if pd.isna(local_time):
            continue
        same_trade = (
            broker_source
            & event.eq(event.iloc[local_index])
            & date.eq(date.iloc[local_index])
            & symbol.eq(symbol.iloc[local_index])
            & signal.eq(signal.iloc[local_index])
            & quantity.eq(quantity.iloc[local_index])
        )
        local_con_id = con_ids.iloc[local_index]
        if local_con_id:
            same_trade &= con_ids.eq(0) | con_ids.eq(local_con_id)
        candidates = out.index[same_trade]
        if candidates.empty:
            continue
        deltas = (timestamps.loc[candidates] - local_time).abs().dt.total_seconds()
        deltas = deltas[deltas <= 600]
        if deltas.empty:
            continue
        broker_index = deltas.idxmin()
        broker_key = out.at[broker_index, "_broker_event_key"]
        if broker_key:
            out.at[local_index, "_broker_event_key"] = broker_key

    source_rank = {"IBKR_EXECUTION": 0, "IBKR_FLEX": 1, "PULSE_EXIT_ORDER": 2, "PULSE_ENTRY_ORDER": 2}
    rows = []
    for _, group in out[out["_broker_event_key"].ne("")].groupby("_broker_event_key", sort=False):
        ranked = group.assign(
            _source_rank=group.get("source", pd.Series("", index=group.index))
            .fillna("").astype(str).str.upper().map(source_rank).fillna(9)
        ).sort_values("_source_rank")
        canonical = ranked.iloc[0].drop(labels=["_source_rank"]).copy()
        for column in group.columns:
            if column == "_broker_event_key":
                continue
            current = canonical.get(column)
            try:
                missing = current is None or pd.isna(current) or str(current).strip() == ""
            except Exception:
                missing = False
            if missing:
                values = group[column].dropna()
                values = values[values.astype(str).str.strip().ne("")]
                if not values.empty:
                    canonical[column] = values.iloc[-1]
        if "exit_reason" in group.columns:
            reasons = group["exit_reason"].dropna().astype(str).str.strip()
            meaningful = reasons[~reasons.str.lower().isin(["", "ibkr sell execution imported"])]
            if not meaningful.empty:
                canonical["exit_reason"] = meaningful.iloc[-1]
        rows.append(canonical)

    untouched = out[out["_broker_event_key"].eq("")].copy()
    canonical_df = pd.DataFrame(rows, columns=out.columns) if rows else out.iloc[0:0].copy()
    result = pd.concat([untouched, canonical_df], ignore_index=True, sort=False)
    return result.drop(columns=["_broker_event_key"], errors="ignore")


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
