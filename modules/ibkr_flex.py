from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests


FLEX_BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
USER_AGENT = "PulseTrade-AI Flex Sync"


def _first_text(root: ET.Element, names: list[str]) -> str:
    lowered = {name.lower() for name in names}
    for elem in root.iter():
        if elem.tag.lower() in lowered and elem.text:
            return elem.text.strip()
    return ""


def _node_value(node: ET.Element, names: list[str]) -> str:
    lowered = {name.lower() for name in names}
    for key, value in node.attrib.items():
        if key.lower() in lowered:
            return str(value or "").strip()
    for child in node:
        if child.tag.lower() in lowered and child.text:
            return child.text.strip()
    return ""


def _number(value, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", ""))
    except Exception:
        return default


def _timestamp(row: ET.Element) -> str:
    raw_datetime = _node_value(row, ["dateTime", "date_time", "tradeDateTime", "transactionDateTime"])
    if raw_datetime:
        parsed = pd.to_datetime(raw_datetime, errors="coerce")
        if pd.notna(parsed):
            return parsed.isoformat()

    raw_date = _node_value(row, ["tradeDate", "date", "transactionDate"])
    raw_time = _node_value(row, ["tradeTime", "time", "transactionTime"])
    combined = f"{raw_date} {raw_time}".strip()
    parsed = pd.to_datetime(combined, errors="coerce")
    if pd.notna(parsed):
        return parsed.isoformat()
    return datetime.now().isoformat()


def _option_side(row: ET.Element) -> str:
    put_call = _node_value(row, ["putCall", "put_call", "right"]).upper()
    buy_sell = _node_value(row, ["buySell", "side", "transactionType"]).upper()
    if put_call.startswith("C"):
        return "CALL"
    if put_call.startswith("P"):
        return "PUT"
    if "CALL" in buy_sell:
        return "CALL"
    if "PUT" in buy_sell:
        return "PUT"
    return ""


def _event(row: ET.Element) -> str:
    buy_sell = _node_value(row, ["buySell", "side", "transactionType"]).upper()
    if buy_sell in {"BUY", "BOT", "B"} or "BUY" in buy_sell:
        return "ENTRY"
    if buy_sell in {"SELL", "SLD", "S"} or "SELL" in buy_sell:
        return "EXIT"
    return "TRADE"


def send_flex_request(token: str, query_id: str, base_url: str = FLEX_BASE_URL) -> str:
    response = requests.get(
        f"{base_url}/SendRequest",
        params={"t": token, "q": query_id, "v": "3"},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    root = ET.fromstring(response.text)
    status = _first_text(root, ["Status"])
    if status and status.lower() != "success":
        message = _first_text(root, ["ErrorMessage", "Message"]) or response.text[:400]
        raise RuntimeError(f"Flex SendRequest failed: {message}")
    reference_code = _first_text(root, ["ReferenceCode", "Reference"])
    if not reference_code:
        raise RuntimeError("Flex SendRequest did not return a reference code.")
    return reference_code


def get_flex_statement(token: str, reference_code: str, base_url: str = FLEX_BASE_URL, retries: int = 5) -> str:
    last_error = ""
    for attempt in range(max(1, int(retries))):
        response = requests.get(
            f"{base_url}/GetStatement",
            params={"t": token, "q": reference_code, "v": "3"},
            headers={"User-Agent": USER_AGENT},
            timeout=60,
        )
        response.raise_for_status()
        text = response.text
        root = ET.fromstring(text)
        status = _first_text(root, ["Status"])
        if not status or status.lower() == "success":
            return text
        last_error = _first_text(root, ["ErrorMessage", "Message"]) or text[:400]
        time.sleep(min(2 + attempt, 8))
    raise RuntimeError(f"Flex GetStatement failed: {last_error or 'statement was not ready'}")


def parse_trade_confirmations(xml_text: str) -> pd.DataFrame:
    root = ET.fromstring(xml_text)
    nodes = [
        elem for elem in root.iter()
        if elem.tag.lower() in {"trade", "tradeconfirm", "tradeconfirmation", "transaction"}
    ]
    rows = []
    for node in nodes:
        symbol = _node_value(node, ["symbol", "underlyingSymbol", "ticker"])
        description = _node_value(node, ["description", "ibDescription", "assetDescription"])
        quantity = abs(_number(_node_value(node, ["quantity", "qty", "shares"])))
        price = _number(_node_value(node, ["price", "tradePrice"]))
        event = _event(node)
        signal = _option_side(node)
        expiry = _node_value(node, ["expiry", "expiryDate", "maturity"])
        strike = _node_value(node, ["strike", "strikePrice"])
        external_id = _node_value(node, ["tradeID", "tradeId", "execID", "executionID", "transactionID", "ibExecID"])
        realized_pnl = _number(_node_value(node, ["realizedPnl", "realizedPNL", "fifoPnl", "mtmPnl"]))
        commission = _number(_node_value(node, ["commission", "ibCommission"]))
        con_id = _node_value(node, ["conid", "conId", "contractId"])

        option = description or " ".join(x for x in [symbol, expiry, str(strike), signal] if x)
        row = {
            "timestamp": _timestamp(node),
            "event": event,
            "source": "IBKR_FLEX",
            "external_id": external_id or f"FLEX-{_timestamp(node)}-{symbol}-{event}-{quantity}-{price}",
            "symbol": symbol,
            "signal": signal,
            "option": option,
            "quantity": int(quantity) if quantity.is_integer() else quantity,
            "entry_price": price if event == "ENTRY" else None,
            "exit_price": price if event == "EXIT" else None,
            "realized_pnl": realized_pnl if event == "EXIT" else 0.0,
            "commission": commission,
            "status": "CONFIRMED",
            "con_id": con_id,
        }
        if symbol or description:
            rows.append(row)
    return pd.DataFrame(rows)


def merge_trade_rows(trades: pd.DataFrame, trade_log_file: str | Path) -> int:
    if trades.empty:
        return 0
    path = Path(trade_log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = pd.read_csv(path) if path.exists() else pd.DataFrame()
    existing_ids = set()
    if not existing.empty and "external_id" in existing.columns:
        existing_ids = set(existing["external_id"].dropna().astype(str).tolist())
    new_rows = trades[~trades["external_id"].astype(str).isin(existing_ids)].copy()
    if new_rows.empty:
        return 0
    combined = pd.concat([existing, new_rows], ignore_index=True, sort=False) if not existing.empty else new_rows
    if "timestamp" in combined.columns:
        combined["timestamp"] = pd.to_datetime(combined["timestamp"], errors="coerce")
        combined = combined.dropna(subset=["timestamp"]).sort_values("timestamp")
    combined.to_csv(path, index=False)
    return len(new_rows)


def sync_flex_trades_to_trade_log(config: dict, trade_log_file: str | Path) -> tuple[int, str]:
    flex = config.get("ibkr_flex", {}) if isinstance(config, dict) else {}
    token = os.getenv("IBKR_FLEX_TOKEN") or flex.get("token", "")
    query_id = os.getenv("IBKR_FLEX_TRADE_QUERY_ID") or flex.get("trade_query_id", "")
    base_url = flex.get("base_url") or FLEX_BASE_URL
    if not token or not query_id:
        return 0, "IBKR Flex token/query ID missing."

    reference_code = send_flex_request(token, query_id, base_url)
    xml_text = get_flex_statement(token, reference_code, base_url)
    trades = parse_trade_confirmations(xml_text)
    imported = merge_trade_rows(trades, trade_log_file)
    return imported, f"Flex rows parsed={len(trades)} imported={imported}"
