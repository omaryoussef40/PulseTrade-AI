from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch, AsyncMock

import pandas as pd
import pytest

import bot_core as core
import engine
from modules.pmb_scan_preparation import (
    boundary, claim_entry, crossed_side, fingerprint, mandatory_confirmation,
    read_snapshot, valid_snapshot, validate_configuration, write_snapshot,
)
from modules.pmb_scan_worker import live_crossing, previous_levels, scan_completed_symbol, OptionPreparation
from test_backtester_live_candles import source_bars


def at(clock):
    return datetime.fromisoformat("2026-10-27T" + clock + "-04:00")


def config():
    return {"automation": {"enabled":True, "scan_preparation":{"enabled":True,
            "lead_seconds":180, "confirmation_window_seconds":90}},
            "strategy":{"active_strategy":"pmb", "breakout_follow_through":{
                "confirmation_mode":"orb_5", "follow_through_enabled":True}},
            "risk":{"max_trades_per_day":3}, "watchlist":["RKLB"],
            "account_mode":"Simulation", "ib":{}}


def confirmed(side="CALL"):
    return {"Symbol":"RKLB", "Signal":side, "Price":56.44 if side=="CALL" else 52.,
            "PDH":54.99, "PDL":52.15, "PDH Break":side=="CALL", "PDL Break":side=="PUT",
            "Confirmation Candle Minutes":5, "Confirmation Closed At":"2026-10-27 10:00 EDT"}


def snapshot(cfg=None):
    cfg=cfg or config()
    return {"fingerprint":fingerprint(cfg,["RKLB"]), "ready":True,
            "generation":"revision-1", "published_at":at("10:00:03").isoformat(),
            "candle_close":at("10:00:00").isoformat(), "results":{"RKLB":confirmed()},
            "options":{}}


@pytest.mark.parametrize("price,expected", [(55.,"CALL"),(52.,"PUT"),(54.,None),(54.99,None),(52.15,None),(float("nan"),None),(None,None)])
def test_shortlist_requires_an_actual_crossing(price, expected):
    assert crossed_side(price,54.99,52.15)==expected


@pytest.mark.parametrize("side",["CALL","PUT"])
def test_crossing_and_completed_five_minute_confirmation_are_both_mandatory(side):
    result=confirmed(side)
    assert mandatory_confirmation(result,at("10:00:00"))
    result["Confirmation Closed At"]="2026-10-27 09:55 EDT"
    assert not mandatory_confirmation(result,at("10:00:00"))
    result["Confirmation Closed At"]="2026-10-27 10:05 EDT"
    assert not mandatory_confirmation(result,at("10:00:00"))
    result["Confirmation Closed At"]="2026-10-27 10:00 EDT"
    result["Price"]=54.
    assert not mandatory_confirmation(result,at("10:00:00"))


@pytest.mark.parametrize("field,value",[("Confirmation Candle Minutes",15),("PDH Break",False),("Signal","WAIT")])
def test_confirmation_cannot_be_bypassed(field,value):
    result=confirmed();result[field]=value
    assert not mandatory_confirmation(result,at("10:00:00"))


def test_snapshot_rejects_stale_future_old_boundary_or_changed_settings():
    cfg=config();payload=snapshot(cfg)
    assert valid_snapshot(payload,cfg,["RKLB"],at("10:00:05"))
    assert not valid_snapshot(payload,cfg,["RKLB"],at("09:59:59"))
    assert not valid_snapshot(payload,cfg,["RKLB"],at("10:00:30"))
    assert not valid_snapshot(payload,cfg,["RKLB"],at("10:05:05"))
    changed=deepcopy(cfg);changed["risk"]["max_trades_per_day"]=4
    assert not valid_snapshot(payload,changed,["RKLB"],at("10:00:05"))
    assert not valid_snapshot(payload,cfg,["SOFI"],at("10:00:05"))
    payload["ready"]=False
    assert not valid_snapshot(payload,cfg,["RKLB"],at("10:00:05"))


def test_preparation_has_a_bounded_entry_window():
    payload=snapshot();payload["published_at"]=at("10:01:40").isoformat()
    assert not valid_snapshot(payload,config(),["RKLB"],at("10:01:41"))


