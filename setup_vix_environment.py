"""
setup_vix_environment.py

Phase 2 (Risk/Volatility Environment block) for the F4P Equities & Options Hub.

Tracks VIX directly via Alpha Vantage INDEX_DATA - explicitly NOT VXX or
any other volatility ETF/ETN. The VIX_Hedges reference material reviewed
earlier is direct on this point: VXX and similar products are structurally
destined to decay from contango and don't track the VIX index itself, so
using one as a stand-in would misrepresent actual volatility conditions.

Same untested-assumption caveat as setup_equity_regime.py: Alpha Vantage's
INDEX_DATA response shape (assumed to match its standard time-series
convention) could not be confirmed live before this was written - the
premium entitlement needed wasn't available through the connector used
while building this. Your ALPHA_VANTAGE_API_KEY is premium tier and
should work; if not, send the actual output.

Vol-regime bands (Low/Normal/Elevated/High) use the standard, widely-cited
VIX interpretation convention (roughly: <15 low, 15-20 normal, 20-30
elevated, 30+ high/crisis) - not something invented for this script.

Required secrets:
  ALPHA_VANTAGE_API_KEY, GOOGLE_CREDENTIALS, EQUITIES_SHEET_ID
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


def fetch_index_series(symbol):
    url = (
        "https://www.alphavantage.co/query"
        "?function=INDEX_DATA"
        f"&symbol={symbol}&interval=daily&return_full_data=true"
        "&apikey=" + os.environ["ALPHA_VANTAGE_API_KEY"]
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    series_key = next((k for k in data if "Time Series" in k), None)
    if series_key is None:
        raise RuntimeError(
            f"[FAIL] Unexpected INDEX_DATA response shape for {symbol}. "
            f"Top-level keys were: {list(data.keys())}. Full response (first 500 chars): "
            f"{json.dumps(data)[:500]}"
        )

    parsed = []
    for date_str, values in data[series_key].items():
        close_key = next((k for k in values if "close" in k.lower()), None)
        if close_key:
            parsed.append((datetime.date.fromisoformat(date_str), float(values[close_key])))
    parsed.sort(key=lambda pair: pair[0], reverse=True)
    return parsed


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
        series = fetch_index_series("VIX")
        if not series:
            row = ["VIX (Cboe Volatility Index)", "N/A - Not Verified", "N/A",
                   "N/A", "N/A", "N/A", "N/A", "N/A", "Alpha Vantage: INDEX_DATA (VIX) - no data returned"]
        else:
            latest_date, latest_val = series[0]
            row = ["VIX (Cboe Volatility Index)", round(latest_val, 2), vol_regime_label(latest_val)]
            for _, days_back in LOOKBACKS:
                target = latest_date - datetime.timedelta(days=days_back)
                prior_val = nearest_value(series, target)
                row.append("N/A - Not Verified" if prior_val is None else round(latest_val - prior_val, 2))
            row.append(latest_date.isoformat())
            row.append("Alpha Vantage: INDEX_DATA (VIX) - NOT VXX or any volatility ETF/ETN")
        error = None
    except Exception as e:
        print(str(e))
        row = ["VIX (Cboe Volatility Index)", "N/A - Not Verified"] + ["N/A"] * 7 + [f"ERROR: {e}"]
        error = str(e)

    ws.clear()
    ws.update("A1", [header, row], raw=False)
    print(f"[OK] Wrote VIX row to '{TAB_NAME}'")
    if error:
        print(f"[FAIL] VIX fetch failed - check API response shape: {error}")


if __name__ == "__main__":
    main()

    
