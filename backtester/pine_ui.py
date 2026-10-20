"""Daily Pine stock backtests, separate from live plans and option results."""
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import subprocess
import sys

import pandas as pd
import streamlit as st

from modules.pine_levels import ROOT, parse_pine
from modules.pine_stock_signals import Settings, bounds
from .pine import EXPORT_DIR,OPTION_EXPORT_DIR,available_plans, load_result, select_plans
from .pine_store import load_preferences,restore_widgets,save_preferences,archive_upload,uploaded_plans


def execute_backtest(payload):
    with TemporaryDirectory(prefix='pine-backtest-') as folder:
        request = Path(folder)/'request.json'
        request.write_text(json.dumps(payload))
        worker = subprocess.run([sys.executable,'-m','backtester.pine_worker',str(request)],
            cwd=str(ROOT),capture_output=True,text=True,timeout=600)
    try:
        response = json.loads(worker.stdout.strip().splitlines()[-1])
    except (ValueError,IndexError):
        raise ValueError('Pine backtest could not finish. Check the IBKR connection and historical data permissions.') from None
    if worker.returncode or not response.get('ok'):
        raise ValueError(response.get('error','Pine backtest failed.'))
    return load_result(OPTION_EXPORT_DIR if response['meta'].get('instrument')=='OPT' else EXPORT_DIR)


def render_results(result):
    if not result:
        return
    meta, metrics = result['meta'], result['metrics']
    st.subheader('Daily Pine backtest results')
    st.caption(f"{meta['strategy']} · {meta['start']} to {meta['end']} New York · {len(meta['plan_dates'])} dated plans · Five-minute confirmation")
    if meta.get('instrument')=='OPT' and (meta.get('option_fill_model_version') or 0)<3:
        st.warning('This saved option backtest used an older fill model that could use stale, zero-volume candles or assume fills beyond recorded trading volume. Its option profit/loss is unverified. Rerun the backtest with the updated model.')
    if meta.get('status') != 'COMPLETE':
        st.warning('Historical data is incomplete. Review the data issues below before interpreting these results.')
    cols = st.columns(4)
    cols[0].metric('Realized net P/L',f"${metrics['net_pnl']:,.2f}")
    cols[1].metric('Unrealized P/L',f"${metrics['unrealized_pnl']:,.2f}")
    cols[2].metric('Ending equity',f"${metrics['ending_equity']:,.2f}")
    cols[3].metric('Closed-trade win rate',f"{metrics['win_rate']:g}%")
    st.caption(f"{metrics['total_trades']} entries · {metrics['closed_trades']} closed trades · {metrics['open_positions']} open positions · Commissions ${metrics['commissions']:,.2f} · Maximum drawdown ${metrics['max_drawdown']:,.2f}")
    curve = result['equity']
    if not curve.empty:
        st.line_chart(curve.set_index('timestamp')['equity'])
    trades = result['trades']
    if not trades.empty:
        st.write('Performance by underlying stock')
        summary = trades.groupby('symbol').agg(trades=('trade_id','count'),realized_pnl=('realized_pnl','sum'),unrealized_pnl=('unrealized_pnl','sum')).sort_values('realized_pnl',ascending=False)
        st.dataframe(summary,width='stretch')
    for label,key in [('Trades and open positions','trades'),('Entry and exit fills','fills'),('Signal decisions','decisions'),('Plan coverage','coverage'),('Data issues','errors')]:
        with st.expander(label,expanded=key=='errors' and not result[key].empty):
            if result[key].empty:
                st.caption('No records.')
            else:
                st.dataframe(result[key],hide_index=True,width='stretch')
                prefix='pine_option' if meta.get('instrument')=='OPT' else 'pine_stock'
                st.download_button(f'Download {label.lower()}',result[key].to_csv(index=False),file_name=f'{prefix}_{key}.csv',mime='text/csv',key=f'pine_bt_download_{key}')


