"""
setup_how_to_use.py

One-time (idempotent) setup script writing static instructional content
into the HOW TO USE tab. No API calls - pure reference text, mirroring
the FX Hub's HOW TO USE tab structure.

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

CONTENT = [
    ["Weekly Workflow",
     "Saturday 10:00 UTC: F4P Equities Master Weekly Update runs "
     "automatically (Alpha Vantage data, Claude research, ranking "
     "refresh, in that order, one workflow). Can also be triggered "
     "manually anytime for a fresh read before a decision. Sunday: team "
     "reviews STRATEGY DASHBOARD together, same as the FX Hub's Sunday "
     "scoring session. Monday-Friday: execution."],

    ["Cardinal Rule (equities version)",
     "Fundamentals = Direction (Endogenous score in EQUITIES HUB DATA), "
     "Technicals = Timing (Momentum + Relative Strength), "
     "Options/Institutional Flow = Confirmation (Put/Call Ratio, "
     "Institutional Holdings, Insider Activity). No trade without all "
     "three aligned - same discipline as the FX side."],

    ["Where to look first",
     "STRATEGY DASHBOARD is the team-facing summary - Catalyst, "
     "Technical Setup, suggested Options Strategy, and Status all in "
     "one row per ticker. EQUITY RANKINGS shows the raw Total Score, "
     "Bias, and Status/Signal behind that. EQUITIES HUB DATA has every "
     "individual indicator with its source and audit link."],

    ["Reading Status",
     "Go = score confirms a directional bias strongly enough to act. "
     "Wait = mixed or insufficient confirmation - this is the most "
     "common state and is not a failure, it means the market hasn't "
     "lined up yet. Stop = confirms the opposite direction strongly. "
     "INCOMPLETE (n/17) = a data row is missing somewhere - don't trust "
     "the score until that's fixed. This guard has already caught real "
     "gaps more than once - trust it over a green checkmark."],

    ["What's live vs. what's a placeholder",
     "17 of 18 planned indicators are live: 15 field-verified against "
     "Alpha Vantage data, plus Forward Guidance and Catalyst Pipeline "
     "via Claude web research. Price Momentum Pulse is explicitly a "
     "placeholder for full technical analysis (just daily % change), "
     "flagged with a 'Technical-Placeholder' tag - don't weight it as "
     "heavily as the other Confirmation-layer indicators. Only IV Rank "
     "remains - it's accumulating weekly ATM IV snapshots in OPTIONS "
     "FLOW & IV, and needs roughly a year of history before a real "
     "percentile means anything. That's a time constraint, not a "
     "build constraint."],

    ["Known limitations",
     "Forward Guidance and Catalyst Pipeline are live research, not a "
     "fixed API response - the same ticker can read slightly "
     "differently between runs since Claude is re-researching current "
     "news each time, not pulling a stored number. Give those two "
     "indicators a bit more skepticism than the numeric ones. Macro "
     "overlay is connected live to the FX Hub's FRED AUTO and CENTRAL "
     "BANK tabs - check SECTOR & MACRO OVERLAY directly rather than "
     "cross-referencing the FX Hub by hand. The Anthropic API used for "
     "qualitative research bills every weekly run regardless of need - "
     "worth an occasional glance at console.anthropic.com billing so a "
     "low balance doesn't silently break that layer."],

    ["If something looks wrong",
     "Check the Source/Audit Link column in EQUITIES HUB DATA first - "
     "every score traces back to a specific Alpha Vantage or Claude "
     "source. If a number still looks off, trace it by hand before "
     "trusting the score. Also check the GitHub Actions run log for "
     "lines starting with [FAIL] or [SUSPICIOUS EMPTY] - those flag "
     "exactly what failed and why, rather than failing silently. That "
     "discipline has already caught real bugs: a margin-trend mislabel, "
     "an insider-transactions date bug, and a duplicate-row bug in the "
     "IV snapshot log."],
]


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(os.environ["EQUITIES_SHEET_ID"])
    ws = spreadsheet.worksheet("HOW TO USE")

    existing = ws.get_all_values()
    if len(existing) > 1:
        ws.batch_clear([f"A2:B{len(existing)}"])

    ws.update("A2", CONTENT, raw=False)
    ws.format("A2:A", {"textFormat": {"bold": True}})
    ws.format("B2:B", {"wrapStrategy": "WRAP"})
    ws.columns_auto_resize(0, 2)
    print(f"[OK] Wrote {len(CONTENT)} sections to HOW TO USE")


if __name__ == "__main__":
    main()

    
