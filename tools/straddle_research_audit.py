"""Offline data-quality audit and chronological checks for a completed sweep."""
from pathlib import Path
import argparse
import json
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--full', action='store_true')
    args = parser.parse_args()
    out = args.directory.resolve()
    manifest = json.loads((out / 'manifest.json').read_text())
    finished = json.loads((out / 'completed.json').read_text())
    holdout = set(finished['sessions'][-3:])
    index = {}
    cache = Path(manifest.get('option_cache_dir', ROOT / 'backtester/cache/straddle/ibkr/options'))
    for p in cache.glob('*.json'):
        info = json.loads(p.read_text())
        if info.get('localSymbol') and p.with_suffix('.parquet').exists():
            # Historical bar caches each contain only the date encoded in the filename.
            day = p.name.split('-')[2:5]
            index[(info['localSymbol'], '-'.join(day))] = p.with_suffix('.parquet')
    memo = {}

    def frame(local, day):
        path = index.get((local, day))
        if path is None:
            return pd.DataFrame()
        if path not in memo:
            memo[path] = pd.read_parquet(path)
        return memo[path]

    rows, summaries = [], []
    rankings = pd.read_csv(out / 'rankings.csv')
    for name in rankings.scenario:
        cfg = json.loads((out / f'{name}_settings.json').read_text())
        trades = pd.read_csv(out / (f'{name}_trades.csv' if args.full else f'{name}_common_trades.csv'))
        trades = trades[trades.option_status == 'AVAILABLE'].copy()
        if trades.empty:
            continue
        flags = []
        for _, row in trades.iterrows():
            day = str(row.session_date)
            entry, confirmation, exit_time = (pd.Timestamp(row[c]) for c in ['entry_time', 'confirmation_time', 'exit_time'])
            call, put = frame(row.call_local_symbol, day), frame(row.put_local_symbol, day)
            checks = [('call_entry', call, entry), ('put_entry', put, entry)]
            if bool(row.broke_orb):
                winner, loser = (call, put) if row.direction == 'CALL' else (put, call)
                checks.append(('loser_exit', loser, confirmation))
                # Stops/targets act on completed closes; timed exits act on opens.
                ts = exit_time if row.exit_reason == 'End-of-day exit' or exit_time == confirmation else exit_time - pd.Timedelta(minutes=5)
                checks.append(('winner_exit', winner, ts))
            else:
                checks.extend([('call_exit', call, exit_time), ('put_exit', put, exit_time)])
            bad = []
            for label, bars, timestamp in checks:
                if bars.empty or timestamp not in bars.index or float(bars.loc[timestamp, 'Volume']) <= 0:
                    bad.append(label)
            flags.append(not bad)
            rows.append({'scenario': name, 'symbol': row.symbol, 'session_date': day,
                         'pnl': row.realized_pnl, 'positive_volume_at_all_fill_bars': not bad,
                         'flagged_fills': ','.join(bad)})
        trades['quality_ok'] = flags
        pnl = trades.realized_pnl
        training = trades[~trades.session_date.astype(str).isin(holdout)]
        test = trades[trades.session_date.astype(str).isin(holdout)]
        clean = trades[trades.quality_ok]
        summaries.append({'scenario': name, 'dte': cfg['option_dte'], ('available_pairs' if args.full else 'common_pairs'): len(trades),
                          'net_pnl': round(pnl.sum(), 2), 'training_pnl': round(training.realized_pnl.sum(), 2),
                          'training_pairs': len(training), 'holdout_pnl': round(test.realized_pnl.sum(), 2),
                          'holdout_pairs': len(test), 'quality_pairs': len(clean),
                          'quality_pnl': round(clean.realized_pnl.sum(), 2),
                          'quality_win_rate': round((clean.realized_pnl > 0).mean()*100, 2) if len(clean) else 0})
    prefix = 'full_' if args.full else ''
    pd.DataFrame(rows).to_csv(out / f'{prefix}fill_quality.csv', index=False)
    pd.DataFrame(summaries).sort_values('training_pnl', ascending=False).to_csv(out / f'{prefix}chronological_validation.csv', index=False)
    print(pd.DataFrame(summaries).sort_values('net_pnl', ascending=False).head(15).to_string(index=False))


if __name__ == '__main__':
    main()
