"""Historical share replay of the existing dated Pine-level strategy."""
from dataclasses import asdict, replace
from datetime import date
from math import isfinite
from pathlib import Path
import json

import numpy as np
import pandas as pd

from modules.pine_levels import PLAN_FILE, load_plan, parse_pine
from modules.pine_stock_signals import (
    Settings, aware, bounds, calendar, confirmed_signal, entry_allowed,
    share_quantity, option_quantity, target_legs,
)
from modules.pine_stock_engine import money

EXPORT_DIR = Path(__file__).resolve().parent/'exports'/'pine_stocks'
OPTION_EXPORT_DIR = EXPORT_DIR.parent/'pine_options'
COLUMNS = ['Open', 'High', 'Low', 'Close', 'Volume']


class OptionHistoryError(ValueError):
    pass


def available_plans(path=PLAN_FILE):
    """Only saved user plans are discovered; example scripts are not history."""
    path = Path(path)
    plans, errors = {}, []
    for item in [*sorted((path.parent/'daily_stock_plan_history').glob('*.json')), path]:
        if not item.exists():
            continue
        try:
            plan = load_plan(item)
            if plan is None:
                raise ValueError('Plan file is unreadable.')
            plans[plan.source_hash] = plan
        except ValueError as exc:
            errors.append(dict(file=item.name, error=str(exc)))
    return list(plans.values()), errors


def select_plans(plans):
    result = {}
    for supplied in plans:
        # Revalidate the immutable source, including imported signal rules.
        plan = parse_pine(supplied.source)
        if plan != supplied:
            raise ValueError('Plan values differ from their Pine source.')
        if plan.session_date in result and result[plan.session_date].source_hash != plan.source_hash:
            raise ValueError(f'Multiple different plans for {plan.session_date}; select one version for that date.')
        result[plan.session_date] = plan
    return result


def regular_frame(frame, minutes, now):
    if frame is None or frame.empty:
        raise ValueError('No historical candles available.')
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise ValueError('Timezone-aware historical candles required.')
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError('Duplicate or unsorted historical candles.')
    out = frame[COLUMNS].copy()
    out.index = out.index.tz_convert('America/New_York')
    out = out.loc[out.index + pd.Timedelta(minutes=minutes) <= now]
    if not np.isfinite(out.to_numpy(dtype=float)).all() or (out.Volume < 0).any() or (out[COLUMNS[:4]] <= 0).any().any():
        raise ValueError('Invalid historical OHLCV values.')
    if (out.High < out[['Open', 'Close', 'Low']].max(axis=1)).any() or (out.Low > out[['Open', 'Close', 'High']].min(axis=1)).any():
        raise ValueError('Invalid historical candle ranges.')
    pieces = []
    for day, group in out.groupby(out.index.date):
        session = bounds(pd.Timestamp(day, tz='America/New_York'))
        if session:
            opening, closing = session
            pieces.append(group.loc[(group.index >= opening) & (group.index + pd.Timedelta(minutes=minutes) <= closing)])
    return pd.concat(pieces) if pieces else out.iloc[:0]


