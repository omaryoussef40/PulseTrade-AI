"""Paste, review and configure daily Pine stock levels."""
from dataclasses import asdict
import json
import pandas as pd
import streamlit as st
from .pine_levels import ROOT, parse_pine, load_plan, save_plan
from .pine_stock_signals import Settings
from .pine_stock_engine import STATUS_FILE, LEDGER_FILE, broker_orders_allowed


def render_editor(app, section="legacy"):
    from bot_core import load_config, save_config
    show_plan=section in ('legacy','Daily Plan')
    show_settings=section in ('legacy','Setup')
    enabled=bool(app.get('pine_stocks',{}).get('enabled',False))
    st.subheader('Daily Plan' if show_plan else 'Stock strategy setup')
    if app.get('strategy_control',{}).get('enabled') and app.get('strategy_control',{}).get('active')=='dual':
        allocation=app['strategy_control'].get('allocation',{})
        st.info(f"Combined mode: PMB options {allocation.get('pmb_pct',80):g}% · Daily stocks {allocation.get('stocks_pct',20):g}% of equity × {allocation.get('stock_margin',2):g} margin.")
    st.caption('Paste your daily Pine code. Five-minute confirmed signals use its entries, stops, targets and VWAP. Choose stock shares or 7-DTE calls/puts in Setup.')
    try:
        saved = load_plan()
    except ValueError as exc:
        saved = None
        st.error(str(exc))
    settings = Settings(**app.get('pine_stocks', {}).get('settings', {}))
    example = ROOT / 'examples' / 'daily_trade_levels_2026-10-08.pine'
    initial = saved.source if saved else example.read_text() if example.exists() else ''
    plan=saved
    if show_plan:
        upload = st.file_uploader('Or upload your daily .pine file', type=['pine', 'txt'])
        if upload is not None and st.session_state.get('pine_upload_id') != upload.file_id:
            st.session_state['daily_pine_source'] = upload.getvalue().decode('utf-8')
            st.session_state['daily_pine_draft'] = st.session_state['daily_pine_source']
            st.session_state['pine_upload_id'] = upload.file_id
        if 'daily_pine_source' not in st.session_state:
            st.session_state['daily_pine_source'] = st.session_state.get('daily_pine_draft', initial)
        source = st.text_area('Paste daily Pine code here', key='daily_pine_source', height=260)
        # Widget state is removed when its tab is hidden. Keep the unsaved draft
        # separately so chart refreshes and switching tabs cannot discard it.
        st.session_state['daily_pine_draft'] = source
        plan = None
        try:
            plan = parse_pine(source)
        except ValueError as exc:
            st.error(str(exc))
        if plan:
            today = pd.Timestamp.now(tz='America/New_York').date()
            st.write(f'{len(plan.levels)} stocks · Trading date: {plan.session_date} · Confirmation: 5 minutes')
            if plan.signal_rules is not None:
                rules=plan.signal_rules
                volume=f'Volume ≥ {rules.volume_multiple:g}× the prior {rules.volume_lookback} candles' if rules.require_volume else 'Volume filter off'
                window=f'{rules.morning_start}–{rules.morning_end} New York candle starts' if rules.morning_only else 'Full regular session'
                st.caption(f'Imported rules: {volume} · {window} · Regular-session VWAP.')
                st.caption('Volume uses regular-session candles, including prior sessions. Match TradingView to regular trading hours when comparing signals. Your stock trading limits also apply.')
            if plan.session_date != today:
                st.warning(f'This plan is dated {plan.session_date}. You can review and save it anytime; it cannot open trades on {today}. Paste a plan dated for the session you intend to trade.')
            st.dataframe(pd.DataFrame([asdict(r) for r in plan.levels]), hide_index=True, use_container_width=True)
    values = asdict(settings)
    if show_settings:
        with st.expander('Pine trading settings', expanded=True):
            values['instrument']=st.selectbox('Trade instrument',['OPT','STK'],index=0 if settings.instrument=='OPT' else 1,format_func=lambda value:'7-DTE calls / puts' if value=='OPT' else 'Stock shares',key='pine_setup_instrument')
            is_option=values['instrument']=='OPT'
            if section=='legacy':
                enabled = st.checkbox('Use daily stock levels in the trading engine', value=bool(app.get('pine_stocks', {}).get('enabled', False)),disabled=bool(app.get('strategy_control',{}).get('enabled')),help='Once Trading controls are saved, select the entry strategy there.')
            st.caption('Execution permissions are managed in Live Trading → Controls.')
            st.caption('Entries require a completed five-minute close crossing above the Pine buy level or below the Pine sell level, with the script’s VWAP and optional volume/time filters.')
            st.caption('The stock’s Pine stop, T1 and T2 prices trigger exits. Account allocation is managed in Strategies → Capital Allocation.')
            if is_option:
                st.caption('Buy calls above the buy level and puts below the sell level. Target 7 calendar days to expiry using the nearest listed expiry and at-the-money strike. Premium budgets determine contract quantity; stock margin and share stop-risk budgets do not apply.')
            if section=='legacy':
                choices = ['Simulation', 'IBKR Paper', 'IBKR Live']
                values['execution'] = st.selectbox('Execution mode', choices, index=choices.index(settings.execution))
                st.caption('Simulation records hypothetical Pine trades using IBKR quotes. IBKR Paper sends orders to your paper account.')
            left, right = st.columns(2)
            with left:
                fields=[('Maximum premium per option trade ($)','option_premium_budget'),('Maximum total open premium ($)','option_max_total_premium'),('Maximum new option premium per day ($)','option_max_daily_premium'),('Daily loss limit for new entries ($)','max_daily_loss')] if is_option else [('Stop risk budget for share sizing ($)', 'risk_dollars'), ('Maximum position value ($)', 'max_position_dollars'), ('Maximum total position value ($)', 'max_total_dollars'), ('Maximum new stock entry value per day ($)', 'max_daily_capital'), ('Daily loss limit for new entries ($)', 'max_daily_loss')]
                for label, name in fields:
                    values[name] = st.number_input(label, min_value=1., value=float(values[name]))
            with right:
                if is_option:
                    values['option_max_contracts']=st.number_input('Maximum contracts per trade',min_value=1,value=int(values['option_max_contracts']))
                    values['option_commission_per_contract']=st.number_input('Simulation commission per contract per side ($)',min_value=0.,value=float(values['option_commission_per_contract']))
                    values['option_max_spread_pct']=st.number_input('Maximum option spread (%)',min_value=.1,value=float(values['option_max_spread_pct']))
                else:
                    values['max_total_risk'] = st.number_input('Maximum combined stop risk ($)', min_value=1., value=float(values['max_total_risk']))
                values['max_positions'] = st.number_input('Maximum open Pine positions', min_value=1, value=int(values['max_positions']))
                values['max_daily_trades'] = st.number_input('Maximum Pine trades per day', min_value=1, value=int(values['max_daily_trades']))
                values['t1_fraction'] = st.number_input('Fraction of contracts to exit at T1' if is_option else 'Fraction of shares to exit at T1', min_value=.01, max_value=.99, value=float(values['t1_fraction']))
            values['bar_minutes'] = 5
            values['entry_start'] = st.text_input('New entries from (New York time)', value=values['entry_start'])
            values['entry_end'] = st.text_input('New entries until (New York time)', value=values['entry_end'])
            if is_option:
                values['option_allow_calls']=st.checkbox('Buy calls on Pine buy signals',value=values['option_allow_calls'])
                values['option_allow_puts']=st.checkbox('Buy puts on Pine sell signals',value=values['option_allow_puts'])
            else:
                values['enable_longs'] = st.checkbox('Allow long stock trades', value=values['enable_longs'])
                values['enable_shorts'] = st.checkbox('Allow short stock trades', value=values['enable_shorts'])
            values['kill_switch'] = st.checkbox('Block all new Pine entries', value=values['kill_switch'])
            if section=='Setup':
                values['trade_all_plan_symbols']=st.checkbox('Allow every ticker in the Pine plan',value=settings.trade_all_plan_symbols)
                symbols=sorted({row.symbol for row in plan.levels} | set(settings.allowed_symbols)) if plan else list(settings.allowed_symbols)
                values['allowed_symbols']=tuple(st.multiselect('Approved stock tickers',symbols,default=[symbol for symbol in settings.allowed_symbols if symbol in symbols],disabled=values['trade_all_plan_symbols']))
            with st.expander('Quote and simulation settings'):
                for label, name in [('Maximum quote age (seconds)', 'quote_max_age_seconds'), ('Maximum completed candle age (seconds)', 'candle_max_age_seconds')]:
                    values[name] = st.number_input(label, min_value=1, value=int(values[name]))
                quote_fields=[('Maximum underlying stock spread (%)','max_spread_pct'),('Maximum entry slippage (%)','max_entry_slippage_pct')] if is_option else [('Maximum spread (%)', 'max_spread_pct'), ('Maximum entry slippage (%)', 'max_entry_slippage_pct'), ('Simulation fee per share ($)', 'simulation_fee_per_share'), ('Simulation minimum fee ($)', 'simulation_min_fee')]
                for label, name in quote_fields:
                    values[name] = st.number_input(label, min_value=0., value=float(values[name]), format='%.4f')
            if section=='legacy':
                if values['execution'] == 'IBKR Live':
                    phrase='TRADE PINE OPTIONS LIVE' if is_option else 'TRADE STOCK LEVELS LIVE'
                    values['live_confirmation'] = st.text_input(f'Separate Pine risk approval: type {phrase}', value=values['live_confirmation'])
                else:
                    values['live_confirmation'] = ''
    try:
        candidate = Settings(**values)
    except ValueError as exc:
        st.error(str(exc))
        return
    if candidate.execution != 'Simulation' and not broker_orders_allowed(app, candidate):
        st.info('Broker orders are locked. The account mode must match, and automation, order placement and risk approval controls must be enabled. Live Pine options require TRADE PINE OPTIONS LIVE in Live Trading → Controls.')
    if show_plan:st.caption('Unsaved plan' if plan and (not saved or plan.source_hash!=saved.source_hash) else 'Saved plan' if plan else 'Plan needs review')
    changed=values!=asdict(settings)
    if show_settings:st.caption('Unsaved changes' if changed else 'Saved settings')
    label={'legacy':'Save daily stock levels and settings','Daily Plan':'Save daily stock plan','Setup':'Save stock strategy settings'}[section]
    if st.button(label, type='primary', disabled=show_plan and plan is None):
        fresh = load_config()
        # Keep open trades in their original execution book.
        old_mode = fresh.get('pine_stocks', {}).get('settings', {}).get('execution', 'Simulation')
        open_rows = []
        if LEDGER_FILE.exists():
            from .pine_stock_engine import Ledger
            ledger = Ledger()
            try:
                open_rows = ledger.db.execute("SELECT key FROM trades WHERE status!='CLOSED'").fetchall()
            finally:
                ledger.close()
        if open_rows and candidate.execution != old_mode:
            st.error('Close or reconcile existing stock trades before changing execution mode.')
        else:
            if show_plan:save_plan(plan)
            if fresh.get('strategy_control',{}).get('enabled'):
                enabled=bool(fresh.get('pine_stocks',{}).get('enabled'))
            if show_settings:
                from .workspace_ui import merge_settings_changes
                fresh_settings=asdict(Settings(**fresh.get('pine_stocks',{}).get('settings',{})))
                fresh['pine_stocks'] = dict(enabled=enabled, settings=merge_settings_changes(asdict(settings),asdict(candidate),fresh_settings))
                save_config(fresh)
            st.success('Saved. The engine will use this dated plan when daily stock levels and automation are enabled.')
    if section=='legacy':
        st.subheader('Stock engine status')
        if STATUS_FILE.exists():
            try:
                status = json.loads(STATUS_FILE.read_text())
                st.caption(f"Last check: {status['timestamp']} · {status['mode']}")
                st.write(status['message'])
                if status.get('decisions'):
                    st.dataframe(pd.DataFrame(status['decisions']), hide_index=True, use_container_width=True)
                rows = status.get('positions', [])
                if rows:
                    st.dataframe(pd.DataFrame([{k:r.get(k) for k in ('day','symbol','side','status','quantity','entry_price','realized','fees')} for r in rows]), hide_index=True, use_container_width=True)
            except (OSError, ValueError, KeyError):
                st.info('Stock status is being updated. Refresh shortly.')
        else:
            st.info('No stock checks recorded yet. Keep TWS or IB Gateway open for market data, and start the trading engine.')
        st.caption('Stops and T1/T2 come from your Pine code. Positions stay open until an exit fills; there is no automatic end-of-day close. Saving a new plan does not change existing trade exits.')


