Phase 1 Trade Journal Foundation

Replace these files in your existing TradingBot folder:
- bot_core.py
- dashboard.py

What this adds:
- SQLite trade journal at database/trades.db
- Automatic mirroring of trade entries, exits, signal-only setups, and replay snapshots into the database
- Trade lifecycle records: SIGNAL_ONLY / OPEN / CLOSED
- Event timeline table for every journal event
- Performance tab now uses the database as the main source
- Metrics added: expectancy, max drawdown, average R, return %, R multiple
- Charts added: equity curve, drawdown curve, daily P/L, symbol P/L, calendar heatmap
- CSV logs are still kept as backup/export files
- One-click Backfill CSV → DB button in the Performance tab

Test:
python -m py_compile bot_core.py dashboard.py engine.py
streamlit run dashboard.py

After replacement:
1. Start the dashboard.
2. Open the Performance tab.
3. Click Backfill CSV → DB if you already have CSV logs.
4. Run the engine normally. New signals/trades will automatically enter the database.

Important:
No order logic was changed. This update only adds database logging and performance analytics foundation.
