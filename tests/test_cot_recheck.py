import os
import sys
import unittest
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import f4p_cot_recheck as cr


def utc(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=timezone.utc)


class ExpectedDateTests(unittest.TestCase):
    def test_saturday_expects_tuesday_four_days_earlier(self):
        self.assertEqual(cr.expected_cot_date(utc(2026, 10, 10, 15)), date(2026, 10, 6))

    def test_friday_before_release_still_expects_previous_week(self):
        self.assertEqual(cr.expected_cot_date(utc(2026, 10, 9, 14)), date(2026, 9, 29))

    def test_friday_after_release_expects_this_week(self):
        self.assertEqual(cr.expected_cot_date(utc(2026, 10, 9, 21)), date(2026, 10, 6))

    def test_midweek_expects_last_fridays_release(self):
        # today in this project: Wed 7 Oct 2026 -> Friday 2 Oct -> Tuesday 29 Sep
        self.assertEqual(cr.expected_cot_date(utc(2026, 10, 7, 9)), date(2026, 9, 29))


class StatusTests(unittest.TestCase):
    NOW = utc(2026, 10, 10, 15)

    def test_current_when_all_dates_match(self):
        r = cr.cot_status(['261006'] * 8, self.NOW)
        self.assertEqual(r['status'], 'CURRENT')

    def test_one_old_currency_makes_it_stale(self):
        r = cr.cot_status(['261006'] * 7 + ['260929'], self.NOW)
        self.assertEqual(r['status'], 'STALE')

    def test_last_weeks_data_is_stale_on_saturday(self):
        r = cr.cot_status(['260929'] * 8, self.NOW)
        self.assertEqual(r['status'], 'STALE')

    def test_empty_is_unknown_never_current(self):
        self.assertEqual(cr.cot_status([], self.NOW)['status'], 'UNKNOWN')
        self.assertEqual(cr.cot_status(['', ' '], self.NOW)['status'], 'UNKNOWN')

    def test_unreadable_date_is_unknown(self):
        self.assertEqual(cr.cot_status(['261006', 'N/A'], self.NOW)['status'], 'UNKNOWN')

    def test_iso_dates_also_read(self):
        self.assertEqual(cr.cot_status(['2026-10-06'] * 8, self.NOW)['status'], 'CURRENT')


if __name__ == '__main__':
    unittest.main()
