"""Stock signal and risk calculations for imported daily levels."""
from dataclasses import dataclass,asdict
from functools import lru_cache
from datetime import time
from math import isfinite,floor
import re
import pandas as pd


@dataclass(frozen=True)
class Settings:
    execution: str = 'Simulation'
    instrument: str = 'STK'
    option_dte: int = 7
    option_premium_budget: float = 500
    option_max_total_premium: float = 1500
    option_max_daily_premium: float = 1500
    option_max_contracts: int = 5
    option_commission_per_contract: float = .65
    option_max_spread_pct: float = 10
    option_allow_calls: bool = True
    option_allow_puts: bool = True
    bar_minutes: int = 5
    risk_dollars: float = 100
    max_position_dollars: float = 5000
    max_total_dollars: float = 15000
    max_daily_capital: float = 15000
    account_allocation_pct: float = 0
    margin_multiplier: float = 1
    trade_all_plan_symbols: bool = True
    allowed_symbols: tuple[str,...] = ()
    enable_longs: bool = True
    max_positions: int = 3
    max_daily_trades: int = 5
    max_daily_loss: float = 300
    max_total_risk: float = 300
    entry_start: str = '09:35'
    entry_end: str = '15:30'
    quote_max_age_seconds: int = 30
    candle_max_age_seconds: int = 90
    max_spread_pct: float = .5
    max_entry_slippage_pct: float = .15
    simulation_fee_per_share: float = .005
    simulation_min_fee: float = 1
    t1_fraction: float = .5
    enable_shorts: bool = False
    kill_switch: bool = False
    live_confirmation: str = ''

    def __post_init__(self):
        if self.instrument not in ('STK','OPT') or self.option_dte != 7:
            raise ValueError('Daily Pine supports shares or 7-DTE long calls/puts.')
        for name in ('option_premium_budget','option_max_total_premium','option_max_daily_premium','option_max_contracts','option_max_spread_pct'):
            if not isfinite(getattr(self,name)) or getattr(self,name)<=0:
                raise ValueError(f'Invalid {name}.')
        if int(self.option_max_contracts)!=self.option_max_contracts or not isfinite(self.option_commission_per_contract) or self.option_commission_per_contract<0:
            raise ValueError('Invalid option contract cap or commission.')
        if not isfinite(self.account_allocation_pct) or not 0<=self.account_allocation_pct<=100 or not isfinite(self.margin_multiplier) or not 1<=self.margin_multiplier<=2:
            raise ValueError('Invalid stock allocation or margin multiplier.')
        if self.execution not in ('Simulation','IBKR Paper','IBKR Live') or self.bar_minutes not in (1,5,15,30):
            raise ValueError('Invalid execution mode or candle interval.')
        for name in ('risk_dollars','max_position_dollars','max_total_dollars','max_daily_capital','max_positions','max_daily_trades','max_daily_loss','max_total_risk','quote_max_age_seconds','candle_max_age_seconds','max_spread_pct'):
            if not isfinite(getattr(self,name)) or getattr(self,name)<=0:
                raise ValueError(f'Invalid {name}.')
        if not isinstance(self.allowed_symbols,(list,tuple)) or any(not isinstance(s,str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9.-]{0,19}',s) for s in self.allowed_symbols):
            raise ValueError('Invalid approved ticker list.')
        for name in ('simulation_fee_per_share','simulation_min_fee'):
            if not isfinite(getattr(self,name)) or getattr(self,name)<0:
                raise ValueError(f'Invalid {name}.')
        for name in ('max_positions','max_daily_trades','quote_max_age_seconds','candle_max_age_seconds'):
            if int(getattr(self,name))!=getattr(self,name):
                raise ValueError(f'{name} must be an integer.')
        if not 0<self.t1_fraction<1 or not 0<=self.max_entry_slippage_pct<100:
            raise ValueError('Invalid target split or slippage.')
        if time.fromisoformat(self.entry_start)>=time.fromisoformat(self.entry_end):
            raise ValueError('Invalid entry window.')


def aware(value):
    ts=pd.Timestamp(value)
    if ts.tzinfo is None or pd.isna(ts):
        raise ValueError('Timezone-aware timestamps required.')
    return ts.tz_convert('America/New_York')


@lru_cache(maxsize=8)
def calendar(year):
    import exchange_calendars as xcals
    return xcals.get_calendar('XNYS',start=f'{year-1}-01-01',end=f'{year+1}-12-31')


def bounds(asof):
    now=aware(asof)
    cal=calendar(now.year)
    day=pd.Timestamp(now.date())
    return (cal.session_open(day).tz_convert(now.tz),cal.session_close(day).tz_convert(now.tz)) if cal.is_session(day) else None


def entry_allowed(now,settings):
    now=aware(now)
    session=bounds(now)
    return bool(session and session[0]<now<session[1] and time.fromisoformat(settings.entry_start)<=now.time()<time.fromisoformat(settings.entry_end))


def validate_quote(q,now,settings):
    age=(aware(now)-aware(q['timestamp'])).total_seconds()
    if not 0<=age<=settings.quote_max_age_seconds or q.get('market_data_type',1)!=1:
        raise ValueError('Stale, future, frozen or delayed stock quote.')
    if not all(isfinite(q[k]) and q[k]>0 for k in ('bid','ask')) or q['bid']>q['ask']:
        raise ValueError('Invalid or crossed stock quote.')
    if (q['ask']-q['bid'])/((q['ask']+q['bid'])/2)*100>settings.max_spread_pct:
        raise ValueError('Stock spread too wide.')


def confirmed_signal(plan,levels,bars,now,settings):
    if settings.bar_minutes != 5:
        raise ValueError('Daily Pine entries require completed five-minute candles.')
    now=aware(now)
    if now.date()!=plan.session_date:
        raise ValueError(f'Pine levels are valid only for {plan.session_date}.')
    if not entry_allowed(now,settings):
        return None
    if bars is None or bars.empty or not isinstance(bars.index,pd.DatetimeIndex) or bars.index.tz is None:
        raise ValueError('Completed intraday data unavailable.')
    if bars.index.has_duplicates or not bars.index.is_monotonic_increasing:
        raise ValueError('Duplicate or unsorted candles.')
    opening,closing=bounds(now)
    frame=bars.copy()
    # IBKR labels candles by their START time.
    frame.index=frame.index.tz_convert(now.tz)+pd.Timedelta(minutes=settings.bar_minutes)
    history=frame.loc[frame.index<=now]
    frame=history.loc[(history.index>opening)&(history.index<=closing)]
    if len(frame)<1:
        return None
    if len(frame)>1 and frame.index[-1]-frame.index[-2]!=pd.Timedelta(minutes=settings.bar_minutes):
        raise ValueError('Missing preceding candle.')
    if (now-frame.index[-1]).total_seconds()>settings.candle_max_age_seconds:
        raise ValueError('Latest completed candle is stale.')
    import numpy as np
    if not np.isfinite(frame[['Open','High','Low','Close','Volume']].to_numpy()).all() or (frame.Volume<0).any() or (frame[['Open','High','Low','Close']]<=0).any().any():
        raise ValueError('Invalid OHLCV values.')
    if (frame.High<frame[['Open','Close','Low']].max(axis=1)).any() or (frame.Low>frame[['Open','Close','High']].min(axis=1)).any():
        raise ValueError('Invalid candle price range.')
    expected=pd.date_range(opening+pd.Timedelta(minutes=settings.bar_minutes),frame.index[-1],freq=f'{settings.bar_minutes}min')
    if not frame.index.equals(expected):
        raise ValueError('Missing session candles; complete session data required for VWAP.')
    total=frame.Volume.sum()
    if total<=0:
        raise ValueError('VWAP volume unavailable.')
    vwap=float((((frame.High+frame.Low+frame.Close)/3)*frame.Volume).sum()/total)
    if len(frame)>1:
        previous=float(frame.Close.iloc[-2])
    else:
        prior=history.loc[history.index<=opening]
        cal=calendar(now.year)
        previous_day=cal.previous_session(pd.Timestamp(now.date()))
        previous_close=cal.session_close(previous_day).tz_convert(now.tz)
        if prior.empty or prior.index[-1]!=previous_close:
            raise ValueError('Previous session closing candle unavailable.')
        previous=float(prior.Close.iloc[-1])
        if not isfinite(previous) or previous<=0:
            raise ValueError('Invalid previous session close.')
    rules=plan.signal_rules
    if rules is not None:
        candle_start=frame.index[-1]-pd.Timedelta(minutes=settings.bar_minutes)
        if rules.morning_only and not time.fromisoformat(rules.morning_start)<=candle_start.time()<time.fromisoformat(rules.morning_end):
            return None
        if rules.require_volume:
            # Pine SMA[1] excludes the signal candle and can span prior sessions.
            prior_volume=history.Volume.iloc[-rules.volume_lookback-1:-1]
            if len(prior_volume)!=rules.volume_lookback or not np.isfinite(prior_volume.to_numpy()).all() or (prior_volume<0).any():
                raise ValueError('Prior 20 completed candle volumes unavailable for Pine volume filter.')
            if float(frame.Volume.iloc[-1])<rules.volume_multiple*float(prior_volume.mean()):
                return None
    last=float(frame.Close.iloc[-1])
    longs=settings.option_allow_calls if settings.instrument=='OPT' else settings.enable_longs
    shorts=settings.option_allow_puts if settings.instrument=='OPT' else settings.enable_shorts
    side='LONG' if previous<=levels.longEntry<last and last>vwap and longs else 'SHORT' if previous>=levels.shortEntry>last and last<vwap and shorts else None
    if side is None:
        return None
    return dict(symbol=levels.symbol,side=side,candle_end=frame.index[-1].isoformat(),decision_at=now.isoformat(),close=last,vwap=vwap,levels=levels.side(side),plan_hash=plan.source_hash,session_date=plan.session_date.isoformat())


def share_quantity(signal,price,settings,*,risk_used=0,notional_used=0,daily_capital_used=0,available_funds=None,account_equity=None):
    levels=signal['levels']
    sign=1 if signal['side']=='LONG' else -1
    if not isfinite(price) or not 0<sign*(price-levels['stop']) or sign*(price-levels['t1'])>=0:
        raise ValueError('Entry price invalid or first target already reached.')
    risk=sign*(price-levels['stop'])
    # Use actual stock fill risk; there is no option multiplier.
    budget=min(settings.risk_dollars,settings.max_total_risk-risk_used)
    capital=min(settings.max_position_dollars,settings.max_total_dollars-notional_used,settings.max_daily_capital-daily_capital_used)
    if settings.account_allocation_pct:
        if account_equity is None or not isfinite(account_equity) or account_equity<=0:
            raise ValueError('Verified account equity required for stock allocation.')
        capital=min(capital,account_equity*settings.account_allocation_pct/100*settings.margin_multiplier-notional_used)
    if available_funds is not None:
        capital=min(capital,available_funds*settings.margin_multiplier)
    quantity=min(floor(budget/risk),floor(capital/price))
    if quantity<1:
        raise ValueError('Insufficient share risk or capital budget.')
    return quantity


def target_legs(quantity,settings):
    first=max(1,floor(quantity*settings.t1_fraction))
    return [(first,'t1')]+([(quantity-first,'t2')] if quantity>first else [])


def option_quantity(premium,settings,*,notional_used=0,daily_capital_used=0,available_funds=None,account_equity=None):
    """Long options reserve their entire premium; stock margin is never used."""
    if not isfinite(premium) or premium<=0:
        raise ValueError('Invalid option premium.')
    budget=min(settings.option_premium_budget,settings.option_max_total_premium-notional_used,
        settings.option_max_daily_premium-daily_capital_used)
    if settings.account_allocation_pct:
        if account_equity is None or not isfinite(account_equity) or account_equity<=0:
            raise ValueError('Verified equity required for option allocation.')
        budget=min(budget,account_equity*settings.account_allocation_pct/100-notional_used)
    if available_funds is not None:
        budget=min(budget,available_funds)
    qty=min(int(settings.option_max_contracts),floor(budget/(premium*100+settings.option_commission_per_contract)))
    if qty<1:
        raise ValueError('Insufficient option premium budget for one contract.')
    return qty
