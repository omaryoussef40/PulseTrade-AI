from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock,patch
from copy import deepcopy

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from modules.pine_stock_signals import Settings,option_quantity
from modules.pine_stock_engine import Ledger,StockEngine,IBStockBroker,broker_orders_allowed,risk_snapshot
from modules.pine_options import select_contract,owned_contracts
from backtester.pine import run_backtest
from test_pine_backtest import PLAN,candles,session,NOW,CFG
from test_pine_stock_levels import Provider,PLAN as LIVE_PLAN,NOW as LIVE_NOW,bars


OPTIONS=replace(CFG,instrument='OPT',option_premium_budget=500.)


def history(price=2.):
    raw=session('2026-10-12',price)
    raw.High=price+.2
    raw.Low=price-.2
    return raw


def loader(raw=None,right='C',expiry='20261019'):
    return Mock(return_value=(dict(symbol='TEST',expiry=expiry,strike=100.,right=right,conId=900,localSymbol='TEST OPTION'),history() if raw is None else raw))


def test_option_premium_budgets_use_100_multiplier_and_no_stock_margin():
    assert option_quantity(2.,OPTIONS)==2
    assert option_quantity(2.,replace(OPTIONS,margin_multiplier=2))==2
    assert option_quantity(2.,OPTIONS,available_funds=250)==1
    with pytest.raises(ValueError,match='premium budget'):
        option_quantity(6.,OPTIONS)
    with pytest.raises(ValueError,match='premium budget'):
        option_quantity(2.,OPTIONS,daily_capital_used=1500.)


def test_option_backtest_uses_historical_premiums_and_underlying_targets():
    stock=candles()
    stock.loc['2026-10-12 09:45',['High','Low','Close']]=[102.2,100.5,102.]
    stock.loc['2026-10-12 09:50',['High','Low','Close']]=[104.2,100.5,104.]
    premium=history()
    premium.loc['2026-10-12 09:45']=[2.5,3.2,2.4,3.,1000]
    premium.loc['2026-10-12 09:50']=[3.1,4.5,3.,4.,1000]
    source=loader(premium)
    result=run_backtest([PLAN],{'TEST':stock},OPTIONS,now=NOW,option_bars_provider=source)
    trade=result['trades'].iloc[0]
    assert trade.instrument=='OPT' and trade.quantity==2
    assert trade.option_right=='C' and trade.option_dte==7 and trade.actual_dte==7
    assert result['fills'].reason.tolist()==['ENTRY','T1','T2']
    assert result['fills'].price.tolist()==[2.,2.4,3.]
    assert trade.realized_pnl==pytest.approx((2.4-2.)*100+(3.-2.)*100-2.6)
    assert result['metrics']['ending_equity']==pytest.approx(10000+trade.realized_pnl)
    assert source.call_args.kwargs['option_dte']==7
    assert source.call_args.kwargs['reference_time'].strftime('%H:%M')=='09:40'


def test_put_backtest_profits_when_put_premium_rises():
    stock=pd.concat([session('2026-10-09',98.5),session('2026-10-12',97.4)])
    stock.loc['2026-10-12 09:30']=[98.5,98.7,98.3,98.5,1000]
    stock.loc['2026-10-12 09:35']=[98.,98.,95.9,97.5,2000]
    stock.loc['2026-10-12 09:45']=[97.4,97.6,93.9,94.5,1000]
    premium=history()
    premium.loc['2026-10-12 09:45']=[3.2,4.,3.,3.5,1000]
    source=loader(premium,right='P')
    result=run_backtest([PLAN],{'TEST':stock},OPTIONS,now=NOW,option_bars_provider=source)
    assert source.call_args.kwargs['signal']=='PUT'
    assert result['trades'].iloc[0].option_right=='P'
    assert result['metrics']['net_pnl']==pytest.approx(200-2.6)


