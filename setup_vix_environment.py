"""
setup_vix_environment.py  (v2 - switched off INDEX_DATA)

Phase 2 (Risk/Volatility Environment block) for the F4P Equities & Options Hub.

v1 used Alpha Vantage's INDEX_DATA for VIX directly - that endpoint
returned "You are not yet entitled to index data access" against the
real key, confirmed 2026-09-12 (same failure as the Equity Regime
script). FRED carries VIX too (series VIXCLS - "CBOE Volatility Index:
VIX"), so this switches to the exact FRED fetch/lookback pattern
already twice-proven today in setup_rates_environment.py and
setup_credit_environment.py, rather than trying to fix Alpha Vantage
entitlement for one series.

Still explicitly NOT VXX or any volatility ETF/ETN - VIXCLS is the
actual index level, which is the whole point per the VIX_Hedges
reference material reviewed earlier.

Vol-regime bands (Low/Normal/Elevated/High) use the standard, widely-
cited VIX interpretation convention (roughly: <15 low, 15-20 normal,
20-30 elevated, 30+ high/crisis) - not something invented for this
script.

Required secrets:
  FRED_API_KEY, GOOGLE_CREDENTIALS, EQUITIES_SHEET_ID
  (FRED_API_KEY already in use by setup_rates_environment.py and
  setup_credit_environment.py - no new secret needed)
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

TAB_NAME = "VIX ENVIRONMENT"
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


def vol_regime_label(vix_level):
    if vix_level < 15:
        return "Low"
    elif vix_level < 20:
        return "Normal"
    elif vix_level < 30:
        return "Elevated"
    else:
        return "High / Crisis"


def get_or_create_worksheet(spreadsheet, tab_name):
    try:
        return spreadsheet.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=tab_name, rows="10", cols="10")


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(os.environ["EQUITIES_SHEET_ID"])
    ws = get_or_create_worksheet(spreadsheet, TAB_NAME)

    header = ["Indicator", "Current", "Vol Regime", "WoW \u0394", "MoM \u0394",
              "QoQ \u0394", "YoY \u0394", "As Of", "Source"]

    try:
        series = fetch_fred_series("VIXCLS")
        if not series:
            row = ["VIX (Cboe Volatility Index)", "N/A - Not Verified", "N/A",
                   "N/A", "N/A", "N/A", "N/A", "N/A", "FRED: VIXCLS - no data returned"]
        else:
            latest_date, latest_val = series[0]
            row = ["VIX (Cboe Volatility Index)", round(latest_val, 2), vol_regime_label(latest_val)]
            for _, days_back in LOOKBACKS:
                target = latest_date - datetime.timedelta(days=days_back)
                prior_val = nearest_value(series, target)
                row.append("N/A - Not Verified" if prior_val is None else round(latest_val - prior_val, 2))
            row.append(latest_date.isoformat())
            row.append("FRED: VIXCLS (https://fred.stlouisfed.org/series/VIXCLS) - NOT VXX or any volatility ETF/ETN")
        error = None
    except Exception as e:
        print(str(e))
        row = ["VIX (Cboe Volatility Index)", "N/A - Not Verified"] + ["N/A"] * 6 + [f"ERROR: {e}"]
        error = str(e)

    ws.clear()
    ws.update("A1", [header, row], raw=False)
    print(f"[OK] Wrote VIX row to '{TAB_NAME}'")
    if error:
        print(f"[FAIL] VIX fetch failed: {error}")


if __name__ == "__main__":
    main()

    
