#!/usr/bin/env python3
"""
F4P Currency Dashboard Setup (one-time)
========================================
Creates 8 new "<CCY> Scorecard" tabs in the Master Macro Scorecard sheet,
one per currency, replicating the F4P GWL Weekly Endogenous Dashboard
templates - but built entirely out of live formulas instead of typed
values, so they update automatically every week the pipeline runs. No
more retyping numbers or repainting Bullish/Bearish/Go-Wait-Stop cell
colors by hand - the formulas and conditional formatting rules do that.

This is a ONE-TIME setup script, not part of the weekly pipeline. Safe to
re-run (idempotent) - re-running rebuilds a tab from scratch, which is
the fix if someone accidentally overtypes a formula cell.

Data sources referenced (already live in the Hub - nothing new to fetch):
  - 'AI HUB DATA' tab  -> 15-indicator table + Total Score
  - 'FRED AUTO' tab    -> COT Long/Short/Net/Signal (rows 18-25)

NOT yet wired: Pair Trade Readiness (Status/Signal/Bias/Explanation per
pair). There is currently no live source for this anywhere in the Hub -
see the open thread on connecting the Weekly Endogenous Engine's output
into a PAIR_READINESS tab. Until that exists, this script writes the
pair rows as placeholders. Re-run with --currency <CCY> to rebuild a
single tab once that source exists, or extend build_dashboard() below to
point the Explanation/Status/Signal/Bias formulas at the new tab.

Usage:
  python setup_currency_dashboards.py                 # build all 8
  python setup_currency_dashboards.py --currency USD  # build/rebuild just one
  python setup_currency_dashboards.py --dry-run        # print, don't write

Required environment variables / GitHub Secrets (same ones already used by
f4p_weekly_update.py and fred_cot_fetcher.py):
  GOOGLE_CREDENTIALS  - JSON string of your Google service account credentials
  GOOGLE_SHEET_ID     - your F4P Google Sheet ID
"""

import argparse
import os
import json

import gspread
from google.oauth2.service_account import Credentials


CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"]

# Must match AI HUB DATA column C exactly - see f4p_weekly_update.py INDICATORS.
# (Slightly longer/different wording than the original xlsx templates used;
# these are the canonical names the pipeline actually writes, so formulas
# built on anything else silently return nothing.)
INDICATORS = [
    "Manufacturing PMI",
    "Services PMI",
    "Consumer Sentiment",
    "Building Permits / Housing",
    "M2 Money Supply",
    "Central Bank Rate & Current Policy Stance",
    "CPI YoY",
    "Core CPI",
    "PPI",
    "Core PPI",
    "Employment",
    "Government Debt / GDP",
    "Fiscal Balance",
    "10-Year Government Bond Yield",
    "Central Bank Balance Sheet / GDP",
]

HUB_TAB = "AI HUB DATA"          # columns A:L, see f4p_weekly_update.py HEADER_ROW
FRED_TAB = "FRED AUTO"           # COT block lives at A18:F25 (see fred_cot_fetcher.py)
COT_RANGE = "A18:F25"            # Currency | Net Position | Long | Short | As Of Date | Signal
COT_SOURCE_URL = "https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm"

# Base-currency precedence for standard FX pair notation (left beats right)
PAIR_PRECEDENCE = ["EUR", "GBP", "AUD", "NZD", "USD", "CAD", "CHF", "JPY"]

HEADER_FILL = {"red": 0.13, "green": 0.15, "blue": 0.20}
HEADER_TEXT = {"red": 1, "green": 1, "blue": 1}

BULLISH_COLOR = {"red": 0.78, "green": 0.94, "blue": 0.81}
BEARISH_COLOR = {"red": 0.96, "green": 0.78, "blue": 0.81}
NEUTRAL_COLOR = {"red": 1.00, "green": 0.92, "blue": 0.61}
GO_COLOR = {"red": 0.0, "green": 1.0, "blue": 0.0}
WAIT_COLOR = {"red": 1.0, "green": 1.0, "blue": 0.0}
STOP_COLOR = {"red": 1.0, "green": 0.0, "blue": 0.0}


def pair_name(a, b):
    """Standard FX notation for the pair between two currencies."""
    ia, ib = PAIR_PRECEDENCE.index(a), PAIR_PRECEDENCE.index(b)
    base, quote = (a, b) if ia < ib else (b, a)
    return f"{base}/{quote}"


def connect_sheet():
    creds_json = os.environ["GOOGLE_CREDENTIALS"]
    creds_dict = json.loads(creds_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    return gc.open_by_key(sheet_id)


def get_or_create_tab(spreadsheet, tab_name, rows, cols):
    try:
        ws = spreadsheet.worksheet(tab_name)
        ws.resize(rows=rows, cols=cols)
        return ws
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=tab_name, rows=str(rows), cols=str(cols))


def hub_lookup(column_letter, ccy, indicator):
    """QUERY formula pulling one field from AI HUB DATA for a given currency+indicator.
    IFERROR -> em-dash so a missing week (e.g. a failed AUD batch) shows as a
    blank cell instead of a scary #N/A across the whole tab."""
    query = "select %s where A='%s' and C='%s' limit 1" % (column_letter, ccy, indicator)
    return "=IFERROR(QUERY('%s'!A:L,\"%s\",0),\"—\")" % (HUB_TAB, query)


def fred_lookup(column_letter, ccy):
    query = "select %s where A='%s' limit 1" % (column_letter, ccy)
    return "=IFERROR(QUERY('%s'!%s,\"%s\",0),\"—\")" % (FRED_TAB, COT_RANGE, query)


