Phase 2 Backtester Update

Replace:
- dashboard.py

Keep/add backtester folder:
- backtester/__init__.py
- backtester/data.py
- backtester/replay.py

What changed:
- Backtester tab now has two separate steps:
  1) Download Historical Data
  2) Replay Market
- Data is cached locally through yfinance.
- Replay runs candle-by-candle with progress, live event log, replay speed, sessions table, and CSV export.
- No trades are simulated yet and no IBKR connection is used.

Requirements:
- yfinance
- pyarrow

Test:
python -m py_compile bot_core.py dashboard.py engine.py backtester/data.py backtester/replay.py
streamlit run dashboard.py
