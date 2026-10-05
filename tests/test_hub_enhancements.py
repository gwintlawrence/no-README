import os
import sys
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import f4p_exposure_check as ex
import f4p_idea_lifecycle as lc
import f4p_freshness_status as fs


class ExposureTests(unittest.TestCase):
    def test_legs(self):
        self.assertEqual(ex.idea_legs('EUR/USD', 'SHORT'), [('EUR', -1), ('USD', 1)])
        self.assertEqual(ex.idea_legs('USDJPY', 'long'), [('USD', 1), ('JPY', -1)])

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            ex.parse_pair('EUR/XXX')
        with self.assertRaises(ValueError):
            ex.idea_legs('EUR/USD', 'SIDEWAYS')

    def test_map_and_share(self):
        ideas = [('EUR/USD', 'SHORT'), ('GBP/USD', 'SHORT'), ('USD/JPY', 'LONG')]
        m = ex.exposure_map(ideas)
        self.assertEqual(m['USD']['net'], 3)
        self.assertEqual(m['EUR']['net'], -1)
        # total |net| = USD 3 + EUR 1 + GBP 1 + JPY 1 = 6 -> USD 50.0%
        self.assertEqual(m['USD']['net_share_pct'], 50.0)

    def test_breach_and_watch_but_never_blocks(self):
        ideas = [('EUR/USD', 'SHORT'), ('GBP/USD', 'SHORT'), ('AUD/USD', 'SHORT')]
        r = ex.check_exposure(ideas, {'max_same_direction_per_currency': 2})  # 3 > 2
        levels = {(w['level'], w['currency']) for w in r['warnings']}
        self.assertIn(('BREACH', 'USD'), levels)
        self.assertIn(('WATCH', 'USD'), levels)
        self.assertFalse(r['blocks_trade'])    # Cardinal Rule: flag only

    def test_offsetting_legs_not_a_breach(self):
        r = ex.check_exposure([('EUR/USD', 'LONG'), ('USD/JPY', 'LONG'), ('GBP/USD', 'LONG')])
        self.assertFalse([w for w in r['warnings'] if w['level'] == 'BREACH'])

    def test_no_concentration_watch_on_tiny_book(self):
        r = ex.check_exposure([('EUR/USD', 'LONG')])
        self.assertEqual(r['warnings'], [])

    def test_glenise_limits_notice_at_4_breach_above_6(self):
        def usd_long(n):
            pairs = ['EUR/USD', 'GBP/USD', 'AUD/USD', 'NZD/USD', 'USD/JPY', 'USD/CHF', 'USD/CAD']
            ideas = [(p, 'LONG' if p.startswith('USD') else 'SHORT') for p in pairs[:n]]
            return {w['level'] for w in ex.check_exposure(ideas)['warnings'] if w['currency'] == 'USD'
                    and w['level'] != 'WATCH'}
        self.assertEqual(usd_long(3), set())
        self.assertEqual(usd_long(4), {'NOTICE'})
        self.assertEqual(usd_long(6), {'NOTICE'})
        self.assertEqual(usd_long(7), {'BREACH'})   # one flag per side, the stronger one

    def test_custom_limits(self):
        r = ex.check_exposure([('EUR/USD', 'SHORT'), ('GBP/USD', 'SHORT')],
                              {'max_same_direction_per_currency': 1, 'notice_same_direction': None})
        self.assertTrue([w for w in r['warnings'] if w['level'] == 'BREACH'])


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.idea = lc.Idea('EUR/USD', 'SHORT', '2026-10-05T10:00:00+00:00')

    def test_pip_size(self):
        self.assertEqual(lc.pip_size('USD/JPY'), 0.01)
        self.assertEqual(lc.pip_size('EUR/USD'), 0.0001)

    def test_pending_until_baseline(self):
        self.assertEqual(self.idea.state, 'PENDING_BASELINE')
        self.assertIsNone(self.idea.pips(1.1))
        with self.assertRaises(ValueError):
            self.idea.close(1.1, '2026-10-06T10:00:00+00:00')

    def test_baseline_rules(self):
        with self.assertRaises(ValueError):    # quote before selection
            self.idea.record_baseline(1.12, '2026-10-05T09:00:00+00:00')
        self.idea.record_baseline(1.1200, '2026-10-05T10:05:00+00:00')
        self.assertEqual(self.idea.state, 'ACTIVE')
        with self.assertRaises(ValueError):    # never overwritten
            self.idea.record_baseline(1.13, '2026-10-05T11:00:00+00:00')

    def test_pips_direction(self):
        self.idea.record_baseline(1.1200, '2026-10-05T10:05:00+00:00')
        self.assertEqual(self.idea.pips(1.1150), 50.0)      # short, price fell: in favour
        self.assertEqual(self.idea.pips(1.1230), -30.0)
        jpy = lc.Idea('USD/JPY', 'LONG', '2026-10-05T10:00:00+00:00')
        jpy.record_baseline(157.00, '2026-10-05T10:01:00+00:00')
        self.assertEqual(jpy.pips(157.50), 50.0)

    def test_close_and_summary(self):
        self.idea.record_baseline(1.1200, '2026-10-05T10:05:00+00:00')
        self.idea.close(1.1150, '2026-10-07T10:00:00+00:00')
        self.assertEqual(self.idea.state, 'CLOSED')
        self.assertEqual(self.idea.result_pips(), 50.0)
        loser = lc.Idea('GBP/USD', 'LONG', '2026-10-05T10:00:00+00:00')
        loser.record_baseline(1.3000, '2026-10-05T10:01:00+00:00')
        loser.close(1.2980, '2026-10-06T10:00:00+00:00')
        s = lc.summarise([self.idea, loser, lc.Idea('EUR/AUD', 'SHORT', '2026-10-05T10:00:00+00:00')])
        self.assertEqual((s['closed'], s['wins'], s['pending_baseline']), (2, 1, 1))
        self.assertEqual(s['win_rate_pct'], 50.0)
        self.assertEqual(s['net_pips'], 30.0)

    def test_close_rules_flag_only(self):
        self.idea.record_baseline(1.1200, '2026-10-05T10:05:00+00:00')
        rules = [lc.adverse_pips_rule(30)]
        self.assertEqual(lc.check_close(self.idea, 1.1210, rules), [])
        self.assertEqual(len(lc.check_close(self.idea, 1.1235, rules)), 1)
        self.assertEqual(self.idea.state, 'ACTIVE')         # flagging never closes it

    def test_quote_delay(self):
        now = '2026-10-05T12:00:00+00:00'
        self.assertEqual(self.idea.quote_status('2026-10-05T11:50:00+00:00', now), 'CURRENT')
        self.assertEqual(self.idea.quote_status('2026-10-05T11:00:00+00:00', now), 'DELAYED')


