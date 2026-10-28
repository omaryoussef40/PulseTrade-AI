from datetime import datetime, time, timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import tempfile

import pandas as pd
import pytest

import bot_core as core
from modules.option_candle_exits import completed_option_closes, evaluate_close, CANDLE_CLOSE_MODE


def at(value):
    return datetime.fromisoformat("2026-10-27T" + value + "-04:00")


def position():
    return dict(symbol="RKLB", signal="CALL", option="RKLB 20261106 56 CALL",
                expiry="20261106", strike=56., con_id=935120198, quantity=5,
                entry_price=3.13, entry_time=at("10:52:07").isoformat(),
                management_mode=core.POSITION_MANAGEMENT_AUTOMATED,
                entry_status="Filled", current_stop_price=2.82,
                emergency_stop_price=2.82, highest_price=3.13,
                exit_evaluation_mode=CANDLE_CLOSE_MODE, trailing_active=False,
                breakeven_active=False, take_profit_price=3.76)


def evaluate(pos, close, timestamp="11:05:00"):
    return evaluate_close(pos, close, at(timestamp), breakeven_trigger_pct=10,
                          trailing_trigger_pct=15, trailing_stop_pct=5)


@pytest.fixture(autouse=True)
def isolated_trade_history():
    with patch("bot_core._trade_log_entry_lookup", return_value={}):
        yield


def test_breakeven_and_trailing_are_separate_stages():
    pos = position()
    assert evaluate(pos, 3.443) is None
    assert pos["breakeven_active"] and not pos["trailing_active"]
    assert pos["current_stop_price"] == 3.13
    assert evaluate(pos, 3.61, "11:10:00") is None
    assert pos["trailing_active"]
    assert pos["current_stop_price"] == pytest.approx(3.4295)
    assert pos["emergency_stop_price"] == 2.82


def test_trail_keeps_following_new_closing_peaks_and_never_falls():
    pos = position()
    evaluate(pos, 4.)
    evaluate(pos, 3.85, "11:10:00")
    assert pos["current_stop_price"] == 3.8
    assert evaluate(pos, 3.79, "11:15:00") == "Trailing stop hit at five-minute close"


def test_breakeven_exit_is_not_called_stop_loss():
    pos = position()
    evaluate(pos, 3.45)
    assert evaluate(pos, 3.12, "11:10:00") == "Breakeven stop hit at five-minute close"


def test_reads_only_completed_fresh_post_entry_bars_once_per_boundary():
    pos = position()
    ib = Mock()
    ib.reqHistoricalData.return_value = [
        NS(date=at("10:45:00"), close=3., volume=4),
        NS(date=at("10:50:00"), close=3.18, volume=20),
        NS(date=at("10:55:00"), close=3.30, volume=18),
        NS(date=at("11:00:00"), close=3.61, volume=12),
        NS(date=at("11:05:00"), close=2., volume=5),  # still forming
    ]
    closes = completed_option_closes(ib, NS(), pos, at("11:05:03"))
    assert [p for _, p in closes] == [3.18, 3.30, 3.61]
    assert closes[-1][0] == at("11:05:00")
    assert completed_option_closes(ib, NS(), pos, at("11:05:25")) == []
    ib.reqHistoricalData.assert_called_once()
    assert ib.reqHistoricalData.call_args.kwargs["whatToShow"] == "TRADES"


def test_zero_volume_carried_forward_price_cannot_trigger_exit():
    ib = Mock()
    ib.reqHistoricalData.return_value = [NS(date=at("11:00:00"), close=2., volume=0)]
    assert completed_option_closes(ib, NS(), position(), at("11:05:03")) == []


def test_historical_timeout_is_retried_without_consuming_boundary():
    ib = Mock()
    ib.reqHistoricalData.side_effect = [TimeoutError("timeout"), [NS(date=at("11:00:00"), close=3.61, volume=12)]]
    pos = position()
    with pytest.raises(TimeoutError):
        completed_option_closes(ib, NS(), pos, at("11:05:03"))
    assert completed_option_closes(ib, NS(), pos, at("11:05:25"))[-1][1] == 3.61


@patch("bot_core.submit_exit_order")
@patch("bot_core.completed_option_closes", return_value=[])
def test_mid_candle_dip_never_sends_a_sell(_closes, sell):
    pos = position()
    evaluate(pos, 3.61)
    pos["current_price"] = 3.35
    core.manage_candle_close_position(Mock(), pos, NS(), at("11:04:00"), account=None,
        stop_loss_pct=10, breakeven_trigger_pct=10, trailing_trigger_pct=15,
        trailing_stop_pct=5, force_exit_enabled=False, force_exit_time=time(15,45),
        require_eod_exit_approval=True, allow_live_orders=True)
    sell.assert_not_called()


