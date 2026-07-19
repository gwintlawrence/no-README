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

CARRY_PAIRS = [
    ("JPY", "USD"), ("JPY", "CAD"), ("JPY", "AUD"),
    ("CHF", "USD"), ("CHF", "CAD"), ("CHF", "AUD"),
    ("EUR", "USD"), ("EUR", "AUD"),
    ("USD", "JPY"),
]

CENTRAL_BANKS = ["FED", "ECB", "BOE", "BOJ", "SNB", "RBA", "RBNZ", "BOC"]

EXOGENOUS_PAIRS = ["GBPUSD", "EURUSD", "AUDUSD", "USDJPY", "USDCAD", "NZDUSD", "USDCHF"]

CARRY_TAB_NAME = "CARRY TRADE"
CARRY_HEADER_ROW = [
    "Funding", "Target", "Real Rate Differential", "Carry Score",
    "Funding Pressure", "Capital Flow", "Source"
]

CENTRAL_BANK_TAB_NAME = "CENTRAL BANK"
CENTRAL_BANK_HEADER_ROW = [
    "Central Bank", "Bias", "Score", "Latest Meeting",
    "Next Meeting", "Forward Guidance Note", "Source"
]

EXOGENOUS_TAB_NAME = "EXOGENOUS"
EXOGENOUS_HEADER_ROW = [
    "Pair", "Base GDP %", "Quote GDP %", "Base Current Account % GDP",
    "Quote Current Account % GDP", "Base Rate + Direction", "Quote Rate + Direction",
    "Base Index Level", "Base Index 12mo High", "Total Score", "Bias", "Source"
]


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
def build_carry_prompt(today: str, pairs: list) -> str:
    pair_list = "\n".join(
        f"  {i+1}. Funding currency={f}, Target currency={t}" for i, (f, t) in enumerate(pairs)
    )
    return f"""You are a professional macro FX research analyst producing the Fishin4Pips (F4P) Carry Trade Data pack for the week of {today}.

Use web search. Use only official primary sources: central bank websites, national statistics offices, FRED (fred.stlouisfed.org), OECD, IMF. Never fabricate a number.

For EACH pair below, use the LATEST RELEASED policy rate and CPI YoY figures (as of {today}) to compute the real rate differential:
  Real Rate Differential = (Target policy rate - Target CPI YoY) - (Funding policy rate - Funding CPI YoY)

Pairs to compute:
{pair_list}

For each pair also provide:
- carry_score: 0-10, where 10 = maximum attractive carry (wide positive differential, low funding pressure, strong capital flow into target)
- funding_pressure: "Low", "Medium", or "High" (risk of funding currency squeeze / unwind risk)
- capital_flow: "↑" (flowing into target), "↓" (flowing out), or "→" (flat/unclear)

Return your ENTIRE response as a single valid JSON object and NOTHING else. Do NOT write any introductory sentence - your response must START with {{ and END with }}.

{{
  "rows": [
    {{
      "funding": "JPY",
      "target": "USD",
      "real_rate_differential": "+4.25%",
      "carry_score": 9,
      "funding_pressure": "Low",
      "capital_flow": "↑",
      "source_url": "https://..."
    }}
  ],
  "data_unavailable_flags": ["List any pairs you could not find data for, or empty list"]
}}

The rows array must contain exactly {len(pairs)} entries, one per pair listed above."""


def build_central_bank_prompt(today: str, banks: list) -> str:
    bank_list = ", ".join(banks)
    return f"""You are a professional macro FX research analyst producing the Fishin4Pips (F4P) Central Bank Data pack for the week of {today}.

Use web search. Use only official primary sources: central bank websites and official statements. Never fabricate a number or quote.

Produce the LATEST data (as of {today}) for these central banks ONLY: {bank_list}.

For EACH central bank, provide:
- bias: "Hawkish", "Neutral", or "Dovish"
- score: 0-10 conviction (10 = maximum hawkish conviction, 0 = maximum dovish conviction, 5 = neutral)
- latest_meeting: date of most recent policy meeting
- next_meeting: date of next scheduled policy meeting
- forward_guidance_note: ONE sentence summarizing the most recent forward guidance or statement language

Return your ENTIRE response as a single valid JSON object and NOTHING else. Do NOT write any introductory sentence - your response must START with {{ and END with }}.

{{
  "rows": [
    {{
      "central_bank": "FED",
      "bias": "Hawkish",
      "score": 8,
      "latest_meeting": "June 17, 2026",
      "next_meeting": "July 29, 2026",
      "forward_guidance_note": "One sentence institutional note.",
      "source_url": "https://..."
    }}
  ],
  "data_unavailable_flags": ["List any banks you could not find data for, or empty list"]
}}

The rows array must contain exactly {len(banks)} entries, one per central bank listed above."""


