from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
import tempfile

import pandas as pd

from bot_core import EASTERN, sync_today_executions_to_trade_log


class ExecutionSyncTests(TestCase):
    def test_delayed_fee_report_is_not_published_as_final_net_profit(self):
        contract = SimpleNamespace(symbol="RKLB", secType="OPT", conId=935120198,
            multiplier="100", lastTradeDateOrContractMonth="20261106", strike=56.,
            right="C", localSymbol="RKLB  261106C00056000")
        fills = []
        for exec_id, qty, price, report in [
            ("SELL-1", 2, 3.60, SimpleNamespace(execId="SELL-1", commission=1., realizedPNL=90.)),
            ("SELL-2", 3, 3.62, SimpleNamespace(execId="", commission=0., realizedPNL=0.)),
        ]:
            execution = SimpleNamespace(side="SLD", shares=qty, price=price,
                time=datetime(2026,10,27,11,4,tzinfo=EASTERN), execId=exec_id,
                acctNumber="", orderId=1, permId=2, clientId=121)
            fills.append(SimpleNamespace(contract=contract, execution=execution, commissionReport=report))
        ib = Mock()
        ib.reqExecutions.return_value = fills
        ib.fills.return_value = fills
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + "/trades.csv"
            with patch("bot_core.TRADE_LOG_FILE", path):
                sync_today_executions_to_trade_log(ib, target_date=date(2026,10,27))
                first = pd.read_csv(path).iloc[0]
                self.assertEqual(first["pnl_basis"], "pending_broker")
                fills[1].commissionReport = SimpleNamespace(execId="SELL-2", commission=2., realizedPNL=143.02)
                sync_today_executions_to_trade_log(ib, target_date=date(2026,10,27))
                settled = pd.read_csv(path)
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled.iloc[0]["pnl_basis"], "broker_net")
        self.assertAlmostEqual(settled.iloc[0]["exit_price"], 3.612)
        self.assertAlmostEqual(settled.iloc[0]["realized_pnl"], 233.02)

    def test_split_entries_use_complete_source_and_broker_net_pnl(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = f"{temp_dir}/trade_log.csv"
            pd.DataFrame([
                {"timestamp": "2026-09-14T13:55:19-04:00", "event": "ENTRY", "source": "IBKR_EXECUTION", "external_id": "BUY-1", "symbol": "BAC", "option": "BAC 59 C", "con_id": 914580381, "quantity": 1, "filled_quantity": 1, "entry_price": 0.96, "sec_type": "OPT", "multiplier": 100},
                {"timestamp": "2026-09-14T13:55:19-04:00", "event": "ENTRY", "source": "IBKR_FLEX", "external_id": "FLEX-1", "symbol": "BAC", "option": "BAC 59 C", "con_id": 914580381, "quantity": 1, "entry_price": 0.96, "sec_type": "OPT", "multiplier": 100},
                {"timestamp": "2026-09-14T14:01:23-04:00", "event": "ENTRY", "source": "IBKR_FLEX", "external_id": "FLEX-2", "symbol": "BAC", "option": "BAC 59 C", "con_id": 914580381, "quantity": 8, "entry_price": 1.03, "sec_type": "OPT", "multiplier": 100},
                {"timestamp": "2026-09-14T14:01:23-04:00", "event": "ENTRY", "source": "IBKR_FLEX", "external_id": "FLEX-3", "symbol": "BAC", "option": "BAC 59 C", "con_id": 914580381, "quantity": 2, "entry_price": 1.02, "sec_type": "OPT", "multiplier": 100},
                {"timestamp": "2026-09-15T14:16:17-04:00", "event": "EXIT", "source": "IBKR_EXECUTION", "external_id": "IBKR_EXEC-2026-09-15-914580381-SELL", "symbol": "BAC", "option": "BAC 59 C", "con_id": 914580381, "quantity": 11, "filled_quantity": 11, "entry_price": 0.96, "exit_price": 1.08, "realized_pnl": 132.0, "sec_type": "OPT", "multiplier": 100},
            ]).to_csv(path, index=False)

            contract = SimpleNamespace(symbol="BAC", secType="OPT", conId=914580381, multiplier="100", lastTradeDateOrContractMonth="20260918", strike=59.0, right="C", localSymbol="BAC   260918C00059000")
            execution = SimpleNamespace(side="SLD", shares=11.0, price=1.08, time=datetime(2026, 9, 15, 14, 16, 17, tzinfo=EASTERN), execId="EXIT-1", acctNumber="", orderId=1, permId=2, clientId=3, orderRef="", liquidation=0)
            commission = SimpleNamespace(execId="EXIT-1", commission=7.543963, realizedPNL=48.571737)
            ib = Mock()
            fill = SimpleNamespace(contract=contract, execution=execution, commissionReport=commission)
            ib.reqExecutions.return_value = [fill]
            ib.fills.return_value = [fill]

            with patch("bot_core.TRADE_LOG_FILE", path):
                updated, _ = sync_today_executions_to_trade_log(ib, target_date=date(2026, 9, 15))

            self.assertEqual(updated, 1)
            result = pd.read_csv(path)
            exit_row = result[result["event"].eq("EXIT")].iloc[0]
            self.assertAlmostEqual(float(exit_row["entry_price"]), 1.0218, places=4)
            self.assertAlmostEqual(float(exit_row["realized_pnl"]), 48.57, places=2)
            self.assertAlmostEqual(float(exit_row["commission"]), -7.543963, places=6)


if __name__ == "__main__":
    import unittest
    unittest.main()
