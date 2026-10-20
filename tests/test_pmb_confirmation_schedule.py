from datetime import datetime, time
import unittest
from zoneinfo import ZoneInfo

from engine import next_aligned_scan_time
from strategies.pmb.candles import confirmation_scan_interval_seconds


class ConfirmationScheduleTests(unittest.TestCase):
    def test_five_minute_confirmation_caps_a_fifteen_minute_schedule(self):
        config = {'follow_through_enabled': True, 'confirmation_minutes': 5}
        interval = confirmation_scan_interval_seconds(900, config)
        self.assertEqual(interval, 300)
        now = datetime(2026, 10, 12, 10, 2, tzinfo=ZoneInfo('America/New_York'))
        next_scan = next_aligned_scan_time(now, interval, time(9, 51))
        self.assertEqual(next_scan.strftime('%H:%M'), '10:05')

    def test_fifteen_minute_confirmation_keeps_existing_schedule(self):
        self.assertEqual(confirmation_scan_interval_seconds(900, {'follow_through_enabled': True, 'confirmation_minutes': 15}), 900)

    def test_faster_existing_schedule_is_preserved(self):
        self.assertEqual(confirmation_scan_interval_seconds(60, {'follow_through_enabled': True, 'confirmation_minutes': 5}), 60)

    def test_disabled_confirmation_does_not_change_polling(self):
        self.assertEqual(confirmation_scan_interval_seconds(900, {'enabled': True, 'follow_through_enabled': False, 'confirmation_minutes': 5}), 900)

    def test_legacy_enabled_flag_uses_default_15_minute_confirmation(self):
        self.assertEqual(confirmation_scan_interval_seconds(1800, {'enabled': True}), 900)


if __name__ == '__main__':
    unittest.main()
