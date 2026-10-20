"""Persistent share trading from imported Pine levels, isolated from option orders."""
from dataclasses import asdict,replace
from decimal import Decimal,ROUND_CEILING,ROUND_FLOOR
from pathlib import Path
from math import isfinite
import hashlib
import json
import sqlite3
import pandas as pd
from .pine_levels import load_plan
from .pine_stock_signals import Settings,aware,bounds,confirmed_signal,validate_quote,share_quantity,option_quantity,target_legs,entry_allowed

ROOT=Path(__file__).resolve().parents[1]
LEDGER_FILE=ROOT/'data'/'pine_stock_trades.sqlite'
STATUS_FILE=ROOT/'exports'/'pine_stock_status.json'
_BAR_CACHE={}


def money(value,up=False,tick=.01):
    step=Decimal(str(tick))
    return float((Decimal(str(value))/step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)*step)


class Ledger:
    def __init__(self,path=LEDGER_FILE):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=30)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS trades(key TEXT PRIMARY KEY, book TEXT, day TEXT, symbol TEXT, status TEXT, payload TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS once_per_symbol ON trades(book,day,symbol);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,timestamp TEXT,kind TEXT,payload TEXT);''')

    def event(self,kind,payload,now):
        self.db.execute('INSERT INTO events(timestamp,kind,payload) VALUES(?,?,?)',(aware(now).isoformat(),kind,json.dumps(payload,default=str,allow_nan=False)))
        self.db.commit()

    def rows(self,book):
        return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM trades WHERE book=? ORDER BY day,key',(book,))]

    def claim(self,row):
        try:
            self.db.execute('INSERT INTO trades VALUES(?,?,?,?,?,?)',(row['key'],row['book'],row['day'],row['symbol'],row['status'],json.dumps(row,default=str,allow_nan=False)))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            self.db.rollback()
            return False

    def update(self,row):
        self.db.execute('UPDATE trades SET status=?,payload=? WHERE key=?',(row['status'],json.dumps(row,default=str,allow_nan=False),row['key']))
        self.db.commit()

    def close(self):
        self.db.close()


def risk_snapshot(rows,day,quotes,settings,option_quotes=None):
    current=[r for r in rows if r['status']!='CLOSED']
    pnl=0.
    for row in rows:
        if 'daily_realized' in row:
            pnl+=row['daily_realized'].get(day,0.)
        elif row['day']==day or row.get('closed_day')==day:
            pnl+=row.get('realized',0.)
        if row in current and row.get('quantity',0)>0:
            is_option=row.get('instrument')=='OPT'
            q=(option_quotes or {}).get(row['key']) if is_option else quotes.get(row['symbol'])
            if q is None:
                raise ValueError('Missing portfolio marks; new entries blocked.')
            price=q['bid'] if is_option or row['side']=='LONG' else q['ask']
            # Open gains cannot conceal today's losses; include lifetime
            # unrealized losses conservatively, including overnight holdings.
            pnl+=min(0.,(100 if is_option else 1 if row['side']=='LONG' else -1)*(price-row['entry_price'])*row['quantity'])
    if any(r['status'] in ('INTENT','UNKNOWN') for r in current):
        raise ValueError('An unresolved broker order requires reconciliation.')
    return dict(positions=len(current),risk=sum(r.get('reserved_risk',0) for r in current),notional=sum(r['entry_price']*r.get('quantity',0)*(100 if r.get('instrument')=='OPT' else 1) for r in current),trades_today=sum(r['day']==day for r in rows),daily_capital=sum(r.get('entry_capital',r.get('entry_price',0)*r.get('initial_quantity',0)) for r in rows if r['day']==day),pnl=pnl)


class StockEngine:
    def __init__(self,settings,ledger,provider,broker=None,book='Simulation'):
        # Legacy saved intervals cannot change the Daily Pine confirmation rule.
        self.settings,self.ledger,self.provider,self.broker,self.book=replace(settings,bar_minutes=5),ledger,provider,broker,book

    def manage_simulation(self,rows,quotes,now,option_quotes=None):
        cfg=self.settings
        for row in rows:
            if row['status']!='SIM_OPEN' or row['symbol'] not in quotes or aware(row['opened_at'])>=aware(now):
                continue
            q=quotes[row['symbol']]
            is_option=row.get('instrument')=='OPT'
            option_quote=(option_quotes or {}).get(row['key'])
            if is_option and option_quote is None:
                continue
            sign=1 if row['side']=='LONG' else -1
            price=q['bid'] if sign==1 else q['ask']
            for leg in row['legs']:
                remaining=leg['remaining']
                if not remaining:
                    continue
                reason='STOP' if sign*(price-row['levels']['stop'])<=0 else leg['target'].upper() if sign*(price-row['levels'][leg['target']])>=0 else None
                if is_option:
                    from .pine_options import expiry_deadline
                    if now>=expiry_deadline(row['contract']['expiry']):
                        reason=reason or 'EXPIRY_EXIT'
                if reason is None:
                    continue
                fill=money(option_quote['bid']*(1-cfg.max_entry_slippage_pct/100)) if is_option else money(price*(1-sign*cfg.max_entry_slippage_pct/100),up=sign==-1) if reason=='STOP' else row['levels'][leg['target']]
                fee=remaining*cfg.option_commission_per_contract if is_option else max(cfg.simulation_min_fee,remaining*cfg.simulation_fee_per_share)
                pnl=(100 if is_option else sign)*(fill-row['entry_price'])*remaining-fee
                row['realized']+=pnl
                day=aware(now).date().isoformat()
                daily=row.setdefault('daily_realized',{})
                daily[day]=daily.get(day,0.)+pnl
                row['fees']+=fee
                row['quantity']-=remaining
                leg['remaining']=0
                row['fills'].append(dict(timestamp=aware(now).isoformat(),quantity=remaining,price=fill,reason=reason,pnl=pnl,asset='OPT' if is_option else 'STK'))
            row['reserved_risk']=(row['entry_price']*100 if is_option else abs(row['entry_price']-row['levels']['stop']))*row['quantity']
            if row['quantity']==0:
                row['status']='CLOSED'
                row['closed_day']=aware(now).date().isoformat()
            self.ledger.update(row)

    def cycle(self,plan,now,*,manage_only=False,orders_allowed=False):
        now=aware(now)
        cfg=self.settings
        rows=self.ledger.rows(self.book)
        symbols=sorted({r['symbol'] for r in rows if r['status']!='CLOSED'}|({l.symbol for l in plan.levels} if plan and not manage_only else set()))
        decisions=[]
        quotes={}
        for symbol in symbols:
            try:
                q=self.provider.quote(symbol)
                validate_quote(q,now,cfg)
                quotes[symbol]=q
            except (ValueError,TypeError,RuntimeError) as exc:
                decisions.append(dict(symbol=symbol,status='BLOCKED',reason=str(exc)))
        option_quotes={}
        for row in rows:
            if row['status']!='CLOSED' and row.get('instrument')=='OPT':
                try:
                    q=self.provider.option_quote(row['contract'])
                    if hasattr(self.provider,'now'):
                        now=aware(self.provider.now())
                    validate_quote(q,now,replace(cfg,max_spread_pct=cfg.option_max_spread_pct))
                    option_quotes[row['key']]=q
                except (ValueError,TypeError,RuntimeError) as exc:
                    decisions.append(dict(symbol=row['symbol'],status='BLOCKED',reason=str(exc)))
        if cfg.execution=='Simulation':
            self.manage_simulation(rows,quotes,now,option_quotes)
        elif self.broker:
            for row in rows:
                if row['status']!='CLOSED':
                    updated=self.broker.reconcile(row)
                    if updated['status']=='CLOSED':
                        updated['closed_day']=now.date().isoformat()
                    self.ledger.update(updated)
        rows=self.ledger.rows(self.book)
        if manage_only or not plan or plan.session_date!=now.date():
            reason='Managing existing stock positions' if manage_only else 'Paste a Pine plan dated for today; expired levels cannot open new trades.'
            result=dict(timestamp=now.isoformat(),mode=cfg.execution,plan_date=str(plan.session_date) if plan else None,decisions=decisions,positions=rows,message=reason)
            self.ledger.event('cycle',result,now)
            return result
        for levels in plan.levels:
            symbol=levels.symbol
            try:
                if not cfg.trade_all_plan_symbols and symbol not in cfg.allowed_symbols:
                    raise ValueError('Ticker is not approved for the Pine stock strategy.')
                bars=self.provider.bars(symbol,cfg.bar_minutes)
                if hasattr(self.provider,'now'):
                    now=aware(self.provider.now())
                self.ledger.event('market_inputs',dict(symbol=symbol,source='IBKR',strategy_version=plan.version,plan_hash=plan.source_hash,settings=asdict(cfg),quote=quotes.get(symbol),bars=json.loads(bars.to_json(orient='split',date_format='iso')) if bars is not None else None),now)
                signal=confirmed_signal(plan,levels,bars,now,cfg)
                if signal is None:
                    decisions.append(dict(symbol=symbol,status='WAIT',reason='Waiting for confirmed crossover and VWAP'))
                    continue
                self.ledger.event('signal',signal,now)
                if cfg.kill_switch:
                    raise ValueError('Kill switch active.')
                if hasattr(self.provider,'now'):
                    # Refresh stock and portfolio marks after historical requests.
                    for target in sorted({symbol}|{r['symbol'] for r in self.ledger.rows(self.book) if r['status']!='CLOSED'}):
                        quotes[target]=self.provider.fresh_quote(target)
                    now=aware(self.provider.now())
                    signal=confirmed_signal(plan,levels,bars,now,cfg)
                    if signal is None:
                        raise ValueError('Entry signal no longer eligible.')
                if symbol not in quotes:
                    raise ValueError('Valid stock quote unavailable.')
                validate_quote(quotes[symbol],now,cfg)
                if cfg.execution!='Simulation' and not orders_allowed:
                    raise ValueError('Broker orders are locked by account/risk approval settings.')
                rows=self.ledger.rows(self.book)
                if any(r['symbol']==symbol and (r['status']!='CLOSED' or r['day']==now.date().isoformat()) for r in rows):
                    raise ValueError('This stock already has an open position or a trade today.')
                for r in rows:
                    if r['status']!='CLOSED':
                        validate_quote(quotes[r['symbol']],now,cfg)
                limits=risk_snapshot(rows,now.date().isoformat(),quotes,cfg,option_quotes)
                if limits['positions']>=cfg.max_positions or limits['trades_today']>=cfg.max_daily_trades or limits['pnl']<=-cfg.max_daily_loss:
                    raise ValueError('Position, daily trade or daily loss limit reached.')
                q=quotes[symbol]
                sign=1 if signal['side']=='LONG' else -1
                reference=q['ask'] if sign==1 else q['bid']
                if sign*(reference-signal['levels']['entry'])<=0:
                    raise ValueError('Price has returned inside the trigger.')
                tick=self.provider.tick(symbol)
                for value in signal['levels'].values():
                    if abs(money(value,tick=tick)-value)>1e-8:
                        raise ValueError('Pine level does not match the broker price increment.')
                price=money(reference*(1+sign*cfg.max_entry_slippage_pct/100),up=sign==1,tick=tick)
                funds=self.provider.available_funds() if cfg.execution!='Simulation' else None
                equity=self.provider.account_equity() if cfg.account_allocation_pct else None
                contract=None
                if cfg.instrument=='OPT':
                    contract=self.provider.select_option(symbol,signal['side'],reference,now)
                    oq=self.provider.option_quote(contract)
                    if hasattr(self.provider,'now'):
                        q=self.provider.fresh_quote(symbol)
                        now=aware(self.provider.now())
                        validate_quote(q,now,cfg)
                        signal=confirmed_signal(plan,levels,bars,now,cfg)
                        if signal is None:
                            raise ValueError('Pine signal expired during option selection.')
                        reference=q['ask'] if sign==1 else q['bid']
                    if sign*(reference-signal['levels']['entry'])<=0 or sign*(reference-signal['levels']['t1'])>=0:
                        raise ValueError('Underlying is inside the entry trigger or beyond T1.')
                    validate_quote(oq,now,replace(cfg,max_spread_pct=cfg.option_max_spread_pct))
                    option_tick=self.provider.option_tick(contract)
                    price=money(oq['ask']*(1+cfg.max_entry_slippage_pct/100),up=True,tick=option_tick)
                    quantity=option_quantity(price,cfg,notional_used=limits['notional'],daily_capital_used=limits['daily_capital'],available_funds=funds,account_equity=equity)
                else:
                    quantity=share_quantity(signal,price,cfg,risk_used=limits['risk'],notional_used=limits['notional'],daily_capital_used=limits['daily_capital'],available_funds=funds,account_equity=equity)
                if cfg.execution!='Simulation':
                    if self.provider.has_stock_exposure(symbol) or (cfg.instrument=='OPT' and self.provider.has_option_exposure(symbol)):
                        raise ValueError('Existing broker stock position/order for this symbol.')
                    if cfg.instrument=='STK' and signal['side']=='SHORT' and (not isfinite(q.get('shortable_shares',float('nan'))) or q['shortable_shares']<quantity):
                        raise ValueError('Borrowable shares unavailable for short entry.')
                key=hashlib.sha256(f'{self.book}|{now.date()}|{symbol}'.encode()).hexdigest()[:24]
                row=dict(key=key,book=self.book,day=now.date().isoformat(),symbol=symbol,side=signal['side'],quantity=quantity,initial_quantity=quantity,entry_price=price,entry_capital=price*quantity,strategy_profile='pine_stocks',reserved_risk=abs(price-signal['levels']['stop'])*quantity,levels=signal['levels'],plan_hash=plan.source_hash,opened_at=now.isoformat(),status='SIM_OPEN' if cfg.execution=='Simulation' else 'INTENT',realized=0.,fees=0.,fills=[],legs=[dict(quantity=n,remaining=n,target=target) for n,target in target_legs(quantity,cfg)],signal=signal)
                if contract:
                    row.update(instrument='OPT',contract=contract,entry_underlying=reference,entry_capital=price*quantity*100,reserved_risk=price*quantity*100)
                if self.broker and cfg.execution!='Simulation':
                    row=self.broker.prepare(row)
                if not self.ledger.claim(row):
                    raise ValueError('Duplicate stock order suppressed.')
                if contract:
                    option_quotes[key]=oq
                if cfg.execution=='Simulation':
                    fee=quantity*cfg.option_commission_per_contract if contract else sum(max(cfg.simulation_min_fee,leg['quantity']*cfg.simulation_fee_per_share) for leg in row['legs'])
                    row['fees']=fee
                    row['realized']=-fee
                    row['daily_realized']={now.date().isoformat():-fee}
                    self.ledger.update(row)
                else:
                    try:
                        row=self.broker.submit(row)
                    except Exception as exc:
                        row['status']='UNKNOWN'
                        row['broker_error']=str(exc)
                    self.ledger.update(row)
                self.ledger.event('stock_entry',row,now)
                decisions.append(dict(symbol=symbol,status=row['status'],side=signal['side'],instrument=cfg.instrument,quantity=quantity,entry=price,contract=contract,stop=row['levels']['stop'],t1=row['levels']['t1'],t2=row['levels']['t2']))
            except (ValueError,KeyError,TypeError,RuntimeError) as exc:
                decisions.append(dict(symbol=symbol,status='BLOCKED',reason=str(exc)))
        result=dict(timestamp=now.isoformat(),mode=cfg.execution,plan_date=str(plan.session_date),decisions=decisions,positions=self.ledger.rows(self.book),message='Daily Pine stock levels evaluated')
        self.ledger.event('cycle',result,now)
        return result


class IBStockProvider:
    def __init__(self,ib,account):
        from market_data import provider_from_ib
        self.ib,self.account=ib,account
        self.data=provider_from_ib(ib)
        self.contracts={}

    def contract(self,symbol):
        if symbol not in self.contracts:
            self.contracts[symbol]=self.data.qualify_stock(symbol)
        if self.contracts[symbol].secType!='STK' or self.contracts[symbol].currency!='USD':
            raise ValueError('Only USD stock contracts are permitted.')
        return self.contracts[symbol]

    def now(self):
        return pd.Timestamp.now(tz='America/New_York')

    def fresh_quote(self,symbol):
        return IBStockProvider.quote(self,symbol)

    def quote(self,symbol):
        contract=self.contract(symbol)
        ticker=self.ib.reqMktData(contract,'236',snapshot=False)
        try:
            self.ib.sleep(1)
            if ticker.time is None:
                raise ValueError('No timestamped stock quote.')
            return dict(bid=float(ticker.bid),ask=float(ticker.ask),timestamp=aware(ticker.time).isoformat(),market_data_type=ticker.marketDataType,shortable_shares=float(ticker.shortableShares))
        finally:
            self.ib.cancelMktData(contract)

    def bars(self,symbol,minutes):
        end=self.now().floor(f'{minutes}min')
        key=(symbol,minutes)
        cached=_BAR_CACHE.get(key)
        if cached and cached[0]==end:
            return cached[1].copy()
        frame=self.data.intraday_bars(symbol,duration='2 D',bar_size=f'{minutes} min' if minutes==1 else f'{minutes} mins',use_rth=True)
        # Cache only completed bars, and only after the latest expected bar
        # arrives. A delayed historical response must be retried this interval.
        if not frame.empty and frame.index.tz is not None:
            frame=frame.loc[frame.index+pd.Timedelta(minutes=minutes)<=end]
            if not frame.empty and frame.index[-1]+pd.Timedelta(minutes=minutes)==end:
                _BAR_CACHE[key]=(end,frame.copy())
        return frame

    def tick(self,symbol):
        details=self.ib.reqContractDetails(self.contract(symbol))
        if not details or details[0].minTick<=0:
            raise ValueError('Broker tick increment unavailable.')
        return float(details[0].minTick)

    def select_option(self,symbol,side,underlying,now):
        from .pine_options import select_contract
        return select_contract(self.ib,self.contract(symbol),side,underlying,now)

    def option_quote(self,info):
        from .pine_options import reconstruct
        contract=reconstruct(info)
        ticker=self.ib.reqMktData(contract,'',snapshot=False)
        try:
            self.ib.sleep(1)
            if ticker.time is None:
                raise ValueError('No timestamped option quote.')
            return dict(bid=float(ticker.bid),ask=float(ticker.ask),timestamp=aware(ticker.time).isoformat(),market_data_type=ticker.marketDataType)
        finally:
            self.ib.cancelMktData(contract)

    def option_tick(self,info):
        from .pine_options import reconstruct
        details=self.ib.reqContractDetails(reconstruct(info))
        if not details or details[0].minTick<=0:
            raise ValueError('Option tick increment unavailable.')
        return float(details[0].minTick)

    def has_option_exposure(self,symbol):
        self.ib.reqAllOpenOrders()
        return any(p.account==self.account and p.contract.secType=='OPT' and p.contract.symbol==symbol and p.position!=0 for p in self.ib.positions()) or any(t.order.account==self.account and t.contract.secType=='OPT' and t.contract.symbol==symbol for t in self.ib.openTrades())

    def account_equity(self):
        from .strategy_controls import verified_account_equity
        return verified_account_equity(self.ib,self.account)

    def available_funds(self):
        values=[float(r.value) for r in self.ib.accountSummary() if r.account==self.account and r.tag=='AvailableFunds' and r.currency=='USD']
        if not values or not isfinite(values[0]) or values[0]<=0:
            raise ValueError('Verified USD available funds unavailable.')
        return values[0]

    def has_stock_exposure(self,symbol):
        self.ib.reqAllOpenOrders()
        return any(p.account==self.account and p.contract.secType=='STK' and p.contract.symbol==symbol and p.position!=0 for p in self.ib.positions()) or any(t.order.account==self.account and t.contract.secType=='STK' and t.contract.symbol==symbol for t in self.ib.openTrades())


class IBStockBroker:
    def __init__(self,provider):
        self.provider,self.ib,self.account=provider,provider.ib,provider.account

    def prepare(self,row):
        for i,leg in enumerate(row['legs']):
            leg.update(parent_id=self.ib.client.getReqId(),target_id=self.ib.client.getReqId(),stop_id=self.ib.client.getReqId(),ref=f"pine-{row['key']}-{i}")
            if row.get('instrument')=='OPT':
                leg['expiry_id']=self.ib.client.getReqId()
        return row

    def submit(self,row):
        from ib_insync import LimitOrder,StopOrder
        contract=self.provider.contract(row['symbol'])
        if row.get('instrument')=='OPT':
            from .pine_options import reconstruct,exit_order,expiry_order
            underlying=contract
            contract=reconstruct(row['contract'])
            # Build every order before submitting the first parent.
            orders=[]
            for leg in row['legs']:
                parent=LimitOrder('BUY',leg['quantity'],row['entry_price'],orderId=leg['parent_id'],account=self.account,orderRef=leg['ref']+'-entry',tif='FOK',outsideRth=False,transmit=False)
                target=exit_order('SELL',leg['quantity'],leg['target_id'],leg['parent_id'],leg['ref']+'-target',leg['ref'],underlying,row['levels'][leg['target']],row['side']=='LONG')
                stop=exit_order('SELL',leg['quantity'],leg['stop_id'],leg['parent_id'],leg['ref']+'-stop',leg['ref'],underlying,row['levels']['stop'],row['side']!='LONG')
                expiry=expiry_order(leg['quantity'],leg['expiry_id'],leg['parent_id'],leg['ref']+'-expiry',leg['ref'],row['contract']['expiry'])
                for order in (target,stop,expiry):
                    order.account=self.account
                orders.extend((parent,target,stop,expiry))
            for order in orders:
                self.ib.placeOrder(contract,order)
            row['status']='SUBMITTED'
            return row
        action='BUY' if row['side']=='LONG' else 'SELL'
        opposite='SELL' if action=='BUY' else 'BUY'
        for leg in row['legs']:
            parent=LimitOrder(action,leg['quantity'],row['entry_price'],orderId=leg['parent_id'],account=self.account,orderRef=leg['ref']+'-entry',tif='FOK',outsideRth=False,transmit=False)
            target=LimitOrder(opposite,leg['quantity'],row['levels'][leg['target']],orderId=leg['target_id'],parentId=leg['parent_id'],account=self.account,orderRef=leg['ref']+'-target',tif='GTC',outsideRth=False,ocaGroup=leg['ref'],ocaType=2,transmit=False)
            stop=StopOrder(opposite,leg['quantity'],row['levels']['stop'],orderId=leg['stop_id'],parentId=leg['parent_id'],account=self.account,orderRef=leg['ref']+'-stop',tif='GTC',outsideRth=False,ocaGroup=leg['ref'],ocaType=2,transmit=True)
            for order in (parent,target,stop):
                self.ib.placeOrder(contract,order)
        row['status']='SUBMITTED'
        return row

    def reconcile(self,row):
        trades=list(self.ib.trades())+list(self.ib.reqAllOpenOrders())+list(self.ib.reqCompletedOrders(apiOnly=False))
        indexed={t.order.orderRef:t for t in trades if t.order.account==self.account}
        pending=False
        quantity=0
        realized=0.
        fees=0.
        entry_filled=0
        entry_value=0.
        unknown=False
        daily={}
        multiplier=100 if row.get('instrument')=='OPT' else 1
        pnl_sign=1 if multiplier==100 or row['side']=='LONG' else -1
        for leg in row['legs']:
            parent=indexed.get(leg['ref']+'-entry')
            target=indexed.get(leg['ref']+'-target')
            stop=indexed.get(leg['ref']+'-stop')
            expiry=indexed.get(leg['ref']+'-expiry') if multiplier==100 else None
            if parent is None:
                unknown=True
                continue
            p=parent.orderStatus
            n=int(p.filled)
            if n not in (0,leg['quantity']):
                unknown=True # FOK partial fill is unexpected; freeze new entries.
            if p.status not in ('Filled','Cancelled','ApiCancelled','Inactive'):
                pending=True
            sold=sum(int(t.orderStatus.filled) for t in (target,stop,expiry) if t is not None)
            if sold>n:
                unknown=True
            if n>sold and (stop is None or stop.orderStatus.status in ('Cancelled','ApiCancelled','Inactive')):
                unknown=True # Never silently accept unprotected holdings.
            quantity+=max(0,n-sold)
            entry_filled+=n
            entry_value+=n*p.avgFillPrice
            for trade in (parent,target,stop,expiry):
                if trade:
                    fees+=sum(f.commissionReport.commission for f in trade.fills if isfinite(f.commissionReport.commission))
                    for f in trade.fills:
                        day=aware(f.time).date().isoformat()
                        value=0. if trade is parent else pnl_sign*multiplier*(f.execution.price-p.avgFillPrice)*f.execution.shares
                        commission=f.commissionReport.commission
                        daily[day]=daily.get(day,0.)+value-(commission if isfinite(commission) else 0.)
            realized+=sum(pnl_sign*multiplier*(t.orderStatus.avgFillPrice-p.avgFillPrice)*t.orderStatus.filled for t in (target,stop,expiry) if t is not None)
            leg['remaining']=max(0,n-sold)
            leg['broker_status']=dict(entry=p.status,target=target.orderStatus.status if target else 'UNKNOWN',stop=stop.orderStatus.status if stop else 'UNKNOWN')
        row['status']='UNKNOWN' if unknown else 'SUBMITTED' if pending else 'OPEN' if quantity else 'CLOSED'
        if not unknown and not pending:
            row['entry_capital']=entry_value*multiplier
        if entry_filled:
            row['entry_price']=entry_value/entry_filled
        row['quantity']=row['initial_quantity'] if unknown or pending else quantity
        row['realized']=realized-fees
        row['fees']=fees
        if daily:
            row['daily_realized']=daily
        row['reserved_risk']=(row['entry_price']*100 if multiplier==100 else abs(row['entry_price']-row['levels']['stop']))*row['quantity']
        return row


def broker_orders_allowed(app,settings):
    from bot_core import orders_unlocked_from_config
    desired='Paper' if settings.execution=='IBKR Paper' else 'Live'
    phrase='TRADE PINE OPTIONS LIVE' if settings.instrument=='OPT' else 'TRADE STOCK LEVELS LIVE'
    return settings.execution!='Simulation' and app.get('account_mode')==desired and orders_unlocked_from_config(app) and (desired!='Live' or settings.live_confirmation==phrase)


def run_pine_cycle(app,*,manage_only=False):
    # Serialize the portfolio decision and durable intent across processes.
    import fcntl
    lock_path=ROOT/'data'/'pine_stock_cycle.lock'
    lock_path.parent.mkdir(parents=True,exist_ok=True)
    with lock_path.open('a') as handle:
        try:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            return _run_pine_cycle(app,manage_only=manage_only)
        finally:
            fcntl.flock(handle,fcntl.LOCK_UN)


def _run_pine_cycle(app,*,manage_only=False):
    from bot_core import IBConfig,connect_ib,write_health
    settings=Settings(**dict(app.get('pine_stocks',{}).get('settings',{}),bar_minutes=5))
    plan=load_plan()
    now=pd.Timestamp.now(tz='America/New_York')
    session=bounds(now)
    if not session or not session[0]<=now<session[1]:
        write_health(last_status='Daily stock levels: waiting for regular market hours',auto_trading=False)
        return
    connection=app.get('ib',{})
    mode='Live' if settings.execution=='IBKR Live' else 'Paper'
    allowed=broker_orders_allowed(app,settings)
    ib=connect_ib(IBConfig(host=connection.get('host','127.0.0.1'),port=int(connection.get('live_port',7496) if mode=='Live' else connection.get('paper_port',7497)),client_id=int(connection.get('client_id',11))+107,account=connection.get('account') or None,readonly=not allowed))
    ledger=Ledger()
    try:
        accounts=ib.managedAccounts()
        account=connection.get('account') or (accounts[0] if len(accounts)==1 else None)
        if not account or account not in accounts:
            raise ValueError('Select an explicit connected IBKR account for stock levels.')
        if (mode=='Paper' and not account.startswith('D')) or (mode=='Live' and account.startswith('D')):
            raise ValueError('Selected execution mode does not match the connected account.')
        provider=IBStockProvider(ib,account)
        broker=IBStockBroker(provider) if settings.execution!='Simulation' else None
        # Quote timestamps are retrieved after this cycle started; take the
        # decision timestamp after collection so valid current quotes are not future.
        original_quote=provider.quote
        quotes={}
        rows=ledger.rows(f'{settings.execution}:{account}')
        symbols={r['symbol'] for r in rows if r['status']!='CLOSED'}|({l.symbol for l in plan.levels} if plan and not manage_only else set())
        for symbol in sorted(symbols):
            try:
                quotes[symbol]=original_quote(symbol)
            except Exception:
                pass
        provider.quote=lambda symbol:quotes[symbol] if symbol in quotes else (_ for _ in ()).throw(ValueError('Stock quote unavailable.'))
        now=pd.Timestamp.now(tz='America/New_York')
        engine=StockEngine(settings,ledger,provider,broker,book=f'{settings.execution}:{account}')
        result=engine.cycle(plan,now,manage_only=manage_only,orders_allowed=allowed)
        STATUS_FILE.parent.mkdir(parents=True,exist_ok=True)
        temp=STATUS_FILE.with_suffix('.tmp')
        temp.write_text(json.dumps(result,indent=2,default=str,allow_nan=False))
        temp.replace(STATUS_FILE)
        write_health(last_status=result['message'],mode=settings.execution,positions_monitored=sum(r['status']!='CLOSED' for r in result['positions']),auto_trading=allowed)
    finally:
        ledger.close()
        ib.disconnect()
