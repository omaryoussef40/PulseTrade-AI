"""Additional research-only controls for the two-leg straddle simulation."""
import streamlit as st


def render_controls(saved):
    values={}
    with st.expander('Entry timing and breakout confirmation',expanded=True):
        a,b,c=st.columns(3)
        values['orb_minutes']=a.selectbox('Opening range (minutes)',[5,10,15,30,45,60,90],index=[5,10,15,30,45,60,90].index(int(saved.get('orb_minutes',15))),key='straddle_orb')
        values['confirmation_minutes']=b.selectbox('Confirmation window (minutes)',[5,10,15,30,45,60],index=[5,10,15,30,45,60].index(int(saved.get('confirmation_minutes',15))),key='straddle_confirmation')
        entry=f"{int(saved.get('entry_hour',9)):02d}:{int(saved.get('entry_minute',30)):02d}"
        times=[f'{hour:02d}:{minute:02d}' for hour in (9,10,11) for minute in range(0,60,5) if hour>9 or minute>=30]
        time=c.selectbox('Buy both legs at (New York)',times,index=times.index(entry) if entry in times else 0,key='straddle_entry')
        values['entry_hour'],values['entry_minute']=map(int,time.split(':'))
        a,b,c=st.columns(3)
        values['breakout_buffer_pct']=a.number_input('Minimum close beyond ORB (%)',min_value=0.,max_value=5.,value=float(saved.get('breakout_buffer_pct',0.)),step=.05,key='straddle_breakout_buffer',help='0 uses the original strict close outside the opening range. Higher values require a larger breakout.')
        directions=['Both','CALL','PUT']
        values['allowed_direction']=b.selectbox('Permitted breakout direction',directions,index=directions.index(saved.get('allowed_direction','Both')),format_func=lambda value:{'Both':'Calls or puts','CALL':'Keep calls only','PUT':'Keep puts only'}[value],key='straddle_direction')
        actions=['Close both','Hold both to timed exit']
        values['no_break_action']=c.selectbox('If no breakout passes the filters',actions,index=actions.index(saved.get('no_break_action','Close both')),key='straddle_no_break')
        a,b,c=st.columns(3)
        values['require_vwap']=a.checkbox('Require confirmation on the correct side of VWAP',value=bool(saved.get('require_vwap',False)),key='straddle_vwap',help='Keep a call only above regular-session VWAP, or a put only below it.')
        values['require_volume']=b.checkbox('Require stronger confirmation volume',value=bool(saved.get('require_volume',False)),key='straddle_volume')
        values['min_confirmation_volume_ratio']=c.number_input('Confirmation / opening volume ratio',min_value=.1,max_value=10.,value=float(saved.get('min_confirmation_volume_ratio',1.)),step=.1,disabled=not values['require_volume'],key='straddle_volume_ratio',help='Average five-minute volume during confirmation divided by average five-minute volume during the opening range.')
    with st.expander('Winner protection and timed exits'):
        a,b,c=st.columns(3)
        values['winner_stop_enabled']=a.checkbox('Enable winner stop',value=bool(saved.get('winner_stop_enabled',True)),key='straddle_stop_enabled')
        values['winner_target_enabled']=b.checkbox('Enable winner profit target',value=bool(saved.get('winner_target_enabled',True)),key='straddle_target_enabled')
        values['trailing_enabled']=c.checkbox('Enable winner trailing stop',value=bool(saved.get('trailing_enabled',True)),key='straddle_trailing_enabled')
        a,b=st.columns(2)
        values['breakeven_enabled']=a.checkbox('Move winner stop to entry price',value=bool(saved.get('breakeven_enabled',False)),key='straddle_breakeven_enabled')
        values['breakeven_trigger_pct']=b.number_input('Breakeven activation profit (%)',min_value=1.,max_value=500.,value=float(saved.get('breakeven_trigger_pct',20.)),step=1.,disabled=not values['breakeven_enabled'],key='straddle_breakeven')
        a,b=st.columns(2)
        values['force_exit_hour']=a.number_input('Timed exit hour (New York)',min_value=9,max_value=16,value=int(saved.get('force_exit_hour',15)),key='straddle_exit_hour')
        values['force_exit_minute']=b.selectbox('Timed exit minute',[0,5,10,15,20,25,30,35,40,45,50,55],index=[0,5,10,15,20,25,30,35,40,45,50,55].index(int(saved.get('force_exit_minute',55))),key='straddle_exit_minute')
        st.caption('Winner percentages use its original entry premium. Protection starts after confirmation. Breakeven is before exit slippage and fees. Timed exit remains active when the other exits are disabled.')
    with st.expander('Position size and trading costs'):
        a,b=st.columns(2)
        values['contracts_per_leg']=a.number_input('Contracts per leg',min_value=1,max_value=1000,value=int(saved.get('contracts_per_leg',1)),key='straddle_quantity',help='2 means buying two calls and two puts. Results include the full position cost.')
        values['commission_per_contract']=b.number_input('Commission per contract per transaction ($)',min_value=0.,max_value=100.,value=float(saved.get('commission_per_contract',.65)),step=.05,key='straddle_commission')
    return values
