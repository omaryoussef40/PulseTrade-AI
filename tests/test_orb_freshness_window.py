import pandas as pd
import pytest
from strategies.pmb.strategy import evaluate_breakout_follow_through


@pytest.mark.parametrize('side', ['CALL', 'PUT'])
@pytest.mark.parametrize('window', [5, 10, 15])
def test_direct_freshness_boundaries_and_completed_confirmation(side, window):
    index = pd.date_range('2026-10-28 09:30', periods=10, freq='5min', tz='America/New_York')
    close = 102 if side == 'CALL' else 98
    frame = pd.DataFrame({'Open':100., 'High':103., 'Low':97., 'Close':[100.]*3+[close]*7, 'Volume':1000}, index=index)
    end = index[3] + pd.Timedelta(minutes=5)
    config = {'volume_enabled':False, 'follow_through_enabled':True, 'confirmation_mode':'orb_5', 'freshness_minutes':window}
    def evaluate(now, data=frame):
        return evaluate_breakout_follow_through(data, data, direction=side, orb_high=101, orb_low=99, orb_bars=3, config=config, confirmation_source_minutes=5, as_of=now)
    assert not evaluate(end-pd.Timedelta(seconds=1))['confirmed']
    for age in range(0, window, 5):
        result = evaluate(end+pd.Timedelta(minutes=age))
        assert result['confirmed']
        assert result['evaluated_close_at'] == end+pd.Timedelta(minutes=age)
    assert evaluate(end+pd.Timedelta(minutes=window))['reason'] == 'Follow-through confirmation is stale'
    if window > 5:
        inside = frame.copy()
        inside.loc[index[4], 'Close'] = 100
        assert not evaluate(end+pd.Timedelta(minutes=5), inside)['confirmed']
