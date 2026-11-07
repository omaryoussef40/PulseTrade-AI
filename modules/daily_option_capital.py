"""Persist the first verified available-funds balance for each New York day."""
from datetime import datetime
from math import isfinite
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

STATE_FILE = Path(__file__).resolve().parents[1] / "data" / "daily_option_capital.sqlite"
EASTERN = ZoneInfo("America/New_York")


def saved_starting_capital(account, *, now=None, state_file=None):
    """Read the baseline for display without capturing or changing it."""
    path = Path(state_file or STATE_FILE)
    if not path.exists() or not account:
        return None
    day = (now or datetime.now(EASTERN)).astimezone(EASTERN).date().isoformat()
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as db:
        row = db.execute("SELECT amount FROM capital WHERE account=? AND day=?", (account, day)).fetchone()
    return float(row[0]) if row else None


def daily_option_capital(ib, account=None, *, now=None, state_file=None):
    """Return (day's starting funds, current funds); missing funds block entries.

    Capture occurs at the first option cycle/approval of the day, not at midnight.
    SQLite keeps the baseline stable across engine restarts and concurrent readers.
    """
    accounts = ib.managedAccounts()
    account = account or (accounts[0] if len(accounts) == 1 else None)
    if not account or account not in accounts:
        raise ValueError("Select one connected IBKR account for daily available capital.")
    try:
        values = [float(row.value) for row in ib.accountSummary()
                  if row.account == account and row.tag == "AvailableFunds" and row.currency == "USD"]
    except (TypeError, ValueError) as exc:
        raise ValueError("Verified USD available funds unavailable; option entries are blocked.") from exc
    if len(values) != 1 or not isfinite(values[0]):
        raise ValueError("Verified USD available funds unavailable; option entries are blocked.")
    current = max(values[0], 0.0)
    day = (now or datetime.now(EASTERN)).astimezone(EASTERN).date().isoformat()
    path = Path(state_file or STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS capital (account TEXT, day TEXT, amount REAL, PRIMARY KEY (account, day))")
        db.execute("INSERT OR IGNORE INTO capital VALUES (?, ?, ?)", (account, day, current))
        starting = db.execute("SELECT amount FROM capital WHERE account=? AND day=?", (account, day)).fetchone()[0]
    return float(starting), current
