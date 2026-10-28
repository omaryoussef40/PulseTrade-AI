"""Read-only persistent stock streams and early option discovery for PMB.

No order, position, or trade-journal writes occur in this process. Only the
engine owns trading decisions and order submission.
"""
import argparse
import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .pmb_scan_preparation import (
    boundary, crossed_side, enabled, fingerprint, json_safe,
    mandatory_confirmation, settings, validate_configuration, write_snapshot,
)


class OptionPreparation:
    """Separate read-only lookups cannot stall stock streaming at the close."""
    def __init__(self, config):
        self.config = config
        self.running = {}
        self.options = {}
        self.attempted = {}

    def poll(self, now):
        for slot, (process, key, started, target) in list(self.running.items()):
            if process.poll() is None and (now - started).total_seconds() <= 150:
                continue
            if process.poll() is None:
                process.kill()
            stdout, _ = process.communicate(timeout=5)
            try:
                option = json.loads(stdout).get("option")
            except (ValueError, AttributeError):
                option = None
            if option:
                self.options[key] = {"option": option, "prepared_at": now.isoformat(), "target": target}
            self.running.pop(slot, None)

    def prepare(self, symbol, side, price, target, now):
        key = f"{symbol}:{side}"
        attempt_key = (key, target)
        previous = self.attempted.get(attempt_key)
        if (previous and (now - previous).total_seconds() < 30) or any(job[1] == key for job in self.running.values()):
            return
        if self.options.get(key, {}).get("target") == target:
            return
        slots = max(1, min(2, int(settings(self.config).get("option_workers", 2))))
        slot = next((s for s in range(slots) if s not in self.running), None)
        if slot is None:
            return
        from bot_core import ib_port_from_config
        ib_cfg = self.config.get("ib", {})
        payload = {
            "ib": {"host": ib_cfg.get("host", "127.0.0.1"),
                   "port": ib_port_from_config(self.config),
                   "client_id": int(ib_cfg.get("client_id", 11)) + 2101 + slot,
                   "account": ib_cfg.get("account") or None, "readonly": True},
            "symbol": symbol, "signal": side, "price": price,
            "dte": int(self.config.get("strategy", {}).get("option_dte", 7)),
            "filters": dict(self.config.get("option_filters", {})),
            "prepare_only": True,
        }
        # Bound discovery work so a few illiquid contracts cannot occupy the
        # entire preparation window. These limits never relax quality filters.
        for name, cap in (("max_contract_qualification_checks", 12),
                          ("max_market_data_checks", 6), ("max_contract_checks", 6)):
            payload["filters"][name] = max(1, min(cap, int(payload["filters"].get(name, cap) or cap)))
        process = subprocess.Popen(
            [sys.executable, "-m", "modules.scanner_option_worker"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        process.stdin.write(json.dumps(payload))
        process.stdin.close()
        process.stdin = None
        self.running[slot] = (process, key, now, target)
        self.attempted[attempt_key] = now

    def get(self, symbol, side, now):
        record = self.options.get(f"{symbol}:{side}")
        if record and 0 <= (now - datetime.fromisoformat(record["prepared_at"])).total_seconds() <= 600:
            return record["option"]
        return None

    def close(self):
        for process, *_ in self.running.values():
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        self.running.clear()


def previous_levels(daily, now):
    if daily.empty:
        return None, None
    prior = daily[daily.index.date < now.date()]
    if prior.empty:
        return None, None
    return float(prior.iloc[-1]["High"]), float(prior.iloc[-1]["Low"])


def live_crossing(ticker, pdh, pdl, now):
    timestamp = getattr(ticker, "time", None)
    if timestamp is None or timestamp.tzinfo is None or getattr(ticker, "marketDataType", None) != 1:
        return None, None
    if not 0 <= (now - timestamp).total_seconds() <= 15:
        return None, None
    from math import isfinite
    price = getattr(ticker, "last", None)
    if price is None or not isfinite(price) or price <= 0:
        bid, ask = getattr(ticker, "bid", None), getattr(ticker, "ask", None)
        if bid is None or ask is None or not all(isfinite(v) and v > 0 for v in (bid, ask)) or ask < bid:
            return None, None
        price = (bid + ask) / 2
    return crossed_side(price, pdh, pdl), price


def scan_completed_symbol(ib, symbol, frame, daily, close_at, strategy):
    from bot_core import clean_for_table, scan_symbol_ib
    expected_start = close_at - timedelta(minutes=5)
    pdh, pdl = previous_levels(daily, close_at)
    if expected_start not in frame.index:
        raise ValueError("Latest completed five-minute stock candle unavailable")
    if crossed_side(frame.loc[expected_start, "Close"], pdh, pdl) is None:
        return None
    result = scan_symbol_ib(ib, symbol,
        bool(strategy.get("use_rvol_score", False)), "pmb",
        int(strategy.get("orb_minutes", 15)), int(strategy.get("min_session_bars", 7)),
        require_retest=bool(strategy.get("require_break_retest", False)),
        retest_tolerance_pct=float(strategy.get("retest_tolerance_pct", .10)),
        retest_max_minutes=int(strategy.get("retest_max_minutes", 45)),
        intraday_bar_size="5 mins", analysis_bar_minutes=15,
        breakout_follow_through=dict(strategy.get("breakout_follow_through") or {}),
        prepared_data=(frame, daily), as_of=close_at)
    return clean_for_table(result) if result else None


def run(parent_pid):
    from bot_core import (EASTERN, IBConfig, connect_ib, ib_port_from_config,
                          is_market_open_now, load_config)
    from market_data import MarketDataConfig, IBKRMarketDataProvider
    from .premarket_watchlist import combined_watchlist, should_auto_build, build_premarket_watchlist
    from .strategy_controls import effective_config
    config = effective_config(load_config())
    validate_configuration(config)
    watchlist = combined_watchlist(config)
    signature = fingerprint(config, watchlist)
    ib_cfg = config.get("ib", {})
    connection = IBConfig(host=ib_cfg.get("host", "127.0.0.1"),
        port=ib_port_from_config(config), client_id=int(ib_cfg.get("client_id", 11)) + 2100,
        account=ib_cfg.get("account") or None, readonly=True)
    ib = connect_ib(connection)
    provider = IBKRMarketDataProvider(MarketDataConfig(timezone=EASTERN), ib=ib)
    options = OptionPreparation(config)
    streams, errors, updates = {}, {}, {}
    session_date = datetime.now(EASTERN).date()

    async def seed():
        semaphore = asyncio.Semaphore(3)
        async def one(symbol):
            async with semaphore:
                try:
                    bars, daily, ticker = await provider.subscribe_prepared_stock(symbol)
                    streams[symbol] = (bars, daily, ticker)
                    updates[symbol] = datetime.now(EASTERN)
                    bars.updateEvent += lambda *_args, symbol=symbol: updates.update({symbol: datetime.now(EASTERN)})
                except Exception as exc:
                    errors[symbol] = str(exc)
        await asyncio.gather(*(one(symbol) for symbol in watchlist))

    try:
        if should_auto_build(config):
            build_premarket_watchlist(config, ib)
            watchlist = combined_watchlist(config)
            signature = fingerprint(config, watchlist)
        ib.run(seed())
        if not streams:
            raise RuntimeError("No live stock streams available; prepared entries remain disabled")
        finalized_at, results, pending = None, {}, list(watchlist)
        while True:
            os.kill(parent_pid, 0)
            now = datetime.now(EASTERN)
            current_config = effective_config(load_config())
            if not enabled(current_config) or not current_config.get("automation", {}).get("enabled") or fingerprint(current_config, combined_watchlist(current_config)) != signature or now.date() != session_date:
                return
            if not ib.isConnected():
                return
            if not is_market_open_now(config):
                ib.sleep(2)
                continue
            options.poll(now)
            close_at = boundary(now)
            next_close = close_at + timedelta(minutes=5)
            before_close = (next_close - now).total_seconds()
            lead = max(1, min(240, int(settings(config).get("lead_seconds", 180))))
            window = int(settings(config).get("confirmation_window_seconds", 90))
            shortlist = []
            for symbol, (_, daily, ticker) in streams.items():
                pdh, pdl = previous_levels(daily, now)
                side, price = live_crossing(ticker, pdh, pdl, now)
                if side:
                    shortlist.append({"symbol": symbol, "signal": side, "price": price})
                    if before_close <= lead:
                        options.prepare(symbol, side, price, next_close.isoformat(), now)
            if finalized_at != close_at:
                finalized_at, results = close_at, {}
            age = (now - close_at).total_seconds()
            if 2 <= age <= window:
                pending = []
                for symbol, (bars, daily, _) in streams.items():
                    if symbol in results:
                        continue
                    frame = provider._bars_to_ohlcv(bars)
                    expected_start = close_at - timedelta(minutes=5)
                    if expected_start not in frame.index or updates[symbol] < close_at:
                        pending.append(symbol)
                        continue
                    try:
                        strategy = config.get("strategy", {})
                        results[symbol] = scan_completed_symbol(ib, symbol, frame, daily, close_at, strategy)
                    except Exception as exc:
                        errors[symbol] = str(exc)
                        pending.append(symbol)
                # A late crossing is admitted from its completed candle, even
                # if it was absent from the earlier quote-based shortlist.
                for symbol, result in results.items():
                    if result and mandatory_confirmation(result, close_at):
                        options.prepare(symbol, result["Signal"], result["Price"], close_at.isoformat(), now)
            prepared_options = {}
            for symbol, result in results.items():
                if result and mandatory_confirmation(result, close_at):
                    option = options.get(symbol, result["Signal"], now)
                    if option:
                        prepared_options[symbol] = option
            content = json_safe({"results": results, "options": prepared_options})
            from hashlib import sha256
            generation = sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
            write_snapshot({**content, "fingerprint": signature, "generation": generation,
                "published_at": now.isoformat(), "candle_close": close_at.isoformat(),
                "ready": age >= 2 and bool(streams), "shortlist": shortlist,
                "pending_symbols": pending, "errors": errors})
            ib.sleep(1)
    finally:
        options.close()
        ib.disconnect()


def main():
    import signal
    # Let run() clean up its option children and IBKR connection on shutdown.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()
    while True:
        try:
            os.kill(args.parent_pid, 0)
            from bot_core import EASTERN, app_log, is_market_open_now, load_config
            from .strategy_controls import effective_config
            cfg = effective_config(load_config())
            if not enabled(cfg):
                return
            if cfg.get("automation", {}).get("enabled") and is_market_open_now(cfg):
                run(args.parent_pid)
            time.sleep(2)
        except ProcessLookupError:
            return
        except Exception as exc:
            from bot_core import app_log
            app_log(f"PMB preparation worker: {exc}", "WARN")
            time.sleep(10)


if __name__ == "__main__":
    main()
