import unittest
from unittest.mock import patch

import pandas as pd

import bot_core
from strategies.pmb import strategy


def frame():
    index = pd.date_range('2026-09-28 09:30', periods=5, freq='15min', tz='America/New_York')
    return pd.DataFrame([
        [105, 106, 100, 101, 100],
        [101, 103, 100, 102, 100],
        [102, 102, 98, 99, 300],
        [99, 100, 97, 98, 100],
        [98, 99, 97, 98, 5],
    ], index=index, columns=strategy.REQUIRED_COLUMNS, dtype=float)


def timestamp(clock):
    return pd.Timestamp('2026-09-28 ' + clock, tz='America/New_York')


class CompletedCandleTests(unittest.TestCase):
    def evaluate(self, clock):
        today = strategy.completed_bars(frame(), pd.Timedelta(minutes=15), timestamp(clock))
        prior = []
        for day in ('2026-09-23', '2026-09-24', '2026-09-25'):
            previous = frame().copy()
            previous.index = pd.date_range(day + ' 09:30', periods=5, freq='15min', tz='America/New_York')
            previous['Volume'] = 100.
            prior.append(previous)
        return strategy.evaluate_breakout_follow_through(
            pd.concat(prior + [today]), today, direction='PUT',
            orb_high=106, orb_low=100, orb_bars=1,
            config={'enabled': True, 'min_breakout_volume_ratio': 2.,
                    'min_volume_sessions': 3, 'require_extension': True})

    def test_unfinished_breakout_not_evaluated(self):
        result = self.evaluate('10:00:41')
        self.assertEqual(result['reason'], 'Waiting for the first close beyond the opening range')

    def test_unfinished_confirmation_waits_instead_of_rejecting(self):
        result = self.evaluate('10:15:41')
        self.assertEqual(result['reason'], 'Breakout volume passed; waiting for the next candle')

    def test_next_developing_bar_does_not_make_confirmation_stale(self):
        self.assertTrue(self.evaluate('10:30:00')['confirmed'])
        self.assertTrue(self.evaluate('10:30:41')['confirmed'])
        self.assertEqual(self.evaluate('10:45:00')['reason'], 'Follow-through confirmation is stale')

    def test_close_boundary_and_utc(self):
        data = frame().tz_convert('UTC')
        before = strategy.completed_bars(data, pd.Timedelta(minutes=15), timestamp('10:30:00') - pd.Timedelta(microseconds=1))
        at = strategy.completed_bars(data, pd.Timedelta(minutes=15), timestamp('10:30:00'))
        self.assertEqual(len(before), 3)
        self.assertEqual(len(at), 4)
        self.assertEqual(len(data), 5)

    def test_live_scan_filters_source_and_freezes_cutoff_before_fetch(self):
        data = frame()
        history = pd.DataFrame(100., index=pd.date_range('2026-09-23 09:30', periods=60, freq='15min', tz='America/New_York'), columns=strategy.REQUIRED_COLUMNS)
        data = pd.concat([history, data])
        cutoff = timestamp('10:30:00')
        def fetched(*args, **kwargs):
            clock.now.return_value = timestamp('10:45:00')
            return data
        with patch.object(bot_core, 'datetime') as clock, \
             patch.object(bot_core, 'fetch_ib_intraday', side_effect=fetched), \
             patch.object(bot_core, 'fetch_ib_daily', return_value=pd.DataFrame()), \
             patch('strategies.registry.scan_dataframe', return_value=None) as scan:
            clock.now.return_value = cutoff
            bot_core.scan_symbol_ib(None, 'TEST', intraday_bar_size='15 mins', analysis_bar_minutes=15)
        supplied = scan.call_args.kwargs
        self.assertEqual(supplied['intraday'].index[-1], timestamp('10:15'))
        self.assertEqual(supplied['completed_as_of'], cutoff)

    def test_resampled_analysis_excludes_partial_bucket(self):
        data = pd.DataFrame(100., index=pd.date_range('2026-09-28 09:30', periods=60, freq='min', tz='America/New_York'), columns=strategy.REQUIRED_COLUMNS)
        data = strategy.completed_bars(data, pd.Timedelta(minutes=1), timestamp('10:27'))
        # Stop at indicator calculation and inspect its actual input; no orders
        # or broker access are involved.
        class Inspected(Exception):
            pass
        def inspect(bars):
            self.assertEqual(bars.index[-1], timestamp('10:20'))
            raise Inspected()
        # Include enough history to satisfy the strategy's 50-bar warmup.
        history = data.copy()
        history.index = history.index - pd.Timedelta(days=1)
        history = pd.concat([history, history.set_axis(history.index - pd.Timedelta(days=1)),
                             history.set_axis(history.index - pd.Timedelta(days=2)),
                             history.set_axis(history.index - pd.Timedelta(days=3)), data]).sort_index()
        with patch.object(strategy, 'calculate_vwap', side_effect=inspect):
            with self.assertRaises(Inspected):
                strategy.scan_dataframe('TEST', history, analysis_bar_minutes=5, completed_as_of=timestamp('10:27'))
