from __future__ import annotations

import argparse

from dotenv import load_dotenv

from modules.ibkr_lovable_sync import PositionSyncService, SyncSettings


def main() -> int:
    parser = argparse.ArgumentParser(description="Push live IBKR positions to Supabase.")
    parser.add_argument("--once", action="store_true", help="Sync one snapshot and exit.")
    args = parser.parse_args()

    load_dotenv(".env.position-sync")
    load_dotenv()
    try:
        settings = SyncSettings.from_env()
    except (TypeError, ValueError) as exc:
        print(f"Position sync configuration error: {exc}")
        return 2
    service = PositionSyncService(settings)
    if not args.once:
        service.run_forever()
        return 0
    try:
        count = service.sync_once()
        print(f"Position snapshot synced | positions={count}")
        return 0
    except Exception as exc:
        print(f"Position snapshot failed | {type(exc).__name__}: {exc}")
        return 1
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
