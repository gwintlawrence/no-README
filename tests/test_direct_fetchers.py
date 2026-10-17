"""
Batch 2: figures downloaded straight from the official source and handed to the weekly update as verified facts.

These tests use made-up downloads (no internet). They check the arithmetic, the safety rules (stale or odd
figures are refused, one failing source never stops the rest) and the round trip through the DIRECT AUTO tab.
"""
import datetime
import json
import os
import sys
import unittest
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import direct_fetchers as d  # noqa: E402

TODAY = datetime.date(2026, 10, 16)         # a Friday
D = Decimal


def day(s):
    return datetime.date.fromisoformat(s)


def facts_by_name(facts):
    return {f["name"]: f for f in facts}


class YieldFacts(unittest.TestCase):
    def points(self):
        # Mon 5 Oct .. Thu 15 Oct 2026 (Fri 16 Oct not published yet)
        vals = [("2026-10-05", "3.20"), ("2026-10-06", "3.22"), ("2026-10-07", "3.25"), ("2026-10-08", "3.27"),
                ("2026-10-09", "3.30"), ("2026-10-12", "3.31"), ("2026-10-13", "3.35"), ("2026-10-14", "3.40"),
                ("2026-10-15", "3.45")]
        return [(day(a), b) for a, b in vals]

    def test_latest_value_week_earlier_and_change(self):
        facts = d.yield_facts("CAD", self.points(), "src", TODAY)
        self.assertEqual(len(facts), 3)
        latest, earlier, change = facts
        self.assertEqual((latest["value"], latest["as_of"]), ("3.45%", "2026-10-15"))
        # one week before Thu 15 Oct is Thu 8 Oct
        self.assertEqual((earlier["value"], earlier["as_of"]), ("3.27%", "2026-10-08"))
        self.assertEqual(change["value"], "+0.18 points")
        self.assertIn("prior_value", earlier["name"])
        self.assertIn("score guide scores", change["name"])

    def test_falls_back_to_the_nearest_earlier_trading_day(self):
        pts = [(a, b) for a, b in self.points() if a != day("2026-10-08")]    # no figure on the exact day
        earlier = d.yield_facts("CAD", pts, "src", TODAY)[1]
        self.assertEqual((earlier["value"], earlier["as_of"]), ("3.25%", "2026-10-07"))

    def test_fall_is_negative_and_zero_has_no_sign(self):
        down = [(day("2026-10-08"), "3.50"), (day("2026-10-15"), "3.40")]
        self.assertEqual(d.yield_facts("CAD", down, "s", TODAY)[2]["value"], "-0.10 points")
        flat = [(day("2026-10-08"), "3.40"), (day("2026-10-15"), "3.40")]
        self.assertEqual(d.yield_facts("CAD", flat, "s", TODAY)[2]["value"], "0.00 points")

    def test_a_stale_latest_value_is_refused(self):
        old = [(day("2026-09-30"), "3.1"), (day("2026-10-07"), "3.2")]        # newest is 9 days old
        with self.assertRaises(d.FetchError):
            d.yield_facts("JPY", old, "s", TODAY)

    def test_no_value_a_week_back_is_refused(self):
        with self.assertRaises(d.FetchError):
            d.yield_facts("CAD", [(day("2026-10-14"), "3.4"), (day("2026-10-15"), "3.45")], "s", TODAY)

    def test_an_earlier_value_too_far_back_is_refused(self):
        with self.assertRaises(d.FetchError):
            d.yield_facts("CAD", [(day("2026-09-25"), "3.4"), (day("2026-10-15"), "3.45")], "s", TODAY)

    def test_silly_numbers_are_refused(self):
        with self.assertRaises(d.FetchError):
            d.yield_facts("CAD", [(day("2026-10-08"), "3.4"), (day("2026-10-15"), "345")], "s", TODAY)
        with self.assertRaises(d.FetchError):      # a 3-point jump in a week means the wrong column was read
            d.yield_facts("CAD", [(day("2026-10-08"), "0.4"), (day("2026-10-15"), "3.45")], "s", TODAY)

    def test_blank_and_dash_entries_are_skipped(self):
        pts = self.points() + [(day("2026-10-16"), "-"), (day("2026-10-14"), "")]
        self.assertEqual(d.yield_facts("CAD", pts, "s", TODAY)[0]["as_of"], "2026-10-15")


