from __future__ import annotations

"""Candle-by-candle market replay engine for the PulseTrade AI backtester.

Drop this file inside:

    TradingBot/backtester/replay.py

This module does not place trades yet. It replays historical candles safely so
future modules can call the scanner exactly as if the market were unfolding live.

Design goal:
- No look-ahead bias. At each timestamp, the strategy only receives candles up
  to that timestamp.
- Multi-symbol replay. Candles from all symbols are merged into one chronological
  event stream.
- Session-aware replay. Events know their trading date and whether they are past
  the ORB confirmation point.
"""

from dataclasses import dataclass
from datetime import date, time as dtime
from typing import Callable, Iterator
from zoneinfo import ZoneInfo

import pandas as pd

EASTERN = ZoneInfo("America/New_York")
REQUIRED_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


@dataclass(frozen=True)
class ReplayConfig:
    interval: str = "5m"
    market_open: dtime = dtime(9, 30)
    market_close: dtime = dtime(16, 0)
    orb_minutes: int = 30
    first_signal_minutes: int = 35
    min_session_bars: int = 7
    timezone: ZoneInfo = EASTERN


@dataclass(frozen=True)
class ReplayEvent:
    symbol: str
    timestamp: pd.Timestamp
    session_date: date
    bar_number: int
    is_new_session: bool
    is_session_close: bool
    past_orb_window: bool
    scanner_allowed: bool
    bar: dict
    history: pd.DataFrame

    @property
    def close(self) -> float:
        return float(self.bar.get("Close", 0.0))


