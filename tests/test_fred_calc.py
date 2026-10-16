"""
Year-over-year figures from FRED must be measured against the same month one year earlier.

Found on 15 Oct 2026 by comparing a manual Data Hunt with the Hub: the Hub said CPI 4.02% and Core CPI 2.79%,
the official figures were 3.7% and 2.6%. The BLS never published October 2025 CPI (government shutdown), so FRED
has no 2025-10-01 value, and "12 readings back" from September 2026 landed on August 2025: a 13-month change.
"""
import importlib
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('FRED_API_KEY', 'test-key')
os.environ.setdefault('GOOGLE_CREDENTIALS', '{}')
fetcher = importlib.import_module('fred_cot_fetcher')


def months(start_year, start_month, count, step=1.0, base=100.0):
    """Monthly observations, newest first, index rising `step` points a month from `base`."""
    obs, y, m = [], start_year, start_month
    for i in range(count):
        obs.append({'date': '%04d-%02d-01' % (y, m), 'value': str(base + step * (count - 1 - i))})
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return obs


class YearOverYear(unittest.TestCase):
    def test_plain_twelve_month_change(self):
        obs = months(2026, 9, 14, step=1.0, base=100.0)           # Sep 2026 = 113, Sep 2025 = 101
        val, date = fetcher.calculate_from_observations(obs, 'yoy')
        self.assertEqual(date, '2026-09-01')
        self.assertEqual(val, round((113 - 101) / 101 * 100, 2))

    def test_a_missing_month_does_not_turn_it_into_a_thirteen_month_change(self):
        obs = months(2026, 9, 14, step=1.0, base=100.0)
        obs = [o for o in obs if o['date'] != '2025-10-01']       # BLS never published Oct 2025 CPI
        obs.insert(10, {'date': '2025-10-01', 'value': '.'})      # FRED lists the month with a '.'
        val, _ = fetcher.calculate_from_observations(obs, 'yoy')
        sep25 = next(float(o['value']) for o in obs if o['date'] == '2025-09-01')
        self.assertEqual(val, round((113 - sep25) / sep25 * 100, 2))
        # the old method (13th reading back, after dropping the '.') gave this, which is wrong
        valid = [o for o in obs if o['value'] != '.']
        wrong = round((113 - float(valid[12]['value'])) / float(valid[12]['value']) * 100, 2)
        self.assertNotEqual(val, wrong)

    def test_blank_when_the_exact_month_is_absent(self):
        obs = [o for o in months(2026, 9, 14) if o['date'] != '2025-09-01']
        self.assertEqual(fetcher.calculate_from_observations(obs, 'yoy'), (None, '2026-09-01'))

    def test_latest_and_month_over_month(self):
        obs = months(2026, 9, 3, step=2.0, base=100.0)            # Jul 100, Aug 102, Sep 104
        self.assertEqual(fetcher.calculate_from_observations(obs, 'latest'), (104.0, '2026-09-01'))
        self.assertEqual(fetcher.calculate_from_observations(obs, 'mom'), (2.0, '2026-09-01'))
        gap = [obs[0], obs[2]]                                     # August missing
        self.assertEqual(fetcher.calculate_from_observations(gap, 'mom'), (None, '2026-09-01'))

    def test_nothing_valid(self):
        self.assertEqual(fetcher.calculate_from_observations([{'date': '2026-09-01', 'value': '.'}], 'yoy'), (None, None))
        self.assertEqual(fetcher.calculate_from_observations([], 'latest'), (None, None))

    def test_year_boundaries(self):
        self.assertEqual(fetcher._months_before('2026-01-01', 12), '2025-01-01')
        self.assertEqual(fetcher._months_before('2026-01-01', 1), '2025-12-01')
        self.assertEqual(fetcher._months_before('2026-09-01', 12), '2025-09-01')


class PpiSeriesMatchTheDefinitions(unittest.TestCase):
    def test_ppi_series_are_the_final_demand_ones(self):
        ids = {row[0]: row[2] for row in fetcher.FRED_SERIES}
        self.assertEqual(ids['PPIFIS'], 'PPI Headline YoY %')           # Final Demand, the BLS headline PPI
        self.assertEqual(ids['WPSFD49116'], 'Core PPI YoY %')           # Final demand less foods, energy and trade services
        self.assertNotIn('PPIACO', ids)                                  # all commodities: not the headline PPI
        self.assertNotIn('PPIFES', ids)                                  # still includes trade services
        self.assertEqual(set(fetcher.FRED_SOURCE_AGENCY), {row[0] for row in fetcher.FRED_SERIES})


class PermitsYearOverYear(unittest.TestCase):
    """The Building Permits guide scores the change over a year, so the weekly run is handed that figure too."""

    def test_the_fetcher_has_both_permit_rows_in_order(self):
        rows = [r for r in fetcher.FRED_SERIES if r[0] == 'PERMIT']
        self.assertEqual([(r[1], r[2]) for r in rows],
                         [('latest', 'Building Permits (000s)'), ('yoy', 'Building Permits YoY %')])
        self.assertEqual(fetcher.FRED_SERIES[-1][2], 'Building Permits YoY %')   # row 14: nothing already on the sheet moves
        self.assertEqual(len({r[2] for r in fetcher.FRED_SERIES}), len(fetcher.FRED_SERIES))  # results are keyed by label

    def test_permits_year_over_year_uses_the_same_month_a_year_earlier(self):
        obs = [{'date': '2026-08-01', 'value': '1403'}, {'date': '2026-07-01', 'value': '1433'},
               {'date': '2025-08-01', 'value': '1347'}]
        self.assertEqual(fetcher.calculate_from_observations(obs, 'yoy'), (4.16, '2026-08-01'))   # not the 2.1% fall from July

    def test_the_weekly_run_is_handed_the_year_over_year_figure(self):
        import f4p_weekly_update as w

        class WS:
            def get_all_values(self):
                return [['1', 'Building Permits (000s)', '1403', '2026-08-01', 'FRED:PERMIT', 'OK'],
                        ['11', 'Building Permits YoY %', '4.16', '2026-08-01', 'FRED:PERMIT', 'OK']]

        class SS:
            def worksheet(self, name):
                return WS()

        facts = w.read_fred_auto_facts(SS())
        self.assertEqual(facts['Building Permits / Housing'][0], '1403')
        extra = [k for k in facts if k.startswith('Building Permits / Housing - %')]
        self.assertEqual([facts[k][0] for k in extra], ['4.16'])
        prompt = w.build_prompt('{{DATE}}', ['USD'], known_facts=facts)
        self.assertIn('a year earlier', prompt)
        self.assertIn('current_value = "4.16"', prompt)


if __name__ == '__main__':
    unittest.main()
