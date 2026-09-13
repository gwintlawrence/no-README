"""
setup_equity_regime.py  (v2 - switched off INDEX_DATA)

Phase 2 (Equity Market Regime block) for the F4P Equities & Options Hub.

v1 used Alpha Vantage's INDEX_DATA for SPX/NDX directly - that endpoint
returned "You are not yet entitled to index data access" against the
real key, confirmed 2026-09-12. Rather than ask for another premium
tier for two data points, this uses TIME_SERIES_DAILY_ADJUSTED instead -
the exact function your own f4p_equities_weekly_update.py already calls
successfully for Relative Strength - on SPY and QQQ as ETF proxies for
the S&P 500 and Nasdaq 100.

This is a different kind of substitution than VXX-for-VIX, and a safe
one: SPY/QQQ are physically-backed funds that track their index's
*level* closely (a few bps of expense-ratio drag over years, nowhere
near the precision a 20%-threshold check needs). VXX's problem was
structural decay from futures-roll contango, which distorts the level
itself - that doesn't apply here.

Uses "close" (not "adjusted_close") deliberately: adjusted_close
retroactively lowers historical prices for dividends, which would bias
the historical-high comparison this regime check depends on. That
adjustment is correct for return calculations (which is what the
existing pipeline uses it for) but wrong for a price-level threshold
check like this one.

Still implements the exact Anton/ITPM Bull_Bear_Markets methodology
verified from the workbook: Bear = current price 20%+ below its rolling
all-time high; Bull = back above that same threshold.

Required secrets:
  ALPHA_VANTAGE_API_KEY, GOOGLE_CREDENTIALS, EQUITIES_SHEET_ID
  (all already in use elsewhere in this pipeline)
"""

import os
import io
import csv
import json
import time
import datetime
import gspread
import requests
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

AV_BASE = "https://www.alphavantage.co/query"
TAB_NAME = "EQUITY MARKET REGIME"
BEAR_THRESHOLD_PCT = 0.20  # 20% fall from high, per the Anton/ITPM workbook

INDICES = [
    ("SPY", "S&P 500 (via SPY)"),
    ("QQQ", "Nasdaq 100 (via QQQ)"),
]

LOOKBACKS = [("WoW", 7), ("MoM", 30), ("QoQ", 91), ("YoY", 365)]


def av_request_csv_all_rows(params, api_key, retries=3):
    """Same retry-on-rate-limit-note pattern already proven in
    f4p_equities_weekly_update.py's av_request() - Alpha Vantage
    sometimes returns a JSON note even when CSV was requested, and
    that's a rate limit signal to retry, not an empty result."""
    query = {**params, "apikey": api_key}
    for attempt in range(retries):
        resp = requests.get(AV_BASE, params=query, timeout=30)
        resp.raise_for_status()
        stripped = resp.text.lstrip()
        if stripped.startswith("{"):
            try:
                data = json.loads(resp.text)
            except json.JSONDecodeError:
                data = {}
            if "Note" in data or "Information" in data:
                print(f"[RATE LIMIT] {params.get('function')} - {data}. Retrying in 15s...")
                time.sleep(15)
                continue
            raise RuntimeError(f"Unexpected JSON response for CSV-format request: {data}")
        reader = csv.DictReader(io.StringIO(resp.text))
        return list(reader)
    raise RuntimeError(f"Alpha Vantage request failed after {retries} retries: {params}")


def fetch_full_daily_series(symbol, api_key):
    rows = av_request_csv_all_rows(
        {"function": "TIME_SERIES_DAILY_ADJUSTED", "symbol": symbol,
         "outputsize": "full", "datatype": "csv"},
        api_key,
    )
    parsed = [
        (datetime.date.fromisoformat(r["timestamp"]), float(r["close"]))
        for r in rows if r.get("timestamp") and r.get("close")
    ]
    parsed.sort(key=lambda pair: pair[0], reverse=True)
    return parsed


def nearest_value(series, target_date, tolerance_days=5):
    best = min(series, key=lambda pair: abs((pair[0] - target_date).days))
    if abs((best[0] - target_date).days) > tolerance_days:
        return None
    return best[1]


def compute_regime_row(symbol, label, api_key):
    series = fetch_full_daily_series(symbol, api_key)
    if not series:
        return [label, "N/A - Not Verified", "N/A", "N/A", "N/A", "N/A", "N/A",
                "N/A", "N/A", "N/A", "N/A", f"Alpha Vantage: TIME_SERIES_DAILY_ADJUSTED ({symbol}) - no data returned"]

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
    row.append(f"Alpha Vantage: TIME_SERIES_DAILY_ADJUSTED ({symbol}, close price, full history)")
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
    api_key = os.environ["ALPHA_VANTAGE_API_KEY"]

    header = ["Index (ETF proxy)", "Current", "All-Time High", "Bear Threshold (-20%)",
              "% From High", "Regime", "WoW %", "MoM %", "QoQ %", "YoY %",
              "As Of", "Source"]
    rows = []
    errors = []
    for symbol, label in INDICES:
        try:
            rows.append(compute_regime_row(symbol, label, api_key))
        except Exception as e:
            print(str(e))
            errors.append(f"{label}: {e}")
            rows.append([label, "N/A - Not Verified"] + ["N/A"] * 9 + [f"ERROR: {e}"])

    ws.clear()
    ws.update("A1", [header] + rows, raw=False)
    print(f"[OK] Wrote {len(rows)} index rows to '{TAB_NAME}'")

    if errors:
        print(f"[FAIL] {len(errors)} of {len(INDICES)} indices failed - check log above.")


if __name__ == "__main__":
    main()

    
