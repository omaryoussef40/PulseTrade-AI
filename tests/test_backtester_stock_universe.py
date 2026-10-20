import unittest

from streamlit.testing.v1 import AppTest


SMOKE_APP = '''
import pandas as pd
import streamlit as st
from unittest.mock import patch
import backtester.ui as ui

class Provider:
    name = 'Smoke'

class Controller:
    provider_name = 'Smoke'
    def __init__(self, **kwargs): pass
    def cache_info(self): return pd.DataFrame()
    def load_last_result(self): return {}
    def run(self, settings, **kwargs):
        st.session_state['tested_run_symbols'] = settings.symbols
        st.session_state['tested_run_interval'] = settings.interval
        return {'meta': {'status': 'COMPLETE'}}
    def disconnect_providers(self): pass

if 'test_config' not in st.session_state:
    st.session_state['test_config'] = {
        'watchlist': list(ui.WATCHLIST),
        'strategy': {}, 'risk': {},
        'strategy_lab': {'symbols': list(ui.WATCHLIST), 'max_symbols': 34, 'data_source': 'Smoke'}
    }
with patch.multiple(ui, save_config=lambda config: None,
                    available_market_data_sources=lambda: ['Smoke'],
                    create_market_data_provider=lambda *args, **kwargs: Provider(),
                    StrategyLabController=Controller):
    ui.render_strategy_lab_tab(st.session_state['test_config'], list(ui.WATCHLIST))
'''