def render(app):
    st.header('Daily Stock Scanner')
    view = st.radio('Workspace', ['Charts', 'Paste & settings', 'Trading controls', 'Daily Brief'], horizontal=True, key='daily_stock_tab')
    if view == 'Trading controls':
        from .strategy_controls_ui import render as render_controls
        render_controls(app)
        return
    if view == 'Daily Brief':
        from .daily_email_brief_ui import render as render_email_brief
        render_email_brief(app)
        return
    if view == 'Paste & settings':
        render_editor(app)
        return
    render_charts_workspace(app)


def render_charts_workspace(app):
    source = st.session_state.get('daily_pine_draft', st.session_state.get('daily_pine_source'))
    if source is None:
        try:
            saved = load_plan()
        except ValueError as exc:
            st.error(str(exc))
            return
        example = ROOT / 'examples' / 'daily_trade_levels_2026-10-08.pine'
        source = saved.source if saved else example.read_text() if example.exists() else ''
    try:
        plan = parse_pine(source)
    except ValueError as exc:
        st.info(f'Open Daily Plan to import a valid daily plan. {exc}')
        return
    saved = None
    try:
        saved = load_plan()
    except ValueError:
        pass
    if saved is None or saved.source_hash != plan.source_hash:
        st.info('Previewing the Pine code in your paste box. Save it under Daily Plan for the trading engine to use it.')
    st.write(f'{len(plan.levels)} tickers · Levels dated {plan.session_date} · Five-minute confirmation')
    from .pine_stock_charts import render as render_charts
    render_charts(plan, app)



