"""Read-only chart wall with the exact imported stock levels."""
import json
import subprocess
import sys
import time
from dataclasses import asdict
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from .pine_levels import ROOT
from .pine_stock_signals import bounds

CHART_REFRESH_SECONDS = 5 * 60

LEVEL_STYLES = (
    ('longEntry','Long entry','#2563eb','solid'),
    ('longStop','Long stop','#dc2626','dash'),
    ('longT1','Long T1','#16a34a','dot'),
    ('longT2','Long T2','#0d9488','dot'),
    ('shortEntry','Short entry','#9333ea','solid'),
    ('shortStop','Short stop','#ea580c','dash'),
    ('shortT1','Short T1','#16a34a','dot'),
    ('shortT2','Short T2','#0d9488','dot'),
)


@st.cache_data(ttl=CHART_REFRESH_SECONDS, show_spinner=False)
def chart_snapshot(symbols, day, host, port, client_id, refresh_slot=None):
    payload=dict(symbols=list(symbols),day=str(day),host=host,port=port,client_id=client_id,chart_data_version='extended-utc-v3')
    try:
        worker=subprocess.run([sys.executable,'-m','modules.pine_stock_chart_data',json.dumps(payload)],
            cwd=str(ROOT),capture_output=True,text=True,timeout=75)
        if worker.returncode:
            raise RuntimeError('Chart data could not be loaded. Check your IBKR connection and data permissions.')
        return json.loads(worker.stdout.strip().splitlines()[-1])
    except (OSError,ValueError,IndexError,RuntimeError,subprocess.TimeoutExpired) as exc:
        return dict(day=str(day),fetched_at=pd.Timestamp.now(tz='America/New_York').isoformat(),source='IBKR',
            candles={},errors={symbol:str(exc) for symbol in symbols})


def candle_frame(records):
    if not records:
        return pd.DataFrame(columns=['Open','High','Low','Close','Volume'])
    frame=pd.DataFrame(records)
    frame.index=pd.to_datetime(frame.pop('timestamp'),utc=True).dt.tz_convert('America/New_York')
    return frame


def make_level_chart(levels, frame, day, plan_date):
    fig=go.Figure()
    session=bounds(pd.Timestamp(day,tz='America/New_York'))
    start,end=session if session else (pd.Timestamp(day,tz='America/New_York')+pd.Timedelta(hours=9,minutes=30),pd.Timestamp(day,tz='America/New_York')+pd.Timedelta(hours=16))
    # Plotly labels these display-only times in New York, regardless of the
    # browser's local zone. Data validation retains aware timestamps upstream.
    start,end=start.tz_localize(None),end.tz_localize(None)
    categories=[]
    ticks=[]
    tick_labels=[]
    if not frame.empty:
        x=frame.index.tz_convert('America/New_York').tz_localize(None)
        categories=x.strftime('%Y-%m-%d %H:%M').tolist()
        # An observed-candle category axis removes overnight, holiday and
        # empty intervals without filling missing candles with invented data.
        hours=set()
        for ts,label in zip(x,categories):
            hour=(ts.date(),ts.hour)
            if hour not in hours or ts in (start,end):
                ticks.append(label)
                tick_labels.append(ts.strftime('%H:%M'))
                hours.add(hour)
        fig.add_trace(go.Candlestick(x=categories,open=frame.Open,high=frame.High,low=frame.Low,close=frame.Close,
            name='Price',increasing_line_color='#16a34a',decreasing_line_color='#dc2626',showlegend=False))
        regular=(x>=start)&(x+pd.Timedelta(minutes=5)<=end)
        regular_frame=frame.loc[regular]
        regular_x=x[regular]
        expected=pd.date_range(start,regular_x[-1],freq='5min') if len(regular_x) else pd.DatetimeIndex([])
        if len(regular_x) and regular_x.equals(expected):
            cumulative=regular_frame.Volume.cumsum().replace(0,float('nan'))
            vwap=(((regular_frame.High+regular_frame.Low+regular_frame.Close)/3)*regular_frame.Volume).cumsum()/cumulative
            fig.add_trace(go.Scatter(x=regular_x.strftime('%Y-%m-%d %H:%M').tolist(),y=vwap,mode='lines',name='Market VWAP',line=dict(color='#475569',width=1.8)))
        for mask,name,color in ((x<start,'Premarket','rgba(37,99,235,.06)'),(regular,'Market','rgba(100,116,139,.03)'),(x>=end,'Postmarket','rgba(147,51,234,.06)')):
            observed=[label for label,include in zip(categories,mask) if include]
            if observed:
                fig.add_vrect(x0=observed[0],x1=observed[-1],fillcolor=color,line_width=0,layer='below',
                    annotation_text=name,annotation_position='top left',annotation_font=dict(size=10,color='#64748b'))
    for field,label,color,dash in LEVEL_STYLES:
        price=getattr(levels,field)
        fig.add_hline(y=price,line=dict(color=color,width=1.4,dash=dash),
            exclude_empty_subplots=False,
            annotation_text=f'{label} {price:,.2f}',annotation_position='right',
            annotation_font=dict(color=color,size=10),name=label)
    if frame.empty:
        prices=[getattr(levels,field) for field,_,_,_ in LEVEL_STYLES]
        pad=max((max(prices)-min(prices))*.1,.01)
        fig.update_yaxes(range=[min(prices)-pad,max(prices)+pad])
        fig.add_annotation(x=.5,y=.5,xref='paper',yref='paper',text='Waiting for candle data',showarrow=False,
            bgcolor='rgba(255,255,255,.9)',font=dict(color='#64748b',size=13))
    fig.update_layout(height=420,template='plotly_white',margin=dict(l=10,r=135,t=10,b=40),
        xaxis=dict(title='New York time · active candles only',type='category',categoryorder='array',categoryarray=categories,
            range=[-.5,len(categories)-.5] if categories else None,rangeslider=dict(visible=False),
            tickmode='array',tickvals=ticks,ticktext=tick_labels),
        yaxis=dict(title='Price ($)',tickformat='.2f',fixedrange=False),
        legend=dict(orientation='h',y=1.03,x=0,font=dict(size=10)),
        hovermode='x unified',dragmode='pan',
        uirevision=f'extended-v2:{levels.symbol}:{day}:{plan_date}:{tuple(asdict(levels).values())}')
    return fig


