import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import f4p_gate_check as gc
import f4p_idea_book as ib


class ParseBookTests(unittest.TestCase):
    def test_reads_open_and_closed_ideas(self):
        rows = [
            ['EUR/USD', 'SHORT', '2026-10-06 14:00', '1.1200', '2026-10-06 14:05'],
            ['GBP/USD', 'SHORT', '2026-10-06 14:00', '1.3000', '2026-10-06 14:05'],
            ['USD/CAD', 'LONG', '2026-10-01 14:00', '1.3500', '2026-10-01 14:10', '1.3560', '2026-10-06 15:00', 'closed'],
            [],
        ]
        ideas, problems = ib.parse_book_rows(rows)
        self.assertEqual(problems, [])
        self.assertEqual([i.state for i in ideas], ['ACTIVE', 'ACTIVE', 'CLOSED'])
        self.assertEqual(ideas[2].result_pips(), 60.0)

    def test_pending_baseline_is_allowed(self):
        ideas, problems = ib.parse_book_rows([['USD/JPY', 'LONG', '2026-10-06 19:30']])
        self.assertEqual((problems, ideas[0].state), ([], 'PENDING_BASELINE'))

    def test_problems_are_reported_not_skipped(self):
        rows = [
            ['EUR/XXX', 'SHORT', '2026-10-06 14:00'],                      # bad pair
            ['EUR/USD', 'SIDEWAYS', '2026-10-06 14:00'],                   # bad direction
            ['EUR/USD', 'SHORT', ''],                                      # no selection time
            ['EUR/USD', 'SHORT', '2026-10-06 14:00', '1.12'],              # price without time
            ['EUR/USD', 'SHORT', '2026-10-06 14:00', 'abc', '2026-10-06 14:05'],
            ['EUR/USD', 'SHORT', '2026-10-06 14:00', '1.12', '2026-10-06 13:00'],  # quote before decision
            ['EUR/USD', 'SHORT', '2026-10-06 14:00', '', '', '1.1', '2026-10-07 10:00'],  # closed, no baseline
        ]
        ideas, problems = ib.parse_book_rows(rows)
        self.assertEqual(ideas, [])
        self.assertEqual(len(problems), 7)
        self.assertTrue(problems[0].startswith('IDEA BOOK row 2'))

    def test_gate_check_uses_the_book_and_flags_problems(self):
        ideas, _ = ib.parse_book_rows([
            ['EUR/USD', 'SHORT', '2026-10-06 14:00', '1.12', '2026-10-06 14:05'],
            ['GBP/USD', 'SHORT', '2026-10-06 14:00', '1.30', '2026-10-06 14:05'],
            ['USD/CAD', 'LONG', '2026-10-01 14:00', '1.35', '2026-10-01 14:10', '1.356', '2026-10-06 15:00']])
        fresh = {'overall': 'CURRENT', 'sources': []}
        p = gc.build_gate_packet(('USD/JPY', 'LONG'), ideas, fresh)
        self.assertEqual(p['exposure_after']['map']['USD']['long'], 3)   # closed CAD idea not counted
        bad = gc.build_gate_packet(('USD/JPY', 'LONG'), ideas, fresh, book_problems=['IDEA BOOK row 5: x'])
        self.assertIn('Idea book', [c['item'] for c in bad['checks']])
        self.assertFalse(bad['blocks_trade'])


if __name__ == '__main__':
    unittest.main()