class BocValet(unittest.TestCase):
    BODY = json.dumps({"terms": {"url": "x"}, "seriesDetail": {"BD.CDN.10YR.DQ.YLD": {"label": "10-year"}},
                       "observations": [{"d": "2026-10-14", "BD.CDN.10YR.DQ.YLD": {"v": "3.40"}},
                                        {"d": "2026-10-15", "BD.CDN.10YR.DQ.YLD": {"v": "3.45"}}]})

    def test_reads_points(self):
        pts = d.parse_boc_valet(self.BODY, "BD.CDN.10YR.DQ.YLD")
        self.assertEqual(pts, [(day("2026-10-14"), "3.40"), (day("2026-10-15"), "3.45")])

    def test_wrong_layout_gives_a_helpful_error(self):
        with self.assertRaises(d.FetchError) as ctx:
            d.parse_boc_valet("<html>Maintenance</html>", "BD.CDN.10YR.DQ.YLD")
        self.assertIn("Maintenance", str(ctx.exception))

    def test_fetch_uses_the_valet_address(self):
        calls = []
        original = d.http_get_text
        d.http_get_text = lambda url, params=None, headers=None: (calls.append((url, params)) or self.BODY.replace(
            "2026-10-14", "2026-10-08").replace("3.40", "3.27"))
        try:
            facts = d.fetch_cad_yield(TODAY)
        finally:
            d.http_get_text = original
        self.assertIn("valet/observations/BD.CDN.10YR.DQ.YLD/json", calls[0][0])
        self.assertEqual(facts[0]["value"], "3.45%")
        self.assertEqual(facts[2]["value"], "+0.18 points")


class ImfFacts(unittest.TestCase):
    BODY = ('{"values": {"GGXWDG_NGDP": {"CHE": {"2023": 36.1, "2024": 35.2, "2025": 34.8, "2026": 34.5, '
            '"2027": 34.0}}}, "api": {"version": "1"}}')

    def test_reads_exact_decimals(self):
        vals = d.parse_imf_datamapper(self.BODY, "CHE")
        self.assertEqual(vals[2025], D("34.8"))
        self.assertEqual(vals[2024], D("35.2"))

    def test_uses_last_completed_year_never_a_forecast(self):
        facts = d.imf_facts("CHF", d.DEBT_NAME, d.parse_imf_datamapper(self.BODY, "CHE"), "IMF", TODAY)
        latest, prior, change = facts
        self.assertEqual(latest["as_of"], "2025")
        self.assertTrue(latest["value"].startswith("34.8%"))
        self.assertEqual(prior["as_of"], "2024")
        self.assertTrue(prior["value"].startswith("35.2%"))
        self.assertEqual(change["value"], "-0.4 points")

    def test_fiscal_balance_keeps_the_sign_and_explains_it(self):
        vals = {2024: D("-1.2"), 2025: D("-2.0")}
        latest, prior, change = d.imf_facts("GBP", d.FISCAL_NAME, vals, "IMF", TODAY)
        self.assertIn("-2.0%", latest["value"])
        self.assertIn("deficit", latest["value"])
        self.assertEqual(change["value"], "-0.8 points")
        self.assertIn("positive = surplus grew or deficit narrowed", change["name"])

    def test_missing_year_or_null_is_refused(self):
        body = '{"values": {"X": {"NZL": {"2024": 40.1, "2025": null}}}}'
        vals = d.parse_imf_datamapper(body, "NZL")
        with self.assertRaises(d.FetchError):
            d.imf_facts("NZD", d.DEBT_NAME, vals, "IMF", TODAY)

    def test_other_layouts_with_the_country_inside_are_still_found(self):
        body = '{"data": [{"series": {"CHE": {"2024": 1.5, "2025": 2.5}}}]}'
        self.assertEqual(d.parse_imf_datamapper(body, "CHE")[2025], D("2.5"))

    def test_not_json_gives_a_helpful_error(self):
        with self.assertRaises(d.FetchError) as ctx:
            d.parse_imf_datamapper("Access denied", "CHE")
        self.assertIn("Access denied", str(ctx.exception))

    def test_silly_debt_is_refused(self):
        with self.assertRaises(d.FetchError):
            d.imf_facts("CHF", d.DEBT_NAME, {2024: D("35"), 2025: D("3500")}, "IMF", TODAY)

    def test_second_address_is_tried_when_the_first_fails(self):
        seen = []

        def fake(url, params=None, headers=None):
            seen.append(url)
            if "/v2/" in url:
                raise RuntimeError("404")
            return self.BODY
        original = d.http_get_text
        d.http_get_text = fake
        try:
            facts = d.make_imf_fetcher("CHF", d.DEBT_NAME)(TODAY)
        finally:
            d.http_get_text = original
        self.assertEqual(len(seen), 2)
        self.assertEqual(facts[0]["as_of"], "2025")


