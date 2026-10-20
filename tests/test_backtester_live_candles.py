from datetime import datetime
import unittest
from unittest.mock import Mock, patch
from tempfile import TemporaryDirectory

import pandas as pd

from backtester.controller import StrategyLabController, StrategyLabSettings, _scan_strategy_replay
from bot_core import scan_symbol_ib
from strategies.pmb.candles import prepare_replay_analysis


EASTERN = 'America/New_York'


def source_bars():
    frames = []
    for day in ('2026-09-04', '2026-09-07', '2026-09-08', '2026-09-09'):
        index = pd.date_range(day + ' 09:30', periods=78, freq='5min', tz=EASTERN)
        frames.append(pd.DataFrame({'Open': 100.0, 'High': 100.2, 'Low': 99.8, 'Close': 100.0, 'Volume': 1000}, index=index))
    index = pd.date_range('2026-09-10 09:30', periods=12, freq='5min', tz=EASTERN)
    close = pd.Series([100.0] * 3 + [101.0] * 3 + [102.0] * 3 + [500.0] * 3, index=index)
    frames.append(pd.DataFrame({'Open': close, 'High': close + 0.2, 'Low': close - 0.2, 'Close': close, 'Volume': 2000}, index=index))
    return pd.concat(frames)


def replay_history(frame, now):
    history = frame.copy()
    history.index = history.index + pd.Timedelta(minutes=5)
    return history[history.index <= now]


