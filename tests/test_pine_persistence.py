from dataclasses import asdict,replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch
import json

import pytest
from streamlit.testing.v1 import AppTest

from backtester import pine_store
from modules.pine_stock_signals import Settings
from modules.pine_levels import parse_pine
from test_pine_backtest import PLAN,SOURCE


SCRIPT='from backtester.pine_ui import render\nrender({})'


def test_settings_survive_a_new_browser_session_without_running_backtest():
    with patch('backtester.pine_ui.available_plans',return_value=([PLAN],[])), \
         patch('backtester.pine_ui.load_result',return_value=None), \
         patch('backtester.pine_ui.execute_backtest') as execute:
        first=AppTest.from_string(SCRIPT).run(timeout=20)
        first.number_input(key='pine_bt_capital').set_value(25000.).run(timeout=20)
        first.number_input(key='pine_bt_option_premium_budget').set_value(8000.).run(timeout=20)
        first.number_input(key='pine_bt_option_max_total_premium').set_value(9000.).run(timeout=20)
        first.number_input(key='pine_bt_option_contracts').set_value(10).run(timeout=20)
        first.checkbox(key='pine_bt_puts').uncheck().run(timeout=20)
        first.date_input(key='pine_bt_end').set_value(date(2026,10,13)).run(timeout=20)
        assert not first.exception
        # AppTest with new state represents a full page refresh/new session.
        second=AppTest.from_string(SCRIPT).run(timeout=20)
        assert not second.exception
        assert second.number_input(key='pine_bt_capital').value==25000.
        assert second.number_input(key='pine_bt_option_premium_budget').value==8000.
        assert second.number_input(key='pine_bt_option_max_total_premium').value==9000.
        assert second.number_input(key='pine_bt_option_contracts').value==10
        assert second.checkbox(key='pine_bt_puts').value is False
        assert second.date_input(key='pine_bt_end').value==date(2026,10,13)
        assert second.multiselect(key='pine_bt_plans').value==[PLAN.source_hash]
        execute.assert_not_called()


def test_uploaded_script_stays_available_without_reupload_and_is_deduplicated():
    upload=SimpleNamespace(name='../../daily-plan.pine',getvalue=lambda:SOURCE.encode())
    with patch('backtester.pine_ui.available_plans',return_value=([],[])), \
         patch('backtester.pine_ui.load_result',return_value=None), \
         patch('modules.pine_levels.save_plan') as live_save:
        with patch('backtester.pine_ui.st.file_uploader',return_value=[upload]):
            first=AppTest.from_string(SCRIPT).run(timeout=20)
            first.run(timeout=20)
            assert not first.exception
        with patch('backtester.pine_ui.st.file_uploader',return_value=[]):
            second=AppTest.from_string(SCRIPT).run(timeout=20)
            assert not second.exception
            assert second.multiselect(key='pine_bt_plans').value==[PLAN.source_hash]
        files=list((pine_store.STORE_DIR/'scripts').glob('*.pine'))
        assert len(files)==1
        assert files[0].read_text()==SOURCE
        live_save.assert_not_called()


def test_deselected_plans_and_stock_mode_survive_refresh():
    with patch('backtester.pine_ui.available_plans',return_value=([PLAN],[])),patch('backtester.pine_ui.load_result',return_value=None):
        first=AppTest.from_string(SCRIPT).run(timeout=20)
        first.radio(key='pine_bt_instrument').set_value('STK').run(timeout=20)
        first.number_input(key='pine_bt_risk_dollars').set_value(75.).run(timeout=20)
        first.multiselect(key='pine_bt_plans').set_value([]).run(timeout=20)
        second=AppTest.from_string(SCRIPT).run(timeout=20)
        assert not second.exception
        assert second.radio(key='pine_bt_instrument').value=='STK'
        assert second.number_input(key='pine_bt_risk_dollars').value==75.
        assert second.multiselect(key='pine_bt_plans').value==[]
        assert second.button(key='pine_bt_run').disabled


def test_switching_to_a_different_plan_resets_saved_dates():
    newer=parse_pine(SOURCE.replace('== 12','== 13'))
    with patch('backtester.pine_ui.available_plans',return_value=([PLAN,newer],[])),patch('backtester.pine_ui.load_result',return_value=None):
        first=AppTest.from_string(SCRIPT).run(timeout=20)
        first.multiselect(key='pine_bt_plans').set_value([PLAN.source_hash]).run(timeout=20)
        first.date_input(key='pine_bt_end').set_value(date(2026,10,14)).run(timeout=20)
        second=AppTest.from_string(SCRIPT).run(timeout=20)
        assert second.date_input(key='pine_bt_end').value==date(2026,10,14)
        second.multiselect(key='pine_bt_plans').set_value([newer.source_hash]).run(timeout=20)
        assert not second.exception
        assert second.date_input(key='pine_bt_start').value==date(2026,10,13)
        assert second.date_input(key='pine_bt_end').value==date(2026,10,13)


def test_preferences_exclude_transient_results_uploads_and_unknown_keys():
    state={'pine_bt_capital':12345.,'pine_bt_result':{'secret':'not persisted'},'pine_bt_uploads':['file'],
        'pine_bt_run':True,'workspace_stock_confirmation':'not persisted'}
    pine_store.save_preferences(state,asdict(Settings()))
    data=json.loads((pine_store.STORE_DIR/'preferences.json').read_text())
    assert data['widgets']=={'pine_bt_capital':12345.}
    restored={}
    pine_store.restore_widgets(restored,data)
    assert restored=={'pine_bt_capital':12345.}


def test_invalid_uploaded_sources_are_not_saved():
    with pytest.raises(ValueError):
        pine_store.archive_upload(replace(PLAN,source='invalid'))
    assert pine_store.uploaded_plans()==([],[])


def test_settings_survive_leaving_backtest_page_and_returning():
    script='''
import streamlit as st
from backtester.pine_ui import render
page=st.radio('Page',['Backtest','Other'],key='test_page')
if page=='Backtest':render({})
'''
    with patch('backtester.pine_ui.available_plans',return_value=([PLAN],[])),patch('backtester.pine_ui.load_result',return_value=None):
        app=AppTest.from_string(script).run(timeout=20)
        app.number_input(key='pine_bt_option_premium_budget').set_value(8000.).run(timeout=20)
        app.date_input(key='pine_bt_end').set_value(date(2026,10,14)).run(timeout=20)
        app.radio(key='test_page').set_value('Other').run(timeout=20)
        app.radio(key='test_page').set_value('Backtest').run(timeout=20)
        assert not app.exception
        assert app.number_input(key='pine_bt_option_premium_budget').value==8000.
        assert app.date_input(key='pine_bt_end').value==date(2026,10,14)
