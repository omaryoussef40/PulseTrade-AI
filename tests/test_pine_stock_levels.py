import copy
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
import pytest
from modules.pine_levels import parse_pine, save_plan, load_plan, StockLevels, DailyPlan
from modules.pine_stock_signals import Settings, confirmed_signal, validate_quote, share_quantity, target_legs, bounds, entry_allowed
from modules.pine_stock_engine import Ledger, StockEngine, IBStockBroker, broker_orders_allowed, money

SOURCE=(Path(__file__).parents[1]/'examples/daily_trade_levels_2026-10-08.pine').read_text()
LEVEL=StockLevels('TEST',100,99,102,104,98,99,96,94)
PLAN=DailyPlan(date(2026,10,8),(LEVEL,),'hash','test')
NOW=pd.Timestamp('2026-10-08 09:40:01',tz='America/New_York')


def bars(closes=(99,100.5),start='2026-10-08 09:30'):
    return pd.DataFrame([dict(Open=c-.2,High=c+.2,Low=c-.4,Close=c,Volume=1000) for c in closes],index=pd.date_range(start,periods=len(closes),freq='5min',tz='America/New_York'))


def quote(now=NOW,**kw):
    return dict(bid=100.49,ask=100.51,timestamp=now.isoformat(),market_data_type=1,**kw)


def test_attached_plan_and_round_trip(tmp_path):
    p=parse_pine(SOURCE)
    assert p.session_date==date(2026,10,8)
    assert len(p.levels)==10
    assert [r.symbol for r in p.levels]==sorted(r.symbol for r in p.levels)
    assert p.levels[0].longEntry==45.7
    path=tmp_path/'plan.json'
    save_plan(p,path)
    assert load_plan(path)==p
    d=json.loads(path.read_text());d['levels'][0]['longEntry']=0
    path.write_text(json.dumps(d))
    with pytest.raises(ValueError):load_plan(path)


@pytest.mark.parametrize('prefix',['activeDay','bool activeDay','var bool activeDay'])
@pytest.mark.parametrize('multiline',[False,True])
def test_explicit_date_accepts_typed_and_wrapped_definitions(prefix,multiline):
    original=next(line for line in SOURCE.splitlines() if line.startswith('activeDay ='))
    expression=original.split('=',1)[1].strip()
    replacement=prefix+' = '+('('+expression.replace(' and ',' and\n    ')+')' if multiline else expression)
    script=SOURCE.replace(original,replacement)
    plan=parse_pine(script)
    assert plan.session_date==date(2026,10,8)
    assert plan.levels==parse_pine(SOURCE).levels
    assert plan.source==script


def test_missing_duplicate_and_unsupported_dates_have_distinct_errors():
    original=next(line for line in SOURCE.splitlines() if line.startswith('activeDay ='))
    with pytest.raises(ValueError,match='No activeDay definition'):
        parse_pine(SOURCE.replace(original,''))
    with pytest.raises(ValueError,match='Multiple activeDay definitions'):
        parse_pine(SOURCE+'\nbool '+original)
    with pytest.raises(ValueError,match='supported activeDay date'):
        parse_pine(SOURCE.replace(original,'bool activeDay = true'))


def test_indented_paste_preserves_ticker_block_structure():
    script='\n'.join('    '+line for line in SOURCE.splitlines())
    assert parse_pine(script).levels==parse_pine(SOURCE).levels


@pytest.mark.parametrize('old,new', [('longEntry := 45.70','longEntry := 45.70 + 1'),('longStop := 44.80','longStop := 99'),('close > vwapLine','close >= vwapLine'),('month(time, "America/New_York") == 10','month(time, "America/New_York") == 99')])
def test_reject_bad_pine(old,new):
    assert old in SOURCE
    with pytest.raises(ValueError):parse_pine(SOURCE.replace(old,new))


def test_duplicate_and_missing_blocks():
    block='\nif syminfo.ticker == "APA"\n'+'\n'.join(f'    {f} := {getattr(parse_pine(SOURCE).levels[0],f)}' for f in ('longEntry','longStop','longT1','longT2','shortEntry','shortStop','shortT1','shortT2'))
    with pytest.raises(ValueError):parse_pine(SOURCE+block)
    with pytest.raises(ValueError):parse_pine(SOURCE.replace('    longEntry := 45.70',''))


