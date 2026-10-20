"""Import the supplied Orora format without dropping its signal conditions."""
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from modules.pine_levels import parse_pine, save_plan, load_plan
from modules.pine_stock_signals import Settings, confirmed_signal

SOURCE=(Path(__file__).parent/'fixtures/orora_daily_study_2026-10-12.pine').read_text()
PLAN=parse_pine(SOURCE)
LEVEL=next(row for row in PLAN.levels if row.symbol=='CRM')


def candles(end='2026-10-12 09:35',last_volume=1300,short=False):
    # Twenty prior regular-session bars plus today's full session to the decision.
    index=pd.date_range('2026-10-09 14:20',periods=20,freq='5min',tz='America/New_York').append(
        pd.date_range('2026-10-12 09:30',end,freq='5min',tz='America/New_York'))
    closes=[235.5 if short else 239.5]*len(index)
    closes[-1]=234.5 if short else 240.5
    frame=pd.DataFrame(dict(Open=closes,High=[v+.2 for v in closes],Low=[v-.2 for v in closes],Close=closes,Volume=1000.),index=index)
    frame.loc[index[-1],'Volume']=last_volume
    return frame


def signal(frame,plan=PLAN,**settings):
    now=frame.index[-1]+pd.Timedelta(minutes=5,seconds=1)
    return confirmed_signal(plan,LEVEL,frame,now,Settings(**settings))


def test_supplied_ten_tickers_date_and_saved_rules(tmp_path):
    assert PLAN.session_date==date(2026,10,12)
    assert {r.symbol for r in PLAN.levels}=={'CRM','IREN','MU','TMUS','URBN','HUM','CCI','AMD','NVDA','XOM'}
    assert LEVEL.longEntry==239.75 and LEVEL.shortT2==226.7
    assert PLAN.source==SOURCE
    assert PLAN.signal_rules.require_volume and PLAN.signal_rules.morning_only
    path=tmp_path/'plan.json'
    save_plan(PLAN,path)
    assert load_plan(path)==PLAN
    import json
    stored=json.loads(path.read_text());stored['signal_rules']['require_volume']=False
    path.write_text(json.dumps(stored))
    with pytest.raises(ValueError,match='differ from their source'):load_plan(path)


@pytest.mark.parametrize('old,new',[
    ('20261012','20260230'),
    ('nyDayKey == 20261012','true'),
    ('America/New_York','UTC'),
    ('ta.sma(volume, 20)[1]','ta.sma(volume, 20)'),
    ('volume >= 1.3','volume >= 1.1'),
    ('close > rthVWAP','close >= rthVWAP'),
    ('0930-1200:1234567','0930-1300:1234567'),
    ('runningPV := 0.0','runningPV := 1.0'),
    ('var float runningPV','float runningPV'),
    ('runningPV += hlc3 * volume','runningPV += close * volume'),
    ('le := 239.75','le := close'),
    ('symbolName := "CRM"','symbolName := "OTHER"'),
])
def test_different_rules_are_rejected(old,new):
    assert old in SOURCE
    with pytest.raises(ValueError):parse_pine(SOURCE.replace(old,new))


@pytest.mark.parametrize('line',[
    'bool correctDate = nyDayKey == 20261012',
    'correctDate := true',
    'volumeOK := true',
    'runningPV += 1',
    'longSignal := true',
    'le := 400',
    'le += 1',
    '    bool volumeOK = true',
])
def test_duplicate_and_override_rules_are_rejected(line):
    with pytest.raises(ValueError):parse_pine(SOURCE+'\n'+line+'\n')


@pytest.mark.parametrize('volume,expected',[(1299,False),(1300,True),(1400,True)])
def test_prior_twenty_volume_excludes_current_candle(volume,expected):
    assert bool(signal(candles(last_volume=volume))) is expected


def test_early_signal_uses_previous_session_volume_and_close():
    first=candles(end='2026-10-12 09:30')
    first.loc[first.index[-1],'Low']=239.0
    assert signal(first)['side']=='LONG'
    with pytest.raises(ValueError,match='Prior 20'):
        signal(candles().iloc[2:])
    # Insufficient baseline even if all today's session bars are present.
    with pytest.raises(ValueError,match='Prior 20'):
        signal(candles().iloc[3:])


def test_short_uses_same_volume_rules_and_explicit_permission():
    assert signal(candles(short=True)) is None
    assert signal(candles(short=True),enable_shorts=True)['side']=='SHORT'
    assert signal(candles(short=True,last_volume=1299),enable_shorts=True) is None


@pytest.mark.parametrize('start,expected',[('11:55',True),('12:00',False),('12:05',False)])
def test_morning_session_uses_candle_start_time(start,expected):
    assert bool(signal(candles(end='2026-10-12 '+start))) is expected


def test_input_defaults_and_app_limits_both_apply():
    plan=parse_pine(SOURCE.replace('input.bool(true, "Require','input.bool(false, "Require').replace('input.bool(true, "Only','input.bool(false, "Only'))
    assert not plan.signal_rules.require_volume and not plan.signal_rules.morning_only
    assert signal(candles(end='2026-10-12 12:00',last_volume=500),plan)['side']=='LONG'
    assert signal(candles(end='2026-10-12 12:00',last_volume=500),plan,entry_end='11:00') is None
    now=pd.Timestamp('2026-10-13 09:40:01',tz='America/New_York')
    with pytest.raises(ValueError,match='valid only'):
        confirmed_signal(plan,LEVEL,candles(),now,Settings())


def test_missing_and_invalid_baseline_volume_block_entry():
    frame=candles()
    for bad in (float('nan'),-1,float('inf')):
        broken=frame.copy();broken.iloc[3,broken.columns.get_loc('Volume')]=bad
        with pytest.raises(ValueError,match='Prior 20'):signal(broken)


def test_incomplete_future_candle_does_not_change_baseline():
    frame=candles()
    future=frame.iloc[[-1]].copy();future.index+=pd.Timedelta(minutes=5);future['Volume']=100000
    now=frame.index[-1]+pd.Timedelta(minutes=5,seconds=1)
    result=confirmed_signal(PLAN,LEVEL,pd.concat([frame,future]),now,Settings())
    assert result['close']==240.5


def test_pasted_study_ui_and_all_ten_chart_previews():
    from unittest.mock import patch
    from streamlit.testing.v1 import AppTest
    snapshot=dict(source='IBKR',fetched_at='2026-10-12T09:40:01-04:00',candles={},errors={})
    with patch('modules.pine_stock_ui.load_plan',return_value=None), patch('modules.pine_stock_charts.chart_snapshot',return_value=snapshot):
        app=AppTest.from_string('from modules.pine_stock_ui import render\nrender({})').run(timeout=20)
        app.radio[0].set_value('Paste & settings').run()
        app.text_area[0].set_value(SOURCE).run()
        assert not app.exception and not app.error
        assert any('10 stocks' in item.value and '2026-10-12' in item.value for item in app.markdown)
        assert any('1.3×' in item.value and '09:30–12:00' in item.value for item in app.caption)
        save_button=next(button for button in app.button if button.label=='Save daily stock levels and settings')
        assert not save_button.disabled
        app.radio[0].set_value('Charts').run()
        assert not app.exception and len(app.get('plotly_chart'))==10
        assert sorted(item.value for item in app.subheader)==sorted(row.symbol for row in PLAN.levels)
