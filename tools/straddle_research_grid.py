"""Combine entry/confirmation variants with exit settings on frozen contracts."""
from dataclasses import asdict, replace
from itertools import product
from pathlib import Path
import argparse
import json
import sys
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backtester.straddle_results import StraddleResultsConfig, scan_straddle_sessions, simulate_straddles


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    out = parser.parse_args().directory.resolve()
    manifest = json.loads((out / 'manifest.json').read_text())
    done = json.loads((out / 'completed.json').read_text())
    cache = Path(manifest.get('option_cache_dir', ROOT / 'backtester/cache/straddle/ibkr/options'))
    lookup, frames = {}, {}
    for p in cache.glob('*.json'):
        info = json.loads(p.read_text())
        if info.get('localSymbol') and p.with_suffix('.parquet').exists():
            lookup[(info['localSymbol'], '-'.join(p.name.split('-')[2:5]))] = (info, p.with_suffix('.parquet'))
    stock = {sym: pd.read_parquet(out / f'{sym}_stocks.parquet') for sym in manifest['symbols']}
    ranks = pd.read_csv(out / 'rankings.csv')
    prior = {json.dumps(json.loads(p.read_text()), sort_keys=True) for p in out.glob('*_settings.json')}
    rows = []
    for dte in ranks.dte.unique():
        common_sample = pd.read_csv(out / f'{ranks[ranks.dte==dte].iloc[0].scenario}_common_trades.csv')
        common_keys = set(zip(common_sample.symbol, common_sample.session_date.astype(str)))
        for template in ('confirm5', 'confirm10', 'orb15_confirm15'):
            template_name = f'{template}_dte{dte}'
            cfg = StraddleResultsConfig(**json.loads((out / f'{template_name}_settings.json').read_text()))
            original = pd.read_csv(out / f'{template_name}_trades.csv')
            pair_rows = {(r.symbol, str(r.session_date)): r for _, r in original.iterrows()}
            def pair(**kw):
                key = (kw['symbol'], str(pd.Timestamp(kw['reference_time']).date()))
                row = pair_rows[key]
                if row.option_status != 'AVAILABLE':
                    raise RuntimeError(str(row.option_error))
                result = []
                for local in (row.call_local_symbol, row.put_local_symbol):
                    info, p = lookup[(local, key[1])]
                    if p not in frames:
                        frames[p] = pd.read_parquet(p)
                    result.extend([info, frames[p]])
                return tuple(result)
            for stop, target, distance in product((20., 30.), (20., 30., 40.), (5., 10.)):
                setting = replace(cfg, winner_stop_loss_pct=stop, winner_target_pct=target, trailing_stop_pct=distance)
                fingerprint = json.dumps(asdict(setting), sort_keys=True)
                if fingerprint in prior:
                    continue
                prior.add(fingerprint)
                name = f'joint_{template_name}_s{stop:g}_t{target:g}_tr{distance:g}'
                t = simulate_straddles(scan_straddle_sessions(stock, setting), pair, setting)
                t.to_csv(out / f'{name}_trades.csv', index=False)
                (out / f'{name}_settings.json').write_text(json.dumps(asdict(setting), indent=2))
                full_available = int((t.option_status == 'AVAILABLE').sum())
                t = t[[(s, str(d)) in common_keys for s, d in zip(t.symbol, t.session_date)]].copy()
                t.to_csv(out / f'{name}_common_trades.csv', index=False)
                pnl = t.realized_pnl
                daily = t.groupby('session_date').realized_pnl.sum().sort_index()
                equity = pd.concat([pd.Series([0.]), daily.reset_index(drop=True)]).cumsum()
                holdout = t[t.session_date.astype(str).isin(done['sessions'][-3:])]
                rows.append({'scenario': name, 'dte': int(dte), 'common_pairs': len(t),
                    'full_available_pairs': full_available, 'expected_pairs': len(stock)*len(done['sessions']),
                    'net_pnl': round(pnl.sum(), 2), 'win_rate': round((pnl>0).mean()*100, 2),
                    'profit_factor': round(pnl[pnl>0].sum()/abs(pnl[pnl<0].sum()), 3) if (pnl<0).any() else None,
                    'daily_drawdown': round((equity.cummax()-equity).max(), 2),
                    'holdout_pairs': len(holdout), 'holdout_pnl': round(holdout.realized_pnl.sum(), 2),
                    'stop': stop, 'target': target, 'trail_trigger': setting.trailing_trigger_pct,
                    'trail_distance': distance})
                print(f'{name}: {len(t)} common pairs; ${pnl.sum():.2f}', flush=True)
    if rows:
        pd.concat([ranks, pd.DataFrame(rows)], ignore_index=True).sort_values(['dte','net_pnl'], ascending=[True,False]).to_csv(out/'rankings.csv', index=False)
    done['joint_exit_scenarios_added'] = done.get('joint_exit_scenarios_added', 0) + len(rows)
    done['scenarios'] += len(rows)
    (out/'completed.json').write_text(json.dumps(done, indent=2))


if __name__ == '__main__':
    main()
