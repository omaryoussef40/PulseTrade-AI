"""Five-minute close decisions; broker emergency protection stays independent."""
from datetime import datetime, timedelta
from math import isfinite


CANDLE_CLOSE_MODE = "five_minute_close"


def candle_boundary(now):
    return now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)


def completed_option_closes(ib, contract, position, now):
    """Fetch once per boundary and never consume an unfinished or pre-entry bar.

    IBKR labels bars with their start. Request a session so a slow scanner or
    restart can recover missed closes. Decisions are executed now, never at a
    hypothetical historical stop fill.
    """
    boundary = candle_boundary(now)
    if position.get("option_candle_checked_through") == boundary.isoformat():
        return []
    entry = datetime.fromisoformat(str(position["entry_time"]))
    if entry.tzinfo is None:
        entry = entry.replace(tzinfo=now.tzinfo)
    last = position.get("last_exit_candle_at")
    last = datetime.fromisoformat(last) if last else entry
    bars = ib.reqHistoricalData(
        contract, endDateTime=boundary, durationStr="1 D",
        barSizeSetting="5 mins", whatToShow="TRADES", useRTH=True,
        formatDate=2, keepUpToDate=False, timeout=10,
    )
    closes = []
    for bar in bars or []:
        start = bar.date
        if not isinstance(start, datetime):
            start = datetime.fromisoformat(str(start))
        if start.tzinfo is None:
            start = start.replace(tzinfo=now.tzinfo)
        end = start + timedelta(minutes=5)
        close = float(bar.close)
        # A zero-volume carried-forward last trade is not a fresh candle close.
        if end <= boundary and end > last and end > entry and end.date() == now.date():
            if isfinite(close) and close > 0 and float(bar.volume or 0) > 0:
                closes.append((end, close))
    if bars:
        position["option_candle_checked_through"] = boundary.isoformat()
    return sorted(closes, key=lambda item: item[0])


def evaluate_close(position, close, timestamp, *, breakeven_trigger_pct,
                   trailing_trigger_pct, trailing_stop_pct):
    """Return an exit reason using the previous level before raising it."""
    entry = float(position["entry_price"])
    stop = float(position.get("current_stop_price") or position["emergency_stop_price"])
    reason = None
    if close <= stop:
        if position.get("trailing_active"):
            reason = "Trailing stop hit at five-minute close"
        elif position.get("breakeven_active"):
            reason = "Breakeven stop hit at five-minute close"
        else:
            reason = "Stop loss hit at five-minute close"
    position["last_exit_candle_at"] = timestamp.isoformat()
    position["last_exit_candle_price"] = round(close, 4)
    if reason:
        return reason
    peak = max(float(position.get("highest_price") or entry), close)
    position["highest_price"] = peak
    gain = (close / entry - 1) * 100
    if gain >= float(breakeven_trigger_pct) - 1e-9:
        position["breakeven_active"] = True
        stop = max(stop, entry)
    if gain >= float(trailing_trigger_pct) - 1e-9:
        position["trailing_active"] = True
    if position.get("trailing_active"):
        stop = max(stop, peak * (1 - float(trailing_stop_pct) / 100))
    position["current_stop_price"] = round(stop, 4)
    return None
