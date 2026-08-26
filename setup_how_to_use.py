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
     "Saturday: automated data pull runs via GitHub Actions "
     "(F4P Equities Weekly Update). Sunday: team reviews STRATEGY "
     "DASHBOARD together, same as the FX Hub's Sunday scoring session. "
     "Monday-Friday: execution."],

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
     "INCOMPLETE (n/12) = a data row is missing somewhere - don't trust "
     "the score until that's fixed, the same discipline that caught "
     "three real bugs during setup."],

    ["What's live vs. what's a placeholder",
     "12 of 18 planned indicators are live and field-verified against "
     "real filings as of 2026-08-25. Price Momentum Pulse is explicitly "
     "a placeholder for full technical analysis (just daily % change), "
     "flagged with a 'Technical-Placeholder' tag - don't weight it as "
     "heavily as the other Confirmation-layer indicators."],

    ["Known limitations",
     "IV Rank isn't built yet - it needs weekly accumulated history "
     "before a real percentile means anything, not a one-time pull. "
     "Forward guidance and catalyst pipeline (qualitative reads on "
     "earnings calls and news) aren't automated - they need a human or "
     "a separate Claude-assisted step, not a numeric threshold. Macro "
     "overlay isn't connected yet - check the FX Hub's CENTRAL BANK and "
     "EXOGENOUS tabs directly for now."],

    ["If something looks wrong",
     "Check the Source/Audit Link column in EQUITIES HUB DATA first - "
     "every score traces back to a specific Alpha Vantage endpoint. If "
     "a number still looks off, trace it by hand against the source "
     "before trusting the score - that's exactly how the margin-trend "
     "mislabel and the insider-transactions date bug got caught."],
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

    