def run_backtest(plans, data, settings=None, *, starting_capital=10000., start=None, end=None, now=None, option_bars_provider=None, option_contract_bars_provider=None):
    """Signals use completed bars; fills start at the next bar's open.

    Stop wins when both stop and target are touched within one candle. Overnight
    positions retain their original plan. No closing fill is invented at test end.
    """
    cfg = replace(settings or Settings(), execution='Simulation', live_confirmation='')
    options = cfg.instrument=='OPT'
    multiplier = 100 if options else 1
    margin = 1 if options else cfg.margin_multiplier
    if options and option_bars_provider is None:
        raise ValueError('Pine option backtests require actual historical option candles.')
    if cfg.bar_minutes != 5:
        raise ValueError('Daily Pine backtests use five-minute regular-session candles.')
    if not isfinite(starting_capital) or starting_capital <= 0:
        raise ValueError('Starting capital must be positive and finite.')
    dated = select_plans(plans)
    if not dated:
        raise ValueError('Select at least one dated Pine plan.')
    start = pd.Timestamp(start).date() if start else min(dated)
    end = pd.Timestamp(end).date() if end else max(dated)
    if end < start:
        raise ValueError('End date must be on or after start date.')
    now = aware(now) if now is not None else pd.Timestamp.now(tz='America/New_York')
    end_session = bounds(pd.Timestamp(end, tz='America/New_York'))
    if end > now.date() or (end_session and end_session[1] > now):
        raise ValueError('Choose an end date whose regular trading session has completed.')
    dated = {day:plan for day,plan in dated.items() if start <= day <= end}
    if not dated:
        raise ValueError('No selected plans fall inside the requested dates.')
    symbols = sorted({level.symbol for plan in dated.values() for level in plan.levels})
    errors, frames, valid_days = [], {}, {}
    cal = calendar(start.year)
    days = [ts.date() for ts in cal.sessions_in_range(str(start), str(end))]
    for symbol in symbols:
        try:
            frame = regular_frame(data.get(symbol), 5, now)
            frames[symbol] = frame
            valid_days[symbol] = set()
            for day in days:
                opening, closing = bounds(pd.Timestamp(day, tz='America/New_York'))
                expected = pd.date_range(opening, closing, freq='5min', inclusive='left')
                actual = frame.loc[frame.index.date == day].index
                if actual.equals(expected):
                    valid_days[symbol].add(day)
                else:
                    errors.append(dict(symbol=symbol, session_date=str(day), error='Missing or incomplete five-minute session; entries disabled. Open positions remain unresolved across missing bars.'))
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(dict(symbol=symbol, error=str(exc)))
    plan_levels = {day:{level.symbol:level for level in plan.levels} for day,plan in dated.items()}
    trades, fills, decisions, curve, positions, pending = [], [], [], [], {}, {}
    claimed, marks, blocked = set(), {}, set()
    option_marks, option_sessions = {}, {}
    option_print_usage = {}
    daily_realized, daily_spend, daily_count = {}, {}, {}
    realized = 0.
    duration = pd.Timedelta(minutes=5)
    timeline = sorted({ts for frame in frames.values() for ts in frame.index if start <= ts.date() <= end})

    def fee(qty):
        return qty*cfg.option_commission_per_contract if options else max(cfg.simulation_min_fee, qty * cfg.simulation_fee_per_share)

    def option_session(info, timestamp):
        key=(info['symbol'],str(info['expiry']),float(info['strike']),info['right'],str(timestamp.date()))
        if key not in option_sessions:
            if option_contract_bars_provider is None:
                raise ValueError('Exact-contract option history unavailable for carried position.')
            _,raw=option_contract_bars_provider(symbol=info['symbol'],signal='CALL' if info['right']=='C' else 'PUT',
                expiry=info['expiry'],strike=info['strike'],option_dte=7,reference_time=timestamp)
            option_sessions[key]=prepare_options(raw)
        return option_sessions[key]

    def prepare_options(raw):
        if raw is None or raw.empty or not isinstance(raw.index,pd.DatetimeIndex) or raw.index.tz is None:
            raise ValueError('Timestamped historical option candles unavailable.')
        frame=raw[COLUMNS].copy()
        frame.index=frame.index.tz_convert('America/New_York')
        if frame.index.has_duplicates or not frame.index.is_monotonic_increasing or not np.isfinite(frame.to_numpy()).all() or (frame<0).any().any():
            raise ValueError('Invalid historical option candles.')
        if (frame.High<frame[['Open','Close','Low']].max(axis=1)).any() or (frame.Low>frame[['Open','Close','High']].min(axis=1)).any():
            raise ValueError('Invalid historical option candle ranges.')
        return frame

    def option_bar(p,timestamp):
        frame=option_session(p['contract'],timestamp)
        if timestamp not in frame.index:
            raise ValueError(f'Option candle missing at {timestamp}; no estimated premium used.')
        return frame.loc[timestamp]

    def select_historical(symbol,sign,underlying,timestamp):
        try:
            info,raw=option_bars_provider(symbol=symbol,signal='CALL' if sign==1 else 'PUT',underlying_price=underlying,option_dte=7,reference_time=timestamp)
            if not info or info.get('right')!=('C' if sign==1 else 'P') or str(info.get('multiplier','100'))!='100':
                raise ValueError('Historical option contract does not match the Pine direction or standard multiplier.')
            info=dict(info,symbol=symbol)
            expiry=pd.Timestamp(str(info['expiry'])).date()
            if expiry<=timestamp.date():
                raise ValueError('Historical option is expired or expires on entry day.')
            key=(symbol,str(info['expiry']),float(info['strike']),info['right'],str(timestamp.date()))
            option_sessions[key]=prepare_options(raw)
            if timestamp not in option_sessions[key].index:
                raise ValueError('No option opening candle for the confirmed entry.')
            if float(option_sessions[key].loc[timestamp,'Volume'])<=0:
                raise ValueError('Option entry candle has zero trading volume; its carried-forward price cannot establish a fill.')
            return info,option_sessions[key].loc[timestamp],expiry
        except (ValueError,RuntimeError,KeyError,TypeError) as exc:
            raise OptionHistoryError(str(exc)) from exc

    def unrealized(symbol,p):
        mark=option_marks.get(symbol,p['entry_price']) if options else marks.get(symbol,p['entry_price'])
        return (1 if options else p['sign'])*(mark-p['entry_price'])*p['remaining']*multiplier

    def equity():
        return starting_capital + realized + sum(unrealized(s,p) for s,p in positions.items())

    def exit_leg(symbol, position, leg, price, reason, timestamp, print_time=None):
        nonlocal realized
        qty = leg['remaining']
        charge = fee(qty)
        net = (1 if options else position['sign']) * (price-position['entry_price']) * qty * multiplier - charge
        realized += net
        daily_realized[timestamp.date()] = daily_realized.get(timestamp.date(), 0.) + net
        position['realized_pnl'] += net
        position['commissions'] += charge
        position['remaining'] -= qty
        leg['remaining'] = 0
        if options:
            key=(symbol,print_time)
            option_print_usage[key]=option_print_usage.get(key,0)+qty
        fills.append(dict(trade_id=position['trade_id'], symbol=symbol, timestamp=timestamp.isoformat(), side=position['side'], reason=reason, quantity=qty, price=price, commission=charge, realized_pnl=net))
        if not position['remaining']:
            position.update(status='CLOSED', exit_time=timestamp.isoformat(), exit_reason=reason)

    def manage(symbol, bar, timestamp, opening_only):
        p = positions.get(symbol)
        if p is None or symbol in blocked:
            return
        sign, levels = p['sign'], p['levels']
        premium_bar=None
        if options:
            try:
                premium_bar=option_bar(p,timestamp if opening_only else timestamp-duration)
                if float(premium_bar.Volume)>0:
                    option_marks[symbol]=float(premium_bar.Open if opening_only else premium_bar.Close)
                    p['last_mark_time']=timestamp.isoformat()
            except (ValueError,RuntimeError) as exc:
                blocked.add(symbol)
                p['status']='UNRESOLVED_OPTION_DATA'
                errors.append(dict(symbol=symbol,error=str(exc)))
                return
        stop_hit = sign*(float(bar.Open)-levels['stop']) <= 0 if opening_only else (bar.Low <= levels['stop'] if sign == 1 else bar.High >= levels['stop'])
        expiry_hit=False
        print_time=timestamp if opening_only else timestamp-duration
        if options:
            from modules.pine_options import expiry_deadline
            expiry_hit=opening_only and timestamp>=expiry_deadline(p['contract']['expiry'])
            requested=sum(leg['remaining'] for leg in p['legs'] if stop_hit or expiry_hit or (
                sign*(float(bar.Open)-levels[leg['target']])>=0 if opening_only else
                bar.High>=levels[leg['target']] if sign==1 else bar.Low<=levels[leg['target']]))
            available=float(premium_bar.Volume)-option_print_usage.get((symbol,print_time),0)
            if requested>available:
                blocked.add(symbol)
                p['status']='UNRESOLVED_OPTION_DATA'
                reason='has zero trading volume' if float(premium_bar.Volume)<=0 else f'has only {available:g} unallocated recorded contracts for a {requested}-contract exit'
                errors.append(dict(symbol=symbol,error=f'Option exit candle at {print_time} {reason}; no full fill can be verified.'))
                return
        for leg in p['legs']:
            if not leg['remaining']:
                continue
            target = levels[leg['target']]
            target_hit = sign*(float(bar.Open)-target) >= 0 if opening_only else (bar.High >= target if sign == 1 else bar.Low <= target)
            if stop_hit:
                reference = float(bar.Open) if opening_only else levels['stop']
                price = money(float(premium_bar.Open if opening_only else premium_bar.Low)*(1-cfg.max_entry_slippage_pct/100)) if options else money(reference*(1-sign*cfg.max_entry_slippage_pct/100), up=sign == -1)
                exit_leg(symbol, p, leg, price, 'STOP_GAP' if opening_only else 'STOP', timestamp,print_time)
            elif target_hit:
                price=money(float(premium_bar.Open if opening_only else premium_bar.Low)*(1-cfg.max_entry_slippage_pct/100)) if options else target
                exit_leg(symbol, p, leg, price, leg['target'].upper(), timestamp,print_time)
            elif expiry_hit:
                price=money(float(premium_bar.Open)*(1-cfg.max_entry_slippage_pct/100))
                exit_leg(symbol,p,leg,price,'EXPIRY_EXIT',timestamp,print_time)
        if not p['remaining']:
            del positions[symbol]

    for ts in timeline:
        day = ts.date()
        bars = {s:frame.loc[ts] for s,frame in frames.items() if ts in frame.index}
        # Missing bars cannot be skipped over to manufacture later exit fills.
        for symbol,p in positions.items():
            if any(date.fromisoformat(p['session_date']) <= missing < day and missing not in valid_days.get(symbol,set()) for missing in days):
                blocked.add(symbol)
                p['status'] = 'UNRESOLVED_DATA_GAP'
            session = bounds(ts)
            if session and session[0] <= ts < session[1]:
                frame = frames[symbol]
                opening, _ = session
                expected = pd.date_range(opening, ts, freq='5min')
                if not frame.loc[(frame.index >= opening) & (frame.index <= ts)].index.equals(expected):
                    blocked.add(symbol)
                    p['status'] = 'UNRESOLVED_DATA_GAP'
        for symbol,bar in bars.items():
            marks[symbol] = float(bar.Open)
            manage(symbol, bar, ts, True)
        for symbol,bar in sorted(bars.items()):
            order = pending.pop(symbol, None)
            if order is None or order['fill_at'] != ts:
                continue
            signal = order['signal']
            try:
                if symbol in positions or (symbol,day) in claimed:
                    raise ValueError('Already traded today or position still open.')
                if blocked:
                    raise ValueError('An open position has unresolved historical data; new entries disabled.')
                if cfg.kill_switch or not entry_allowed(ts, cfg):
                    raise ValueError('Entry window closed or kill switch active.')
                loss = daily_realized.get(day,0.) + sum(min(0.,unrealized(s,p)) for s,p in positions.items())
                if len(positions) >= cfg.max_positions or daily_count.get(day,0) >= cfg.max_daily_trades or loss <= -cfg.max_daily_loss:
                    raise ValueError('Position, daily trade or daily loss limit reached.')
                sign = 1 if signal['side'] == 'LONG' else -1
                if sign*(float(bar.Open)-signal['levels']['entry']) <= 0:
                    raise ValueError('Next opening price returned inside the trigger.')
                price = money(float(bar.Open)*(1+sign*cfg.max_entry_slippage_pct/100), up=sign == 1)
                exposure = sum(p['entry_price']*p['remaining']*multiplier for p in positions.values())
                contract=None
                if options:
                    if sign*(float(bar.Open)-signal['levels']['t1'])>=0:
                        raise ValueError('Underlying has already reached T1.')
                    contract,premium_bar,expiry=select_historical(symbol,sign,float(bar.Open),ts)
                    price=money(float(premium_bar.Open)*(1+cfg.max_entry_slippage_pct/100),up=True)
                    qty=option_quantity(price,cfg,notional_used=exposure,daily_capital_used=daily_spend.get(day,0.),available_funds=max(0.,equity()-exposure),account_equity=equity())
                    available=float(premium_bar.Volume)-option_print_usage.get((symbol,ts),0)
                    if qty>available:
                        raise OptionHistoryError(f'Option entry requires {qty} contracts but its candle records only {available:g}; no full fill can be verified.')
                    option_marks[symbol]=float(premium_bar.Open)
                else:
                    qty = share_quantity(signal, price, cfg,
                        risk_used=sum(abs(p['entry_price']-p['levels']['stop'])*p['remaining'] for p in positions.values()),
                        notional_used=exposure, daily_capital_used=daily_spend.get(day,0.),
                        available_funds=max(0.,equity()-exposure/margin), account_equity=equity())
                legs = [dict(quantity=n, remaining=n, target=target) for n,target in target_legs(qty,cfg)]
                charge = sum(fee(leg['quantity']) for leg in legs)
                if price*qty*multiplier/margin+charge > max(0.,equity()-exposure/margin):
                    raise ValueError('Insufficient capital including entry commissions.')
                p = dict(trade_id=len(trades)+1, symbol=symbol, session_date=str(day), side=signal['side'], sign=sign,
                    plan_hash=signal['plan_hash'], signal_time=signal['candle_end'], entry_time=ts.isoformat(),
                    entry_price=price, quantity=qty, remaining=qty, levels=signal['levels'], legs=legs,
                    realized_pnl=-charge, commissions=charge, status='OPEN', exit_time=None, exit_reason=None)
                if contract:
                    p.update(contract=contract,instrument='OPT',option_dte=7,option_expiry=contract['expiry'],option_strike=contract['strike'],
                        option_right=contract['right'],actual_dte=(expiry-day).days,entry_underlying=float(bar.Open))
                trades.append(p)
                positions[symbol] = p
                if options:
                    option_print_usage[(symbol,ts)]=option_print_usage.get((symbol,ts),0)+qty
                claimed.add((symbol,day))
                realized -= charge
                daily_realized[day] = daily_realized.get(day,0.)-charge
                daily_spend[day] = daily_spend.get(day,0.)+price*qty*multiplier
                daily_count[day] = daily_count.get(day,0)+1
                fills.append(dict(trade_id=p['trade_id'],symbol=symbol,timestamp=ts.isoformat(),side=p['side'],reason='ENTRY',quantity=qty,price=price,commission=charge,realized_pnl=-charge))
                decisions.append(dict(timestamp=ts.isoformat(),symbol=symbol,status='TRADED',reason='Confirmed signal filled at next candle open.'))
            except (ValueError,RuntimeError) as exc:
                decisions.append(dict(timestamp=ts.isoformat(),symbol=symbol,status='REJECTED',reason=str(exc)))
                if isinstance(exc,OptionHistoryError):
                    errors.append(dict(symbol=symbol,session_date=str(day),error=str(exc)))
        for symbol,bar in bars.items():
            manage(symbol, bar, ts+duration, False)
            marks[symbol] = float(bar.Close)
        for symbol,bar in sorted(bars.items()):
            if day not in dated or symbol not in plan_levels[day] or day not in valid_days.get(symbol,set()):
                continue
            if symbol in positions or (symbol,day) in claimed:
                continue
            if not cfg.trade_all_plan_symbols and symbol not in cfg.allowed_symbols:
                continue
            try:
                signal = confirmed_signal(dated[day],plan_levels[day][symbol],frames[symbol].loc[:ts],ts+duration,cfg)
                if signal:
                    # A pending order never rolls over to another session.
                    if ts+duration in frames[symbol].index and (ts+duration).date() == day and ts+duration < bounds(ts)[1]:
                        pending[symbol] = dict(signal=signal, fill_at=ts+duration)
                    else:
                        decisions.append(dict(timestamp=(ts+duration).isoformat(),symbol=symbol,status='UNFILLED',reason='No next regular-session candle.'))
            except ValueError as exc:
                decisions.append(dict(timestamp=(ts+duration).isoformat(),symbol=symbol,status='DATA_ERROR',reason=str(exc)))
                issue = dict(symbol=symbol,session_date=str(day),error=str(exc))
                if issue not in errors:
                    errors.append(issue)
        curve.append(dict(timestamp=(ts+duration).isoformat(),equity=equity(),realized_pnl=realized))
    for p in trades:
        if p['remaining'] and any(day >= date.fromisoformat(p['session_date']) and day not in valid_days.get(p['symbol'],set()) for day in days):
            p['status'] = 'UNRESOLVED_DATA_GAP'
        p['unrealized_pnl'] = unrealized(p['symbol'],p)
        p['last_mark'] = option_marks.get(p['symbol']) if options else marks.get(p['symbol'])
        if options and p['remaining'] and timeline and p.get('last_mark_time')!=(timeline[-1]+duration).isoformat():
            errors.append(dict(symbol=p['symbol'],error=f'End-of-test option mark is stale; last traded candle mark was {p.get("last_mark_time","unavailable")}. Unrealized P/L is not a current quote valuation.'))
        p['return_pct'] = p['realized_pnl']/(p['entry_price']*p['quantity']*multiplier)*100
    trade_frame = pd.DataFrame([{k:v for k,v in p.items() if k not in ('legs','levels','sign','contract')} for p in trades])
    closed = [p for p in trades if p['status']=='CLOSED']
    equity_values = pd.Series([starting_capital]+[r['equity'] for r in curve])
    ending = equity()
    metrics = dict(total_trades=len(trades),closed_trades=len(closed),open_positions=len(positions),
        net_pnl=round(realized,2),unrealized_pnl=round(ending-starting_capital-realized,2),
        ending_equity=round(ending,2),return_pct=round((ending/starting_capital-1)*100,2),
        win_rate=round(100*sum(p['realized_pnl']>0 for p in closed)/len(closed),1) if closed else 0.,
        max_drawdown=round(float((equity_values-equity_values.cummax()).min()),2),
        commissions=round(sum(p['commissions'] for p in trades),2))
    coverage = [dict(session_date=str(day), plan_available=day in dated,
        plan_symbols=len(dated[day].levels) if day in dated else 0,
        complete_data_symbols=sum(day in valid_days.get(symbol,set()) for symbol in symbols)) for day in days]
    return dict(trades=trade_frame,fills=pd.DataFrame(fills),decisions=pd.DataFrame(decisions),
        equity=pd.DataFrame(curve),coverage=pd.DataFrame(coverage),errors=pd.DataFrame(errors),metrics=metrics,
        meta=dict(strategy='Daily Pine Levels — 7-DTE options' if options else 'Daily Pine Levels — stocks',instrument=cfg.instrument,start=str(start),end=str(end),
            completed_at=pd.Timestamp.now(tz='America/New_York').isoformat(),warmup_calendar_days=7,
            plan_dates=[str(day) for day in sorted(dated)],symbols=symbols,settings=asdict(cfg),
            starting_capital=starting_capital,entry_model='next_bar_open_with_adverse_slippage',
            ambiguous_bar_policy='stop_first',end_policy='retain_open_positions',
            option_pricing='historical_open_entry_low_intrabar_exit_close_mark' if options else None,
            option_fill_model_version=3 if options else None,
            short_borrow_model='assumed_available_no_borrow_fees' if cfg.enable_shorts else 'shorts_disabled',
            status='COMPLETE_WITH_DATA_ERRORS' if errors else 'COMPLETE'))


def save_result(result, directory=None):
    directory = Path(directory) if directory is not None else OPTION_EXPORT_DIR if result['meta'].get('instrument')=='OPT' else EXPORT_DIR
    directory.mkdir(parents=True,exist_ok=True)
    for name,value in result.items():
        if isinstance(value,pd.DataFrame):
            value.to_csv(directory/f'{name}.csv',index=False)
        else:
            (directory/f'{name}.json').write_text(json.dumps(value,indent=2,default=str)+'\n')


def load_result(directory=EXPORT_DIR):
    directory = Path(directory)
    if not (directory/'meta.json').exists():
        return None
    result = {}
    for name in ('meta','metrics'):
        result[name] = json.loads((directory/f'{name}.json').read_text())
    for name in ('trades','fills','decisions','equity','coverage','errors'):
        try:
            result[name] = pd.read_csv(directory/f'{name}.csv')
        except pd.errors.EmptyDataError:
            result[name] = pd.DataFrame()
    return result
