from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import pandas as pd

from bot_core import EASTERN, is_top_candidate
from engine import (
    opening_orb_directional_reject_reason,
    opening_orb_spend_limit,
    opening_orb_trade_pauses_normal_entries,
    opening_orb_trade_window_phase,
    read_opening_orb_trade_state,
    run_opening_orb_trade_cycle,
    staged_trading_timeline_stage,
    update_opening_orb_trade_state,
)
from strategies.pmb.strategy import scan_dataframe


def _opening_orb_frames(post_orb_close: float = 100.35) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = []
    prior_start = datetime(2026, 8, 27, 9, 30, tzinfo=EASTERN)
    for index in range(60):
        timestamp = prior_start + timedelta(minutes=5 * index)
        close = 100.0 + (0.08 if index % 2 else -0.08)
        rows.append((timestamp, close - 0.05, close + 0.15, close - 0.15, close, 10_000))

    current_start = datetime(2026, 8, 28, 9, 30, tzinfo=EASTERN)
    rows.extend([
        (current_start, 100.0, 100.20, 99.80, 100.00, 20_000),
        (current_start + timedelta(minutes=5), 100.05, max(100.15, post_orb_close), 99.95, post_orb_close, 15_000),
    ])
    intraday = pd.DataFrame(
        rows,
        columns=["Datetime", "Open", "High", "Low", "Close", "Volume"],
    ).set_index("Datetime")
    daily = pd.DataFrame(
        [{"Open": 100.0, "High": 110.0, "Low": 90.0, "Close": 100.0, "Volume": 5_000_000}],
        index=[pd.Timestamp("2026-08-27", tz=EASTERN)],
    )
    return intraday, daily


class OpeningOrbSignalTests(unittest.TestCase):
    def test_opening_trade_does_not_bypass_internal_score_floor(self):
        intraday, daily = _opening_orb_frames()
        result = scan_dataframe(
            "TEST",
            intraday,
            daily,
            min_score=70,
            orb_minutes=5,
            min_session_bars=2,
        )
        self.assertEqual(result["Signal"], "WAIT")
        self.assertLess(result["Score"], 70)
        self.assertTrue(result["ORB Up"])

    def test_call_requires_orb_pdh_vwap_and_ema_together(self):
        result = {
            "Signal": "CALL",
            "Retest Confirmed": True,
            "ORB Up": True,
            "PDH Break": False,
            "Above VWAP": True,
            "EMA Bullish": True,
        }
        self.assertEqual(
            opening_orb_directional_reject_reason(result),
            "missing opening conditions: PDH break",
        )
        result["PDH Break"] = True
        result["Retest Confirmed"] = False
        self.assertEqual(opening_orb_directional_reject_reason(result), "")

    def test_put_requires_orb_pdl_vwap_and_ema_together(self):
        result = {
            "Signal": "PUT",
            "Retest Confirmed": True,
            "ORB Down": True,
            "PDL Break": False,
            "Below VWAP": True,
            "EMA Bearish": True,
        }
        self.assertEqual(
            opening_orb_directional_reject_reason(result),
            "missing opening conditions: PDL break",
        )
        result["PDL Break"] = True
        self.assertEqual(opening_orb_directional_reject_reason(result), "")


class OpeningOrbStateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "opening_orb_trade": {
                "enabled": True,
                "start_hour": 9,
                "start_minute": 35,
                "end_hour": 9,
                "end_minute": 45,
                "capital_pct": 50.0,
            }
        }

    def test_window_is_active_from_935_until_945(self):
        self.assertEqual(opening_orb_trade_window_phase(self.cfg, datetime(2026, 8, 28, 9, 34, 59, tzinfo=EASTERN)), "before")
        self.assertEqual(opening_orb_trade_window_phase(self.cfg, datetime(2026, 8, 28, 9, 35, tzinfo=EASTERN)), "active")
        self.assertEqual(opening_orb_trade_window_phase(self.cfg, datetime(2026, 8, 28, 9, 44, 59, tzinfo=EASTERN)), "active")
        self.assertEqual(opening_orb_trade_window_phase(self.cfg, datetime(2026, 8, 28, 9, 45, tzinfo=EASTERN)), "after")
        self.assertEqual(opening_orb_trade_window_phase(self.cfg, datetime(2026, 8, 28, 9, 45, 1, tzinfo=EASTERN)), "after")

    def test_automatic_timeline_switches_strategy_and_cadence(self):
        cfg = {"staged_trading_timeline": {"enabled": True}}
        opening = staged_trading_timeline_stage(cfg, datetime(2026, 8, 28, 9, 35, tzinfo=EASTERN))
        orb = staged_trading_timeline_stage(cfg, datetime(2026, 8, 28, 9, 45, tzinfo=EASTERN))
        retest = staged_trading_timeline_stage(cfg, datetime(2026, 8, 28, 11, 30, tzinfo=EASTERN))
        final_scan = staged_trading_timeline_stage(cfg, datetime(2026, 8, 28, 13, 30, 1, tzinfo=EASTERN))
        after = staged_trading_timeline_stage(cfg, datetime(2026, 8, 28, 13, 31, tzinfo=EASTERN))

        self.assertEqual((opening["name"], opening["orb_minutes"], opening["scan_interval_seconds"]), ("opening_orb", 5, 60))
        self.assertEqual((orb["name"], orb["orb_minutes"], orb["require_retest"], orb["scan_interval_seconds"]), ("orb_15m", 15, False, 300))
        self.assertEqual((retest["name"], retest["orb_minutes"], retest["require_retest"]), ("break_retest", 15, True))
        self.assertEqual(final_scan["name"], "break_retest")
        self.assertFalse(after["entries_allowed"])

    def test_normal_entries_pause_before_window_and_until_opening_trade_closes(self):
        waiting = {"status": "WAITING"}
        self.assertTrue(opening_orb_trade_pauses_normal_entries(
            self.cfg,
            waiting,
            datetime(2026, 8, 28, 9, 35, tzinfo=EASTERN),
        ))
        self.assertTrue(opening_orb_trade_pauses_normal_entries(
            self.cfg,
            {"status": "OPEN"},
            datetime(2026, 8, 28, 10, 30, tzinfo=EASTERN),
        ))
        self.assertFalse(opening_orb_trade_pauses_normal_entries(
            self.cfg,
            {"status": "COMPLETE"},
            datetime(2026, 8, 28, 10, 30, tzinfo=EASTERN),
        ))

    def test_opening_budget_is_capped_at_half_of_account(self):
        self.assertEqual(opening_orb_spend_limit(10_000, 9_500, 50.0, 8_000), 5_000)
        self.assertEqual(opening_orb_spend_limit(10_000, 4_000, 50.0, 8_000), 4_000)
        self.assertEqual(opening_orb_spend_limit(10_000, 9_500, 50.0, 3_500), 3_500)

    def test_daily_state_survives_restart_and_resets_next_session(self):
        with TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "opening_orb_trade_state.json"
            first_day = datetime(2026, 8, 28, 9, 40, tzinfo=EASTERN)
            with patch("engine.OPENING_ORB_TRADE_STATE_FILE", state_file):
                update_opening_orb_trade_state(first_day, status="OPEN", symbol="TEST")
                self.assertEqual(read_opening_orb_trade_state(first_day)["status"], "OPEN")
                next_day = first_day + timedelta(days=1)
                self.assertEqual(read_opening_orb_trade_state(next_day)["status"], "WAITING")


