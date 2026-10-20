"""User-facing strategy assignment and daily budget controls."""
from dataclasses import asdict
import pandas as pd
import streamlit as st
from .strategy_controls import LABELS,active_profile,default_option_profile,validate_option_profile,validate_allocation
from .pine_levels import load_plan,parse_pine,ROOT
from .pine_stock_signals import Settings
from .pine_stock_engine import LEDGER_FILE,Ledger


def render(app, allocation_only=False):
    from bot_core import load_config,save_config,read_active_positions
    st.subheader('Capital Allocation' if allocation_only else 'Trading controls')
    pine_options=app.get('pine_stocks',{}).get('settings',{}).get('instrument')=='OPT'
    st.caption('Run one strategy or PMB and Daily Pine together. Save separate limits for each profile. Existing broker order approvals still apply.')
    st.dataframe(pd.DataFrame([{'Strategy':LABELS[key],'Trades':'7-DTE calls / puts' if pine_options else 'Shares','Ticker source':'Your daily Pine plan'} if key=='pine_stocks' else {'Strategy':LABELS[key],'Trades':'Call / put options','Ticker source':'Approved watchlist'} for key in ('pine_stocks','pmb','brt')]),hide_index=True,width='stretch')
    choices=list(LABELS)
    current=active_profile(app)
    active=st.selectbox('Strategy allowed to open new trades',choices,index=choices.index(current) if current in choices else choices.index('paused'),format_func=lambda key:LABELS[key],key='active_entry_profile')
    allocation=dict(app.get('strategy_control',{}).get('allocation',{}))
    if active=='dual':
        left,right=st.columns(2)
        with left:
            allocation['pmb_pct']=st.number_input('Account equity allocated to PMB options (%)',min_value=1.,max_value=99.,value=float(allocation.get('pmb_pct',80.)))
        with right:
            allocation['stocks_pct']=st.number_input('Account equity allocated to Daily Pine (%)',min_value=1.,max_value=99.,value=float(allocation.get('stocks_pct',20.)))
        allocation['stock_margin']=st.number_input('Stock margin multiplier',min_value=1.,max_value=2.,value=float(allocation.get('stock_margin',2.)),step=.1,disabled=pine_options)
        try:validate_allocation(allocation)
        except ValueError as exc:
            st.error(str(exc))
            return
        st.caption('Allocations use verified USD account equity. Pine options use premium limits with no stock margin. Stock shares use their allocation × margin. Broker available funds also limit entries; PMB keeps its own signals and exits.')
    edit=None
    option=None
    stock=None
    if not allocation_only:
        edit_choices=['pine_stocks','brt'] if active=='dual' else choices[:3]
        edit=st.selectbox('Edit budget and ticker permissions for',edit_choices,format_func=lambda key:LABELS[key],key='edit_entry_profile')
        stock=Settings(**app.get('pine_stocks',{}).get('settings',{}))
        profiles=dict(app.get('strategy_control',{}).get('profiles',{}))
        option=None
        if edit=='pine_stocks':
            values=asdict(stock)
            if pine_options:
                for name,label in [('option_premium_budget','Maximum premium per Pine trade ($)'),('option_max_daily_premium','Maximum new Pine premium per day ($)'),('option_max_total_premium','Maximum open Pine premium ($)')]:
                    values[name]=st.number_input(label,min_value=1.,value=float(values[name]),key='controls_'+name)
                values['option_max_contracts']=st.number_input('Maximum contracts per Pine trade',min_value=1,value=int(values['option_max_contracts']),key='controls_pine_contracts')
                st.caption('Instrument: 7-DTE calls/puts. Change instrument in Daily Pine Setup. The full premium is reserved as risk; exits follow the underlying Pine levels.')
            else:
                values['max_daily_capital']=st.number_input('Maximum new stock entry value per day ($)',min_value=1.,value=float(stock.max_daily_capital),key='controls_stock_daily_capital')
                values['max_position_dollars']=st.number_input('Maximum value per stock trade ($)',min_value=1.,value=float(stock.max_position_dollars),key='controls_stock_per_trade')
                values['risk_dollars']=st.number_input('Maximum stop risk per stock trade ($)',min_value=1.,value=float(stock.risk_dollars),key='controls_stock_stop_risk')
            values['max_daily_loss']=st.number_input('Stock daily loss limit ($)',min_value=1.,value=float(stock.max_daily_loss),key='controls_stock_daily_loss')
            values['max_daily_trades']=st.number_input('Maximum stock trades per day',min_value=1,value=int(stock.max_daily_trades),key='controls_stock_trade_count')
            values['max_positions']=st.number_input('Maximum stocks open together',min_value=1,value=int(stock.max_positions),key='controls_stock_open_count')
            if pine_options:
                values['option_allow_calls']=st.checkbox('Buy calls on buy signals',value=stock.option_allow_calls,key='controls_pine_calls')
                values['option_allow_puts']=st.checkbox('Buy puts on sell signals',value=stock.option_allow_puts,key='controls_pine_puts')
            else:
                values['enable_longs']=st.checkbox('Allow long stock entries',value=stock.enable_longs,key='controls_stock_longs')
                values['enable_shorts']=st.checkbox('Allow short stock entries',value=stock.enable_shorts,key='controls_stock_shorts')
            values['trade_all_plan_symbols']=st.checkbox('Allow every ticker in the Pine plan',value=stock.trade_all_plan_symbols,key='controls_stock_all_symbols')
            symbols=[]
            try:
                draft=st.session_state.get('daily_pine_draft')
                plan=parse_pine(draft) if draft else load_plan()
                if plan is None:
                    example=ROOT/'examples'/'daily_trade_levels_2026-10-08.pine'
                    plan=parse_pine(example.read_text()) if example.exists() else None
                symbols=[l.symbol for l in plan.levels] if plan else []
            except ValueError:pass
            values['allowed_symbols']=tuple(st.multiselect('Approved stock tickers',symbols,default=[s for s in stock.allowed_symbols if s in symbols],disabled=values['trade_all_plan_symbols'],key='controls_stock_symbols'))
            stock=Settings(**values)
            st.caption('Daily entry value is cumulative: closing a trade does not replenish it. Position value, stop risk and daily loss are separate limits. An empty approved list allows no entries when “Allow every ticker” is off.')
        else:
            option=dict(profiles.get(edit,default_option_profile(app)))
            option['daily_capital']=st.number_input('Maximum new option premium per day ($)',min_value=1.,value=float(option['daily_capital']),key=edit+'_daily_capital')
            option['per_trade_capital']=st.number_input('Maximum premium per option trade ($)',min_value=1.,value=float(option['per_trade_capital']),key=edit+'_per_trade')
            option['max_daily_trades']=st.number_input('Maximum option trades per day',min_value=1,value=int(option['max_daily_trades']),key=edit+'_count')
            option['all_watchlist']=st.checkbox('Allow the existing app watchlist',value=bool(option.get('all_watchlist',True)),key=edit+'_all_symbols')
            text=st.text_input('Approved option underlying tickers (comma separated)',value=', '.join(option.get('symbols',[])),disabled=option['all_watchlist'],key=edit+'_symbols')
            option['symbols']=sorted(set(s.strip().upper() for s in text.split(',') if s.strip()))
            try:
                validate_option_profile(option)
            except ValueError as exc:
                st.error(str(exc))
                return
            profiles[edit]=option
            st.caption('PMB and Break & Retest save separate caps, but use the existing shared option trade ledger. All option entries today count against the selected profile: switching profiles does not reset spending or trade count. Controlled option entries use limit orders.')
    st.caption('Single-strategy profiles override opening-ORB and staged-timeline presets. Combined mode preserves PMB settings and runs the stock scanner independently. Pause blocks new automatic entries; the active position monitor continues under the existing trading permissions.')
    if st.button('Save strategy selection and limits',type='primary'):
        fresh=load_config()
        old=active_profile(fresh)
        previous=fresh.get('strategy_control',{}).get('last_active', 'pine_stocks' if fresh.get('pine_stocks',{}).get('enabled') else fresh.get('strategy',{}).get('active_strategy','pmb')) if old=='paused' else old
        open_stock=[]
        if LEDGER_FILE.exists():
            ledger=Ledger()
            try:open_stock=ledger.db.execute("SELECT key FROM trades WHERE status!='CLOSED'").fetchall()
            finally:ledger.close()
        adds_strategy=active=='dual' and previous in ('pmb','pine_stocks')
        if active not in ('paused',previous) and not adds_strategy and (open_stock or read_active_positions()):
            st.error('Close or reconcile existing positions before switching entry strategies. You can pause new entries while positions are open.')
        else:
            controls=fresh.setdefault('strategy_control',{})
            controls.update(enabled=True,active=active,last_active=previous if active=='paused' else active)
            if active=='dual':controls['allocation']=allocation
            if option is not None:controls.setdefault('profiles',{})[edit]=option
            if edit=='pine_stocks':fresh.setdefault('pine_stocks',{})['settings']=asdict(stock)
            if active!='paused':fresh.setdefault('pine_stocks',{})['enabled']=active in ('pine_stocks','dual')
            save_config(fresh)
            st.success('Saved. The engine applies these controls on its next cycle. Restart an engine that was running before this update once to load the new controls.')
    st.info('Workflow: paste today’s script → review and save levels → choose the strategy and budgets here → review Live Trading → Controls. The script itself never sends an order.')
