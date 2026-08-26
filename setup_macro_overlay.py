"""
setup_macro_overlay.py

One-time (idempotent) setup script that writes LIVE cross-spreadsheet
formulas into SECTOR & MACRO OVERLAY, pulling directly from the FX Hub
(Master Macro Scorecard) instead of re-fetching macro data the FX
pipeline already maintains.

Uses IMPORTRANGE + QUERY (matched by label text, not fixed cell
position) so this stays robust if row order shifts on the FX side.

IMPORTANT - manual step required once: the first time IMPORTRANGE
references a new source spreadsheet, Google Sheets will show a
"Allow access" prompt in the cell. This has to be clicked manually in
the UI; there's no way to pre-authorize it via the API.

IMPORTANT - tab name assumption: this targets 'FRED AUTO' and
'CENTRAL BANK' tabs on the FX Hub, based on documented naming from
prior sessions. If a tab name is wrong, the formula will show a clear
#REF! or #N/A error rather than fail silently - check the Sheet after
running and correct the tab name here if needed.

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

FX_HUB_SHEET_ID = "18ZgUq7uvyodHSQvreVNoCQFiS7Ks6Gp89CBbIItcPO0"


def query_formula(tab, range_str, select_col, label_col, label_value):
    """Builds a QUERY(IMPORTRANGE(...)) formula matched by label text
    rather than a fixed cell position."""
    return (
        f'=QUERY(IMPORTRANGE("{FX_HUB_SHEET_ID}","{tab}!{range_str}"),'
        f'"select {select_col} where {label_col} = \'{label_value}\'",0)'
    )


# Each row: (Indicator label, value formula, last-updated formula, source tab ref)
MACRO_ROWS = [
    (
        "Federal Funds Rate %",
        query_formula("FRED AUTO", "A:F", "Col3", "Col2", "Federal Funds Rate %"),
        query_formula("FRED AUTO", "A:F", "Col4", "Col2", "Federal Funds Rate %"),
        "FX Hub: FRED AUTO",
    ),
    (
        "CPI YoY %",
        query_formula("FRED AUTO", "A:F", "Col3", "Col2", "CPI YoY %"),
        query_formula("FRED AUTO", "A:F", "Col4", "Col2", "CPI YoY %"),
        "FX Hub: FRED AUTO",
    ),
    (
        "Core CPI YoY %",
        query_formula("FRED AUTO", "A:F", "Col3", "Col2", "Core CPI YoY %"),
        query_formula("FRED AUTO", "A:F", "Col4", "Col2", "Core CPI YoY %"),
        "FX Hub: FRED AUTO",
    ),
    (
        "GDP Growth Rate %",
        query_formula("FRED AUTO", "A:F", "Col3", "Col2", "GDP Growth Rate %"),
        query_formula("FRED AUTO", "A:F", "Col4", "Col2", "GDP Growth Rate %"),
        "FX Hub: FRED AUTO",
    ),
    (
        "Fed Policy Bias",
        query_formula("CENTRAL BANK", "A:G", "Col2", "Col1", "FED"),
        query_formula("CENTRAL BANK", "A:G", "Col4", "Col1", "FED"),
        "FX Hub: CENTRAL BANK (Latest Meeting shown as date)",
    ),
    (
        "Fed Next FOMC Meeting",
        query_formula("CENTRAL BANK", "A:G", "Col5", "Col1", "FED"),
        "N/A",
        "FX Hub: CENTRAL BANK",
    ),
]


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(os.environ["EQUITIES_SHEET_ID"])
    ws = spreadsheet.worksheet("SECTOR & MACRO OVERLAY")

    existing = ws.get_all_values()
    if len(existing) > 1:
        ws.batch_clear([f"A2:D{len(existing)}"])

    rows = [[label, value_f, tab_ref, updated_f] for label, value_f, updated_f, tab_ref in MACRO_ROWS]
    ws.update("A2", rows, raw=False)
    print(f"[OK] Wrote {len(rows)} macro indicator rows to SECTOR & MACRO OVERLAY")
    print("[NOTE] First load will likely show 'Allow access' prompts in the affected "
          "cells - this must be clicked manually once in the Sheet UI.")
    print("[NOTE] If any cell shows #REF! or #N/A, the FX Hub tab name or label text "
          "doesn't match exactly - check FRED AUTO / CENTRAL BANK tab names and the "
          "exact indicator label text there.")


if __name__ == "__main__":
    main()

    
