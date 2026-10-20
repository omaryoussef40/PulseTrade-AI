import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from providers.ibkr_provider import IBKRMarketDataProvider


def chain(trading_class, strike):
    return SimpleNamespace(exchange='SMART', tradingClass=trading_class,
        multiplier='100', strikes={strike}, expirations={'20990116'})


def test_amd_standard_chain_wins_over_adjusted_smart_chain(tmp_path):
    ib = Mock(RequestTimeout=0)
    ib.reqSecDefOptParams.return_value = [chain('2AMD', 443), chain('AMD', 587.5)]
    ib.qualifyContracts.side_effect = lambda contract: [contract]
    provider = IBKRMarketDataProvider(ib=ib, cache_dir=tmp_path)
    frame = pd.DataFrame({'Open': [1.], 'High': [2.], 'Low': [1.], 'Close': [2.], 'Volume': [10.]},
        index=pd.date_range('2099-01-05 09:55', periods=1, tz='America/New_York'))
    with patch.object(provider, 'qualify_stock', return_value=SimpleNamespace(secType='STK', conId=1)), \
            patch.object(provider, '_bars_to_ohlcv', return_value=frame):
        info, bars = provider.historical_option_bars('AMD', 'CALL', 588.52, 7, frame.index[0], force_refresh=True)
    requested = ib.reqHistoricalData.call_args.args[0]
    assert requested.tradingClass == 'AMD'
    assert requested.strike == 587.5
    assert info['tradingClass'] == 'AMD'
    assert not bars.empty


def test_adjusted_only_chain_is_rejected_before_requesting_history(tmp_path):
    ib = Mock(RequestTimeout=0)
    ib.reqSecDefOptParams.return_value = [chain('2AMD', 443)]
    provider = IBKRMarketDataProvider(ib=ib, cache_dir=tmp_path)
    with patch.object(provider, 'qualify_stock', return_value=SimpleNamespace(secType='STK', conId=1)):
        with pytest.raises(RuntimeError, match='adjusted option classes are excluded'):
            provider.historical_option_bars('AMD', 'CALL', 588.52, 7, pd.Timestamp('2099-01-05', tz='America/New_York'))
    ib.reqHistoricalData.assert_not_called()


def test_empty_history_identifies_the_contract_instead_of_generic_missing_candles(tmp_path):
    ib = Mock(RequestTimeout=0)
    ib.reqSecDefOptParams.return_value = [chain('AMD', 587.5)]
    ib.qualifyContracts.side_effect = lambda contract: [contract]
    provider = IBKRMarketDataProvider(ib=ib, cache_dir=tmp_path)
    with patch.object(provider, 'qualify_stock', return_value=SimpleNamespace(secType='STK', conId=1)), \
            patch.object(provider, '_bars_to_ohlcv', return_value=pd.DataFrame()):
        with pytest.raises(RuntimeError, match='no TRADES option candles for AMD C 20990116 587.5'):
            provider.historical_option_bars('AMD', 'CALL', 588.52, 7, pd.Timestamp('2099-01-05', tz='America/New_York'))


def test_previous_adjusted_cache_is_not_reused_for_standard_option(tmp_path):
    provider = IBKRMarketDataProvider(cache_dir=tmp_path)
    reference = pd.Timestamp('2099-01-05 09:55', tz='America/New_York')
    frame = pd.DataFrame({'Open': [1.], 'High': [2.], 'Low': [1.], 'Close': [2.], 'Volume': [10.]}, index=[reference])
    for root, strike in [('2AMD', 587.5), ('AMD', 590.)]:
        info = dict(symbol='AMD', option_dte=7, expiry='20990116', strike=strike,
            right='C', localSymbol=f'{root:<6}990116C00590000')
        path = provider._contract_option_cache_path(info, reference, '5 mins', 'TRADES', True)
        provider._write_option_cache(path, info, frame)
    with patch.object(provider, 'connect', side_effect=AssertionError('network request made')):
        info, bars = provider.historical_option_bars('AMD', 'CALL', 588.52, 7, reference)
    assert info['strike'] == 590.
    assert not bars.empty


def test_dashboard_local_health_check_does_not_open_incomplete_api_connection():
    source = (Path(__file__).parents[1] / 'dashboard.py').read_text()
    node = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == 'cached_socket_ping')
    node.decorator_list = []
    subprocess = Mock()
    subprocess.run.return_value.returncode = 0
    socket = Mock()
    namespace = {'subprocess': subprocess, 'socket': socket}
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'dashboard.py', 'exec'), namespace)
    assert namespace['cached_socket_ping']('127.0.0.1', 7496, .4)
    socket.create_connection.assert_not_called()
    assert '-iTCP:7496' in subprocess.run.call_args.args[0]
    subprocess.run.return_value.returncode = 1
    assert not namespace['cached_socket_ping']('localhost', 7496, .4)