@patch("bot_core.submit_exit_order")
@patch("bot_core.completed_option_closes")
def test_confirmed_close_sells_at_market_without_claiming_a_stop_fill(closes, sell):
    pos = position()
    evaluate(pos, 3.61)
    closes.return_value = [(at("11:10:00"), 3.35)]
    core.manage_candle_close_position(Mock(), pos, NS(), at("11:10:03"), account=None,
        stop_loss_pct=10, breakeven_trigger_pct=10, trailing_trigger_pct=15,
        trailing_stop_pct=5, force_exit_enabled=False, force_exit_time=time(15,45),
        require_eod_exit_approval=True, allow_live_orders=True)
    assert sell.call_args.args[2] is None
    assert "Trailing stop" in sell.call_args.args[3]
    assert sell.call_args.kwargs["use_market"] is True


@pytest.mark.parametrize("quantity", [5, 15])
def test_virtual_trail_never_reaches_broker_orders(quantity):
    pos = position()
    pos["quantity"] = quantity
    evaluate(pos, 4.)
    ib = Mock()
    ib.qualifyContracts.return_value = []
    ib.placeOrder.side_effect = lambda contract, order: NS(order=order)
    with patch("bot_core._open_sell_trades_for_position", return_value=[]):
        core.ensure_protective_orders(ib, pos, stop_loss_pct=10)
    assert ib.placeOrder.call_count == quantity // 5
    for call in ib.placeOrder.call_args_list:
        order = call.args[1]
        assert order.orderType == "STP"
        assert order.auxPrice == 2.82
    assert pos["current_stop_price"] == 3.8


def test_missing_broker_execution_waits_instead_of_sending_zero_profit():
    pos = position()
    ib = Mock()
    ib.positions.return_value = []
    with patch("bot_core.read_active_positions", return_value=[pos]), patch("bot_core.write_active_positions") as write, patch("bot_core._latest_logged_exit_for_position", return_value={}):
        events = core.reconcile_active_positions_with_broker(ib)
    assert events[0]["Action"] == "CLOSE_PENDING"
    assert "P/L $" not in events[0]
    assert write.call_args.args[0] == [pos]
    assert not core.send_position_closed_telegram_message(NS(bot_token="", chat_id=""), events[0])


def test_close_uses_net_broker_result_and_normalizes_float_contract_ids():
    pos = position()
    pos.update(exit_pending=True, pending_exit_reason="Trailing stop hit at five-minute close")
    ib = Mock()
    ib.positions.return_value = []
    with tempfile.TemporaryDirectory() as tmp:
        path = tmp + "/trades.csv"
        pd.DataFrame([dict(timestamp=at("11:04:00").isoformat(), event="EXIT", source="IBKR_EXECUTION",
                          con_id="935120198.0", option="RKLB  261106C00056000", exit_price=3.61,
                          realized_pnl=233.02, pnl_basis="broker_net", quantity=5)]).to_csv(path, index=False)
        with patch("bot_core.TRADE_LOG_FILE", path), patch("bot_core.read_active_positions", return_value=[pos]), patch("bot_core.write_active_positions") as write:
            events = core.reconcile_active_positions_with_broker(ib, log_closures=False)
    event = events[0]
    assert event["Current"] == 3.61
    assert event["P/L $"] == 233.02
    assert "Trailing" in event["Reason"]
    assert write.call_args.args[0] == []
    with patch("bot_core.send_telegram_message", return_value=True) as send:
        assert core.send_position_closed_telegram_message(NS(), event)
    assert "+$233.02" in send.call_args.args[1]
    assert "includes broker-reported fees" in send.call_args.args[1]


def test_partial_fee_data_is_not_final_profit():
    pos = position()
    ib = Mock()
    ib.positions.return_value = []
    with patch("bot_core.read_active_positions", return_value=[pos]), patch("bot_core.write_active_positions"), patch("bot_core._latest_logged_exit_for_position", return_value={"exit_price":3.61, "realized_pnl":240., "pnl_basis":"pending_broker"}):
        assert core.reconcile_active_positions_with_broker(ib)[0]["Action"] == "CLOSE_PENDING"


def test_pending_sell_does_not_submit_again_and_cancelled_sell_can_recover():
    pos = position()
    pos.update(exit_pending=True, pending_exit_order_ids=[123])
    ib = Mock()
    ib.trades.return_value = [NS(order=NS(orderId=123), orderStatus=NS(status="Submitted"))]
    assert core.exit_order_still_pending(ib, pos)
    ib.trades.return_value[0].orderStatus.status = "Cancelled"
    assert not core.exit_order_still_pending(ib, pos)
    assert not pos.get("exit_pending")


def test_unknown_profit_is_pending_not_zero():
    message = core.format_position_closed_telegram_message({"symbol":"RKLB", "exit_price":None})
    assert "Pending broker confirmation" in message
    assert "$0.00" not in message


