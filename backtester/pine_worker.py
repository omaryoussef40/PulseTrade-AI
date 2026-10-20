"""Read-only IBKR history loading in a process separate from Streamlit."""
from dataclasses import fields
from functools import partial
from pathlib import Path
import json
import os
import sys

import pandas as pd

from market_data import IBKRMarketDataProvider, MarketDataConfig
from modules.pine_levels import ROOT, parse_pine
from modules.pine_stock_signals import Settings, bounds
from .pine import regular_frame, run_backtest, save_result


def load_candles(provider, symbols, start, end, *, force_refresh=False, cache_dir=None, now=None):
    cache_dir = Path(cache_dir) if cache_dir else ROOT/'backtester'/'cache'/'pine_stocks'
    cache_dir.mkdir(parents=True,exist_ok=True)
    now = now if now is not None else pd.Timestamp.now(tz='America/New_York')
    first = pd.Timestamp(start,tz='America/New_York')-pd.Timedelta(days=7)
    last = pd.Timestamp(end,tz='America/New_York')+pd.Timedelta(days=1)
    data, errors = {}, []
    for symbol in symbols:
        parts = []
        if not force_refresh:
            for path in (cache_dir/f'{symbol}_5m.parquet', ROOT/'backtester'/'cache'/'ibkr'/'stocks'/f'{symbol}_5m_rth_raw.parquet'):
                if path.exists():
                    try:
                        parts.append(regular_frame(pd.read_parquet(path),5,now))
                    except (ValueError,KeyError,TypeError,OSError):
                        pass
        cached = pd.concat(parts).sort_index() if parts else pd.DataFrame()
        if not cached.empty:
            cached = cached.loc[~cached.index.duplicated(keep='first')]
        cursor = last
        while cursor > first:
            chunk_start = max(first,cursor-pd.Timedelta(days=7))
            required = []
            for day in pd.date_range(chunk_start.normalize(),cursor.normalize(),inclusive='left'):
                session = bounds(day)
                if session:
                    required.extend(pd.date_range(*session,freq='5min',inclusive='left'))
            missing = force_refresh or cached.empty or any(ts not in cached.index for ts in required)
            if missing:
                try:
                    frame = provider.historical_bars(symbol,duration='7 D',bar_size='5 mins',use_rth=True,
                        end_datetime=cursor.tz_convert('UTC').strftime('%Y%m%d-%H:%M:%S'),timeout_seconds=30)
                    frame = regular_frame(frame,5,now)
                    frame = frame.loc[(frame.index>=chunk_start)&(frame.index<cursor)]
                    # Fresh responses replace these sessions completely.
                    if not cached.empty:
                        cached = cached.loc[(cached.index<chunk_start)|(cached.index>=cursor)]
                    cached = pd.concat([cached,frame]) if not cached.empty else frame
                except Exception as exc:
                    errors.append(dict(symbol=symbol,error=f'History request ending {cursor.date()}: {exc}'))
            cursor = chunk_start
        if not cached.empty:
            cached = cached.sort_index()
            data[symbol] = cached.loc[(cached.index>=first)&(cached.index<last)]
            cached.to_parquet(cache_dir/f'{symbol}_5m.parquet')
    return data, errors


def run_request(payload):
    plans = [parse_pine(source) for source in payload['sources']]
    symbols = sorted({level.symbol for plan in plans for level in plan.levels})
    connection = payload.get('connection',{})
    provider = IBKRMarketDataProvider(MarketDataConfig(host=connection.get('host','127.0.0.1'),
        port=int(connection.get('port',7497)),client_id=int(connection.get('client_id',11))+208+os.getpid(),readonly=True))
    try:
        data, errors = load_candles(provider,symbols,payload['start'],payload['end'],force_refresh=payload.get('force_refresh',False))
    finally:
        provider.disconnect()
    allowed = {field.name for field in fields(Settings)}
    settings = Settings(**{key:value for key,value in payload.get('settings',{}).items() if key in allowed})
    options_provider=None
    try:
        option_kwargs={}
        if settings.instrument=='OPT':
            from providers.ibkr_provider import IBKRMarketDataProvider as ResearchProvider,IBKRProviderConfig
            options_provider=ResearchProvider(IBKRProviderConfig(host=connection.get('host','127.0.0.1'),
                port=int(connection.get('port',7497)),client_id=int(connection.get('client_id',11))+209+os.getpid(),readonly=True))
            option_kwargs=dict(option_bars_provider=partial(options_provider.historical_option_bars,force_refresh=payload.get('force_refresh',False)),
                option_contract_bars_provider=partial(options_provider.historical_option_bars_for_contract,force_refresh=payload.get('force_refresh',False)))
        result = run_backtest(plans,data,settings,starting_capital=float(payload['starting_capital']),start=payload['start'],end=payload['end'],**option_kwargs)
    finally:
        if options_provider is not None:
            options_provider.disconnect()
    if errors:
        result['errors'] = pd.concat([result['errors'],pd.DataFrame(errors)],ignore_index=True)
        result['meta']['status'] = 'COMPLETE_WITH_DATA_ERRORS'
    save_result(result)
    return result['meta']


if __name__ == '__main__':
    try:
        print(json.dumps(dict(ok=True,meta=run_request(json.loads(Path(sys.argv[1]).read_text())))))
    except Exception as exc:
        print(json.dumps(dict(ok=False,error=str(exc))))
        sys.exit(1)