def test_missing_option_history_never_generates_estimated_trades():
    with pytest.raises(ValueError,match='actual historical'):
        run_backtest([PLAN],{'TEST':candles()},OPTIONS,now=NOW)
    source=Mock(side_effect=RuntimeError('Expired option history not available'))
    result=run_backtest([PLAN],{'TEST':candles()},OPTIONS,now=NOW,option_bars_provider=source)
    assert result['trades'].empty
    assert result['meta']['status']=='COMPLETE_WITH_DATA_ERRORS'
    assert 'not available' in result['errors'].iloc[0].error


def test_option_data_gap_leaves_position_unresolved():
    premium=history().drop(pd.Timestamp('2026-10-12 09:45',tz='America/New_York'))
    result=run_backtest([PLAN],{'TEST':candles()},OPTIONS,now=NOW,option_bars_provider=loader(premium))
    assert result['trades'].iloc[0].status=='UNRESOLVED_OPTION_DATA'
    assert result['fills'].reason.tolist()==['ENTRY']
    assert not result['errors'].empty


def test_zero_volume_option_entry_cannot_use_carried_forward_price():
    premium=history()
    premium.loc['2026-10-12 09:40','Volume']=0
    result=run_backtest([PLAN],{'TEST':candles()},OPTIONS,now=NOW,option_bars_provider=loader(premium))
    assert result['trades'].empty and result['fills'].empty
    assert result['meta']['status']=='COMPLETE_WITH_DATA_ERRORS'
    assert 'zero trading volume' in result['errors'].iloc[0].error


def test_entry_quantity_cannot_exceed_printed_contract_volume():
    premium=history()
    premium.loc['2026-10-12 09:40','Volume']=1
    result=run_backtest([PLAN],{'TEST':candles()},OPTIONS,now=NOW,option_bars_provider=loader(premium))
    assert result['trades'].empty
    assert 'requires 2 contracts' in result['errors'].iloc[0].error


def test_stop_checks_total_exit_quantity_across_both_target_legs():
    stock=candles()
    stock.loc['2026-10-12 09:45',['High','Low','Close']]=[101.,98.,100.6]
    premium=history()
    premium.loc['2026-10-12 09:45','Volume']=1
    result=run_backtest([PLAN],{'TEST':stock},OPTIONS,now=NOW,option_bars_provider=loader(premium))
    assert result['fills'].reason.tolist()==['ENTRY']
    assert result['trades'].iloc[0].remaining==2
    assert result['trades'].iloc[0].status=='UNRESOLVED_OPTION_DATA'
    assert '2-contract exit' in result['errors'].iloc[0].error


def test_entry_and_same_bar_exit_cannot_reuse_the_same_printed_volume():
    stock=candles()
    stock.loc['2026-10-12 09:40',['High','Low','Close']]=[101.,98.,100.6]
    premium=history()
    premium.loc['2026-10-12 09:40','Volume']=2
    result=run_backtest([PLAN],{'TEST':stock},OPTIONS,now=NOW,option_bars_provider=loader(premium))
    assert result['fills'].reason.tolist()==['ENTRY']
    assert result['trades'].iloc[0].status=='UNRESOLVED_OPTION_DATA'


def test_zero_volume_option_exit_is_unresolved_instead_of_filled():
    stock=candles()
    stock.loc['2026-10-12 09:45',['High','Low','Close']]=[102.2,100.5,102.]
    premium=history()
    premium.loc['2026-10-12 09:45','Volume']=0
    result=run_backtest([PLAN],{'TEST':stock},OPTIONS,now=NOW,option_bars_provider=loader(premium))
    assert result['fills'].reason.tolist()==['ENTRY']
    assert result['trades'].iloc[0].status=='UNRESOLVED_OPTION_DATA'
    assert 'zero trading volume' in result['errors'].iloc[0].error


