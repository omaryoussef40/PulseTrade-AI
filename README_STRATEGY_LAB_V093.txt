PulseTrade AI Strategy Lab v0.9.3 - Broker Simulation Engine

Changed files:
- backtester/simulator.py
- backtester/controller.py
- backtester/ui.py

What changed:
- Replaced independent trade sizing with a broker-style account model.
- Added cash, buying power, equity, commissions and max drawdown tracking.
- Position sizing now uses the minimum of:
  1) Max Spend / Trade
  2) Current Buying Power
  3) Remaining Daily Capital
- Contract cost is calculated correctly as: option premium * 100 + commission.
- Execution Decisions now include buying power before entry, after entry and after exit.
- Simulated Trades now include account equity and buying-power fields.
- Strategy Lab UI now includes a Position Sizing Preview and warnings for inconsistent capital settings.

Notes:
- Option premium is still estimated from Yahoo stock candles. It is not yet real historical option pricing.
- This improves account/execution realism, but exact P/L remains approximate until IBKR historical option pricing is integrated.
