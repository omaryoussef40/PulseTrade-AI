Replace only these files in your existing TradingBot folder:
- bot_core.py
- dashboard.py

Adds:
- Performance period filters: Today, This Week, This Month, All Time, Custom Range
- Symbol / CALL-PUT / Winners-Losers filters
- Summary metrics: Net P/L, Win Rate, Profit Factor, Avg Trade, Avg Winner/Loser, Largest Win/Loss
- Charts: Equity Curve, Daily P/L, Trade P/L, Symbol P/L
- Filtered trade journal with CSV download

Test:
python -m py_compile bot_core.py dashboard.py engine.py
streamlit run dashboard.py