def test_configuration_cannot_disable_confirmation_under_prepared_mode():
    cfg=config();validate_configuration(cfg)
    cfg["strategy"]["breakout_follow_through"]["follow_through_enabled"]=False
    with pytest.raises(ValueError,match="five-minute"):
        validate_configuration(cfg)


def test_entry_claim_survives_retries_and_restarts(tmp_path):
    path=tmp_path/"claims.json"
    assert claim_entry("10:00","RKLB","CALL",path)
    assert not claim_entry("10:00","RKLB","CALL",path)
    assert claim_entry("10:00","SOFI","CALL",path)
    assert claim_entry("10:05","RKLB","CALL",path)


def test_corrupt_claim_file_fails_closed(tmp_path):
    path=tmp_path/"claims.json";path.write_text("broken")
    with pytest.raises(ValueError):
        claim_entry("10:00","RKLB","CALL",path)


def test_snapshot_publication_is_atomic_and_json_safe(tmp_path):
    path=tmp_path/"snapshot.json";payload=snapshot()
    payload["nan"]=float("nan")
    write_snapshot(payload,path)
    assert not path.with_suffix(".json.tmp").exists()
    with patch("modules.pmb_scan_preparation.PREPARATION_FILE",path):
        assert read_snapshot(config(),["RKLB"],at("10:00:05"))["nan"] is None


def test_live_shortlist_rejects_delayed_or_stale_quotes():
    now=at("09:57:00")
    ticker=NS(time=now,marketDataType=1,last=56.44)
    assert live_crossing(ticker,54.99,52.15,now)[0]=="CALL"
    ticker.last=54.
    assert live_crossing(ticker,54.99,52.15,now)[0] is None
    ticker.last=56.44;ticker.marketDataType=3
    assert live_crossing(ticker,54.99,52.15,now)[0] is None
    ticker.marketDataType=1;ticker.time=now-timedelta(seconds=16)
    assert live_crossing(ticker,54.99,52.15,now)[0] is None


def test_previous_levels_exclude_todays_incomplete_daily_candle():
    daily=pd.DataFrame({"High":[54.99,99.],"Low":[52.15,1.]},
                       index=pd.to_datetime(["2026-10-26","2026-10-27"]))
    assert previous_levels(daily,at("10:00:00"))==(54.99,52.15)


def test_final_scan_excludes_non_crossers_but_admits_late_crossings():
    frame=pd.DataFrame({"Close":[54.]},index=[at("09:55:00")])
    daily=pd.DataFrame({"High":[54.99],"Low":[52.15]},index=pd.to_datetime(["2026-10-26"]))
    with patch("bot_core.scan_symbol_ib",return_value=confirmed()) as scan:
        assert scan_completed_symbol(None,"RKLB",frame,daily,at("10:00:00"),{}) is None
        scan.assert_not_called()
        frame.iloc[0,0]=56.44
        result=scan_completed_symbol(None,"RKLB",frame,daily,at("10:00:00"),{})
        assert result["Signal"]=="CALL"
        assert scan.call_args.kwargs["as_of"]==at("10:00:00")


def test_missing_final_stock_candle_cannot_be_used():
    frame=pd.DataFrame({"Close":[56.]},index=[at("09:50:00")])
    with pytest.raises(ValueError,match="unavailable"):
        scan_completed_symbol(None,"RKLB",frame,pd.DataFrame(),at("10:00:00"),{})


def test_cached_stock_scan_does_no_network_fetch_and_excludes_unfinished_candle():
    raw=source_bars()
    close=pd.Timestamp("2026-09-10 09:50",tz=core.EASTERN)
    confirmation={"confirmation_mode":"orb_5","follow_through_enabled":True,
                  "volume_enabled":False,"require_extension":True}
    with patch("bot_core.fetch_ib_intraday") as intraday, patch("bot_core.fetch_ib_daily") as daily:
        result=core.scan_symbol_ib(None,"TEST",min_score=70,min_session_bars=2,
            intraday_bar_size="5 mins",analysis_bar_minutes=15,
            breakout_follow_through=confirmation,prepared_data=(raw,pd.DataFrame()),as_of=close)
    intraday.assert_not_called();daily.assert_not_called()
    assert result["Signal"]=="CALL"
    assert result["Confirmation Candle Minutes"]==5
    assert "09:50" in result["Confirmation Closed At"]
    assert result["Intraday Data"].index.max()+timedelta(minutes=5)<=close


