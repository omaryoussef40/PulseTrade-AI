from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from modules.strategy_controls import effective_config, check_automated_option_entry, verified_account_equity
from modules.pine_stock_signals import Settings, share_quantity
from modules.pine_stock_engine import StockEngine, Ledger
from test_pine_stock_levels import PLAN, NOW, LEVEL, Provider


def config():
    return dict(strategy=dict(active_strategy='pmb',min_score=90,orb_minutes=15,breakout_follow_through=dict(confirmation_minutes=15)),risk=dict(account_size=10000,max_daily_capital=9500,max_daily_capital_pct=95,max_spend_per_trade=6500,max_spend_per_trade_pct=65,max_trades_per_day=3,recycle_capital_after_exit=True,stop_loss_pct=10),watchlist=['TEST'],automation=dict(enabled=False,place_orders=False),strategy_control=dict(enabled=True,active='dual',last_active='dual',allocation=dict(pmb_pct=80,stocks_pct=20,stock_margin=2)),pine_stocks=dict(enabled=True,settings=dict(execution='Simulation')))


def test_dual_preserves_pmb_settings_and_both_profiles():
    raw=config();original=deepcopy(raw);cfg=effective_config(raw)
    assert cfg['_concurrent_strategies'] and cfg['pine_stocks']['enabled']
    assert cfg['strategy']==raw['strategy'] and cfg['watchlist']==raw['watchlist']
    expected=dict(raw['risk'],max_daily_capital_pct=80)
    assert cfg['risk']==expected
    assert cfg['automation']==raw['automation']
    assert cfg['pine_stocks']['settings']['account_allocation_pct']==20
    assert cfg['pine_stocks']['settings']['margin_multiplier']==2
    assert raw==original
    assert effective_config(cfg)==cfg


@pytest.mark.parametrize('allocation',[dict(pmb_pct=81,stocks_pct=20),dict(stock_margin=3),dict(pmb_pct=float('nan')),dict(stocks_pct=0)])
def test_invalid_allocation_blocks_both_strategies(allocation):
    raw=config();raw['strategy_control']['allocation'].update(allocation)
    with pytest.raises(ValueError):effective_config(raw)


def test_options_cap_is_eighty_percent_not_stock_margin():
    raw=config();order=dict(strategy_profile='pmb',symbol='TEST',limit_price=10,quantity=1,order_type='LIMIT')
    check_automated_option_entry(raw,order,1,7000,account_size=10000)
    with pytest.raises(ValueError):check_automated_option_entry(raw,order,1,7001,account_size=10000)
    with pytest.raises(ValueError):check_automated_option_entry(raw,order,0,0)
    with pytest.raises(ValueError):check_automated_option_entry(raw,dict(order,strategy_profile='brt'),0,0,account_size=10000)
    with pytest.raises(ValueError):check_automated_option_entry(raw,order,3,0,account_size=10000)
    # A new account value must reduce the budget; saved $10k is not used.
    with pytest.raises(ValueError):check_automated_option_entry(raw,order,1,7000,account_size=8000)


def test_stock_equity_allocation_margin_open_exposure_and_broker_funds():
    settings=Settings(account_allocation_pct=20,margin_multiplier=2,risk_dollars=10000,max_total_risk=10000)
    sig=dict(side='LONG',levels=LEVEL.side('LONG'))
    assert share_quantity(sig,100,settings,account_equity=10000)==40
    assert share_quantity(sig,100,settings,account_equity=10000,notional_used=3000)==10
    assert share_quantity(sig,100,settings,account_equity=10000,available_funds=1000)==20
    assert share_quantity(sig,100,settings,account_equity=5000)==20
    with pytest.raises(ValueError):share_quantity(sig,100,settings,account_equity=10000,notional_used=4000)
    with pytest.raises(ValueError):share_quantity(sig,100,settings)
    assert share_quantity(sig,100,replace(settings,margin_multiplier=1),account_equity=10000)==20
    # Existing risk and position limits remain stricter when applicable.
    assert share_quantity(sig,100,replace(settings,max_position_dollars=500),account_equity=10000)==5


def test_stock_engine_uses_verified_equity_even_in_simulation(tmp_path):
    provider=Provider();provider.account_equity=lambda:1000
    ledger=Ledger(tmp_path/'stock.sqlite')
    settings=Settings(account_allocation_pct=20,margin_multiplier=2)
    result=StockEngine(settings,ledger,provider).cycle(PLAN,NOW)
    assert result['positions'][0]['entry_capital']<=400
    ledger.close()


def account_row(account='A',tag='NetLiquidation',value='10000',currency='USD'):
    return SimpleNamespace(account=account,tag=tag,value=value,currency=currency)