def build_exogenous_prompt(today: str, pairs: list) -> str:
    pair_list = ", ".join(pairs)
    return f"""You are a professional macro FX research analyst producing the Fishin4Pips (F4P) Exogenous Drivers Data pack for the week of {today}, matching the structure of the F4P Exogenous Drivers tool.

Use web search. Use only official primary sources: national statistics offices, central banks, stock exchange data, FRED, OECD, IMF. Never fabricate a number.

Produce the LATEST data (as of {today}) for these pairs ONLY: {pair_list}.

For EACH pair, score 4 structural drivers on a flat +/-2 scale each (total range -8 to +8):
1. Relative GDP Growth - base country GDP% vs quote country GDP%
2. Balance of Payments - base current account % of GDP vs quote current account % of GDP
3. Interest Rate Differentials & Carry - base policy rate + direction (Hiking/Hold-Hawkish/Hold-Neutral/Hold-Dovish/Cutting) vs quote
4. Stock Market Returns / Relative Wealth - base country's major index level vs its 12-month high

For each pair provide the raw inputs plus:
- total_score: sum of the 4 driver scores (-8 to +8)
- bias: "Structurally Bullish" (base currency), "Mixed-Neutral", or "Structurally Bearish" (base currency)

Return your ENTIRE response as a single valid JSON object and NOTHING else. Do NOT write any introductory sentence - your response must START with {{ and END with }}.

{{
  "rows": [
    {{
      "pair": "GBPUSD",
      "base_gdp": "1.2%",
      "quote_gdp": "2.1%",
      "base_current_account": "-2.5%",
      "quote_current_account": "-3.1%",
      "base_rate_direction": "3.75% (Hold-Hawkish)",
      "quote_rate_direction": "3.625% (Hold-Hawkish)",
      "base_index_level": "8,150",
      "base_index_12mo_high": "8,400",
      "total_score": -2,
      "bias": "Mixed-Neutral",
      "source_url": "https://..."
    }}
  ],
  "data_unavailable_flags": ["List any pairs you could not find data for, or empty list"]
}}

The rows array must contain exactly {len(pairs)} entries, one per pair listed above. You MUST write out the complete JSON object for EVERY pair in full - never use "...", "etc.", or any other abbreviation to skip or shorten repeated data, even if pairs share similar values. Each pair's full object must be written out explicitly. Use EXACTLY these field names in every object: pair, base_gdp, quote_gdp, base_current_account, quote_current_account, base_rate_direction, quote_rate_direction, base_index_level, base_index_12mo_high, total_score, bias, source_url - do not add extra fields, do not rename any field, and never omit a field (use "N/A" as its value instead of leaving it out)."""

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
    ws = get_or_create_tab(spreadsheet, SHEET_TAB_NAME, rows=250, cols=12)

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

    # Stamp the cutoff date and any flags in the rows directly below the data
    # table (column A) rather than off to the side - avoids exceeding the
    # sheet's column count regardless of how many columns the tab has.
    footer_row = len(values) + 2
    footer_values = [[f"Last auto-updated: {today} | {data.get('data_cutoff', '')}"]]

    flags = data.get("data_unavailable_flags", [])
    if flags:
        footer_values.append(["DATA UNAVAILABLE / NEEDS MANUAL CHECK:"])
        footer_values.extend([[f] for f in flags])

    ws.update(footer_values, f"A{footer_row}")
