from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest
import bot_core as core


def ticker():
    return NS(bid=2, ask=2.02, last=2.01, modelGreeks=None, bidGreeks=None,
              askGreeks=None, lastGreeks=None, volume=100, marketDataType=1,
              time=datetime.now(core.EASTERN))


@pytest.mark.parametrize("required,expected_sleeps", [(False, 1), (True, 12)])
def test_optional_greeks_do_not_delay_a_valid_two_sided_quote(required, expected_sleeps):
    tick = ticker()
    ib = Mock()
    ib.reqMktData.return_value = tick
    elapsed = [0.0]
    ib.sleep.side_effect = lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
    with patch.object(core.time, "time", side_effect=lambda: elapsed[0]):
        market = core.get_snapshot_mid(ib, NS(secType="OPT"), require_live_greeks=required)
    assert ib.sleep.call_count == expected_sleeps
    assert market["Mid"] == pytest.approx(2.01)


def test_greeks_arriving_before_bid_ask_do_not_cause_premature_quote_rejection():
    tick = ticker()
    tick.bid = tick.ask = float("nan")
    tick.modelGreeks = NS(delta=.55)
    ib = Mock()
    ib.reqMktData.return_value = tick
    elapsed = [0.0]
    def update(seconds):
        elapsed[0] += seconds
        if elapsed[0] >= .5:
            tick.bid, tick.ask = 2, 2.02
    ib.sleep.side_effect = update
    with patch.object(core.time, "time", side_effect=lambda: elapsed[0]):
        market = core.get_snapshot_mid(ib, NS(secType="OPT"))
    assert ib.sleep.call_count == 2
    assert market["Bid"] == 2 and market["Ask"] == 2.02


@pytest.mark.parametrize("required,bid,ask,expected", [
    (False, 2, 2.02, True),
    (True, 2, 2.02, False),
    (False, float("nan"), float("nan"), False),
    (False, 1, 2, False),
])
def test_missing_greeks_honor_toggle_without_bypassing_quote_filters(required, bid, ask, expected):
    expiry = (datetime.now(core.EASTERN) + timedelta(days=7)).strftime("%Y%m%d")
    ib = Mock()
    ib.qualifyContracts.side_effect = lambda contract: [contract]
    market = dict(Bid=bid, Ask=ask, Mid=(bid + ask) / 2, Delta=float("nan"),
                  Volume=100, **{"Spread %": (ask - bid) / ((bid + ask) / 2)})
    chain = dict(expiry=expiry, strikes=[100], multiplier="100", tradingClass="TEST", exchange="SMART")
    with (patch.object(core, "_option_chain_candidates", return_value=[chain]),
          patch.object(core, "get_snapshot_mid", return_value=market) as quote):
        result = core.recommend_option_ib(ib, "TEST", "CALL", 100,
            option_filters=dict(require_live_greeks=required, max_contract_qualification_checks=1,
                                max_market_data_checks=1, min_volume=0))
    assert bool(result) is expected
    assert quote.call_args.kwargs["require_live_greeks"] is required
    if expected:
        assert result["Delta"] == result["Estimated Delta"] == .5