def test_zero_volume_end_mark_reports_last_actual_trade_time():
    premium=history()
    premium.loc['2026-10-12 15:55']=[9.,9.,9.,9.,0]
    result=run_backtest([PLAN],{'TEST':candles()},OPTIONS,now=NOW,option_bars_provider=loader(premium))
    assert result['trades'].iloc[0].last_mark==2.
    assert result['trades'].iloc[0].last_mark_time=='2026-10-12T15:55:00-04:00'
    assert result['metrics']['unrealized_pnl']==0.
    assert any('mark is stale' in error for error in result['errors'].error)


def test_older_saved_option_results_show_unverified_pricing_warning():
    result=run_backtest([PLAN],{'TEST':candles()},OPTIONS,now=NOW,option_bars_provider=loader())
    del result['meta']['option_fill_model_version']
    with patch('backtester.pine_ui.load_result',return_value=result), \
         patch('backtester.pine_ui.available_plans',return_value=([],[])):
        app=AppTest.from_string('from backtester.pine_ui import render\nrender({})').run(timeout=20)
        assert not app.exception
        assert any('zero-volume' in item.value and 'unverified' in item.value for item in app.warning)


def test_carried_option_uses_same_expiry_and_strike_on_next_day():
    stock=pd.concat([candles(),session('2026-10-13')])
    stock.loc['2026-10-13 09:30']=[105.,106.,104.5,105.,1000]
    exact=Mock(return_value=({},session('2026-10-13',4.)))
    result=run_backtest([PLAN],{'TEST':stock},OPTIONS,end='2026-10-13',now=NOW,
        option_bars_provider=loader(),option_contract_bars_provider=exact)
    assert exact.call_args.kwargs['expiry']=='20261019'
    assert exact.call_args.kwargs['strike']==100.
    assert result['fills'].reason.tolist()==['ENTRY','T1','T2']
    assert result['fills'].price.tolist()[1:]==[4.,4.]


def test_expiration_close_uses_option_open_five_minutes_before_market_close():
    stock=pd.concat([candles(),session('2026-10-13')])
    exact=Mock(return_value=({},session('2026-10-13',1.)))
    result=run_backtest([PLAN],{'TEST':stock},OPTIONS,end='2026-10-13',now=NOW,
        option_bars_provider=loader(expiry='20261013'),option_contract_bars_provider=exact)
    assert result['trades'].iloc[0].exit_time=='2026-10-13T15:55:00-04:00'
    assert result['fills'].reason.tolist()==['ENTRY','EXPIRY_EXIT','EXPIRY_EXIT']
    assert result['fills'].price.tolist()[1:]==[1.,1.]


class OptionProvider(Provider):
    def __init__(self,side='LONG'):
        super().__init__()
        self.side=side
        if side=='SHORT':
            self.f=bars((99,97.5))
            self.q.update(bid=97.49,ask=97.51)
        self.oq=dict(bid=2.,ask=2.01,timestamp=LIVE_NOW.isoformat(),market_data_type=1)
    def select_option(self,symbol,side,underlying,now):
        return dict(symbol=symbol,expiry='20261016',strike=100.,right='C' if side=='LONG' else 'P',conId=900,localSymbol='TEST OPTION')
    def option_quote(self,info):return self.oq
    def option_tick(self,info):return .01


