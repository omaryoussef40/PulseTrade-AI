"""Read-only, frozen-data straddle sweep. Never saves live or UI settings."""
from dataclasses import asdict, fields, replace
from itertools import product
from pathlib import Path
import argparse
import json
import sys
import time
import shutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pandas as pd
from backtester.straddle_results import StraddleResultsConfig, scan_straddle_sessions, simulate_straddles
from backtester.straddle_windows import lookback_window, filter_window
from providers.ibkr_provider import IBKRMarketDataProvider, IBKRProviderConfig


class ResearchProvider(IBKRMarketDataProvider):
    weekly = False

    def connect(self):
        ib = super().connect()
        if not getattr(ib, '_research_standard_chains', False):
            original = ib.reqSecDefOptParams
            chain_cache = {}
            def standard_chains(symbol, *args, **kwargs):
                if symbol in chain_cache:
                    return chain_cache[symbol]
                chains = original(symbol, *args, **kwargs)
                standard = [c for c in chains if c.tradingClass == symbol and str(c.multiplier) == '100']
                if not standard:
                    raise RuntimeError(f'No standard 100-share option chain for {symbol}')
                if self.weekly:
                    standard = [c._replace(expirations=[e for e in c.expirations if pd.Timestamp(e).weekday() == 4]) for c in standard]
                chain_cache[symbol] = standard
                return standard
            ib.reqSecDefOptParams = standard_chains
            qualify = ib.qualifyContracts
            qualified_cache = {}
            def frozen_qualification(*contracts):
                key = tuple((c.secType, c.symbol, c.lastTradeDateOrContractMonth, c.strike,
                             c.right, c.exchange, c.currency, c.multiplier, c.tradingClass,
                             c.includeExpired) for c in contracts)
                if key not in qualified_cache:
                    qualified_cache[key] = qualify(*contracts)
                return qualified_cache[key]
            ib.qualifyContracts = frozen_qualification
            historical = ib.reqHistoricalData
            history_cache = {}
            empty_sessions = set()
            for p in self.option_cache_dir.glob('*.json'):
                info = json.loads(p.read_text())
                data = p.with_suffix('.parquet')
                if info.get('conId') and data.exists() and pd.read_parquet(data).empty:
                    empty_sessions.add((int(info['conId']), '-'.join(p.name.split('-')[2:5])))
            def frozen_history(contract, *args, **kwargs):
                key = (contract.conId, repr(args), repr(sorted(kwargs.items())))
                if key not in history_cache:
                    end = kwargs.get('endDateTime')
                    if (kwargs.get('durationStr') == '1 D' and kwargs.get('barSizeSetting') == '5 mins'
                            and kwargs.get('whatToShow') == 'TRADES' and end
                            and (contract.conId, str(pd.Timestamp(end).date())) in empty_sessions):
                        history_cache[key] = []
                    else:
                        history_cache[key] = historical(contract, *args, **kwargs)
                return history_cache[key]
            ib.reqHistoricalData = frozen_history
            ib._research_standard_chains = True
        return ib


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--weekly14', action='store_true')
    parser.add_argument('--frozen-source', type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    config = json.loads((ROOT / 'config.json').read_text())
    names = {f.name for f in fields(StraddleResultsConfig)}
    baseline = StraddleResultsConfig(**{k: v for k, v in config['straddle_results'].items() if k in names})
    baseline = replace(baseline, period='14d', contracts_per_leg=1)
    dtes = (14,) if args.weekly14 else (7, 14)
    if args.weekly14:
        baseline = replace(baseline, option_dte=14)
    symbols = ['AMZN', 'NVDA', 'AAPL', 'MSFT', 'META', 'GOOGL', 'TSLA', 'AMD', 'NFLX', 'AVGO']
    fetch, start, end = lookback_window('14d')
    ibc = config.get('ib', {})
    provider = ResearchProvider(IBKRProviderConfig(
        host=ibc.get('host', '127.0.0.1'),
        port=int(ibc.get('live_port', 7496) if config.get('account_mode') == 'Live' else ibc.get('paper_port', 7497)),
        client_id=1969 if args.weekly14 else 1968, readonly=True, request_timeout_seconds=20),
        cache_dir=out / 'cache' if args.weekly14 else ROOT / 'backtester/cache/straddle/ibkr')
    provider.weekly = args.weekly14
    if args.weekly14:
        for path in (ROOT / 'backtester/cache/straddle/ibkr/options').glob('*.json'):
            info = json.loads(path.read_text())
            if info.get('expiry') and info.get('option_dte') == 14 and pd.Timestamp(info['expiry']).weekday() == 4 and path.with_suffix('.parquet').exists():
                for src in (path, path.with_suffix('.parquet')):
                    shutil.copy2(src, provider.option_cache_dir / src.name)
    memo, coverage, stock = {}, [], {}
    prior_failures = {}
    if (out / 'option_coverage.csv').exists():
        previous = pd.read_csv(out / 'option_coverage.csv')
        for _, row in previous[previous.error.notna() & (previous.error != '')].iterrows():
            prior_failures[(row.symbol, str(row.entry), int(row.dte))] = str(row.error)
    manifest = {'start': str(start), 'end': str(end), 'symbols': symbols, 'baseline': asdict(baseline),
                'readonly': True, 'stock_refresh': True,
                'expiration_selection': 'Closest Friday expiration, 14-day target' if args.weekly14 else 'Current backtester nearest-expiration selection',
                'option_cache_dir': str(provider.option_cache_dir),
                'option_data': 'Dedicated straddle cache, supplemented by historical IBKR requests. Expired uncached contracts excluded.',
                'sizing': 'One call plus one put per symbol/session; no portfolio capital constraints',
                'holdout': 'Last three trading sessions; descriptive only, all scenarios also ranked on full sample',
                'limitations': 'TRADES bars are not executable quotes. Zero-volume or stale marks can affect results.'}
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2))

    def pair(**kw):
        key = (kw['symbol'], str(pd.Timestamp(kw['reference_time'])), kw['option_dte'], round(kw['underlying_price'], 6))
        if key not in memo:
            try:
                if (key[0], key[1], key[2]) in prior_failures:
                    raise RuntimeError(prior_failures[(key[0], key[1], key[2])])
                ts = pd.Timestamp(kw['reference_time'])
                cached_info, cached = provider._find_cached_option_session(
                    kw['symbol'], 'CALL', kw['underlying_price'], kw['option_dte'], ts, '5 mins', 'TRADES', True)
                if not cached_info and (ts + pd.Timedelta(days=kw['option_dte'])).date() < pd.Timestamp.now(tz='America/New_York').date():
                    raise RuntimeError('Expired target option: no existing straddle cache; IBKR does not supply expired option history')
                ci, cb = provider.historical_option_bars(signal='CALL', **kw)
                pi, pb = provider.historical_option_bars_for_contract(
                    symbol=kw['symbol'], signal='PUT', expiry=ci['expiry'], strike=ci['strike'],
                    option_dte=kw['option_dte'], reference_time=kw['reference_time'])
                if cb.empty or pb.empty:
                    raise RuntimeError('Empty option history')
                memo[key] = (ci, cb.copy(), pi, pb.copy())
                coverage.append({'symbol': kw['symbol'], 'session_date': str(ts.date()), 'entry': str(ts),
                                 'dte': kw['option_dte'], 'expiry': ci['expiry'], 'strike': ci['strike'],
                                 'call_bars': len(cb), 'put_bars': len(pb),
                                 'call_positive_volume_bars': int((cb['Volume'] > 0).sum()),
                                 'put_positive_volume_bars': int((pb['Volume'] > 0).sum()),
                                 'source': 'existing_cache' if cached_info else 'download', 'error': ''})
            except Exception as exc:
                memo[key] = str(exc)
                coverage.append({'symbol': kw['symbol'], 'session_date': str(pd.Timestamp(kw['reference_time']).date()),
                                 'entry': str(kw['reference_time']), 'dte': kw['option_dte'], 'error': str(exc)})
            pd.DataFrame(coverage).to_csv(out / 'option_coverage.csv', index=False)
            print(f"Pair {len(memo)}: {key[0]} {key[1]} DTE{key[2]} {'OK' if not isinstance(memo[key], str) else memo[key][:100]}", flush=True)
        if isinstance(memo[key], str):
            raise RuntimeError(memo[key])
        return memo[key]

    try:
        for symbol in symbols:
            p = out / f'{symbol}_stocks.parquet'
            if not p.exists() and args.frozen_source:
                shutil.copy2(args.frozen_source / p.name, p)
            if p.exists():
                stock[symbol] = pd.read_parquet(p)
            else:
                stock[symbol] = filter_window(provider.load(symbol, period=fetch, interval='5m',
                    force_refresh=True, allow_cache_fallback=False), start, end)
                stock[symbol].to_parquet(p)
            print(f"Stock {symbol}: {len(stock[symbol])} bars, {len(set(stock[symbol].index.date))} sessions", flush=True)
        variants = [('current', baseline)]
        for dte, stop, target, distance in product(dtes, (20., 30.), (20., 30., 40.), (5., 10.)):
            variants.append((f'dte{dte}_stop{stop:g}_target{target:g}_trail{distance:g}',
                replace(baseline, option_dte=dte, winner_stop_loss_pct=stop,
                        winner_target_pct=target, trailing_stop_pct=distance)))
        for label, changes in [
            ('entry0930', {'entry_minute': 30}), ('entry0935', {'entry_minute': 35}),
            ('entry0940', {'entry_minute': 40}),
            ('confirm5', {'confirmation_minutes': 5, 'entry_minute': 35}),
            ('confirm10', {'confirmation_minutes': 10, 'entry_minute': 40}),
            ('orb15_confirm15', {'orb_minutes': 15}),
            ('vwap', {'require_vwap': True}), ('volume', {'require_volume': True}),
            ('breakeven10', {'breakeven_enabled': True, 'breakeven_trigger_pct': 10}),
            ('no_trailing', {'trailing_enabled': False}),
            ('hold_no_break', {'no_break_action': 'Hold both to timed exit'}),
            ('exit1200', {'force_exit_hour': 12, 'force_exit_minute': 0}),
            ('exit1400', {'force_exit_hour': 14, 'force_exit_minute': 0}),
            ('activation25', {'trailing_trigger_pct': 25}),
            ('slippage1', {'slippage_pct': 1}), ('slippage2', {'slippage_pct': 2}),
        ]:
            for dte in dtes:
                variants.append((f'{label}_dte{dte}', replace(baseline, option_dte=dte, **changes)))
        results, all_trades = [], {}
        dates = sorted({d for frame in stock.values() for d in frame.index.date})
        for name, settings in variants:
            print(f'Scenario {name}', flush=True)
            existing = out / f'{name}_trades.csv'
            saved_settings = out / f'{name}_settings.json'
            if existing.exists() and saved_settings.exists() and json.loads(saved_settings.read_text()) == asdict(settings):
                trades = pd.read_csv(existing)
            else:
                trades = simulate_straddles(scan_straddle_sessions(stock, settings), pair, settings)
            trades.to_csv(out / f'{name}_trades.csv', index=False)
            (out / f'{name}_settings.json').write_text(json.dumps(asdict(settings), indent=2))
            all_trades[name] = trades
        # Every setting in the same DTE group is compared on exactly the same symbol-days.
        for dte in dtes:
            subset = [(n, s) for n, s in variants if s.option_dte == dte]
            common = None
            for n, _ in subset:
                t = all_trades[n]
                valid = t[t.option_status == 'AVAILABLE']
                keys = set(zip(valid.symbol, valid.session_date.astype(str)))
                common = keys if common is None else common & keys
            for n, s in subset:
                t = all_trades[n]
                t = t[[ (sym, str(day)) in common for sym, day in zip(t.symbol, t.session_date) ]].copy()
                t.to_csv(out / f'{n}_common_trades.csv', index=False)
                pnl = pd.to_numeric(t.realized_pnl) if len(t) else pd.Series(dtype=float)
                daily = t.groupby('session_date').realized_pnl.sum().sort_index() if len(t) else pd.Series(dtype=float)
                equity = pd.concat([pd.Series([0.]), daily.reset_index(drop=True)]).cumsum()
                holdout = t[t.session_date.astype(str).isin([str(d) for d in dates[-3:]])]
                results.append({'scenario': n, 'dte': dte, 'common_pairs': len(t),
                    'full_available_pairs': int((all_trades[n].option_status == 'AVAILABLE').sum()),
                    'expected_pairs': len(symbols) * len(dates), 'net_pnl': round(pnl.sum(), 2),
                    'win_rate': round((pnl > 0).mean()*100, 2) if len(pnl) else 0,
                    'profit_factor': round(pnl[pnl>0].sum()/abs(pnl[pnl<0].sum()), 3) if (pnl<0).any() else None,
                    'daily_drawdown': round((equity.cummax()-equity).max(), 2),
                    'holdout_pairs': len(holdout), 'holdout_pnl': round(holdout.realized_pnl.sum(), 2) if len(holdout) else 0,
                    'stop': s.winner_stop_loss_pct, 'target': s.winner_target_pct,
                    'trail_trigger': s.trailing_trigger_pct, 'trail_distance': s.trailing_stop_pct})
        pd.DataFrame(results).sort_values(['dte', 'net_pnl'], ascending=[True, False]).to_csv(out / 'rankings.csv', index=False)
        (out / 'completed.json').write_text(json.dumps({'scenarios': len(variants), 'unique_pairs': len(memo),
            'failed_pairs': sum(isinstance(v, str) for v in memo.values()), 'sessions': [str(d) for d in dates]}, indent=2))
        print('COMPLETED', flush=True)
    finally:
        provider.disconnect()


if __name__ == '__main__':
    main()
