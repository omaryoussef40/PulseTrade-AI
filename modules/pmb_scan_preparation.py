"""Contracts between the read-only PMB preparation worker and order engine."""
from datetime import datetime
from hashlib import sha256
import json
import math
from pathlib import Path


PREPARATION_FILE = Path(__file__).resolve().parents[1] / "exports" / "pmb_scan_preparation.json"
CONSUMED_FILE = PREPARATION_FILE.with_name("pmb_prepared_entries.json")


def settings(config):
    return config.get("automation", {}).get("scan_preparation", {})


def enabled(config):
    return (bool(settings(config).get("enabled", False))
            and str(config.get("strategy", {}).get("active_strategy", "pmb")).lower() == "pmb"
            and not config.get("_entries_paused", False)
            and not (config.get("pine_stocks", {}).get("enabled") and not config.get("_concurrent_strategies")))


def validate_configuration(config):
    from strategies.pmb.candles import follow_through_enabled, confirmation_minutes
    confirmation = config.get("strategy", {}).get("breakout_follow_through", {})
    if not follow_through_enabled(confirmation) or confirmation_minutes(confirmation) != 5:
        raise ValueError("Prepared PMB scans require five-minute candle confirmation.")
    if config.get("staged_trading_timeline", {}).get("enabled"):
        raise ValueError("Prepared PMB scans require the fixed PMB timeline.")


def boundary(now):
    return now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)


def fingerprint(config, watchlist):
    # Include all inputs which can change a prepared signal or option selection.
    relevant = {key: config.get(key) for key in ("strategy", "risk", "option_filters", "strategy_control", "account_mode", "ib", "automation", "staged_trading_timeline")}
    relevant["watchlist"] = list(watchlist)
    return sha256(json.dumps(relevant, sort_keys=True, default=str).encode()).hexdigest()


def json_safe(value):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        return json_safe(value.item())
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def crossed_side(price, pdh, pdl):
    try:
        price, pdh, pdl = float(price), float(pdh), float(pdl)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) and v > 0 for v in (price, pdh, pdl)) or pdh < pdl:
        return None
    return "CALL" if price > pdh else "PUT" if price < pdl else None


def mandatory_confirmation(result, close_at):
    """A pre-close crossing alone can never authorize an entry."""
    side = crossed_side(result.get("Price"), result.get("PDH"), result.get("PDL"))
    if side not in {"CALL", "PUT"} or side != result.get("Signal"):
        return False
    if result.get("PDH Break" if side == "CALL" else "PDL Break") is not True:
        return False
    if result.get("Confirmation Candle Minutes") != 5:
        return False
    try:
        # PMB displays completed confirmation times in exchange-local minutes.
        raw = str(result.get("Confirmation Closed At") or result.get("ORB Confirmation Time") or "")
        confirmed = datetime.strptime(raw[:16], "%Y-%m-%d %H:%M").replace(tzinfo=close_at.tzinfo)
    except ValueError:
        return False
    return confirmed == close_at


def valid_snapshot(payload, config, watchlist, now):
    try:
        validate_configuration(config)
        expected = boundary(now)
        published = datetime.fromisoformat(payload["published_at"])
        return (
            enabled(config) and config.get("automation", {}).get("enabled")
            and payload["fingerprint"] == fingerprint(config, watchlist)
            and payload["candle_close"] == expected.isoformat()
            and payload.get("ready") is True
            and 0 <= (now - published).total_seconds() <= 15
            and 0 <= (now - expected).total_seconds() <= int(settings(config).get("confirmation_window_seconds", 90))
        )
    except (KeyError, TypeError, ValueError):
        return False


def read_snapshot(config, watchlist, now):
    try:
        payload = json.loads(PREPARATION_FILE.read_text())
    except (OSError, ValueError):
        return None
    return payload if valid_snapshot(payload, config, watchlist, now) else None


def write_snapshot(payload, path=PREPARATION_FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(json_safe(payload), allow_nan=False))
    temporary.replace(path)


def claim_entry(candle_close, symbol, side, path=CONSUMED_FILE):
    """One order/approval attempt per confirmed symbol per close, across retries.

    Only the single order engine calls this; the read-only worker never does.
    Persist before submission so an uncertain/rejected order is not duplicated
    when another prepared option becomes ready or the engine restarts.
    """
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        state = {}
    if state.get("candle_close") != candle_close:
        state = {"candle_close": candle_close, "attempted": []}
    key = f"{symbol}:{side}"
    if key in state["attempted"]:
        return False
    state["attempted"].append(key)
    write_snapshot(state, path)
    return True
