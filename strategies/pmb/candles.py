"""Completed-candle preparation shared by PMB's live scanner and replay."""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo

import pandas as pd


EASTERN = ZoneInfo('America/New_York')
NORMAL_ANALYSIS_MINUTES = 15
OPENING_ANALYSIS_MINUTES = 5
OHLCV = {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}


def candle_minutes(interval: str) -> int:
    match = re.fullmatch(r'(\d+)\s*(m|min|mins|minute|minutes|h|hour|hours)', str(interval).strip().lower())
    if not match:
        raise ValueError(f'Unsupported intraday candle interval: {interval}')
    return max(1, int(match[1])) * (60 if match[2] in {'h', 'hour', 'hours'} else 1)


def completed_live_bars(
    frame: pd.DataFrame,
    bar_size: str,
    analysis_bar_minutes: int | None = None,
    now: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Keep start-labeled bars whose source and analysis intervals have closed."""
    if frame is None or frame.empty:
        return pd.DataFrame() if frame is None else frame.copy()
    current = pd.Timestamp.now(tz=EASTERN) if now is None else pd.Timestamp(now)
    current = current.tz_localize(EASTERN) if current.tzinfo is None else current.tz_convert(EASTERN)
    index = pd.DatetimeIndex(pd.to_datetime(frame.index, errors='coerce'))
    index = index.tz_localize(EASTERN) if index.tz is None else index.tz_convert(EASTERN)
    source_minutes = candle_minutes(bar_size)
    mask = index.notna() & (index + pd.Timedelta(minutes=source_minutes) <= current)
    if analysis_bar_minutes and int(analysis_bar_minutes) > source_minutes:
        bucket_end = index.floor(f'{int(analysis_bar_minutes)}min') + pd.Timedelta(minutes=int(analysis_bar_minutes))
        mask &= bucket_end <= current
    result = frame.loc[mask].copy()
    result.index = index[mask]
    return result


# Keep the legacy choice when loading a configuration saved before modes existed.
CONFIRMATION_MODES = {
    'orb_5': '5-minute candle after the ORB',
    'orb_15': '15-minute candle after the ORB',
    'breakout_5': '15-minute breakout, then 5-minute confirmation',
    'legacy_next': 'Legacy: next candle after the breakout',
}


def confirmation_mode(config: dict | None) -> str:
    mode = (config or {}).get('confirmation_mode', 'legacy_next')
    if mode not in CONFIRMATION_MODES:
        raise ValueError('Unknown breakout confirmation mode.')
    return mode


def confirmation_analysis_minutes(config: dict | None, fallback: int = 15) -> int:
    if not follow_through_enabled(config):
        return fallback
    mode = confirmation_mode(config)
    return 5 if mode == 'orb_5' else 15 if mode in {'orb_15', 'breakout_5'} else fallback


def direct_confirmation(config: dict | None) -> bool:
    return follow_through_enabled(config) and confirmation_mode(config) in {'orb_5', 'orb_15'}


def explicit_confirmation(config: dict | None) -> bool:
    return 'confirmation_minutes' in (config or {}) or 'confirmation_mode' in (config or {})


def confirmation_minutes(config: dict | None, analysis_minutes: int = 15) -> int:
    mode = confirmation_mode(config)
    value = 5 if mode in {'orb_5', 'breakout_5'} else 15 if mode == 'orb_15' else int((config or {}).get('confirmation_minutes', analysis_minutes))
    if value not in {5, 15}:
        raise ValueError('Confirmation candle must be 5 or 15 minutes.')
    return value


def follow_through_enabled(config: dict | None) -> bool:
    cfg = config or {}
    return bool(cfg.get('follow_through_enabled', cfg.get('enabled', False)))


def confirmation_scan_interval_seconds(requested_seconds: int, config: dict | None) -> int:
    """Poll frequently enough to observe the selected confirmation window."""
    requested = max(10, int(requested_seconds))
    if follow_through_enabled(config):
        return min(requested, confirmation_minutes(config) * 60)
    return requested


def prepare_live_analysis(
    raw: pd.DataFrame,
    source_interval: str,
    analysis_minutes: int,
    now: pd.Timestamp,
    *,
    keep_source_bars: bool = False,
) -> pd.DataFrame:
    """Prepare complete analysis bins while retaining live start labels."""
    source_minutes = candle_minutes(source_interval)
    if source_minutes > analysis_minutes or analysis_minutes % source_minutes:
        raise ValueError(f'PMB {analysis_minutes}-minute analysis requires a compatible finer historical candle interval.')
    raw = completed_live_bars(raw, source_interval, analysis_minutes, now)
    if raw.empty or source_minutes == analysis_minutes:
        return raw
    grouped = raw.resample(f'{analysis_minutes}min', label='left', closed='left')
    complete = grouped['Close'].count() == analysis_minutes // source_minutes
    if keep_source_bars:
        complete_bins = complete[complete].index
        return raw[raw.index.floor(f'{analysis_minutes}min').isin(complete_bins)].copy()
    return grouped.agg(OHLCV).loc[complete].dropna(subset=['Open', 'High', 'Low', 'Close'])


def prepare_replay_inputs(
    history: pd.DataFrame,
    source_interval: str,
    analysis_minutes: int,
    *,
    keep_source_bars: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return closed analysis candles and independently closed source candles."""
    if history is None or history.empty:
        empty = pd.DataFrame() if history is None else history.copy()
        return empty, empty.copy()
    now = pd.Timestamp(history.index[-1])
    raw = history.copy()
    raw.index = pd.DatetimeIndex(raw.index) - pd.Timedelta(minutes=candle_minutes(source_interval))
    analysis = prepare_live_analysis(raw, source_interval, analysis_minutes, now, keep_source_bars=keep_source_bars)
    source = completed_live_bars(raw, source_interval, None, now)
    return analysis, source


def prepare_replay_analysis(
    history: pd.DataFrame,
    source_interval: str,
    analysis_minutes: int,
    *,
    keep_source_bars: bool = False,
) -> pd.DataFrame:
    """Convert replay close labels to complete live start-labeled analysis."""
    return prepare_replay_inputs(history, source_interval, analysis_minutes, keep_source_bars=keep_source_bars)[0]
