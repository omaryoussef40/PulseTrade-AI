from __future__ import annotations

"""Historical ORB and option-premium outcome research."""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable

import pandas as pd


EASTERN = "America/New_York"
REQUIRED_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


@dataclass(frozen=True)
class ORBResultsConfig:
    period: str = "90d"
    orb_minutes: int = 15
    confirmation_minutes: int = 15
    option_dte: int = 7
    contract_gain_pct: float = 20.0
    interval: str = "5m"


def _normalize_bars(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=REQUIRED_COLUMNS)
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"Historical bars are missing columns: {missing}")
    out = frame[REQUIRED_COLUMNS].copy().apply(pd.to_numeric, errors="coerce")
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    idx = pd.DatetimeIndex(pd.to_datetime(out.index, errors="coerce"))
    valid = ~pd.isna(idx)
    out = out.loc[valid].copy()
    idx = idx[valid]
    if idx.tz is None:
        idx = idx.tz_localize(EASTERN)
    else:
        idx = idx.tz_convert(EASTERN)
    out.index = idx
    out.index.name = "Datetime"
    return out[~out.index.duplicated(keep="last")].sort_index()


def scan_orb_sessions(
    data: dict[str, pd.DataFrame],
    config: ORBResultsConfig | None = None,
) -> pd.DataFrame:
    """Classify the first post-ORB window for every complete ticker-session."""
    settings = config or ORBResultsConfig()
    if str(settings.interval).lower() != "5m":
        raise ValueError("ORB Results requires 5-minute stock candles")
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
            confirmation_end = orb_end + pd.Timedelta(minutes=int(settings.confirmation_minutes))
            opening = session[(session.index >= session_start) & (session.index < orb_end)]
            confirmation = session[(session.index >= orb_end) & (session.index < confirmation_end)]
            if len(opening) < orb_bars_required or len(confirmation) < confirmation_bars_required:
                continue

            orb_high = float(opening["High"].max())
            orb_low = float(opening["Low"].min())
            orb_close = float(opening.iloc[-1]["Close"])
            confirmation_close = float(confirmation.iloc[-1]["Close"])
            direction = "CALL" if confirmation_close > orb_high else "PUT" if confirmation_close < orb_low else ""
            boundary = orb_high if direction == "CALL" else orb_low if direction == "PUT" else None
            break_distance_pct = (
                abs(confirmation_close - float(boundary)) / float(boundary) * 100.0
                if boundary not in {None, 0}
                else 0.0
            )
            rows.append({
                "session_date": session_date.isoformat(),
                "symbol": symbol,
                "orb_start": session_start,
                "orb_end": orb_end,
                "confirmation_time": confirmation_end,
                "orb_high": round(orb_high, 4),
                "orb_low": round(orb_low, 4),
                "orb_close": round(orb_close, 4),
                "confirmation_close": round(confirmation_close, 4),
                "broke_orb": bool(direction),
                "direction": direction,
                "break_distance_pct": round(break_distance_pct, 4),
                "option_status": "PENDING" if direction else "NOT_APPLICABLE",
                "option_dte": int(settings.option_dte),
                "contract_gain_target_pct": float(settings.contract_gain_pct),
            })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["session_date", "symbol"]).reset_index(drop=True)


