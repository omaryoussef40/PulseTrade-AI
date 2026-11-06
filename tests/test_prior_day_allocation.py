from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo
import pytest
import bot_core as core

@pytest.mark.parametrize('include,expected',[(True,7900),(False,400)])
def test_prior_day_allocation_switch(include,expected):
    positions=[
        dict(option='old',entry_time='2026-11-04T12:00:00-05:00',quantity=15,entry_price=5),
        dict(option='today',entry_time='2026-11-05T10:00:00-05:00',quantity=1,entry_price=3),
        dict(option='unknown',quantity=1,entry_price=1),
    ]
    with patch.object(core,'read_active_positions',return_value=positions), patch.object(core,'_trade_log_entry_lookup',return_value={}), patch.object(core,'datetime',wraps=datetime) as clock:
        clock.now.return_value=datetime(2026,11,5,12,tzinfo=ZoneInfo('America/New_York'))
        assert core.get_open_position_deployed(count_prior_day_positions=include)==expected
        if include:
            assert core.get_open_position_deployed()==expected
    assert positions[0]['quantity']==15


def test_prior_day_date_falls_back_to_history_and_uses_exchange_timezone():
    positions=[dict(option='old',quantity=1,entry_price=5),dict(option='utc',entry_time='2026-11-05T02:00:00+00:00',quantity=1,entry_price=2)]
    with patch.object(core,'read_active_positions',return_value=positions), patch.object(core,'_trade_log_entry_lookup',return_value={'label:old':{'timestamp':'2026-11-04T10:00:00-05:00'}}), patch.object(core,'datetime',wraps=datetime) as clock:
        clock.now.return_value=datetime(2026,11,5,12,tzinfo=ZoneInfo('America/New_York'))
        assert core.get_open_position_deployed(False)==0
        assert core.get_open_position_deployed(True)==700
