"""
setup_equities_dashboards.py

Creates/refreshes the tab structure for the F4P Equities & Options Scorecard.
Mirrors the pattern used in setup_currency_dashboards.py for the FX Hub:
  - Idempotent (safe to re-run; won't duplicate tabs)
  - Error isolation per tab (one failure doesn't block the rest)
  - raw=False so formulas added later are interpreted live, not as text

Run via GitHub Actions workflow_dispatch (setup_equities_dashboards.yml).

Required secrets:
  GOOGLE_CREDENTIALS   - same service account JSON used by the FX pipeline
  EQUITIES_SHEET_ID    - the NEW spreadsheet ID (not the FX Sheet ID)
"""

import os
import json
import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

TAB_DEFINITIONS = {
    "EQUITIES HUB DATA": [
        "Ticker", "Indicator #", "Indicator", "Current Value", "Prior Value",
        "Forecast", "Surprise", "Release Date", "Tag", "F4P Score",
        "Institutional Analysis", "Source/Audit Link",
    ],
    "EQUITY RANKINGS": [
        "Ticker", "Total Score", "Bias", "Status", "Signal", "Last Updated",
    ],
    "OPTIONS FLOW & IV": [
        "Ticker", "Put/Call Ratio", "IV Rank", "IV Percentile",
        "Unusual Volume Flag", "Institutional Holdings Change (delta)",
        "Insider Activity", "Notes",
    ],
    "EARNINGS CALENDAR": [
        "Ticker", "Next Earnings Date", "EPS Estimate", "Prior EPS",
        "Revenue Estimate", "Days to Event",
    ],
    "SECTOR & MACRO OVERLAY": [
        "Indicator", "Value", "Source Tab Reference", "Last Updated",
    ],
    "STRATEGY DASHBOARD": [
        "Ticker", "Catalyst", "Technical Setup", "Options Strategy Idea",
        "Total Score", "Bias", "Status/Signal", "Expiration Window",
    ],
    "TRADE LOG": [
        "Date", "Ticker", "Strategy", "Entry", "Expiration", "Strike(s)",
        "P/L", "Notes",
    ],
    "HOW TO USE": [
        "Section", "Instructions",
    ],
}

HEADER_FORMAT = {
    "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
    "backgroundColor": {"red": 0.09, "green": 0.13, "blue": 0.18},
}


def get_client():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def get_spreadsheet(client):
    sheet_id = os.environ["EQUITIES_SHEET_ID"]
    return client.open_by_key(sheet_id)


def ensure_tab(spreadsheet, tab_name, headers):
    try:
        try:
            ws = spreadsheet.worksheet(tab_name)
            print(f"[OK] '{tab_name}' already exists - refreshing headers only")
        except gspread.exceptions.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(
                title=tab_name, rows=200, cols=max(len(headers), 12)
            )
            print(f"[OK] Created '{tab_name}'")

        ws.update("A1", [headers], raw=False)
        end_col = gspread.utils.rowcol_to_a1(1, len(headers)).rstrip("1")
        ws.format(f"A1:{end_col}1", HEADER_FORMAT)
        ws.freeze(rows=1)
        return True
    except Exception as e:
        print(f"[FAIL] '{tab_name}': {e}")
        return False


def main():
    client = get_client()
    spreadsheet = get_spreadsheet(client)

    try:
        default_ws = spreadsheet.worksheet("Sheet1")
        if default_ws.row_count <= 1000 and not default_ws.get_all_values():
            spreadsheet.del_worksheet(default_ws)
            print("[OK] Removed empty default 'Sheet1'")
    except gspread.exceptions.WorksheetNotFound:
        pass
    except Exception as e:
        print(f"[SKIP] Could not remove default Sheet1: {e}")

    results = {name: ensure_tab(spreadsheet, name, headers)
               for name, headers in TAB_DEFINITIONS.items()}

    failed = [t for t, ok in results.items() if not ok]
    if failed:
        print(f"\nWARNING - failed tabs: {failed}")
        raise SystemExit(1)
    else:
        print(f"\nAll {len(results)} tabs created/verified successfully.")


if __name__ == "__main__":
    main()