def render_workspace(app):
    view=st.radio('Daily Stocks workspace',['Scanner','Charts','Setup','Daily Plan'],horizontal=True,key='stock_workspace')
    if view=='Charts':
        render_charts_workspace(app)
    elif view=='Setup':
        render_editor(app,section='Setup')
    elif view=='Daily Plan':
        source=st.radio('Plan source',['Paste Pine','Email brief'],horizontal=True,key='stock_plan_source')
        if source=='Paste Pine':render_editor(app,section='Daily Plan')
        else:
            from .daily_email_brief_ui import render as render_email
            render_email(app)
    else:
        from .workspace_ui import read_json
        st.subheader('Stock scanner')
        status=read_json(STATUS_FILE)
        if status:
            st.caption(f"Last check: {status.get('timestamp','Unavailable')} · {status.get('mode','Unavailable')}")
            st.write(status.get('message','Waiting for a scan.'))
            decisions=status.get('decisions',[])
            if decisions:st.dataframe(pd.DataFrame(decisions),hide_index=True,width='stretch')
            else:st.info('No stock decisions in the latest scan.')
        else:st.info('No stock scan recorded yet. Save a daily plan and review Live Trading → Controls.')


def render_execution_settings(app):
    from bot_core import load_config,save_config
    st.subheader('Stock execution permissions')
    settings=Settings(**app.get('pine_stocks',{}).get('settings',{}))
    modes=['Simulation','IBKR Paper','IBKR Live']
    execution=st.selectbox('Stock execution mode',modes,index=modes.index(settings.execution),key='workspace_stock_execution')
    confirmation=settings.live_confirmation
    if execution=='IBKR Live':
        phrase='TRADE PINE OPTIONS LIVE' if settings.instrument=='OPT' else 'TRADE STOCK LEVELS LIVE'
        confirmation=st.text_input(f'Separate Pine risk approval: type {phrase}',value=confirmation,key='workspace_stock_confirmation')
    else:confirmation=''
    st.caption('PMB uses the global account mode above. Stock orders require matching account mode and their existing separate approvals.')
    changed=execution!=settings.execution or confirmation!=settings.live_confirmation
    st.caption('Unsaved changes' if changed else 'Saved execution permissions')
    if st.button('Save stock execution permissions',key='save_stock_execution'):
        from .workspace_ui import stock_positions
        if execution!=settings.execution:
            try:holdings=stock_positions(app,all_books=True)
            except ValueError as exc:
                st.error(str(exc))
                return
            if holdings:
                st.error('Close or reconcile existing stock positions before changing execution mode.')
                return
        fresh=load_config()
        values=asdict(Settings(**fresh.get('pine_stocks',{}).get('settings',{})))
        values.update(execution=execution,live_confirmation=confirmation)
        fresh.setdefault('pine_stocks',{})['settings']=values
        save_config(fresh)
        st.success('Stock execution permissions saved.')
