"""Timing, retry, and live/replay parity for selectable ORB confirmation."""
from datetime import datetime
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from backtester.controller import StrategyLabController, StrategyLabSettings, _scan_strategy_replay
from bot_core import scan_symbol_ib
from strategies.pmb.candles import confirmation_scan_interval_seconds
from strategies.pmb.strategy import evaluate_breakout_follow_through
from test_backtester_live_candles import source_bars, replay_history

EASTERN = 'America/New_York'


def config(mode, fresh=False):
    return dict(confirmation_mode=mode, fresh_breakout_enabled=fresh,
                follow_through_enabled=True, volume_enabled=False, require_extension=True)


@pytest.mark.parametrize('mode,clock,previous,minutes', [
    ('orb_5', '09:50', '09:49:59', 5),
    ('orb_15', '10:00', '09:59:59', 15),
    ('breakout_5', '10:05', '10:04:59', 5),
])
@pytest.mark.parametrize('direction', ['CALL', 'PUT'])
def test_modes_match_live_and_replay_at_first_completed_confirmation(mode, clock, previous, minutes, direction):
    raw = source_bars()
    if direction == 'PUT':
        high, low = raw.High.copy(), raw.Low.copy()
        raw['High'], raw['Low'] = 200 - low, 200 - high
        raw['Open'], raw['Close'] = 200 - raw.Open, 200 - raw.Close
    cfg = config(mode)
    settings = StrategyLabSettings(symbols=['TEST'], interval='5m', min_score=70,
                                   min_session_bars=2, breakout_follow_through=cfg)
    now = pd.Timestamp('2026-09-10 ' + clock, tz=EASTERN)
    replay = _scan_strategy_replay('PMB', 'TEST', replay_history(raw, now), settings)
    def live_at(timestamp):
        with patch('bot_core.fetch_ib_intraday', side_effect=lambda *args, **kwargs: raw.resample('15min').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna() if kwargs['bar_size'] == '15 mins' else raw) as fetch, patch('bot_core.fetch_ib_daily', return_value=pd.DataFrame()), patch('bot_core.datetime') as wallclock:
            wallclock.now.return_value = timestamp.to_pydatetime()
            wallclock.fromisoformat.side_effect = datetime.fromisoformat
            result = scan_symbol_ib(None, 'TEST', min_score=70, min_session_bars=2,
                                    intraday_bar_size='15 mins', analysis_bar_minutes=15,
                                    breakout_follow_through=cfg)
        return result, fetch
    live, fetch = live_at(now)
    assert live['Signal'] == replay['Signal'] == direction
    assert live['Confirmation Candle Minutes'] == minutes
    assert clock in live['Confirmation Closed At']
    for key in ('Signal', 'Price', 'Score', 'VWAP', 'Above VWAP', 'ORB High', 'ORB Low', 'Confirmation Closed At'):
        assert live[key] == replay[key], key
    if minutes == 5:
        assert fetch.call_args.kwargs['bar_size'] == '5 mins'
    earlier, _ = live_at(pd.Timestamp('2026-09-10 ' + previous, tz=EASTERN))
    assert earlier is None or earlier['Signal'] == 'WAIT'
    later, _ = live_at(now + pd.Timedelta(minutes=5 if mode != 'orb_15' else 15))
    assert later['Signal'] == 'WAIT'
    assert confirmation_scan_interval_seconds(900, cfg) == minutes * 60


@pytest.mark.parametrize('mode,clock', [('orb_5', '09:50'), ('orb_15', '10:00'), ('breakout_5', '10:05')])
def test_full_replay_records_first_entry_at_selected_time(mode, clock):
    settings = StrategyLabSettings(symbols=['TEST'], period='1d', interval='5m',
                                   first_signal_minutes=20, min_session_bars=2,
                                   min_score=70, min_atr=0, use_sr_filter=False,
                                   breakout_follow_through=config(mode))
    _, signals, _ = StrategyLabController(data_provider=Mock()).replay_and_scan({'TEST': source_bars()}, settings)
    today = signals[signals.timestamp.dt.date == pd.Timestamp('2026-09-10').date()]
    assert len(today) == 1
    assert today.iloc[0].timestamp.strftime('%H:%M') == clock


