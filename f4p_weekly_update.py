#!/usr/bin/env python3
"""
F4P Weekly Hub Data Automation
================================
Calls Claude (with web search) to gather all 8 currencies x 15 indicators,
then writes the result directly into the Google Sheet "AI HUB DATA" tab.

Runs every Sunday via GitHub Actions. See .github/workflows/f4p_weekly_update.yml

Required environment variables / GitHub Secrets:
  ANTHROPIC_API_KEY   - from console.anthropic.com
  GOOGLE_CREDENTIALS  - JSON string of your Google service account credentials
                        (same secret already used by your FRED AUTO script)
  GOOGLE_SHEET_ID     - your F4P Google Sheet ID (already in use for FRED AUTO)
"""

import os
import json
import sys
from datetime import datetime, timezone

import anthropic
import gspread
from google.oauth2.service_account import Credentials


CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"]

INDICATORS = [
    "Manufacturing PMI",
    "Services PMI",
    "Consumer Sentiment",
    "Building Permits / Housing",
    "M2 Money Supply",
    "Central Bank Rate & Current Policy Stance",
    "CPI YoY",
    "Core CPI",
    "PPI",
    "Core PPI",
    "Employment",
    "Government Debt / GDP",
    "Fiscal Balance",
    "10-Year Government Bond Yield",
    "Central Bank Balance Sheet / GDP",
]

SHEET_TAB_NAME = "AI HUB DATA"
RANKINGS_TAB_NAME = "AI RANKINGS"

HEADER_ROW = [
    "Currency", "Indicator #", "Indicator", "Current Value", "Prior Value",
    "Forecast / Consensus", "Surprise", "Release Date", "Tag",
    "F4P Score", "Institutional Analysis", "Source / Audit Link",
]

RANKINGS_HEADER = ["Rank", "Currency", "Total Score", "Bias"]


def build_prompt(today: str) -> str:
    indicator_list = "\n".join(f"  {i+1}. {name}" for i, name in enumerate(INDICATORS))
    currency_list = ", ".join(CURRENCIES)
    return f"""You are a professional macro FX research analyst producing the Fishin4Pips (F4P) institutional Hub Data pack for the week of {today}.

Use web search. Use only official primary sources: central bank websites, national statistics offices, ismworld.org, FRED (fred.stlouisfed.org), OECD, IMF. Never fabricate a number. If a number is genuinely unavailable, write "data unavailable" and score it 0.

Produce the LATEST RELEASED data (as of {today}) for ALL 8 currencies: {currency_list}.

For EACH currency, give all 15 indicators in this exact order:
{indicator_list}

SCORING RULE - use this exact scale, whole numbers only:
+2 = Strongly supportive for the currency   |   +1 = Mildly supportive
 0 = Neutral / no clear signal              |   -1 = Mildly negative
-2 = Strongly negative for the currency

Institutional Analysis = one sentence. State the reading, compare to expectation or prior, and say what it means for the currency. Write like an institutional desk note.

Return your ENTIRE response as a single valid JSON object and NOTHING else - no preamble, no markdown fences, no commentary. Use this exact schema:

{{
  "data_cutoff": "Week of {today}",
  "rows": [
    {{
      "currency": "USD",
      "indicator_num": 1,
      "indicator": "Manufacturing PMI",
      "current_value": "54.0",
      "prior_value": "52.7",
      "forecast": "53.0",
      "surprise": "Beat",
      "release_date": "May 2026",
      "tag": "Expansion/Contraction",
      "score": 2,
      "analysis": "One sentence institutional analysis.",
      "source_url": "https://..."
    }}
  ],
  "rankings": [
    {{"rank": 1, "currency": "USD", "total_score": 12, "bias": "Mild Bullish"}}
  ],
  "data_unavailable_flags": ["List any indicator/currency combos you could not find data for, or empty list"]
}}

The rows array must contain exactly {len(CURRENCIES) * len(INDICATORS)} entries (8 currencies x 15 indicators each).
The rankings array must contain exactly 8 entries, sorted strongest to weakest (rank 1 = highest total_score).
Bias labels: above +15 Strong Bullish, +6 to +15 Mild Bullish, -5 to +5 Neutral, -15 to -6 Mild Bearish, below -15 Strong Bearish.
"""


