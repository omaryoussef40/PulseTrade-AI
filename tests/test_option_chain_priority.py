from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import bot_core as core


def test_standard_smart_class_is_checked_before_adjusted_classes():
    expiry = (datetime.now(core.EASTERN) + timedelta(days=7)).strftime("%Y%m%d")
    def chain(name, exchange="SMART", multiplier="100"):
        return NS(exchange=exchange, tradingClass=name, multiplier=multiplier,
                  expirations=[expiry], strikes=[700, 740, 750])
    ib = Mock()
    ib.reqSecDefOptParams.return_value = [chain("2SPY"), chain("SPY", "CBOE"), chain("SPY"), chain("1SPY")]
    with patch.object(core, "qualify_stock", return_value=NS(symbol="SPY", secType="STK", conId=123)):
        candidates = core._option_chain_candidates(ib, "SPY")
    assert candidates[0]["tradingClass"] == "SPY"
    assert candidates[0]["exchange"] == "SMART"
    assert {row["tradingClass"] for row in candidates} == {"SPY", "1SPY", "2SPY"}


def test_worker_budget_reaches_regular_contracts_despite_adjusted_class():
    expiry = (datetime.now(core.EASTERN) + timedelta(days=7)).strftime("%Y%m%d")
    def chain(name):
        return NS(exchange="SMART", tradingClass=name, multiplier="100",
                  expirations=[expiry], strikes=[99, 100, 101])
    ib = Mock()
    ib.reqSecDefOptParams.return_value = [chain("2TEST"), chain("TEST")]
    ib.qualifyContracts.side_effect = lambda contract: [contract] if contract.tradingClass == "TEST" else []
    quote = dict(Bid=2, Ask=2.02, Mid=2.01, Last=2.01, **{"Spread %": .01}, Delta=.55,
                 Volume=1000, Gamma=.01, Theta=-.01, Vega=.1, **{"Implied Vol": .2})
    with (patch.object(core, "qualify_stock", return_value=NS(symbol="TEST", secType="STK", conId=123)),
          patch.object(core, "get_snapshot_mid", return_value=quote)):
        option = core.recommend_option_ib(ib, "TEST", "CALL", 100,
            option_filters=dict(max_contract_qualification_checks=1, max_market_data_checks=1,
                                min_volume=0, min_abs_delta=.45, max_abs_delta=.8))
    assert option is not None
    assert ib.qualifyContracts.call_args.args[0].tradingClass == "TEST"
