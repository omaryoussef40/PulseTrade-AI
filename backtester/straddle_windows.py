"""Date windows for short straddle scans, using New York trading sessions."""
from datetime import timedelta
import pandas as pd

PERIOD_LABELS={'Yesterday':'yesterday','7 days':'7d','14 days':'14d','1 month':'30d','3 months':'90d','6 months':'180d'}


def lookback_window(period,now=None):
    timestamp=pd.Timestamp.now(tz='America/New_York') if now is None else pd.Timestamp(now)
    if timestamp.tzinfo is None:raise ValueError('Use a timezone-aware timestamp for straddle lookbacks.')
    timestamp=timestamp.tz_convert('America/New_York')
    today=timestamp.date()
    from modules.pine_stock_signals import calendar
    cal=calendar(today.year)
    current=pd.Timestamp(today)
    today_completed=cal.is_session(current) and cal.session_close(current)<=timestamp
    end=today if today_completed else today-timedelta(days=1)
    if period=='yesterday':
        sessions=cal.sessions_in_range(pd.Timestamp(today-timedelta(days=31)),pd.Timestamp(end))
        if sessions.empty:raise ValueError('Previous trading session unavailable.')
        day=sessions[-1].date()
        return f'{max(7,(today-day).days+2)}d',day,day
    if period in ('7d','14d'):
        days=int(period[:-1])
        # Extra fetch days preserve the first full session when provider caches
        # are sliced relative to their latest candle instead of midnight.
        return f'{days+2}d',end-timedelta(days=days-1),end
    return period,None,None


def filter_window(frame,start,end):
    if start is None or frame is None or frame.empty:return frame
    from .orb_results import _normalize_bars
    normalized=_normalize_bars(frame)
    return normalized.loc[(normalized.index.date>=start)&(normalized.index.date<=end)]
