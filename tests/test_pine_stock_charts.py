from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest
from modules.pine_levels import StockLevels,parse_pine
from modules.pine_stock_charts import make_level_chart,candle_frame
from modules.pine_stock_chart_data import fetch_snapshot

DAY=date(2026,10,8)
NOW=pd.Timestamp('2026-10-08 09:42',tz='America/New_York')
LEVEL=StockLevels('TEST',100,99,102,104,98,99,96,94)


def data():
    return pd.DataFrame(dict(Open=[99,100,100],High=[100,101,102],Low=[98,99,99],Close=[99.5,100.5,101],Volume=[1000,2000,3000]),index=pd.date_range('2026-10-08 09:30',periods=3,freq='5min',tz='America/New_York'))


def test_all_eight_lines_and_completed_price_vwap():
    f=data().iloc[:2]
    fig=make_level_chart(LEVEL,f,DAY,DAY)
    lines=[s for s in fig.layout.shapes if s.type=='line']
    assert len(lines)==8
    assert {s.y0 for s in lines}=={100,99,102,104,98,96,94}
    assert all(s.y0==s.y1 for s in lines)
    assert len(fig.layout.annotations)==9
    assert fig.data[0].type=='candlestick'
    assert fig.data[1].name=='Market VWAP'
    expected=(((f.High+f.Low+f.Close)/3)*f.Volume).sum()/f.Volume.sum()
    assert fig.data[1].y[-1]==pytest.approx(expected)
    assert pd.Timestamp(fig.data[0].x[0]).hour==9 and pd.Timestamp(fig.data[0].x[0]).minute==30
    assert pd.Timestamp(fig.data[0].x[0]).tzinfo is None


def test_empty_charts_still_display_levels_without_fabricating_candles():
    fig=make_level_chart(LEVEL,candle_frame([]),DAY,DAY)
    assert len(fig.layout.shapes)==8 and not fig.data
    assert fig.layout.yaxis.range[0]<94 and fig.layout.yaxis.range[1]>104


def test_session_gaps_withhold_vwap():
    fig=make_level_chart(LEVEL,data().iloc[1:],DAY,DAY)
    assert len(fig.data)==1


def test_early_close_chart_range():
    fig=make_level_chart(LEVEL,candle_frame([]),date(2026,11,27),DAY)
    assert fig.layout.xaxis.type=='category'
    assert fig.layout.xaxis.categoryarray==()


def test_loader_read_only_historical_date_and_no_incomplete_candles():
    calls=[];disconnected=[];configs=[]
    class Provider:
        def __init__(self,cfg):configs.append(cfg)
        def connect(self):pass
        def disconnect(self):disconnected.append(True)
        def historical_bars(self,symbol,**kw):calls.append((symbol,kw));return data()
    with patch('modules.pine_stock_chart_data.IBKRMarketDataProvider',Provider):
        result=fetch_snapshot(dict(day=str(DAY),symbols=['TEST'],port=7497),NOW)
    records=result['candles']['TEST']
    assert len(records)==2 and records[-1]['Close']==100.5
    assert configs[0].readonly and configs[0].port==7497
    assert calls[0][1]['end_datetime']=='20261008-13:42:00'
    assert calls[0][1]['use_rth'] is False
    assert disconnected==[True]
    assert candle_frame(records).index.tz is not None


@pytest.mark.parametrize('day,now', [('2026-10-10',NOW),('2026-10-09',NOW),('2026-10-08',pd.Timestamp('2026-10-08 03:59',tz='America/New_York'))])
def test_non_open_sessions_do_not_connect(day,now):
    with patch('modules.pine_stock_chart_data.IBKRMarketDataProvider') as provider:
        result=fetch_snapshot(dict(day=day,symbols=['TEST']),now)
        provider.assert_not_called()
    assert result['errors']['TEST'] and not result['candles']


def test_connection_failure_and_individual_bad_symbol():
    class Provider:
        def __init__(self,cfg):pass
        def connect(self):pass
        def disconnect(self):pass
        def historical_bars(self,symbol,**kw):
            if symbol=='BAD':raise RuntimeError('No historical permissions')
            return data()
    with patch('modules.pine_stock_chart_data.IBKRMarketDataProvider',Provider):
        result=fetch_snapshot(dict(day=str(DAY),symbols=['BAD','TEST']),NOW)
    assert result['errors']['BAD']=='No historical permissions'
    assert len(result['candles']['TEST'])==2
    with patch('modules.pine_stock_chart_data.IBKRMarketDataProvider') as provider:
        provider.return_value.connect.side_effect=RuntimeError('Connection unavailable')
        result=fetch_snapshot(dict(day=str(DAY),symbols=['TEST']),NOW)
    assert 'Connection unavailable' in result['errors']['TEST']


def test_chart_wall_renders_all_imported_tickers_and_exact_lines():
    source=(Path(__file__).parents[1]/'examples/daily_trade_levels_2026-10-08.pine').read_text()
    plan=parse_pine(source)
    snapshot=dict(source='IBKR',fetched_at=NOW.isoformat(),candles={},errors={})
    with patch('modules.pine_stock_ui.load_plan',return_value=None), patch('modules.pine_stock_charts.chart_snapshot',return_value=snapshot):
        at=AppTest.from_string('from modules.pine_stock_ui import render\nrender({})').run(timeout=20)
    assert not at.exception
    assert sorted(s.value for s in at.subheader)==[row.symbol for row in plan.levels]
    assert len(at.get('plotly_chart'))==10
    assert at.radio[0].value=='Charts'


