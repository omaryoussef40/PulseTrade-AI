import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from api import map_position
from bot_core import EASTERN, portfolio_market_snapshot_for_position, record_position_market_snapshot


class PositionMarketSnapshotTests(unittest.TestCase):
    def test_records_live_quote_and_open_position_pnl(self):
        position = {
            "symbol": "TEST",
            "signal": "CALL",
            "quantity": 2,
            "entry_price": 10.0,
        }
        market = {
            "Mid": 12.0,
            "Bid": 11.8,
            "Ask": 12.2,
            "Spread %": (12.2 - 11.8) / 12.0,
            "Delta": 0.55,
            "Theta": -0.15,
            "Volume": 120,
        }

        current = record_position_market_snapshot(
            position,
            market,
            datetime(2026, 8, 17, 11, 5, tzinfo=EASTERN),
        )

        self.assertEqual(current, 12.0)
        self.assertEqual(position["current_price"], 12.0)
        self.assertEqual(position["cost_basis"], 2000.0)
        self.assertEqual(position["market_value"], 2400.0)
        self.assertEqual(position["unrealized_pnl"], 400.0)
        self.assertEqual(position["unrealized_pct"], 20.0)
        self.assertEqual(position["current_spread_pct"], 3.33)
        self.assertEqual(position["market_data_status"], "Live")
        self.assertFalse(position["market_data_stale"])

    def test_marks_missing_quote_as_unavailable(self):
        position = {"entry_price": 10.0, "quantity": 1}

        current = record_position_market_snapshot(
            position,
            {"Mid": None},
            datetime(2026, 8, 17, 11, 5, tzinfo=EASTERN),
        )

        self.assertIsNone(current)
        self.assertEqual(position["market_data_status"], "Quote unavailable")
        self.assertEqual(position["premium_health"], "Unavailable")
        self.assertTrue(position["market_data_stale"])

    def test_uses_ibkr_portfolio_mark_as_quote_fallback(self):
        contract = Mock(
            conId=904377140,
            symbol="MRVL",
            lastTradeDateOrContractMonth="20260904",
            strike=235.0,
            right="C",
        )
        item = Mock(
            account="DU123",
            contract=contract,
            marketPrice=20.8963814,
            marketValue=6268.91,
            unrealizedPNL=-174.14,
        )
        position = {
            "symbol": "MRVL",
            "signal": "CALL",
            "expiry": "20260904",
            "strike": 235.0,
            "con_id": 904377140,
        }

        market = portfolio_market_snapshot_for_position([item], position, account="DU123")

        self.assertAlmostEqual(market["Mid"], 20.8963814)
        self.assertEqual(market["Portfolio Market Value"], 6268.91)
        self.assertEqual(market["Portfolio Unrealized PnL"], -174.14)
        self.assertEqual(market["Quote Source"], "IBKR portfolio mark")

    def test_api_position_includes_monitoring_and_protection_fields(self):
        mapped = map_position({
            "symbol": "MRVL",
            "signal": "CALL",
            "option": "MRVL 20260904 235 CALL",
            "expiry": "20260904",
            "strike": 235,
            "quantity": 3,
            "entry_price": 21.47,
            "current_price": 22.0,
            "unrealized_pnl": 159.0,
            "unrealized_pct": 2.47,
            "current_stop_price": 19.32,
            "take_profit_price": 27.91,
            "premium_health": "Healthy",
            "premium_health_detail": "Premium 2.5%; stock is 0.4% with trade.",
            "market_data_status": "Live",
            "current_bid": 21.9,
            "current_ask": 22.1,
            "current_delta": 0.56,
            "protective_orders_status": "Already protected",
        })

        self.assertEqual(mapped["last"], 22.0)
        self.assertEqual(mapped["unrealized"], 159.0)
        self.assertEqual(mapped["option"], "MRVL 20260904 235 CALL")
        self.assertEqual(mapped["premium_health"], "Healthy")
        self.assertEqual(mapped["market_data_status"], "Live")
        self.assertEqual(mapped["protective_orders_status"], "Already protected")


class FastPositionMonitorTests(unittest.TestCase):
    @patch("engine.write_health")
    @patch("engine.read_active_positions", return_value=[{"symbol": "MRVL"}])
    @patch("engine.notify_position_close_events", return_value=0)
    @patch("engine.manage_open_positions", return_value=[{"Symbol": "MRVL", "Action": "HOLD"}])
    @patch("engine.sync_active_positions_from_broker", return_value=[])
    @patch("engine.sync_today_executions_to_trade_log", return_value=(0, "nothing new"))
    @patch("engine.is_market_open_now", return_value=True)
    @patch("engine.connect_ib")
    @patch("engine.load_config")
    def test_broker_sync_runs_position_management(
        self,
        load_config,
        connect_ib,
        _market_open,
        _sync_trades,
        _sync_positions,
        manage_positions,
        _notify,
        _read_positions,
        write_health,
    ):
        load_config.return_value = {
            "account_mode": "Live",
            "automation": {
                "enabled": True,
                "place_orders": True,
                "confirm_order_risk": True,
                "live_confirm_text": "TRADE LIVE",
            },
            "ib": {"host": "127.0.0.1", "client_id": 121, "readonly": False},
            "risk": {
                "stop_loss_pct": 10,
                "take_profit_pct": 30,
                "breakeven_trigger_pct": 10,
                "trailing_trigger_pct": 28,
                "trailing_stop_pct": 4,
                "force_exit_hour": 15,
                "force_exit_minute": 55,
                "force_exit_enabled": True,
            },
        }
        ib = Mock()
        ib.isConnected.return_value = True
        connect_ib.return_value = ib

        from engine import run_broker_sync_cycle

        run_broker_sync_cycle()

        self.assertEqual(connect_ib.call_args.args[0].client_id, 121)
        manage_positions.assert_called_once()
        self.assertTrue(manage_positions.call_args.kwargs["allow_live_orders"])
        final_health = write_health.call_args_list[-1].kwargs
        self.assertIn("last_position_refresh", final_health)
        self.assertEqual(final_health["positions_monitored"], 1)
        ib.disconnect.assert_called_once()


if __name__ == "__main__":
    unittest.main()
