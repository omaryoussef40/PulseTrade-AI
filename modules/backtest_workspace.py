"""Copy and compare live PMB settings without changing live configuration."""
from copy import deepcopy

MAPPING={
 'sl_orb':('strategy','orb_minutes',15),'sl_first_signal':('strategy','first_signal_minutes',20),
 'sl_min_bars':('strategy','min_session_bars',7),'sl_require_retest':('strategy','require_break_retest',False),
 'sl_retest_tolerance':('strategy','retest_tolerance_pct',.1),'sl_retest_max_minutes':('strategy','retest_max_minutes',45),
 'sl_max_trades':('risk','max_trades_per_day',2),'sl_max_contracts':('risk','max_contracts',5),
 'sl_stop':('risk','stop_loss_pct',20.),'sl_tp':('risk','take_profit_pct',30.),
 'sl_breakeven':('risk','breakeven_trigger_pct',15.),'sl_trailing_trigger':('risk','trailing_trigger_pct',25.),
 'sl_trailing_stop':('risk','trailing_stop_pct',10.),'sl_entry_cutoff_hour':('risk','entry_cutoff_hour',14),
 'sl_entry_cutoff_minute':('risk','entry_cutoff_minute',30),'sl_force_exit_enabled':('risk','force_exit_enabled',True),
 'sl_force_exit_hour':('risk','force_exit_hour',15),'sl_force_exit_minute':('risk','force_exit_minute',55),
 'sl_max_consecutive_losses':('risk','max_consecutive_losses',2),'sl_max_daily_drawdown':('risk','max_daily_drawdown_pct',5.),
 'sl_reserve_capital':('risk','reserve_capital_for_remaining_trades',True),'sl_recycle_capital':('risk','recycle_capital_after_exit',False),
 'sl_same_symbol':('risk','allow_same_symbol_same_day',False),
}


def live_widget_values(config):
    from .strategy_controls import effective_config
    cfg=effective_config(config)
    values={key:deepcopy(cfg.get(section,{}).get(field,default)) for key,(section,field,default) in MAPPING.items()}
    follow=cfg.get('strategy',{}).get('breakout_follow_through',{})
    values.update(sl_strategy_pmb=True,sl_strategy_brt=False,sl_strategy_pullback=False,sl_strategy_gap=False,
        sl_capital=float(cfg.get('risk',{}).get('account_size',1000)),sl_staged_timeline=bool(cfg.get('staged_trading_timeline',{}).get('enabled',False)),
        sl_breakout_volume_enabled=bool(follow.get('volume_enabled',follow.get('enabled',False))),
        sl_next_candle_confirmation_enabled=bool(follow.get('follow_through_enabled',follow.get('enabled',False))),
        sl_confirmation_minutes=int(follow.get('confirmation_minutes',15)),sl_breakout_volume_ratio=float(follow.get('min_breakout_volume_ratio',1.5)),
        sl_follow_through_require_extension=bool(follow.get('require_extension',False)))
    risk=cfg.get('risk',{})
    pct=float(risk.get('max_spend_per_trade_pct',0))
    values['sl_sizing_model']='% of Equity' if pct else 'Fixed Dollar'
    if pct:
        values.update(sl_alloc_pct=pct,sl_daily_exp_pct=float(risk.get('max_daily_capital_pct',80)))
    else:
        values.update(sl_spend=float(risk.get('max_spend_per_trade',200)),sl_daily_cap=float(risk.get('max_daily_capital',500)))
    dte=int(cfg.get('strategy',{}).get('option_dte',7))
    if dte in (7,14):values['sl_option_dtes']=[dte]
    return values


def comparison_rows(config,settings):
    from .strategy_controls import effective_config
    cfg=effective_config(config)
    rows=[]
    for label,live,replay in [
        ('ORB minutes',cfg['strategy'].get('orb_minutes',15),settings.orb_minutes),
        ('Minimum session bars',cfg['strategy'].get('min_session_bars',7),settings.min_session_bars),
        ('Break and retest',cfg['strategy'].get('require_break_retest',False),settings.require_break_retest),
        ('Confirmation minutes',cfg['strategy'].get('breakout_follow_through',{}).get('confirmation_minutes',15),(settings.breakout_follow_through or {}).get('confirmation_minutes',15)),
        ('Trades per day',cfg['risk'].get('max_trades_per_day',2),settings.max_trades_per_day),
        ('Contract cap',cfg['risk'].get('max_contracts',5),settings.max_contracts),
        ('Stop loss %',cfg['risk'].get('stop_loss_pct',20),settings.stop_loss_pct),
        ('Take profit %',cfg['risk'].get('take_profit_pct',30),settings.take_profit_pct),
        ('PMB capital cap %',cfg['risk'].get('max_daily_capital_pct',0),settings.max_daily_exposure_pct),
    ]:
        rows.append({'Setting':label,'Live':str(live),'Backtest':str(replay),'Status':'Matches' if live==replay else 'Different'})
    return rows
