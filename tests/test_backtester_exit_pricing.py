from datetime import time
import unittest

import pandas as pd

from backtester.simulator import OptionSimulationConfig, simulate_option_trades_with_decisions


EASTERN = 'America/New_York'


class HistoricalOptionExitPricingTests(unittest.TestCase):
    def run_trade(self, force_exit=True, stop_loss=90.0, slippage=0.0, falling=False):
        index = pd.date_range('2026-09-30 09:30', '2026-09-30 15:55', freq='5min', tz=EASTERN)
        stocks = pd.DataFrame({'Open': 100.0, 'High': 101.0, 'Low': 99.0, 'Close': 100.0, 'Volume': 1000}, index=index)
        premiums = pd.Series(10.0, index=index)
        # 14:20 start-labeled candle becomes available at the 14:25 exit.
        premiums.loc[pd.Timestamp('2026-09-30 14:20', tz=EASTERN)] = 11.0
        premiums.iloc[-1] = 8.0
        if falling:
            premiums.loc[pd.Timestamp('2026-09-30 10:25', tz=EASTERN)] = 7.0
        def provider(**kwargs):
            return {'expiry': '20261009', 'strike': 100.0, 'right': 'C'}, pd.DataFrame({'Close': premiums})
        config = OptionSimulationConfig(
            starting_capital=10000.0, sizing_method='fixed_dollar',
            max_spend_per_trade=2000.0, max_daily_capital=2000.0,
            max_trades_per_day=1, reserve_capital_for_remaining_trades=False,
            option_bars_provider=provider, force_exit_enabled=force_exit,
            force_exit_time=time(14, 25), stop_loss_pct=stop_loss,
            take_profit_pct=100.0, trailing_trigger_pct=100.0,
            breakeven_trigger_pct=100.0, slippage_pct=slippage,
        )
        signals = pd.DataFrame([{'timestamp': pd.Timestamp('2026-09-30 10:05', tz=EASTERN), 'symbol': 'MSFT', 'signal': 'CALL', 'price': 100.0}])
        trades, decisions = simulate_option_trades_with_decisions(signals, {'MSFT': stocks}, config)
        self.assertEqual(len(trades), 1)
        return trades.iloc[0], decisions.iloc[0]

    def test_forced_exit_uses_price_at_exit_not_later_close(self):
        trade, decision = self.run_trade()
        self.assertEqual(trade.exit_time, pd.Timestamp('2026-09-30 14:25', tz=EASTERN))
        self.assertEqual(trade.exit_premium, 11.0)
        self.assertEqual(trade.exit_reason, 'End-of-day forced exit')
        self.assertGreater(trade.realized_pnl, 0)
        self.assertEqual(decision.realized_pnl, trade.realized_pnl)

    def test_slippage_and_commissions_apply_to_scheduled_exit_price(self):
        trade, _ = self.run_trade(slippage=2.0)
        self.assertEqual(trade.entry_premium, 10.2)
        self.assertEqual(trade.exit_premium, 10.78)
        self.assertAlmostEqual(trade.realized_pnl, (10.78 - 10.2) * trade.quantity * 100 - trade.commissions)

    def test_session_close_still_uses_last_price_when_force_exit_disabled(self):
        trade, _ = self.run_trade(force_exit=False)
        self.assertEqual(trade.exit_time, pd.Timestamp('2026-09-30 16:00', tz=EASTERN))
        self.assertEqual(trade.exit_premium, 8.0)
        self.assertEqual(trade.exit_reason, 'Session close')

    def test_earlier_stop_fill_is_preserved(self):
        trade, _ = self.run_trade(stop_loss=20.0, falling=True)
        self.assertEqual(trade.exit_time, pd.Timestamp('2026-09-30 10:30', tz=EASTERN))
        self.assertEqual(trade.exit_premium, 8.0)
        self.assertEqual(trade.exit_reason, 'Stop loss hit')


if __name__ == '__main__':
    unittest.main()
