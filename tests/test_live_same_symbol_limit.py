from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

import bot_core
import engine


class LiveSameSymbolLimitTests(unittest.TestCase):
    def test_today_traded_symbols_only_returns_working_entries(self):
        now = datetime.now(bot_core.EASTERN)
        rows = pd.DataFrame(
            [
                {"timestamp": now.isoformat(), "event": "ENTRY", "symbol": "aapl", "status": "Filled"},
                {"timestamp": now.isoformat(), "event": "ENTRY", "symbol": "MSFT", "status": "Cancelled"},
                {"timestamp": now.isoformat(), "event": "EXIT", "symbol": "NVDA", "status": "Filled"},
                {"timestamp": now.isoformat(), "event": "ENTRY", "symbol": "META", "status": "Filled", "source": "manual_order"},
                {"timestamp": (now - timedelta(days=1)).isoformat(), "event": "ENTRY", "symbol": "TSLA", "status": "Filled"},
            ]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            trade_log = Path(tmpdir) / "trade_log.csv"
            rows.to_csv(trade_log, index=False)
            with patch.object(bot_core, "TRADE_LOG_FILE", str(trade_log)):
                self.assertEqual(bot_core.get_today_traded_symbols(), {"AAPL"})

    def test_approved_scanner_order_cannot_repeat_ticker(self):
        ib = Mock()
        ib.qualifyContracts.return_value = []
        order = {
            "id": "approval-1",
            "source": "scanner",
            "symbol": "AAPL",
            "signal": "CALL",
            "action": "BUY",
            "quantity": 1,
            "order_type": "MARKET",
            "mid": 1.0,
        }
        with (
            patch.object(engine, "reconstruct_option_contract", return_value=SimpleNamespace()),
            patch.object(engine, "load_config", return_value={"risk": {"allow_same_symbol_same_day": False}}),
            patch.object(engine, "get_today_traded_symbols", return_value={"AAPL"}),
            patch.object(engine, "place_option_order") as place_order,
        ):
            with self.assertRaisesRegex(RuntimeError, "already traded today"):
                engine.submit_approved_order(ib, SimpleNamespace(account="TEST"), order)
        place_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
