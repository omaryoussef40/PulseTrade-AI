from __future__ import annotations

import unittest
from datetime import date

import pandas as pd

from modules.ibkr_lovable_trades import LovableTradeStore, closed_trade_payloads


class FakeResponse:
    status_code = 200
    text = '{"ok":true,"imported":2,"updated":0}'

    def json(self):
        return {"ok": True, "imported": 2, "updated": 0}


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse()


class LovableTradeSyncTests(unittest.TestCase):
    def test_pairs_only_broker_fills_and_excludes_cancelled_local_entry(self):
        rows = pd.DataFrame([
            {
                "timestamp": "2026-09-01T09:52:01-04:00", "event": "ENTRY",
                "symbol": "AAPL", "signal": "CALL", "quantity": 10,
                "filled_quantity": 10, "entry_price": 4.66, "source": "IBKR_EXECUTION",
                "external_id": "BUY", "con_id": 917428558, "expiry": 20260909.0,
                "strike": 322.5, "sec_type": "OPT", "multiplier": 100,
            },
            {
                "timestamp": "2026-09-01T10:12:21-04:00", "event": "EXIT",
                "symbol": "AAPL", "signal": "CALL", "quantity": 10,
                "filled_quantity": 10, "exit_price": 5.55, "realized_pnl": 890,
                "source": "IBKR_EXECUTION", "external_id": "SELL", "con_id": 917428558,
                "expiry": 20260909.0, "strike": 322.5, "sec_type": "OPT", "multiplier": 100,
            },
            {
                "timestamp": "2026-09-01T10:16:19-04:00", "event": "ENTRY",
                "symbol": "AAPL", "signal": "CALL", "quantity": 5,
                "status": "Filled", "source": None,
            },
        ])

        trades = closed_trade_payloads(rows, date(2026, 9, 1), ["AAPL"])

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["external_id"], "CLOSED-SELL")
        self.assertEqual(trades[0]["entry_price"], 4.66)
        self.assertEqual(trades[0]["exit_price"], 5.55)
        self.assertEqual(trades[0]["realized_pnl"], 890)
        self.assertEqual(trades[0]["expiry"], "2026-09-09")
        self.assertEqual(trades[0]["entry_time"], "2026-09-01T13:52:01.000000Z")
        self.assertEqual(trades[0]["exit_time"], "2026-09-01T14:12:21.000000Z")

    def test_today_close_includes_overnight_entry_without_reusing_old_close(self):
        common = dict(symbol="NVDA", signal="CALL", quantity=1, filled_quantity=1,
                      con_id=123, source="IBKR_EXECUTION", multiplier=100)
        rows = pd.DataFrame([
            dict(common, timestamp="2026-09-10T10:00:00-04:00", event="ENTRY", entry_price=7, external_id="OLD-BUY"),
            dict(common, timestamp="2026-09-10T11:00:00-04:00", event="EXIT", exit_price=8, external_id="OLD-SELL"),
            dict(common, timestamp="2026-09-11T11:57:12-04:00", event="ENTRY", entry_price=8.1, external_id="BUY"),
            dict(common, timestamp="2026-09-21T11:03:02-04:00", event="EXIT", exit_price=9.69, external_id="SELL"),
        ])
        trades = closed_trade_payloads(rows, date(2026, 9, 21))
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["external_id"], "CLOSED-SELL")
        self.assertEqual(trades[0]["entry_price"], 8.1)
        self.assertEqual(trades[0]["entry_time"], "2026-09-11T15:57:12.000000Z")

    def test_posts_idempotent_payload_with_both_supported_headers(self):
        session = FakeSession()
        store = LovableTradeStore("https://thedesk.dev/api/public/ibkr/trades", "secret", session=session)
        trades = [{"external_id": "one"}]

        result = store.upsert("U1", trades)

        url, request = session.calls[0]
        self.assertEqual(url, "https://thedesk.dev/api/public/ibkr/trades")
        self.assertEqual(request["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(request["headers"]["X-Position-Sync-Secret"], "secret")
        self.assertEqual(request["json"], {"account_id": "U1", "trades": trades})
        self.assertEqual(result["imported"], 2)


if __name__ == "__main__":
    unittest.main()
