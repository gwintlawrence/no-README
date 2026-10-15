"""Keeps the Hub, the manual Data Hunt prompts and the spec in agreement."""
import ast
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import f4p_spec
import f4p_weekly_update as w
import build_prompt_pack as bpp

SPEC = f4p_spec.load_spec()
FIX = os.path.join(ROOT, 'tests', 'fixtures')


def _consts(path, names):
    tree = ast.parse(open(os.path.join(ROOT, path), encoding='utf-8').read())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in names:
                out[node.targets[0].id] = ast.literal_eval(node.value)
    return out


class SpecMatchesPipeline(unittest.TestCase):
    def test_spec_loads(self):
        self.assertIsNotNone(SPEC)

    def test_weights_are_40_25_15_10_10(self):
        self.assertEqual(SPEC['weights_pct'],
                         {'endogenous': 40, 'central_bank': 25, 'exogenous': 15, 'cot': 10, 'volatility': 10})
        self.assertEqual(sum(SPEC['weights_pct'].values()), 100)

    def test_same_currencies_and_indicators_as_the_pipeline(self):
        cur = SPEC['endogenous']['currencies']
        self.assertEqual(list(cur), w.CURRENCIES)
        self.assertEqual(SPEC['endogenous']['hub_indicator_names'], w.INDICATORS)
        self.assertEqual(len(w.INDICATORS), 15)
        self.assertEqual(SPEC['endogenous']['max_abs_total'], 30)
        for code in w.CURRENCIES:
            inds = cur[code]['indicators']
            self.assertEqual(len(inds), 15, code)
            self.assertEqual([i['hub_name'] for i in inds], w.INDICATORS, code)
            self.assertEqual([i['num'] for i in inds], list(range(1, 16)), code)
            for i in inds:
                for k in ('id', 'measure', 'category', 'source_url', 'where_to_find', 'scoring_guide'):
                    self.assertTrue(str(i.get(k, '')).strip(), f'{code} #{i["num"]} missing {k}')
                self.assertTrue(i['source_url'].startswith('https://'), f'{code} #{i["num"]} source url')

    def test_carry_central_bank_exogenous_match_the_pipeline(self):
        self.assertEqual([(p['funding'], p['target']) for p in SPEC['carry_trade']['pairs']], w.CARRY_PAIRS)
        self.assertEqual(SPEC['central_banks']['banks'], w.CENTRAL_BANKS)
        self.assertEqual(SPEC['exogenous']['currencies'], w.CURRENCIES)

    def test_cot_codes_match_the_fetcher(self):
        fetcher = _consts('fred_cot_fetcher.py', {'COT_CODES'})
        self.assertEqual(SPEC['cot']['contract_codes'], fetcher['COT_CODES'])

    def test_usd_fred_series_match_the_fetcher(self):
        fetcher = _consts('fred_cot_fetcher.py', {'FRED_SERIES'})
        fetched = {row[0] for row in fetcher['FRED_SERIES']}
        usd = SPEC['endogenous']['currencies']['USD']['indicators']
        series = {i['hub_series']['series'] for i in usd if 'hub_series' in i}
        self.assertTrue(series)
        self.assertTrue(series <= fetched, series - fetched)
        # exactly the indicators the pipeline pre-fills from FRED
        auto = set(w.FRED_AUTO_INDICATOR_MAP)
        self.assertEqual({i['hub_name'] for i in usd if 'hub_series' in i}, auto)


class PromptsCarryTheSpec(unittest.TestCase):
    def test_every_definition_is_in_the_prompt(self):
        for code in w.CURRENCIES:
            p = w.build_prompt('{{DATE}}', [code])
            self.assertIn('INDICATOR DEFINITIONS', p)
            for i in SPEC['endogenous']['currencies'][code]['indicators']:
                for k in ('measure', 'source_url', 'where_to_find', 'scoring_guide'):
                    self.assertIn(i[k], p, f'{code} #{i["num"]} {k}')

    def test_json_only_instructions_survive(self):
        p = w.build_prompt('{{DATE}}', ['EUR'])
        self.assertIn('your response must START with the character {', p)
        self.assertIn('exactly 15 entries', p)
        self.assertIn('"indicator_num"', p)

    def test_if_the_spec_is_unavailable_the_prompt_is_exactly_the_old_one(self):
        eur = open(os.path.join(FIX, 'prompt_before_spec_EUR.txt'), encoding='utf-8').read()
        self.assertEqual(w.build_prompt('{{DATE}}', ['EUR'], spec={}), eur)
        facts = {"CPI YoY": ("3.0", "2026-09-11", "BLS"), "Consumer Sentiment": ("55.1", "2026-10-02", "UMich")}
        usd = open(os.path.join(FIX, 'prompt_before_spec_USD_facts.txt'), encoding='utf-8').read()
        self.assertEqual(w.build_prompt('{{DATE}}', ['USD'], known_facts=facts, spec={}), usd)

    def test_unreadable_spec_file_does_not_raise(self):
        self.assertIsNone(f4p_spec.load_spec('/nonexistent/spec.json'))
        self.assertEqual(f4p_spec.indicator_definitions_block(None, ['USD']), '')
        self.assertEqual(f4p_spec.indicator_definitions_block(SPEC, ['XXX']), '')


class PromptPackIsCurrent(unittest.TestCase):
    def test_pack_matches_what_the_hub_would_send(self):
        on_disk = open(os.path.join(ROOT, 'prompt_pack.json'), encoding='utf-8').read()
        self.assertEqual(on_disk, bpp.dumps(bpp.build()),
                         'prompt_pack.json is out of date. Run: python build_prompt_pack.py')

    def test_pack_has_eleven_prompts(self):
        ids = [p['id'] for p in json.loads(open(os.path.join(ROOT, 'prompt_pack.json'), encoding='utf-8').read())['prompts']]
        self.assertEqual(ids, [f'endo-{c}' for c in w.CURRENCIES] + ['carry', 'central_bank', 'exogenous'])


if __name__ == '__main__':
    unittest.main()