def test_live_monitor_ignores_snapshot_dip_then_consumes_completed_candle():
    pos = position()
    pos.update(trailing_active=True, breakeven_active=True, highest_price=3.55,
               current_stop_price=3.37)
    ib = Mock()
    ib.qualifyContracts.return_value = [NS(conId=935120198)]
    ib.portfolio.return_value = []
    with patch("bot_core.read_active_positions", return_value=[pos]), patch("bot_core.write_active_positions"), patch("bot_core.reconcile_active_positions_with_broker", return_value=[]), patch("bot_core.ensure_protective_orders", return_value=[]), patch("bot_core.get_snapshot_mid", return_value={"Mid":3.35}), patch("bot_core.completed_option_closes") as closes, patch("bot_core.submit_exit_order") as sell, patch("bot_core.datetime") as clock:
        args = dict(ib=ib, account=None, stop_loss_pct=10, take_profit_pct=20,
                    breakeven_trigger_pct=10, trailing_trigger_pct=15,
                    trailing_stop_pct=5, force_exit_time=time(15,45),
                    allow_live_orders=True, force_exit_enabled=False,
                    exit_evaluation_mode=CANDLE_CLOSE_MODE)
        clock.now.return_value = at("11:04:00")
        closes.return_value = []
        core.manage_open_positions(**args)
        sell.assert_not_called()
        assert pos["current_stop_price"] == 3.37
        clock.now.return_value = at("11:05:03")
        closes.return_value = [(at("11:05:00"), 3.61)]
        core.manage_open_positions(**args)
        sell.assert_not_called()
        assert pos["current_stop_price"] == pytest.approx(3.4295)


def test_eod_approval_does_not_wait_for_option_candle_data():
    pos = position()
    with patch("bot_core.completed_option_closes") as closes, patch("bot_core.submit_exit_order") as sell:
        events = core.manage_candle_close_position(Mock(), pos, NS(), at("15:46:00"),
            account=None, stop_loss_pct=10, breakeven_trigger_pct=10,
            trailing_trigger_pct=15, trailing_stop_pct=5, force_exit_enabled=True,
            force_exit_time=time(15,45), require_eod_exit_approval=True,
            allow_live_orders=True)
    assert events[0]["Action"] == "EXIT_APPROVAL_REQUIRED"
    closes.assert_not_called()
    sell.assert_not_called()


def test_exit_submission_logs_actual_fill_and_waits_for_net_settlement():
    pos = position()
    ib = Mock()
    ib.qualifyContracts.return_value = []
    trade = NS(order=NS(orderId=123, permId=456, clientId=121),
               orderStatus=NS(status="Filled", filled=5, remaining=0, avgFillPrice=3.61),
               fills=[])
    ib.placeOrder.return_value = trade
    with patch("bot_core.read_active_positions", return_value=[]), patch("bot_core.cancel_protective_orders"), patch("bot_core.log_trade") as log:
        _, pnl = core.submit_exit_order(ib, pos, 3.35, "Trailing stop hit at five-minute close")
    assert pnl is None
    assert pos["exit_pending"]
    assert log.call_args.args[0]["exit_price"] == 3.61
    assert log.call_args.args[0]["realized_pnl"] is None
    assert log.call_args.args[0]["filled_quantity"] == 5


@pytest.mark.parametrize("quantity", [5, 15])
def test_existing_bot_stops_migrate_to_emergency_level_without_tightening_virtual_stop(quantity):
    pos = position()
    pos.update(quantity=quantity, current_stop_price=3.8)
    ids = list(range(1, quantity // 5 + 1))
    pos.update(ibkr_stop_order_ids=ids, ibkr_stop_order_prices_by_id={str(i):3.37 for i in ids})
    trades = [NS(order=NS(orderId=i, orderType="STP", auxPrice=3.37)) for i in ids]
    ib = Mock()
    ib.qualifyContracts.return_value = []
    with patch("bot_core._open_sell_trades_for_position", return_value=trades):
        core.ensure_protective_orders(ib, pos, stop_loss_pct=10)
    assert ib.placeOrder.call_count == len(ids)
    assert all(t.order.auxPrice == 2.82 for t in trades)
    assert pos["current_stop_price"] == 3.8


def test_manual_broker_stop_override_is_preserved():
    pos = position()
    pos.update(current_stop_price=3.8, ibkr_stop_order_id=1,
               ibkr_stop_order_prices_by_id={"1":3.37})
    trade = NS(order=NS(orderId=1, orderType="STP", auxPrice=3.4))
    ib = Mock()
    ib.qualifyContracts.return_value = []
    with patch("bot_core._open_sell_trades_for_position", return_value=[trade]):
        core.ensure_protective_orders(ib, pos, stop_loss_pct=10)
    ib.placeOrder.assert_not_called()
    assert pos["emergency_stop_price"] == 3.4
    assert pos["current_stop_price"] == 3.8


def test_incomplete_chunk_submission_recovers_only_after_broker_quantity_updates():
    pos = position()
    pos.update(quantity=15, exit_pending=True, pending_exit_order_ids=[123],
               pending_exit_requested_quantity=15, pending_exit_submitted_quantity=5)
    ib = Mock()
    ib.trades.return_value = [NS(order=NS(orderId=123), orderStatus=NS(status="Filled", filled=5))]
    assert core.exit_order_still_pending(ib, pos)
    pos["quantity"] = 10  # confirmed by reconciliation, not inferred from submission
    assert not core.exit_order_still_pending(ib, pos)
