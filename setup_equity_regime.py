"""
setup_equity_regime.py

Phase 2 (Equity Market Regime block) for the F4P Equities & Options Hub.

Implements the exact Anton/ITPM Bull_Bear_Markets methodology verified
earlier from the supplied workbook: Bear = current price 20% or more
below its rolling all-time high; Bull = price back above that same
80%-of-peak threshold. This is a level check against the historical
high, not a simple day-over-day move.

Tracks SPX (S&P 500) and NDX (Nasdaq 100) - broad market plus the
tech-heavy index that better matches this Hub's own watchlist
(NVDA/AAPL/AMZN/GOOGL/TSLA/META/COIN/NFLX/QQQ skews growth/tech).

*** IMPORTANT - UNTESTED ASSUMPTION, UNLIKE RATES/CREDIT ***
Alpha Vantage's INDEX_DATA endpoint requires premium-tier entitlement,
which the MCP connector available while building this did not have -
so unlike setup_rates_environment.py and setup_credit_environment.py,
the exact response shape below (a "Time Series (Daily)" dict keyed by
date, matching Alpha Vantage's general time-series convention) has NOT
been confirmed against a real response. Your ALPHA_VANTAGE_API_KEY is
premium tier and should have access, but if this errors or the parsed
values look wrong, send the actual output and it'll get fixed against
what's really there rather than this assumption.

Required secrets:
  ALPHA_VANTAGE_API_KEY, GOOGLE_CREDENTIALS, EQUITIES_SHEET_ID
  (ALPHA_VANTAGE_API_KEY already in use by f4p_equities_weekly_update.py)
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

TAB_NAME = "EQUITY MARKET REGIME"
BEAR_THRESHOLD_PCT = 0.20  # 20% fall from high, per the Anton/ITPM workbook

INDICES = [
    ("SPX", "S&P 500"),
    ("NDX", "Nasdaq 100"),
]

LOOKBACKS = [("WoW", 7), ("MoM", 30), ("QoQ", 91), ("YoY", 365)]


def fetch_index_series(symbol):
    """Pulls full daily history for an index via Alpha Vantage INDEX_DATA.
    Response shape assumed to match Alpha Vantage's standard time-series
    convention - see the untested-assumption note above."""
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


def compute_regime_row(symbol, label):
    series = fetch_index_series(symbol)
    if not series:
        return [label, "N/A - Not Verified", "N/A", "N/A", "N/A", "N/A", "N/A", "N/A",
                f"Alpha Vantage: INDEX_DATA ({symbol}) - no data returned"]

    latest_date, latest_close = series[0]
    all_time_high = max(v for _, v in series)
    bear_threshold = round(all_time_high * (1 - BEAR_THRESHOLD_PCT), 2)
    pct_from_high = round((latest_close - all_time_high) / all_time_high * 100, 2)
    regime = "Bear" if latest_close < bear_threshold else "Bull"

    row = [label, latest_close, all_time_high, bear_threshold, f"{pct_from_high}%", regime]
    for _, days_back in LOOKBACKS:
        target = latest_date - datetime.timedelta(days=days_back)
        prior_val = nearest_value(series, target)
        row.append("N/A - Not Verified" if prior_val is None
                    else f"{round((latest_close - prior_val) / prior_val * 100, 2)}%")
    row.append(latest_date.isoformat())
    row.append(f"Alpha Vantage: INDEX_DATA ({symbol})")
    return row


def get_or_create_worksheet(spreadsheet, tab_name):
    try:
        return spreadsheet.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=tab_name, rows="20", cols="14")


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(os.environ["EQUITIES_SHEET_ID"])
    ws = get_or_create_worksheet(spreadsheet, TAB_NAME)

    header = ["Index", "Current", "All-Time High", "Bear Threshold (-20%)",
              "% From High", "Regime", "WoW %", "MoM %", "QoQ %", "YoY %",
              "As Of", "Source"]
    rows = []
    errors = []
    for symbol, label in INDICES:
        try:
            rows.append(compute_regime_row(symbol, label))
        except Exception as e:
            print(str(e))
            errors.append(f"{label}: {e}")
            rows.append([label, "N/A - Not Verified"] + ["N/A"] * 9 + [f"ERROR: {e}"])

    ws.clear()
    ws.update("A1", [header] + rows, raw=False)
    print(f"[OK] Wrote {len(rows)} index rows to '{TAB_NAME}'")

    if errors:
        print(f"[FAIL] {len(errors)} of {len(INDICES)} indices failed - check API response shape.")


if __name__ == "__main__":
    main()

    
