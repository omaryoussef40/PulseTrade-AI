from dataclasses import replace
from copy import deepcopy
from unittest.mock import patch

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from backtester.straddle_results import StraddleResultsConfig,scan_straddle_sessions,simulate_straddles,_winner_exit,_timed_exit_price
from test_straddle_results import _stock_session,_option_bars


def provider(**kwargs):
    info=dict(expiry='20260911',strike=100.)
    return info,_option_bars([4.,5.2,7.],[4.,7.,8.]),info,_option_bars([4.,2.8,2.5],[4.,2.5,2.])


def test_shorter_confirmation_changes_actual_decision_time():
    cfg=StraddleResultsConfig(confirmation_minutes=5)
    row=scan_straddle_sessions({'TEST':_stock_session('CALL')},cfg).iloc[0]
    assert row.confirmation_time.strftime('%H:%M')=='09:50'
    assert row.direction=='CALL' and row.confirmation_close==101.2


def test_buffer_volume_and_direction_filters_change_leg_selection():
    source=_stock_session('CALL')
    assert scan_straddle_sessions({'TEST':source}).iloc[0].direction=='CALL'
    for cfg in [StraddleResultsConfig(breakout_buffer_pct=1.),StraddleResultsConfig(allowed_direction='PUT'),StraddleResultsConfig(require_volume=True,min_confirmation_volume_ratio=1.5)]:
        assert scan_straddle_sessions({'TEST':source},cfg).iloc[0].direction==''
    source.loc[source.index[3:],'Volume']=2000
    result=scan_straddle_sessions({'TEST':source},StraddleResultsConfig(require_volume=True,min_confirmation_volume_ratio=1.5)).iloc[0]
    assert result.direction=='CALL' and result.confirmation_volume_ratio==2


def test_vwap_filter_uses_only_completed_regular_session_bars():
    source=_stock_session('CALL')
    # An earlier confirmation spike puts VWAP above its final close.
    source.loc[source.index[3],'High']=200
    cfg=StraddleResultsConfig(require_vwap=True)
    row=scan_straddle_sessions({'TEST':source},cfg).iloc[0]
    assert row.direction=='' and row.decision_reason=='VWAP filter not met'
    assert row.confirmation_vwap>row.confirmation_close
    # New future data cannot change confirmation VWAP.
    future=source.iloc[[-1]].copy();future.index+=pd.Timedelta(minutes=5);future['Volume']=1000000
    other=scan_straddle_sessions({'TEST':pd.concat([source,future])},cfg).iloc[0]
    assert other.confirmation_vwap==row.confirmation_vwap


def test_entry_time_changes_underlying_and_missing_entry_is_not_invented():
    source=_stock_session('CALL')
    source.loc[source.index[1],'Open']=100.3
    row=scan_straddle_sessions({'TEST':source},StraddleResultsConfig(entry_minute=35)).iloc[0]
    assert row.entry_time.strftime('%H:%M')=='09:35' and row.entry_underlying==100.3
    assert scan_straddle_sessions({'TEST':source.iloc[1:]}).empty


def test_quantity_scales_both_premiums_and_all_commissions():
    sessions=scan_straddle_sessions({'TEST':_stock_session('CALL')})
    cfg=StraddleResultsConfig(winner_target_pct=300,trailing_enabled=False,slippage_pct=0)
    one=simulate_straddles(sessions,provider,cfg).iloc[0]
    three=simulate_straddles(sessions,provider,replace(cfg,contracts_per_leg=3)).iloc[0]
    assert three.entry_cost==pytest.approx(one.entry_cost*3)
    assert three.realized_pnl==pytest.approx(one.realized_pnl*3)
    assert three.combined_return_pct==one.combined_return_pct
    assert three.contracts_per_leg==3


def test_no_break_can_hold_both_legs_to_timed_exit():
    cfg=StraddleResultsConfig(no_break_action='Hold both to timed exit',slippage_pct=0,commission_per_contract=0)
    sessions=scan_straddle_sessions({'TEST':_stock_session('NONE')},cfg)
    row=simulate_straddles(sessions,provider,cfg).iloc[0]
    assert row.call_exit==8 and row.put_exit==2
    assert row.exit_reason=='No ORB break; both legs held to timed exit'


