Yahoo Backtester module

Replace / add these files in your TradingBot folder:
- Add: backtester.py
- Replace: dashboard.py
- Replace: requirements.txt

What it adds:
- New dashboard tab: Yahoo Backtester
- Fetches historical intraday stock bars from yfinance
- Replays the same core strategy logic: ORB, VWAP, EMA9/EMA21, PDH/PDL, ATR, optional RVOL
- Selects top N setups per day, using your risk controls
- Simulates 7DTE option-style P/L with configurable delta and premium estimate
- Applies stop loss, take profit, breakeven, trailing stop, and end-of-day exit
- Shows equity curve, trade P/L, symbol P/L, metrics, and CSV download

Important:
Yahoo does not provide reliable historical intraday option chains, so option P/L is modeled from stock movement using the selected delta and premium percentage. Use this for strategy efficiency testing, not exact option fill simulation.

Install/update dependencies:
pip install -r requirements.txt

Run:
streamlit run dashboard.py

Test:
python -m py_compile bot_core.py dashboard.py engine.py backtester.py
