from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import hashlib

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from modules.pine_levels import parse_pine, save_plan, load_plan
from modules.pine_stock_signals import Settings, confirmed_signal


SOURCE = (Path(__file__).parent/'fixtures/daily_trade_2026-10-13.pine').read_text()
EXPECTED = {
    'UNH': (398.4,392.6,405.,412.2,390.8,396.1,384.3,377.5),
    'GS': (931.2,922.,942.8,952.5,919.,927.8,909.,898.),
    'MU': (954.,943.,969.5,986.,932.,944.4,915.,898.),
    'WDC': (365.,357.8,373.5,382.8,352.5,360.1,343.,334.),
    'WFC': (82.15,81.05,83.5,84.65,80.5,81.55,79.25,78.1),
    'JNJ': (260.8,258.3,263.75,267.,256.8,259.35,253.8,251.),
    'JPM': (339.7,336.8,343.3,347.,334.75,337.8,331.,327.5),
    'AMD': (587.5,580.5,595.5,603.5,575.5,582.,567.,558.5),
    'NVDA': (226.2,224.15,228.95,231.8,222.75,225.,220.,217.1),
    'C': (133.6,132.35,135.1,136.8,130.65,131.9,129.1,127.4),
}


def test_attached_script_preserves_date_all_levels_and_original_source(tmp_path):
    from modules.pine_levels import FIELDS
    plan = parse_pine(SOURCE)
    assert plan.session_date == date(2026,10,13)
    assert {row.symbol:tuple(getattr(row,field) for field in FIELDS) for row in plan.levels} == EXPECTED
    assert plan.signal_rules.require_volume is False
    assert plan.signal_rules.morning_only is True
    assert plan.source == SOURCE
    assert plan.source_hash == hashlib.sha256(SOURCE.encode()).hexdigest()
    path = tmp_path/'plan.json'
    save_plan(plan,path)
    assert load_plan(path) == plan


def frame(last_volume=10):
    index = pd.date_range('2026-10-13 09:30',periods=2,freq='5min',tz='America/New_York')
    return pd.DataFrame([
        [225.9,226.,225.8,225.9,1000],
        [226.3,226.7,226.2,226.5,last_volume],
    ],columns=['Open','High','Low','Close','Volume'],index=index)


def test_new_format_uses_original_volume_default_and_closed_five_minute_cross():
    plan = parse_pine(SOURCE)
    level = next(row for row in plan.levels if row.symbol=='NVDA')
    now = pd.Timestamp('2026-10-13 09:40:01',tz='America/New_York')
    signal = confirmed_signal(plan,level,frame(),now,Settings())
    assert signal['side'] == 'LONG'
    assert signal['levels'] == level.side('LONG')
    # Enabling the optional filter in the script must require its baseline.
    volume_on = parse_pine(SOURCE.replace('requireVolume = input.bool(false','requireVolume = input.bool(true'))
    with pytest.raises(ValueError,match='Prior 20'):
        confirmed_signal(volume_on,level,frame(),now,Settings())
    with pytest.raises(ValueError,match='valid only for'):
        confirmed_signal(plan,level,frame(),now+pd.Timedelta(days=1),Settings())


@pytest.mark.parametrize('old,new',[
    ('dayKey == 20261013','dayKey == 20260230'),
    ('dayKey == 20261013','true'),
    ('"America/New_York"','"UTC"'),
    ('ta.sma(volume, 20)[1]','ta.sma(volume, 20)'),
    ('volume >= 1.3','volume >= 1.1'),
    ('close > rthVWAP','close >= rthVWAP'),
    ('"0930-1200:1234567"','"0930-1300:1234567"'),
    ('sumPriceVolume := 0.0','sumPriceVolume := 1.0'),
    ('sumPriceVolume += hlc3 * volume','sumPriceVolume += close * volume'),
    ('var float sumPriceVolume','float sumPriceVolume'),
    ('le := 226.20','le := close'),
    ('selectedSymbol := "NVDA"','selectedSymbol := "OTHER"'),
    ('barstate.isconfirmed and sessionOK','sessionOK'),
])
def test_unsupported_changes_are_not_silently_imported(old,new):
    assert old in SOURCE
    with pytest.raises(ValueError):
        parse_pine(SOURCE.replace(old,new))


@pytest.mark.parametrize('extra',[
    'bool targetDate = dayKey == 20261013',
    'targetDate := true',
    'volumeOK := true',
    'longSignal := true',
    'sumPriceVolume += 1',
    'bool correctDate = true',
])
def test_duplicate_date_or_rule_overrides_are_rejected(extra):
    with pytest.raises(ValueError):
        parse_pine(SOURCE+'\n'+extra+'\n')


def test_backtester_upload_accepts_attached_script_without_live_plan_changes():
    upload = SimpleNamespace(name='daily_trade_2026-10-13.pine',getvalue=lambda:SOURCE.encode())
    with patch('backtester.pine_ui.available_plans',return_value=([],[])), \
         patch('backtester.pine_ui.st.file_uploader',return_value=[upload]), \
         patch('backtester.pine_ui.load_result',return_value=None), \
         patch('backtester.pine_ui.execute_backtest',return_value=None) as execute, \
         patch('modules.pine_levels.save_plan') as save:
        app = AppTest.from_string('from backtester.pine_ui import render\nrender({})')
        app.session_state['pine_bt_start'] = date(2026,10,12)
        app.session_state['pine_bt_end'] = date(2026,10,12)
        app.run(timeout=20)
        assert not app.exception and not app.error
        assert app.date_input(key='pine_bt_start').value == date(2026,10,13)
        assert app.date_input(key='pine_bt_end').value == date(2026,10,13)
        assert len(app.multiselect(key='pine_bt_plans').value) == 1
        app.button(key='pine_bt_run').click().run(timeout=20)
        assert not app.exception and not app.error
        assert execute.call_args.args[0]['sources'] == [SOURCE]
        save.assert_not_called()


def test_plan_switch_updates_dates_but_other_reruns_preserve_manual_ranges():
    plans = [parse_pine(SOURCE.replace('dayKey == 20261013','dayKey == 20261012')),parse_pine(SOURCE)]
    with patch('backtester.pine_ui.available_plans',return_value=(plans,[])), \
         patch('backtester.pine_ui.load_result',return_value=None):
        app = AppTest.from_string('from backtester.pine_ui import render\nrender({})').run(timeout=20)
        assert app.date_input(key='pine_bt_start').value == date(2026,10,12)
        assert app.date_input(key='pine_bt_end').value == date(2026,10,13)
        app.multiselect(key='pine_bt_plans').set_value([plans[0].source_hash]).run(timeout=20)
        assert app.date_input(key='pine_bt_end').value == date(2026,10,12)
        app.multiselect(key='pine_bt_plans').set_value([plans[1].source_hash]).run(timeout=20)
        assert app.date_input(key='pine_bt_start').value == date(2026,10,13)
        assert app.date_input(key='pine_bt_end').value == date(2026,10,13)
        app.date_input(key='pine_bt_end').set_value(date(2026,10,14)).run(timeout=20)
        app.number_input(key='pine_bt_capital').set_value(5000.).run(timeout=20)
        assert not app.exception
        assert app.date_input(key='pine_bt_end').value == date(2026,10,14)
