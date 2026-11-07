from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest

from modules.daily_option_capital import daily_option_capital, saved_starting_capital

ET = ZoneInfo("America/New_York")


def broker(funds="3000"):
    rows = [SimpleNamespace(account="A", tag="AvailableFunds", currency="USD", value=funds),
            SimpleNamespace(account="A", tag="NetLiquidation", currency="USD", value="5000"),
            SimpleNamespace(account="B", tag="AvailableFunds", currency="USD", value="9000")]
    return SimpleNamespace(managedAccounts=lambda: ["A", "B"], accountSummary=lambda: rows)


def test_available_funds_baseline_survives_restart_and_resets_on_new_york_day(tmp_path):
    path = tmp_path / "capital.sqlite"
    today = datetime(2026, 11, 5, 10, tzinfo=ET)
    assert saved_starting_capital("A", now=today, state_file=path) is None
    assert daily_option_capital(broker(), "A", now=today, state_file=path) == (3000, 3000)
    # A new broker object simulates restart; changing funds does not shrink the baseline.
    assert daily_option_capital(broker("2000"), "A", now=today, state_file=path) == (3000, 2000)
    utc_evening = datetime.fromisoformat("2026-11-06T02:00:00+00:00")
    assert daily_option_capital(broker("3500"), "A", now=utc_evening, state_file=path) == (3000, 3500)
    tomorrow = datetime(2026, 11, 6, 10, tzinfo=ET)
    assert daily_option_capital(broker("2500"), "A", now=tomorrow, state_file=path) == (2500, 2500)
    assert saved_starting_capital("A", now=today, state_file=path) == 3000
    assert daily_option_capital(broker(), "B", now=today, state_file=path) == (9000, 9000)


@pytest.mark.parametrize("funds", ["nan", "inf", "invalid"])
def test_invalid_available_funds_never_fall_back_to_total_equity(tmp_path, funds):
    with pytest.raises(ValueError, match="available funds unavailable"):
        daily_option_capital(broker(funds), "A", state_file=tmp_path / "capital.sqlite")


def test_missing_account_or_funds_blocks_entries(tmp_path):
    ib = broker()
    with pytest.raises(ValueError, match="Select one"):
        daily_option_capital(ib, state_file=tmp_path / "capital.sqlite")
    ib.accountSummary = lambda: []
    with pytest.raises(ValueError, match="available funds unavailable"):
        daily_option_capital(ib, "A", state_file=tmp_path / "capital.sqlite")


@pytest.mark.parametrize("funds", ["0", "-100"])
def test_zero_and_negative_funds_do_not_enable_buying_power_fallback(tmp_path, funds):
    assert daily_option_capital(broker(funds), "A", state_file=tmp_path / "capital.sqlite") == (0, 0)


@pytest.mark.parametrize("available,blocked", [(0, True), (199, True), (300, False)])
def test_pending_approval_uses_daily_baseline_and_rechecks_current_funds(available, blocked):
    import engine
    cfg = {"risk": {"use_daily_available_capital": True, "recycle_capital_after_exit": True}}
    order = dict(symbol="TEST", action="BUY", quantity=1, order_type="LIMIT", limit_price=2)
    ib = Mock()
    ib.qualifyContracts.return_value = [SimpleNamespace()]
    with (patch.object(engine, "load_config", return_value=cfg),
          patch.object(engine, "reconstruct_option_contract", return_value=SimpleNamespace()),
          patch.object(engine, "daily_option_capital", return_value=(3000, available)),
          patch.object(engine, "get_today_trade_stats", return_value=(0, 400)),
          patch.object(engine, "get_today_traded_symbols", return_value=set()),
          patch.object(engine, "get_open_position_deployed", return_value=400) as deployed,
          patch.object(engine, "verified_account_equity") as equity,
          patch.object(engine, "check_automated_option_entry") as check,
          patch.object(engine, "place_option_order", side_effect=RuntimeError("test order boundary")) as place):
        with pytest.raises((ValueError, RuntimeError), match="available funds" if blocked else "test order boundary"):
            engine.submit_approved_order(ib, SimpleNamespace(account="A"), order)
        equity.assert_not_called()
        if blocked:
            place.assert_not_called()
        else:
            deployed.assert_called_once_with(count_prior_day_positions=False)
            check.assert_called_once_with(cfg, order, 0, 400, account_size=3000)
