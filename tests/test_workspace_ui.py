import ast
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from modules.workspace_ui import PAGES,resolve_page,merge_settings_changes,stock_positions
from modules.pine_levels import parse_pine
from bot_core import default_config

ROOT=Path(__file__).parents[1]
DASHBOARD=(ROOT/'dashboard.py').read_text()
TREE=ast.parse(DASHBOARD)
SETTINGS=ast.get_source_segment(DASHBOARD,next(node for node in TREE.body if isinstance(node,ast.FunctionDef) and node.name=='render_platform_settings'))
ROUTES=DASHBOARD.split('# Page routing from sidebar navigation',1)[1].split('\nif selected_page == "🏦 Account Status":',1)[0]
SOURCE=(ROOT/'tests/fixtures/orora_daily_study_2026-10-12.pine').read_text()
PLAN=parse_pine(SOURCE)

SETTINGS_APP='''
from copy import deepcopy
from types import SimpleNamespace
import streamlit as st
from bot_core import *
from bot_core import _number_or_none
from modules.workspace_ui import merge_settings_changes
if 'config' not in st.session_state:
    st.session_state['config']=default_config()
    st.session_state['config']['automation']['enabled']=False
    st.session_state['config']['risk']['use_ibkr_buying_power']=False
load_config=lambda:deepcopy(st.session_state['config'])
def save_config(config):
    st.session_state['config']=config
    st.session_state['saved_count']=st.session_state.get('saved_count',0)+1
ib_cfg=SimpleNamespace(host='localhost',port=7497,client_id=1,account=None)
dashboard_auto_start_enabled=lambda cfg:False
approval_mode_from_config=lambda cfg:'Dashboard'
read_opening_orb_trade_state=lambda:{}
TRADING_STYLE_PRESETS={}
load_dynamic_payload=lambda:{}
cached_account_summary=lambda *args:{}
'''+SETTINGS+'''\nsection=st.radio('Section',['Trading','Strategy','Risk & Size','Watchlists','Schedule','Connection','Integrations','Display'])
render_platform_settings(section)
'''


@pytest.mark.parametrize('section',['Trading','Strategy','Risk & Size','Watchlists','Schedule','Connection','Integrations','Display'])
def test_each_moved_settings_section_renders_without_saving(section):
    app=AppTest.from_string(SETTINGS_APP).run(timeout=20)
    app.radio[0].set_value(section).run()
    assert not app.exception
    assert app.session_state.filtered_state.get('saved_count',0)==0
    assert any(button.label.startswith('Save ') for button in app.button)


def test_settings_save_is_explicit_and_preserves_other_sections():
    app=AppTest.from_string(SETTINGS_APP).run(timeout=20)
    original=deepcopy(app.session_state['config'])
    checkbox=next(widget for widget in app.checkbox if widget.label=='Enable engine automation')
    checkbox.set_value(True).run()
    assert app.session_state['config']==original
    assert any(c.value=='Unsaved changes' for c in app.caption)
    next(button for button in app.button if button.label=='Save trading settings').click().run()
    assert app.session_state['config']['automation']['enabled']
    assert app.session_state['config']['strategy']==original['strategy']


def test_merge_keeps_changes_from_other_pages():
    original={'risk':{'cap':80,'stop':10},'automation':{'enabled':False}}
    edited=deepcopy(original);edited['risk']['stop']=12
    current=deepcopy(original);current['risk']['cap']=75;current['automation']['enabled']=True
    assert merge_settings_changes(original,edited,current)=={'risk':{'cap':75,'stop':12},'automation':{'enabled':True}}
    assert original['risk']['stop']==10


@pytest.mark.parametrize('old,page',[('scanner','strategies'),('daily-stock-levels','strategies'),('account','settings'),('logs','settings'),('ai-audit','journal'),('market-intel','research'),('earnings','research')])
def test_old_links_resolve_to_new_workspaces(old,page):
    assert resolve_page(old)[0]==page
    assert len(PAGES)==7


