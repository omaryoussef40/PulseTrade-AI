"""Navigation and read-only summaries for the trading workspace."""
from datetime import datetime
from contextlib import closing
from pathlib import Path
import json
import math
import sqlite3
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT=Path(__file__).resolve().parents[1]
PAGES={'overview':'Overview','strategies':'Strategies','live-trading':'Live Trading','backtesting':'Backtesting','journal':'Trade Journal','research':'Market Research','settings':'Settings'}
LEGACY={'scanner':('strategies','PMB'),'daily-stock-levels':('strategies','Daily Stocks'),'account':('settings','Connection'),'logs':('settings','Logs'),'ai-audit':('journal','AI Audit'),'market-intel':('research','News & intelligence'),'earnings':('research','Earnings'),'positions':('live-trading','Positions'),'strategy':('backtesting',None),'price-action':('strategies','PMB')}


def resolve_page(slug):
    return (slug,None) if slug in PAGES else LEGACY.get(slug,('overview',None))


def read_json(path):
    try:
        value=json.loads(Path(path).read_text())
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):
        return {}


def stock_positions(config,account=None,*,all_books=False):
    path=ROOT/'data'/'pine_stock_trades.sqlite'
    if not path.exists():return []
    execution=config.get('pine_stocks',{}).get('settings',{}).get('execution','Simulation')
    try:
        with closing(sqlite3.connect(f'file:{path}?mode=ro',uri=True)) as db:
            # This view must never create a ledger or change an order intent.
            records=db.execute("SELECT payload FROM trades WHERE status!='CLOSED'").fetchall()
        rows=[json.loads(row[0]) for row in records]
        if all_books:return rows
        return [row for row in rows if row.get('book','').startswith(execution+':') and (not account or row.get('book')==execution+':'+account)]
    except (sqlite3.Error,OSError,ValueError):
        if all_books:raise ValueError('Stock ledger unavailable; execution changes are blocked.') from None
        return []


def number(value):
    try:
        result=float(value)
        return result if math.isfinite(result) else None
    except (TypeError,ValueError):return None


def money(value):
    value=number(value)
    return '—' if value is None else f'${value:,.2f}'


def strategy_status(config,health,stock_status=None):
    from .strategy_controls import active_profile,effective_config
    try:
        profile=active_profile(config);effective=effective_config(config)
    except ValueError:
        return {'pmb':'Configuration needs attention','pine_stocks':'Configuration needs attention'}
    stock_status=stock_status or {}
    fresh_stock=False
    try:
        checked=pd.Timestamp(stock_status.get('timestamp'))
        fresh_stock=checked.tzinfo is not None and 0<=(pd.Timestamp.now(tz='America/New_York')-checked).total_seconds()<=90
    except (TypeError,ValueError):pass
    running=bool(health.get('engine_running')) and bool(config.get('automation',{}).get('enabled'))
    paused=profile=='paused'
    return {
        'pmb':'Paused' if paused else 'Scanning' if running and profile in ('pmb','dual') else 'Inactive',
        'pine_stocks':'Paused' if paused else 'Monitoring' if running and effective.get('pine_stocks',{}).get('enabled') and fresh_stock else 'Awaiting scan' if effective.get('pine_stocks',{}).get('enabled') else 'Inactive',
    }


