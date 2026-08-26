"""
setup_equity_rankings.py

One-time (idempotent) setup script that writes LIVE formulas into the
EQUITY RANKINGS tab, aggregating EQUITIES HUB DATA into a per-ticker
Total Score, Bias, Status, and Signal.

This is a structure-setup script, not a weekly data-fetch script - it
writes formulas once (raw=False, so Sheets interprets them as live
formulas rather than literal text), and those formulas recalculate
automatically every time EQUITIES HUB DATA is refreshed by
f4p_equities_weekly_update.py. Re-running this script is safe; it
rewrites the same formulas rather than duplicating rows.

Guard against silent false readings: if a ticker has fewer than the
expected 12 indicator rows (e.g. an API call failed entirely rather
than writing an explicit N/A row), Status shows "INCOMPLETE" instead
of a confident Go/Wait/Stop built on partial data.

Required secrets:
  GOOGLE_CREDENTIALS   - same service account used by the rest of the Hub
  EQUITIES_SHEET_ID    - F4P Equities & Options Scorecard file ID
"""

import os
import json
import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

WATCHLIST = ["NVDA", "AAPL", "AMZN", "GOOGL", "TSLA", "META", "COIN", "NFLX", "QQQ"]

# Must match the actual indicator count in f4p_equities_weekly_update.py.
# Update this constant whenever a new indicator is added to that script -
# otherwise every ticker will show as "INCOMPLETE" even when data is fine.
EXPECTED_INDICATOR_COUNT = 12

HUB_TAB = "EQUITIES HUB DATA"


def get_client():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def build_row_formulas(row_num):
    """Returns the formula strings for one ticker's row. row_num is the
    1-indexed Sheets row (2 for the first ticker, since row 1 is the header)."""
    ticker_ref = f"A{row_num}"

    total_score = (
        f"=SUMIF('{HUB_TAB}'!A:A,{ticker_ref},'{HUB_TAB}'!J:J)"
    )
    row_count_expr = f"COUNTIF('{HUB_TAB}'!A:A,{ticker_ref})"
    bias = (
        f'=IF(B{row_num}>=6,"Bullish",IF(B{row_num}<=-6,"Bearish","Neutral"))'
    )
    status = (
        f'=IF({row_count_expr}<{EXPECTED_INDICATOR_COUNT},'
        f'"INCOMPLETE ("&{row_count_expr}&"/{EXPECTED_INDICATOR_COUNT})",'
        f'IF(B{row_num}>=8,"Go",IF(B{row_num}<=-8,"Stop","Wait")))'
    )
    signal = (
        f'=IF(D{row_num}="Go","GREEN",IF(D{row_num}="Stop","RED",'
        f'IF(LEFT(D{row_num},10)="INCOMPLETE","GRAY","AMBER")))'
    )
    last_updated = f"=MAXIFS('{HUB_TAB}'!H:H,'{HUB_TAB}'!A:A,{ticker_ref})"

    return [total_score, bias, status, signal, last_updated]


def main():
    client = get_client()
    spreadsheet = client.open_by_key(os.environ["EQUITIES_SHEET_ID"])
    ws = spreadsheet.worksheet("EQUITY RANKINGS")

    # Clear any existing data rows (keep header)
    existing = ws.get_all_values()
    if len(existing) > 1:
        ws.batch_clear([f"A2:F{len(existing)}"])

    rows = []
    for i, ticker in enumerate(WATCHLIST):
        row_num = i + 2  # row 1 is header
        formulas = build_row_formulas(row_num)
        rows.append([ticker] + formulas)

    ws.update("A2", rows, raw=False)
    print(f"[OK] Wrote {len(rows)} ticker rows with live formulas to EQUITY RANKINGS")
    print(f"[NOTE] Status guard expects {EXPECTED_INDICATOR_COUNT} indicator rows per "
          f"ticker - update EXPECTED_INDICATOR_COUNT in this script if the indicator "
          f"count in f4p_equities_weekly_update.py changes.")


if __name__ == "__main__":
    main()

    
