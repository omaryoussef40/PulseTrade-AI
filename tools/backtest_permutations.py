"""Frozen-data PMB parameter sweep using the existing Strategy Lab simulator.

Scan each interval/ORB once, then apply the independent confirmation gates to
all qualifying stock observations before ranking. This is equivalent to the
shared strategy because those gates only remove signals, not change scores.
All output lives in a separate directory; live config and Lab results are not
changed. IBKR connections are read-only and only historical data is requested.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import fields, replace, asdict
from itertools import product
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import backtester.controller as controller_module
from backtester.controller import StrategyLabController, StrategyLabSettings, _select_live_style_signals
from providers.ibkr_provider import IBKRMarketDataProvider, IBKRProviderConfig
from strategies.pmb.strategy import evaluate_breakout_follow_through, _bar_minutes


class ResearchProvider(IBKRMarketDataProvider):
    def _prune_old_cache_files(self):
        # Research runs preserve existing caches.
        pass

    def historical_option_bars(self, *args, **kwargs):
        key = tuple(str(value) for value in args) + tuple(sorted((key, str(value)) for key, value in kwargs.items()))
        if key not in self.memo:
            try:
                self.memo[key] = super().historical_option_bars(*args, **kwargs)
            except Exception as exc:
                self.memo[key] = str(exc)
        value = self.memo[key]
        if isinstance(value, str):
            raise RuntimeError(value)
        return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    snapshot = output / "baseline_meta.json"
    if not snapshot.exists():
        snapshot.write_text((ROOT / "backtester/exports/strategy_lab_meta.json").read_text())
    meta = json.loads(snapshot.read_text())
    valid = {field.name for field in fields(StrategyLabSettings)}
    baseline = StrategyLabSettings(**{key: value for key, value in meta["settings"].items() if key in valid})
    baseline = replace(baseline, period="14d", selected_strategies=("PMB",), use_staged_timeline=False)
    config = json.loads((ROOT / "config.json").read_text())
    ib = config.get("ib", {})
    port = ib.get("live_port", 7496) if config.get("account_mode") == "Live" else ib.get("paper_port", 7497)
    provider = ResearchProvider(IBKRProviderConfig(host=ib.get("host", "127.0.0.1"), port=int(port), client_id=1967, readonly=True))
    provider.memo = {}
    lab = StrategyLabController(export_dir=output, data_provider=provider)
    start = pd.Timestamp(meta["data_start"])
    end_date = pd.Timestamp(meta["data_end"]).date()
    manifest = {"baseline_settings": asdict(baseline), "start": start.isoformat(), "end_date": str(end_date),
                "combinations": 64, "ranking_excludes": "5-minute ORB with 15-minute candles: engine rounds to one 15-minute bar",
                "option_data": "Existing IBKR cache plus read-only historical requests; unavailable contracts remain rejected",
                "extension_note": "Extension ON/OFF is equivalent when next-candle confirmation is OFF"}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    data_by_interval = {}
    coverage = []
    for interval in ("5m", "15m"):
        data = {}
        for symbol in baseline.symbols:
            path = ROOT / f"backtester/cache/ibkr/stocks/{symbol}_{interval}_rth_raw.parquet"
            frame = pd.read_parquet(path)
            frame = frame[(frame.index >= start) & (frame.index.date <= end_date)]
            if frame.empty:
                raise RuntimeError(f"No frozen stock candles for {symbol} {interval}")
            data[symbol] = frame
            coverage.append({"symbol": symbol, "interval": interval, "bars": len(frame), "start": str(frame.index.min()), "end": str(frame.index.max())})
        data_by_interval[interval] = data
    pd.DataFrame(coverage).to_csv(output / "data_coverage.csv", index=False)
    summaries = []
    original_scan = controller_module._scan_strategy_replay
    try:
        for interval, orb in (("15m", 15), ("5m", 5), ("5m", 15)):
            base_path = output / f"base_{interval}_orb{orb}.pkl"
            flags_path = output / f"flags_{interval}_orb{orb}.pkl"
            settings = replace(baseline, interval=interval, orb_minutes=orb, top_n_tickers=10000,
                               breakout_follow_through={**(baseline.breakout_follow_through or {}), "enabled": False,
                                                       "volume_enabled": False, "follow_through_enabled": False})
            if base_path.exists() and flags_path.exists():
                raw_signals = pd.read_pickle(base_path)
                flags = pd.read_pickle(flags_path)
                print(f"Loaded frozen signals: {interval}, ORB {orb}", flush=True)
            else:
                flags = {}
                checked = 0
                def capture(strategy_name, symbol, history, scan_settings):
                    nonlocal checked
                    result = original_scan(strategy_name, symbol, history, scan_settings)
                    if not result or result.get("Signal") not in {"CALL", "PUT"}:
                        return result
                    intraday = result["Intraday Data"]
                    today = intraday[intraday.index.date == intraday.index[-1].date()]
                    orb_bars = max(1, int(round(orb / max(_bar_minutes(today.index), 1))))
                    opening = today.iloc[:orb_bars]
                    evaluations = {}
                    for volume, follow, extend in product((False, True), repeat=3):
                        confirmation_config = {**(baseline.breakout_follow_through or {}), "volume_enabled": volume,
                                               "follow_through_enabled": follow, "require_extension": extend}
                        evaluation = evaluate_breakout_follow_through(intraday, today, direction=result["Signal"],
                            orb_high=float(opening["High"].max()), orb_low=float(opening["Low"].min()),
                            orb_bars=orb_bars, config=confirmation_config)
                        evaluations[(volume, follow, extend)] = evaluation
                        # Check optimized gate application against direct strategy calls.
                        if checked < 2:
                            direct = original_scan(strategy_name, symbol, history,
                                replace(scan_settings, breakout_follow_through=confirmation_config))
                            expected = result["Signal"] if evaluation["confirmed"] else "WAIT"
                            assert direct["Signal"] == expected, (symbol, history.index[-1], confirmation_config)
                            assert direct["Score"] == result["Score"]
                    checked += 1
                    flags[(symbol, str(history.index[-1]))] = evaluations
                    return result
                controller_module._scan_strategy_replay = capture
                last_print = time.monotonic()
                def progress(current, total, row, signals):
                    nonlocal last_print
                    if time.monotonic() - last_print >= 20 or current == total:
                        print(f"Scanning {interval} ORB {orb}: {current}/{total}; qualifying observations {signals}", flush=True)
                        last_print = time.monotonic()
                print(f"Starting frozen scan: {interval}, ORB {orb}", flush=True)
                _, raw_signals, _ = lab.replay_and_scan(data_by_interval[interval], settings, progress_callback=progress)
                raw_signals.to_pickle(base_path)
                pd.to_pickle(flags, flags_path)
                controller_module._scan_strategy_replay = original_scan
            for stop, volume, follow, extend in product((10., 20.), (False, True), (False, True), (False, True)):
                run_id = f"orb{orb}_{interval}_stop{int(stop)}_v{int(volume)}_f{int(follow)}_e{int(extend)}"
                run_dir = output / run_id
                run_dir.mkdir(exist_ok=True)
                summary_path = run_dir / "summary.json"
                if summary_path.exists():
                    summaries.append(json.loads(summary_path.read_text()))
                    continue
                cfg = {**(baseline.breakout_follow_through or {}), "enabled": volume or follow,
                       "volume_enabled": volume, "follow_through_enabled": follow, "require_extension": extend}
                effective = replace(baseline, interval=interval, orb_minutes=orb, stop_loss_pct=stop, breakout_follow_through=cfg)
                accepted = []
                blockers = Counter()
                for row in raw_signals.to_dict("records"):
                    evaluation = flags[(row["symbol"], str(row["timestamp"]))][(volume, follow, extend)]
                    if not evaluation["confirmed"]:
                        blockers[evaluation["reason"]] += 1
                        continue
                    row.update(stop_loss_pct=stop, breakout_volume_ratio=evaluation["volume_ratio"],
                               breakout_follow_through=evaluation["follow_through_confirmed"],
                               breakout_volume_required=volume, breakout_follow_through_required=follow,
                               breakout_confirmation_reason=evaluation["reason"])
                    accepted.append(row)
                signals = _select_live_style_signals(accepted, effective)
                trades, metrics, decisions, _ = lab.simulate(signals, data_by_interval[interval], effective)
                rejected = decisions[decisions["stage"] == "OPTION_DATA"] if not decisions.empty and "stage" in decisions else pd.DataFrame()
                reason_counts = Counter(decisions["reason"]) if not decisions.empty else Counter()
                summary = {"run_id": run_id, "orb_minutes": orb, "interval": interval, "stop_loss_pct": stop,
                           "volume_enabled": volume, "follow_through_enabled": follow, "require_extension": extend,
                           "valid_orb": True, "extension_effective": follow, "raw_qualifying_observations": len(raw_signals),
                           "confirmation_rejected": sum(blockers.values()), "signals": len(signals),
                           "option_data_rejections": len(rejected), **metrics}
                summary_path.write_text(json.dumps(summary, indent=2, default=str))
                (run_dir / "settings.json").write_text(json.dumps(asdict(effective), indent=2))
                (run_dir / "blockers.json").write_text(json.dumps({"confirmation": dict(blockers), "simulator": dict(reason_counts)}, indent=2))
                trades.to_csv(run_dir / "trades.csv", index=False)
                signals.to_csv(run_dir / "signals.csv", index=False)
                decisions.to_csv(run_dir / "decisions.csv", index=False)
                summaries.append(summary)
                pd.DataFrame(summaries).to_csv(output / "results_partial.csv", index=False)
                print(f"{run_id}: {metrics['total_trades']} trades; P&L ${metrics['net_pnl']:.2f}; PF {metrics['profit_factor']}; option-data rejects {len(rejected)}", flush=True)
        # Retain all requested combinations, explicitly labeled as unsupported.
        for summary in list(summaries):
            if summary["interval"] == "15m":
                duplicate = {**summary, "orb_minutes": 5, "valid_orb": False, "effective_orb_minutes": 15,
                             "status": "UNSUPPORTED_5M_ORB_ON_15M_CANDLES", "source_run_id": summary["run_id"]}
                duplicate["run_id"] = summary["run_id"].replace("orb15_", "orb5_")
                summaries.append(duplicate)
        ranked = pd.DataFrame(summaries).sort_values(["valid_orb", "net_pnl"], ascending=[False, False])
        ranked.to_csv(output / "results.csv", index=False)
        (output / "completed.json").write_text(json.dumps({"combinations": len(summaries), "valid_runs": 48,
                "unique_option_requests": len(provider.memo), "option_request_failures": sum(isinstance(value, str) for value in provider.memo.values())}, indent=2))
        print("Completed parameter sweep", flush=True)
    finally:
        controller_module._scan_strategy_replay = original_scan
        provider.disconnect()


if __name__ == "__main__":
    main()