class YearOnYear(unittest.TestCase):
    def months(self):
        out = []
        for m in range(1, 10):                       # Jan-Sep 2025
            out.append(((2025, m), str(1000 + m)))
        for m in range(1, 10):                       # Jan-Sep 2026
            out.append(((2026, m), str(1100 + m)))
        return out

    def test_percent_is_worked_out_to_one_decimal(self):
        facts = d.yoy_facts("CHF", d.M2_NAME, self.months(), "SNB", TODAY, "CHF million", 100, "M2 level")
        latest, prior, pct = facts
        self.assertEqual((latest["value"], latest["as_of"]), ("1109 CHF million", "2026-09"))
        self.assertEqual((prior["value"], prior["as_of"]), ("1009 CHF million", "2025-09"))
        self.assertEqual(pct["value"], "9.9%")                      # (1109-1009)/1009*100 = 9.91
        self.assertIn("(latest - year earlier) / year earlier x 100", pct["name"])

    def test_a_missing_year_earlier_month_is_refused(self):
        pts = [p for p in self.months() if p[0] != (2025, 9)]
        with self.assertRaises(d.FetchError):
            d.yoy_facts("CHF", d.M2_NAME, pts, "SNB", TODAY, "CHF million", 100, "M2 level")

    def test_an_old_latest_month_is_refused(self):
        with self.assertRaises(d.FetchError):
            # the newest month is September 2026, which ended 16 days before TODAY
            d.yoy_facts("CHF", d.M2_NAME, self.months(), "SNB", TODAY, "CHF million", 10, "M2 level")


class RunningEverything(unittest.TestCase):
    def test_one_failure_does_not_stop_the_others(self):
        def good(today):
            return d.yield_facts("CAD", [(day("2026-10-08"), "3.4"), (day("2026-10-15"), "3.5")], "s", today)

        def bad(today):
            raise d.FetchError("page layout changed")

        def crash(today):
            raise ValueError("boom")
        units = [{"currency": "CAD", "indicator": d.YIELD_NAME, "label": "boc", "run": good},
                 {"currency": "JPY", "indicator": d.YIELD_NAME, "label": "mof", "run": bad},
                 {"currency": "NZD", "indicator": d.YIELD_NAME, "label": "rbnz", "run": crash}]
        logs = []
        facts, failures = d.fetch_all(TODAY, units, log=logs.append)
        self.assertEqual(len(facts), 3)
        self.assertEqual([f["currency"] for f in failures], ["JPY", "NZD"])
        self.assertIn("page layout changed", failures[0]["reason"])
        self.assertIn("ValueError", failures[1]["reason"])
        self.assertTrue(any(line.startswith("::warning::") for line in logs))

    def test_downloads_that_run_out_of_time_are_skipped_not_hung(self):
        ran = []

        def unit(name):
            return {"currency": name, "indicator": d.YIELD_NAME, "label": name,
                    "run": lambda today: ran.append(name) or d.yield_facts(
                        "CAD", [(day("2026-10-08"), "3.4"), (day("2026-10-15"), "3.5")], "s", today)}
        facts, failures = d.fetch_all(TODAY, [unit("EUR"), unit("GBP")], log=lambda *_: None, budget_seconds=-1)
        self.assertEqual(ran, [])
        self.assertEqual(len(failures), 2)
        self.assertIn("ran out", failures[0]["reason"])

    def test_every_planned_download_names_a_currency_and_indicator(self):
        units = d.build_units()
        keys = [(u["currency"], u["indicator"]) for u in units]
        self.assertEqual(len(keys), len(set(keys)))
        for ccy in ("GBP", "JPY", "CHF", "CAD", "AUD", "NZD"):
            self.assertIn((ccy, d.DEBT_NAME), keys)
            self.assertIn((ccy, d.FISCAL_NAME), keys)
        self.assertNotIn(("USD", d.DEBT_NAME), keys)               # USD stays on FRED
        for ccy in ("EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"):
            self.assertIn((ccy, d.YIELD_NAME), keys)
        self.assertIn(("CHF", d.M2_NAME), keys)
        self.assertIn(("CHF", d.BALANCE_SHEET_NAME), keys)