def render(app):
    try:
        preferences=load_preferences()
        # Hidden pages lose Streamlit widget state even within one session.
        # Restore missing keys on every visit; active edits take precedence.
        restore_widgets(st.session_state,preferences)
        st.session_state['pine_bt_saved_settings']=preferences.get('settings',{})
    except (ValueError,OSError,TypeError) as exc:
        st.error(f'Could not restore Pine backtest settings: {exc}')
    st.header('Daily Pine Levels backtest')
    st.caption('Replays the existing stock strategy using each plan’s original trading date, entries, stops, T1 and T2.')
    instrument=st.radio('Trade instrument',['OPT','STK'],format_func=lambda value:'7-DTE calls / puts' if value=='OPT' else 'Stock shares',horizontal=True,key='pine_bt_instrument')
    result_dir=OPTION_EXPORT_DIR if instrument=='OPT' else EXPORT_DIR
    saved, errors = available_plans()
    library,library_errors=uploaded_plans()
    saved=[*saved,*library]
    errors.extend(library_errors)
    for error in errors:
        st.warning(f"{error['file']}: {error['error']}")
    uploads = st.file_uploader('Add historical daily Pine plans',type=['pine','txt'],accept_multiple_files=True,key='pine_bt_uploads')
    uploaded_hashes={plan.source_hash for plan in library}
    plans = {plan.source_hash:plan for plan in saved}
    for upload in uploads or []:
        try:
            plan = parse_pine(upload.getvalue().decode('utf-8-sig'))
            archive_upload(plan)
            uploaded_hashes.add(plan.source_hash)
            if plan.source_hash not in plans and 'pine_bt_plans' in st.session_state:
                st.session_state['pine_bt_plans']=[*[key for key in st.session_state['pine_bt_plans'] if key not in plans or plans[key].session_date!=plan.session_date],plan.source_hash]
            plans[plan.source_hash] = plan
        except (ValueError,UnicodeError,OSError) as exc:
            st.error(f'{upload.name}: {exc}')
    draft = st.session_state.get('daily_pine_draft')
    if draft:
        try:
            plan = parse_pine(draft)
            plans[plan.source_hash] = plan
        except ValueError:
            pass
    if uploaded_hashes:
        st.caption(f'{len(uploaded_hashes)} uploaded plans saved. Select them below; no re-upload is needed after refresh.')
    if not plans:
        try:
            save_preferences(st.session_state,st.session_state.get('pine_bt_saved_settings',{}))
        except OSError as exc:
            st.error(f'Could not save Pine backtest settings: {exc}')
        st.info('Save a Daily Pine plan under Strategies → Daily Stocks → Daily Plan, or upload historical plans here.')
        render_results(load_result(result_dir))
        return
    defaults = {plan.session_date:plan.source_hash for plan in plans.values()}
    if 'pine_bt_plans' not in st.session_state:
        st.session_state['pine_bt_plans']=list(defaults.values())
    else:
        st.session_state['pine_bt_plans']=[key for key in st.session_state['pine_bt_plans'] if key in plans]
    selected = st.multiselect('Daily plans to backtest',list(plans),
        format_func=lambda key:f'{plans[key].session_date} · {len(plans[key].levels)} stocks · {key[:8]}',key='pine_bt_plans')
    selected_plans = [plans[key] for key in selected if key in plans]
    dates = [plan.session_date for plan in selected_plans] or [plan.session_date for plan in plans.values()]
    selection = tuple(sorted(plan.source_hash for plan in selected_plans))
    if st.session_state.get('pine_bt_date_selection') != selection:
        # Explicit widget keys retain old dates across uploads and plan changes.
        # Reset only when the selected plans change, preserving manual ranges
        # (including later dates for managing open positions) on other reruns.
        st.session_state['pine_bt_start'] = min(dates)
        st.session_state['pine_bt_end'] = max(dates)
        st.session_state['pine_bt_date_selection'] = selection
    cols = st.columns(3)
    start = cols[0].date_input('Start date',key='pine_bt_start')
    end = cols[1].date_input('End date',key='pine_bt_end')
    capital = cols[2].number_input('Starting capital USD',min_value=1.,value=10000.,step=100.,key='pine_bt_capital')
    from modules.strategy_controls import effective_config
    base = Settings(**dict(effective_config(app).get('pine_stocks',{}).get('settings',{}),**st.session_state.get('pine_bt_saved_settings',{})))
    cfg = replace(base,execution='Simulation',instrument=instrument,bar_minutes=5,live_confirmation='')
    with st.expander('Stock risk and execution assumptions'):
        st.caption('Exits use the Pine stop, T1 and T2 prices. Dollar risk settings determine share quantity and entry limits. Defaults come from the stock strategy setup; changes here apply only to this backtest.')
        values = asdict(cfg)
        values['account_allocation_pct'] = st.number_input('Backtest account allocation %',min_value=0.,max_value=100.,value=float(cfg.account_allocation_pct),key='pine_bt_account_allocation_pct',help='Limits position sizing to this percentage of backtest equity. Set 0 to disable this percentage cap and use the dollar budgets. Applies only to this backtest and saves automatically.')
        labels = {'risk_dollars':'Stop risk budget for share sizing USD','max_position_dollars':'Maximum position value USD',
            'max_total_dollars':'Maximum total position value USD','max_daily_capital':'Daily entry budget USD',
            'max_daily_loss':'Daily loss limit for new entries USD','max_total_risk':'Maximum total stop risk USD',
            'max_entry_slippage_pct':'Adverse fill slippage %','simulation_fee_per_share':'Commission per share USD',
            'simulation_min_fee':'Minimum commission per order USD','t1_fraction':'Position fraction sold at T1'}
        if instrument=='OPT':
            labels={'option_premium_budget':'Maximum premium per option trade USD',
                'option_max_total_premium':'Maximum total open option premium USD','option_max_daily_premium':'Maximum new option premium per day USD',
                'max_daily_loss':'Daily loss limit for new entries USD','max_entry_slippage_pct':'Adverse option fill slippage %',
                'option_commission_per_contract':'Commission per contract per side USD','t1_fraction':'Fraction of contracts sold at T1'}
        for name,label in labels.items():
            values[name] = st.number_input(label,min_value=0.01 if name=='t1_fraction' else 0. if name in ('max_entry_slippage_pct','simulation_fee_per_share','simulation_min_fee','option_commission_per_contract') else 0.01,
                max_value=.99 if name=='t1_fraction' else 99. if name=='max_entry_slippage_pct' else None,
                value=float(values[name]),key=f'pine_bt_{name}')
        values['max_positions'] = st.number_input('Maximum open positions',min_value=1,value=int(cfg.max_positions),key='pine_bt_positions')
        values['max_daily_trades'] = st.number_input('Maximum daily entries',min_value=1,value=int(cfg.max_daily_trades),key='pine_bt_daily_trades')
        if instrument=='OPT':
            values['option_max_contracts']=st.number_input('Maximum contracts per trade',min_value=1,value=int(cfg.option_max_contracts),key='pine_bt_option_contracts')
            values['option_allow_calls']=st.checkbox('Buy calls on Pine buy signals',value=cfg.option_allow_calls,key='pine_bt_calls')
            values['option_allow_puts']=st.checkbox('Buy puts on Pine sell signals',value=cfg.option_allow_puts,key='pine_bt_puts')
            st.caption('Target: 7 calendar days to expiration, nearest listed expiry and at-the-money strike. The full premium is at risk; stock margin does not apply. Stop/T1/T2 triggers remain stock prices. A single contract exits fully at T1.')
        else:
            values['enable_longs'] = st.checkbox('Enable long trades',value=cfg.enable_longs,key='pine_bt_longs')
            values['enable_shorts'] = st.checkbox('Enable short trades',value=cfg.enable_shorts,key='pine_bt_shorts')
        st.caption(f'Entry window {cfg.entry_start}–{cfg.entry_end} New York · Initial ticker permissions and allocation defaults come from Setup and are retained with the backtest settings.')
        st.caption(f'Allocation cap: {values["account_allocation_pct"]:g}% of backtest equity' if values['account_allocation_pct'] else 'Percentage allocation cap disabled; dollar budgets still apply.')
        st.caption(f'Margin multiplier: {1 if instrument=="OPT" else cfg.margin_multiplier:g}× · Tickers: '+('all stocks in each plan' if cfg.trade_all_plan_symbols else ', '.join(cfg.allowed_symbols) or 'none approved'))
        if instrument=='STK' and values['enable_shorts']:
            st.caption('Shorts assume shares are borrowable; historical borrow fees are not included.')
        force = st.checkbox('Refresh historical candles from IBKR',key='pine_bt_refresh')
    try:
        persisted=asdict(Settings(**values))
        save_preferences(st.session_state,persisted)
        st.session_state['pine_bt_saved_settings']=persisted
        st.caption('Settings save automatically. Uploaded scripts remain available in the plan list after refresh.')
    except (ValueError,OSError) as exc:
        st.error(f'Could not save Pine backtest settings: {exc}')
    st.info('Each plan applies only to its original date. Sessions without a plan allow exits but no new entries. Saved plans are now archived for future backtests.')
    st.caption('Entries fill at the next five-minute candle’s open with adverse slippage. Stops take priority when a candle touches both stop and target. Open positions stay open at test end and are marked separately.' if instrument=='OPT' else 'Entries fill at the next five-minute candle’s open with adverse slippage. Stops take priority when a candle touches both stop and target. Targets fill at their set prices; opening gaps through stops use the opening price plus slippage. Open positions stay open at test end and are marked separately.')
    if instrument=='OPT':
        st.caption('Option fills require enough recorded contract volume for the entire quantity, counted across entries and all exits in the same candle. Zero-volume carried-forward prices cannot establish a fill. This is a conservative data check, not a guarantee of execution. Intrabar exits use the traded candle’s low with adverse slippage; opening-trigger exits use the option open. Remaining contracts exit five minutes before expiration-session close.')
    if st.button('Run Daily Pine backtest',type='primary',key='pine_bt_run',disabled=not selected_plans):
        try:
            select_plans(selected_plans)
            if start>end:
                raise ValueError('End date must be on or after start date.')
            now = pd.Timestamp.now(tz='America/New_York')
            session = bounds(pd.Timestamp(end,tz='America/New_York'))
            if end>now.date() or (session and session[1]>now):
                raise ValueError('Choose an end date whose regular trading session has completed.')
            cfg = Settings(**values)
            from bot_core import ib_port_from_config
            connection = dict(app.get('ib',{}),port=ib_port_from_config(app))
            payload = dict(sources=[plan.source for plan in selected_plans if start<=plan.session_date<=end],start=str(start),end=str(end),
                starting_capital=capital,settings=asdict(cfg),connection={key:connection[key] for key in ('host','port','client_id') if key in connection},force_refresh=force)
            if not payload['sources']:
                raise ValueError('No selected plans fall inside the requested dates.')
            with st.spinner('Loading historical stock candles and replaying Daily Pine plans…'):
                st.session_state['pine_bt_result'] = execute_backtest(payload)
        except (ValueError,OSError,subprocess.TimeoutExpired) as exc:
            st.error(str(exc))
    current=st.session_state.get('pine_bt_result')
    render_results(current if current and current['meta'].get('instrument','STK')==instrument else load_result(result_dir))
