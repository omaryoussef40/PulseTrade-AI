"""Safe Streamlit smoke target for the research-only GAP controls."""

import backtester.ui as strategy_lab_ui


class _NoConnectProvider:
    name = "Smoke"


strategy_lab_ui.save_config = None
strategy_lab_ui.available_market_data_sources = lambda: ["Smoke"]
strategy_lab_ui.create_market_data_provider = lambda *args, **kwargs: _NoConnectProvider()

SMOKE_CONFIG = {
    "strategy": {"min_score": 70, "min_rvol": 1.5, "min_atr": 0.3},
    "risk": {"account_size": 10_000, "max_trades_per_day": 2},
    "strategy_lab": {
        "symbols": ["TEST"],
        "selected_strategies": ["GAP"],
        "period": "7d",
        "interval": "5m",
        "max_symbols": 1,
        "data_source": "Smoke",
        "gap": {},
    },
}

strategy_lab_ui.render_strategy_lab_tab(SMOKE_CONFIG, ["TEST"])