def test_completed_cross_vwap_and_future():
    assert confirmed_signal(PLAN,LEVEL,bars(),NOW,Settings())['side']=='LONG'
    with pytest.raises(ValueError,match='stale'):
        confirmed_signal(PLAN,LEVEL,bars(),NOW-pd.Timedelta(minutes=1),Settings())
    future=bars((99,100.5,80))
    assert confirmed_signal(PLAN,LEVEL,future,NOW,Settings())['close']==100.5
    assert confirmed_signal(PLAN,LEVEL,bars((100.3,100.5)),NOW,Settings()) is None
    f=bars();f['High']=150
    assert confirmed_signal(PLAN,LEVEL,f,NOW,Settings()) is None


def test_short_is_explicit():
    f=bars((99,97.5))
    assert confirmed_signal(PLAN,LEVEL,f,NOW,Settings()) is None
    assert confirmed_signal(PLAN,LEVEL,f,NOW,Settings(enable_shorts=True))['side']=='SHORT'


def test_first_candle_cross_uses_previous_session():
    f=pd.concat([bars((99,),start='2026-10-07 15:55'),bars((100.5,))])
    assert confirmed_signal(PLAN,LEVEL,f,NOW-pd.Timedelta(minutes=5),Settings())['side']=='LONG'
    with pytest.raises(ValueError,match='Previous session'):confirmed_signal(PLAN,LEVEL,bars((100.5,)),NOW-pd.Timedelta(minutes=5),Settings())


@pytest.mark.parametrize('change', ['date','naive','stale','missing','ohlc','zero_volume'])
def test_invalid_signal_data(change):
    f=bars();now=NOW
    if change=='date':now+=pd.Timedelta(days=1)
    if change=='naive':f.index=f.index.tz_localize(None)
    if change=='stale':now+=pd.Timedelta(minutes=2)
    if change=='missing':f.index+=pd.Timedelta(minutes=5);now+=pd.Timedelta(minutes=5)
    if change=='ohlc':f.loc[f.index[0],'High']=1
    if change=='zero_volume':f['Volume']=0
    with pytest.raises(ValueError):confirmed_signal(PLAN,LEVEL,f,now,Settings())


def test_session_boundaries_and_holiday():
    assert bounds(pd.Timestamp('2026-10-10',tz='America/New_York')) is None
    assert bounds(pd.Timestamp('2026-12-25',tz='America/New_York')) is None
    early=pd.Timestamp('2026-11-27 13:01',tz='America/New_York')
    assert not entry_allowed(early,Settings())
    assert bounds(early)[1].hour==13
    assert bounds(pd.Timestamp('2026-03-09',tz='America/New_York'))[0].utcoffset().total_seconds()==-14400


@pytest.mark.parametrize('bad', [dict(bid=102,ask=100),dict(bid=0),dict(ask=float('nan')),dict(timestamp=(NOW-pd.Timedelta(seconds=31)).isoformat()),dict(timestamp=(NOW+pd.Timedelta(seconds=1)).isoformat()),dict(market_data_type=3),dict(ask=105)])
def test_bad_quotes(bad):
    q=quote();q.update(bad)
    with pytest.raises(ValueError):validate_quote(q,NOW,Settings())


def test_shares_and_rounding():
    s=dict(side='LONG',levels=LEVEL.side('LONG'))
    assert share_quantity(s,100,Settings())==50
    assert share_quantity(s,100,Settings(),available_funds=250)==2
    assert share_quantity(s,100,Settings(),risk_used=299)==1
    with pytest.raises(ValueError):share_quantity(s,102,Settings())
    assert target_legs(5,Settings())==[(2,'t1'),(3,'t2')]
    assert target_legs(1,Settings())==[(1,'t1')]
    assert money(100.001,True)==100.01
    assert money(100.009)==100


class Provider:
    def __init__(self):self.q=quote();self.f=bars()
    def quote(self,symbol):return self.q
    def bars(self,symbol,minutes):assert minutes==5;return self.f
    def tick(self,symbol):return .01


