from datetime import datetime
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from backtester.controller import StrategyLabController, StrategyLabSettings, _scan_strategy_replay
from bot_core import scan_symbol_ib
from strategies.pmb.strategy import evaluate_breakout_follow_through
from test_backtester_live_candles import source_bars, replay_history


EASTERN = 'America/New_York'


def confirmation_config(minutes):
    return {'confirmation_minutes': minutes, 'volume_enabled': True, 'follow_through_enabled': True,
            'min_breakout_volume_ratio': 1.5, 'min_volume_sessions': 3, 'require_extension': False}


class ConfirmationIntervalTests(unittest.TestCase):
    def replay(self, minutes, clock, frame=None):
        now = pd.Timestamp('2026-09-10 ' + clock, tz=EASTERN)
        settings = StrategyLabSettings(symbols=['TEST'], interval='5m', min_score=70, min_session_bars=2,
                                       breakout_follow_through=confirmation_config(minutes))
        return _scan_strategy_replay('PMB', 'TEST', replay_history(source_bars() if frame is None else frame, now), settings)

    def test_five_minute_confirmation_waits_at_1000_and_enters_at_1005(self):
        waiting = self.replay(5, '10:00')
        confirmed = self.replay(5, '10:05')
        self.assertEqual(waiting['Signal'], 'WAIT')
        self.assertEqual(confirmed['Signal'], 'CALL')
        self.assertEqual(confirmed['Confirmation Candle Minutes'], 5)
        self.assertIn('10:05', confirmed['Confirmation Closed At'])
        self.assertEqual(confirmed['Analysis Price'], 101)
        self.assertEqual(confirmed['Price'], 102)
        for key in ('Score', 'VWAP', 'ATR %', 'EMA9', 'EMA21', 'ORB High', 'ORB Low'):
            self.assertEqual(confirmed.get(key), waiting.get(key), key)
        pd.testing.assert_frame_equal(confirmed['Intraday Data'], waiting['Intraday Data'])

    def test_fifteen_minute_confirmation_waits_until_1015(self):
        self.assertIsNone(self.replay(15, '10:05'))
        result = self.replay(15, '10:15')
        self.assertEqual(result['Signal'], 'CALL')
        self.assertEqual(result['Confirmation Candle Minutes'], 15)
        self.assertIn('10:15', result['Confirmation Closed At'])

    def test_five_minute_confirmation_rejects_a_close_inside_range(self):
        raw = source_bars()
        raw.loc[pd.Timestamp('2026-09-10 10:00', tz=EASTERN), 'Close'] = 100.1
        result = self.replay(5, '10:05', raw)
        self.assertEqual(result['Signal'], 'WAIT')
        self.assertIn('inside the opening range', result['Breakout Follow-through Reason'])

    def test_five_minute_confirmation_handles_put_direction(self):
        raw = source_bars()
        high, low = raw.High.copy(), raw.Low.copy()
        raw['High'], raw['Low'] = 200 - low, 200 - high
        raw['Open'], raw['Close'] = 200 - raw.Open, 200 - raw.Close
        result = self.replay(5, '10:05', raw)
        self.assertEqual(result['Signal'], 'PUT')
        self.assertEqual(result['Price'], 98)
        self.assertTrue(result['Breakout Follow-through Confirmed'])

    def test_five_minute_confirmation_is_not_reused_at_1010(self):
        self.assertIsNone(self.replay(5, '10:10'))
        self.assertEqual(self.replay(5, '10:15')['Signal'], 'WAIT')

    def test_volume_filter_still_checks_the_15_minute_breakout(self):
        result = self.replay(5, '10:05')
        self.assertEqual(result['Breakout Volume'], 6000)
        self.assertEqual(result['Breakout Volume Baseline'], 3000)
        self.assertEqual(result['Breakout Volume Ratio'], 2)

    def test_live_wrapper_fetches_five_minute_history_and_matches_replay(self):
        raw = source_bars()
        now = pd.Timestamp('2026-09-10 10:05', tz=EASTERN)
        with patch('bot_core.fetch_ib_intraday', return_value=raw) as fetch, patch('bot_core.fetch_ib_daily', return_value=pd.DataFrame()), patch('bot_core.datetime') as clock:
            clock.now.return_value = datetime(2026, 9, 10, 10, 5, tzinfo=now.tz)
            clock.fromisoformat.side_effect = datetime.fromisoformat
            live = scan_symbol_ib(None, 'TEST', min_score=70, min_session_bars=2, intraday_bar_size='15 mins',
                                  analysis_bar_minutes=15, breakout_follow_through=confirmation_config(5))
        replay = self.replay(5, '10:05')
        self.assertEqual(fetch.call_args.kwargs['bar_size'], '5 mins')
        self.assertEqual(live['Signal'], 'CALL')
        for key in ('Signal', 'Price', 'Score', 'VWAP', 'ATR %', 'ORB High', 'Breakout Volume Ratio', 'Confirmation Closed At'):
            self.assertEqual(live.get(key), replay.get(key), key)
        pd.testing.assert_frame_equal(live['Intraday Data'], replay['Intraday Data'])

    def test_live_five_minute_confirmation_excludes_the_still_forming_candle(self):
        raw = source_bars()
        now = pd.Timestamp('2026-09-10 10:04:59', tz=EASTERN)
        with patch('bot_core.fetch_ib_intraday', return_value=raw), patch('bot_core.fetch_ib_daily', return_value=pd.DataFrame()), patch('bot_core.datetime') as clock:
            clock.now.return_value = now.to_pydatetime()
            clock.fromisoformat.side_effect = datetime.fromisoformat
            result = scan_symbol_ib(None, 'TEST', min_score=70, min_session_bars=2, intraday_bar_size='15 mins',
                                    analysis_bar_minutes=15, breakout_follow_through=confirmation_config(5))
        self.assertEqual(result['Signal'], 'WAIT')
        self.assertFalse(result['Breakout Follow-through Confirmed'])

    def test_live_fifteen_minute_selection_keeps_native_15_minute_history(self):
        native = source_bars().resample('15min').agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}).dropna()
        now = pd.Timestamp('2026-09-10 10:15', tz=EASTERN)
        with patch('bot_core.fetch_ib_intraday', return_value=native) as fetch, patch('bot_core.fetch_ib_daily', return_value=pd.DataFrame()), patch('bot_core.datetime') as clock:
            clock.now.return_value = now.to_pydatetime()
            clock.fromisoformat.side_effect = datetime.fromisoformat
            live = scan_symbol_ib(None, 'TEST', min_score=70, min_session_bars=2, intraday_bar_size='15 mins',
                                  analysis_bar_minutes=15, breakout_follow_through=confirmation_config(15))
        replay = self.replay(15, '10:15')
        self.assertEqual(fetch.call_args.kwargs['bar_size'], '15 mins')
        self.assertEqual(live['Signal'], 'CALL')
        self.assertEqual(live['Score'], replay['Score'])
        self.assertEqual(live['Confirmation Closed At'], replay['Confirmation Closed At'])
        self.assertEqual(live['Price'], replay['Price'])

    def test_replay_signal_is_timestamped_1005_with_confirmation_close_price(self):
        settings = StrategyLabSettings(symbols=['TEST'], period='1d', interval='5m', min_score=70, min_session_bars=2,
                                       min_atr=0, use_sr_filter=False, breakout_follow_through=confirmation_config(5))
        controller = StrategyLabController(data_provider=Mock())
        _, signals, _ = controller.replay_and_scan({'TEST': source_bars()}, settings)
        today = signals[signals.timestamp.dt.date == pd.Timestamp('2026-09-10').date()]
        self.assertEqual(len(today), 1)
        self.assertEqual(today.iloc[0].timestamp.strftime('%H:%M'), '10:05')
        self.assertEqual(today.iloc[0].price, 102)
        self.assertEqual(today.iloc[0].confirmation_candle_minutes, 5)

    def test_missing_confirmation_source_candle_cannot_qualify(self):
        raw = source_bars()
        history = replay_history(raw, pd.Timestamp('2026-09-10 10:05', tz=EASTERN))
        analysis = history.copy()
        analysis.index -= pd.Timedelta(minutes=5)
        analysis = analysis.resample('15min').agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}).dropna()
        analysis = analysis[analysis.index < pd.Timestamp('2026-09-10 10:00', tz=EASTERN)]
        today = analysis[analysis.index.date == pd.Timestamp('2026-09-10').date()]
        source = raw[raw.index < pd.Timestamp('2026-09-10 10:00', tz=EASTERN)]
        result = evaluate_breakout_follow_through(analysis, today, direction='CALL', orb_high=100.2, orb_low=99.8,
                   orb_bars=1, config=confirmation_config(5), confirmation_intraday=source, confirmation_source_minutes=5,
                   as_of=pd.Timestamp('2026-09-10 10:05', tz=EASTERN))
        self.assertFalse(result['confirmed'])
        self.assertIn('Waiting', result['reason'])

    def test_five_minute_confirmation_cannot_use_15_minute_source(self):
        settings = StrategyLabSettings(symbols=['TEST'], interval='15m', breakout_follow_through=confirmation_config(5))
        controller = StrategyLabController(data_provider=Mock())
        with self.assertRaisesRegex(ValueError, '5-minute confirmation requires'):
            controller.replay_and_scan({'TEST': source_bars()}, settings)


if __name__ == '__main__':
    unittest.main()
