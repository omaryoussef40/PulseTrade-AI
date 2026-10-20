from datetime import datetime
from unittest import TestCase
from unittest.mock import patch

import pandas as pd

from bot_core import EASTERN, completed_live_bars, scan_symbol_ib
from strategies.pmb.strategy import evaluate_breakout_follow_through


class CompletedLiveCandlesTests(TestCase):
    def setUp(self):
        rows = []
        for day in (25, 28, 29, 30):
            for clock, high, close, volume in (
                ('09:30', 100, 99.5, 1000),
                ('09:45', 102, 101.5, 2000 if day == 30 else 1000),
                ('10:00', 103, 102.5, 1000),
                ('10:15', 104, 103.5, 100),
            ):
                rows.append((pd.Timestamp(f'2026-09-{day} {clock}', tz=EASTERN), close, high, 99, close, volume))
        self.frame = pd.DataFrame(rows, columns=['time', 'Open', 'High', 'Low', 'Close', 'Volume']).set_index('time')

    def evaluate(self, clock):
        frame = completed_live_bars(self.frame, '15 mins', 15, pd.Timestamp(f'2026-09-30 {clock}', tz=EASTERN))
        today = frame[frame.index.day == 30]
        return evaluate_breakout_follow_through(frame, today, direction='CALL', orb_high=100, orb_low=99, orb_bars=1,
            config={'enabled': True, 'min_breakout_volume_ratio': 1.5, 'min_volume_sessions': 3, 'require_extension': True})

    def test_1000_waits_for_follow_through_to_close(self):
        self.assertEqual(self.evaluate('10:00:15')['reason'], 'Breakout volume passed; waiting for the next candle')

    def test_1015_accepts_closed_follow_through_despite_new_live_bar(self):
        self.assertTrue(self.evaluate('10:15:15')['confirmed'])
        self.assertTrue(self.evaluate('10:15:00')['confirmed'])

    def test_1030_still_rejects_genuinely_stale_confirmation(self):
        self.assertEqual(self.evaluate('10:30:00')['reason'], 'Follow-through confirmation is stale')

    def test_resampling_excludes_partial_analysis_bucket(self):
        frame = self.frame.iloc[:6].copy()
        frame.index = pd.date_range('2026-09-30 09:55', periods=6, freq='5min', tz=EASTERN)
        result = completed_live_bars(frame, '5 mins', 15, pd.Timestamp('2026-09-30 10:12', tz=EASTERN))
        self.assertEqual(list(result.index), [frame.index[0]])

    def test_one_minute_opening_scan_keeps_only_completed_bars(self):
        frame = self.frame.iloc[:3].copy()
        frame.index = pd.date_range('2026-09-30 09:30', periods=3, freq='min', tz=EASTERN)
        result = completed_live_bars(frame, '1 min', None, pd.Timestamp('2026-09-30 09:32:20', tz=EASTERN))
        self.assertEqual(len(result), 2)

    def test_live_wrapper_passes_closed_bars_to_strategy(self):
        frame = pd.concat([self.frame] * 4).sort_index()
        now = datetime(2026, 9, 30, 10, 15, 15, tzinfo=EASTERN)
        with patch('bot_core.datetime') as clock, patch('bot_core.fetch_ib_intraday', return_value=frame), patch('bot_core.fetch_ib_daily', return_value=pd.DataFrame()), patch('strategies.registry.scan_dataframe', return_value=None) as scan:
            clock.now.return_value = now
            scan_symbol_ib(None, 'GOOGL', intraday_bar_size='15 mins', analysis_bar_minutes=15)
        self.assertEqual(scan.call_args.kwargs['intraday'].index.max(), pd.Timestamp('2026-09-30 10:00', tz=EASTERN))