class OpeningOrbSelectionTests(unittest.TestCase):
    def test_cycle_uses_highest_ranked_breakout_and_half_account_budget(self):
        fixed_now = datetime(2026, 8, 28, 9, 40, tzinfo=EASTERN)

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

        def scan_result(symbol: str, score: float) -> dict:
            return {
                "Symbol": symbol,
                "Signal": "CALL",
                "Score": score,
                "Grade": "C",
                "Setup Quality": "Opening ORB",
                "Price": 100.0,
                "RVOL": 1.0,
                "ATR %": 1.0,
                "ORB High": 99.5,
                "ORB Low": 98.5,
                "ORB Up": True,
                "ORB Down": False,
                "Retest Confirmed": True,
                "PDH Break": True,
                "PDL Break": False,
                "Above VWAP": True,
                "Below VWAP": False,
                "EMA Bullish": True,
                "EMA Bearish": False,
                "ORB Confirmation Time": "2026-08-28 09:35 EDT",
                "Reasons": "5m ORB breakout up",
            }

        results = {
            "LOW": scan_result("LOW", 92.0),
            "HIGH": scan_result("HIGH", 96.0),
        }
        option_contract = Mock(conId=12345)
        option = {
            "Contract": option_contract,
            "Option": "HIGH 20260904 100 CALL",
            "Expiry": "20260904",
            "Strike": 100.0,
            "Type": "CALL",
            "Mid": 1.0,
            "Option Score": 80.0,
        }
        cfg = {
            "account_mode": "Paper",
            "opening_orb_trade": {
                "enabled": True,
                "start_hour": 9,
                "start_minute": 35,
                "end_hour": 9,
                "end_minute": 45,
                "orb_minutes": 5,
                "capital_pct": 50.0,
            },
            "strategy": {
                "option_dte": 7,
                "min_score": 90,
                "min_confidence": 70,
                "use_rvol_filter": False,
                "use_rvol_ranking": False,
                "use_sr_filter": False,
                "min_atr": 0.3,
            },
            "risk": {
                "max_trades_per_day": 3,
                "recycle_capital_after_exit": True,
                "max_contracts": 20,
            },
            "order": {"type": "LIMIT"},
            "option_filters": {},
            "telegram": {"send_alerts": False},
        }

        with TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "opening_orb_trade_state.json"
            with (
                patch("engine.datetime", FixedDatetime),
                patch("engine.OPENING_ORB_TRADE_STATE_FILE", state_file),
                patch("engine.combined_watchlist", return_value=["LOW", "HIGH"]),
                patch("engine.get_today_trade_stats", return_value=(0, 0.0)),
                patch("engine.get_open_position_deployed", return_value=0.0),
                patch("engine.read_active_positions", return_value=[]),
                patch("engine.scan_symbol_ib", side_effect=lambda _ib, symbol, *_args, **_kwargs: results[symbol]) as scan_symbol,
                patch("engine.is_top_candidate", wraps=is_top_candidate) as candidate_filter,
                patch("engine.recommend_option_ib", return_value=option) as recommend_option,
                patch("engine.write_current_scan_candidates"),
                patch("engine.write_health"),
                patch("engine.log_engine_decision"),
            ):
                run_opening_orb_trade_cycle(
                    ib=Mock(),
                    ib_cfg=Mock(account="DU123"),
                    cfg=cfg,
                    can_trade=False,
                    account_size=10_000.0,
                    liquidity_available=8_000.0,
                    max_daily_capital=9_500.0,
                    approval_mode="Automatic",
                    tg_cfg=Mock(),
                )

        self.assertEqual(recommend_option.call_args.args[1], "HIGH")
        self.assertEqual(recommend_option.call_args.args[5]["_max_contract_cost"], 5_000.0)
        self.assertTrue(all(call.kwargs["min_score"] == 70.0 for call in scan_symbol.call_args_list))
        self.assertTrue(all(call.kwargs["require_retest"] is False for call in scan_symbol.call_args_list))
        self.assertTrue(all(call.kwargs["intraday_bar_size"] == "1 min" for call in scan_symbol.call_args_list))
        self.assertTrue(all(call.kwargs["analysis_bar_minutes"] == 5 for call in scan_symbol.call_args_list))
        self.assertTrue(all(call.args[1] == 90.0 for call in candidate_filter.call_args_list))


if __name__ == "__main__":
    unittest.main()