def retry_frames(inside=True, mirror=False):
    index = pd.date_range('2026-09-10 09:30', periods=6, freq='15min', tz=EASTERN)
    close = [100, 102, 100 if inside else 102, 103, 104, 105]
    analysis = pd.DataFrame(dict(Open=close, High=[x+.2 for x in close], Low=[x-.2 for x in close], Close=close, Volume=2000), index=index)
    source_index = pd.date_range(index[0], periods=18, freq='5min')
    source_close = [100]*3 + [102]*3 + [100 if inside else 102]*3 + [103]*3 + [104]*3 + [105]*3
    source = pd.DataFrame(dict(Open=source_close, High=[x+.2 for x in source_close], Low=[x-.2 for x in source_close], Close=source_close, Volume=700), index=source_index)
    if mirror:
        for frame in (analysis, source):
            high, low = frame.High.copy(), frame.Low.copy()
            frame['High'], frame['Low'] = 200-low, 200-high
            frame['Open'], frame['Close'] = 200-frame.Open, 200-frame.Close
    return analysis, source


@pytest.mark.parametrize('mirror', [False, True])
def test_failed_confirmation_can_reset_and_confirm_a_fresh_breakout(mirror):
    analysis, source = retry_frames(mirror=mirror)
    now = pd.Timestamp('2026-09-10 10:35', tz=EASTERN)
    analysis = analysis[analysis.index + pd.Timedelta(minutes=15) <= now]
    source = source[source.index + pd.Timedelta(minutes=5) <= now]
    args = dict(direction='PUT' if mirror else 'CALL', orb_high=101, orb_low=99,
                orb_bars=1, confirmation_intraday=source, confirmation_source_minutes=5, as_of=now)
    legacy = evaluate_breakout_follow_through(analysis, analysis, config=config('breakout_5'), **args)
    fresh = evaluate_breakout_follow_through(analysis, analysis, config=config('breakout_5', True), **args)
    assert not legacy['confirmed']
    assert fresh['confirmed']
    assert fresh['breakout_time'].strftime('%H:%M') == '10:15'
    assert fresh['confirmation_closed_at'] == now


def test_continuous_outside_closes_do_not_create_a_fresh_breakout():
    analysis, source = retry_frames(inside=False)
    now = pd.Timestamp('2026-09-10 10:35', tz=EASTERN)
    result = evaluate_breakout_follow_through(analysis.iloc[:4], analysis.iloc[:4], direction='CALL',
        orb_high=101, orb_low=99, orb_bars=1, config=config('breakout_5', True),
        confirmation_intraday=source[source.index+pd.Timedelta(minutes=5)<=now], confirmation_source_minutes=5, as_of=now)
    assert not result['confirmed']
    assert result['breakout_time'].strftime('%H:%M') == '09:45'


def test_fresh_breakout_rechecks_the_new_candles_volume():
    analysis, source = retry_frames()
    now = pd.Timestamp('2026-09-10 10:35', tz=EASTERN)
    today = analysis.iloc[:4]
    previous = []
    for days in [1, 2, 3]:
        frame = today.copy()
        frame.index -= pd.Timedelta(days=days)
        frame['Volume'] = 2000
        previous.append(frame)
    result = evaluate_breakout_follow_through(pd.concat([*previous, today]).sort_index(), today,
        direction='CALL', orb_high=101, orb_low=99, orb_bars=1,
        config={**config('breakout_5', True), 'volume_enabled':True, 'min_breakout_volume_ratio':1.5},
        confirmation_intraday=source[source.index+pd.Timedelta(minutes=5)<=now], confirmation_source_minutes=5, as_of=now)
    assert result['breakout_time'].strftime('%H:%M') == '10:15'
    assert result['volume_ratio'] == 1
    assert not result['confirmed']

