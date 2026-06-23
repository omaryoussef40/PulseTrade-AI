PMB Production Patch

Replace these files/folders in your existing PulseTrade-AI project:

Root files:
- bot_core.py
- engine.py
- dashboard.py
- config.json

Strategy files:
- strategies/registry.py
- strategies/pmb/strategy.py
- strategies/pmb/config.json
- strategies/pmb/README.md

Backtester parity files:
- backtester/strategy.py
- backtester/controller.py
- backtester/ui.py

What changed:
- PMB is the only active production strategy.
- BRT remains present but cannot affect trading.
- Live scanner now uses strategies/pmb/strategy.py directly through the registry.
- Strategy Lab and live scanner now share the same PMB v2 strategy path.
- PMB v2 defaults to 15-minute ORB and first signal minute 20.
- Confidence is no longer a trade blocker.
- Filtering is now: Signal CALL/PUT, Score >= min score, ATR >= min ATR, optional RVOL filter.
- Setup cards and Telegram alerts show Score + Grade + Setup Quality.
- config.json is delivered safe by default: Simulation mode, automation disabled, order placement disabled.

Before live trading:
1. Run python -m py_compile bot_core.py engine.py dashboard.py strategies/registry.py strategies/pmb/strategy.py backtester/strategy.py backtester/controller.py backtester/ui.py
2. Start dashboard: streamlit run dashboard.py
3. Connect IBKR in Paper mode first.
4. Run scanner manually and verify TSLA/NVDA/AMD-style PMB setups show Score + Grade.
5. Enable automation only after Paper test.
6. Keep Live mode locked until you intentionally type TRADE LIVE in the dashboard.