def test_daily_workspace_preserves_draft_between_plan_and_chart_views():
    snapshot=dict(source='IBKR',fetched_at='2026-10-12T09:40:01-04:00',candles={},errors={})
    with patch('modules.pine_stock_ui.load_plan',return_value=None),patch('modules.pine_stock_charts.chart_snapshot',return_value=snapshot),patch('bot_core.save_config') as save:
        app=AppTest.from_string('from modules.pine_stock_ui import render_workspace\nrender_workspace({})').run(timeout=20)
        app.radio[0].set_value('Daily Plan').run()
        app.text_area[0].set_value(SOURCE).run()
        assert not app.exception and not app.error
        assert not app.number_input
        assert not any(widget.label=='Execution mode' for widget in app.selectbox)
        app.radio[0].set_value('Charts').run()
        assert not app.exception and len(app.get('plotly_chart'))==10
        app.radio[0].set_value('Daily Plan').run()
        assert app.text_area[0].value==SOURCE
        app.radio[0].set_value('Setup').run()
        assert not app.exception
        assert app.number_input and not app.text_area
        assert not any('risk approval' in widget.label for widget in app.text_input)
        save.assert_not_called()


def test_stock_plan_save_does_not_modify_execution_permissions():
    with patch('modules.pine_stock_ui.load_plan',return_value=PLAN),patch('modules.pine_stock_ui.save_plan') as plan_save,patch('bot_core.save_config') as config_save:
        app=AppTest.from_string('from modules.pine_stock_ui import render_editor\nrender_editor({},section="Daily Plan")').run(timeout=20)
        next(button for button in app.button if button.label=='Save daily stock plan').click().run()
        assert not app.exception
        plan_save.assert_called_once()
        config_save.assert_not_called()


def test_email_configuration_is_only_in_settings():
    with patch('modules.daily_email_brief_ui.load_brief',return_value=None):
        app=AppTest.from_string('from modules.daily_email_brief_ui import render\nrender({},settings_only=True)').run(timeout=20)
        assert not app.exception
        assert app.text_input[0].value=='hello@orora.me'
        assert not any(button.label=='Check inbox now' for button in app.button)


def test_stock_ledger_display_is_read_only_and_filters_account(tmp_path):
    from modules.pine_stock_engine import Ledger
    import modules.workspace_ui as workspace
    ledger=Ledger(tmp_path/'data/pine_stock_trades.sqlite')
    for account in ('A','B'):
        ledger.claim(dict(key=account,book='IBKR Live:'+account,day='2026-10-12',symbol='CRM',status='OPEN'))
    ledger.close()
    with patch.object(workspace,'ROOT',tmp_path):
        rows=stock_positions({'pine_stocks':{'settings':{'execution':'IBKR Live'}}},'A')
        assert [row['key'] for row in rows]==['A']


def test_all_workspace_routes_run_without_broker_calls_or_config_saves():
    # Execute actual dashboard routing with inert backend callbacks. The dashboard's
    # automatic startup is deliberately excluded from this UI-only smoke test.
    script='''
from copy import deepcopy
from pathlib import Path
import streamlit as st
from bot_core import default_config, WATCHLIST
from modules.workspace_ui import *
from modules.pine_stock_ui import *
PROJECT_ROOT=Path.cwd()
cfg=default_config()
read_health=lambda:{}
read_active_positions=lambda:[]
sync_ibkr_account_status=lambda force=False:{'connected':True}
symbols=list(WATCHLIST)
render_platform_settings=lambda section:st.write('Settings section: '+section)
render_account_status_tab=lambda:st.write('Connection')
start_trading_engine_once=lambda:None
workspace_page=st.radio('Page',list(PAGES),format_func=PAGES.get)
legacy_view=None
selected_page=workspace_page
'''+ROUTES+'''\nst.caption('Route: '+selected_page)
'''
    with patch('modules.pine_stock_ui.load_plan',return_value=None),patch('modules.pine_levels.load_plan',return_value=None),patch('bot_core.save_config') as save:
        app=AppTest.from_string(script).run(timeout=20)
        for page in PAGES:
            app.radio[0].set_value(page).run()
            assert not app.exception,page
        save.assert_not_called()


def test_overview_fetches_account_and_refresh_button_forces_new_snapshot():
    from unittest.mock import Mock
    routing = ROUTES.split('if workspace_page == "overview":', 1)[1].split('elif workspace_page == "strategies":', 1)[0]
    for force in (False, True):
        st = Mock()
        st.button.return_value = force
        sync = Mock(return_value={'connected': True, 'NetLiquidation': 10000})
        render = Mock()
        exec('if True:'+routing, dict(st=st, sync_ibkr_account_status=sync,
            render_overview=render, cfg={}, read_health=lambda:{}, read_active_positions=lambda:[]))
        sync.assert_called_once_with(force=force)
        assert render.call_args.args[2]['NetLiquidation'] == 10000