class SheetRoundTrip(unittest.TestCase):
    NOW = datetime.datetime(2026, 10, 17, 14, 0)

    def rows(self):
        facts = d.yield_facts("CAD", [(day("2026-10-08"), "3.27"), (day("2026-10-15"), "3.45")], "BoC", TODAY)
        failures = [{"currency": "JPY", "indicator": d.YIELD_NAME, "label": "MOF CSV", "reason": "layout changed"}]
        return d.rows_for_sheet(facts, failures, self.NOW)

    def test_rows_have_nine_columns_and_failures_are_visible(self):
        rows = self.rows()
        self.assertTrue(all(len(r) == len(d.DIRECT_AUTO_HEADER) for r in rows))
        failed = [r for r in rows if r[0] == "JPY"][0]
        self.assertEqual(failed[3], "N/A")
        self.assertTrue(failed[6].startswith("FETCH FAILED"))
        self.assertIn("layout changed", failed[8])

    def test_reading_back_returns_ok_rows_by_currency(self):
        got = d.facts_from_rows(self.rows(), datetime.datetime(2026, 10, 18, 10, 0))
        self.assertEqual(list(got), ["CAD"])
        self.assertEqual(got["CAD"][d.YIELD_NAME], ("3.45%", "2026-10-15", "BoC"))
        self.assertEqual(len(got["CAD"]), 3)

    def test_rows_older_than_three_days_are_ignored(self):
        got = d.facts_from_rows(self.rows(), datetime.datetime(2026, 10, 21, 10, 0))
        self.assertEqual(got, {})

    def test_title_header_and_junk_rows_are_ignored(self):
        junk = [["F4P DIRECT AUTO - Last run"], [""], d.DIRECT_AUTO_HEADER, ["CAD", "x"], []]
        self.assertEqual(d.facts_from_rows(junk + self.rows(), datetime.datetime(2026, 10, 18, 10, 0)).keys(),
                         {"CAD"})


def patched(text=None, content=None, record=None):
    """Context helper: replace the two download functions with canned answers."""
    class Ctx:
        def __enter__(self_inner):
            self_inner.old = (d.http_get_text, d.http_get_bytes)

            def fake_text(url, params=None, headers=None):
                if record is not None:
                    record.append((url, params))
                return text(url, params) if callable(text) else text

            def fake_bytes(url, params=None, headers=None):
                if record is not None:
                    record.append((url, params))
                return content
            d.http_get_text, d.http_get_bytes = fake_text, fake_bytes

        def __exit__(self_inner, *a):
            d.http_get_text, d.http_get_bytes = self_inner.old
    return Ctx()


class BundesbankTimeSeriesLayout(unittest.TestCase):
    """What the live run returned on 16 Oct 2026 for format=csv: a header with the series key, description rows,
    then date, value, flag rows."""
    KEY = "BBSSY.D.REN.EUR.A630.000000WT1010.A"
    CSV = ('\ufeff"",%s,%s_FLAGS\n'
           '"",Daily yield of the current 10 year federal bond, Comment (in english),,\n'
           '"", Decimals,2,\n"", Time format code,P1D,\n"", category,,\n'
           '2026-10-08,2.55,\n2026-10-09,2.57,\n2026-10-12,.,\n2026-10-15,2.62,\n2026-10-16,2.64,\n' % (KEY, KEY))

    def test_reads_dates_and_values_and_skips_dots(self):
        pts = d.parse_sdmx_csv(self.CSV)
        self.assertEqual(pts[0], (day("2026-10-08"), "2.55"))
        self.assertEqual(len(pts), 5)
        self.assertEqual(d.clean_points(pts)[-1], (day("2026-10-16"), D("2.64")))
        self.assertNotIn(day("2026-10-12"), [p[0] for p in d.clean_points(pts)])

    def test_fetch_gives_the_right_week_earlier(self):
        with patched(text=self.CSV):
            facts = d.fetch_eur_yield(TODAY)
        self.assertEqual((facts[0]["value"], facts[0]["as_of"]), ("2.64%", "2026-10-16"))
        # one week before Friday 16 Oct is Friday 9 Oct
        self.assertEqual((facts[1]["value"], facts[1]["as_of"]), ("2.57%", "2026-10-09"))
        self.assertEqual(facts[2]["value"], "+0.07 points")


class BundesbankReader(unittest.TestCase):
    CSV = ("KEY,FREQ,REF_AREA,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
           "BBSSY.D.REN.EUR.A630.000000WT1010.A,D,DE,2026-10-08,2.55,A\n"
           "BBSSY.D.REN.EUR.A630.000000WT1010.A,D,DE,2026-10-09,2.57,A\n"
           "BBSSY.D.REN.EUR.A630.000000WT1010.A,D,DE,2026-10-12,,A\n"
           "BBSSY.D.REN.EUR.A630.000000WT1010.A,D,DE,2026-10-15,2.62,A\n")

    def test_reads_time_period_and_value(self):
        pts = d.parse_sdmx_csv(self.CSV)
        self.assertEqual(pts[0], (day("2026-10-08"), "2.55"))
        self.assertEqual(len(pts), 4)

    def test_semicolon_files_work_too(self):
        self.assertEqual(len(d.parse_sdmx_csv(self.CSV.replace(",", ";"))), 4)

    def test_fetch_builds_the_request_and_scores_the_change(self):
        seen = []
        with patched(text=self.CSV, record=seen):
            facts = d.fetch_eur_yield(TODAY)
        self.assertTrue(seen[0][0].endswith("/rest/data/BBSSY/D.REN.EUR.A630.000000WT1010.A"))
        self.assertEqual(seen[0][1]["format"], "csv")
        self.assertEqual(facts[0]["value"], "2.62%")
        self.assertEqual(facts[1]["as_of"], "2026-10-08")
        self.assertEqual(facts[2]["value"], "+0.07 points")

    def test_web_page_instead_of_data_is_reported(self):
        with self.assertRaises(d.FetchError) as ctx:
            d.parse_sdmx_csv("<html><body>Blocked</body></html>")
        self.assertIn("web page", str(ctx.exception))


