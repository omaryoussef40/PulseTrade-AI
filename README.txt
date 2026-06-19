Replace only dashboard.py in your existing TradingBot folder.

What this update does:
- Keeps Performance visually populated even when there are no trades yet.
- Adds placeholder metrics, disabled filters, empty preview charts, and a replay journal placeholder.
- Does not change scanner, engine, IBKR, order, or risk logic.

Test:
python -m py_compile bot_core.py dashboard.py engine.py
streamlit run dashboard.py
