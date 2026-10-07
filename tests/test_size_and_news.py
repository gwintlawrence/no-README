import unittest
from datetime import datetime, timezone

import f4p_gate_check as gc
import f4p_news_flag as nf
import f4p_risk_check as rk


def T(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


class SizeTests(unittest.TestCase):
    def test_usdjpy_matches_the_practice_trade(self):
        s = rk.suggest_lots('USD/JPY', 'LONG', 158.5, 158.0)
        self.assertEqual(s['lots'], 0.03)          # $10 / (50 pips * ~$6.31)
        self.assertLessEqual(s['risk_usd'], 10.0)

    def test_rounds_down_never_up(self):
        s = rk.suggest_lots('EUR/USD', 'LONG', 1.1000, 1.0988)   # 12 pips = $120/lot, $10/120 = 0.083
        self.assertEqual(s['lots'], 0.08)

    def test_portfolio_budget_limits(self):
        s = rk.suggest_lots('EUR/USD', 'LONG', 1.1000, 1.0988, other_risk_usd=45.0)
        self.assertEqual(s['limited_by'], 'portfolio budget')
        self.assertLessEqual(s['risk_usd'], 5.0)

    def test_budget_used_up_gives_zero(self):
        self.assertEqual(rk.suggest_lots('EUR/USD', 'LONG', 1.1, 1.0988, other_risk_usd=60)['lots'], 0.0)

    def test_gate_packet_shows_suggestion_and_flags_big_size(self):
        p = gc.build_gate_packet(('USD/JPY', 'LONG'), candidate_trade={
            'entry': 158.5, 'stop': 158.0, 'lots': 0.3})
        row = [c for c in p['checks'] if c['item'] == 'Suggested size'][0]
        self.assertIn('0.03', row['note'])
        self.assertIn('above that', row['note'])
        self.assertFalse(p['blocks_trade'])

    def test_no_entry_no_suggestion(self):
        p = gc.build_gate_packet(('USD/JPY', 'LONG'))
        self.assertIn('--entry', [c for c in p['checks'] if c['item'] == 'Suggested size'][0]['note'])


class NewsTests(unittest.TestCase):
    ROWS = [['2026-10-08', '18:00', 'usd', 'FOMC minutes', 'high'],
            ['2026-10-09', '12:30', 'JPY', 'Something', ''],
            ['bad row', '', 'USD', 'x']]

    def test_parse_reports_bad_rows(self):
        ev, probs = nf.parse_events(self.ROWS)
        self.assertEqual(len(ev), 2)
        self.assertEqual(len(probs), 1)

    def test_flags_event_in_window_for_either_currency(self):
        ev, _ = nf.parse_events(self.ROWS)
        r = nf.check_news('USD/JPY', T(2026, 10, 8, 14), ev)
        self.assertEqual(r['status'], 'FLAGGED')
        self.assertEqual(len(r['events']), 2)
        self.assertEqual(nf.check_news('EUR/GBP', T(2026, 10, 8, 9), ev)['status'], 'CLEAR')

    def test_far_event_is_clear(self):
        ev, _ = nf.parse_events([['2026-10-20', '12:00', 'USD', 'CPI', '']])
        self.assertEqual(nf.check_news('USD/JPY', T(2026, 10, 8, 9), ev)['status'], 'CLEAR')

    def test_empty_calendar_is_not_checked_never_clear(self):
        self.assertEqual(nf.check_news('USD/JPY', T(2026, 10, 8, 9), [])['status'], 'NOT CHECKED')

    def test_stale_calendar_is_not_checked(self):
        ev, _ = nf.parse_events([['2026-09-01', '12:00', 'USD', 'old', '']])
        self.assertEqual(nf.check_news('USD/JPY', T(2026, 10, 8, 9), ev)['status'], 'NOT CHECKED')

    def test_first_friday_reminder(self):
        self.assertEqual(nf.first_friday(2026, 10).day, 2)
        r = nf.check_news('USD/JPY', T(2026, 10, 1, 9), [])
        self.assertEqual(r['status'], 'FLAGGED')
        self.assertTrue(r['reminders'])

    def test_sheets_serial_numbers(self):
        ev, probs = nf.parse_events([[46303, 0.75, 'USD', 'Serial', '']])   # 2026-10-08 18:00
        self.assertEqual(ev[0]['when'], T(2026, 10, 8, 18))

    def test_gate_shows_news_and_never_blocks(self):
        ev, _ = nf.parse_events(self.ROWS)
        news = nf.check_news('USD/JPY', T(2026, 10, 8, 9), ev)
        p = gc.build_gate_packet(('USD/JPY', 'LONG'), news=news)
        self.assertFalse([c for c in p['checks'] if c['item'] == 'News window'][0]['ok'])
        self.assertFalse(p['blocks_trade'])


if __name__ == '__main__':
    unittest.main()
