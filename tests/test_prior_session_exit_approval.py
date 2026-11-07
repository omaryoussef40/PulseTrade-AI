from datetime import datetime, time
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest
import bot_core as core
import engine


NOW = datetime.fromisoformat("2026-11-05T16:00:00-05:00")


def position():
    return dict(id="old", symbol="TEST", signal="CALL", option="TEST CALL", con_id=123,
                expiry="20261113", strike=100, quantity=2, entry_price=3,
                entry_time="2026-11-04T10:00:00-05:00", management_mode="automated",
                current_stop_price=2.7, take_profit_price=3.6, highest_price=3)


@pytest.fixture(autouse=True)
def isolate(tmp_path):
    with (patch.object(core, "_trade_log_entry_lookup", return_value={}),
          patch.object(core, "read_active_positions", return_value=[]),
          patch.object(core, "datetime", wraps=datetime) as clock,
          patch.object(engine, "datetime", wraps=datetime) as engine_clock,
          patch("modules.telegram_polling.LOCK_DIR", tmp_path / "polling"),
          patch.object(engine, "PENDING_APPROVALS_FILE", tmp_path / "approvals.json"),
          patch.object(engine, "TELEGRAM_APPROVAL_STATE_FILE", tmp_path / "telegram.json")):
        clock.now.return_value = NOW
        engine_clock.now.return_value = NOW
        yield


@pytest.mark.parametrize("candle", [True, False])
@pytest.mark.parametrize("reason", ["stop", "eod"])
def test_older_positions_request_approval_in_both_exit_modes(candle, reason):
    pos = position()
    with (patch.object(core, "read_active_positions", return_value=[pos]),
          patch.object(core, "write_active_positions"),
          patch.object(core, "reconcile_active_positions_with_broker", return_value=[]),
          patch.object(core, "ensure_protective_orders", return_value=[]),
          patch.object(core, "get_snapshot_mid", return_value={"Mid": 2}),
          patch.object(core, "evaluate_premium_health", return_value=[]),
          patch.object(core, "completed_option_closes", return_value=[(NOW, 2)]),
          patch.object(core, "submit_exit_order") as sell):
        ib = Mock()
        ib.portfolio.return_value = []
        ib.qualifyContracts.return_value = []
        events = core.manage_open_positions(ib, None, 10, 20, 10, 15, 5, time(15, 45), True,
            force_exit_enabled=reason == "eod", require_eod_exit_approval=False,
            exit_evaluation_mode="five_minute_close" if candle else "snapshot")
    assert any(event["Action"] == "EXIT_APPROVAL_REQUIRED" for event in events)
    sell.assert_not_called()


def test_direct_and_low_level_sells_cannot_bypass_confirmation():
    ib = Mock()
    pos = position()
    with patch.object(core, "read_active_positions", return_value=[pos]):
        with pytest.raises(ValueError, match="Telegram exit confirmation"):
            core.submit_exit_order(ib, pos, 2, "Stop loss hit")
        contract = NS(conId=123, symbol="TEST", lastTradeDateOrContractMonth="20261113", strike=100, right="C")
        with pytest.raises(ValueError, match="Telegram exit confirmation"):
            core.place_option_order(ib, contract, "SELL", 2, "MARKET", None)
    ib.placeOrder.assert_not_called()


def test_cancels_only_bot_owned_protection_and_does_not_create_orders():
    pos = position()
    pulse = NS(ocaGroup="PulseProtect-old", orderId=1, account="A")
    manual = NS(ocaGroup="", orderId=2, account="A")
    other = NS(ocaGroup="PulseProtect-other", orderId=3, account="B")
    ib = Mock()
    with patch.object(core, "_open_sell_trades_for_position", return_value=[NS(order=o) for o in (pulse, manual, other)]):
        core.ensure_protective_orders(ib, pos, "A")
    ib.cancelOrder.assert_called_once_with(pulse)
    ib.placeOrder.assert_not_called()
    assert pos["management_mode"] == "automated"


def test_pending_or_rejected_request_is_not_repeated_and_message_explains_older_position():
    order, created = engine.create_pending_exit_approval(position(), 2, "Stop loss hit", "Live")
    assert created and order["prior_session_exit"]
    assert "Previous-Day Exit Approval" in engine.approval_message(order)
    assert not engine.create_pending_exit_approval(position(), 2, "Stop loss hit", "Live")[1]
    engine.update_pending_approval(order["id"], status="rejected")
    assert not engine.create_pending_exit_approval(position(), 2, "End-of-day forced exit", "Live")[1]


