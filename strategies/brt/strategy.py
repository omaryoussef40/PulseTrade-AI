from __future__ import annotations

"""BRT — Break & Retest Strategy placeholder.

This file is intentionally simple for now. BRT is not enabled yet. Keeping this
module in place lets the project structure stay clean without changing live
behavior before the strategy rules are fully defined and tested.
"""

import pandas as pd


def scan_dataframe(
    symbol: str,
    intraday: pd.DataFrame,
    daily: pd.DataFrame | None = None,
    use_rvol_score: bool = False,
    min_score: float = 70,
    **kwargs,
) -> dict | None:
    return None


def scan_replay_history(
    symbol: str,
    history: pd.DataFrame,
    daily: pd.DataFrame | None = None,
    use_rvol_score: bool = False,
    min_score: float = 70,
) -> dict | None:
    return scan_dataframe(
        symbol=symbol,
        intraday=history,
        daily=daily,
        use_rvol_score=use_rvol_score,
        min_score=min_score,
    )
