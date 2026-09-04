from __future__ import annotations

import math
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from modules.ibkr_lovable_sync import (
    DEFAULT_LOVABLE_POSITION_SYNC_URL,
    LovablePositionStore,
    PositionSyncService,
    SyncSettings,
    contract_key,
    position_snapshot,
)


class FakeResponse:
    status_code = 204
    text = ""

    def json(self):
        return {"ok": True, "positions": 0}


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse()


class PositionSnapshotTests(unittest.TestCase):
    def test_settings_require_all_private_connection_values(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "IBKR_POSITION_SYNC_SECRET"):
                SyncSettings.from_env()

    def test_positions_are_authoritative_and_portfolio_enriches_values(self):
        contract = SimpleNamespace(
            conId=123,
            symbol="AAPL",
            localSymbol="AAPL  TEST",
            secType="OPT",
            currency="USD",
            exchange="SMART",
            lastTradeDateOrContractMonth="20260909",
            strike=322.5,
            right="C",
            multiplier="100",
        )
        position = SimpleNamespace(account="U1", contract=contract, position=2, avgCost=450)
        portfolio = SimpleNamespace(
            account="U1",
            contract=contract,
            averageCost=455,
            marketPrice=5.1,
            marketValue=1020,
            unrealizedPNL=110,
            realizedPNL=0,
        )
        ib = SimpleNamespace(positions=lambda: [position], portfolio=lambda: [portfolio])

        result = position_snapshot(ib, "U1")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["conid"], "123")
        self.assertEqual(result[0]["avg_cost"], 4.55)
        self.assertEqual(result[0]["unrealized_pnl"], 110)
        self.assertEqual(result[0]["expiry"], "2026-09-09")
        self.assertEqual(result[0]["option_type"], "C")

    def test_other_accounts_zero_positions_and_non_finite_values_are_ignored(self):
        contract = SimpleNamespace(
            conId=0, symbol="MSFT", secType="STK", currency="USD",
            lastTradeDateOrContractMonth="", strike=0, right="",
        )
        rows = [
            SimpleNamespace(account="OTHER", contract=contract, position=2, avgCost=1),
            SimpleNamespace(account="U1", contract=contract, position=0, avgCost=1),
            SimpleNamespace(account="U1", contract=contract, position=3, avgCost=math.nan),
        ]
        ib = SimpleNamespace(positions=lambda: rows, portfolio=lambda: [])

        result = position_snapshot(ib, "U1")

        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]["avg_cost"])
        self.assertTrue(contract_key(contract).startswith("contract:STK:MSFT"))

    def test_duplicate_position_callbacks_produce_one_contract_row(self):
        contract = SimpleNamespace(
            conId=123, symbol="PLTR", localSymbol="PLTR TEST", secType="OPT",
            currency="USD", lastTradeDateOrContractMonth="20260911",
            strike=175, right="P", multiplier="100",
        )
        position = SimpleNamespace(account="U1", contract=contract, position=5, avgCost=596)
        ib = SimpleNamespace(positions=lambda: [position, position, position], portfolio=lambda: [])

        result = position_snapshot(ib, "U1")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["quantity"], 5)

    def test_snapshot_posts_lovable_contract_with_bearer_secret(self):
        session = FakeSession()
        store = LovablePositionStore(DEFAULT_LOVABLE_POSITION_SYNC_URL, "secret", session=session)

        count = store.replace_snapshot("U1", [])

        url, kwargs = session.calls[0]
        self.assertEqual(url, DEFAULT_LOVABLE_POSITION_SYNC_URL)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(kwargs["headers"]["X-Position-Sync-Secret"], "secret")
        self.assertEqual(kwargs["json"], {"account_id": "U1", "connected": True, "positions": []})
        self.assertEqual(count, 0)

    def test_service_refreshes_positions_and_validates_managed_account(self):
        contract = SimpleNamespace(
            conId=123, symbol="PLTR", localSymbol="PLTR TEST", secType="OPT",
            currency="USD", lastTradeDateOrContractMonth="20260911",
            strike=175, right="P", multiplier="100",
        )
        position = SimpleNamespace(account="U1", contract=contract, position=5, avgCost=596)

        class FakeIB:
            def managedAccounts(self):
                return ["U1"]

            def reqPositions(self):
                return [position]

            def positions(self):
                return []

            def portfolio(self):
                return []

        class FakeStore:
            def __init__(self):
                self.positions = None

            def replace_snapshot(self, account_id, positions):
                self.positions = positions
                return len(positions)

        settings = SyncSettings("https://example.test", "secret", "U1")
        store = FakeStore()
        service = PositionSyncService(settings, store=store)
        service.ib = FakeIB()
        service.ib.isConnected = lambda: True

        self.assertEqual(service.sync_once(), 1)
        self.assertEqual(store.positions[0]["symbol"], "PLTR")

        service.settings = SyncSettings("https://example.test", "secret", "OTHER")
        with self.assertRaisesRegex(RuntimeError, "not managed"):
            service.sync_once()


if __name__ == "__main__":
    unittest.main()
