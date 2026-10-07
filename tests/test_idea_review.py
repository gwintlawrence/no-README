import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import f4p_idea_lifecycle as lc
import f4p_idea_review as rv


def closed(pair, d, base, close, lots=None, stop=None, notes=''):
    i = lc.Idea(pair, d, '2026-10-06T08:00:00-04:00', notes=notes, stop_price=stop, lots=lots)
    i.record_baseline(base, '2026-10-06T09:00:00-04:00')
    i.close(close, '2026-10-07T09:00:00-04:00')
    return i


class OneIdeaTests(unittest.TestCase):
    def test_eurusd_short_win(self):
        r = rv.review_idea(closed('EUR/USD', 'SHORT', 1.1250, 1.1160, 0.02, 1.1300))
        self.assertEqual(r['pips'], 90.0)
        self.assertEqual(r['usd'], 18.0)
        self.assertEqual(r['r'], 1.8)
        self.assertEqual((r['risk_usd'], r['risk_pct'], r['over_rule']), (10.0, 2.0, False))

    def test_oversized_trade_is_flagged(self):
        r = rv.review_idea(closed('EUR/USD', 'SHORT', 1.1250, 1.1300, 0.1, 1.1300))
        self.assertEqual(r['usd'], -50.0)
        self.assertEqual(r['r'], -1.0)
        self.assertTrue(r['over_rule'])

    def test_usdjpy_converted_at_close_price(self):
        r = rv.review_idea(closed('USD/JPY', 'LONG', 158.50, 159.50, 0.03, 158.00))
        self.assertEqual(r['pips'], 100.0)
        self.assertAlmostEqual(r['usd'], 100 * 0.03 * 1000 / 159.50, places=2)

    def test_missing_lots_is_not_computed_never_guessed(self):
        r = rv.review_idea(closed('EUR/USD', 'SHORT', 1.1250, 1.1160, None, 1.1300))
        self.assertIsNone(r['usd'])
        self.assertEqual(r['r'], 1.8)
        self.assertTrue(r['not_computed'])

    def test_breakeven_stop_gives_no_r(self):
        r = rv.review_idea(closed('EUR/USD', 'SHORT', 1.1250, 1.1160, 0.02, 1.12317))
        self.assertIsNone(r['r'])
        self.assertTrue(any('break-even' in x for x in r['not_computed']))

    def test_cross_dollars_not_computed(self):
        r = rv.review_idea(closed('EUR/GBP', 'LONG', 0.8650, 0.8700, 0.02, 0.8600))
        self.assertIsNone(r['usd'])
        self.assertEqual(r['r'], 1.0)


class TotalsTests(unittest.TestCase):
    def test_totals(self):
        ideas = [closed('EUR/USD', 'SHORT', 1.1250, 1.1160, 0.02, 1.1300),     # +18
                 closed('GBP/USD', 'SHORT', 1.3260, 1.3310, 0.02, 1.3310),     # -10
                 lc.Idea('USD/JPY', 'LONG', '2026-10-07T06:30:00-04:00')]      # pending
        t = rv.build_review(ideas)['totals']
        self.assertEqual((t['closed'], t['counted'], t['wins'], t['open_or_pending']), (2, 2, 1, 1))
        self.assertEqual((t['net_usd'], t['win_rate_pct'], t['profit_factor']), (8.0, 50.0, 1.8))
        self.assertEqual(t['net_pct_of_account'], 1.6)
        self.assertEqual(t['worst_loss_usd'], -10.0)
        self.assertEqual(t['avg_r'], 0.4)

    def test_no_closed_ideas(self):
        out = rv.format_review(rv.build_review([lc.Idea('USD/JPY', 'LONG', '2026-10-07T06:30:00-04:00')]))
        self.assertIn('No closed ideas yet', out)

    def test_small_sample_is_labelled(self):
        out = rv.format_review(rv.build_review([closed('EUR/USD', 'SHORT', 1.1250, 1.1160, 0.02, 1.1300)]))
        self.assertIn('small sample', out)

    def test_never_blocks(self):
        self.assertFalse(rv.build_review([])['blocks_trade'])


if __name__ == '__main__':
    unittest.main()
