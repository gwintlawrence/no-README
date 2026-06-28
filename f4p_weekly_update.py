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


def extract_json_object(text: str) -> str:
    """
    Claude sometimes narrates its reasoning ('Now let me compile the JSON...')
    before or after the actual JSON object, even when explicitly told not to.
    This finds the first '{' and walks forward counting brace depth (respecting
    strings, so braces inside quoted text don't throw off the count) to find
    the matching closing '}' - returning just that substring.
    """
    start = text.find("{")
    if start == -1:
        return text  # nothing brace-like at all; let json.loads raise its own error

    depth = 0
    in_string = False
    escape_next = False

    for i in range(start, len(text)):
        ch = text[i]

        if escape_next:
            escape_next = False
            continue

        if ch == "\\" and in_string:
            escape_next = True
            continue

        if ch == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    # Unbalanced - likely truncated mid-object. Return from start anyway so the
    # caller's error log shows something useful for debugging.
    return text[start:]


def build_prompt(today: str, currency_batch: list) -> str:
    indicator_list = "\n".join(f"  {i+1}. {name}" for i, name in enumerate(INDICATORS))
    currency_list = ", ".join(currency_batch)
    return f"""You are a professional macro FX research analyst producing the Fishin4Pips (F4P) institutional Hub Data pack for the week of {today}.

Use web search. Use only official primary sources: central bank websites, national statistics offices, ismworld.org, FRED (fred.stlouisfed.org), OECD, IMF. Never fabricate a number. If a number is genuinely unavailable, write "data unavailable" and score it 0.

Produce the LATEST RELEASED data (as of {today}) for these currencies ONLY: {currency_list}.

For EACH currency, give all 15 indicators in this exact order:
{indicator_list}

SCORING RULE - use this exact scale, whole numbers only:
+2 = Strongly supportive for the currency   |   +1 = Mildly supportive
 0 = Neutral / no clear signal              |   -1 = Mildly negative
-2 = Strongly negative for the currency

Institutional Analysis = one sentence. State the reading, compare to expectation or prior, and say what it means for the currency. Write like an institutional desk note.

Return your ENTIRE response as a single valid JSON object and NOTHING else. Do NOT write any introductory sentence like "Now I have all the data" or "Let me compile the response" - your response must START with the character {{ and END with }}, with no other text anywhere before or after it. Use this exact schema:

{{
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
  "data_unavailable_flags": ["List any indicator/currency combos you could not find data for, or empty list"]
}}

The rows array must contain exactly {len(currency_batch) * len(INDICATORS)} entries ({len(currency_batch)} currencies x 15 indicators each). Do not include currencies other than the ones listed above. Do not include a rankings array - that will be computed separately.
"""


