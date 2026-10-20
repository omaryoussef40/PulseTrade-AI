from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from providers.ibkr_provider import IBKRMarketDataProvider
from market_data import create_market_data_provider
from test_ibkr_provider_cache import _bars,_five_minute_bars
from test_straddle_results import _stock_session,_option_bars

REF=pd.Timestamp('2026-09-01 09:30',tz='America/New_York')


def info(right='C'):
    return dict(symbol='AAPL',option_dte=7,expiry='20260911',strike=100.,right=right,conId=123 if right=='C' else 456,localSymbol=right)


def fake_broker():
    broker=Mock()
    broker.RequestTimeout=45
    broker.qualifyContracts.side_effect=lambda contract:[SimpleNamespace(localSymbol='OPTION',conId=123)]
    broker.reqSecDefOptParams.return_value=[SimpleNamespace(exchange='SMART',expirations=['20260911'],strikes=[100.])]
    broker.reqHistoricalData.return_value=['fresh']
    return broker


def test_force_refresh_bypasses_call_prices_and_cached_failures(tmp_path):
    provider=IBKRMarketDataProvider(cache_dir=tmp_path)
    old=_five_minute_bars('2026-09-01',1)
    path=provider._contract_option_cache_path(info(),REF,'5 mins','TRADES',True)
    provider._write_option_cache(path,info(),old)
    failure=provider._option_failure_cache_path('AAPL','CALL',100.,7,REF,'5 mins','TRADES',True)
    provider._write_option_failure_cache(failure,'Old failed request')
    fresh=old.copy();fresh['Close']+=1
    broker=fake_broker()
    with patch.object(provider,'connect',return_value=broker),patch.object(provider,'qualify_stock',return_value=SimpleNamespace(secType='STK',conId=1)),patch.object(provider,'_bars_to_ohlcv',return_value=fresh):
        _,result=provider.historical_option_bars('AAPL','CALL',100.,7,REF,force_refresh=True)
    assert result.Close.iloc[0]==fresh.Close.iloc[0]
    broker.reqHistoricalData.assert_called_once()
    assert not failure.exists()
    cached_info,cached=provider._read_option_cache(path)
    pd.testing.assert_frame_equal(cached,fresh,check_names=False)


def test_force_refresh_bypasses_exact_put_cache(tmp_path):
    provider=IBKRMarketDataProvider(cache_dir=tmp_path)
    old=_five_minute_bars('2026-09-01',1)
    path=provider._contract_option_cache_path(info('P'),REF,'5 mins','TRADES',True)
    provider._write_option_cache(path,info('P'),old)
    fresh=old.copy();fresh['Close']+=2
    broker=fake_broker()
    with patch.object(provider,'connect',return_value=broker),patch.object(provider,'_bars_to_ohlcv',return_value=fresh):
        _,result=provider.historical_option_bars_for_contract('AAPL','PUT','20260911',100.,7,REF,force_refresh=True)
    pd.testing.assert_frame_equal(result,fresh)
    broker.reqHistoricalData.assert_called_once()


@pytest.mark.parametrize('empty',[False,True])
def test_strict_stock_refresh_never_silently_falls_back_to_old_data(tmp_path,empty):
    provider=IBKRMarketDataProvider(cache_dir=tmp_path)
    provider._write_cache(_bars('2026-01-01','2026-01-10'),provider._stock_cache_path('AAPL','5m',True))
    kwargs={'return_value':pd.DataFrame()} if empty else {'side_effect':RuntimeError('IBKR request failed')}
    with patch.object(provider,'_cache_covers_period',return_value=True),patch.object(provider,'historical_bars',**kwargs):
        with pytest.raises(RuntimeError):provider.load('AAPL',period='7d',interval='5m',force_refresh=True,allow_cache_fallback=False)
        # Default PMB fallback behavior stays intact.
        result=provider.load('AAPL',period='7d',interval='5m',force_refresh=True)
        assert not result.empty


