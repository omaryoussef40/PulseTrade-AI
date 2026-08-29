from __future__ import annotations

"""Paced IBKR extended-hours candle loading for GAP research."""

from time import sleep
from typing import Any, Callable

import pandas as pd


def load_ibkr_gap_candles(
    provider: Any,
    symbols: list[str],
    *,
    period: str,
    interval: str,
    request_delay_seconds: float = 0.25,
    max_retries: int = 2,
    force_refresh: bool = True,
    progress_callback=None,
    sleep_fn: Callable[[float], None] = sleep,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Load one extended-hours IBKR series per symbol with bounded retries."""
    if provider is None or not callable(getattr(provider, "load", None)):
        raise RuntimeError("An IBKR market-data provider is required for GAP candles")

    provider_name = str(getattr(provider, "name", "")).strip().upper()
    if provider_name and provider_name != "IBKR":
        raise RuntimeError(f"GAP candles require IBKR; received {provider_name}")

    clean_symbols = list(dict.fromkeys(
        str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()
    ))
    total = max(len(clean_symbols), 1)
    delay = max(0.0, float(request_delay_seconds))
    attempts_allowed = max(1, int(max_retries) + 1)
    data: dict[str, pd.DataFrame] = {}
    errors: list[dict[str, Any]] = []

    for index, symbol in enumerate(clean_symbols, start=1):
        last_error = "IBKR returned no extended-hours candles"
        attempts_used = 0
        for attempt in range(1, attempts_allowed + 1):
            attempts_used = attempt
            if progress_callback:
                progress_callback(
                    index,
                    total,
                    {
                        "stage": "Loading IBKR GAP candles",
                        "symbol": symbol,
                        "timestamp": "",
                        "attempt": attempt,
                    },
                    len(data),
                )
            try:
                frame = provider.load(
                    symbol=symbol,
                    period=str(period),
                    interval=str(interval),
                    regular_hours_only=False,
                    force_refresh=bool(force_refresh),
                )
                if isinstance(frame, pd.DataFrame) and not frame.empty:
                    data[symbol] = frame
                    last_error = ""
                    break
                last_error = "IBKR returned no extended-hours candles"
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__

            if attempt < attempts_allowed:
                sleep_fn(delay * attempt)

        if symbol not in data:
            errors.append({
                "symbol": symbol,
                "error": last_error,
                "attempts": attempts_used,
            })
        if index < len(clean_symbols):
            sleep_fn(delay)

    return data, pd.DataFrame(errors)