def call_claude(prompt: str) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )

    # Concatenate all text blocks (web search may interleave tool_use/tool_result blocks)
    full_text = ""
    for block in message.content:
        if block.type == "text":
            full_text += block.text

    full_text = full_text.strip()
    # Defensive cleanup in case the model wraps in a code fence despite instructions
    if full_text.startswith("```"):
        full_text = full_text.split("```")[1]
        if full_text.startswith("json"):
            full_text = full_text[4:]
        full_text = full_text.rsplit("```", 1)[0]

    return json.loads(full_text)


def connect_sheet():
    creds_json = os.environ["GOOGLE_CREDENTIALS"]
    creds_dict = json.loads(creds_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    return gc.open_by_key(sheet_id)


def get_or_create_tab(spreadsheet, tab_name: str, rows: int, cols: int):
    try:
        ws = spreadsheet.worksheet(tab_name)
        ws.clear()
        return ws
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=tab_name, rows=str(rows), cols=str(cols))


def write_hub_data(spreadsheet, data: dict, today: str):
    ws = get_or_create_tab(spreadsheet, SHEET_TAB_NAME, rows=200, cols=12)

    values = [HEADER_ROW]
    for row in data["rows"]:
        values.append([
            row["currency"],
            row["indicator_num"],
            row["indicator"],
            row["current_value"],
            row["prior_value"],
            row.get("forecast", "N/A"),
            row.get("surprise", "N/A"),
            row["release_date"],
            row.get("tag", ""),
            row["score"],
            row["analysis"],
            row.get("source_url", ""),
        ])

    ws.update(values, "A1")
    ws.format("A1:L1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.05, "green": 0.1, "blue": 0.16}})

    # Stamp the cutoff date in a clearly visible cell
    ws.update([[f"Last auto-updated: {today} | {data.get('data_cutoff', '')}"]], "N1")

    flags = data.get("data_unavailable_flags", [])
    if flags:
        ws.update([["DATA UNAVAILABLE - VERIFY MANUALLY:"], *[[f] for f in flags]], "N3")


def write_rankings(spreadsheet, data: dict):
    ws = get_or_create_tab(spreadsheet, RANKINGS_TAB_NAME, rows=20, cols=4)

    values = [RANKINGS_HEADER]
    for r in data["rankings"]:
        values.append([r["rank"], r["currency"], r["total_score"], r["bias"]])

    ws.update(values, "A1")
    ws.format("A1:D1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.05, "green": 0.1, "blue": 0.16}})


def main():
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    print(f"[F4P Weekly Update] Starting run for {today}")

    prompt = build_prompt(today)

    print("[F4P Weekly Update] Calling Claude with web search...")
    data = call_claude(prompt)

    expected_rows = len(CURRENCIES) * len(INDICATORS)
    actual_rows = len(data.get("rows", []))
    if actual_rows != expected_rows:
        print(f"[WARNING] Expected {expected_rows} rows, got {actual_rows}. Proceeding anyway.")

    print("[F4P Weekly Update] Connecting to Google Sheet...")
    spreadsheet = connect_sheet()

    print(f"[F4P Weekly Update] Writing {actual_rows} rows to '{SHEET_TAB_NAME}' tab...")
    write_hub_data(spreadsheet, data, today)

    print(f"[F4P Weekly Update] Writing rankings to '{RANKINGS_TAB_NAME}' tab...")
    write_rankings(spreadsheet, data)

    flags = data.get("data_unavailable_flags", [])
    if flags:
        print(f"[F4P Weekly Update] {len(flags)} indicator(s) flagged as unavailable - check sheet.")

    print("[F4P Weekly Update] Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[F4P Weekly Update] FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