@pytest.mark.parametrize("action", ["confirm", "reject"])
def test_telegram_buttons_confirm_or_leave_the_position_open(action):
    order, _ = engine.create_pending_exit_approval(position(), 2, "Stop loss hit", "Live")
    engine.update_pending_approval(order["id"], status="sent", telegram_message_id=42)
    update = {"result": [{"update_id": 1, "callback_query": {"id": "c", "data": f"{action}:{order['id']}",
        "message": {"chat": {"id": "chat"}, "message_id": 42}}}]}
    tg = NS(bot_token="test", chat_id="chat")
    with (patch.object(engine, "telegram_post", return_value=update),
          patch.object(engine, "telegram_try_post"),
          patch.object(engine, "submit_approved_order", return_value="Submitted") as submit):
        engine.process_telegram_order_callbacks(Mock(), NS(account="A"), tg, can_trade=True)
    if action == "confirm":
        assert submit.call_args.args[2]["status"] == "approved"
        assert submit.call_args.args[2]["confirmed_via"] == "Telegram"
    else:
        submit.assert_not_called()
        assert engine.read_pending_approvals()[0]["status"] == "rejected"


@pytest.mark.parametrize("confirmed", [False, True])
def test_final_order_boundary_accepts_only_persisted_telegram_confirmation(confirmed):
    pos = position()
    order, _ = engine.create_pending_exit_approval(pos, 2, "Stop loss hit", "Live")
    if confirmed:
        engine.update_pending_approval(order["id"], status="approved", confirmed_via="Telegram", telegram_message_id=42)
    ib = Mock()
    ib.qualifyContracts.return_value = [NS()]
    with (patch.object(core, "read_active_positions", return_value=[pos]),
          patch.object(engine, "read_active_positions", return_value=[pos]),
          patch.object(engine, "suspend_prior_session_protection", return_value=[]),
          patch.object(engine, "place_option_order", side_effect=RuntimeError("test boundary")) as place):
        with pytest.raises((RuntimeError, ValueError), match="test boundary" if confirmed else "Telegram exit confirmation"):
            engine.submit_approved_order(ib, NS(account="A"), order)
        if confirmed:
            assert place.call_args.kwargs["prior_session_approved"] is True
        else:
            place.assert_not_called()


def test_failed_message_send_retries_without_selling_or_spamming():
    event = {"Action": "EXIT_APPROVAL_REQUIRED", "Position": position(), "Current": 2, "Reason": "Stop loss hit"}
    with patch.object(engine, "send_order_approval_message", return_value=False) as send:
        engine.request_eod_exit_approvals(NS(), [event], "Live")
        engine.request_eod_exit_approvals(NS(), [event], "Live")
    send.assert_called_once()
    assert engine.read_pending_approvals()[0]["status"] == "approval_send_failed"


def test_same_day_position_still_uses_existing_exit_rules():
    core.assert_position_exit_allowed(dict(position(), entry_time=NOW.isoformat()))


def test_quantity_change_expires_old_request():
    with patch.object(engine, "make_approval_id", side_effect=["old-request", "new-request"]):
        old, _ = engine.create_pending_exit_approval(position(), 2, "Stop loss hit", "Live")
        fresh, created = engine.create_pending_exit_approval(dict(position(), quantity=1), 2, "Stop loss hit", "Live")
    assert created and fresh["quantity"] == 1
    assert next(order for order in engine.read_pending_approvals() if order["id"] == old["id"])["status"] == "expired"


def test_duplicate_telegram_click_does_not_submit_twice():
    order, _ = engine.create_pending_exit_approval(position(), 2, "Stop loss hit", "Live")
    engine.update_pending_approval(order["id"], status="sent", telegram_message_id=42)
    callback = {"id": "c", "data": f"confirm:{order['id']}", "message": {"chat": {"id": "chat"}, "message_id": 42}}
    updates = {"result": [{"update_id": i, "callback_query": callback} for i in (1, 2)]}
    def submitted(*args):
        engine.update_pending_approval(order["id"], status="submitted")
        return "Submitted"
    with (patch.object(engine, "telegram_post", return_value=updates),
          patch.object(engine, "telegram_try_post"),
          patch.object(engine, "submit_approved_order", side_effect=submitted) as submit):
        engine.process_telegram_order_callbacks(Mock(), NS(account="A"), NS(bot_token="test", chat_id="chat"), True)
    submit.assert_called_once()


def test_current_local_entry_date_takes_precedence_over_older_contract_history():
    today = dict(position(), entry_time=NOW.isoformat())
    with patch.object(core, "read_active_positions", return_value=[today]):
        assert not core.requires_prior_session_exit_approval(position())