@pytest.mark.parametrize('side,stock_t1,stock_t2',[('LONG',102.,104.),('SHORT',96.,94.)])
def test_live_simulation_buys_calls_or_puts_and_exits_on_stock_levels(tmp_path,side,stock_t1,stock_t2):
    ledger=Ledger(tmp_path/'ledger.sqlite')
    provider=OptionProvider(side)
    engine=StockEngine(OPTIONS,ledger,provider)
    try:
        row=engine.cycle(LIVE_PLAN,LIVE_NOW)['positions'][0]
        assert row['instrument']=='OPT' and row['quantity']==2
        assert row['contract']['right']==('C' if side=='LONG' else 'P')
        assert row['reserved_risk']==pytest.approx(row['entry_price']*200)
        entry=row['entry_price']
        later=LIVE_NOW+pd.Timedelta(seconds=20)
        provider.q.update(bid=stock_t1,ask=stock_t1+.01,timestamp=later.isoformat())
        if side=='SHORT':
            provider.q['ask']=stock_t1
        provider.oq.update(bid=3.,ask=3.01,timestamp=later.isoformat())
        row=engine.cycle(LIVE_PLAN,later,manage_only=True)['positions'][0]
        assert row['quantity']==1
        later+=pd.Timedelta(seconds=20)
        provider.q.update(bid=stock_t2,ask=stock_t2+.01,timestamp=later.isoformat())
        # Short-side triggers use ask, so place it at the target too.
        if side=='SHORT':provider.q['ask']=stock_t2
        provider.oq.update(bid=4.,ask=4.01,timestamp=later.isoformat())
        row=engine.cycle(LIVE_PLAN,later,manage_only=True)['positions'][0]
        assert row['status']=='CLOSED'
        assert row['realized']==pytest.approx((3.-entry)*100+(4.-entry)*100-2.6)
    finally:
        ledger.close()


@pytest.mark.parametrize('side,above',[('LONG',True),('SHORT',False)])
def test_broker_buys_options_with_stock_conditional_oca_exits(side,above):
    orders=[]
    ids=iter(range(1,30))
    ib=SimpleNamespace(client=SimpleNamespace(getReqId=lambda:next(ids)),placeOrder=lambda c,o:orders.append((c,o)))
    provider=SimpleNamespace(ib=ib,account='DU123',contract=lambda symbol:SimpleNamespace(secType='STK',conId=123,symbol=symbol))
    broker=IBStockBroker(provider)
    level=LIVE_PLAN.levels[0].side(side)
    row=dict(key='opt',symbol='TEST',side=side,instrument='OPT',contract=dict(symbol='TEST',expiry='20261016',strike=100.,right='C' if side=='LONG' else 'P',conId=900),entry_price=2.,levels=level,
        legs=[dict(quantity=1,target='t1'),dict(quantity=1,target='t2')])
    broker.submit(broker.prepare(row))
    assert len(orders)==8
    assert all(contract.secType=='OPT' for contract,order in orders)
    for offset in (0,4):
        parent,target,stop,expiry=[order for contract,order in orders[offset:offset+4]]
        assert parent.action=='BUY' and parent.orderType=='LMT' and parent.tif=='FOK'
        assert target.action==stop.action==expiry.action=='SELL'
        assert target.orderType==stop.orderType=='MKT'
        assert target.conditions[0].conId==stop.conditions[0].conId==123
        assert target.conditions[0].isMore==above
        assert stop.conditions[0].isMore!=above
        assert stop.conditions[0].price==level['stop']
        assert target.ocaGroup==stop.ocaGroup==expiry.ocaGroup
        assert expiry.transmit and not parent.transmit and not stop.transmit


def test_live_option_permission_requires_option_specific_confirmation():
    app=dict(account_mode='Live',ib={},automation=dict(enabled=True,place_orders=True,confirm_order_risk=True,live_confirm_text='TRADE LIVE'))
    assert not broker_orders_allowed(app,replace(OPTIONS,execution='IBKR Live',live_confirmation='TRADE STOCK LEVELS LIVE'))
    assert broker_orders_allowed(app,replace(OPTIONS,execution='IBKR Live',live_confirmation='TRADE PINE OPTIONS LIVE'))