@pytest.mark.parametrize('minutes',[1,15,30])
def test_engine_always_requests_five_minute_confirmation(minutes,tmp_path):
    ledger=Ledger(tmp_path/'ledger.sqlite')
    try:
        engine=StockEngine(Settings(bar_minutes=minutes),ledger,Provider())
        result=engine.cycle(PLAN,NOW)
        assert engine.settings.bar_minutes==5
        assert result['positions'][0]['signal']['candle_end']=='2026-10-08T09:40:00-04:00'
        with pytest.raises(ValueError,match='five-minute'):
            confirmed_signal(PLAN,LEVEL,bars(),NOW,Settings(bar_minutes=minutes))
    finally:
        ledger.close()


@pytest.mark.parametrize('closes,high,low',[
    ((99,99.8),100.5,99.4),
    ((99,98.2),99.1,97.5),
    ((99,100),100.5,99.4),
    ((99,98),99.1,97.5),
])
def test_wick_or_close_on_level_cannot_confirm_entry(closes,high,low):
    frame=bars(closes)
    frame.loc[frame.index[-1],['High','Low']]=[high,low]
    assert confirmed_signal(PLAN,LEVEL,frame,NOW,Settings(enable_shorts=True)) is None


@pytest.mark.parametrize('risk',[10.,100.])
def test_dollar_risk_sizes_shares_but_keeps_pine_exits(risk,tmp_path):
    ledger=Ledger(tmp_path/'ledger.sqlite')
    try:
        provider=Provider()
        engine=StockEngine(Settings(risk_dollars=risk,max_daily_loss=1.),ledger,provider)
        row=engine.cycle(PLAN,NOW)['positions'][0]
        assert row['levels']==LEVEL.side('LONG')
        assert row['initial_quantity']==int(min(risk/(row['entry_price']-99),5000/row['entry_price']))
        # Entry fees exceed the daily new-entry loss gate; the position still
        # closes only at its Pine stop/targets.
        later=NOW+pd.Timedelta(seconds=20)
        provider.q=dict(bid=101.,ask=101.01,timestamp=later.isoformat())
        row=engine.cycle(PLAN,later)['positions'][0]
        assert row['status']=='SIM_OPEN' and not row['fills']
        assert row['levels']==LEVEL.side('LONG')
    finally:
        ledger.close()


def test_simulated_trade_restart_targets_and_fees(tmp_path):
    path=tmp_path/'ledger.sqlite';ledger=Ledger(path);provider=Provider()
    engine=StockEngine(Settings(),ledger,provider)
    result=engine.cycle(PLAN,NOW)
    row=result['positions'][0]
    assert row['quantity']>1 and row['status']=='SIM_OPEN'
    assert row['realized']==-2
    ledger.close();ledger=Ledger(path);engine=StockEngine(Settings(),ledger,provider)
    assert engine.cycle(PLAN,NOW)['decisions'][0]['status']=='BLOCKED'
    later=NOW+pd.Timedelta(seconds=20)
    provider.q=dict(bid=102,ask=102.01,timestamp=later.isoformat())
    row=engine.cycle(PLAN,later,manage_only=True)['positions'][0]
    assert 0<row['quantity']<row['initial_quantity']
    later+=pd.Timedelta(seconds=20);provider.q=dict(bid=104,ask=104.01,timestamp=later.isoformat())
    row=engine.cycle(PLAN,later,manage_only=True)['positions'][0]
    assert row['status']=='CLOSED' and row['fees']==4
    assert [f['reason'] for f in row['fills']]==['T1','T2']
    assert row['reserved_risk']==0
    ledger.close()


@pytest.mark.parametrize('settings', [Settings(kill_switch=True),Settings(max_daily_loss=.5),Settings(max_total_risk=.01)])
def test_entry_risk_blocks(settings,tmp_path):
    ledger=Ledger(tmp_path/'l.sqlite');p=Provider()
    if settings.max_daily_loss==.5:
        ledger.claim(dict(key='old',book='Simulation',day='2026-10-08',symbol='OTHER',status='CLOSED',realized=-1))
    result=StockEngine(settings,ledger,p).cycle(PLAN,NOW)
    assert result['decisions'][0]['status']=='BLOCKED'
    ledger.close()