class PresetWatchlistUITests(unittest.TestCase):
    def test_removing_presets_after_custom_list_excludes_removed_tickers(self):
        app = AppTest.from_string(SMOKE_APP).run(timeout=20)
        app.text_area(key='sl_custom_symbols').set_value('NVDA, AMD, TSLA').run(timeout=20)
        self.assertEqual(app.multiselect(key='sl_symbol_presets').value, ['NVDA', 'AMD', 'TSLA'])
        app.multiselect(key='sl_symbol_presets').set_value(['NVDA']).run(timeout=20)
        self.assertEqual(app.text_area(key='sl_custom_symbols').value, '')
        app.button(key='sl_run').click().run(timeout=20)
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state['tested_run_symbols'], ['NVDA'])
        app.multiselect(key='sl_symbol_presets').set_value([]).run(timeout=20)
        app.button(key='sl_run').click().run(timeout=20)
        self.assertTrue(any('Add at least one symbol' in item.value for item in app.warning))

    def test_previous_trade_counts_hidden_after_selection_changes(self):
        import pandas as pd
        app = AppTest.from_string(SMOKE_APP).run(timeout=20)
        app.session_state['sl_last_result'] = {
            'meta': {'status': 'COMPLETE', 'symbols_requested': ['NVDA', 'AMD']},
            'replay': pd.DataFrame({'symbol': ['NVDA', 'AMD']}),
            'metrics': {'total_trades': 99},
        }
        app.multiselect(key='sl_symbol_presets').set_value(['NVDA']).run(timeout=20)
        self.assertFalse(app.exception)
        self.assertTrue(any('Ticker selection changed' in item.value for item in app.info))
        self.assertFalse(any(item.label == 'Trades' for item in app.metric))
        app.button(key='sl_run').click().run(timeout=20)
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state['tested_run_symbols'], ['NVDA'])
        self.assertFalse(any('Ticker selection changed' in item.value for item in app.info))

    def test_only_presets_are_available_and_run_receives_all_34(self):
        app = AppTest.from_string(SMOKE_APP).run(timeout=20)
        self.assertFalse(app.exception)
        presets = list(app.multiselect(key='sl_symbol_presets').value)
        self.assertEqual(len(presets), 34)
        self.assertFalse(any(s.key == 'sl_symbol_universe' for s in app.selectbox))
        self.assertFalse(any(t.key == 'sl_extra_symbols' for t in app.text_input))
        app.button(key='sl_run').click().run(timeout=20)
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state['tested_run_symbols'], presets)

    def test_existing_index_selections_restore_presets(self):
        for old_value in ('sp500', 'sp500_top100'):
            with self.subTest(old_value=old_value):
                script = SMOKE_APP.replace("'max_symbols': 34,", f"'symbol_universe': '{old_value}', 'max_symbols': 34,")
                app = AppTest.from_string(script)
                app.session_state['sl_symbol_universe'] = old_value
                app.session_state['sl_symbol_presets'] = ['AAPL']
                app.run(timeout=20)
                self.assertFalse(app.exception)
                self.assertEqual(len(app.multiselect(key='sl_symbol_presets').value), 34)
                app.button(key='sl_save_settings').click().run(timeout=20)
                self.assertEqual(app.session_state['test_config']['strategy_lab']['symbol_universe'], 'watchlist')
                app.button(key='sl_run').click().run(timeout=20)
                self.assertEqual(len(app.session_state['tested_run_symbols']), 34)

    def test_user_can_still_limit_the_preset_run(self):
        app = AppTest.from_string(SMOKE_APP).run(timeout=20)
        app.number_input(key='sl_max_symbols').set_value(5).run(timeout=20)
        app.button(key='sl_run').click().run(timeout=20)
        self.assertFalse(app.exception)
        self.assertEqual(len(app.session_state['tested_run_symbols']), 5)

    def test_custom_list_can_run_fifty_unique_symbols_and_reload(self):
        tickers = ['NVDA', 'TSLA', 'AAPL', 'AMZN', 'MSFT', 'INTC', 'MU', 'PLTR', 'META', 'MSTR',
                   'AMD', 'NFLX', 'GOOGL', 'SMCI', 'SOFI', 'MARA', 'IREN', 'SPCX', 'ORCL', 'AVGO',
                   'HOOD', 'NOK', 'COIN', 'GOOG', 'NBIS', 'CRWV', 'ONDS', 'TSM', 'BMNR', 'BABA',
                   'SNDK', 'MRVL', 'GME', 'ASTS', 'HIMS', 'WULF', 'NOW', 'BAC', 'RKLB', 'OPEN',
                   'RIVN', 'MRNA', 'WMT', 'NKE', 'F', 'CRCL', 'AI', 'PFE', 'AAL', 'PYPL']
        app = AppTest.from_string(SMOKE_APP).run(timeout=20)
        self.assertEqual(app.number_input(key='sl_max_symbols').max, 50)
        app.number_input(key='sl_max_symbols').set_value(50)
        app.text_area(key='sl_custom_symbols').set_value(', '.join(tickers + ['nvda'])).run(timeout=20)
        app.button(key='sl_save_settings').click().run(timeout=20)
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state['test_config']['strategy_lab']['symbols'], tickers)
        app.button(key='sl_run').click().run(timeout=20)
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state['tested_run_symbols'], tickers)
        presets = app.multiselect(key='sl_symbol_presets')
        self.assertTrue(set(tickers).issubset(presets.options))
        self.assertEqual(len(presets.options), len(set(presets.options)))
        app.text_area(key='sl_custom_symbols').set_value('')
        presets.set_value(tickers).run(timeout=20)
        app.button(key='sl_run').click().run(timeout=20)
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state['tested_run_symbols'], tickers)
        script = SMOKE_APP.replace("'symbols': list(ui.WATCHLIST), 'max_symbols': 34", f"'symbols': {tickers!r}, 'max_symbols': 50")
        restored = AppTest.from_string(script).run(timeout=20)
        self.assertEqual(restored.text_area(key='sl_custom_symbols').value, '')
        self.assertEqual(restored.multiselect(key='sl_symbol_presets').value, tickers)
        restored.button(key='sl_run').click().run(timeout=20)
        self.assertFalse(restored.exception)
        self.assertEqual(restored.session_state['tested_run_symbols'], tickers)

    def test_staged_timeline_selects_one_minute_history(self):
        app = AppTest.from_string(SMOKE_APP).run(timeout=20)
        app.checkbox(key='sl_staged_timeline').check().run(timeout=20)
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key='sl_interval').value, '1m')
        self.assertTrue(app.selectbox(key='sl_interval').disabled)
        app.button(key='sl_run').click().run(timeout=20)
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state['tested_run_interval'], '1m')

    def test_confirmation_choice_saves_and_applies_to_live_engine(self):
        script = SMOKE_APP.replace("'data_source': 'Smoke'", "'data_source': 'Smoke', 'interval': '15m'")
        app = AppTest.from_string(script).run(timeout=20)
        self.assertEqual(app.selectbox(key='sl_confirmation_minutes').value, 15)
        app.checkbox(key='sl_next_candle_confirmation_enabled').check().run(timeout=20)
        app.selectbox(key='sl_confirmation_minutes').select(5).run(timeout=20)
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key='sl_interval').value, '5m')
        app.button(key='sl_save_settings').click().run(timeout=20)
        saved = app.session_state['test_config']['strategy_lab']['breakout_follow_through']
        self.assertEqual(saved['confirmation_minutes'], 5)
        app.checkbox(key='sl_apply_confirm').check().run(timeout=20)
        app.button(key='sl_apply_to_engine').click().run(timeout=20)
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state['test_config']['strategy']['breakout_follow_through']['confirmation_minutes'], 5)
        app.selectbox(key='sl_confirmation_minutes').select(15).run(timeout=20)
        app.button(key='sl_save_settings').click().run(timeout=20)
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state['test_config']['strategy_lab']['breakout_follow_through']['confirmation_minutes'], 15)


if __name__ == '__main__':
    unittest.main()