def test_target_and_trailing_can_be_disabled_independently():
    bars=_option_bars([4,5.2,7],[4,7,8]);at=bars.index[1]
    cfg=StraddleResultsConfig(winner_target_pct=30,trailing_enabled=False)
    assert _winner_exit(bars,at,4,cfg)[2]=='Winner target'
    assert _winner_exit(bars,at,4,replace(cfg,winner_target_enabled=False))[2]=='End-of-day exit'


def test_breakeven_and_stop_gaps_use_observed_prices():
    bars=_option_bars([4,5.2,4.1],[4,4.1,3.8]);at=bars.index[1]
    cfg=StraddleResultsConfig(winner_target_enabled=False,trailing_enabled=False,breakeven_enabled=True,breakeven_trigger_pct=20)
    result=_winner_exit(bars,at,4,cfg)
    assert result[1]==3.8 and result[2]=='Winner breakeven stop'
    bars=_option_bars([4,2,2],[4,2,2])
    result=_winner_exit(bars,at,4,replace(cfg,breakeven_enabled=False))
    assert result[1]==2 and result[2]=='Winner stop loss'


def test_timed_exit_never_uses_cutoff_candles_future_close():
    bars=_option_bars([4,5.2,7],[4,7,99]);cutoff=bars.index[-1]
    assert _timed_exit_price(bars,cutoff)==7
    cfg=StraddleResultsConfig(force_exit_hour=10,force_exit_minute=5,winner_target_enabled=False,trailing_enabled=False)
    assert _winner_exit(bars,bars.index[1],4,cfg)[1]==7


@pytest.mark.parametrize('changes',[dict(confirmation_minutes=7),dict(entry_hour=10,entry_minute=0),dict(force_exit_hour=9,force_exit_minute=55),dict(contracts_per_leg=0),dict(breakout_buffer_pct=-1),dict(trailing_stop_pct=101)])
def test_invalid_rules_are_rejected(changes):
    with pytest.raises(ValueError):StraddleResultsConfig(**changes)


def test_straddle_controls_save_explicitly_without_connecting():
    script='''
import streamlit as st
import backtester.ui as ui
from unittest.mock import patch
if 'config' not in st.session_state:st.session_state['config']={'straddle_results':{'symbols':['AAPL'],'winner_target_pct':30},'strategy':{'min_score':90},'risk':{'max_daily_capital_pct':80}}
def save(config):st.session_state['saved']=config
with patch.object(ui,'save_config',save),patch('bot_core.load_config',lambda:dict(st.session_state['config'])):
    ui._render_straddle_backtest_view(st.session_state['config'],['AAPL'])
'''
    with patch('backtester.ui.create_market_data_provider') as market:
        app=AppTest.from_string(script).run(timeout=20)
        assert not app.exception
        before=deepcopy(app.session_state['config'])
        app.selectbox(key='straddle_confirmation').set_value(5).run()
        app.checkbox(key='straddle_target_enabled').uncheck().run()
        app.number_input(key='straddle_quantity').set_value(3).run()
        assert app.session_state['config']==before
        app.button(key='straddle_save').click().run()
        assert not app.exception
        saved=app.session_state['saved']
        assert saved['straddle_results']['confirmation_minutes']==5
        assert saved['straddle_results']['contracts_per_leg']==3
        assert not saved['straddle_results']['winner_target_enabled']
        assert saved['strategy']==before['strategy'] and saved['risk']==before['risk']
        market.assert_not_called()


def test_missing_prices_are_not_silently_filled_at_a_later_entry_or_confirmation():
    cfg=StraddleResultsConfig(entry_minute=35)
    sessions=scan_straddle_sessions({'TEST':_stock_session('CALL')},cfg)
    result=simulate_straddles(sessions,provider,cfg).iloc[0]
    assert result.option_status=='UNAVAILABLE' and 'entry time' in result.option_error
    cfg=StraddleResultsConfig(confirmation_minutes=5)
    sessions=scan_straddle_sessions({'TEST':_stock_session('CALL')},cfg)
    result=simulate_straddles(sessions,provider,cfg).iloc[0]
    assert result.option_status=='UNAVAILABLE' and 'confirmation' in result.option_error
