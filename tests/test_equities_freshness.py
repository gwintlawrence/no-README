import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import f4p_freshness_status as fs

NOW = datetime(2026, 10, 10, 15, tzinfo=timezone.utc)      # a Saturday afternoon
EQ = [s for s in fs.SOURCES if s['name'] == 'Equities weekly run'][0]


def serial(y, m, d):
    return (datetime(y, m, d) - datetime(1899, 12, 30)).days


class ParseTests(unittest.TestCase):
    def test_serial_number_reads_as_a_date(self):
        self.assertEqual(fs.parse_date(serial(2026, 10, 3)).date().isoformat(), '2026-10-03')

    def test_serial_as_text_also_reads(self):
        self.assertEqual(fs.parse_date(str(serial(2026, 10, 3))).date().isoformat(), '2026-10-03')

    def test_small_numbers_are_not_dates(self):
        self.assertIsNone(fs.parse_date(12))

    def test_cot_yymmdd_still_reads(self):
        self.assertEqual(fs.parse_date('260929').date().isoformat(), '2026-09-29')


class LatestDateTests(unittest.TestCase):
    def test_newest_date_decides(self):
        ts = fs.source_timestamp(EQ, [serial(2026, 9, 12), serial(2026, 10, 3), serial(2026, 9, 26), ''])
        self.assertEqual(ts.date().isoformat(), '2026-10-03')

    def test_nothing_readable_is_unknown(self):
        self.assertIsNone(fs.source_timestamp(EQ, ['', None, 'Date']))

    def test_junk_cells_ignored_but_never_make_it_fresher(self):
        ts = fs.source_timestamp(EQ, ['n/a', serial(2026, 9, 26)])
        self.assertEqual(ts.date().isoformat(), '2026-09-26')

    def test_last_saturdays_run_is_current(self):
        st = fs.classify(fs.source_timestamp(EQ, [serial(2026, 10, 3)]), NOW, EQ['current_hours'], EQ['stale_hours'])
        self.assertEqual(st, 'CURRENT')

    def test_two_weeks_old_is_delayed_three_is_stale(self):
        c, s = EQ['current_hours'], EQ['stale_hours']
        self.assertEqual(fs.classify(fs.source_timestamp(EQ, [serial(2026, 9, 26)]), NOW, c, s), 'DELAYED')
        self.assertEqual(fs.classify(fs.source_timestamp(EQ, [serial(2026, 9, 19)]), NOW, c, s), 'STALE')


class EvaluateTests(unittest.TestCase):
    def reader(self, calls):
        def read(tab, a1, env=None):
            calls.append((tab, env))
            if env:
                return [serial(2026, 10, 3)]
            return ['Last run: 2026-10-10 10:00 UTC'] if tab == 'FRED AUTO' and a1 == 'A1' else ['261006'] * 8
        return read

    def test_equities_read_uses_its_own_spreadsheet(self):
        calls = []
        r = fs.evaluate(fs.SOURCES, self.reader(calls), NOW)
        self.assertIn(('OPTIONS FLOW & IV', 'EQUITIES_SHEET_ID'), calls)
        self.assertEqual({x['name']: x['status'] for x in r['sources']}['Equities weekly run'], 'CURRENT')

    def test_missing_equities_sheet_is_unknown_not_current(self):
        def read(tab, a1, env=None):
            if env:
                raise RuntimeError('EQUITIES_SHEET_ID is not set')
            return ['Last run: 2026-10-10 10:00 UTC'] if a1 == 'A1' else ['261006'] * 8
        r = fs.evaluate(fs.SOURCES, read, NOW)
        self.assertEqual({x['name']: x['status'] for x in r['sources']}['Equities weekly run'], 'UNKNOWN')
        self.assertEqual(r['overall'], 'UNKNOWN')

    def test_fx_sources_never_touch_the_equities_sheet(self):
        calls = []
        two_arg = lambda tab, a1: self.reader(calls)(tab, a1)
        fs.evaluate(fs.FX_SOURCES, two_arg, NOW)
        self.assertTrue(all(env is None for _, env in calls))
        self.assertEqual(len(fs.FX_SOURCES), 2)


if __name__ == '__main__':
    unittest.main()
