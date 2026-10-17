"""Citation markers in Claude's answer must not reach the Hub text.

Found on 16 Oct 2026: the Weekly Hub Data Update wrote <cite index="9-2">...</cite> markers into the Institutional
Analysis text. The quotes inside the marker broke the JSON, the quote repair rescued the batch, and the markers stayed.
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import f4p_weekly_update as w


class StripCiteTags(unittest.TestCase):
    def test_markers_go_and_the_words_stay(self):
        text = 'The PMI <cite index="9-2">declined to 55.3 from 57.1</cite> in September.'
        self.assertEqual(w.strip_cite_tags(text), 'The PMI declined to 55.3 from 57.1 in September.')

    def test_several_markers_and_a_closing_tag_with_spaces(self):
        text = '<cite index="1-1,2-3">A</cite> and <CITE index="4-5">B</CITE> and <cite  index="6-7" >C</cite >'
        self.assertEqual(w.strip_cite_tags(text), 'A and B and C')

    def test_plain_text_and_other_tags_are_untouched(self):
        text = 'Rate 3.5% (hold); see <b>note</b> and cite the source, not a <citeless> tag.'
        self.assertEqual(w.strip_cite_tags(text).replace('<citeless>', ''), text.replace('<citeless>', ''))
        self.assertIn('<b>note</b>', w.strip_cite_tags(text))

    def test_json_with_markers_parses_without_repair(self):
        raw = ('{"rows": [{"currency": "CHF", "indicator_num": 1, "current_value": "55.3", '
               '"analysis": "PMI <cite index=\\"9-2\\">declined</cite> but <cite index="9-3">stayed</cite> above 50."}]}')
        cleaned = w.strip_cite_tags(raw)
        # the second marker has unescaped quotes, exactly the case that used to need repair_unescaped_quotes
        self.assertNotIn('cite', cleaned)
        data = json.loads(w.extract_json_object(cleaned))
        self.assertEqual(data['rows'][0]['analysis'], 'PMI declined but stayed above 50.')


if __name__ == '__main__':
    unittest.main()