def render_strategy_cards(config,health,stock_status=None):
    from .pine_levels import load_plan
    from .strategy_controls import effective_config
    try:display=effective_config(config)
    except ValueError as exc:
        st.error(str(exc))
        return
    status=strategy_status(config,health,stock_status)
    allocation=config.get('strategy_control',{}).get('allocation',{})
    combined=bool(display.get('_concurrent_strategies'))
    stock_status=stock_status or {}
    left,right=st.columns(2)
    with left,st.container(border=True):
        st.subheader('PMB')
        st.caption('OPTIONS')
        st.write(f"**{status['pmb']}**")
        pct=allocation.get('pmb_pct',80) if combined else display.get('risk',{}).get('max_daily_capital_pct',0)
        st.write(f'{pct:g}% capital cap' if pct else money(display.get('risk',{}).get('max_daily_capital'))+' capital cap')
        strategy=config.get('strategy',{})
        from strategies.pmb.candles import CONFIRMATION_MODES, confirmation_mode, follow_through_enabled
        follow=strategy.get('breakout_follow_through',{})
        confirmation=CONFIRMATION_MODES[confirmation_mode(follow)] if follow_through_enabled(follow) else 'Off'
        st.caption(f"ORB {strategy.get('orb_minutes',15)} min · Confirmation: {confirmation} · Score ≥ {strategy.get('min_score',70)}")
        st.caption(f"Last scan: {health.get('last_scan_finish') or 'Awaiting first scan'}")
    with right,st.container(border=True):
        stock=display.get('pine_stocks',{}).get('settings',{})
        pine_options=stock.get('instrument')=='OPT'
        st.subheader('Daily Pine')
        st.caption('7-DTE CALLS / PUTS' if pine_options else 'SHARES')
        st.write(f"**{status['pine_stocks']}**")
        pct=allocation.get('stocks_pct',20) if combined else stock.get('account_allocation_pct',0)
        margin=1 if pine_options else allocation.get('stock_margin',2) if combined else stock.get('margin_multiplier',1)
        st.write(f'{pct:g}% equity allocation · {margin:g}× margin' if pct else 'Saved option premium limits' if pine_options else 'Saved stock position limits')
        if pct:st.caption(f'Maximum stock position value: {pct*margin:g}% of equity')
        try:
            plan=load_plan();st.caption(f'Plan date: {plan.session_date}' if plan else 'No daily plan saved')
        except ValueError:st.caption('Daily plan needs review')
        st.caption(f"Last check: {stock_status.get('timestamp') or 'Awaiting first scan'}")


def render_overview(config,health,account,options):
    from .pine_levels import load_plan
    from .pine_stock_engine import broker_orders_allowed
    from .pine_stock_signals import Settings
    st.header('Overview')
    st.caption('Your account and strategies at a glance.')
    account=account or {}
    stocks=stock_positions(config,account.get('account_id'))
    exposure=sum((number(row.get('entry_price')) or 0)*(number(row.get('quantity')) or 0)*100 for row in options)
    exposure+=sum((number(row.get('entry_price')) or 0)*(number(row.get('quantity')) or 0) for row in stocks)
    cols=st.columns(4)
    for col,label,value in zip(cols,['Account equity','Realized P&L','Open exposure','Available funds'],[account.get('NetLiquidation'),account.get('RealizedPnL'),exposure,account.get('AvailableFunds')]):
        col.metric(label,money(value))
    st.caption(f"Account snapshot: {account.get('fetched_at','Unavailable')}")
    st.caption('Open exposure uses entry value for tracked positions. Account figures use the latest IBKR snapshot; unavailable figures show a dash.')
    render_strategy_cards(config,health,read_json(ROOT/'exports'/'pine_stock_status.json'))
    st.subheader('Attention needed')
    notices=[]
    if str(health.get('last_status','')).lower() == 'engine error':
        detail=str(health.get('last_error') or '').strip().splitlines()
        st.error('The trading engine is failing; automatic scans and entries may not run. '+(detail[-1] if detail else 'Review Live Trading → Controls and the engine logs.'))
    if not account.get('connected'):notices.append('IBKR account data is unavailable. Check Settings → Connection.')
    if not health.get('engine_running'):notices.append('The trading engine is stopped. Open Live Trading → Controls.')
    if not config.get('automation',{}).get('enabled'):notices.append('Automation is paused. Review Live Trading → Controls.')
    if config.get('pine_stocks',{}).get('enabled'):
        try:
            plan=load_plan()
            today=datetime.now(ZoneInfo('America/New_York')).date()
            if plan is None or plan.session_date!=today:notices.append(f'The live Daily Pine plan needs updating for the New York session ({today}). Saved plan: {plan.session_date if plan else "none"}. Open Strategies → Daily Stocks → Daily Plan. Backtest uploads do not replace the live plan.')
        except ValueError:notices.append('The saved stock plan needs review. Open Strategies → Daily Stocks → Daily Plan.')
        settings=Settings(**config.get('pine_stocks',{}).get('settings',{}))
        if settings.execution!='Simulation' and not broker_orders_allowed(config,settings):notices.append('Daily Pine broker orders are locked. Review Live Trading → Controls. This warning applies to Daily Pine, separately from PMB.')
    for notice in notices:st.warning(notice)
    if not notices and str(health.get('last_status','')).lower()!='engine error':st.success('No setup issues require your attention.')
    def open_positions():
        st.session_state['sidebar_selected_page']='live-trading'
        st.session_state['live_workspace']='Positions'
        st.query_params['page']='live-trading'
    st.button('Open live positions',on_click=open_positions,key='overview_open_positions')