class BankOfEnglandReader(unittest.TestCase):
    CSV = "DATE,IUDMNPY\n08 Oct 2026,4.40\n09 Oct 2026,4.43\n12 Oct 2026,4.45\n15 Oct 2026,4.50\n"

    def test_reads_rows(self):
        self.assertEqual(d.parse_boe_iadb(self.CSV, "IUDMNPY")[-1], (day("2026-10-15"), "4.50"))

    def test_date_style_with_slashes_and_html_error(self):
        self.assertEqual(len(d.parse_boe_iadb("DATE,IUDMNPY\n08/Oct/2026,4.4\n15/Oct/2026,4.5\n", "IUDMNPY")), 2)
        with self.assertRaises(d.FetchError):
            d.parse_boe_iadb("<html>error</html>", "IUDMNPY")

    def test_request_dates_use_english_month_names(self):
        seen = []
        with patched(text=self.CSV, record=seen):
            facts = d.fetch_gbp_yield(TODAY)
        self.assertEqual(seen[0][1]["Datefrom"], "16/Sep/2026")
        self.assertEqual(seen[0][1]["SeriesCodes"], "IUDMNPY")
        self.assertEqual(facts[2]["value"], "+0.10 points")


class JapanMofReader(unittest.TestCase):
    CSV = ("Interest Rate (as of 2026/10/15)\n"
           "Date,1Y,2Y,5Y,10Y,20Y,30Y\n"
           "2026/10/08,0.80,0.90,1.20,1.60,2.30,2.60\n"
           "2026/10/09,0.81,0.91,1.21,1.62,2.31,2.61\n"
           "2026/10/14,0.82,0.92,1.25,1.66,2.35,2.65\n"
           "2026/10/15,0.83,-,1.26,1.68,2.36,2.66\n")

    def test_reads_the_ten_year_column(self):
        pts = d.parse_mof_jgb(self.CSV)
        self.assertEqual(pts[-1], (day("2026-10-15"), "1.68"))

    def test_plain_header_without_a_title_row(self):
        pts = d.parse_mof_jgb("Date,1Y,10Y\n2026-10-15,0.8,1.7\n")
        self.assertEqual(pts, [(day("2026-10-15"), "1.7")])

    def test_missing_ten_year_column_names_what_was_seen(self):
        with self.assertRaises(d.FetchError) as ctx:
            d.parse_mof_jgb("Date,1Y,2Y\n2026/10/15,0.8,0.9\n")
        self.assertIn("10-year", str(ctx.exception))

    def test_fetch(self):
        with patched(text=self.CSV):
            facts = d.fetch_jpy_yield(TODAY)
        self.assertEqual((facts[0]["value"], facts[1]["value"], facts[2]["value"]),
                         ("1.68%", "1.60%", "+0.08 points"))


class RbaReader(unittest.TestCase):
    CSV = ("F2  CAPITAL MARKET YIELDS - GOVERNMENT BONDS - DAILY\n"
           "Title,Australian Government 2 year bond,Australian Government 10 year bond\n"
           "Frequency,Daily,Daily\n"
           "Units,Per cent per annum,Per cent per annum\n"
           "Series ID,FCMYGBAG2D,FCMYGBAG10D\n"
           "08-Oct-2026,3.90,4.60\n"
           "09-Oct-2026,3.92,4.62\n"
           "14-Oct-2026,3.95,4.66\n"
           "15-Oct-2026,3.96,4.70\n")

    def test_finds_the_column_by_series_id(self):
        self.assertEqual(d.parse_rba_table(self.CSV, d.RBA_SERIES)[-1], (day("2026-10-15"), "4.70"))

    def test_unknown_series_is_reported(self):
        with self.assertRaises(d.FetchError):
            d.parse_rba_table(self.CSV, ("NOPE",))

    def test_fetch(self):
        with patched(text=self.CSV):
            facts = d.fetch_aud_yield(TODAY)
        self.assertEqual((facts[0]["value"], facts[2]["value"]), ("4.70%", "+0.10 points"))


