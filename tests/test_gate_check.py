import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import f4p_gate_check as gc
import f4p_idea_lifecycle as lc

T = '2026-10-05T10:00:00+00:00'
FRESH = {'overall': 'CURRENT', 'sources': []}


def active(pair, d, px):
    i = lc.Idea(pair, d, T)
    i.record_baseline(px, '2026-10-05T10:05:00+00:00')
    return i


class GateTests(unittest.TestCase):
    def test_never_blocks_or_signs(self):
        book = [active('EUR/USD', 'SHORT', 1.1), active('GBP/USD', 'SHORT', 1.3)]
        p = gc.build_gate_packet(('AUD/USD', 'SHORT'), book, FRESH)
        self.assertFalse(p['blocks_trade'])
        self.assertIsNone(p['analyst_signoff'])
        self.assertTrue(p['new_exposure_flags'])

    def test_clear_when_nothing_flagged(self):
        trade = {'entry': 1.12, 'stop': 1.115, 'lots': 0.02}     # 50 pips x $0.20 = $10 = 2.0%
        p = gc.build_gate_packet(('EUR/USD', 'LONG'), [], FRESH, candidate_trade=trade)
        self.assertIn('NO FLAGS', p['summary'])

    def test_risk_not_provided_is_not_checked_never_safe(self):
        p = gc.build_gate_packet(('EUR/USD', 'LONG'), [], FRESH)
        risk = [c for c in p['checks'] if c['item'] == 'Risk per trade'][0]
        self.assertEqual((risk['status'], risk['ok']), ('NOT CHECKED', False))

    def test_missing_freshness_is_unknown_not_green(self):
        p = gc.build_gate_packet(('EUR/USD', 'LONG'), [], None)
        fresh = [c for c in p['checks'] if c['item'] == 'Data freshness'][0]
        self.assertEqual(fresh['status'], 'UNKNOWN')
        self.assertFalse(fresh['ok'])

    def test_preexisting_flags_are_not_blamed_on_candidate(self):
        book = [active('EUR/USD', 'SHORT', 1.1), active('GBP/USD', 'SHORT', 1.3),
                active('AUD/USD', 'SHORT', 0.65)]
        p = gc.build_gate_packet(('USD/JPY', 'LONG'), book, FRESH)
        self.assertTrue(p['new_exposure_flags'])      # candidate adds a 4th long USD
        p2 = gc.build_gate_packet(('EUR/GBP', 'LONG'), book, FRESH)
        self.assertFalse([w for w in p2['new_exposure_flags'] if w['currency'] == 'AUD'])

    def test_open_book_pips_and_close_flags(self):
        book = [active('EUR/USD', 'SHORT', 1.12)]
        p = gc.build_gate_packet(('GBP/USD', 'LONG'), book, FRESH,
                                 prices={'EUR/USD': 1.1235},
                                 close_rules=[lc.adverse_pips_rule(30)])
        self.assertEqual(p['open_book'][0]['pips'], -35.0)
        self.assertEqual(len(p['open_book'][0]['close_flags']), 1)
        self.assertEqual(book[0].state, 'ACTIVE')       # flagging never closes

    def test_bad_candidate_and_html(self):
        with self.assertRaises(ValueError):
            gc.build_gate_packet(('EUR/XXX', 'LONG'), [], FRESH)
        out = gc.render_html(gc.build_gate_packet(('EUR/USD', 'LONG'), [], FRESH))
        self.assertIn('never blocks', out)


if __name__ == '__main__':
    unittest.main()