def test_strict_refresh_does_not_hide_partial_new_sessions_with_old_afternoon(tmp_path):
    provider=IBKRMarketDataProvider(cache_dir=tmp_path)
    old=_five_minute_bars('2026-09-01',1)
    provider._write_cache(old,provider._stock_cache_path('AAPL','5m',True))
    fresh=old.iloc[:3]
    with patch.object(provider,'historical_bars',return_value=fresh),patch.object(provider,'_cache_is_current',return_value=True):
        result=provider.load('AAPL',period='7d',interval='5m',force_refresh=True,allow_cache_fallback=False)
    assert len(result)==3 and result.index.max()==fresh.index.max()


def test_factory_namespaces_are_separate_and_clear_does_not_touch_pmb(tmp_path):
    pmb=create_market_data_provider('IBKR',{},cache_dir=tmp_path/'pmb')
    straddle=create_market_data_provider('IBKR',{},cache_dir=tmp_path/'straddle')
    for provider in (pmb,straddle):
        provider._write_cache(_bars('2026-01-01','2026-01-02'),provider._stock_cache_path('AAPL','5m',True))
    straddle.clear_cache('AAPL')
    assert pmb._stock_cache_path('AAPL','5m',True).exists()
    assert not straddle._stock_cache_path('AAPL','5m',True).exists()
    assert pmb.option_cache_dir!=straddle.option_cache_dir
    with patch('providers.ibkr_provider.IBKRMarketDataProvider') as constructor:
        create_market_data_provider('IBKR',{})
        assert 'cache_dir' not in constructor.call_args.kwargs


def test_straddle_ui_refreshes_stock_and_both_options_in_dedicated_cache():
    calls=[]
    def load(symbol,**kwargs):calls.append(('stock',kwargs));return _stock_session('CALL')
    def call(**kwargs):calls.append(('call',kwargs));return info(),_option_bars([4,5,6],[4,5,6])
    def put(**kwargs):calls.append(('put',kwargs));return info('P'),_option_bars([4,3,2],[4,3,2])
    provider=SimpleNamespace(load=load,historical_option_bars=call,historical_option_bars_for_contract=put,disconnect=lambda:None)
    script='''
import streamlit as st
import backtester.ui as ui
if 'config' not in st.session_state:st.session_state['config']={'straddle_results':{'symbols':['AAPL'],'period':'30d'}}
ui._render_straddle_backtest_view(st.session_state['config'],['AAPL'])
'''
    with patch('backtester.ui.create_market_data_provider',return_value=provider) as factory,patch('backtester.ui.save_config'),patch('backtester.ui.save_straddle_results') as export:
        app=AppTest.from_string(script).run(timeout=20)
        assert app.checkbox(key='straddle_force_refresh').label=='Refresh stock and option data'
        app.checkbox(key='straddle_force_refresh').check().run()
        factory.assert_not_called()
        app.button(key='straddle_run').click().run(timeout=20)
        assert not app.exception
        assert factory.call_args.kwargs['cache_dir']==Path(__file__).parents[1]/'backtester/cache/straddle/ibkr'
        assert factory.call_args.kwargs['readonly_override'] is True
        assert {kind for kind,_ in calls}=={'stock','call','put'}
        assert all(kwargs['force_refresh'] for _,kwargs in calls)
        assert calls[0][1]['allow_cache_fallback'] is False
        meta=export.call_args.args[2]
        assert meta['cache_namespace']=='straddle' and meta['force_refresh_stock_and_options'] is True


def test_clear_button_only_uses_the_straddle_namespace():
    provider=Mock()
    provider.clear_cache.return_value=3
    script='''
import backtester.ui as ui
ui._render_straddle_backtest_view({'straddle_results':{'symbols':['AAPL']}},['AAPL'])
'''
    with patch('backtester.ui.create_market_data_provider',return_value=provider) as factory:
        app=AppTest.from_string(script).run(timeout=20)
        factory.assert_not_called()
        app.button(key='straddle_clear_cache').click().run()
        assert not app.exception
        provider.clear_cache.assert_called_once_with('AAPL')
        provider.disconnect.assert_called_once()
        assert factory.call_args.kwargs['cache_dir']==Path(__file__).parents[1]/'backtester/cache/straddle/ibkr'
        provider.load.assert_not_called()
        provider.historical_option_bars.assert_not_called()