class SnbReader(unittest.TestCase):
    META = '"CubeId";"{cube}"\n"PublishingDate";"2026-10-15 09:00"\n\n'

    def cube(self, cube, header, rows):
        return self.META.format(cube=cube) + ";".join('"%s"' % h for h in header) + "\n" + "\n".join(
            ";".join('"%s"' % c for c in r) for r in rows) + "\n"

    def test_reads_dimensions_and_rows(self):
        text = self.cube("snbmonagg", ["Date", "D0", "Value"], [["2026-08", "M1", "700000"], ["2026-08", "M2", "1100000"]])
        dims, rows = d.parse_snb_cube(text)
        self.assertEqual(dims, ["D0"])
        self.assertEqual(rows[1], {"Date": "2026-08", "D0": "M2", "Value": "1100000"})

    def test_picks_one_series(self):
        text = self.cube("snbmonagg", ["Date", "D0", "Value"],
                         [["2026-07", "M1", "1"], ["2026-07", "M2", "2"], ["2026-08", "M2", "3"]])
        dims, rows = d.parse_snb_cube(text)
        self.assertEqual([r["Value"] for r in d.snb_series(dims, rows, ["M2"], "M2")], ["2", "3"])

    def test_more_than_one_series_left_is_reported_with_what_each_dimension_holds(self):
        text = self.cube("rendoblid", ["Date", "D0", "D1", "Value"],
                         [["2026-10-15", "10J", "CHF", "0.5"], ["2026-10-15", "10J", "EUR", "2.6"]])
        dims, rows = d.parse_snb_cube(text)
        with self.assertRaises(d.FetchError) as ctx:
            d.snb_series(dims, rows, d.SNB_TEN_YEAR_CODES, "the 10-year yield")
        self.assertIn("CHF", str(ctx.exception))
        self.assertIn("EUR", str(ctx.exception))

    def test_no_match_lists_what_the_cube_holds(self):
        text = self.cube("x", ["Date", "D0", "Value"], [["2026-08", "M1", "1"]])
        dims, rows = d.parse_snb_cube(text)
        with self.assertRaises(d.FetchError) as ctx:
            d.snb_series(dims, rows, ["M2"], "M2")
        self.assertIn("M1", str(ctx.exception))

    def test_yield_fetch(self):
        rows = [["2026-10-08", "10J", "0.40"], ["2026-10-09", "10J", "0.41"], ["2026-10-15", "10J", "0.52"],
                ["2026-10-15", "5J", "0.20"]]
        with patched(text=self.cube("rendoblid", ["Date", "D0", "Value"], rows)):
            facts = d.fetch_chf_yield(TODAY)
        self.assertEqual((facts[0]["value"], facts[2]["value"]), ("0.52%", "+0.12 points"))

    def m2_cube(self, vv_offset=Decimal("0")):
        """The layout the live run showed: D0 is B or VV, D1 is the aggregate (GM1, GM2, GM3 ...)."""
        rows = []
        levels = {}
        for y, base in ((2025, 1000000), (2026, 1030000)):
            for m in range(1, 9):
                levels[(y, m)] = base + m * 1000
        for (y, m), v in levels.items():
            rows.append(["%d-%02d" % (y, m), "B", "GM2", str(v)])
            rows.append(["%d-%02d" % (y, m), "B", "GM1", "5000"])
            if y == 2026:
                prior = levels[(2025, m)]
                pct = ((Decimal(v) - prior) / prior * 100 + vv_offset).quantize(Decimal("0.1"))
                rows.append(["%d-%02d" % (y, m), "VV", "GM2", str(pct)])
        return self.cube("snbmonagg", ["Date", "D0", "D1", "Value"], rows)

    def test_m2_fetch_uses_the_level_series_and_works_the_percent_out(self):
        seen = []
        with patched(text=self.m2_cube(), record=seen):
            facts = d.fetch_chf_m2(TODAY)
        self.assertEqual(seen[0][1]["fromDate"], "2024-10")
        self.assertEqual(facts[0]["as_of"], "2026-08")
        self.assertEqual(facts[0]["value"], "1038000 million francs")
        self.assertEqual(facts[1]["value"], "1008000 million francs")
        self.assertEqual(facts[2]["value"], "3.0%")                  # (1038000-1008000)/1008000*100 = 2.98

    def test_m2_is_refused_when_the_other_series_disagrees(self):
        with patched(text=self.m2_cube(vv_offset=Decimal("2.5"))):
            with self.assertRaises(d.FetchError) as ctx:
                d.fetch_chf_m2(TODAY)
        self.assertIn("nothing is used", str(ctx.exception))

    def test_m2_without_a_gm2_series_lists_what_the_cube_holds(self):
        text = self.cube("snbmonagg", ["Date", "D0", "D1", "Value"], [["2026-08", "B", "GM1", "5000"]])
        with patched(text=text):
            with self.assertRaises(d.FetchError) as ctx:
                d.fetch_chf_m2(TODAY)
        self.assertIn("GM1", str(ctx.exception))

    def test_m2_that_does_not_look_like_a_level_is_refused(self):
        rows = [["2026-08", "B", "GM2", "3.2"], ["2025-08", "B", "GM2", "3.1"]]
        with patched(text=self.cube("snbmonagg", ["Date", "D0", "D1", "Value"], rows)):
            with self.assertRaises(d.FetchError):
                d.fetch_chf_m2(TODAY)

    def test_two_ten_year_codes_are_not_guessed_between(self):
        rows = [["2026-10-15", "10J0", "0.52"], ["2026-10-15", "10J1", "0.60"], ["2026-10-15", "5J", "0.2"],
                ["2026-10-08", "10J0", "0.40"], ["2026-10-08", "10J1", "0.48"]]
        with patched(text=self.cube("rendoblid", ["Date", "D0", "Value"], rows)):
            with self.assertRaises(d.FetchError) as ctx:
                d.fetch_chf_yield(TODAY)
        msg = str(ctx.exception)
        self.assertIn("10J0", msg)
        self.assertIn("10J1", msg)
        self.assertIn("0.60", msg)

    def test_the_chosen_ten_year_code_is_used_once_it_is_set(self):
        rows = [["2026-10-15", "10J0", "0.52"], ["2026-10-15", "10J1", "0.60"], ["2026-10-08", "10J0", "0.40"],
                ["2026-10-08", "10J1", "0.48"]]
        old = d.SNB_TEN_YEAR_CODE
        d.SNB_TEN_YEAR_CODE = "10J0"
        try:
            with patched(text=self.cube("rendoblid", ["Date", "D0", "Value"], rows)):
                facts = d.fetch_chf_yield(TODAY)
        finally:
            d.SNB_TEN_YEAR_CODE = old
        self.assertEqual((facts[0]["value"], facts[2]["value"]), ("0.52%", "+0.12 points"))

    def test_balance_sheet_is_shown_in_billions(self):
        rows = [["2025-08", "T0", "800000"], ["2026-08", "T0", "840000"], ["2026-08", "T1", "840000"]]
        with patched(text=self.cube("snbbipo", ["Date", "D0", "Value"], rows)):
            facts = d.fetch_chf_balance_sheet(TODAY)
        self.assertEqual(facts[0]["value"], "840.0 billion francs")
        self.assertEqual(facts[1]["value"], "800.0 billion francs")
        self.assertEqual(facts[2]["value"], "5.0%")


