from types import SimpleNamespace
import subprocess
import unittest
from unittest.mock import patch

from backtester.controller import StrategyLabSettings
from backtester.ui import _apply_strategy_lab_to_engine
from modules.scanner_option_worker import lookup_option


class StrategyVolumeSettingsTests(unittest.TestCase):
    def test_backtester_volume_setting_applies_to_engine(self):
        config = {"strategy": {}, "risk": {}, "strategy_lab": {}}
        settings = StrategyLabSettings(
            symbols=["META"],
            selected_strategies=("PMB",),
            breakout_follow_through={
                "enabled": True,
                "volume_enabled": True,
                "follow_through_enabled": False,
                "min_breakout_volume_ratio": 1.75,
                "min_volume_sessions": 3,
                "require_extension": True,
            },
        )

        with patch("backtester.ui.save_config"):
            _apply_strategy_lab_to_engine(config, settings, ["META"])

        self.assertEqual(
            config["strategy"]["breakout_follow_through"]["min_breakout_volume_ratio"],
            1.75,
        )
        self.assertTrue(config["strategy"]["breakout_follow_through"]["volume_enabled"])
        self.assertFalse(config["strategy"]["breakout_follow_through"]["follow_through_enabled"])

    def test_scanner_option_lookup_returns_clean_worker_result(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout='{"option": {"Strike": 100.0}}',
            stderr="",
        )
        with patch("modules.scanner_option_worker.subprocess.run", return_value=completed):
            option = lookup_option({"symbol": "META"})

        self.assertEqual(option, {"Strike": 100.0})

    def test_scanner_option_lookup_has_a_deadline(self):
        with patch(
            "modules.scanner_option_worker.subprocess.run",
            side_effect=subprocess.TimeoutExpired("worker", 45),
        ):
            with self.assertRaisesRegex(TimeoutError, "stock results are still available"):
                lookup_option({"symbol": "META"})


if __name__ == "__main__":
    unittest.main()
