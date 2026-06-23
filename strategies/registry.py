from __future__ import annotations

"""Simple strategy registry for PulseTrade AI.

This is intentionally lightweight. It avoids a complex plug-in system while
letting the live scanner and Strategy Lab call strategies by name.
"""

from importlib import import_module
from typing import Any

AVAILABLE_STRATEGIES = {
    "pmb": "strategies.pmb.strategy",
    "brt": "strategies.brt.strategy",
}

DEFAULT_STRATEGY = "pmb"


def normalize_strategy_name(name: str | None) -> str:
    key = str(name or DEFAULT_STRATEGY).strip().lower()
    return key if key in AVAILABLE_STRATEGIES else DEFAULT_STRATEGY


def get_strategy(name: str | None = None):
    key = normalize_strategy_name(name)
    return import_module(AVAILABLE_STRATEGIES[key])


def scan_dataframe(*, strategy_name: str | None = None, **kwargs: Any) -> dict | None:
    strategy = get_strategy(strategy_name)
    return strategy.scan_dataframe(**kwargs)
