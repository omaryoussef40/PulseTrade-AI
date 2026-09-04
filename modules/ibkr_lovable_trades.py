from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests

from modules.performance_metrics import deduplicate_broker_event_rows


DEFAULT_LOVABLE_TRADE_SYNC_URL = "https://thedesk.dev/api/public/ibkr/trades"


def _text(value: Any) -> str:
    try:
        if value is None or pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and abs(result) < 1e100 else None


def _expiry(value: Any) -> str | None:
    raw = _text(value).split(".", 1)[0].replace("-", "")
    if len(raw) >= 8 and raw[:8].isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return _text(value) or None


def _utc_datetime(value: pd.Timestamp) -> str:
    return value.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def closed_trade_payloads(
    trade_log: pd.DataFrame,
    trade_date: date,
    symbols: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Pair authoritative IBKR entries/exits into Lovable closed trades."""
    if trade_log is None or trade_log.empty:
        return []
    df = deduplicate_broker_event_rows(trade_log)
    if "timestamp" not in df.columns or "event" not in df.columns:
        return []
    df = df.copy()
    try:
        df["_timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True, format="mixed")
    except TypeError:
        df["_timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    df = df.dropna(subset=["_timestamp"])
    df["_et_date"] = df["_timestamp"].dt.tz_convert("America/New_York").dt.date
    source = df.get("source", pd.Series("", index=df.index)).fillna("").astype(str).str.upper()
    event = df["event"].fillna("").astype(str).str.upper()
    df = df[(df["_et_date"] == trade_date) & source.eq("IBKR_EXECUTION") & event.isin(["ENTRY", "EXIT"])].copy()
    wanted = {str(symbol).strip().upper() for symbol in symbols or [] if str(symbol).strip()}
    if wanted:
        symbols_series = df.get("symbol", pd.Series("", index=df.index)).fillna("").astype(str).str.upper()
        df = df[symbols_series.isin(wanted)]
    if df.empty:
        return []
    df = df.sort_values("_timestamp")
    entries = df[df["event"].fillna("").astype(str).str.upper().eq("ENTRY")]
    exits = df[df["event"].fillna("").astype(str).str.upper().eq("EXIT")]
    used_entries: set[int] = set()
    trades: list[dict[str, Any]] = []
    for exit_index, exit_row in exits.iterrows():
        con_id = int(_number(exit_row.get("con_id")) or 0)
        exit_qty = abs(_number(exit_row.get("filled_quantity")) or _number(exit_row.get("quantity")) or 0)
        if not con_id or not exit_qty:
            continue
        entry_ids = pd.to_numeric(entries.get("con_id", pd.Series(0, index=entries.index)), errors="coerce").fillna(0).astype(int)
        candidates = entries[
            entry_ids.eq(con_id)
            & entries["_timestamp"].le(exit_row["_timestamp"])
            & ~entries.index.isin(used_entries)
        ].copy()
        if candidates.empty:
            continue
        qty_source = candidates.get("filled_quantity", candidates.get("quantity", pd.Series(0, index=candidates.index)))
        candidate_qty = pd.to_numeric(qty_source, errors="coerce").fillna(0).abs()
        exact = candidates[candidate_qty.eq(exit_qty)]
        if not exact.empty:
            candidates = exact
        entry_index = candidates.sort_values("_timestamp").index[-1]
        used_entries.add(int(entry_index))
        entry_row = entries.loc[entry_index]

        entry_price = _number(entry_row.get("entry_price")) or _number(entry_row.get("limit_price"))
        exit_price = _number(exit_row.get("exit_price"))
        multiplier = _number(exit_row.get("multiplier")) or _number(entry_row.get("multiplier"))
        asset_class = (_text(exit_row.get("sec_type")) or _text(entry_row.get("sec_type")) or "OPT").upper()
        multiplier = multiplier or (100.0 if asset_class in {"OPT", "FOP"} else 1.0)
        realized_pnl = _number(exit_row.get("realized_pnl"))
        if realized_pnl is None and entry_price is not None and exit_price is not None:
            realized_pnl = round((exit_price - entry_price) * exit_qty * multiplier, 2)
        commission_values = [_number(entry_row.get("commission")), _number(exit_row.get("commission"))]
        commission = sum(value for value in commission_values if value is not None) if any(
            value is not None for value in commission_values
        ) else None
        signal = (_text(exit_row.get("signal")) or _text(entry_row.get("signal"))).upper()
        exit_external_id = _text(exit_row.get("external_id"))
        external_id = f"CLOSED-{exit_external_id}" if exit_external_id else f"CLOSED-{trade_date}-{con_id}-{exit_index}"
        trades.append({
            "external_id": external_id,
            "symbol": (_text(exit_row.get("symbol")) or _text(entry_row.get("symbol"))).upper(),
            "asset_class": asset_class,
            "option_type": "C" if signal.startswith("C") else "P" if signal.startswith("P") else None,
            "conid": str(con_id),
            "expiry": _expiry(exit_row.get("expiry")) or _expiry(entry_row.get("expiry")),
            "strike": _number(exit_row.get("strike")) or _number(entry_row.get("strike")),
            "quantity": exit_qty,
            "entry_time": _utc_datetime(entry_row["_timestamp"]),
            "exit_time": _utc_datetime(exit_row["_timestamp"]),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "realized_pnl": realized_pnl,
            "commission": commission,
            "currency": "USD",
            "status": "CLOSED",
            "source": "IBKR_EXECUTION",
        })
    return trades


class LovableTradeStore:
    def __init__(self, url: str, secret: str, timeout: float = 15.0, session: Any = None):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.headers = {
            "Authorization": f"Bearer {secret}",
            "X-Position-Sync-Secret": secret,
            "Content-Type": "application/json",
        }

    def upsert(self, account_id: str, trades: list[dict[str, Any]]) -> dict[str, Any]:
        response = self.session.post(
            self.url,
            headers=self.headers,
            json={"account_id": account_id, "trades": trades},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            detail = _text(getattr(response, "text", ""))[:500]
            raise RuntimeError(f"Lovable trade sync failed ({response.status_code}): {detail}")
        try:
            body = response.json()
        except Exception as exc:
            raise RuntimeError("Lovable trade sync returned invalid JSON") from exc
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise RuntimeError(f"Lovable trade sync rejected payload: {_text(body)[:500]}")
        return body


def load_closed_trade_payloads(
    trade_log_file: str | Path,
    trade_date: date,
    symbols: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    path = Path(trade_log_file)
    if not path.exists():
        return []
    return closed_trade_payloads(pd.read_csv(path, low_memory=False), trade_date, symbols)
