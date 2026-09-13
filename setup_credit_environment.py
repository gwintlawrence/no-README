"""
setup_credit_environment.py

Phase 2 (Credit Environment block) for the F4P Equities & Options Hub.

Same shape as setup_rates_environment.py: daily FRED series, Current/WoW/
MoM/QoQ/YoY via nearest-date lookback (the lookback logic here is
identical and was already unit-tested for the Rates script - not
re-tested here since the code is unchanged, only the series IDs differ).

Uses FRED's ICE BofA Option-Adjusted Spread (OAS) series - these are
already spreads (extra yield demanded over Treasuries for credit risk),
not raw yields needing a separate Treasury subtraction step, so they're
a cleaner fit than reconstructing AAA/BBB/CCC yields by hand the way the
Anton/ITPM Corporate_Bond_Indices workbook did:

  - Investment Grade: BAMLC0A0CM  (ICE BofA US Corporate Index OAS)
  - High Yield:       BAMLH0A0HYM2 (ICE BofA US High Yield Index OAS)
  - HY minus IG spread: the credit-stress gap between junk and
    investment-grade - widening = credit conditions deteriorating,
    narrowing = improving. Same role here that the 2s10s spread plays
    in Rates Environment.

Writes to a new dedicated "CREDIT ENVIRONMENT" tab, same reasoning as
Rates Environment: keeps SECTOR & MACRO OVERLAY's simple schema intact,
matches the Hub's one-tab-per-concern pattern.

Required secrets:
  FRED_API_KEY, GOOGLE_CREDENTIALS, EQUITIES_SHEET_ID
  (all already in use by setup_rates_environment.py - no new secrets needed)
"""

import os
import json
import datetime
import gspread
import requests
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

TAB_NAME = "CREDIT ENVIRONMENT"

FRED_SERIES = [
    ("BAMLC0A0CM", "Investment Grade Credit Spread (OAS) %"),
    ("BAMLH0A0HYM2", "High Yield Credit Spread (OAS) %"),
]

LOOKBACKS = [("WoW", 7), ("MoM", 30), ("QoQ", 91), ("YoY", 365)]


def fetch_fred_series(series_id):
    url = (
        "https://api.stlouisfed.org/fred/series/observations"
        "?series_id=" + series_id +
        "&api_key=" + os.environ["FRED_API_KEY"] +
        "&sort_order=desc&limit=450&file_type=json"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    observations = resp.json().get("observations", [])
    valid = [
        (datetime.date.fromisoformat(o["date"]), float(o["value"]))
        for o in observations if o["value"] != "."
    ]
    return valid


def nearest_value(series, target_date, tolerance_days=5):
    best = min(series, key=lambda pair: abs((pair[0] - target_date).days))
    if abs((best[0] - target_date).days) > tolerance_days:
        return None
    return best[1]


def compute_row(series_id, label):
    series = fetch_fred_series(series_id)
    if not series:
        return [label, "N/A - Not Verified", "N/A", "N/A", "N/A", "N/A",
                "N/A", f"FRED: {series_id} (no data returned)"]

    latest_date, latest_val = series[0]
    row = [label, round(latest_val, 2)]
    for _, days_back in LOOKBACKS:
        target = latest_date - datetime.timedelta(days=days_back)
        prior_val = nearest_value(series, target)
        row.append("N/A - Not Verified" if prior_val is None else round(latest_val - prior_val, 2))
    row.append(latest_date.isoformat())
    row.append(f"FRED: {series_id} (https://fred.stlouisfed.org/series/{series_id})")
    return row


def compute_spread_row(hy_series_id, ig_series_id):
    """HY minus IG: the credit-stress gap. Rising = risk appetite
    falling (junk bonds getting relatively more expensive to insure
    against default); falling = risk appetite improving."""
    hy = fetch_fred_series(hy_series_id)
    ig = fetch_fred_series(ig_series_id)
    if not hy or not ig:
        return ["HY minus IG Spread (credit stress gap)", "N/A - Not Verified",
                "N/A", "N/A", "N/A", "N/A", "N/A", "N/A - missing source series"]

    latest_date = hy[0][0]
    latest_spread = hy[0][1] - nearest_value(ig, hy[0][0], tolerance_days=3)
    row = ["HY minus IG Spread (credit stress gap)", round(latest_spread, 2)]
    for _, days_back in LOOKBACKS:
        target = latest_date - datetime.timedelta(days=days_back)
        v_hy = nearest_value(hy, target)
        v_ig = nearest_value(ig, target)
        if v_hy is None or v_ig is None:
            row.append("N/A - Not Verified")
        else:
            prior_spread = v_hy - v_ig
            row.append(round(latest_spread - prior_spread, 2))
    row.append(latest_date.isoformat())
    row.append("FRED: BAMLH0A0HYM2 minus BAMLC0A0CM (computed)")
    return row


def get_or_create_worksheet(spreadsheet, tab_name):
    try:
        return spreadsheet.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=tab_name, rows="20", cols="8")


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(os.environ["EQUITIES_SHEET_ID"])
    ws = get_or_create_worksheet(spreadsheet, TAB_NAME)

    header = ["Indicator", "Current", "WoW \u0394", "MoM \u0394", "QoQ \u0394", "YoY \u0394", "As Of", "Source"]
    rows = [compute_row(sid, label) for sid, label in FRED_SERIES]
    rows.append(compute_spread_row("BAMLH0A0HYM2", "BAMLC0A0CM"))

    ws.clear()
    ws.update("A1", [header] + rows, raw=False)
    print(f"[OK] Wrote {len(rows)} credit indicator rows to '{TAB_NAME}'")

    for row in rows:
        if any("N/A" in str(cell) for cell in row):
            print(f"[SUSPICIOUS EMPTY] {row[0]}: at least one field could not be verified - "
                  f"check FRED_API_KEY and series availability before trusting this row.")


if __name__ == "__main__":
    main()

    
