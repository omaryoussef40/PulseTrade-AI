import pandas as pd
import pytest
from strategies.pmb.strategy import evaluate_breakout_follow_through


@pytest.mark.parametrize('side', ['CALL', 'PUT'])
@pytest.mark.parametrize('window', [5, 10, 15])
def test_crossing_then_body_confirmation_and_freshness(side, window):
    idx = pd.date_range('2026-11-02 09:30', periods=10, freq='5min', tz='America/New_York')
    # ORB breaks at 09:50; PDH crosses at 09:55; next body confirms at 10:00.
    df = pd.DataFrame({'Open':[100,100,100,100,102,104,104,104,104,104],
                       'Close':[100,100,100,102,104,104,104,104,104,104],
                       'High':106.,'Low':98.,'Volume':1000},index=idx)
    if side == 'PUT':
        df['Open'], df['Close'] = 200-df.Open, 200-df.Close
    cfg = {'confirmation_mode':'orb_5','follow_through_enabled':True,'volume_enabled':False,'freshness_minutes':window}
    def check(clock, frame=df):
        return evaluate_breakout_follow_through(frame,frame,direction=side,orb_high=101,orb_low=99,orb_bars=3,config=cfg,pdh=103,pdl=97,as_of=pd.Timestamp('2026-11-02 '+clock,tz=idx.tz),confirmation_source_minutes=5)
    assert not check('09:50')['confirmed']
    assert not check('09:55')['confirmed']  # Close across PDH/PDL alone is insufficient.
    assert not check('09:59:59')['confirmed']
    result=check('10:00')
    assert result['confirmed']  # Wicks back through the level are allowed.
    assert result['confirmation_closed_at'] == idx[6]
    assert not check(f'10:{window:02d}')['confirmed']
    bad=df.copy();bad.loc[idx[5],'Open']=103 if side=='CALL' else 97
    assert not check('10:00',bad)['confirmed']  # Strictly beyond, not equal.
    bad=df.copy();bad.loc[idx[5],'Close']=103 if side=='CALL' else 97
    assert not check('10:00',bad)['confirmed']


def test_body_beyond_pdh_still_needs_close_beyond_orb():
    idx=pd.date_range('2026-11-02 09:30',periods=5,freq='5min',tz='America/New_York')
    df=pd.DataFrame({'Open':[100,100,100,100,102], 'Close':[100,100,100,104,102], 'High':105.,'Low':99.,'Volume':1000},index=idx)
    result=evaluate_breakout_follow_through(df,df,direction='CALL',orb_high=103,orb_low=99,orb_bars=3,config={'confirmation_mode':'orb_5','follow_through_enabled':True,'volume_enabled':False},pdh=101,pdl=98,as_of=idx[-1]+pd.Timedelta(minutes=5))
    assert not result['confirmed']


@pytest.mark.parametrize('side', ['CALL', 'PUT'])
@pytest.mark.parametrize('enabled', [True, False])
def test_body_toggle_restores_close_only_crossing_entry(side, enabled):
    idx = pd.date_range('2026-11-02 09:30', periods=4, freq='5min', tz='America/New_York')
    frame = pd.DataFrame({'Open':[100,100,100,100], 'Close':[100,100,100,104], 'High':105., 'Low':95., 'Volume':1000}, index=idx)
    if side == 'PUT':
        frame['Open'],frame['Close']=200-frame.Open,200-frame.Close
    cfg={'confirmation_mode':'orb_5','follow_through_enabled':True,'volume_enabled':False,'pdh_pdl_body_confirmation':enabled}
    result=evaluate_breakout_follow_through(frame,frame,direction=side,orb_high=101,orb_low=99,orb_bars=3,config=cfg,pdh=103,pdl=97,as_of=idx[-1]+pd.Timedelta(minutes=5))
    assert result['confirmed'] is (not enabled)
