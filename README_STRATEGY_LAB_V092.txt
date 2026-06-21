PulseTrade AI Strategy Lab v0.9.2

Focus: Execution Pipeline transparency.

Replace/add these files:
- dashboard.py
- requirements.txt
- backtester/__init__.py
- backtester/controller.py
- backtester/data.py
- backtester/metrics.py
- backtester/replay.py
- backtester/simulator.py
- backtester/strategy.py
- backtester/ui.py

What changed:
- Every scanner signal now receives a final execution status.
- New Execution Decisions tab shows TRADED, SKIPPED, or REJECTED.
- Simulator no longer silently ignores signals.
- Rejection reasons include max trades/day, duplicate symbol/day, no data, no future candles, max daily capital, or position size = 0.
- Default Entry Premium % lowered from 0.80% to 0.25% to avoid 0-contract simulations on high-priced symbols with a $250 max spend.
- Execution decisions are exported to backtester/exports/strategy_lab_signal_decisions.csv.

Run:
python -m py_compile bot_core.py dashboard.py engine.py backtester/data.py backtester/replay.py backtester/strategy.py backtester/simulator.py backtester/controller.py backtester/metrics.py backtester/ui.py
streamlit run dashboard.py