@pytest.mark.parametrize('mode', ['orb_5', 'orb_15', 'breakout_5'])
def test_new_controls_save_and_apply_to_live(mode):
    from streamlit.testing.v1 import AppTest
    from test_backtester_stock_universe import SMOKE_APP
    app = AppTest.from_string(SMOKE_APP).run(timeout=20)
    app.checkbox(key='sl_next_candle_confirmation_enabled').check().run(timeout=20)
    app.selectbox(key='sl_confirmation_mode').select(mode).run(timeout=20)
    app.checkbox(key='sl_fresh_breakout_enabled').check().run(timeout=20)
    assert not app.exception
    app.button(key='sl_save_settings').click().run(timeout=20)
    saved = app.session_state['test_config']['strategy_lab']['breakout_follow_through']
    assert saved['confirmation_mode'] == mode
    assert saved['fresh_breakout_enabled']
    app.checkbox(key='sl_apply_confirm').check().run(timeout=20)
    app.button(key='sl_apply_to_engine').click().run(timeout=20)
    assert not app.exception
    live = app.session_state['test_config']['strategy']['breakout_follow_through']
    assert live == saved


def test_fresh_breakout_live_replay_parity_after_a_failed_attempt():
    raw = source_bars()
    index = pd.date_range('2026-09-10 09:30', periods=18, freq='5min', tz=EASTERN)
    closes = [100]*3 + [101]*3 + [100]*3 + [102]*3 + [103]*6
    today = pd.DataFrame(dict(Open=closes, High=[x+.2 for x in closes], Low=[x-.2 for x in closes], Close=closes, Volume=2000), index=index)
    raw = pd.concat([raw[raw.index.date < index[0].date()], today])
    now = pd.Timestamp('2026-09-10 10:35', tz=EASTERN)
    cfg = {**config('breakout_5', True), 'require_extension':False}
    settings = StrategyLabSettings(symbols=['TEST'], interval='5m', min_session_bars=2, min_score=70, breakout_follow_through=cfg)
    replay = _scan_strategy_replay('PMB', 'TEST', replay_history(raw, now), settings)
    with patch('bot_core.fetch_ib_intraday', return_value=raw), patch('bot_core.fetch_ib_daily', return_value=pd.DataFrame()), patch('bot_core.datetime') as wallclock:
        wallclock.now.return_value = now.to_pydatetime()
        wallclock.fromisoformat.side_effect = datetime.fromisoformat
        live = scan_symbol_ib(None, 'TEST', min_session_bars=2, min_score=70, intraday_bar_size='15 mins', analysis_bar_minutes=15, breakout_follow_through=cfg)
    assert live['Signal'] == replay['Signal'] == 'CALL'
    for key in ['Confirmation Breakout Time', 'Confirmation Closed At', 'Price', 'Score']:
        assert live[key] == replay[key]
    assert '10:15' in live['Confirmation Breakout Time']


def test_inside_wick_without_completed_inside_close_does_not_reset():
    analysis, source = retry_frames(inside=False)
    analysis.loc[analysis.index[2], 'Low'] = 100
    source.loc[source.index[6:9], 'Low'] = 100
    now = pd.Timestamp('2026-09-10 10:35', tz=EASTERN)
    result = evaluate_breakout_follow_through(analysis.iloc[:4], analysis.iloc[:4], direction='CALL', orb_high=101, orb_low=99,
        orb_bars=1, config=config('breakout_5', True), confirmation_intraday=source[source.index+pd.Timedelta(minutes=5)<=now],
        confirmation_source_minutes=5, as_of=now)
    assert not result['confirmed']
    assert result['breakout_time'].strftime('%H:%M') == '09:45'

@pytest.mark.parametrize('mode', ['orb_5', 'orb_15', 'breakout_5'])
def test_live_settings_save_the_new_controls_explicitly(mode):
    from streamlit.testing.v1 import AppTest
    from test_workspace_ui import SETTINGS_APP
    app = AppTest.from_string(SETTINGS_APP).run(timeout=20)
    app.radio[0].set_value('Strategy').run(timeout=20)
    original = app.session_state['config']['strategy'].get('breakout_follow_through')
    next(widget for widget in app.checkbox if widget.label == 'Require breakout confirmation').check().run(timeout=20)
    app.selectbox(key='live_confirmation_mode').select(mode).run(timeout=20)
    app.checkbox(key='live_fresh_breakout_enabled').check().run(timeout=20)
    assert not app.exception
    assert app.session_state['config']['strategy'].get('breakout_follow_through') == original
    app.button(key='save_platform_Strategy').click().run(timeout=20)
    assert not app.exception
    saved = app.session_state['config']['strategy']['breakout_follow_through']
    assert saved['confirmation_mode'] == mode
    assert saved['fresh_breakout_enabled']
    assert saved['follow_through_enabled']