class MarketReplayEngine:
    """Replay historical OHLCV bars in chronological order.

    Example:
        from backtester.data import YahooDataClient
        from backtester.replay import MarketReplayEngine

        data = YahooDataClient().load_many(["SPY", "QQQ"], period="30d")
        engine = MarketReplayEngine(data)

        for event in engine.events():
            if event.scanner_allowed:
                # future step: call scanner with event.history
                print(event.symbol, event.timestamp, event.close)
    """

    def __init__(self, data: dict[str, pd.DataFrame], config: ReplayConfig | None = None):
        self.config = config or ReplayConfig()
        self.data = self._prepare_data(data)
        self.timeline = self._build_timeline()

    def events(
        self,
        symbols: list[str] | None = None,
        start: date | str | None = None,
        end: date | str | None = None,
        only_scanner_allowed: bool = False,
    ) -> Iterator[ReplayEvent]:
        selected = {s.strip().upper() for s in symbols} if symbols else set(self.data.keys())
        start_date = pd.to_datetime(start).date() if start is not None else None
        end_date = pd.to_datetime(end).date() if end is not None else None

        for timestamp, symbol in self.timeline:
            if symbol not in selected:
                continue
            session_date = timestamp.date()
            if start_date and session_date < start_date:
                continue
            if end_date and session_date > end_date:
                continue

            event = self._make_event(symbol, timestamp)
            if only_scanner_allowed and not event.scanner_allowed:
                continue
            yield event

    def run(
        self,
        on_event: Callable[[ReplayEvent], dict | None],
        symbols: list[str] | None = None,
        start: date | str | None = None,
        end: date | str | None = None,
        only_scanner_allowed: bool = True,
    ) -> pd.DataFrame:
        """Run a callback across replay events and collect returned rows.

        The callback receives a ReplayEvent. If it returns a dict, that row is
        appended to the output DataFrame. If it returns None, nothing is stored.
        """
        rows: list[dict] = []
        for event in self.events(
            symbols=symbols,
            start=start,
            end=end,
            only_scanner_allowed=only_scanner_allowed,
        ):
            result = on_event(event)
            if result is None:
                continue
            rows.append({
                "symbol": event.symbol,
                "timestamp": event.timestamp,
                "session_date": event.session_date,
                "bar_number": event.bar_number,
                **result,
            })
        return pd.DataFrame(rows)

    def sessions(self) -> pd.DataFrame:
        rows = []
        for symbol, df in self.data.items():
            for session_date, session in df.groupby(df.index.date):
                rows.append({
                    "symbol": symbol,
                    "session_date": session_date,
                    "bars": int(len(session)),
                    "first_bar": session.index.min(),
                    "last_bar": session.index.max(),
                    "open": float(session.iloc[0]["Open"]),
                    "close": float(session.iloc[-1]["Close"]),
                    "volume": float(session["Volume"].sum()),
                    "valid_for_scanner": bool(len(session) >= self.config.min_session_bars),
                })
        return pd.DataFrame(rows).sort_values(["session_date", "symbol"]) if rows else pd.DataFrame()

    def scanner_ready_history(self, symbol: str, timestamp: pd.Timestamp) -> pd.DataFrame:
        symbol = symbol.strip().upper()
        if symbol not in self.data:
            return pd.DataFrame(columns=REQUIRED_COLUMNS)
        return self.data[symbol][self.data[symbol].index <= timestamp].copy()

    def _prepare_data(self, data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        prepared: dict[str, pd.DataFrame] = {}
        for symbol, df in data.items():
            clean_symbol = str(symbol).strip().upper()
            if not clean_symbol or df is None or df.empty:
                continue
            missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
            if missing:
                raise ValueError(f"{clean_symbol} data is missing columns: {missing}")

            out = df[REQUIRED_COLUMNS].copy()
            idx = pd.to_datetime(out.index, errors="coerce")
            valid = ~pd.isna(idx)
            out = out.loc[valid].copy()
            idx = idx[valid]
            if getattr(idx, "tz", None) is None:
                idx = idx.tz_localize(self.config.timezone)
            else:
                idx = idx.tz_convert(self.config.timezone)
            out.index = idx
            out.index.name = "Datetime"
            out = out.between_time(self.config.market_open, self.config.market_close, inclusive="both")
            out = out[~out.index.duplicated(keep="last")].sort_index()
            if not out.empty:
                prepared[clean_symbol] = out
        return prepared

    def _build_timeline(self) -> list[tuple[pd.Timestamp, str]]:
        rows: list[tuple[pd.Timestamp, str]] = []
        for symbol, df in self.data.items():
            rows.extend((ts, symbol) for ts in df.index)
        rows.sort(key=lambda item: (item[0], item[1]))
        return rows

    def _make_event(self, symbol: str, timestamp: pd.Timestamp) -> ReplayEvent:
        df = self.data[symbol]
        history = df[df.index <= timestamp].copy()
        session = df[df.index.date == timestamp.date()]
        session_so_far = session[session.index <= timestamp]
        bar_number = int(len(session_so_far))
        bar = df.loc[timestamp].to_dict()

        is_new_session = bar_number == 1
        is_session_close = bool(timestamp == session.index.max()) if not session.empty else False
        minutes_from_open = self._minutes_from_open(timestamp)
        past_orb_window = minutes_from_open >= self.config.orb_minutes
        scanner_allowed = (
            bar_number >= self.config.min_session_bars
            and minutes_from_open >= self.config.first_signal_minutes
            and not history.empty
        )

        return ReplayEvent(
            symbol=symbol,
            timestamp=timestamp,
            session_date=timestamp.date(),
            bar_number=bar_number,
            is_new_session=is_new_session,
            is_session_close=is_session_close,
            past_orb_window=past_orb_window,
            scanner_allowed=scanner_allowed,
            bar=bar,
            history=history,
        )

    def _minutes_from_open(self, timestamp: pd.Timestamp) -> int:
        open_dt = timestamp.replace(
            hour=self.config.market_open.hour,
            minute=self.config.market_open.minute,
            second=0,
            microsecond=0,
        )
        return int((timestamp - open_dt).total_seconds() // 60)


def make_replay_log(engine: MarketReplayEngine, limit: int = 500) -> pd.DataFrame:
    """Return a lightweight event log useful for testing the replay engine."""
    rows = []
    for i, event in enumerate(engine.events()):
        if i >= limit:
            break
        rows.append({
            "symbol": event.symbol,
            "timestamp": event.timestamp,
            "session_date": event.session_date,
            "bar_number": event.bar_number,
            "close": event.close,
            "scanner_allowed": event.scanner_allowed,
            "is_new_session": event.is_new_session,
            "is_session_close": event.is_session_close,
        })
    return pd.DataFrame(rows)
