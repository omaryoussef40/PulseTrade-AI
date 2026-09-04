# Lovable Cloud Live IBKR Positions

This service pushes a complete live position snapshot from the locally running
IBKR Gateway/TWS session to the secured Lovable Cloud endpoint. The browser
never connects to IBKR and never receives the shared sync secret.

## Configure the local secret

Lovable Cloud must contain a secret named `IBKR_POSITION_SYNC_SECRET`. Put the
same value in the ignored local file `.env.position-sync`:

```dotenv
IBKR_POSITION_SYNC_URL=https://profit-buddy-portal.lovable.app/api/public/ibkr/positions
IBKR_POSITION_SYNC_SECRET=...
IBKR_SYNC_ACCOUNT_ID=...
IBKR_SYNC_CLIENT_ID=311
IBKR_SYNC_INTERVAL_SECONDS=20
```

The repository ignores `.env.position-sync`. Do not put the secret in frontend
code, source control, screenshots, logs, or chat.

## Test one snapshot

Publish the Lovable project first. With IBKR Gateway/TWS running and API socket
access enabled, run:

```bash
./.venv/bin/python position_sync.py --once
```

Expected output:

```text
Position snapshot synced | positions=1
```

A successful account with no open positions reports `positions=0`. A 401 means
the local secret does not exactly match the Lovable Cloud secret. A 404 usually
means the Lovable project has not yet been published with the endpoint.

## Run continuously

```bash
./.venv/bin/python position_sync.py
```

The service sends a complete snapshot every 20 seconds. Lovable removes rows
that are absent from a successful snapshot. If IBKR or Lovable is unavailable,
the local process does not send an empty snapshot; the last good data remains
and the Lovable health indicator becomes stale.