def test_market_data_explicit_end_keeps_legacy_default():
    from market_data import IBKRMarketDataProvider
    seen=[]
    ib=SimpleNamespace(isConnected=lambda:True,qualifyContracts=lambda c:[c],reqHistoricalData=lambda c,**kw:(seen.append(kw) or []))
    provider=IBKRMarketDataProvider(ib=ib)
    provider.historical_bars('TEST')
    provider.historical_bars('TEST',end_datetime='20261008 16:00:00 America/New_York')
    assert seen[0]['endDateTime']=='' and seen[0]['formatDate']==1
    assert seen[1]['endDateTime'].startswith('20261008') and seen[1]['formatDate']==2


def test_unsaved_pine_survives_chart_refresh_and_tab_switching():
    snapshot=dict(source='IBKR',fetched_at=NOW.isoformat(),candles={},errors={})
    with patch('modules.pine_stock_ui.load_plan',return_value=None), patch('modules.pine_stock_charts.chart_snapshot',return_value=snapshot):
        at=AppTest.from_string('from modules.pine_stock_ui import render\nrender({})').run(timeout=20)
        at.radio[0].set_value('Paste & settings').run()
        changed=at.text_area[0].value.replace('longEntry := 45.70','longEntry := 45.71')
        at.text_area[0].set_value(changed).run()
        at.radio[0].set_value('Charts').run()
        assert not at.exception and len(at.get('plotly_chart'))==10
        at.date_input[0].set_value(date(2026,10,9)).run()
        assert not at.exception and at.warning
        at.radio[0].set_value('Paste & settings').run()
        assert at.text_area[0].value==changed


def extended_data():
    f=data().iloc[:1]
    timestamps=['2026-10-08 03:55','2026-10-08 04:00','2026-10-08 09:25','2026-10-08 09:30','2026-10-08 09:35','2026-10-08 15:55','2026-10-08 16:00','2026-10-08 19:55','2026-10-08 20:00']
    result=pd.concat([f]*len(timestamps),ignore_index=True)
    result.index=pd.DatetimeIndex(timestamps,tz='America/New_York')
    return result


def test_extended_data_loaded_from_four_to_twenty_without_future_bars():
    class Provider:
        def __init__(self,cfg):pass
        def connect(self):pass
        def disconnect(self):pass
        def historical_bars(self,symbol,**kw):return extended_data()
    with patch('modules.pine_stock_chart_data.IBKRMarketDataProvider',Provider):
        result=fetch_snapshot(dict(day=str(DAY),symbols=['TEST']),pd.Timestamp('2026-10-08 21:00',tz='America/New_York'))
        early=fetch_snapshot(dict(day=str(DAY),symbols=['TEST']),pd.Timestamp('2026-10-08 04:07',tz='America/New_York'))
    f=candle_frame(result['candles']['TEST'])
    assert f.index[0].strftime('%H:%M')=='04:00'
    assert f.index[-1].strftime('%H:%M')=='19:55'
    assert len(f)==7
    assert len(early['candles']['TEST'])==1


def test_category_axis_removes_empty_intervals_and_labels_all_sessions():
    f=extended_data().iloc[1:-1]
    fig=make_level_chart(LEVEL,f,DAY,DAY)
    assert fig.layout.xaxis.type=='category'
    categories=list(fig.layout.xaxis.categoryarray)
    assert categories==f.index.strftime('%Y-%m-%d %H:%M').tolist()
    assert len(categories)==len(f)
    assert '2026-10-08 11:00' not in categories
    assert fig.layout.xaxis.range==(-.5,len(f)-.5)
    names={a.text for a in fig.layout.annotations}
    assert {'Premarket','Market','Postmarket'}<=names


def test_premarket_does_not_enter_market_vwap():
    f=data().iloc[:2]
    pre=f.iloc[:1].copy();pre.index=pd.DatetimeIndex(['2026-10-08 09:25'],tz='America/New_York')
    pre['High']=200;pre['Close']=180;pre['Volume']=1_000_000
    post=f.iloc[:1].copy();post.index=pd.DatetimeIndex(['2026-10-08 16:00'],tz='America/New_York')
    fig=make_level_chart(LEVEL,pd.concat([pre,f,post]),DAY,DAY)
    vwap=next(t for t in fig.data if t.name=='Market VWAP')
    expected=(((f.High+f.Low+f.Close)/3)*f.Volume).sum()/f.Volume.sum()
    assert vwap.y[-1]==pytest.approx(expected)
    assert list(vwap.x)==f.index.strftime('%Y-%m-%d %H:%M').tolist()


def test_early_close_classifies_postmarket_from_thirteen():
    f=data().iloc[:2].copy()
    f.index=pd.DatetimeIndex(['2026-11-27 12:55','2026-11-27 13:00'],tz='America/New_York')
    fig=make_level_chart(LEVEL,f,date(2026,11,27),DAY)
    post=next(s for s in fig.layout.shapes if s.type=='rect' and s.x0=='2026-11-27 13:00')
    assert post.x1=='2026-11-27 13:00'
    assert 'Postmarket' in {a.text for a in fig.layout.annotations}
