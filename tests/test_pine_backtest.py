from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from modules.pine_levels import parse_pine, save_plan, load_plan
from modules.pine_stock_signals import Settings, confirmed_signal
from backtester.pine import available_plans, run_backtest, save_result, load_result
from backtester.pine_worker import load_candles


SOURCE = '''//@version=5
indicator("Replay test", overlay=true)
activeDay = year(time, "America/New_York") == 2026 and month(time, "America/New_York") == 10 and dayofmonth(time, "America/New_York") == 12
if syminfo.ticker == "TEST"
    longEntry := 100
    longStop := 99
    longT1 := 102
    longT2 := 104
    shortEntry := 98
    shortStop := 99
    shortT1 := 96
    shortT2 := 94
vwapLine = ta.vwap(hlc3)
longCross = ta.crossover(close, longEntry)
shortCross = ta.crossunder(close, shortEntry)
longSignal = activeDay and timeframe.isintraday and barstate.isconfirmed and not na(longEntry) and longCross and close > vwapLine
shortSignal = activeDay and timeframe.isintraday and barstate.isconfirmed and not na(shortEntry) and shortCross and close < vwapLine
'''
PLAN = parse_pine(SOURCE)
NOW = pd.Timestamp('2026-10-14 12:00',tz='America/New_York')
CFG = Settings(max_entry_slippage_pct=0.,max_daily_loss=1000.)


def session(day, price=100.6):
    index = pd.date_range(day+' 09:30',periods=78,freq='5min',tz='America/New_York')
    return pd.DataFrame(dict(Open=price,High=price+.2,Low=price-.2,Close=price,Volume=1000.),index=index)


def candles():
    prior = session('2026-10-09',99.5)
    today = session('2026-10-12')
    today.iloc[0] = [99.5,99.7,99.3,99.5,1000]
    # Target was touched in the signal candle, before any entry could fill.
    today.iloc[1] = [100.,102.1,100.,100.5,2000]
    return pd.concat([prior,today])


def replay(frame=None, settings=CFG, **kwargs):
    return run_backtest([PLAN],{'TEST':candles() if frame is None else frame},settings,now=kwargs.pop('now',NOW),**kwargs)


def test_next_open_fill_ignores_signal_candle_target_and_keeps_position_open():
    result = replay()
    assert result['errors'].empty
    trade = result['trades'].iloc[0]
    assert trade.signal_time == '2026-10-12T09:40:00-04:00'
    assert trade.entry_time == trade.signal_time
    assert trade.entry_price == 100.6
    assert trade.status == 'OPEN'
    assert list(result['fills'].reason) == ['ENTRY']
    assert result['metrics']['closed_trades'] == 0
    assert result['metrics']['open_positions'] == 1
    assert result['metrics']['net_pnl'] == -2
    assert result['coverage'].plan_available.tolist() == [True]


def test_partial_targets_and_commissions_reconcile():
    raw = candles()
    raw.loc['2026-10-12 09:45',['High','Low','Close']] = [102.2,100.5,102.]
    raw.loc['2026-10-12 09:50',['High','Low','Close']] = [104.2,100.5,104.]
    result = replay(raw)
    trade = result['trades'].iloc[0]
    assert trade.quantity == 49
    assert trade.remaining == 0
    assert trade.status == 'CLOSED'
    assert result['fills'].reason.tolist() == ['ENTRY','T1','T2']
    assert result['fills'].quantity.tolist() == [49,24,25]
    expected = (102-100.6)*24+(104-100.6)*25-4
    assert trade.realized_pnl == pytest.approx(expected)
    assert result['metrics']['ending_equity'] == pytest.approx(10000+expected)
    assert result['fills'].realized_pnl.sum() == pytest.approx(expected)


@pytest.mark.parametrize('risk',[10.,100.])
def test_backtest_risk_input_cannot_move_pine_targets_or_stop(risk):
    raw=candles()
    raw.loc['2026-10-12 09:45',['High','Low','Close']]=[102.2,100.5,102.]
    raw.loc['2026-10-12 09:50',['High','Low','Close']]=[104.2,100.5,104.]
    result=replay(raw,settings=replace(CFG,risk_dollars=risk))
    trade=result['trades'].iloc[0]
    assert trade.quantity==int(min(risk/(trade.entry_price-99),5000/trade.entry_price))
    assert result['fills'].price.tolist()[1:]==[102.,104.]
    raw.loc['2026-10-12 09:45',['High','Low','Close']]=[105.,98.,100.6]
    stopped=replay(raw,settings=replace(CFG,risk_dollars=risk))
    assert stopped['fills'].price.tolist()[1:]==[99.,99.]


def test_ambiguous_exit_bar_uses_stop_before_targets():
    raw = candles()
    raw.loc['2026-10-12 09:45',['High','Low','Close']] = [105.,98.,100.6]
    result = replay(raw)
    assert result['fills'].reason.tolist() == ['ENTRY','STOP','STOP']
    assert result['trades'].iloc[0].realized_pnl == pytest.approx((99-100.6)*49-4)


