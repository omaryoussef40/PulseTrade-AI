from unittest.mock import patch
from types import SimpleNamespace
from dataclasses import replace
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest
from modules.strategy_controls import effective_config,check_automated_option_entry
from modules.pine_stock_signals import Settings,share_quantity,confirmed_signal
from modules.pine_stock_engine import Ledger,StockEngine,risk_snapshot,IBStockBroker
from test_pine_stock_levels import LEVEL,PLAN,NOW,Provider,bars


def config(active='pmb'):
    return dict(pine_stocks=dict(enabled=False),strategy=dict(active_strategy='pmb'),risk=dict(max_daily_capital_pct=90,max_spend_per_trade_pct=90,recycle_capital_after_exit=True),automation=dict(enabled=True,place_orders=True),watchlist=['AAPL','MSFT'],opening_orb_trade=dict(enabled=True),staged_trading_timeline=dict(enabled=True),dynamic_watchlist=dict(enabled=True),strategy_control=dict(enabled=True,active=active,profiles={'pmb':dict(daily_capital=500,per_trade_capital=200,max_daily_trades=2,all_watchlist=False,symbols=['AAPL'])}))


def test_profiles_apply_caps_symbols_and_do_not_change_order_approval():
    raw=config();cfg=effective_config(raw)
    assert cfg['risk']['max_daily_capital']==500 and cfg['risk']['max_spend_per_trade']==200
    assert cfg['risk']['max_daily_capital_pct']==cfg['risk']['max_spend_per_trade_pct']==0
    assert not cfg['risk']['recycle_capital_after_exit']
    assert cfg['watchlist']==['AAPL'] and not cfg['dynamic_watchlist']['enabled']
    assert cfg['order']['type']=='LIMIT'
    assert not cfg['opening_orb_trade']['enabled'] and not cfg['staged_trading_timeline']['enabled']
    assert cfg['automation']==raw['automation']
    assert raw['risk']['max_daily_capital_pct']==90
    assert not effective_config(config('pine_stocks'))['_entries_paused']
    assert effective_config(config('pine_stocks'))['pine_stocks']['enabled']
    assert effective_config(config('paused'))['_entries_paused']
    with pytest.raises(ValueError):effective_config(config('unknown'))


@pytest.mark.parametrize('change,count,deployed',[({'symbol':'MSFT'},0,0),({'strategy_profile':'brt'},0,0),({'strategy_profile':None},0,0),({'limit_price':3},0,0),({'limit_price':float('nan')},0,0),({},2,0),({},0,400),({'order_type':'MARKET'},0,0)])
def test_pending_approvals_recheck_profile_and_budget(change,count,deployed):
    order=dict(strategy_profile='pmb',symbol='AAPL',limit_price=2,quantity=1,order_type='LIMIT')
    order.update(change)
    with pytest.raises(ValueError):check_automated_option_entry(config(),order,count,deployed)
    with pytest.raises(ValueError):check_automated_option_entry(config('pine_stocks'),order,0,0)
    with pytest.raises(ValueError):check_automated_option_entry(config('paused'),order,0,0)


def test_valid_controlled_option_entry():
    check_automated_option_entry(config(),dict(strategy_profile='pmb',symbol='AAPL',limit_price=2,quantity=1),1,300)


def test_daily_stock_budget_caps_quantity_and_is_not_recycled(tmp_path):
    settings=Settings(max_daily_capital=250)
    ledger=Ledger(tmp_path/'l.sqlite');p=Provider();e=StockEngine(settings,ledger,p)
    row=e.cycle(PLAN,NOW)['positions'][0]
    assert row['initial_quantity']==2 and row['entry_capital']<=250
    later=NOW+pd.Timedelta(seconds=20)
    p.q=dict(bid=104,ask=104.01,timestamp=later.isoformat())
    closed=e.cycle(PLAN,later,manage_only=True)['positions'][0]
    usage=risk_snapshot([closed],str(NOW.date()),{},settings)
    assert usage['daily_capital']==row['entry_capital'] and usage['notional']==0
    with pytest.raises(ValueError):share_quantity(dict(side='LONG',levels=LEVEL.side('LONG')),100,settings,daily_capital_used=250)
    ledger.close()


def test_ticker_and_direction_permissions(tmp_path):
    settings=Settings(trade_all_plan_symbols=False,allowed_symbols=())
    ledger=Ledger(tmp_path/'l.sqlite')
    result=StockEngine(settings,ledger,Provider()).cycle(PLAN,NOW)
    assert not result['positions'] and 'not approved' in result['decisions'][0]['reason']
    assert confirmed_signal(PLAN,LEVEL,bars(),NOW,Settings(enable_longs=False)) is None
    ledger.close()


def test_paused_stock_strategy_monitors_without_entries():
    import engine
    raw=config('paused');raw['pine_stocks']['enabled']=True
    with patch.object(engine,'load_config',return_value=raw),patch('modules.pine_stock_engine.run_pine_cycle') as run:
        engine.run_cycle();engine.run_broker_sync_cycle()
        assert run.call_count==2
        assert all(call.kwargs=={'manage_only':True} for call in run.call_args_list)


def test_paused_options_cycle_does_not_connect():
    import engine
    with patch.object(engine,'load_config',return_value=config('paused')),patch.object(engine,'connect_ib') as connect:
        engine.run_cycle()
        connect.assert_not_called()


def test_control_page_displays_all_profiles_without_activating_them():
    snapshot=dict(source='IBKR',fetched_at=NOW.isoformat(),candles={},errors={})
    with patch('modules.pine_stock_charts.chart_snapshot',return_value=snapshot):
        at=AppTest.from_string('from modules.pine_stock_ui import render\nrender({})').run(timeout=20)
        at.radio[0].set_value('Trading controls').run()
    assert not at.exception
    assert len(at.dataframe[0].value)==3
    assert at.selectbox[0].value=='pmb'
    assert any(n.label=='Maximum new stock entry value per day ($)' for n in at.number_input)


@pytest.mark.parametrize('has_position',[False,True])
def test_control_save_changes_selection_only_after_explicit_save_and_blocks_switch_with_holdings(tmp_path,has_position):
    raw=config()
    with patch('bot_core.load_config',return_value=raw),patch('bot_core.save_config') as save,patch('bot_core.read_active_positions',return_value=[{'symbol':'AAPL'}] if has_position else []),patch('modules.strategy_controls_ui.LEDGER_FILE',tmp_path/'absent.sqlite'):
        at=AppTest.from_string('from bot_core import load_config\nfrom modules.strategy_controls_ui import render\nrender(load_config())').run(timeout=20)
        at.selectbox[0].set_value('pine_stocks').run()
        next(n for n in at.number_input if n.label=='Maximum new stock entry value per day ($)').set_value(2000.).run()
        save.assert_not_called()
        at.button[0].click().run()
        assert not at.exception
        if has_position:
            save.assert_not_called()
            assert any('existing positions' in error.value for error in at.error)
        else:
            saved=save.call_args.args[0]
            assert saved['strategy_control']['active']=='pine_stocks'
            assert saved['pine_stocks']['enabled']
            assert saved['pine_stocks']['settings']['max_daily_capital']==2000
            assert saved['automation']==raw['automation']