class LiveCandleParityTests(unittest.TestCase):
    def test_replay_excludes_unselected_stock_even_if_cached_data_is_supplied(self):
        controller = StrategyLabController(data_provider=Mock(name='Provider'))
        settings = StrategyLabSettings(symbols=['TEST'], period='1d', interval='5m', min_session_bars=2)
        with patch('backtester.controller.scan_replay_history', return_value={'Signal': 'WAIT'}) as scan:
            replay, signals, sessions = controller.replay_and_scan({'TEST': source_bars(), 'REMOVED': source_bars()}, settings)
        self.assertEqual(set(replay.symbol), {'TEST'})
        self.assertEqual(set(sessions.symbol), {'TEST'})
        self.assertTrue(signals.empty)
        self.assertTrue(scan.called)
        self.assertTrue(all(call.kwargs['symbol'] == 'TEST' for call in scan.call_args_list))

    def test_normal_replay_matches_live_native_15_minute_strategy_output(self):
        raw = source_bars()
        native = raw.resample('15min').agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}).dropna()
        now = pd.Timestamp('2026-09-10 10:15:00', tz=EASTERN)
        confirmation = {'volume_enabled': True, 'follow_through_enabled': True, 'min_breakout_volume_ratio': 1.5, 'min_volume_sessions': 3, 'require_extension': True}
        settings = StrategyLabSettings(symbols=['TEST'], interval='5m', min_score=70, min_session_bars=2, breakout_follow_through=confirmation)
        replay = _scan_strategy_replay('PMB', 'TEST', replay_history(raw, now), settings)
        with patch('bot_core.fetch_ib_intraday', return_value=native), patch('bot_core.fetch_ib_daily', return_value=pd.DataFrame()), patch('bot_core.datetime') as clock:
            clock.now.return_value = datetime(2026, 9, 10, 10, 15, tzinfo=now.tz)
            clock.fromisoformat.side_effect = datetime.fromisoformat
            live = scan_symbol_ib(None, 'TEST', min_score=70, min_session_bars=2, intraday_bar_size='15 mins', analysis_bar_minutes=15, breakout_follow_through=confirmation)
        self.assertIsNotNone(live)
        self.assertIsNotNone(replay)
        for key in ('Signal', 'Score', 'ORB High', 'ORB Low', 'VWAP', 'ATR %', 'EMA9', 'EMA21', 'Breakout Volume Ratio', 'Breakout Follow-through Confirmed'):
            self.assertEqual(replay.get(key), live.get(key), key)
        pd.testing.assert_frame_equal(replay['Intraday Data'], live['Intraday Data'], check_names=False, check_freq=False)
        self.assertEqual(replay['Signal'], 'CALL')
        self.assertTrue(replay['Breakout Follow-through Confirmed'])
        self.assertLess(replay['Price'], 500)  # The later 10:15 bucket is unseen.

    def test_incomplete_15_minute_bucket_does_not_leak_into_analysis(self):
        raw = source_bars()
        history = replay_history(raw, pd.Timestamp('2026-09-10 10:10', tz=EASTERN))
        analysis = prepare_replay_analysis(history, '5m', 15)
        self.assertEqual(analysis.index[-1], pd.Timestamp('2026-09-10 09:45', tz=EASTERN))
        self.assertEqual(analysis.iloc[-1].Close, 101)
        with patch('backtester.controller.scan_replay_history') as scan:
            self.assertIsNone(_scan_strategy_replay('PMB', 'TEST', history, StrategyLabSettings(symbols=['TEST'], min_session_bars=2)))
        scan.assert_not_called()

    def test_missing_source_candle_cannot_confirm_a_new_15_minute_signal(self):
        raw = source_bars().drop(pd.Timestamp('2026-09-10 10:05', tz=EASTERN))
        history = replay_history(raw, pd.Timestamp('2026-09-10 10:15', tz=EASTERN))
        with patch('backtester.controller.scan_replay_history') as scan:
            self.assertIsNone(_scan_strategy_replay('PMB', 'TEST', history, StrategyLabSettings(symbols=['TEST'], min_session_bars=2)))
        scan.assert_not_called()

    def test_minimum_session_bars_counts_current_analysis_candles(self):
        history = replay_history(source_bars(), pd.Timestamp('2026-09-10 10:15', tz=EASTERN))
        with patch('backtester.controller.scan_replay_history') as scan:
            self.assertIsNone(_scan_strategy_replay('PMB', 'TEST', history, StrategyLabSettings(symbols=['TEST'], min_session_bars=7)))
        scan.assert_not_called()  # Prior complete sessions cannot satisfy today's minimum.

    def test_replay_evaluates_each_new_normal_candle_once(self):
        controller = StrategyLabController(data_provider=Mock(name='Provider'))
        settings = StrategyLabSettings(symbols=['TEST'], period='1d', interval='5m', min_session_bars=2)
        with patch('backtester.controller.scan_replay_history', return_value={'Signal': 'WAIT'}) as scan:
            replay, signals, _ = controller.replay_and_scan({'TEST': source_bars()}, settings)
        today = replay[replay.timestamp.dt.date == pd.Timestamp('2026-09-10').date()]
        self.assertEqual(today[today.scanner_allowed].timestamp.dt.strftime('%H:%M').tolist(), ['10:00', '10:15', '10:30'])
        completed_calls = [call.kwargs['history'].index[-1] + pd.Timedelta(minutes=15) for call in scan.call_args_list]
        completed_calls = [timestamp for timestamp in completed_calls if timestamp.date() == pd.Timestamp('2026-09-10').date()]
        self.assertEqual([timestamp.strftime('%H:%M') for timestamp in completed_calls], ['10:00', '10:15', '10:30'])
        self.assertTrue(signals.empty)

    def test_opening_scan_keeps_one_minute_source_with_five_minute_analysis(self):
        index = pd.date_range('2026-09-10 09:31', periods=10, freq='min', tz=EASTERN)
        history = pd.DataFrame({'Open': 100, 'High': 101, 'Low': 99, 'Close': 100, 'Volume': 1000}, index=index)
        settings = StrategyLabSettings(symbols=['TEST'], interval='1m', min_session_bars=2, orb_minutes=5)
        with patch('backtester.controller.scan_replay_history', return_value={'Signal': 'WAIT'}) as scan:
            _scan_strategy_replay('PMB', 'TEST', history, settings, opening_stage=True)
        self.assertEqual(scan.call_args.kwargs['analysis_bar_minutes'], 5)
        self.assertEqual(len(scan.call_args.kwargs['history']), 10)
        self.assertEqual(scan.call_args.kwargs['history'].index[-1], pd.Timestamp('2026-09-10 09:39', tz=EASTERN))

    def test_warmup_is_loaded_but_cannot_generate_earlier_trades(self):
        provider = Mock()
        provider.load.return_value = source_bars()
        controller = StrategyLabController(data_provider=provider)
        settings = StrategyLabSettings(symbols=['TEST'], period='1d', interval='5m', min_session_bars=2)
        data, _ = controller.load_data(settings)
        self.assertEqual(provider.load.call_args.kwargs['period'], '8d')
        with patch('backtester.controller.scan_replay_history', return_value={'Signal': 'WAIT'}):
            replay, _, _ = controller.replay_and_scan(data, settings)
        self.assertGreaterEqual(replay.timestamp.min(), replay.timestamp.max() - pd.Timedelta(days=1))
        self.assertLess(data['TEST'].index.min(), replay.timestamp.min())

    def test_fourteen_day_run_has_prior_sessions_on_first_scan(self):
        # The selected period begins after a weekend and a market holiday.
        dates = pd.bdate_range('2026-08-18', '2026-09-08').difference(pd.DatetimeIndex(['2026-09-07']))
        raw = pd.concat([source_bars().iloc[:78].set_axis(
            pd.date_range(f'{day.date()} 09:30', periods=78, freq='5min', tz=EASTERN)
        ) for day in dates])
        provider = Mock()
        provider.load.return_value = raw
        with TemporaryDirectory() as export_dir:
            controller = StrategyLabController(data_provider=provider, export_dir=export_dir)
            settings = StrategyLabSettings(symbols=['TEST'], period='14d', interval='5m', min_session_bars=2)
            data, errors = controller.load_data(settings)
            with patch('backtester.controller.scan_replay_history', return_value={'Signal': 'WAIT'}) as scan:
                replay, signals, sessions = controller.replay_and_scan(data, settings)

        self.assertTrue(errors.empty)
        self.assertEqual(provider.load.call_args.kwargs['period'], '21d')
        cutoff = raw.index.max() + pd.Timedelta(minutes=5) - pd.Timedelta(days=14)
        self.assertTrue((replay.timestamp >= cutoff).all())
        self.assertEqual(set(sessions.session_date), set(replay.session_date))
        history = scan.call_args_list[0].kwargs['history']
        prior_dates = {day for day in history.index.date if day < history.index[-1].date()}
        self.assertGreaterEqual(len(prior_dates), 3)
        self.assertTrue(signals.empty)


if __name__ == '__main__':
    unittest.main()
