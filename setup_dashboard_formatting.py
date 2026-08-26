"""
setup_dashboard_formatting.py

One-time (idempotent) setup applying conditional color formatting to
EQUITY RANKINGS and STRATEGY DASHBOARD, so Status reads as an actual
visual signal (green/amber/red/gray cells) instead of plain text -
matching the FX Hub's existing dashboard style.

Safe to re-run: clears prior conditional format rules on the target
sheets before reapplying, so running this again after adding more
ticker rows won't duplicate or stack rules.

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

GREEN = {"red": 0.72, "green": 0.88, "blue": 0.75}
RED = {"red": 0.96, "green": 0.75, "blue": 0.75}
AMBER = {"red": 1.0, "green": 0.92, "blue": 0.70}
GRAY = {"red": 0.85, "green": 0.85, "blue": 0.85}

# Generous row range so newly added tickers stay covered without
# re-running this script every time.
MAX_ROW = 100


def text_rule(sheet_id, start_row, end_row, col_index, contains_text, bg_color):
    """Builds one addConditionalFormatRule request: highlight the cell
    if it contains the given text (not exact match, so combined strings
    like 'Go / GREEN' still trigger correctly)."""
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id,
                    "startRowIndex": start_row, "endRowIndex": end_row,
                    "startColumnIndex": col_index, "endColumnIndex": col_index + 1,
                }],
                "booleanRule": {
                    "condition": {
                        "type": "TEXT_CONTAINS",
                        "values": [{"userEnteredValue": contains_text}],
                    },
                    "format": {"backgroundColor": bg_color},
                },
            },
            "index": 0,
        }
    }


def clear_existing_rules(spreadsheet, sheet_id):
    """Removes any conditional format rules already on this sheet, so
    re-running doesn't stack duplicate rules."""
    meta = spreadsheet.fetch_sheet_metadata()
    for sheet in meta.get("sheets", []):
        if sheet["properties"]["sheetId"] == sheet_id:
            existing_rules = sheet.get("conditionalFormats", [])
            requests = [
                {"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}}
                for _ in existing_rules
            ]
            if requests:
                spreadsheet.batch_update({"requests": requests})
            return


def apply_rankings_formatting(spreadsheet):
    ws = spreadsheet.worksheet("EQUITY RANKINGS")
    sheet_id = ws.id
    clear_existing_rules(spreadsheet, sheet_id)
    # Status is column D (index 3)
    requests = [
        text_rule(sheet_id, 1, MAX_ROW, 3, "Go", GREEN),
        text_rule(sheet_id, 1, MAX_ROW, 3, "Stop", RED),
        text_rule(sheet_id, 1, MAX_ROW, 3, "Wait", AMBER),
        text_rule(sheet_id, 1, MAX_ROW, 3, "INCOMPLETE", GRAY),
    ]
    spreadsheet.batch_update({"requests": requests})
    print("[OK] Applied conditional formatting to EQUITY RANKINGS (Status column)")


def apply_dashboard_formatting(spreadsheet):
    ws = spreadsheet.worksheet("STRATEGY DASHBOARD")
    sheet_id = ws.id
    clear_existing_rules(spreadsheet, sheet_id)
    # Status/Signal is column G (index 6) - combined text like "Go / GREEN"
    requests = [
        text_rule(sheet_id, 1, MAX_ROW, 6, "Go", GREEN),
        text_rule(sheet_id, 1, MAX_ROW, 6, "Stop", RED),
        text_rule(sheet_id, 1, MAX_ROW, 6, "Wait", AMBER),
        text_rule(sheet_id, 1, MAX_ROW, 6, "INCOMPLETE", GRAY),
    ]
    spreadsheet.batch_update({"requests": requests})
    print("[OK] Applied conditional formatting to STRATEGY DASHBOARD (Status/Signal column)")


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(os.environ["EQUITIES_SHEET_ID"])

    apply_rankings_formatting(spreadsheet)
    apply_dashboard_formatting(spreadsheet)
    print("\nDone. Go=green, Stop=red, Wait=amber, INCOMPLETE=gray on both tabs.")


if __name__ == "__main__":
    main()

    