def option():
    return {"Type":"CALL","Expiry":(datetime.now(core.EASTERN)+timedelta(days=7)).strftime("%Y%m%d"),
            "Strike":56.,"Trading Class":"RKLB"}


def quote(**updates):
    result={"Bid":3.5,"Ask":3.6,"Mid":3.55,"Spread %":.1/3.55,
            "Delta":.55,"Gamma":.05,"Theta":-.1,"Vega":.04,
            "Volume":100,"Market Data Type":1,"Quote Time":datetime.now(core.EASTERN)}
    result.update(updates);return result


def test_prepared_option_reuses_discovery_but_refreshes_quotes_and_filters():
    ib=Mock();ib.qualifyContracts.side_effect=lambda c:[c]
    with patch("bot_core._option_chain_candidates") as discovery, patch("bot_core.get_snapshot_mid",return_value=quote()) as quotes:
        result=core.recommend_option_ib(ib,"RKLB","CALL",56.44,prepared_option=option())
    discovery.assert_not_called();quotes.assert_called_once()
    assert result["Mid"]==3.55


@pytest.mark.parametrize("market",[quote(**{"Market Data Type":3}),quote(Delta=.1),
    quote(Bid=1.,Ask=4.,**{"Spread %":1.2}),quote(**{"Quote Time":None})])
def test_prepared_option_cannot_bypass_live_quote_or_quality_checks(market):
    ib=Mock();ib.qualifyContracts.side_effect=lambda c:[c]
    with patch("bot_core.get_snapshot_mid",return_value=market), patch("bot_core._option_chain_candidates") as discovery:
        assert core.recommend_option_ib(ib,"RKLB","CALL",56.44,prepared_option=option()) is None
    discovery.assert_not_called()


def test_invalid_preparation_never_connects_or_falls_back_to_full_scan():
    with patch("engine.load_config",return_value=config()), patch("engine.write_health"), patch("engine.read_prepared_scan",return_value=None), patch("engine.connect_ib") as connect, patch("engine.scan_symbol_ib") as scan:
        engine.run_cycle()
    connect.assert_not_called();scan.assert_not_called()


def test_read_only_option_preparation_uses_separate_clients(tmp_path):
    cfg=config();cfg["ib"]["client_id"]=121
    preparer=OptionPreparation(cfg)
    process=Mock();process.poll.return_value=None
    stdin=process.stdin
    with patch("modules.pmb_scan_worker.subprocess.Popen",return_value=process):
        preparer.prepare("RKLB","CALL",56.44,"10:00",at("09:57:00"))
    import json
    payload=json.loads(stdin.write.call_args.args[0])
    assert payload["ib"]["readonly"] is True
    assert payload["ib"]["client_id"]!=121
    assert payload["prepare_only"] is True
    assert payload["filters"]["max_market_data_checks"]==6


def test_stream_subscription_is_persistent_and_daily_levels_are_fetched_once():
    import asyncio
    from market_data import IBKRMarketDataProvider
    ib=Mock()
    stock=NS(conId=123,secType="STK")
    ib.qualifyContractsAsync=AsyncMock(return_value=[stock])
    daily=[NS(close=54.)];stream=[NS(close=56.)]
    ib.reqHistoricalDataAsync=AsyncMock(side_effect=[daily,stream])
    provider=IBKRMarketDataProvider(ib=ib)
    frame=pd.DataFrame({"High":[55.],"Low":[52.]},index=pd.to_datetime(["2026-10-26"]))
    with patch.object(provider,"_bars_to_ohlcv",return_value=frame):
        actual=asyncio.run(provider.subscribe_prepared_stock("RKLB"))
    assert actual[0] is stream
    calls=ib.reqHistoricalDataAsync.call_args_list
    assert calls[0].kwargs["barSizeSetting"]=="1 day"
    assert calls[0].kwargs["keepUpToDate"] is False
    assert calls[1].kwargs["barSizeSetting"]=="5 mins"
    assert calls[1].kwargs["keepUpToDate"] is True
    assert ib.reqMktData.call_args.kwargs["snapshot"] is False
    ib.placeOrder.assert_not_called()


