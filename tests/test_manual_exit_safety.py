import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import pandas as pd
import bot_core as core
import engine


class ManualExitSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'trades.csv')
        self.patch_log = patch.object(core, 'TRADE_LOG_FILE', self.path)
        self.patch_log.start()
        self.addCleanup(self.patch_log.stop)
        self.positions = patch.object(core, 'read_active_positions', return_value=[])
        self.positions.start()
        self.addCleanup(self.positions.stop)
        self.pos = dict(symbol='MSFT', signal='CALL', expiry='20260921', strike=500,
                        con_id=921800756, quantity=2, entry_price=6.5)

    def history(self):
        pd.DataFrame([
            dict(self.pos, event='ENTRY', timestamp='2026-09-15T09:34:12-04:00',
                 score='MANUAL', setup_quality='Manual order', source='manual_order'),
            dict(self.pos, event='ENTRY', timestamp='2026-09-15T09:34:13-04:00',
                 source='IBKR_EXECUTION'),
        ]).to_csv(self.path, index=False)

    def test_missing_csv_contract_fields_do_not_break_history(self):
        pd.DataFrame([dict(event='ENTRY', con_id=float('nan'), strike=float('nan'),
                           symbol='MSFT', score='MANUAL', setup_quality='Manual order')]).to_csv(self.path, index=False)
        core._trade_log_entry_lookup()

    def test_broker_import_cannot_erase_manual_provenance(self):
        self.history()
        self.assertTrue(core.is_monitor_only_position(self.pos))
        # CSV float formatting must match an IBKR contract even without conId.
        self.assertTrue(core.is_monitor_only_position(dict(self.pos, con_id=None)))

    def test_direct_exit_of_legacy_manual_position_is_rejected(self):
        self.history()
        ib = Mock()
        with self.assertRaisesRegex(ValueError, 'exits are prohibited'):
            core.submit_exit_order(ib, self.pos, 3.13, 'Stop loss hit')
        ib.placeOrder.assert_not_called()
        ib.cancelOrder.assert_not_called()

    def test_low_level_sell_cannot_bypass_manual_guard(self):
        self.history()
        ib = Mock()
        contract = NS(conId=921800756, symbol='MSFT', lastTradeDateOrContractMonth='20260921', strike=500, right='C')
        with self.assertRaisesRegex(ValueError, 'exits are prohibited'):
            core.place_option_order(ib, contract, 'SELL', 2, 'MARKET', None)
        ib.placeOrder.assert_not_called()

    def test_stale_exit_approval_is_rejected(self):
        self.history()
        ib = Mock()
        with self.assertRaisesRegex(ValueError, 'exits are prohibited'):
            engine.submit_approved_order(ib, NS(account=None), dict(self.pos, action='SELL'))
        ib.placeOrder.assert_not_called()

    def test_local_manual_flag_cannot_be_overridden_by_exit_payload(self):
        with patch.object(core, 'read_active_positions', return_value=[dict(self.pos, software_control_enabled=False)]):
            with self.assertRaises(ValueError):
                core.assert_position_exit_allowed(dict(self.pos, management_mode='automated'))

    def test_cleanup_cancels_only_pulse_owned_orders(self):
        self.history()
        pulse = NS(orderId=6111, ocaGroup='PulseProtect-MSFT-legacy', account='A')
        user = NS(orderId=6112, ocaGroup='', account='A')
        other_account = NS(orderId=6113, ocaGroup='PulseProtect-MSFT-other', account='B')
        ib = Mock()
        pos = dict(self.pos, account='A', ibkr_stop_order_id=6112)
        with patch.object(core, '_open_sell_trades_for_position', return_value=[NS(order=x) for x in [pulse, user, other_account]]):
            events = core.ensure_protective_orders(ib, pos)
        ib.cancelOrder.assert_called_once_with(pulse)
        ib.placeOrder.assert_not_called()
        self.assertIsNone(pos['current_stop_price'])
        self.assertEqual(events[0]['Action'], 'CANCEL_MANUAL_POSITION_PROTECTION')

    def test_cleanup_failure_is_visible_and_never_sells(self):
        ib = Mock()
        ib.cancelOrder.side_effect = RuntimeError('Not connected')
        pos = dict(self.pos, management_mode='monitor_only')
        with patch.object(core, '_open_sell_trades_for_position', return_value=[NS(order=NS(orderId=1, ocaGroup='PulseProtect-old'))]):
            events = core.ensure_protective_orders(ib, pos)
        self.assertEqual(events[0]['Action'], 'CANCEL_PROTECTION_FAILED')
        ib.placeOrder.assert_not_called()

    def test_unidentified_broker_position_is_monitor_only(self):
        pd.DataFrame([dict(self.pos, event='ENTRY', source='IBKR_EXECUTION')]).to_csv(self.path, index=False)
        self.assertTrue(core.is_monitor_only_position(self.pos))
        with self.assertRaises(ValueError):
            core.assert_position_exit_allowed(self.pos)

    def test_manual_history_overrides_explicit_automated_mode(self):
        self.history()
        self.assertTrue(core.is_monitor_only_position(dict(self.pos, management_mode='automated')))

    def test_automated_position_remains_eligible_for_exit(self):
        pd.DataFrame([dict(self.pos, event='ENTRY', source='strategy', score=96)]).to_csv(self.path, index=False)
        core.assert_position_exit_allowed(dict(self.pos, management_mode='automated'))

if __name__ == '__main__':
    unittest.main()