def test_overview_displays_engine_failure_even_if_process_is_running():
    script = '''
from bot_core import default_config
from modules.workspace_ui import render_overview
cfg=default_config()
cfg['pine_stocks']={'enabled':False}
cfg['automation']['enabled']=True
render_overview(cfg, {'engine_running':True,'last_status':'Engine error','last_error':'ValueError: Unknown active strategy. Automatic entries are blocked.'}, {'connected':True}, [])
'''
    with patch('modules.workspace_ui.stock_positions', return_value=[]):
        app=AppTest.from_string(script).run(timeout=20)
    assert not app.exception
    assert any('Unknown active strategy' in message.value for message in app.error)
    assert not app.success


def test_copy_live_settings_is_a_draft_and_keeps_watchlist():
    from test_backtester_stock_universe import SMOKE_APP
    script=SMOKE_APP.replace("'strategy': {}, 'risk': {}", "'strategy': {'orb_minutes': 15, 'min_session_bars': 2, 'option_dte':7, 'breakout_follow_through': {'enabled':True,'confirmation_minutes':5,'min_breakout_volume_ratio':1.5}}, 'risk': {'max_contracts':20,'max_daily_capital_pct':80.,'max_spend_per_trade_pct':65.,'stop_loss_pct':10.}")
    app=AppTest.from_string(script).run(timeout=20)
    original=deepcopy(app.session_state['test_config'])
    app.button(key='sl_use_live').click().run(timeout=20)
    assert not app.exception
    assert app.number_input(key='sl_min_bars').value==2
    assert app.number_input(key='sl_max_contracts').value==20
    assert app.selectbox(key='sl_confirmation_minutes').value==5
    assert app.number_input(key='sl_daily_exp_pct').value==80
    assert app.multiselect(key='sl_option_dtes').value==[7]
    assert app.session_state['test_config']==original
    app.button(key='sl_save_settings').click().run(timeout=20)
    assert app.session_state['test_config']['watchlist']==original['watchlist']
    assert app.session_state['test_config']['risk']==original['risk']


def test_comparison_flags_backtest_settings_differences():
    from modules.backtest_workspace import comparison_rows
    from backtester.controller import StrategyLabSettings
    config=default_config()
    settings=StrategyLabSettings(symbols=['AAPL'],orb_minutes=30)
    rows=comparison_rows(config,settings)
    assert next(row for row in rows if row['Setting']=='ORB minutes')['Status']=='Different'


def test_position_table_joins_both_strategies_without_mislabeling_manual_stock():
    from modules.workspace_ui import position_rows
    broker=[dict(account='A',symbol='AAPL',secType='OPT',conId=1,avgCost=200,position=2),dict(account='A',symbol='CRM',secType='STK',conId=2,avgCost=240,position=4),dict(account='A',symbol='MSFT',secType='STK',conId=3,avgCost=400,position=1)]
    options=[dict(con_id=1,strategy_profile='pmb',current_stop_price=1.8)]
    stocks=[dict(key='stock',book='IBKR Live:A',symbol='CRM',levels=dict(stop=236,t1=244,t2=248),status='OPEN')]
    marks={'conid:1':dict(market_price=2.2,unrealized_pnl=40),'conid:2':dict(market_price=241,unrealized_pnl=4)}
    rows=position_rows(broker,options,stocks,marks)
    assert len(rows)==3
    assert rows[0]['Strategy']=='PMB' and rows[0]['Entry']==2 and rows[0]['P&L']==40
    assert rows[1]['Strategy']=='Daily Stocks' and rows[1]['Stop']==236 and rows[1]['Current price']==241
    assert rows[2]['Strategy']=='Other / manual'


def test_simulation_and_unresolved_stock_positions_remain_visible_without_broker_holdings():
    from modules.workspace_ui import position_rows
    records=[dict(key='s',book='Simulation:A',symbol='CRM',quantity=4,status='SIM_OPEN',entry_price=240,levels=dict(stop=236)),dict(key='u',book='IBKR Live:A',symbol='XOM',quantity=1,status='UNKNOWN',entry_price=170)]
    rows=position_rows([],[],records)
    assert len(rows)==2 and rows[1]['Status']=='UNKNOWN'
    assert rows[0]['Current price'] is None
