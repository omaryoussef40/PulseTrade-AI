from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from bot_core import IBConfig, connect_ib, ib_port_from_config, load_config


DEFAULT_LOVABLE_POSITION_SYNC_URL = "https://profit-buddy-portal.lovable.app/api/public/ibkr/positions"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and abs(result) < 1e100 else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _expiry(value: Any) -> str | None:
    raw = _text(value).replace("-", "")
    if len(raw) >= 8 and raw[:8].isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return _text(value) or None


def contract_key(contract: Any) -> str:
    con_id = _integer(getattr(contract, "conId", None))
    if con_id:
        return f"conid:{con_id}"
    parts = [
        _text(getattr(contract, "secType", None)).upper(),
        _text(getattr(contract, "symbol", None)).upper(),
        _text(getattr(contract, "lastTradeDateOrContractMonth", None)),
        str(_number(getattr(contract, "strike", None)) or ""),
        _text(getattr(contract, "right", None)).upper(),
        _text(getattr(contract, "currency", None)).upper(),
    ]
    return "contract:" + ":".join(parts)


def position_snapshot(
    ib: Any,
    account_id: str,
    broker_positions: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Build Lovable's position payload, using IBKR positions as authority."""
    portfolio_by_key: dict[tuple[str, str], Any] = {}
    for item in list(ib.portfolio() or []):
        contract = getattr(item, "contract", None)
        item_account = _text(getattr(item, "account", None))
        if contract is not None:
            portfolio_by_key[(item_account, contract_key(contract))] = item

    position_items = ib.positions() if broker_positions is None else broker_positions
    unique_positions: dict[tuple[str, str], Any] = {}
    for item in list(position_items or []):
        item_account = _text(getattr(item, "account", None))
        if item_account != account_id:
            continue
        quantity = _number(getattr(item, "position", None))
        contract = getattr(item, "contract", None)
        if contract is None or quantity in (None, 0):
            continue
        unique_positions[(item_account, contract_key(contract))] = item

    rows: list[dict[str, Any]] = []
    for item in unique_positions.values():
        item_account = _text(getattr(item, "account", None))
        quantity = _number(getattr(item, "position", None))
        contract = getattr(item, "contract", None)
        key = contract_key(contract)
        portfolio = portfolio_by_key.get((item_account, key))
        symbol = _text(getattr(contract, "symbol", None)).upper()
        asset_class = _text(getattr(contract, "secType", None)).upper()
        multiplier = _number(getattr(contract, "multiplier", None))
        strike = _number(getattr(contract, "strike", None))
        if asset_class not in {"OPT", "FOP"} and strike == 0:
            strike = None
        average_cost = _number(
            getattr(portfolio, "averageCost", None)
            if portfolio is not None
            else getattr(item, "avgCost", None)
        )
        if average_cost is not None and asset_class in {"OPT", "FOP"} and multiplier:
            average_cost /= multiplier
        rows.append({
            "conid": str(_integer(getattr(contract, "conId", None)) or key),
            "symbol": symbol,
            "description": _text(getattr(contract, "localSymbol", None)) or symbol,
            "asset_class": asset_class,
            "underlying_symbol": symbol,
            "strike": strike,
            "expiry": _expiry(getattr(contract, "lastTradeDateOrContractMonth", None)),
            "option_type": _text(getattr(contract, "right", None)).upper() or None,
            "multiplier": multiplier,
            "quantity": quantity,
            "avg_cost": average_cost,
            "mark_price": _number(getattr(portfolio, "marketPrice", None)),
            "market_value": _number(getattr(portfolio, "marketValue", None)),
            "unrealized_pnl": _number(getattr(portfolio, "unrealizedPNL", None)),
            "currency": _text(getattr(contract, "currency", None)).upper() or None,
        })
    return sorted(rows, key=lambda row: (row["symbol"], row["conid"]))


@dataclass(frozen=True)
class SyncSettings:
    lovable_url: str
    position_sync_secret: str
    account_id: str
    interval_seconds: float = 20.0
    client_id: int = 311
    config_path: str = "config.json"

    @classmethod
    def from_env(cls) -> "SyncSettings":
        secret = os.getenv("IBKR_POSITION_SYNC_SECRET", "")
        account_id = os.getenv("IBKR_SYNC_ACCOUNT_ID", "")
        missing = []
        if not secret:
            missing.append("IBKR_POSITION_SYNC_SECRET")
        if not account_id:
            missing.append("IBKR_SYNC_ACCOUNT_ID")
        if missing:
            raise ValueError("Missing required environment variables: " + ", ".join(missing))
        return cls(
            lovable_url=os.getenv("IBKR_POSITION_SYNC_URL", DEFAULT_LOVABLE_POSITION_SYNC_URL).rstrip("/"),
            position_sync_secret=secret,
            account_id=account_id,
            interval_seconds=max(5.0, float(os.getenv("IBKR_SYNC_INTERVAL_SECONDS", "20"))),
            client_id=int(os.getenv("IBKR_SYNC_CLIENT_ID", "311")),
            config_path=os.getenv("IBKR_SYNC_CONFIG_PATH", "config.json"),
        )


class LovablePositionStore:
    def __init__(self, url: str, secret: str, timeout: float = 15.0, session: Any = None):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.headers = {
            "Authorization": f"Bearer {secret}",
            "X-Position-Sync-Secret": secret,
            "Content-Type": "application/json",
        }

    def replace_snapshot(self, account_id: str, positions: list[dict[str, Any]]) -> int:
        response = self.session.post(
            self.url,
            headers=self.headers,
            json={
                "account_id": account_id,
                "connected": True,
                "positions": positions,
            },
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            detail = _text(getattr(response, "text", ""))[:500]
            raise RuntimeError(f"Lovable position sync failed ({response.status_code}): {detail}")
        try:
            body = response.json()
        except Exception:
            body = {}
        if body and body.get("ok") is False:
            raise RuntimeError(f"Lovable position sync rejected snapshot: {_text(body)[:500]}")
        return int(body.get("positions", len(positions))) if isinstance(body, dict) else len(positions)


class PositionSyncService:
    def __init__(self, settings: SyncSettings, store: LovablePositionStore | None = None):
        self.settings = settings
        self.store = store or LovablePositionStore(
            settings.lovable_url,
            settings.position_sync_secret,
        )
        self.ib = None

    def _connect(self) -> Any:
        if self.ib is not None and self.ib.isConnected():
            return self.ib
        config = load_config(Path(self.settings.config_path))
        ib_config = config.get("ib", {})
        self.ib = connect_ib(IBConfig(
            host=_text(ib_config.get("host")) or "127.0.0.1",
            port=ib_port_from_config(config),
            client_id=self.settings.client_id,
            account=self.settings.account_id,
            readonly=True,
        ))
        self.ib.sleep(2)
        return self.ib

    def sync_once(self) -> int:
        ib = self._connect()
        managed_accounts = [_text(account) for account in list(ib.managedAccounts() or [])]
        if self.settings.account_id not in managed_accounts:
            raise RuntimeError(
                f"Configured account {self.settings.account_id} is not managed by this IBKR session "
                f"(managed_accounts={managed_accounts})"
            )
        # reqPositions waits for IBKR's positionEnd callback. Reading the cached
        # positions list alone can remain empty after a Gateway reconnect.
        broker_positions = list(ib.reqPositions() or [])
        positions = position_snapshot(ib, self.settings.account_id, broker_positions)
        print(
            "IBKR snapshot ready | "
            f"managed_accounts={','.join(managed_accounts)} | "
            f"account={self.settings.account_id} | local_positions={len(positions)}",
            flush=True,
        )
        return self.store.replace_snapshot(self.settings.account_id, positions)

    def close(self) -> None:
        if self.ib is not None:
            try:
                self.ib.disconnect()
            except Exception:
                pass

    def run_forever(self) -> None:
        try:
            while True:
                started = time.monotonic()
                try:
                    count = self.sync_once()
                    print(f"Position snapshot synced | positions={count}", flush=True)
                except Exception as exc:
                    print(f"Position snapshot failed | {type(exc).__name__}: {exc}", flush=True)
                    # Do not send an empty snapshot on failure: Lovable correctly
                    # interprets missing positions in a valid snapshot as closed.
                    self.close()
                    self.ib = None
                elapsed = time.monotonic() - started
                time.sleep(max(1.0, self.settings.interval_seconds - elapsed))
        finally:
            self.close()
