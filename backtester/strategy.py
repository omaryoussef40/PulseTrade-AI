from __future__ import annotations

"""Backward-compatible Strategy Lab strategy interface.

The live scanner and Strategy Lab now use the same strategy implementation under
strategies/. PMB v2 is the only active production strategy.
"""

from zoneinfo import ZoneInfo
import pandas as pd

try:
    from strategies.registry import scan_dataframe as _scan_dataframe
except Exception:  # pragma: no cover
    _scan_dataframe = None

EASTERN = ZoneInfo("America/New_York")
MIN_SCORE = 70


def scan_dataframe(
    symbol: str,
    intraday: pd.DataFrame,
    daily: pd.DataFrame | None = None,
    use_rvol_score: bool = False,
    min_score: float = MIN_SCORE,
    timezone: ZoneInfo = EASTERN,
    strategy_name: str = "pmb",
    orb_minutes: int = 15,
) -> dict | None:
    if _scan_dataframe is None:
        return None
    return _scan_dataframe(
        strategy_name=strategy_name,
        symbol=symbol,
        intraday=intraday,
        daily=daily,
        use_rvol_score=use_rvol_score,
        min_score=min_score,
        timezone=timezone,
        orb_minutes=int(orb_minutes),
    )


def scan_replay_history(
    symbol: str,
    history: pd.DataFrame,
    daily: pd.DataFrame | None = None,
    use_rvol_score: bool = False,
    min_score: float = MIN_SCORE,
    strategy_name: str = "pmb",
    orb_minutes: int = 15,
) -> dict | None:
    return scan_dataframe(
        symbol=symbol,
        intraday=history,
        daily=daily,
        use_rvol_score=use_rvol_score,
        min_score=min_score,
        strategy_name=strategy_name,
        orb_minutes=int(orb_minutes),
    )


def clean_signal_row(result: dict | None) -> dict | None:
    if not result:
        return None
    row = dict(result)
    row.pop("Intraday Data", None)
    return row