def test_expired_plan_no_entry(tmp_path):
    ledger=Ledger(tmp_path/'l.sqlite');p=Provider()
    result=StockEngine(Settings(),ledger,p).cycle(PLAN,NOW+pd.Timedelta(days=1))
    assert not result['positions']
    ledger.close()


def test_stock_brackets_only_and_gates():
    orders=[];ids=iter(range(1,30))
    ib=SimpleNamespace(client=SimpleNamespace(getReqId=lambda:next(ids)),placeOrder=lambda c,o:orders.append((c,o)))
    p=SimpleNamespace(ib=ib,account='DU123',contract=lambda s:SimpleNamespace(secType='STK',symbol=s))
    broker=IBStockBroker(p)
    row=dict(key='key',symbol='TEST',side='LONG',entry_price=100,levels=LEVEL.side('LONG'),legs=[dict(quantity=5,target='t1'),dict(quantity=5,target='t2')])
    broker.submit(broker.prepare(row))
    assert len(orders)==6 and all(c.secType=='STK' for c,o in orders)
    for start in [0,3]:
        parent,target,stop=[o for c,o in orders[start:start+3]]
        assert parent.tif=='FOK' and not parent.transmit
        assert target.parentId==stop.parentId==parent.orderId
        assert stop.transmit and stop.auxPrice==99
        assert target.ocaGroup==stop.ocaGroup and stop.ocaType==2
    app=dict(account_mode='Paper',ib=dict(readonly=False),automation=dict(enabled=True,place_orders=True,confirm_order_risk=True))
    assert broker_orders_allowed(app,Settings(execution='IBKR Paper'))
    assert not broker_orders_allowed(app,Settings())
    app['account_mode']='Live';app['automation']['live_confirm_text']='TRADE LIVE'
    assert not broker_orders_allowed(app,Settings(execution='IBKR Live'))
    assert broker_orders_allowed(app,Settings(execution='IBKR Live',live_confirmation='TRADE STOCK LEVELS LIVE'))


def test_engine_dispatch_uses_stock_route():
    import engine
    cfg=dict(pine_stocks=dict(enabled=True),automation=dict(enabled=True))
    with patch.object(engine,'load_config',return_value=cfg),patch('modules.pine_stock_engine.run_pine_cycle') as run:
        engine.run_cycle();engine.run_broker_sync_cycle();engine.run_cycle(opening_trade_only=True)
        assert run.call_count==2
    cfg['automation']['enabled']=False
    with patch.object(engine,'load_config',return_value=cfg),patch('modules.pine_stock_engine.run_pine_cycle') as run:
        engine.run_cycle();engine.run_broker_sync_cycle()
        run.assert_not_called()


def test_stop_after_restart_even_on_expired_plan(tmp_path):
    ledger=Ledger(tmp_path/'l.sqlite');p=Provider();e=StockEngine(Settings(),ledger,p)
    e.cycle(PLAN,NOW)
    later=NOW+pd.Timedelta(days=1)
    p.q=dict(bid=98.5,ask=98.51,timestamp=later.isoformat())
    row=e.cycle(PLAN,later)['positions'][0]
    assert row['status']=='CLOSED' and row['quantity']==0
    assert all(f['reason']=='STOP' and f['price']<99 for f in row['fills'])
    ledger.close()


def test_failed_broker_submission_stays_claimed(tmp_path):
    ledger=Ledger(tmp_path/'l.sqlite');p=Provider()
    p.available_funds=lambda:10000;p.has_stock_exposure=lambda s:False
    b=SimpleNamespace(prepare=lambda r:r,submit=lambda r:(_ for _ in ()).throw(RuntimeError('connection lost')),reconcile=lambda r:r)
    e=StockEngine(Settings(execution='IBKR Paper'),ledger,p,b)
    first=e.cycle(PLAN,NOW,orders_allowed=True)['positions'][0]
    assert first['status']=='UNKNOWN'
    assert e.cycle(PLAN,NOW,orders_allowed=True)['decisions'][0]['status']=='BLOCKED'
    assert len(ledger.rows('Simulation'))==1
    ledger.close()


