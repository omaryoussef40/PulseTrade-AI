"""Read-only chart snapshots, isolated from Streamlit's event loop."""
import json
import sys
import os
from math import isfinite
import pandas as pd
from market_data import IBKRMarketDataProvider, MarketDataConfig
from .pine_stock_signals import aware, bounds


def fetch_snapshot(payload, now=None):
    day = pd.Timestamp(payload['day']).date()
    now = aware(now) if now is not None else pd.Timestamp.now(tz='America/New_York')
    session = bounds(pd.Timestamp(day, tz='America/New_York'))
    result = dict(day=str(day), fetched_at=now.isoformat(), source='IBKR', candles={}, errors={})
    symbols = sorted(set(payload['symbols']))
    if not session:
        result['errors'] = {symbol:'No U.S. trading session on this date.' for symbol in symbols}
        return result
    opening, closing = session
    extended_start = opening.normalize() + pd.Timedelta(hours=4)
    extended_end = opening.normalize() + pd.Timedelta(hours=20)
    if now <= extended_start:
        result['errors'] = {symbol:'This session has not opened yet. Select a completed session to review charts.' for symbol in symbols}
        return result
    end = min(now, extended_end)
    provider = IBKRMarketDataProvider(MarketDataConfig(
        host=payload.get('host','127.0.0.1'), port=int(payload.get('port',7497)),
        client_id=int(payload.get('client_id',11))+108+os.getpid(), readonly=True,
    ))
    try:
        provider.connect()
        for symbol in symbols:
            try:
                frame = provider.historical_bars(symbol, duration='2 D', bar_size='5 mins', use_rth=False,
                    # TWS rejects some IANA zone names (error 10314). Its
                    # UTC dash format works across operator/instrument zones.
                    end_datetime=end.tz_convert('UTC').strftime('%Y%m%d-%H:%M:%S'), timeout_seconds=5)
                if frame.empty:
                    raise ValueError('No completed five-minute candles available for this session.')
                if frame.index.tz is None:
                    raise ValueError('Timestamped candles unavailable.')
                frame.index = frame.index.tz_convert('America/New_York')
                frame = frame.loc[(frame.index>=extended_start) & (frame.index+pd.Timedelta(minutes=5)<=end)]
                if frame.empty:
                    raise ValueError('No completed five-minute candles available for this session.')
                if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
                    raise ValueError('Duplicate or unsorted candles.')
                if not all(isfinite(float(v)) for v in frame[['Open','High','Low','Close','Volume']].to_numpy().flat):
                    raise ValueError('Invalid chart data.')
                if (frame[['Open','High','Low','Close']]<=0).any().any() or (frame.Volume<0).any():
                    raise ValueError('Invalid chart prices or volume.')
                if (frame.High<frame[['Open','Close','Low']].max(axis=1)).any() or (frame.Low>frame[['Open','Close','High']].min(axis=1)).any():
                    raise ValueError('Invalid candle ranges.')
                result['candles'][symbol] = [dict(timestamp=ts.isoformat(), **{k:float(row[k]) for k in ('Open','High','Low','Close','Volume')}) for ts,row in frame.iterrows()]
            except Exception as exc:
                result['errors'][symbol] = str(exc)
    except Exception as exc:
        result['errors'] = {symbol:f'IBKR chart connection unavailable: {exc}' for symbol in symbols}
    finally:
        provider.disconnect()
    return result


if __name__ == '__main__':
    print(json.dumps(fetch_snapshot(json.loads(sys.argv[1])), allow_nan=False))
