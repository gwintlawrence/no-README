"""
setup_rates_environment.py

Phase 2 (Rates Environment block) for the F4P Equities & Options Hub.

Fetches 10-Year and 2-Year Treasury yields from FRED (daily series),
computes Current/WoW/MoM/QoQ/YoY changes by nearest-date lookback (daily
series skip weekends/holidays, so a fixed-index lookback like the monthly
FRED_SERIES pattern in fred_cot_fetcher.py doesn't apply here), and adds
the 2s10s spread - the standard yield-curve-inversion recession signal,
explicitly called out in the Bond Market and Interest Rates Basics
reference material as "a predictor of the economic situation of the
country."

Deliberately scoped to 10Y + 2Y + the 2s10s spread, not the full 9-maturity
curve from the Anton/ITPM Benchmark_Yields workbooks - this is for an
equities dashboard, not a bond desk, and those two points are the
standard equity-market-relevant reference rate and the standard recession
signal. Full curve tracking can be added later if it turns out to matter.

Writes to a new dedicated "RATES ENVIRONMENT" tab rather than extending
SECTOR & MACRO OVERLAY's columns - keeps that tab's simple single-value
schema intact, and matches the Hub's existing one-tab-per-concern pattern
(EQUITIES HUB DATA, EQUITY RANKINGS, OPTIONS FLOW & IV, etc. are all
separate tabs already).

Required secrets:
  FRED_API_KEY        - same key already used by fred_cot_fetcher.py
  GOOGLE_CREDENTIALS   - same service account used by the rest of the Hub
  EQUITIES_SHEET_ID    - F4P Equities & Options Scorecard file ID
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

TAB_NAME = "RATES ENVIRONMENT"

FRED_SERIES = [
    ("DGS10", "10-Year Treasury Yield %"),
    ("DGS2", "2-Year Treasury Yield %"),
]

# (label, lookback in calendar days)
LOOKBACKS = [("WoW", 7), ("MoM", 30), ("QoQ", 91), ("YoY", 365)]


def fetch_fred_series(series_id):
    """Pulls a generous window of daily observations - enough to cover a
    YoY lookback plus slack for missing/holiday days - sorted most recent
    first. Mirrors the request pattern already proven in
    fred_cot_fetcher.py's get_fred_value(), just with a much larger
    window since these are daily series, not monthly."""
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
    return valid  # already sorted desc by date since the API request was


def nearest_value(series, target_date, tolerance_days=5):
    """Finds the observation closest to target_date, within tolerance.
    Daily series don't have a value every calendar day (weekends,
    holidays), so this can't be a fixed-index lookback - has to match by
    actual date distance."""
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
        if prior_val is None:
            row.append("N/A - Not Verified")
        else:
            row.append(round(latest_val - prior_val, 2))
    row.append(latest_date.isoformat())
    row.append(f"FRED: {series_id} (https://fred.stlouisfed.org/series/{series_id})")
    return row


def compute_spread_row(series_10y, series_2y):
    """2s10s spread: 10Y minus 2Y, at current and each lookback point.
    A negative value is an inverted curve - the classic recession
    signal."""
    s10 = fetch_fred_series(series_10y)
    s2 = fetch_fred_series(series_2y)
    if not s10 or not s2:
        return ["2s10s Spread (10Y minus 2Y)", "N/A - Not Verified",
                "N/A", "N/A", "N/A", "N/A", "N/A", "N/A - missing source series"]

    latest_date = s10[0][0]
    latest_spread = s10[0][1] - nearest_value(s2, s10[0][0], tolerance_days=3)
    row = ["2s10s Spread (10Y minus 2Y)", round(latest_spread, 2)]
    for _, days_back in LOOKBACKS:
        target = latest_date - datetime.timedelta(days=days_back)
        v10 = nearest_value(s10, target)
        v2 = nearest_value(s2, target)
        if v10 is None or v2 is None:
            row.append("N/A - Not Verified")
        else:
            prior_spread = v10 - v2
            row.append(round(latest_spread - prior_spread, 2))
    row.append(latest_date.isoformat())
    row.append("FRED: DGS10 minus DGS2 (computed)")
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
    rows.append(compute_spread_row("DGS10", "DGS2"))

    ws.clear()
    ws.update("A1", [header] + rows, raw=False)
    print(f"[OK] Wrote {len(rows)} rate indicator rows to '{TAB_NAME}'")

    for row in rows:
        if any("N/A" in str(cell) for cell in row):
            print(f"[SUSPICIOUS EMPTY] {row[0]}: at least one field could not be verified - "
                  f"check FRED_API_KEY and series availability before trusting this row.")


if __name__ == "__main__":
    main()

    