def test_live_reconciliation_values_put_profit_and_expiry_fill_with_100_multiplier():
    ids=iter(range(1,30))
    records=[]
    ib=SimpleNamespace(client=SimpleNamespace(getReqId=lambda:next(ids)),trades=lambda:records,reqAllOpenOrders=lambda:[],reqCompletedOrders=lambda **kwargs:[])
    broker=IBStockBroker(SimpleNamespace(ib=ib,account='DU123'))
    row=dict(key='o',symbol='TEST',side='SHORT',instrument='OPT',entry_price=2.,quantity=2,initial_quantity=2,
        levels=LIVE_PLAN.levels[0].side('SHORT'),legs=[dict(quantity=1,target='t1'),dict(quantity=1,target='t2')])
    broker.prepare(row)
    for index,leg in enumerate(row['legs']):
        for kind in ('entry','target','stop','expiry'):
            filled=kind=='entry' or (index==0 and kind=='target') or (index==1 and kind=='expiry')
            price=2. if kind=='entry' else 3. if kind=='target' else 4. if kind=='expiry' else 0.
            records.append(SimpleNamespace(order=SimpleNamespace(orderRef=leg['ref']+'-'+kind,account='DU123'),
                orderStatus=SimpleNamespace(status='Filled' if filled else 'Cancelled',filled=int(filled),avgFillPrice=price),fills=[]))
    result=broker.reconcile(row)
    assert result['status']=='CLOSED'
    assert result['realized']==300.
    assert result['entry_capital']==400.
    assert result['reserved_risk']==0.


def test_position_display_identifies_pine_options_without_duplicate_rows():
    from modules.workspace_ui import position_rows
    broker=[dict(account='DU123',symbol='TEST',secType='OPT',conId=900,position=2,avgCost=200.)]
    pine=[dict(key='o',book='IBKR Paper:DU123',symbol='TEST',instrument='OPT',contract={'conId':900},levels={'stop':99.,'t1':102.,'t2':104.},status='OPEN')]
    rows=position_rows(broker,[],pine)
    assert len(rows)==1
    assert rows[0]['Strategy']=='Daily Pine options'
    assert rows[0]['Instrument']=='Options' and rows[0]['Entry']==2.


def test_pine_owned_contract_is_excluded_from_pmb_management_but_blocks_duplicate_symbol(tmp_path):
    from bot_core import broker_open_option_positions,broker_open_option_symbols
    ledger=Ledger(tmp_path/'ledger.sqlite')
    row=dict(key='o',book='IBKR Paper:DU123',day='2026-10-12',symbol='TEST',status='OPEN',instrument='OPT',contract={'conId':900})
    ledger.claim(row)
    ledger.close()
    assert owned_contracts(tmp_path/'ledger.sqlite','DU123')=={900}
    holding=SimpleNamespace(account='DU123',position=1,avgCost=200.,contract=SimpleNamespace(secType='OPT',symbol='TEST',conId=900,right='C'))
    ib=SimpleNamespace(positions=lambda:[holding])
    with patch('modules.pine_options.owned_contracts',return_value={900}):
        assert broker_open_option_positions(ib,'DU123')==[]
        assert broker_open_option_symbols(ib,'DU123')=={'TEST'}


def test_live_risk_snapshot_values_puts_as_long_premium():
    row=dict(key='o',status='OPEN',quantity=2,symbol='TEST',side='SHORT',entry_price=2.,instrument='OPT',reserved_risk=400.,day='2026-10-12',entry_capital=400.)
    risk=risk_snapshot([row],'2026-10-12',{},OPTIONS,{'o':dict(bid=1.,ask=1.1)})
    assert risk['pnl']==-200. and risk['notional']==400.
    risk=risk_snapshot([row],'2026-10-12',{},OPTIONS,{'o':dict(bid=3.,ask=3.1)})
    assert risk['pnl']==0


def test_nearest_expiry_and_atm_strike_selection():
    chain=SimpleNamespace(multiplier='100',exchange='SMART',tradingClass='TEST',expirations={'20261016','20261023'},strikes={99.,100.,101.,102.})
    def qualify(contract):
        contract.conId=900;contract.localSymbol='TEST OPTION'
        return [contract]
    ib=SimpleNamespace(reqSecDefOptParams=lambda *args:[chain],qualifyContracts=qualify)
    stock=SimpleNamespace(symbol='TEST',secType='STK',conId=123)
    contract=select_contract(ib,stock,'SHORT',100.6,pd.Timestamp('2026-10-13 10:00',tz='America/New_York'))
    assert contract['expiry']=='20261023' and contract['actual_dte']==10
    assert contract['strike']==101. and contract['right']=='P'