def render(plan, app):
    st.caption('Premarket, market and postmarket · Five-minute completed candles · Closed and empty intervals hidden')
    left,right=st.columns([2,1])
    with left:
        day=st.date_input('Chart session (New York)',value=plan.session_date,key=f'stock_chart_day_{plan.source_hash}')
    with right:
        automatic=st.toggle('Refresh charts every 5 minutes',value=True,key='stock_chart_auto_refresh')
    if day!=plan.session_date:
        st.warning(f'Charts show {day}, with levels from {plan.session_date}. These levels cannot trigger new trades on a different date.')
    elif plan.session_date!=pd.Timestamp.now(tz='America/New_York').date():
        st.info(f'Reviewing the {plan.session_date} plan. New trades require a plan dated for today’s New York session.')
    st.caption('Blue / purple: long / short entry · Red / orange: stops · Green: T1 · Teal: T2 · Gray: VWAP. Drag to pan; scroll to zoom.')
    st.caption('Chart data uses your configured IBKR connection in read-only mode. Keep TWS or IB Gateway open. Chart refresh does not submit orders.')
    connection=app.get('ib',{})
    port=int(connection.get('live_port',7496) if app.get('account_mode')=='Live' else connection.get('paper_port',7497))
    symbols=tuple(row.symbol for row in plan.levels)

    @st.fragment(run_every=CHART_REFRESH_SECONDS if automatic else None)
    def chart_wall():
        if st.button('Refresh charts now',key='stock_chart_refresh'):
            chart_snapshot.clear()
        with st.spinner('Loading ticker charts…'):
            # Each five-minute tick gets a new cache key. Otherwise a cache
            # populated after a slow fetch can turn five-minute refresh into ten.
            snapshot=chart_snapshot(symbols,day,connection.get('host','127.0.0.1'),port,int(connection.get('client_id',11)),
                refresh_slot=int(time.time()//CHART_REFRESH_SECONDS))
        st.caption(f"Source: {snapshot['source']} · Snapshot: {pd.Timestamp(snapshot['fetched_at']).tz_convert('America/New_York').strftime('%Y-%m-%d %H:%M:%S')} ET")
        columns=st.columns(2)
        for i,levels in enumerate(plan.levels):
            with columns[i%2]:
                with st.container(border=True):
                    st.subheader(levels.symbol)
                    frame=candle_frame(snapshot.get('candles',{}).get(levels.symbol,[]))
                    if frame.empty:
                        st.caption('No candle data · imported levels remain visible')
                    else:
                        end=frame.index[-1]+pd.Timedelta(minutes=5)
                        st.caption(f"Last completed close: ${frame.Close.iloc[-1]:,.2f} · {end.strftime('%H:%M')} ET")
                    fig=make_level_chart(levels,frame,day,plan.session_date)
                    st.plotly_chart(fig,width='stretch',key=f'stock_chart_{levels.symbol}',config=dict(scrollZoom=True,displaylogo=False))
                    if levels.symbol in snapshot.get('errors',{}):
                        st.info(snapshot['errors'][levels.symbol])
    chart_wall()