def test_locked_broker_does_not_submit(tmp_path):
    ledger=Ledger(tmp_path/'l.sqlite');p=Provider()
    e=StockEngine(Settings(execution='IBKR Paper'),ledger,p,SimpleNamespace(submit=lambda r:pytest.fail('Order submitted')))
    assert e.cycle(PLAN,NOW)['decisions'][0]['status']=='BLOCKED'
    assert ledger.rows('Simulation')==[]
    ledger.close()


@pytest.mark.parametrize('field,value',[('risk_dollars',0),('simulation_fee_per_share',-1),('max_positions',1.5),('t1_fraction',1),('bar_minutes',2),('entry_end','08:00')])
def test_invalid_settings(field,value):
    with pytest.raises(ValueError):Settings(**{field:value})


def test_unprotected_broker_position_freezes():
    parent=SimpleNamespace(order=SimpleNamespace(account='DU123',orderRef='ref-entry'),orderStatus=SimpleNamespace(filled=5,status='Filled',avgFillPrice=100),fills=[])
    ib=SimpleNamespace(trades=lambda:[parent],reqAllOpenOrders=lambda:[],reqCompletedOrders=lambda **kw:[])
    b=IBStockBroker(SimpleNamespace(ib=ib,account='DU123'))
    row=dict(initial_quantity=5,entry_price=100,side='LONG',levels=LEVEL.side('LONG'),legs=[dict(quantity=5,ref='ref')])
    assert b.reconcile(row)['status']=='UNKNOWN'


def test_ui_preview_simulation_and_expiry():
    from streamlit.testing.v1 import AppTest
    script="from modules.pine_stock_ui import render\nrender({})"
    with patch('modules.pine_stock_charts.chart_snapshot',return_value=dict(source='IBKR',fetched_at=NOW.isoformat(),candles={},errors={})):
        at=AppTest.from_string(script).run(timeout=20)
        at.radio[0].set_value('Paste & settings').run()
    assert not at.exception
    assert len(at.dataframe[0].value)==10
    assert next(widget for widget in at.selectbox if widget.label=='Execution mode').value=='Simulation'
    assert not at.checkbox[0].value
    at.text_area[0].set_value('invalid Pine').run()
    assert at.error and at.button[0].disabled


def test_daily_realized_limits_do_not_credit_overnight_gains():
    from modules.pine_stock_engine import risk_snapshot
    row=dict(status='SIM_OPEN',quantity=5,symbol='TEST',side='LONG',entry_price=100,reserved_risk=5,day='2026-10-07',daily_realized={'2026-10-08':-301})
    result=risk_snapshot([row],'2026-10-08',{'TEST':dict(bid=110,ask=110.01)},Settings())
    assert result['pnl']==-301
    result=risk_snapshot([row],'2026-10-08',{'TEST':dict(bid=98,ask=98.01)},Settings())
    assert result['pnl']==-311


def test_completed_bar_cache_retries_missing_latest():
    from modules.pine_stock_engine import IBStockProvider,_BAR_CACHE
    _BAR_CACHE.clear()
    requests=[]
    p=IBStockProvider.__new__(IBStockProvider)
    p.now=lambda:NOW
    p.data=SimpleNamespace(intraday_bars=lambda symbol,**kw:(requests.append(kw) or bars()))
    assert len(p.bars('TEST',5))==2
    p.bars('TEST',5)
    assert len(requests)==1
    _BAR_CACHE.clear()
    p.data=SimpleNamespace(intraday_bars=lambda symbol,**kw:(requests.append(kw) or bars((99,))))
    p.bars('TEST',5);p.bars('TEST',5)
    assert len(requests)==3
    _BAR_CACHE.clear()


def test_signal_override_rejected():
    with pytest.raises(ValueError):parse_pine(SOURCE+'\nlongSignal := true\n')
    with pytest.raises(ValueError):parse_pine(SOURCE+'\nactiveDay = true\n')
