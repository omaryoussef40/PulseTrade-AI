from datetime import datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
import pytest
import bot_core as core

ET = ZoneInfo("America/New_York")

@pytest.mark.parametrize("entry,expected", [
    ("2026-11-02T10:00:00-05:00", True),
    ("2026-11-03T10:00:00-05:00", False),
    ("2026-11-03T02:00:00+00:00", True),
    ("2026-11-02T10:00:00", True),
])
def test_prior_session_uses_new_york_calendar(entry, expected):
    assert core.is_prior_session_position({"entry_time":entry},datetime(2026,11,3,12,tzinfo=ET)) is expected


def test_old_engine_position_never_sells_or_changes_broker_orders():
    pos=dict(symbol="TEST", entry_time="2020-01-01T10:00:00-05:00", management_mode="automated", entry_price=5, quantity=1)
    ib=Mock()
    with patch.object(core,"read_active_positions",return_value=[pos]), patch.object(core,"write_active_positions"), patch.object(core,"reconcile_active_positions_with_broker",return_value=[]):
        events=core.manage_open_positions(ib,None,10,20,10,15,5,datetime.now().time(),True)
    assert events[0]["Action"] == "PRIOR_SESSION_MONITOR_ONLY"
    ib.placeOrder.assert_not_called()
    ib.cancelOrder.assert_not_called()
    assert core.ensure_protective_orders(ib,pos)[0]["Action"] == "PRIOR_SESSION_MONITOR_ONLY"
    assert core._ensure_protective_orders(ib,pos) == []
    with pytest.raises(ValueError,match="Prior-session"):
        core.assert_position_exit_allowed(pos)


def test_missing_local_entry_time_uses_trade_history():
    pos=dict(symbol="TEST",option="test")
    with patch.object(core,"_trade_log_entry_lookup",return_value={"label:test":{"timestamp":"2020-01-01T10:00:00-05:00"}}):
        assert core.is_prior_session_position(pos)


def test_pending_exit_payload_cannot_override_prior_day_local_position():
    current=datetime.now(ET).isoformat()
    old=dict(option="test",entry_time="2020-01-01T10:00:00-05:00",management_mode="automated")
    with patch.object(core,"read_active_positions",return_value=[old]), patch.object(core,"_trade_log_entry_lookup",return_value={}):
        with pytest.raises(ValueError,match="exits are prohibited"):
            core.assert_position_exit_allowed(dict(option="test",entry_time=current,management_mode="automated"))
