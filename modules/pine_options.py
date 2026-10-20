"""7-DTE contract selection and stock-level conditional option exits."""
from datetime import datetime,timedelta
from math import isfinite
import sqlite3
import json

from .pine_stock_signals import aware,bounds


def select_contract(ib,stock,side,underlying,now):
    from ib_insync import Option
    chains=ib.reqSecDefOptParams(stock.symbol,'',stock.secType,stock.conId)
    chains=[chain for chain in chains if str(chain.multiplier)=='100']
    if not chains:
        raise ValueError('No standard 100-share option chain available.')
    chain=next((chain for chain in chains if chain.exchange=='SMART'),chains[0])
    target=aware(now).date()+timedelta(days=7)
    expiries=[value for value in chain.expirations if datetime.strptime(value,'%Y%m%d').date()>aware(now).date()]
    if not expiries:
        raise ValueError('No unexpired options available.')
    expiry=min(expiries,key=lambda value:(abs((datetime.strptime(value,'%Y%m%d').date()-target).days),value))
    strikes=sorted((float(value) for value in chain.strikes if isfinite(float(value)) and float(value)>0),key=lambda value:(abs(value-underlying),value))
    for strike in strikes[:5]:
        contract=Option(stock.symbol,expiry,strike,'C' if side=='LONG' else 'P','SMART',currency='USD',multiplier='100',tradingClass=chain.tradingClass)
        qualified=ib.qualifyContracts(contract)
        if qualified:
            contract=qualified[0]
            return dict(symbol=stock.symbol,expiry=expiry,strike=strike,right=contract.right,
                conId=contract.conId,localSymbol=contract.localSymbol,multiplier=100,
                actual_dte=(datetime.strptime(expiry,'%Y%m%d').date()-aware(now).date()).days)
    raise ValueError('No nearby at-the-money contract could be qualified.')


def reconstruct(info):
    from ib_insync import Option
    return Option(info['symbol'],info['expiry'],float(info['strike']),info['right'],'SMART',
        currency='USD',multiplier='100',conId=int(info.get('conId') or 0))


def exit_order(action,quantity,order_id,parent_id,ref,group,underlying,level,above):
    from ib_insync import MarketOrder,PriceCondition
    condition=PriceCondition(price=float(level),conId=underlying.conId,exch='SMART',isMore=above,triggerMethod=2)
    return MarketOrder(action,quantity,orderId=order_id,parentId=parent_id,orderRef=ref,
        conditions=[condition],conditionsIgnoreRth=False,tif='GTC',outsideRth=False,
        ocaGroup=group,ocaType=2,transmit=False)


def expiry_order(quantity,order_id,parent_id,ref,group,expiry):
    from ib_insync import MarketOrder,TimeCondition
    import pandas as pd
    day=datetime.strptime(expiry,'%Y%m%d').date()
    session=bounds(pd.Timestamp(day,tz='America/New_York'))
    if not session:
        raise ValueError('Option expiration has no regular trading session.')
    deadline=session[1]-pd.Timedelta(minutes=5)
    return MarketOrder('SELL',quantity,orderId=order_id,parentId=parent_id,orderRef=ref,
        conditions=[TimeCondition(isMore=True,time=deadline.strftime('%Y%m%d %H:%M:%S US/Eastern'))],
        conditionsIgnoreRth=False,tif='GTC',outsideRth=False,ocaGroup=group,ocaType=2,transmit=True)


def expiry_deadline(expiry):
    import pandas as pd
    day=datetime.strptime(expiry,'%Y%m%d').date()
    session=bounds(pd.Timestamp(day,tz='America/New_York'))
    if not session:
        raise ValueError('Option expiration has no regular trading session.')
    return session[1]-pd.Timedelta(minutes=5)


def owned_contracts(path,account=None):
    """IDs excluded from the PMB position importer while Pine owns them."""
    from pathlib import Path
    if not Path(path).exists():
        return set()
    with sqlite3.connect(f'file:{path}?mode=ro',uri=True) as db:
        rows=[json.loads(row[0]) for row in db.execute("SELECT payload FROM trades WHERE status!='CLOSED'")]
    return {int(row['contract']['conId']) for row in rows if row.get('instrument')=='OPT'
        and row.get('contract',{}).get('conId') and (not account or row.get('book','').endswith(':'+account))
        and not row.get('book','').startswith('Simulation')}