def add_option_outcomes(
    sessions: pd.DataFrame,
    option_bars_provider: Callable[..., tuple[dict[str, Any] | None, pd.DataFrame]],
    config: ORBResultsConfig | None = None,
    progress_callback: Callable[[int, int, dict[str, Any]], None] | None = None,
) -> pd.DataFrame:
    """Attach historical contract performance to confirmed ORB breaks."""
    settings = config or ORBResultsConfig()
    if sessions is None or sessions.empty:
        return pd.DataFrame()
    out = sessions.copy()
    for column, default in {
        "option_expiry": None,
        "option_strike": None,
        "option_local_symbol": None,
        "entry_premium": None,
        "max_premium": None,
        "max_contract_gain_pct": None,
        "contract_target_hit": False,
        "target_hit_by_confirmation": False,
        "target_hit_time": None,
        "option_error": None,
    }.items():
        out[column] = default

    break_indexes = list(out.index[out["broke_orb"].fillna(False).astype(bool)])
    total = len(break_indexes)
    for number, index in enumerate(break_indexes, start=1):
        row = out.loc[index]
        if progress_callback:
            progress_callback(number, total, row.to_dict())
        try:
            info, raw_bars = option_bars_provider(
                symbol=str(row["symbol"]),
                signal=str(row["direction"]),
                underlying_price=float(row["orb_close"]),
                option_dte=int(settings.option_dte),
                reference_time=pd.Timestamp(row["orb_end"]),
            )
            bars = raw_bars.copy() if isinstance(raw_bars, pd.DataFrame) else pd.DataFrame()
            if bars.empty:
                raise RuntimeError("No historical contract bars returned")
            idx = pd.DatetimeIndex(pd.to_datetime(bars.index, errors="coerce"))
            valid = ~pd.isna(idx)
            bars = bars.loc[valid].copy()
            idx = idx[valid]
            orb_end = pd.Timestamp(row["orb_end"])
            if idx.tz is None:
                idx = idx.tz_localize(orb_end.tz)
            else:
                idx = idx.tz_convert(orb_end.tz)
            bars.index = idx
            bars = bars[(bars.index.date == orb_end.date()) & (bars.index >= orb_end)].sort_index()
            if bars.empty:
                raise RuntimeError("No historical contract bars at or after 09:45")

            entry_values = pd.to_numeric(bars.get("Open"), errors="coerce") if "Open" in bars.columns else pd.Series(dtype=float)
            if entry_values.empty or not (entry_values > 0).any():
                entry_values = pd.to_numeric(bars.get("Close"), errors="coerce") if "Close" in bars.columns else pd.Series(dtype=float)
            entry_values = entry_values[entry_values > 0]
            if entry_values.empty:
                raise RuntimeError("Historical contract has no positive entry premium")
            entry_index = entry_values.index[0]
            entry_premium = float(entry_values.iloc[0])
            future = bars[bars.index >= entry_index]
            highs = pd.to_numeric(future.get("High"), errors="coerce") if "High" in future.columns else pd.to_numeric(future.get("Close"), errors="coerce")
            highs = highs.dropna()
            if highs.empty:
                raise RuntimeError("Historical contract has no usable high prices")

            target_premium = entry_premium * (1.0 + float(settings.contract_gain_pct) / 100.0)
            hits = highs[highs >= target_premium]
            hit_time = hits.index[0] if not hits.empty else None
            max_premium = float(highs.max())
            max_gain_pct = (max_premium / entry_premium - 1.0) * 100.0
            confirmation_time = pd.Timestamp(row["confirmation_time"])
            option_info = info or {}
            out.at[index, "option_status"] = "AVAILABLE"
            out.at[index, "option_expiry"] = option_info.get("expiry")
            out.at[index, "option_strike"] = option_info.get("strike")
            out.at[index, "option_local_symbol"] = option_info.get("localSymbol")
            out.at[index, "entry_premium"] = round(entry_premium, 4)
            out.at[index, "max_premium"] = round(max_premium, 4)
            out.at[index, "max_contract_gain_pct"] = round(max_gain_pct, 2)
            out.at[index, "contract_target_hit"] = bool(hit_time is not None)
            out.at[index, "target_hit_by_confirmation"] = bool(hit_time is not None and hit_time < confirmation_time)
            out.at[index, "target_hit_time"] = hit_time
        except Exception as exc:
            out.at[index, "option_status"] = "UNAVAILABLE"
            out.at[index, "option_error"] = str(exc).strip() or repr(exc)
    return out


def summarize_orb_results(sessions: pd.DataFrame, contract_gain_pct: float = 20.0) -> pd.DataFrame:
    if sessions is None or sessions.empty:
        return pd.DataFrame()
    target_label = f"+{float(contract_gain_pct):g}%"

    def summarize_group(symbol: str, group: pd.DataFrame) -> dict[str, Any]:
        breaks = group[group["broke_orb"].fillna(False).astype(bool)]
        available = breaks[breaks["option_status"].astype(str) == "AVAILABLE"]
        hits = available[available["contract_target_hit"].fillna(False).astype(bool)]
        call_breaks = breaks[breaks["direction"].astype(str) == "CALL"]
        put_breaks = breaks[breaks["direction"].astype(str) == "PUT"]
        call_available = available[available["direction"].astype(str) == "CALL"]
        put_available = available[available["direction"].astype(str) == "PUT"]
        return {
            "Ticker": symbol,
            "Sessions": len(group),
            "ORB Breaks": len(breaks),
            "ORB Break Rate %": round(len(breaks) / len(group) * 100.0, 1) if len(group) else 0.0,
            "CALL Breaks": len(call_breaks),
            "PUT Breaks": len(put_breaks),
            "Contracts Found": len(available),
            "Contract Coverage %": round(len(available) / len(breaks) * 100.0, 1) if len(breaks) else 0.0,
            f"{target_label} Hits": len(hits),
            f"{target_label} Hit Rate %": round(len(hits) / len(available) * 100.0, 1) if len(available) else 0.0,
            f"CALL {target_label} Rate %": round(call_available["contract_target_hit"].fillna(False).mean() * 100.0, 1) if len(call_available) else 0.0,
            f"PUT {target_label} Rate %": round(put_available["contract_target_hit"].fillna(False).mean() * 100.0, 1) if len(put_available) else 0.0,
            f"{target_label} By 10:00": int(available["target_hit_by_confirmation"].fillna(False).sum()),
            "Median Max Contract Gain %": round(float(pd.to_numeric(available["max_contract_gain_pct"], errors="coerce").median()), 1) if len(available) else 0.0,
        }

    rows = [summarize_group(symbol, group) for symbol, group in sessions.groupby("symbol", sort=True)]
    total = summarize_group("ALL", sessions)
    return pd.DataFrame([total, *rows])


def save_orb_results(
    sessions: pd.DataFrame,
    summary: pd.DataFrame,
    meta: dict[str, Any],
    export_dir: str | Path,
) -> None:
    directory = Path(export_dir)
    directory.mkdir(parents=True, exist_ok=True)
    sessions.to_csv(directory / "orbresults_details.csv", index=False)
    summary.to_csv(directory / "orbresults_summary.csv", index=False)
    with (directory / "orbresults_meta.json").open("w", encoding="utf-8") as file:
        json.dump(meta, file, indent=2, default=str)
