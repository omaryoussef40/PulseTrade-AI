"""Explicit selection and saved budgets for automatic entry strategies."""
from copy import deepcopy
from math import isfinite
import re

LABELS={'pine_stocks':'Daily Pine Levels','pmb':'PMB — options','brt':'Break & Retest — options','dual':'PMB options + Daily Pine','paused':'Pause automatic entries'}


def active_profile(config):
    controls=config.get('strategy_control',{})
    if controls.get('enabled'):
        value=controls.get('active','paused')
        if value not in LABELS:raise ValueError('Unknown active strategy. Automatic entries are blocked.')
        return value
    return 'pine_stocks' if config.get('pine_stocks',{}).get('enabled') else config.get('strategy',{}).get('active_strategy','pmb')


def default_option_profile(config):
    risk=config.get('risk',{})
    return dict(daily_capital=float(risk.get('max_daily_capital',500)),per_trade_capital=float(risk.get('max_spend_per_trade',200)),max_daily_trades=int(risk.get('max_trades_per_day',2)),all_watchlist=True,symbols=list(config.get('watchlist',[])))


def validate_option_profile(profile):
    for key in ('daily_capital','per_trade_capital','max_daily_trades'):
        if not isfinite(profile[key]) or profile[key]<=0:raise ValueError(f'Invalid {key}.')
    if int(profile['max_daily_trades'])!=profile['max_daily_trades']:raise ValueError('Daily trade limit must be a whole number.')
    if not isinstance(profile.get('symbols'),(list,tuple)) or any(not re.fullmatch(r'[A-Z0-9][A-Z0-9.-]{0,19}',s) for s in profile['symbols']):raise ValueError('Invalid approved ticker list.')


def effective_config(config):
    cfg=deepcopy(config)
    controls=cfg.get('strategy_control',{})
    if not controls.get('enabled'):return cfg
    active=active_profile(cfg)
    cfg['_entries_paused']=active=='paused'
    concurrent=active=='dual' or (active=='paused' and controls.get('last_active')=='dual')
    cfg['_concurrent_strategies']=concurrent
    if concurrent:
        validate_allocation(controls.get('allocation',{}))
        cfg.setdefault('pine_stocks',{})['enabled']=True
        stock=cfg['pine_stocks'].setdefault('settings',{})
        stock.update(account_allocation_pct=controls.get('allocation',{}).get('stocks_pct',20.),margin_multiplier=1. if stock.get('instrument')=='OPT' else controls.get('allocation',{}).get('stock_margin',2.))
        cfg.setdefault('strategy',{})['active_strategy']='pmb'
        cfg.setdefault('risk',{})['max_daily_capital_pct']=controls.get('allocation',{}).get('pmb_pct',80.)
        return cfg
    cfg.setdefault('opening_orb_trade',{})['enabled']=False
    cfg.setdefault('staged_trading_timeline',{})['enabled']=False
    if active=='paused':return cfg
    cfg.setdefault('pine_stocks',{})['enabled']=active=='pine_stocks'
    if active=='pine_stocks':return cfg
    profile=controls.get('profiles',{}).get(active,default_option_profile(cfg))
    validate_option_profile(profile)
    cfg.setdefault('strategy',{})['active_strategy']=active
    cfg.setdefault('risk',{}).update(max_daily_capital=profile['daily_capital'],max_spend_per_trade=profile['per_trade_capital'],max_trades_per_day=int(profile['max_daily_trades']),max_daily_capital_pct=0,max_spend_per_trade_pct=0,recycle_capital_after_exit=False)
    cfg.setdefault('order',{})['type']='LIMIT'
    if not profile.get('all_watchlist',True):
        cfg['watchlist']=list(profile['symbols'])
        cfg.setdefault('dynamic_watchlist',{})['enabled']=False
    return cfg


def check_automated_option_entry(config,order,trades_today,deployed_today,*,account_size=None):
    if not config.get('strategy_control',{}).get('enabled'):return
    active=active_profile(config)
    if active=='dual':active='pmb'
    if active not in ('pmb','brt'):raise ValueError('Automatic option entries are disabled by the selected strategy.')
    if order.get('strategy_profile')!=active:raise ValueError('This approval belongs to a different or older strategy. Rescan before trading.')
    if str(order.get('source','')).lower()=='opening_orb':raise ValueError('Opening ORB entries are disabled by strategy controls.')
    effective=effective_config(config);risk=effective['risk']
    symbol=str(order.get('symbol','')).upper()
    profile=config['strategy_control'].get('profiles',{}).get(active,default_option_profile(config))
    if not profile.get('all_watchlist',True) and symbol not in profile['symbols']:raise ValueError('Ticker is not approved for this strategy.')
    price=float(order.get('limit_price') or order.get('mid') or 0)
    cost=price*int(order.get('quantity') or 0)*100
    if str(order.get('order_type','LIMIT')).upper()!='LIMIT' or not isfinite(cost) or cost<=0:raise ValueError('Controlled strategy entries require a valid limit price.')
    daily=float(risk['max_daily_capital']);per_trade=float(risk['max_spend_per_trade'])
    if effective.get('_concurrent_strategies'):
        if account_size is None or not isfinite(account_size) or account_size<=0:
            raise ValueError('Verified account equity required for concurrent option entries.')
        daily=account_size*float(risk['max_daily_capital_pct'])/100
        if float(risk.get('max_spend_per_trade_pct',0))>0:
            per_trade=account_size*float(risk['max_spend_per_trade_pct'])/100
    if trades_today>=risk['max_trades_per_day'] or cost>per_trade+1e-8 or deployed_today+cost>daily+1e-8:raise ValueError('Strategy daily budget, per-trade budget or trade count is exhausted.')


def validate_allocation(allocation):
    values=[allocation.get('pmb_pct',80.),allocation.get('stocks_pct',20.),allocation.get('stock_margin',2.)]
    if any(not isinstance(v,(int,float)) or not isfinite(v) for v in values) or not 0<values[0]<=100 or not 0<values[1]<=100 or values[0]+values[1]>100 or not 1<=values[2]<=2:
        raise ValueError('Allocations must total at most 100%; stock margin must be between 1× and 2×.')


def verified_account_equity(ib,account=None):
    """Account-specific USD equity; never use leveraged buying power as equity."""
    accounts=ib.managedAccounts()
    account=account or (accounts[0] if len(accounts)==1 else None)
    if not account or account not in accounts:
        raise ValueError('Select one connected IBKR account for concurrent strategies.')
    values=[float(row.value) for row in ib.accountSummary() if row.account==account and row.tag=='NetLiquidation' and row.currency=='USD']
    if len(values)!=1 or not isfinite(values[0]) or values[0]<=0:
        raise ValueError('Verified USD account equity unavailable; concurrent entries are blocked.')
    return values[0]
