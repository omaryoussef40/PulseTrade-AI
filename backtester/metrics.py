from __future__ import annotations

"""Small analytics helpers for Strategy Lab results."""

import pandas as pd


def daily_pnl(trades: pd.DataFrame) -> pd.DataFrame:
    if trades is None or trades.empty or "realized_pnl" not in trades.columns:
        return pd.DataFrame(columns=["date", "realized_pnl"])
    df = trades.copy()
    if "date" not in df.columns:
        df["date"] = pd.to_datetime(df.get("exit_time"), errors="coerce").dt.date.astype(str)
    df["realized_pnl"] = pd.to_numeric(df["realized_pnl"], errors="coerce").fillna(0.0)
    return df.groupby("date", as_index=False)["realized_pnl"].sum()


def symbol_pnl(trades: pd.DataFrame) -> pd.DataFrame:
    if trades is None or trades.empty or "symbol" not in trades.columns or "realized_pnl" not in trades.columns:
        return pd.DataFrame(columns=["symbol", "trades", "realized_pnl", "win_rate"])
    df = trades.copy()
    df["realized_pnl"] = pd.to_numeric(df["realized_pnl"], errors="coerce").fillna(0.0)
    out = df.groupby("symbol").agg(
        trades=("symbol", "count"),
        realized_pnl=("realized_pnl", "sum"),
        wins=("realized_pnl", lambda x: int((x > 0).sum())),
    ).reset_index()
    out["win_rate"] = (out["wins"] / out["trades"] * 100).round(1)
    return out.sort_values("realized_pnl", ascending=False)


def monthly_pnl(trades: pd.DataFrame) -> pd.DataFrame:
    if trades is None or trades.empty or "realized_pnl" not in trades.columns:
        return pd.DataFrame(columns=["month", "realized_pnl"])
    df = trades.copy()
    ts = pd.to_datetime(df.get("exit_time", df.get("entry_time")), errors="coerce")
    df = df.loc[~ts.isna()].copy()
    df["month"] = ts.dt.strftime("%Y-%m")
    df["realized_pnl"] = pd.to_numeric(df["realized_pnl"], errors="coerce").fillna(0.0)
    return df.groupby("month", as_index=False)["realized_pnl"].sum()