try:
    import openpyxl  # noqa: F401
    HAVE_OPENPYXL = True
except ImportError:
    HAVE_OPENPYXL = False


@unittest.skipUnless(HAVE_OPENPYXL, "openpyxl not installed")
class RbnzReader(unittest.TestCase):
    def workbook(self, heading):
        import io
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Data"
        ws.append(["Series Id", "INM.DB90", "INM.DG10"])
        ws.append(["Description", "90-day bank bill", heading])
        ws.append(["Units", "Percent", "Percent"])
        for dt, a, b in ((datetime.datetime(2026, 10, 8), 3.1, 4.40), (datetime.datetime(2026, 10, 15), 3.2, 4.52)):
            ws.append([dt, a, b])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_finds_the_ten_year_government_bond_column(self):
        pts = d.parse_rbnz_b2(self.workbook("Government bond 10 year (secondary market yield)"))
        self.assertEqual(pts[-1], (day("2026-10-15"), 4.52))

    def test_fetch(self):
        with patched(content=self.workbook("Government bond 10 year")):
            facts = d.fetch_nzd_yield(TODAY)
        self.assertEqual((facts[0]["value"], facts[2]["value"]), ("4.52%", "+0.12 points"))

    def test_the_request_carries_browser_style_headers(self):
        seen = {}
        old = d.http_get_bytes
        d.http_get_bytes = lambda url, params=None, headers=None: (seen.update(headers=headers) or
                                                                    self.workbook("Government bond 10 year"))
        try:
            d.fetch_nzd_yield(TODAY)
        finally:
            d.http_get_bytes = old
        self.assertIn("Referer", seen["headers"])
        self.assertIn("Accept-Language", seen["headers"])

    def test_unrecognised_headings_are_listed(self):
        with self.assertRaises(d.FetchError) as ctx:
            d.parse_rbnz_b2(self.workbook("something else"))
        self.assertIn("headings seen", str(ctx.exception))

    def test_not_an_excel_file(self):
        with self.assertRaises(d.FetchError):
            d.parse_rbnz_b2(b"<html>Access denied</html>")