def call_claude(prompt: str) -> dict:
    """
    Calls Claude with the server-side web_search tool. For large research tasks,
    Claude may return stop_reason='pause_turn' partway through - this means it is
    still actively researching and needs another turn to continue, NOT that it
    failed. We must continue the SAME conversation (resend its own content back)
    until it reaches a real stop (end_turn) with a final text answer.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    messages = [{"role": "user", "content": prompt}]
    max_continuations = 6  # safety cap - each continuation lets it keep researching

    for attempt in range(max_continuations):
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=16000,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 40}],
            messages=messages,
        )

        print(f"[F4P Weekly Update] Attempt {attempt+1}: stop_reason={message.stop_reason}, "
              f"blocks={len(message.content)}")

        if message.stop_reason == "max_tokens":
            raise RuntimeError(
                "Claude hit the max_tokens limit before writing a final answer. "
                "Split the request into smaller batches (fewer currencies per call)."
            )

        if message.stop_reason == "pause_turn":
            # Still researching - feed its own turn back in verbatim and let it continue.
            print("[F4P Weekly Update] pause_turn received - continuing same turn...")
            messages.append({"role": "assistant", "content": message.content})
            continue

        # Any other stop_reason (end_turn, stop_sequence, etc.) means it's done.
        full_text = "".join(b.text for b in message.content if b.type == "text").strip()

        if not full_text:
            print("[F4P Weekly Update] No text block found. Block types were: "
                  f"{[b.type for b in message.content]}")
            raise RuntimeError(
                f"Claude stopped (stop_reason={message.stop_reason}) with no final "
                f"text block to parse."
            )

        if full_text.startswith("```"):
            full_text = full_text.split("```", 1)[1]
            if full_text.startswith("json"):
                full_text = full_text[4:]
            full_text = full_text.rsplit("```", 1)[0].strip()

        json_candidate = extract_json_object(full_text)

        try:
            return json.loads(json_candidate)
        except json.JSONDecodeError:
            print("[F4P Weekly Update] JSON parse failed. First 800 chars of extracted candidate:")
            print(json_candidate[:800])
            print("[F4P Weekly Update] Last 800 chars of extracted candidate:")
            print(json_candidate[-800:])
            raise

    raise RuntimeError(
        f"Hit max_continuations ({max_continuations}) without reaching a final answer. "
        f"This batch may be too large even when split - consider 1 currency per call."
    )


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


def compute_rankings(all_rows: list) -> list:
    """Sum scores per currency locally in Python - far more reliable than
    asking the model to do arithmetic across a huge response."""
    totals = {}
    for row in all_rows:
        ccy = row["currency"]
        totals[ccy] = totals.get(ccy, 0) + int(row.get("score", 0))

    def bias_label(score):
        if score > 15:
            return "Strong Bullish"
        if score >= 6:
            return "Mild Bullish"
        if score >= -5:
            return "Neutral"
        if score >= -15:
            return "Mild Bearish"
        return "Strong Bearish"

    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    return [
        {"rank": i + 1, "currency": ccy, "total_score": score, "bias": bias_label(score)}
        for i, (ccy, score) in enumerate(ranked)
    ]


def write_rankings(spreadsheet, rankings: list):
    ws = get_or_create_tab(spreadsheet, RANKINGS_TAB_NAME, rows=20, cols=4)

    values = [RANKINGS_HEADER]
    for r in rankings:
        values.append([r["rank"], r["currency"], r["total_score"], r["bias"]])

    ws.update(values, "A1")
    ws.format("A1:D1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.05, "green": 0.1, "blue": 0.16}})


def chunk(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def main():
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    print(f"[F4P Weekly Update] Starting run for {today}")

    all_rows = []
    all_flags = []
    batch_size = 1  # 1 currency per call (15 indicators) - most reliable, avoids pause_turn loops

    for batch_num, batch in enumerate(chunk(CURRENCIES, batch_size), start=1):
        print(f"[F4P Weekly Update] Batch {batch_num}: {batch}")
        prompt = build_prompt(today, batch)

        try:
            data = call_claude(prompt)
        except Exception as exc:
            print(f"[F4P Weekly Update] Batch {batch_num} ({batch}) FAILED: {exc}")
            print("[F4P Weekly Update] Continuing with remaining batches...")
            all_flags.append(f"ENTIRE BATCH FAILED: {batch} - {exc}")
            continue

        batch_rows = data.get("rows", [])
        expected = len(batch) * len(INDICATORS)
        if len(batch_rows) != expected:
            print(f"[WARNING] Batch {batch_num}: expected {expected} rows, got {len(batch_rows)}.")

        all_rows.extend(batch_rows)
        all_flags.extend(data.get("data_unavailable_flags", []))

    if not all_rows:
        raise RuntimeError("All batches failed - no data collected at all. Aborting before writing to sheet.")

    print(f"[F4P Weekly Update] Collected {len(all_rows)} total rows across all batches.")

    rankings = compute_rankings(all_rows)
    full_data = {"data_cutoff": f"Week of {today}", "rows": all_rows, "data_unavailable_flags": all_flags}

    print("[F4P Weekly Update] Connecting to Google Sheet...")
    spreadsheet = connect_sheet()

    print(f"[F4P Weekly Update] Writing {len(all_rows)} rows to '{SHEET_TAB_NAME}' tab...")
    write_hub_data(spreadsheet, full_data, today)

    print(f"[F4P Weekly Update] Writing rankings to '{RANKINGS_TAB_NAME}' tab...")
    write_rankings(spreadsheet, rankings)

    if all_flags:
        print(f"[F4P Weekly Update] {len(all_flags)} item(s) flagged as unavailable/failed - check sheet.")

    expected_total = len(CURRENCIES) * len(INDICATORS)
    if len(all_rows) < expected_total:
        print(f"[F4P Weekly Update] WARNING: only {len(all_rows)}/{expected_total} rows collected. "
              f"Glenise should verify missing currencies manually this week.")

    print("[F4P Weekly Update] Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[F4P Weekly Update] FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