def test_opening_stop_gap_uses_adverse_open_instead_of_stop_level():
    raw = candles()
    raw.loc['2026-10-12 09:45'] = [97.,98.,96.,97.,1000]
    result = replay(raw)
    assert result['fills'].reason.tolist() == ['ENTRY','STOP_GAP','STOP_GAP']
    assert result['fills'].price.tolist()[1:] == [97.,97.]


def test_next_open_back_inside_trigger_is_rejected():
    raw = candles()
    raw.loc['2026-10-12 09:40'] = [99.8,100.8,99.5,100.6,1000]
    result = replay(raw)
    assert result['trades'].empty
    assert 'inside the trigger' in result['decisions'].iloc[0].reason


def test_slippage_and_single_share_target():
    result = replay(settings=replace(CFG,max_position_dollars=102,max_entry_slippage_pct=.1))
    trade = result['trades'].iloc[0]
    assert trade.quantity == 1
    assert trade.entry_price == 100.71
    assert trade.commissions == 1


def test_missing_session_bars_disable_entries_and_report_coverage():
    raw = candles().drop(pd.Timestamp('2026-10-12 11:00',tz='America/New_York'))
    result = replay(raw)
    assert result['trades'].empty
    assert result['meta']['status'] == 'COMPLETE_WITH_DATA_ERRORS'
    assert 'incomplete' in result['errors'].iloc[0].error


def test_missing_history_blocks_first_candle_cross():
    raw = session('2026-10-12')
    raw.iloc[0] = [100.,101.,99.,100.5,1000]
    result = replay(raw)
    assert result['trades'].empty
    assert 'Previous session' in result['decisions'].iloc[0].reason


def test_exit_on_day_without_plan_retains_original_levels():
    raw = pd.concat([candles(),session('2026-10-13')])
    raw.loc['2026-10-13 09:30'] = [105.,106.,104.5,105.,1000]
    result = replay(raw,end='2026-10-13')
    assert result['coverage'].plan_available.tolist() == [True,False]
    assert len(result['trades']) == 1
    assert result['fills'].reason.tolist() == ['ENTRY','T1','T2']
    assert result['trades'].iloc[0].exit_time == '2026-10-13T09:30:00-04:00'


def test_missing_carry_session_does_not_invent_exit_at_later_target():
    raw = pd.concat([candles(),session('2026-10-13')])
    raw = raw.drop(pd.Timestamp('2026-10-13 09:30',tz='America/New_York'))
    raw.loc['2026-10-13 09:35'] = [105.,106.,104.5,105.,1000]
    result = replay(raw,end='2026-10-13')
    assert result['trades'].iloc[0].status == 'UNRESOLVED_DATA_GAP'
    assert result['fills'].reason.tolist() == ['ENTRY']


def test_entire_missing_carry_day_blocks_a_later_exit():
    raw = pd.concat([candles(),session('2026-10-14',150.)])
    result = replay(raw,end='2026-10-14',now=pd.Timestamp('2026-10-15',tz='America/New_York'))
    assert result['trades'].iloc[0].status == 'UNRESOLVED_DATA_GAP'
    assert result['fills'].reason.tolist() == ['ENTRY']


def test_conflicting_plans_and_forged_values_are_rejected():
    other = parse_pine(SOURCE.replace('longT2 := 104','longT2 := 105'))
    with pytest.raises(ValueError,match='Multiple different plans'):
        run_backtest([PLAN,other],{},now=NOW)
    with pytest.raises(ValueError,match='differ'):
        run_backtest([replace(PLAN,source_hash='wrong')],{},now=NOW)


def test_future_or_incomplete_end_session_is_rejected():
    with pytest.raises(ValueError,match='completed'):
        replay(end='2026-10-14',now=NOW)


def test_short_permissions_and_target_direction():
    raw = pd.concat([session('2026-10-09',98.5),session('2026-10-12',97.4)])
    raw.loc['2026-10-12 09:30'] = [98.5,98.7,98.3,98.5,1000]
    raw.loc['2026-10-12 09:35'] = [98.,98.,95.9,97.5,2000]
    assert replay(raw)['trades'].empty
    raw.loc['2026-10-12 09:45'] = [97.4,97.6,93.9,94.5,1000]
    result = replay(raw,settings=replace(CFG,enable_shorts=True))
    assert result['trades'].iloc[0].side == 'SHORT'
    assert result['fills'].reason.tolist() == ['ENTRY','T1','T2']
    assert result['metrics']['net_pnl'] > 0


@pytest.mark.parametrize('settings,expected',[
    (replace(CFG,max_positions=1),1),
    (replace(CFG,max_daily_trades=1),1),
    (replace(CFG,max_daily_capital=5000.),1),
    (replace(CFG,trade_all_plan_symbols=False,allowed_symbols=('TEST',)),1),
    (replace(CFG,kill_switch=True),0),
])
def test_portfolio_limits_are_shared_across_symbols(settings,expected):
    block = SOURCE[SOURCE.index('if syminfo.ticker'):SOURCE.index('vwapLine')]
    plan = parse_pine(SOURCE.replace('vwapLine =',block.replace('"TEST"','"OTHER"')+'vwapLine =',1))
    result = run_backtest([plan],{'TEST':candles(),'OTHER':candles()},settings,now=NOW)
    assert len(result['trades']) == expected


