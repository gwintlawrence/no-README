"""
setup_strategy_dashboard.py

One-time (idempotent) setup script that writes LIVE formulas into the
STRATEGY DASHBOARD tab - the team-facing output matching the "Stocks to
Watch" infographic format: Ticker | Catalyst | Technical Setup |
Options Strategy Idea | Score | Bias | Status/Signal | Expiration Window.

Pulls from three tabs, all via formula (nothing pre-computed in Python):
  - EQUITY RANKINGS      - Total Score, Bias, Status, Signal
  - EARNINGS CALENDAR    - next earnings date, timing, days to event
  - EQUITIES HUB DATA    - Price Momentum + Relative Strength notes,
                           pulled via QUERY (ticker + indicator# match)

Re-running this script is safe; it rewrites the same formulas rather
than duplicating rows. IMPORTANT: formula correctness here can only be
confirmed by looking at the live Sheet after running - unlike the
Python fetch logic, these aren't something that can be unit-tested
locally before deployment. Check a couple of tickers by hand against
what EQUITY RANKINGS and EARNINGS CALENDAR already show before trusting
this tab.

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

WATCHLIST = ["NVDA", "AAPL", "AMZN", "GOOGL", "TSLA", "META", "COIN", "NFLX", "QQQ"]

HUB_TAB = "EQUITIES HUB DATA"
RANKINGS_TAB = "EQUITY RANKINGS"
CALENDAR_TAB = "EARNINGS CALENDAR"


def get_client():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def build_row_formulas(row_num):
    """row_num is the 1-indexed Sheets row (2 for the first ticker)."""
    t = f"A{row_num}"

    catalyst = (
        f'=IFERROR("Earnings "&VLOOKUP({t},\'{CALENDAR_TAB}\'!A:F,2,FALSE)'
        f'&" - "&VLOOKUP({t},\'{CALENDAR_TAB}\'!A:F,6,FALSE)&"d away, est $"'
        f'&VLOOKUP({t},\'{CALENDAR_TAB}\'!A:F,3,FALSE)&" EPS","No near-term earnings data")'
    )

    technical_setup = (
        f'="Momentum: "&IFERROR(QUERY(\'{HUB_TAB}\'!A:K,'
        f'"select K where A=\'"&{t}&"\' and B=18",0),"N/A")'
        f'&" | Rel.Str vs SPY: "&IFERROR(QUERY(\'{HUB_TAB}\'!A:K,'
        f'"select K where A=\'"&{t}&"\' and B=11",0),"N/A")'
    )

    status_ref = f"VLOOKUP({t},'{RANKINGS_TAB}'!A:F,4,FALSE)"
    options_strategy = (
        f'=IF(LEFT({status_ref},10)="INCOMPLETE","Data incomplete - wait",'
        f'IF({status_ref}="Go","Buy Calls (bullish confirmation)",'
        f'IF({status_ref}="Stop","Buy Puts (bearish confirmation)",'
        f'"Wait - insufficient confirmation")))'
    )

    total_score = f"=VLOOKUP({t},'{RANKINGS_TAB}'!A:F,2,FALSE)"
    bias = f"=VLOOKUP({t},'{RANKINGS_TAB}'!A:F,3,FALSE)"
    status_signal = (
        f"=VLOOKUP({t},'{RANKINGS_TAB}'!A:F,4,FALSE)&\" / \"&"
        f"VLOOKUP({t},'{RANKINGS_TAB}'!A:F,5,FALSE)"
    )

    days_ref = f"VLOOKUP({t},'{CALENDAR_TAB}'!A:F,6,FALSE)"
    expiration_window = (
        f'=IFERROR(IF({days_ref}<=7,'
        f'"Post-earnings: "&({days_ref}+14)&"-"&({days_ref}+28)&"d out",'
        f'"2-4 wks out (no near-term earnings)"),"2-4 wks out")'
    )

    return [catalyst, technical_setup, options_strategy, total_score, bias,
            status_signal, expiration_window]


def main():
    client = get_client()
    spreadsheet = client.open_by_key(os.environ["EQUITIES_SHEET_ID"])
    ws = spreadsheet.worksheet("STRATEGY DASHBOARD")

    existing = ws.get_all_values()
    if len(existing) > 1:
        ws.batch_clear([f"A2:H{len(existing)}"])

    rows = []
    for i, ticker in enumerate(WATCHLIST):
        row_num = i + 2
        formulas = build_row_formulas(row_num)
        rows.append([ticker] + formulas)

    ws.update("A2", rows, raw=False)
    print(f"[OK] Wrote {len(rows)} ticker rows with live formulas to STRATEGY DASHBOARD")
    print("[NOTE] Verify by hand: open the Sheet and check that Catalyst, Options "
          "Strategy, and Total Score for a couple of tickers match what EQUITY "
          "RANKINGS and EARNINGS CALENDAR already show independently.")


if __name__ == "__main__":
    main()

    
