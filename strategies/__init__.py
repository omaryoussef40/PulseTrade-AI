"""Strategy modules for PulseTrade AI.

Keep this layer simple: the engine asks for one active strategy, and the
strategy returns the standard scanner result dictionary.
"""

from .registry import AVAILABLE_STRATEGIES, get_strategy, scan_dataframe

__all__ = ["AVAILABLE_STRATEGIES", "get_strategy", "scan_dataframe"]
