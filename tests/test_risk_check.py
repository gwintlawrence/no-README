import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import f4p_gate_check as gc
import f4p_idea_book as ib
import f4p_risk_check as rk

FRESH = {'overall': 'CURRENT', 'sources': []}


def t(label, pair, d, entry, stop, lots, target=None):
    return {'label': label, 'pair': pair, 'direction': d, 'entry': entry,
            'stop': stop, 'lots': lots, 'target': target}


class RiskTests(unittest.TestCase):
    def test_gbpusd_point_one_lot_is_ten_percent_of_500(self):
        r = rk.check_risk([t('GBP/USD SHORT', 'GBP/USD', 'SHORT', 1.3260, 1.3310, 0.1, 1.3200)])
        row = r['rows'][0]
        self.assertEqual((row['stop_pips'], row['risk_usd'], row['risk_pct']), (50.0, 50.0, 10.0))
        self.assertEqual(row['rr'], 1.2)
        self.assertEqual(len(r['flags']), 1)
        self.assertIn('0.02 lots', r['flags'][0])
        self.assertFalse(r['blocks_trade'])

    def test_exactly_two_percent_is_not_flagged(self):
        r = rk.check_risk([t('EUR/USD SHORT', 'EUR/USD', 'SHORT', 1.1250, 1.1300, 0.02, 1.1160)])
        self.assertEqual((r['rows'][0]['risk_usd'], r['rows'][0]['risk_pct'], r['rows'][0]['rr']), (10.0, 2.0, 1.8))
        self.assertEqual(r['flags'], [])

    def test_usd_base_pair_converts_pip_value(self):
        r = rk.check_risk([t('USD/JPY LONG', 'USD/JPY', 'LONG', 157.00, 156.50, 0.01)])
        self.assertEqual(r['rows'][0]['stop_pips'], 50.0)
        self.assertAlmostEqual(r['rows'][0]['risk_usd'], 3.18, places=2)

    def test_unknown_never_assumed_safe(self):
        r = rk.check_risk([
            t('wrong side', 'EUR/USD', 'SHORT', 1.1250, 1.1200, 0.02),   # stop below a short entry
            t('cross', 'EUR/GBP', 'LONG', 0.87, 0.865, 0.02),
            t('no lots', 'GBP/USD', 'SHORT', 1.326, 1.331, None)])
        self.assertEqual(r['rows'], [])
        self.assertEqual(len(r['not_checked']), 3)
        self.assertIn('wrong side', r['not_checked'][0])

    def test_breakeven_stop_on_a_filled_trade_has_no_open_risk(self):
        filled = dict(t('GBP/USD SHORT', 'GBP/USD', 'SHORT', 1.3259, 1.3259, 0.02, 1.3200), filled=True)
        r = rk.check_risk([filled])
        self.assertEqual((r['rows'][0]['risk_usd'], r['rows'][0]['risk_pct']), (0.0, 0.0))
        self.assertEqual(r['flags'] + r['not_checked'], [])
        trailed = dict(filled, stop=1.3250)                      # 9 pips of profit locked in
        self.assertEqual(rk.check_risk([trailed])['rows'][0]['locked_pips'], 9.0)
        self.assertIn('no open risk', rk.format_risk(rk.check_risk([filled])))

    def test_same_stop_on_an_unfilled_order_is_still_refused(self):
        pending = t('GBP/USD SHORT', 'GBP/USD', 'SHORT', 1.3260, 1.3260, 0.02)
        r = rk.check_risk([pending])
        self.assertEqual(r['rows'], [])
        self.assertIn('wrong side', r['not_checked'][0])

    def test_gate_check_uses_baseline_to_know_a_trade_filled(self):
        rows = [['GBP/USD', 'SHORT', 46302.2, 1.3259, 46302.3, '', '', 'SL to break-even', 1.3260, 1.3259, 1.3200, 0.02]]
        ideas, problems = ib.parse_book_rows(rows)
        self.assertEqual(problems, [])
        p = gc.build_gate_packet(('USD/JPY', 'LONG'), ideas, FRESH,
                                 candidate_trade={'entry': 157.0, 'stop': 156.5, 'lots': 0.01})
        gbp = [r for r in p['risk']['rows'] if r['label'].startswith('GBP/USD')][0]
        self.assertEqual(gbp['risk_usd'], 0.0)

    def test_total_if_everything_triggers(self):
        r = rk.check_risk([t('a', 'EUR/USD', 'SHORT', 1.125, 1.130, 0.1),
                           t('b', 'GBP/USD', 'SHORT', 1.326, 1.331, 0.1)])
        self.assertEqual((r['total_risk_usd'], r['total_risk_pct']), (100.0, 20.0))

    def test_idea_book_columns_flow_into_gate_check(self):
        rows = [['GBP/USD', 'SHORT', 46301.5, '', '', '', '', 'Sell Stop', 1.3260, 1.3310, 1.3200, 0.1]]
        ideas, problems = ib.parse_book_rows(rows)
        self.assertEqual(problems, [])
        self.assertEqual((ideas[0].entry_price, ideas[0].stop_price, ideas[0].lots), (1.326, 1.331, 0.1))
        p = gc.build_gate_packet(('USD/JPY', 'LONG'), ideas, FRESH,
                                 candidate_trade={'entry': 157.0, 'stop': 156.5, 'lots': 0.01})
        risk = [c for c in p['checks'] if c['item'] == 'Risk per trade'][0]
        self.assertEqual(risk['status'], 'FLAGGED')
        self.assertFalse(p['blocks_trade'])

    def test_baseline_replaces_order_price_once_filled(self):
        rows = [['EUR/USD', 'SHORT', 46301.5, 1.1245, 46301.6, '', '', '', 1.1250, 1.1300, '', 0.02]]
        ideas, _ = ib.parse_book_rows(rows)
        p = gc.build_gate_packet(('USD/JPY', 'LONG'), ideas, FRESH)
        eur = [r for r in p['risk']['rows'] if r['label'].startswith('EUR/USD')][0]
        self.assertEqual(eur['stop_pips'], 55.0)       # measured from the real fill, 1.1245


if __name__ == '__main__':
    unittest.main()
