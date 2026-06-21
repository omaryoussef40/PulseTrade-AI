Phase 4 Yahoo Backtester Update

Replace:
- dashboard.py

Add:
- backtester/simulator.py

Keep existing:
- backtester/__init__.py
- backtester/data.py
- backtester/replay.py
- backtester/strategy.py
- bot_core.py
- engine.py

What this adds:
- Step 3 in the Backtester tab: simulated 7-DTE option trades from historical Yahoo scanner signals.
- Approximate 0.50-delta CALL/PUT option pricing model.
- Stop loss, take profit, breakeven, trailing stop, end-of-day forced exit.
- Max trades/day, max spend/trade, max daily capital, max contracts.
- Performance metrics: Net P/L, Return %, Win Rate, Profit Factor, Max Drawdown, Trades.
- Simulated equity curve.
- Simulated trades table and CSV download.
- Results persist under backtester/exports/.

Test:
python -m py_compile bot_core.py dashboard.py engine.py backtester/data.py backtester/replay.py backtester/strategy.py backtester/simulator.py
streamlit run dashboard.py
