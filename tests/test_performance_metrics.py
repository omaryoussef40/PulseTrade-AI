from __future__ import annotations

import unittest

import pandas as pd

from modules.performance_metrics import realized_r_multiple


class PerformanceMetricTests(unittest.TestCase):
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
