import pytest


@pytest.fixture(autouse=True)
def isolate_pine_backtest_preferences(tmp_path,monkeypatch):
    """UI tests must never read or overwrite the user's saved backtest state."""
    from backtester import pine_store
    monkeypatch.setattr(pine_store,'STORE_DIR',tmp_path/'pine_backtest_state')
    monkeypatch.setattr(pine_store,'latest_preferences',lambda:{})
