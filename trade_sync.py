from __future__ import annotations

import argparse
import os
from datetime import datetime

from dotenv import load_dotenv
from pytz import timezone

from bot_core import TRADE_LOG_FILE
from modules.ibkr_lovable_trades import (
    DEFAULT_LOVABLE_TRADE_SYNC_URL,
    LovableTradeStore,
    load_closed_trade_payloads,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Push closed IBKR trades to Lovable Cloud.")
    parser.add_argument("--date", help="ET trade date in YYYY-MM-DD; defaults to today.")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols to publish.")
    parser.add_argument("--dry-run", action="store_true", help="Build the payload without posting it.")
    args = parser.parse_args()

    load_dotenv(".env.position-sync")
    load_dotenv()
    secret = os.getenv("IBKR_POSITION_SYNC_SECRET", "")
    account_id = os.getenv("IBKR_SYNC_ACCOUNT_ID", "")
    if not secret or not account_id:
        print("Trade sync configuration error: IBKR_POSITION_SYNC_SECRET and IBKR_SYNC_ACCOUNT_ID are required")
        return 2
    try:
        trade_date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.now(timezone("America/New_York")).date()
    except ValueError:
        print("Trade sync configuration error: --date must use YYYY-MM-DD")
        return 2
    trades = load_closed_trade_payloads(TRADE_LOG_FILE, trade_date, args.symbols)
    if not trades:
        print(f"No closed IBKR trades found for {trade_date}")
        return 0
    if args.dry_run:
        print(f"Trade payload ready | date={trade_date} | trades={len(trades)}")
        for trade in trades:
            print(f"{trade['symbol']} | qty={trade['quantity']:g} | pnl={trade['realized_pnl']}")
        return 0
    url = os.getenv("IBKR_TRADE_SYNC_URL", DEFAULT_LOVABLE_TRADE_SYNC_URL)
    try:
        result = LovableTradeStore(url, secret).upsert(account_id, trades)
    except Exception as exc:
        print(f"Trade sync failed | {type(exc).__name__}: {exc}")
        return 1
    print(
        "Trade sync complete"
        f" | sent={len(trades)}"
        f" | imported={int(result.get('imported', 0) or 0)}"
        f" | updated={int(result.get('updated', 0) or 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