class FakeSheet:
    def __init__(self, tabs):
        self.tabs = tabs

    def worksheet(self, name):
        if name not in self.tabs:
            raise KeyError(name)
        rows = self.tabs[name]

        class WS:
            def get_all_values(self_inner):
                return rows
        return WS()


class WeeklyUpdateUsesTheFacts(unittest.TestCase):
    """The Sunday update hands each non-USD currency its own verified figures, and falls back cleanly."""

    def setUp(self):
        import f4p_weekly_update as w
        self.w = w
        now = datetime.datetime.utcnow()
        facts = d.yield_facts("CAD", [(day("2026-10-08"), "3.27"), (day("2026-10-15"), "3.45")], "Bank of Canada Valet", TODAY)
        self.rows = d.rows_for_sheet(facts, [], now)
        self.sheet = FakeSheet({"DIRECT AUTO": [["title"], [""], d.DIRECT_AUTO_HEADER] + self.rows})

    def test_reads_fresh_rows(self):
        got = self.w.read_direct_auto_facts(self.sheet)
        self.assertEqual(list(got), ["CAD"])
        self.assertEqual(got["CAD"][d.YIELD_NAME][0], "3.45%")

    def test_missing_tab_is_not_an_error(self):
        self.assertEqual(self.w.read_direct_auto_facts(FakeSheet({})), {})

    def test_prompt_lists_the_verified_figures_for_that_currency_only(self):
        facts = self.w.read_direct_auto_facts(self.sheet)
        with_facts = self.w.build_prompt("October 18, 2026", ["CAD"], known_facts=facts["CAD"])
        self.assertIn('current_value = "3.45%"', with_facts)
        self.assertIn("already verified", with_facts)
        self.assertIn("+0.18 points", with_facts)
        plain_prompt = self.w.build_prompt("October 18, 2026", ["EUR"])
        self.assertNotIn("already verified", plain_prompt)

    def test_prompt_explains_which_line_is_the_current_value(self):
        facts = self.w.read_direct_auto_facts(self.sheet)["CAD"]
        note = "supporting figure for the indicator named before the dash"
        self.assertIn(note, self.w.build_prompt("October 18, 2026", ["CAD"], known_facts=facts))
        # USD's prompt is left exactly as it was
        usd = {"CPI YoY": ("3.7", "Sep 2026", "FRED"), "Building Permits / Housing - % change versus the same "
               "month a year earlier": ("2.0", "Sep 2026", "FRED")}
        self.assertNotIn(note, self.w.build_prompt("October 18, 2026", ["USD"], known_facts=usd))

    def test_search_budget(self):
        facts = self.w.read_direct_auto_facts(self.sheet)["CAD"]
        self.assertEqual(self.w.search_budget("CAD", facts), 19)          # one indicator covered
        self.assertEqual(self.w.search_budget("EUR", None), 20)          # nothing verified: as before
        self.assertEqual(self.w.search_budget("USD", {}), 20)            # USD with no FRED figures: as before
        self.assertEqual(self.w.search_budget("USD", {"CPI YoY": ("1", "x", "y")}), 12)   # USD unchanged

    def test_run_gives_each_batch_only_its_own_facts(self):
        w = self.w
        seen = {}

        def fake_call(prompt, max_uses=20):
            ccy = [c for c in w.CURRENCIES if ("for these currencies ONLY: " + c) in prompt.replace("\n", " ")
                   or ("ONLY: " + c + ".") in prompt][0]
            seen[ccy] = (prompt, max_uses)
            return {"rows": [{"currency": ccy, "indicator_num": i + 1, "indicator": n, "current_value": "1",
                              "prior_value": "1", "forecast": "1", "surprise": "In line", "release_date": "x",
                              "tag": "t", "score": 0, "analysis": "a", "source_url": "u"}
                             for i, n in enumerate(w.INDICATORS)],
                    "data_unavailable_flags": []}
        saved = (w.call_claude, w.write_hub_data, w.write_rankings)
        w.call_claude = fake_call
        w.write_hub_data = lambda *a, **k: None
        w.write_rankings = lambda *a, **k: None
        try:
            w.run_hub_data(self.sheet, "October 18, 2026")
        finally:
            w.call_claude, w.write_hub_data, w.write_rankings = saved
        self.assertEqual(set(seen), set(w.CURRENCIES))
        self.assertIn('current_value = "3.45%"', seen["CAD"][0])
        self.assertEqual(seen["CAD"][1], 19)
        for other in ("EUR", "GBP", "JPY", "CHF", "AUD", "NZD"):
            self.assertNotIn("already verified", seen[other][0])
            self.assertEqual(seen[other][1], 20)
        self.assertNotIn("3.45%", seen["USD"][0])


if __name__ == "__main__":
    unittest.main()