def test_account_equity_is_account_specific_and_never_buying_power():
    ib=SimpleNamespace(managedAccounts=lambda:['A','B'],accountSummary=lambda:[account_row('B',value='99999'),account_row(),account_row(tag='BuyingPower',value='40000')])
    assert verified_account_equity(ib,'A')==10000
    with pytest.raises(ValueError):verified_account_equity(ib)
    for rows in ([account_row(tag='BuyingPower')],[account_row(currency='EUR')],[account_row(value='nan')],[]):
        ib.accountSummary=lambda:rows
        with pytest.raises(ValueError):verified_account_equity(ib,'A')


def test_pausing_dual_preserves_both_monitors_and_blocks_options():
    raw=config();raw['strategy_control']['active']='paused'
    cfg=effective_config(raw)
    assert cfg['_entries_paused'] and cfg['_concurrent_strategies'] and cfg['pine_stocks']['enabled']
    with pytest.raises(ValueError):check_automated_option_entry(raw,dict(strategy_profile='pmb'),0,0,account_size=10000)
    from modules import pine_stock_worker
    raw['automation']['enabled']=True
    with patch('bot_core.load_config',return_value=raw),patch('modules.pine_stock_engine.run_pine_cycle') as run:
        assert pine_stock_worker.cycle()
        assert run.call_args.kwargs==dict(manage_only=True)


def test_dual_option_cycles_do_not_return_at_stock_dispatch():
    import engine
    # Automation is off: options must reach their heartbeat gate, never connect.
    with patch.object(engine,'load_config',return_value=config()),patch.object(engine,'connect_ib') as connect,patch.object(engine,'write_health') as health,patch('modules.pine_stock_engine.run_pine_cycle') as stocks:
        engine.run_cycle();engine.run_broker_sync_cycle()
        connect.assert_not_called();stocks.assert_not_called()
        assert health.call_count==2


def test_worker_is_independent_single_child_and_stops_on_mode_change():
    import engine
    child=SimpleNamespace(poll=lambda:None,terminate=lambda:None,wait=lambda **kw:None)
    with patch.object(engine,'_STOCK_WORKER',None),patch.object(engine.subprocess,'Popen',return_value=child) as spawn:
        engine.ensure_stock_worker(effective_config(config()))
        engine.ensure_stock_worker(effective_config(config()))
        spawn.assert_called_once()
        assert 'modules.pine_stock_worker' in spawn.call_args.args[0]
        engine.ensure_stock_worker({})
        assert engine._STOCK_WORKER is None


def test_combined_controls_show_allocation_and_save_without_altering_pmb(tmp_path):
    raw=config()
    with patch('bot_core.load_config',return_value=raw),patch('bot_core.save_config') as save,patch('bot_core.read_active_positions',return_value=[]),patch('modules.strategy_controls_ui.LEDGER_FILE',tmp_path/'none.sqlite'):
        app=AppTest.from_string('from bot_core import load_config\nfrom modules.strategy_controls_ui import render\nrender(load_config())').run(timeout=20)
        assert not app.exception
        assert app.selectbox[0].value=='dual'
        assert [n.value for n in app.number_input[:3]]==[80,20,2]
        app.button[0].click().run()
        assert not app.exception
        saved=save.call_args.args[0]
        assert saved['strategy']==raw['strategy'] and saved['risk']==raw['risk']
        assert saved['strategy_control']['allocation']==dict(pmb_pct=80,stocks_pct=20,stock_margin=2)


def test_stock_fills_do_not_consume_option_trade_or_loss_budgets(tmp_path):
    import bot_core
    import pandas as pd
    from datetime import datetime, timedelta
    now=datetime.now(bot_core.EASTERN)
    entries=[dict(timestamp=now.isoformat(),event='ENTRY',symbol='STOCK',status='Filled',sec_type='STK',quantity=40,filled_quantity=40,entry_price=100,realized_pnl=0),
             dict(timestamp=now.isoformat(),event='ENTRY',symbol='OPTION',status='Filled',sec_type='OPT',signal='CALL',quantity=1,filled_quantity=1,entry_price=2,realized_pnl=0),
             dict(timestamp=(now+timedelta(seconds=1)).isoformat(),event='EXIT',symbol='STOCK',status='Filled',sec_type='STK',quantity=40,exit_price=95,realized_pnl=-200)]
    path=tmp_path/'trades.csv';pd.DataFrame(entries).to_csv(path,index=False)
    with patch.object(bot_core,'TRADE_LOG_FILE',str(path)):
        assert bot_core.get_today_trade_stats()==(1,200.)
        assert bot_core.get_today_traded_symbols()=={'OPTION'}
        assert bot_core.get_today_loss_stats()==(0,0.)
