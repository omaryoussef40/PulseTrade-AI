from datetime import date
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest
from backtester.straddle_windows import lookback_window,filter_window

@pytest.mark.parametrize('now,expected',[
    ('2026-10-13 12:00',date(2026,10,12)),
    ('2026-10-12 18:00',date(2026,10,12)),
    ('2026-10-12 12:00',date(2026,10,9)),
    ('2026-10-11 12:00',date(2026,10,9)),
    ('2026-12-28 12:00',date(2026,12,24)),
])
def test_yesterday_skips_weekends_and_exchange_holidays(now,expected):
    request,start,end=lookback_window('yesterday',pd.Timestamp(now,tz='America/New_York'))
    assert start==end==expected
    assert request.endswith('d') and int(request[:-1])>=7

@pytest.mark.parametrize('period,days',[('7d',7),('14d',14)])
def test_short_period_uses_exact_calendar_dates_and_excludes_today(period,days):
    request,start,end=lookback_window(period,pd.Timestamp('2026-10-13 12:00',tz='America/New_York'))
    assert start==date(2026,10,13)-pd.Timedelta(days=days) and end==date(2026,10,12)
    assert int(request[:-1])>days
    index=pd.date_range('2026-09-25 09:30',periods=25,freq='D',tz='America/New_York')
    frame=pd.DataFrame(dict(Open=100,High=101,Low=99,Close=100,Volume=1000),index=index)
    filtered=filter_window(frame,start,end)
    assert filtered.index.min().date()==start and filtered.index.max().date()==end


def test_dubai_clock_is_converted_to_new_york_date():
    _,start,end=lookback_window('yesterday',pd.Timestamp('2026-10-13 02:00',tz='Asia/Dubai'))
    assert start==end==date(2026,10,12)


def test_long_lookbacks_keep_existing_fetch_behavior():
    assert lookback_window('30d',pd.Timestamp('2026-10-13',tz='America/New_York'))==('30d',None,None)


def test_ui_offers_and_saves_all_short_periods_without_connecting():
    script='''
import streamlit as st
from unittest.mock import patch
import backtester.ui as ui
if 'config' not in st.session_state:st.session_state['config']={'straddle_results':{'symbols':['AAPL']}}
with patch.object(ui,'save_config',lambda config:None):
    ui._render_straddle_backtest_view(st.session_state['config'],['AAPL'])
'''
    app=AppTest.from_string(script).run(timeout=20)
    for label,period in [('Yesterday','yesterday'),('7 days','7d'),('14 days','14d')]:
        app.get('button_group')[0].set_value(label).run()
        app.button(key='straddle_save').click().run()
        assert not app.exception
        assert app.session_state['config']['straddle_results']['period']==period