def test_worker_lifecycle_keeps_one_preparer_and_restarts_on_settings_change():
    cfg=config()
    process=Mock();process.poll.return_value=None
    with patch.object(engine,"_PREPARATION_WORKER",None), patch.object(engine,"_PREPARATION_SIGNATURE",None), patch("engine.subprocess.Popen",return_value=process) as spawn:
        engine.ensure_preparation_worker(cfg)
        engine.ensure_preparation_worker(cfg)
        assert spawn.call_count==1
        assert "modules.pmb_scan_worker" in spawn.call_args.args[0]
        cfg["risk"]["max_trades_per_day"]=4
        engine.ensure_preparation_worker(cfg)
        assert spawn.call_count==2
        process.terminate.assert_called_once()
        cfg["automation"]["enabled"]=False
        engine.ensure_preparation_worker(cfg)
        assert engine._PREPARATION_WORKER is None


def test_turning_preparation_off_cannot_consume_an_old_prepared_signal():
    cfg=config();payload=snapshot(cfg)
    cfg["automation"]["scan_preparation"]["enabled"]=False
    with patch("engine.load_config",return_value=cfg), patch("engine.connect_ib") as connect:
        engine.run_cycle(prepared_scan=payload)
    connect.assert_not_called()


@pytest.mark.parametrize("blocked",[None,"confirmation","filters","already_traded","daily_limit","changed_at_submission"])
def test_prepared_engine_path_keeps_entry_gates_and_never_fetches_full_watchlist(blocked):
    from contextlib import ExitStack
    cfg=core.default_config()
    cfg.update(config())
    cfg["risk"].update(account_size=3000,max_spend_per_trade=1950,max_daily_capital=2850,
        max_spend_per_trade_pct=0,max_daily_capital_pct=0,max_contracts=5,
        entry_cutoff_hour=15,entry_cutoff_minute=0)
    cfg["automation"].update(approval_mode="Automatic",require_trade_approval=False)
    cfg["telegram"]={}
    payload=snapshot(cfg)
    row=payload["results"]["RKLB"]
    row.update(Score=99.,Grade="A+",Confidence=100.,RVOL=2.,**{"ATR %":1.,"ORB Confirmation Time":"2026-10-27 10:00 EDT"})
    payload["options"]["RKLB"]=option()
    if blocked=="confirmation":
        row["PDH Break"]=False
    latest=deepcopy(payload)
    if blocked=="changed_at_submission":
        latest["results"]["RKLB"]["PDH Break"]=False
    ib=Mock()
    contract=NS(conId=935120198)
    selected={**option(),"Contract":contract,"Option":"RKLB 20261106 56 CALL", "Mid":3.55,"Option Score":100}
    trade=NS(orderStatus=NS(status="Filled",filled=2,remaining=0,avgFillPrice=3.55),fills=[])
    clock=NS(now=lambda *_:at("10:00:05"),fromisoformat=datetime.fromisoformat)
    returns={"load_config":cfg,"connect_ib":ib,"is_market_open_now":True,
        "orders_unlocked_from_config":True,"get_today_loss_stats":(0,0.),
        "get_today_trade_stats":(3 if blocked=="daily_limit" else 0,0.),
        "read_active_positions":[],"broker_open_option_symbols":set(),
        "get_today_traded_symbols":{"RKLB"} if blocked=="already_traded" else set(),
        "sync_today_executions_to_trade_log":(0,""),"sync_active_positions_from_broker":[],
        "manage_open_positions":[],"refresh_opening_orb_trade_state":{},
        "opening_orb_trade_pauses_normal_entries":False,"is_top_candidate":blocked!="filters",
        "recommend_option_ib":selected,"place_option_order":trade,
        "read_prepared_scan":latest,"claim_prepared_entry":True,
        "should_auto_build":False,"expire_open_scanner_approvals":0}
    with ExitStack() as stack:
        mocks={name:stack.enter_context(patch.object(engine,name,return_value=value)) for name,value in returns.items()}
        for name in ["app_log","write_health","write_current_scan_candidates","log_engine_decision",
                     "log_trade","save_trade_replay","add_active_position_from_entry"]:
            stack.enter_context(patch.object(engine,name))
        scan=stack.enter_context(patch.object(engine,"scan_symbol_ib"))
        stack.enter_context(patch.object(engine,"datetime",clock))
        engine.run_cycle(prepared_scan=payload)
    scan.assert_not_called()
    assert mocks["place_option_order"].call_count==(0 if blocked else 1)
    if blocked is None:
        assert mocks["recommend_option_ib"].call_args.kwargs["prepared_option"]==payload["options"]["RKLB"]