class FreshnessTests(unittest.TestCase):
    NOW = datetime(2026, 10, 5, 23, 0, tzinfo=timezone.utc)

    def test_classify(self):
        t = lambda h: self.NOW - timedelta(hours=h)
        self.assertEqual(fs.classify(t(2), self.NOW, 36, 72), 'CURRENT')
        self.assertEqual(fs.classify(t(50), self.NOW, 36, 72), 'DELAYED')
        self.assertEqual(fs.classify(t(100), self.NOW, 36, 72), 'STALE')
        self.assertEqual(fs.classify(None, self.NOW, 36, 72), 'UNKNOWN')

    def test_overall_is_worst_never_green_over_warning(self):
        self.assertEqual(fs.overall(['CURRENT', 'DELAYED', 'CURRENT']), 'DELAYED')
        self.assertEqual(fs.overall(['CURRENT', 'UNKNOWN']), 'UNKNOWN')
        self.assertEqual(fs.overall(['DELAYED', 'STALE']), 'STALE')
        self.assertEqual(fs.overall([]), 'UNKNOWN')

    def test_run_stamp_parse(self):
        ts = fs.parse_run_stamp('F4P MACRO DATA — AUTO FETCHED — Last run: 2026-10-05 22:31 UTC')
        self.assertEqual(ts, datetime(2026, 10, 5, 22, 31, tzinfo=timezone.utc))
        self.assertIsNone(fs.parse_run_stamp('no stamp here'))

    def test_evaluate_with_fake_sheet(self):
        data = {
            ('FRED AUTO', 'A1'): ['F4P MACRO DATA - Last run: 2026-10-05 22:31 UTC'],
            ('FRED AUTO', 'E18:E25'): ['2026-09-29', '2026-09-29'],
        }
        r = fs.evaluate(fs.SOURCES, lambda tab, a1: data[(tab, a1)], self.NOW)
        self.assertEqual([x['status'] for x in r['sources']], ['CURRENT', 'CURRENT'])
        self.assertEqual(r['overall'], 'CURRENT')

    def test_unreadable_feed_is_unknown_not_green(self):
        def boom(tab, a1):
            raise RuntimeError('sheet unavailable')
        r = fs.evaluate(fs.SOURCES, boom, self.NOW)
        self.assertEqual(r['overall'], 'UNKNOWN')

    def test_cot_yymmdd_dates_from_live_sheet(self):
        self.assertEqual(fs.parse_date('260922'), datetime(2026, 9, 22, tzinfo=timezone.utc))
        data = {('FRED AUTO', 'A1'): ['Last run: 2026-10-05 23:33 UTC'],
                ('FRED AUTO', 'E18:E25'): ['260922'] * 8}
        r = fs.evaluate(fs.SOURCES, lambda tab, a1: data[(tab, a1)], self.NOW)
        self.assertEqual(r['sources'][1]['status'], 'DELAYED')   # 13 days old, honest flag

    def test_one_bad_cot_date_makes_feed_unknown(self):
        data = {('FRED AUTO', 'A1'): ['Last run: 2026-10-05 22:31 UTC'],
                ('FRED AUTO', 'E18:E25'): ['2026-09-29', 'garbled']}
        r = fs.evaluate(fs.SOURCES, lambda tab, a1: data[(tab, a1)], self.NOW)
        self.assertEqual(r['sources'][1]['status'], 'UNKNOWN')
        self.assertEqual(r['overall'], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