def test_backtest_ui_defaults_to_options_and_sends_premium_limits():
    result=run_backtest([PLAN],{'TEST':candles()},OPTIONS,now=NOW,option_bars_provider=loader())
    with patch('backtester.pine_ui.available_plans',return_value=([PLAN],[])), \
         patch('backtester.pine_ui.load_result',return_value=None), \
         patch('backtester.pine_ui.execute_backtest',return_value=result) as execute:
        app=AppTest.from_string('from backtester.pine_ui import render\nrender({})').run(timeout=20)
        assert app.radio(key='pine_bt_instrument').value=='OPT'
        assert not any(widget.key=='pine_bt_risk_dollars' for widget in app.number_input)
        app.number_input(key='pine_bt_option_premium_budget').set_value(400.).run(timeout=20)
        app.button(key='pine_bt_run').click().run(timeout=20)
        assert not app.exception
        payload=execute.call_args.args[0]
        assert payload['settings']['instrument']=='OPT'
        assert payload['settings']['option_dte']==7
        assert payload['settings']['option_premium_budget']==400.
        assert payload['settings']['option_allow_calls'] and payload['settings']['option_allow_puts']
        assert app.session_state['pine_bt_result']['meta']['instrument']=='OPT'


def test_live_setup_can_save_option_mode_without_changing_execution_permissions():
    cfg=dict(pine_stocks=dict(enabled=True,settings={'execution':'Simulation'}),
        automation=dict(enabled=False,place_orders=False),account_mode='Simulation')
    script=f'from modules.pine_stock_ui import render_editor\nrender_editor({cfg!r},section="Setup")'
    with patch('modules.pine_stock_ui.load_plan',return_value=LIVE_PLAN), \
         patch('bot_core.load_config',return_value=deepcopy(cfg)), \
         patch('bot_core.save_config') as save:
        app=AppTest.from_string(script).run(timeout=20)
        app.selectbox(key='pine_setup_instrument').set_value('OPT').run(timeout=20)
        next(button for button in app.button if button.label=='Save stock strategy settings').click().run(timeout=20)
        assert not app.exception
        saved=save.call_args.args[0]
        assert saved['pine_stocks']['settings']['instrument']=='OPT'
        assert saved['pine_stocks']['settings']['execution']=='Simulation'
        assert saved['pine_stocks']['settings']['option_dte']==7
        assert saved['automation']==cfg['automation']
        assert saved['account_mode']==cfg['account_mode']


def test_worker_loads_actual_option_history_and_saves_option_export(tmp_path):
    from backtester.pine_worker import run_request
    from backtester.pine import save_result,load_result
    from test_pine_backtest import SOURCE
    payload=dict(sources=[SOURCE],start='2026-10-12',end='2026-10-12',starting_capital=10000.,
        settings={'instrument':'OPT','max_entry_slippage_pct':0.},force_refresh=True)
    research=Mock()
    research.historical_option_bars=loader()
    with patch('backtester.pine_worker.IBKRMarketDataProvider') as underlying, \
         patch('backtester.pine_worker.load_candles',return_value=({'TEST':candles()},[])), \
         patch('providers.ibkr_provider.IBKRMarketDataProvider',return_value=research) as factory, \
         patch('backtester.pine_worker.save_result',side_effect=lambda result:save_result(result,tmp_path)):
        meta=run_request(payload)
    assert factory.call_args.args[0].readonly is True
    assert research.historical_option_bars.call_args.kwargs['force_refresh'] is True
    research.disconnect.assert_called_once()
    assert meta['instrument']=='OPT'
    assert load_result(tmp_path)['trades'].iloc[0].instrument=='OPT'
