from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import hashlib
import re

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from modules.pine_levels import parse_pine, save_plan, load_plan, FIELDS
from modules.pine_stock_signals import Settings, confirmed_signal


SOURCE = (Path(__file__).parent/'fixtures/daily_trade_2026-10-14.pine').read_text()
EXPECTED = {
    'BAC': (51.25,50.4,52.4,53.65,49.7,50.55,48.6,47.45),
    'STT': (162.25,159.7,165.35,168.55,157.5,160.,154.2,150.8),
    'PGR': (212.8,209.9,216.5,220.4,207.8,210.6,204.2,200.1),
    'HPE': (66.85,65.65,68.4,70.,64.9,66.15,63.3,61.8),
    'DELL': (561.5,551.5,574.,586.,544.5,553.5,533.,521.),
    'MS': (185.2,182.9,188.1,191.2,180.7,183.1,177.55,174.3),
    'BLK': (1107.,1092.,1126.,1146.,1085.,1100.,1066.,1047.),
    'MU': (943.,934.,954.,966.5,926.5,935.,915.,901.),
    'NVDA': (229.2,226.95,232.,235.1,223.8,226.,220.9,217.7),
    'XOM': (175.6,174.35,177.25,179.,172.95,174.35,171.1,169.2),
}


def test_oct14_preserves_all_prices_date_rules_and_source(tmp_path):
    plan = parse_pine(SOURCE)
    assert plan.session_date == date(2026,10,14)
    assert {row.symbol:tuple(getattr(row,field) for field in FIELDS) for row in plan.levels} == EXPECTED
    assert plan.signal_rules.morning_start == '09:40'
    assert plan.signal_rules.morning_end == '12:00'
    assert plan.signal_rules.morning_only and not plan.signal_rules.require_volume
    assert plan.source == SOURCE
    assert plan.source_hash == hashlib.sha256(SOURCE.encode()).hexdigest()
    path=tmp_path/'plan.json'
    save_plan(plan,path)
    assert load_plan(path) == plan


@pytest.mark.parametrize('names',[
    {'tradeDay':'chosenTradingSession'},
    {'nyDay':'localDateNumber', 'tradeDay':'isTradingDate', 'tz':'exchangeZone'},
])
def test_date_variable_names_do_not_determine_whether_script_imports(names):
    code=re.sub(r'"(?:\\.|[^"\\])*"|\b[A-Za-z_]\w*\b',lambda m:names.get(m.group(),m.group()),SOURCE)
    plan=parse_pine(code)
    assert plan.session_date == date(2026,10,14)
    assert plan.signal_rules.morning_start == '09:40'


def test_exact_morning_start_is_enforced_on_confirmed_five_minute_candles():
    plan=parse_pine(SOURCE)
    level=next(row for row in plan.levels if row.symbol=='BAC')
    for start, permitted in [('09:35', False), ('09:40', True)]:
        index=pd.date_range('2026-10-14 09:30',f'2026-10-14 {start}',freq='5min',tz='America/New_York')
        closes=[51.]*(len(index)-1)+[51.5]
        frame=pd.DataFrame(dict(Open=closes,High=[v+.1 for v in closes],Low=[v-.1 for v in closes],Close=closes,Volume=1000.),index=index)
        signal=confirmed_signal(plan,level,frame,index[-1]+pd.Timedelta(minutes=5,seconds=1),Settings())
        assert bool(signal) == permitted


@pytest.mark.parametrize('old,new',[
    ('nyDay == 20261014','true'),
    ('nyDay == 20261014','nyDay == 20260230'),
    ('"America/New_York"','"UTC"'),
    ('isIntraday = timeframe.isintraday','isIntraday = true'),
    ('accumPV += hlc3 * nz(volume, 0)','accumPV += close * nz(volume, 0)'),
    ('accumVol += nz(volume, 0)','accumVol += nz(volume, 1)'),
    ('"0940-1200:1234567"','"0940-1300:1234567"'),
    ('barstate.isconfirmed and signalTime','signalTime'),
    ('close > sessionVWAP','close >= sessionVWAP'),
    ('ta.sma(volume, 20)[1]','ta.sma(volume, 20)'),
])
def test_unsupported_rules_are_rejected_instead_of_assumed(old,new):
    assert old in SOURCE
    with pytest.raises(ValueError):parse_pine(SOURCE.replace(old,new))


@pytest.mark.parametrize('extra',[
    'tradeDay := true', 'isIntraday := true', 'volumeOK := true',
    'bool anotherDate = nyDay == 20261015', 'accumPV += 1',
])
def test_overrides_and_ambiguous_dates_are_rejected(extra):
    with pytest.raises(ValueError):parse_pine(SOURCE+'\n'+extra+'\n')


def test_upload_archives_oct14_and_selects_correct_backtest_date():
    upload=SimpleNamespace(name='daily_trade_2026-10-14.pine',getvalue=lambda:SOURCE.encode())
    with patch('backtester.pine_ui.available_plans',return_value=([],[])), \
            patch('backtester.pine_ui.st.file_uploader',return_value=[upload]), \
            patch('backtester.pine_ui.load_result',return_value=None), \
            patch('modules.pine_levels.save_plan') as live_save:
        app=AppTest.from_string('from backtester.pine_ui import render\nrender({})').run(timeout=20)
        assert not app.exception and not app.error
        assert app.date_input(key='pine_bt_start').value == date(2026,10,14)
        assert app.date_input(key='pine_bt_end').value == date(2026,10,14)
        from backtester.pine_store import uploaded_plans
        plans,errors=uploaded_plans()
        assert not errors and plans[0].source == SOURCE
        live_save.assert_not_called()
