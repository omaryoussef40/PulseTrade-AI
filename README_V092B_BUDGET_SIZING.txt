PulseTrade AI v0.9.2b - Strategy Lab Budget-Based Contract Sizing

Replace these files:
- backtester/simulator.py
- backtester/controller.py
- backtester/ui.py

What changed:
- Strategy Lab no longer caps simulated trades at 2 contracts.
- max_contracts=0 now means unlimited contract cap in Strategy Lab.
- Contract quantity is calculated from Max spend/trade USD:
    quantity = floor(max_spend_per_trade / (entry_premium * 100))
- Max daily capital still limits total daily exposure.
- Live/paper IBKR trading engine sizing is not changed.

Important:
If Max daily capital is lower than Max spend/trade, trades may still be skipped.
For example, Max spend/trade = $5,000 but Max daily capital = $500 will prevent large trades.
