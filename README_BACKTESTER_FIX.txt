Replace only dashboard.py.

Fix:
- Phase 3 Yahoo replay results are now saved to disk under backtester/exports/.
- Last Replay Result reloads after Streamlit reruns/autorefresh.
- Clear Yahoo Cache also clears the last replay result files.

Test:
python -m py_compile bot_core.py dashboard.py engine.py backtester/data.py backtester/replay.py backtester/strategy.py
streamlit run dashboard.py
