PulseTrade AI v0.9.1 - Strategy Lab Foundation

Replace/add the files from this release, then run:

python -m py_compile bot_core.py dashboard.py engine.py backtester/data.py backtester/replay.py backtester/strategy.py backtester/simulator.py backtester/controller.py backtester/metrics.py backtester/ui.py
streamlit run dashboard.py

What changed:
- Added backtester/controller.py as the single Strategy Lab orchestrator.
- Added backtester/ui.py so dashboard.py no longer owns the backtest pipeline.
- Added backtester/metrics.py for analytics helpers.
- Restored Strategy Lab tab reliably.
- Protected dashboard auto-refresh from interrupting long backtest jobs.
- Full backtest now runs as one pipeline: Yahoo data -> replay -> shared scanner -> signals -> option simulator -> metrics.
- Strategy Lab results persist to backtester/exports and reload after Streamlit reruns.
- Lower default simulated option premium to avoid 0-contract results with small accounts.

Notes:
- Yahoo/yfinance does not provide reliable historical option chains, so option P/L is an approximation.
- IBKR live execution is unchanged.
- This is a foundation release. Next release should focus on trade explorer and deeper analytics.
