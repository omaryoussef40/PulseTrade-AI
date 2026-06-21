"""Backtester package for PulseTrade AI.

Phase 1 modules:
- data.py: Yahoo/yfinance historical data download + local cache
- replay.py: candle-by-candle historical market replay
"""

from .data import YahooDataClient, YahooDataRequest, load_symbol_data
from .replay import ReplayConfig, ReplayEvent, MarketReplayEngine

__all__ = [
    "YahooDataClient",
    "YahooDataRequest",
    "load_symbol_data",
    "ReplayConfig",
    "ReplayEvent",
    "MarketReplayEngine",
]

from .controller import StrategyLabController, StrategyLabSettings

__all__ += ["StrategyLabController", "StrategyLabSettings"]