def bias_formula(score_cell):
    return (
        '=IF(%s="","—",IF(N(%s)>0,"Bullish",IF(N(%s)<0,"Bearish","Neutral")))'
        % (score_cell, score_cell, score_cell)
    )


def build_dashboard(spreadsheet, ccy, dry_run=False):
    tab_name = f"{ccy} Scorecard"
    print(f"[setup] Building {tab_name}...")

    values = [
        [f"Fishin4Pips (F4P) Weekly Endogenous Scorecard: {ccy}", "", "", "", "", ""],
        ["Indicator", "Previous", "Latest", "Bias", "Score", "Source Link"],
    ]

    for i, ind in enumerate(INDICATORS, start=1):
        row_num = 3 + (i - 1)  # row 3 = indicator 1
        values.append([
            f"{i}. {ind}",
            hub_lookup("E", ccy, ind),   # Previous
            hub_lookup("D", ccy, ind),   # Latest
            bias_formula(f"E{row_num}"),  # Bias, derived from this row's Score
            hub_lookup("J", ccy, ind),   # Score
            hub_lookup("L", ccy, ind),   # Source
        ])

    total_row = 3 + len(INDICATORS)  # row 18
    values.append([
        "TOTAL SCORE", "", "",
        bias_formula(f"E{total_row}"),
        f"=SUMIF('{HUB_TAB}'!$A:$A,\"{ccy}\",'{HUB_TAB}'!$J:$J)",
        "",
    ])

    values.append([""] * 6)
    values.append(["Commitment of Traders (COT) Analysis (CFTC Data)", "", "", "", "", ""])
    cot_header_row = len(values) + 1
    values.append(["Metric", "Long Contracts", "Short Contracts", "Net Positions", "As Of Date", "Source Link"])
    cot_data_row = len(values) + 1
    values.append([
        f"{ccy} Futures (Leveraged Funds)",
        fred_lookup("C", ccy),   # Long
        fred_lookup("D", ccy),   # Short
        fred_lookup("B", ccy),   # Net
        fred_lookup("E", ccy),   # As Of Date
        COT_SOURCE_URL,
    ])
    values.append(["Note: weekly WoW change isn't available live yet - FRED AUTO is overwritten"
                    " each run with no history kept. Ping me once we want that tracked.", "", "", "", "", ""])

    values.append([""] * 6)
    values.append([f"{ccy} Pairs Trade Readiness — PENDING (Weekly Endogenous Engine not yet connected to the Hub)",
                    "", "", "", "", ""])
    pairs_header_row = len(values) + 1
    values.append(["Currency Pair", "Status", "Signal", "Bias", "Explanation", ""])
    pairs_start_row = len(values) + 1
    others = [c for c in CURRENCIES if c != ccy]
    for other in others:
        values.append([pair_name(ccy, other), "—", "—", "—", "Pending pair-readiness data source", ""])
    pairs_end_row = len(values)

    if dry_run:
        print(f"  (dry run - {len(values)} rows, not written)")
        return

    ws = get_or_create_tab(spreadsheet, tab_name, rows=len(values) + 5, cols=6)
    ws.clear()
    ws.update(values, "A1")

    ws.merge_cells("A1:F1")
    ws.format("A1:F1", {"textFormat": {"bold": True, "fontSize": 13}})
    ws.format("A2:F2", {"textFormat": {"bold": True, "foregroundColor": HEADER_TEXT}, "backgroundColor": HEADER_FILL})
    ws.format(f"A{cot_header_row}:F{cot_header_row}",
              {"textFormat": {"bold": True, "foregroundColor": HEADER_TEXT}, "backgroundColor": HEADER_FILL})
    ws.format(f"A{pairs_header_row}:F{pairs_header_row}",
              {"textFormat": {"bold": True, "foregroundColor": HEADER_TEXT}, "backgroundColor": HEADER_FILL})

    add_conditional_formatting(spreadsheet, ws, total_row, pairs_start_row, pairs_end_row)
    print(f"  done - {tab_name} ({len(values)} rows)")


def add_conditional_formatting(spreadsheet, ws, total_row, pairs_start_row, pairs_end_row):
    sheet_id = ws.id
    requests = []

    def add_rule(ranges_a1, text, color):
        grid_ranges = [gspread.utils.a1_range_to_grid_range(r, sheet_id) for r in ranges_a1]
        requests.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": grid_ranges,
                    "booleanRule": {
                        "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": text}]},
                        "format": {"backgroundColor": color},
                    },
                },
                "index": 0,
            }
        })

    bias_ranges = [f"D3:D{total_row}", f"D{pairs_start_row}:D{pairs_end_row}"]
    add_rule(bias_ranges, "Bullish", BULLISH_COLOR)
    add_rule(bias_ranges, "Bearish", BEARISH_COLOR)
    add_rule(bias_ranges, "Neutral", NEUTRAL_COLOR)

    status_range = [f"B{pairs_start_row}:B{pairs_end_row}"]
    add_rule(status_range, "Go", GO_COLOR)
    add_rule(status_range, "Wait", WAIT_COLOR)
    add_rule(status_range, "Stop", STOP_COLOR)

    spreadsheet.batch_update({"requests": requests})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--currency", choices=CURRENCIES, help="Build/rebuild just one currency's tab")
    parser.add_argument("--dry-run", action="store_true", help="Print without writing to the Sheet")
    args = parser.parse_args()

    spreadsheet = None if args.dry_run else connect_sheet()
    targets = [args.currency] if args.currency else CURRENCIES
    for ccy in targets:
        build_dashboard(spreadsheet, ccy, dry_run=args.dry_run)

    print("[setup] Done.")


if __name__ == "__main__":
    main()
