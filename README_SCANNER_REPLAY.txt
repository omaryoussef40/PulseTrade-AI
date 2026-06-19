Replace only these files in your existing TradingBot folder:
- bot_core.py
- dashboard.py
- engine.py

Adds:
- Scanner top-opportunity cards with CALL/PUT badges.
- Cleaner scanner output sections.
- Trade Replay Journal in Performance tab.
- Replay snapshots saved to exports/trade_replay.csv and exports/trade_snapshots/*.json.
- Engine records qualified signal-only setups and submitted entries.
- Entry trade log now includes key setup context: ORB, PDH/PDL, VWAP, RVOL, ATR, reasons.

Test:
python -m py_compile bot_core.py dashboard.py engine.py
streamlit run dashboard.py

Note:
Replay snapshots begin appearing after the engine runs and records qualified signals/entries.
