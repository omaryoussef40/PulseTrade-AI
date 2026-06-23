PulseTrade AI - Simple Strategy Modules

Add / replace these files in your existing PulseTrade-AI folder.

What changed:
- PMB is now its own clean strategy module under strategies/pmb/.
- BRT folder exists as a safe placeholder but does not affect live trading yet.
- Existing live engine, IBKR execution, risk management, option selection, and trade management are not redesigned.
- Existing backtester imports still work through backtester/strategy.py.
- Dashboard has a simple Active strategy selector in Signal Filters.

Important:
- Keep Active strategy = pmb for live/paper trading.
- BRT returns no signal for now. It is only there to keep the folder structure ready.
- config.json does not need to be replaced. The default config now handles active_strategy automatically.

Test:
python -m py_compile bot_core.py dashboard.py engine.py backtester/strategy.py backtester/controller.py backtester/ui.py strategies/registry.py strategies/pmb/strategy.py strategies/brt/strategy.py
streamlit run dashboard.py