def write_carry_trade(spreadsheet, data: dict, today: str):
    ws = get_or_create_tab(spreadsheet, CARRY_TAB_NAME, rows=50, cols=7)
    values = [CARRY_HEADER_ROW]
    for row in data["rows"]:
        values.append([
            row["funding"],
            row["target"],
            row["real_rate_differential"],
            row["carry_score"],
            row["funding_pressure"],
            row["capital_flow"],
            row.get("source_url", ""),
        ])
    ws.update(values, "A1")
    ws.format("A1:G1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.05, "green": 0.1, "blue": 0.16}})

    footer_row = len(values) + 2
    footer_values = [[f"Last auto-updated: {today} | {data.get('data_cutoff', '')}"]]
    flags = data.get("data_unavailable_flags", [])
    if flags:
        footer_values.append(["DATA UNAVAILABLE / NEEDS MANUAL CHECK:"])
        footer_values.extend([[f] for f in flags])
    ws.update(footer_values, f"A{footer_row}")


def write_central_bank(spreadsheet, data: dict, today: str):
    ws = get_or_create_tab(spreadsheet, CENTRAL_BANK_TAB_NAME, rows=30, cols=7)
    values = [CENTRAL_BANK_HEADER_ROW]
    for row in data["rows"]:
        values.append([
            row["central_bank"],
            row["bias"],
            row["score"],
            row["latest_meeting"],
            row["next_meeting"],
            row["forward_guidance_note"],
            row.get("source_url", ""),
        ])
    ws.update(values, "A1")
    ws.format("A1:G1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.05, "green": 0.1, "blue": 0.16}})

    footer_row = len(values) + 2
    footer_values = [[f"Last auto-updated: {today} | {data.get('data_cutoff', '')}"]]
    flags = data.get("data_unavailable_flags", [])
    if flags:
        footer_values.append(["DATA UNAVAILABLE / NEEDS MANUAL CHECK:"])
        footer_values.extend([[f] for f in flags])
    ws.update(footer_values, f"A{footer_row}")


def write_exogenous(spreadsheet, data: dict, today: str):
    ws = get_or_create_tab(spreadsheet, EXOGENOUS_TAB_NAME, rows=30, cols=12)
    values = [EXOGENOUS_HEADER_ROW]
    for row in data["rows"]:
        print(f"[F4P Weekly Update] DEBUG Exogenous row keys: {list(row.keys())}")
        values.append([
            row.get("pair", "N/A"),
            row.get("base_gdp", "N/A"),
            row.get("quote_gdp", "N/A"),
            row.get("base_current_account", "N/A"),
            row.get("quote_current_account", "N/A"),
            row.get("base_rate_direction", "N/A"),
            row.get("quote_rate_direction", "N/A"),
            row.get("base_index_level", "N/A"),
            row.get("base_index_12mo_high", "N/A"),
            row.get("total_score", "N/A"),
            row.get("bias", "N/A"),
            row.get("source_url", ""),
        ])
    ws.update(values, "A1")
    ws.format("A1:L1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.05, "green": 0.1, "blue": 0.16}})

    footer_row = len(values) + 2
    footer_values = [[f"Last auto-updated: {today} | {data.get('data_cutoff', '')}"]]
    flags = data.get("data_unavailable_flags", [])
    if flags:
        footer_values.append(["DATA UNAVAILABLE / NEEDS MANUAL CHECK:"])
        footer_values.extend([[f] for f in flags])
    ws.update(footer_values, f"A{footer_row}")

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
# --- Carry Trade ---
    try:
        print("[F4P Weekly Update] Building Carry Trade batch...")
        carry_prompt = build_carry_prompt(today, CARRY_PAIRS)
        carry_data = call_claude(carry_prompt)
        print(f"[F4P Weekly Update] Writing Carry Trade to '{CARRY_TAB_NAME}' tab...")
        write_carry_trade(spreadsheet, carry_data, today)
    except Exception as exc:
        print(f"[F4P Weekly Update] Carry Trade batch FAILED: {exc}")
        all_flags.append(f"CARRY TRADE BATCH FAILED: {exc}")

    # --- Central Bank ---
    try:
        print("[F4P Weekly Update] Building Central Bank batch...")
        cb_prompt = build_central_bank_prompt(today, CENTRAL_BANKS)
        cb_data = call_claude(cb_prompt)
        print(f"[F4P Weekly Update] Writing Central Bank to '{CENTRAL_BANK_TAB_NAME}' tab...")
        write_central_bank(spreadsheet, cb_data, today)
    except Exception as exc:
        print(f"[F4P Weekly Update] Central Bank batch FAILED: {exc}")
        all_flags.append(f"CENTRAL BANK BATCH FAILED: {exc}")

    # --- Exogenous ---
    try:
        print("[F4P Weekly Update] Building Exogenous batch...")
        exo_prompt = build_exogenous_prompt(today, EXOGENOUS_PAIRS)
        exo_data = call_claude(exo_prompt)
        print(f"[F4P Weekly Update] Writing Exogenous to '{EXOGENOUS_TAB_NAME}' tab...")
        write_exogenous(spreadsheet, exo_data, today)
    except Exception as exc:
        print(f"[F4P Weekly Update] Exogenous batch FAILED: {exc}")
        all_flags.append(f"EXOGENOUS BATCH FAILED: {exc}")
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