def test_existing_orora_rules_are_used_by_replay():
    source = (Path(__file__).parent/'fixtures/orora_daily_study_2026-10-12.pine').read_text()
    plan = parse_pine(source)
    raw = pd.concat([session('2026-10-09',239.5),session('2026-10-12',240.6)])
    raw.loc['2026-10-12 09:30'] = [239.5,239.7,239.3,239.5,1000]
    raw.loc['2026-10-12 09:35'] = [240.,240.7,240.,240.5,1299]
    data = {'CRM':raw}
    with patch('backtester.pine.confirmed_signal',wraps=confirmed_signal) as scan:
        result = run_backtest([plan],data,CFG,now=NOW)
    assert result['trades'].empty
    assert scan.call_args.args[0].signal_rules == plan.signal_rules
    raw.loc['2026-10-12 09:35','Volume'] = 1300
    result = run_backtest([plan],data,CFG,now=NOW)
    assert result['trades'].symbol.tolist() == ['CRM']


def test_future_bars_are_not_used_for_an_earlier_test_end():
    raw = pd.concat([candles(),session('2026-10-13',150.)])
    original = replay()
    later = replay(raw,end='2026-10-12')
    assert later['metrics'] == original['metrics']


def test_plan_archive_retains_previous_and_new_source(tmp_path):
    path = tmp_path/'daily_stock_levels.json'
    save_plan(PLAN,path)
    second = parse_pine(SOURCE.replace('== 12','== 13'))
    save_plan(second,path)
    plans, errors = available_plans(path)
    assert not errors
    assert {plan.source_hash for plan in plans} == {PLAN.source_hash,second.source_hash}
    assert load_plan(path) == second


def test_results_round_trip_and_isolation_from_option_exports(tmp_path):
    sentinel = tmp_path/'strategy_lab_trades.csv'
    sentinel.write_text('unchanged')
    result = replay()
    save_result(result,tmp_path/'pine_stocks')
    loaded = load_result(tmp_path/'pine_stocks')
    assert loaded['metrics'] == result['metrics']
    assert len(loaded['trades']) == 1
    assert loaded['errors'].empty
    assert sentinel.read_text() == 'unchanged'


def test_loader_uses_explicit_historical_end_regular_hours_and_warmup(tmp_path):
    provider = Mock()
    provider.historical_bars.return_value = candles()
    data, errors = load_candles(provider,['TEST'],'2026-10-12','2026-10-12',cache_dir=tmp_path,now=NOW)
    assert not errors
    assert data['TEST'].index.min().date().isoformat() == '2026-10-09'
    for call in provider.historical_bars.call_args_list:
        assert call.kwargs['use_rth']
        assert call.kwargs['bar_size'] == '5 mins'
        assert '-' in call.kwargs['end_datetime']
        assert call.kwargs['duration'] == '7 D'


def test_worker_uses_readonly_connection_and_never_retains_live_execution():
    from backtester.pine_worker import run_request
    payload = dict(sources=[SOURCE],start='2026-10-12',end='2026-10-12',starting_capital=10000,
        settings={'execution':'IBKR Live'},connection={'port':4002})
    with patch('backtester.pine_worker.IBKRMarketDataProvider') as provider, \
         patch('backtester.pine_worker.load_candles',return_value=({'TEST':candles()},[])), \
         patch('backtester.pine_worker.save_result') as save:
        meta = run_request(payload)
    assert provider.call_args.args[0].readonly is True
    assert provider.call_args.args[0].port == 4002
    provider.return_value.disconnect.assert_called_once()
    assert meta['settings']['execution'] == 'Simulation'
    assert save.call_args.args[0]['meta']['strategy'] == 'Daily Pine Levels — stocks'


def test_ui_runs_selected_saved_plan_without_saving_live_configuration():
    result = replay()
    with patch('backtester.pine_ui.available_plans',return_value=([PLAN],[])), \
         patch('backtester.pine_ui.load_result',return_value=None), \
         patch('backtester.pine_ui.execute_backtest',return_value=result) as execute, \
         patch('bot_core.save_config') as save:
        app = AppTest.from_string('from backtester.pine_ui import render\nrender({"ib":{"host":"testhost","paper_port":4002}})').run(timeout=20)
        assert not app.exception
        execute.assert_not_called()
        app.button(key='pine_bt_run').click().run(timeout=20)
        assert not app.exception
        payload = execute.call_args.args[0]
        assert payload['sources'] == [SOURCE]
        assert payload['settings']['execution'] == 'Simulation'
        assert payload['connection']['port'] == 4002
        assert payload['connection']['host'] == 'testhost'
        save.assert_not_called()