def render_orders():
    st.subheader('Order history')
    # Use the existing log location.
    from bot_core import TRADE_LOG_FILE
    path=Path(TRADE_LOG_FILE)
    try:
        frame=pd.read_csv(path)
        columns=[c for c in ['timestamp','symbol','signal','event','quantity','entry_price','exit_price','status','source'] if c in frame]
        st.dataframe(frame[columns].tail(100).iloc[::-1],hide_index=True,width='stretch') if not frame.empty else st.info('No orders recorded yet.')
    except (OSError,ValueError):st.info('No orders recorded yet.')


def merge_settings_changes(original,edited,current):
    """Save edited leaves while preserving other screens' latest settings."""
    from copy import deepcopy
    result=deepcopy(current)
    for key,value in edited.items():
        if isinstance(value,dict) and isinstance(original.get(key),dict):
            result[key]=merge_settings_changes(original[key],value,result.get(key,{}))
        elif key not in original or value!=original[key]:
            result[key]=deepcopy(value)
    return result


def position_rows(broker,options,stocks,portfolio=None):
    from bot_core import is_manual_order_metadata
    portfolio=portfolio or {}
    rows=[]
    matched_stocks=set()
    for holding in broker:
        asset=str(holding.get('secType','')).upper()
        record=None
        if asset=='STK':
            record=next((row for row in stocks if row.get('instrument','STK')=='STK' and row.get('symbol')==holding.get('symbol') and (not holding.get('account') or row.get('book','').endswith(':'+holding['account']))),None)
            if record:matched_stocks.add(record['key'])
            strategy='Daily Stocks' if record else 'Other / manual'
        else:
            record=next((row for row in stocks if row.get('instrument')=='OPT' and row.get('contract',{}).get('conId') and str(row['contract']['conId'])==str(holding.get('conId')) and (not holding.get('account') or row.get('book','').endswith(':'+holding['account']))),None)
            if record:
                matched_stocks.add(record['key'])
                strategy='Daily Pine options'
            else:
                record=next((row for row in options if row.get('con_id') and str(row['con_id'])==str(holding.get('conId')) or row.get('option') and str(row['option']).replace(' ','')==str(holding.get('localSymbol','')).replace(' ','')),None)
                strategy='PMB' if record and record.get('strategy_profile','pmb')=='pmb' and not is_manual_order_metadata(record) else 'Other / manual'
        record=record or {}
        mark=portfolio.get('conid:'+str(holding.get('conId')),portfolio.get('local:'+str(holding.get('localSymbol','')).replace(' ','').upper(),{}))
        cost=number(holding.get('avgCost'))
        entry=cost/(100 if asset=='OPT' else 1) if cost is not None else None
        levels=record.get('levels',{})
        rows.append({'Strategy':strategy,'Symbol':holding.get('symbol'),'Instrument':'Shares' if asset=='STK' else 'Options' if asset=='OPT' else asset,'Quantity':holding.get('position'),'Entry':entry,'Current price':mark.get('market_price',record.get('current_price')),'P&L':mark.get('unrealized_pnl',record.get('unrealized_pnl')),'Stop':levels.get('stop',record.get('current_stop_price')),'Target 1':levels.get('t1',record.get('take_profit_price')),'Target 2':levels.get('t2'),'Status':record.get('status','Broker position')})
    for record in stocks:
        if record['key'] in matched_stocks:continue
        levels=record.get('levels',{})
        rows.append({'Strategy':'Daily Pine options' if record.get('instrument')=='OPT' else 'Daily Stocks','Symbol':record.get('symbol'),'Instrument':'Options' if record.get('instrument')=='OPT' else 'Shares','Quantity':record.get('quantity'),'Entry':record.get('entry_price'),'Current price':None,'P&L':None,'Stop':levels.get('stop'),'Target 1':levels.get('t1'),'Target 2':levels.get('t2'),'Status':record.get('status')})
    return rows
