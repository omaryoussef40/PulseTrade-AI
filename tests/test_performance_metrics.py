from __future__ import annotations

import unittest

import pandas as pd

from modules.performance_metrics import deduplicate_broker_event_rows, realized_r_multiple


class PerformanceMetricTests(unittest.TestCase):
    def test_broker_execution_replaces_duplicate_local_exit(self):
        rows = pd.DataFrame([
            {
                "timestamp": "2026-08-31T09:35:20-04:00",
                "event": "EXIT",
                "symbol": "SMCI",
                "signal": "CALL",
                "quantity": 20,
                "exit_price": 0.675,
                "realized_pnl": -2110.0,
                "source": "IBKR_EXECUTION",
                "broker_order_ids": "5251,5252,5253,5254",
                "broker_perm_ids": "1504019224,1504019225,1504019226,1504019227",
                "exit_reason": "IBKR sell execution imported",
            },
            {
                "timestamp": "2026-08-31T09:35:24-04:00",
                "event": "EXIT",
                "symbol": "SMCI",
                "signal": "CALL",
                "quantity": 20,
                "exit_price": 0.72,
                "realized_pnl": -2030.0,
                "source": "PULSE_EXIT_ORDER",
                "broker_order_ids": "5254,5253,5252,5251",
                "broker_perm_ids": "1504019224,1504019225,1504019226,1504019227",
                "exit_reason": "Stop loss hit",
            },
        ])

        result = deduplicate_broker_event_rows(rows)

        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["source"], "IBKR_EXECUTION")
        self.assertEqual(result.iloc[0]["exit_price"], 0.675)
        self.assertEqual(result.iloc[0]["realized_pnl"], -2110.0)
        self.assertEqual(result.iloc[0]["exit_reason"], "Stop loss hit")

    def test_broker_execution_replaces_nearby_local_exit_with_different_order_ids(self):
        rows = pd.DataFrame([
            {
                "timestamp": "2026-09-01T10:12:21-04:00",
                "event": "EXIT",
                "symbol": "AAPL",
                "signal": "CALL",
                "quantity": 10,
                "entry_price": 4.66,
                "exit_price": 5.55,
                "realized_pnl": 890.0,
                "source": "IBKR_EXECUTION",
                "con_id": 917428558,
                "broker_order_ids": "0",
                "broker_perm_ids": "588633907",
                "exit_reason": "IBKR sell execution imported",
            },
            {
                "timestamp": "2026-09-01T10:14:15.497215-04:00",
                "event": "EXIT",
                "symbol": "AAPL",
                "signal": "CALL",
                "quantity": 10,
                "entry_price": 4.66,
                "exit_price": 5.70,
                "realized_pnl": 1040.0,
                "source": "PULSE_EXIT_ORDER",
                "broker_order_ids": "6,7",
                "broker_perm_ids": "453434800,453434801",
                "exit_reason": "Dashboard market close",
            },
        ])

        result = deduplicate_broker_event_rows(rows)

        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["source"], "IBKR_EXECUTION")
        self.assertEqual(result.iloc[0]["exit_price"], 5.55)
        self.assertEqual(result.iloc[0]["realized_pnl"], 890.0)
        self.assertEqual(result.iloc[0]["exit_reason"], "Dashboard market close")

    def test_separate_same_contract_trades_are_not_proximity_deduplicated(self):
        rows = pd.DataFrame([
            {
                "timestamp": "2026-09-01T10:00:00-04:00", "event": "EXIT",
                "symbol": "AAPL", "signal": "CALL", "quantity": 1,
                "source": "IBKR_EXECUTION", "broker_perm_ids": "100",
            },
            {
                "timestamp": "2026-09-01T10:30:00-04:00", "event": "EXIT",
                "symbol": "AAPL", "signal": "CALL", "quantity": 1,
                "source": "PULSE_EXIT_ORDER", "broker_perm_ids": "200",
            },
        ])

        self.assertEqual(len(deduplicate_broker_event_rows(rows)), 2)

    def test_flex_exit_uses_matching_entry_contract(self):
        entries = pd.DataFrame([{
            "timestamp": pd.Timestamp("2026-08-14 10:17", tz="America/New_York"),
            "symbol": "AMD",
            "signal": "CALL",
            "con_id": 826198120,
            "quantity": 1,
            "entry_price": 16.36,
        }])
        exits = pd.DataFrame([{
            "timestamp": pd.Timestamp("2026-08-14 10:36", tz="America/New_York"),
            "symbol": "AMD",
            "signal": "CALL",
            "con_id": 826198120,
            "quantity": 1,
            "entry_price": None,
            "realized_pnl": 489.074294,
        }])
        self.assertEqual(realized_r_multiple(exits, 10.0, entries), 2.99)

    def test_total_r_sums_trade_level_results(self):
        exits = pd.DataFrame([
            {"entry_price": 5.0, "quantity": 1, "multiplier": 100, "realized_pnl": 50.0},
            {"entry_price": 10.0, "quantity": 1, "multiplier": 100, "realized_pnl": 200.0},
        ])
        self.assertEqual(realized_r_multiple(exits, 10.0), 3.0)

    def test_stock_trade_defaults_to_one_share_multiplier(self):
        exits = pd.DataFrame([{
            "symbol": "TEST",
            "sec_type": "STK",
            "entry_price": 10.0,
            "quantity": 100,
            "realized_pnl": 50.0,
        }])
        self.assertEqual(realized_r_multiple(exits, 5.0), 1.0)


if __name__ == "__main__":
    unittest.main()
