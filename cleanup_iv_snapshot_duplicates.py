"""
cleanup_iv_snapshot_duplicates.py

ONE-TIME cleanup script. Not part of the regular weekly pipeline.

Fixes a confirmed bug from 2026-08-26: repeated test runs that day each
appended a full batch of 9 rows to OPTIONS FLOW & IV instead of
recognizing that day's snapshot already existed, leaving 54 rows (6x
duplication) instead of 9. The root cause is fixed in
f4p_equities_weekly_update.py going forward (de-dupes by today's date
before appending) - this script cleans up the mess that already exists.

Keeps the LAST occurrence of each (Date, Ticker) pair - the final batch
from that day reflected the most recent options data pulled, since
values legitimately drift slightly within a trading day (e.g. NVDA's
strike moved from 215 to 210 as the underlying price ticked).

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


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(os.environ["EQUITIES_SHEET_ID"])
    ws = spreadsheet.worksheet("OPTIONS FLOW & IV")

    all_values = ws.get_all_values()
    if len(all_values) <= 1:
        print("[OK] Nothing to clean - tab has no data rows")
        return

    header = all_values[0]
    data_rows = all_values[1:]
    print(f"[INFO] Found {len(data_rows)} total data rows before cleanup")

    # Keep the LAST occurrence of each (Date, Ticker) pair
    seen = {}
    for row in data_rows:
        if len(row) < 2:
            continue
        key = (row[0], row[1])  # (Date, Ticker)
        seen[key] = row  # later occurrences overwrite earlier ones

    deduped_rows = list(seen.values())
    # Sort by Date then Ticker for readability
    deduped_rows.sort(key=lambda r: (r[0], r[1]))

    removed_count = len(data_rows) - len(deduped_rows)
    print(f"[INFO] Removing {removed_count} duplicate rows, keeping {len(deduped_rows)}")

    # Rewrite the tab clean: header + deduped rows only
    ws.clear()
    ws.update("A1", [header], raw=False)
    ws.format("A1:F1", {
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        "backgroundColor": {"red": 0.09, "green": 0.13, "blue": 0.18},
    })
    ws.freeze(rows=1)
    if deduped_rows:
        ws.update("A2", deduped_rows, raw=False)

    print(f"[OK] Cleanup complete. {len(deduped_rows)} clean rows remain "
          f"(was {len(data_rows)}, removed {removed_count} duplicates).")


if __name__ == "__main__":
    main()

    
