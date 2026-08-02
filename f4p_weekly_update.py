    #!/usr/bin/env python3
"""
F4P Weekly Hub Data Automation
================================
Calls Claude (with web search) to gather all 8 currencies x 15 indicators,
then writes the result directly into the Google Sheet "AI HUB DATA" tab.

Runs every Sunday via GitHub Actions. See .github/workflows/f4p_weekly_update.yml

--section flag (new): run one section in isolation instead of the full
pipeline. Use this to test/re-run a single tab fix without spending API
budget on the other 10 batches:

    python f4p_weekly_update.py --section exogenous
    python f4p_weekly_update.py --section hub
    python f4p_weekly_update.py --section carry
    python f4p_weekly_update.py --section central_bank
    python f4p_weekly_update.py            # same as --section all (default)

The GitHub Actions workflow exposes the same choice via workflow_dispatch,
so this can also be triggered from the Actions tab without touching a
terminal at all.

Required environment variables / GitHub Secrets:
  ANTHROPIC_API_KEY   - from console.anthropic.com
  GOOGLE_CREDENTIALS  - JSON string of your Google service account credentials
                        (same secret already used by your FRED AUTO script)
  GOOGLE_SHEET_ID     - your F4P Google Sheet ID (already in use for FRED AUTO)
"""

import argparse
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

# Model note: Sonnet 5 is running introductory pricing ($2/$10 per MTok)
# through Aug 31, 2026, vs Sonnet 4.6's standard $3/$15 - a straight ~33%
# cut on every batch below at equal or better quality. Reverts to $3/$15
# (same as 4.6) on Sep 1, 2026 - worth revisiting the model string then.
MODEL = "claude-sonnet-5"

# fred_cot_fetcher.py (a separate, already-scheduled, FREE workflow - see
# .github/workflows/fred_cot_fetcher.yml) writes real FRED figures into this
# tab every day. It's US-series only, so this only ever helps the USD batch -
# it has zero effect on the other 7 currencies. Mapping is deliberately
# conservative: only indicators where the FRED series is a clean, direct
# match for what our prompt asks for. Left out on purpose: GDP Growth Rate %
# (FRED series is growth-rate, our "Government Debt / GDP" indicator is a
# debt ratio - different concept) and Unemployment Rate % (no direct
# indicator of ours asks for this specifically). "Central Bank Rate &
# Current Policy Stance" only gets the rate NUMBER pre-filled - the
# hawkish/dovish "stance" half of that indicator still needs real research,
# so it isn't fully covered by this map.
FRED_AUTO_TAB_NAME = "FRED AUTO"
FRED_AUTO_INDICATOR_MAP = {
    "Consumer Sentiment": "Michigan Consumer Sentiment",
    "Building Permits / Housing": "Building Permits (000s)",
    "CPI YoY": "CPI YoY %",
    "Core CPI": "Core CPI YoY %",
    "PPI": "PPI Headline YoY %",
    "Core PPI": "Core PPI YoY %",
    "Central Bank Rate & Current Policy Stance": "Federal Funds Rate %",
}


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


def read_fred_auto_facts(spreadsheet) -> dict:
    """
    Reads the FRED AUTO tab - populated free, daily, by the separate
    fred_cot_fetcher.py workflow - and returns {our_indicator_name: (value,
    release_date, source)} for whichever of the 15 USD indicators have a
    direct FRED equivalent (see FRED_AUTO_INDICATOR_MAP above).

    This is a pure optimization: any failure here (tab missing, malformed
    row, whatever) just means the USD batch searches for everything itself,
    same as it always has. It must never be allowed to fail the actual run.
    """
    try:
        ws = spreadsheet.worksheet(FRED_AUTO_TAB_NAME)
        rows = ws.get_all_values()
    except Exception as exc:
        print(f"[F4P Weekly Update] Could not read '{FRED_AUTO_TAB_NAME}' tab ({exc}) - "
              f"USD batch will search for all 15 indicators as normal.")
        return {}

    by_fred_label = {}
    for row in rows:
        if len(row) < 6:
            continue
        _, indicator, value, release_date, source, status = row[:6]
        if indicator and value and status.strip().upper() == "OK":
            by_fred_label[indicator.strip()] = (value.strip(), release_date.strip(), source.strip())

    facts = {}
    for our_name, fred_label in FRED_AUTO_INDICATOR_MAP.items():
        if fred_label in by_fred_label:
            facts[our_name] = by_fred_label[fred_label]

    print(f"[F4P Weekly Update] FRED AUTO supplied {len(facts)}/{len(FRED_AUTO_INDICATOR_MAP)} "
          f"pre-verified USD figures (saves searching for those specific numbers).")
    return facts


def build_prompt(today: str, currency_batch: list, known_facts: dict = None) -> str:
    indicator_list = "\n".join(f"  {i+1}. {name}" for i, name in enumerate(INDICATORS))
    currency_list = ", ".join(currency_batch)

    known_facts_block = ""
    if known_facts:
        fact_lines = "\n".join(
            f'  - {name}: current_value = "{value}" (release date {release_date}, source {source}) - '
            f"already verified, do NOT search for this number, use it exactly as given."
            for name, (value, release_date, source) in known_facts.items()
        )
        known_facts_block = f"""
IMPORTANT - the current_value for these indicators is already verified below. Do NOT web search to re-find these specific numbers - use them exactly as given, and spend that search budget instead on finding the prior_value, forecast/consensus, and scoring context needed to score and analyze them properly:
{fact_lines}
"""

    return f"""You are a professional macro FX research analyst producing the Fishin4Pips (F4P) institutional Hub Data pack for the week of {today}.

Use web search. Use only official primary sources: central bank websites, national statistics offices, ismworld.org, FRED (fred.stlouisfed.org), OECD, IMF. Never fabricate a number. If a number is genuinely unavailable, write "data unavailable" and score it 0.

Produce the LATEST RELEASED data (as of {today}) for these currencies ONLY: {currency_list}.
{known_facts_block}
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

The rows array must contain exactly {len(pairs)} entries, one per pair listed above. You MUST write out the complete JSON object for EVERY pair in full - never use "...", "etc.", or any other abbreviation to skip or shorten repeated data. Use EXACTLY these field names in every object: funding, target, real_rate_differential, carry_score, funding_pressure, capital_flow, source_url - do not rename any field, and every row MUST include a real source_url pointing to the specific page you used for that pair's rate/CPI data. Never omit source_url or leave it blank."""


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

For EACH pair, report the raw facts behind 4 structural drivers, and score each driver on a flat -2 to +2 scale from the BASE currency's perspective (positive = supportive of the base currency, negative = supportive of the quote currency):
1. gdp   - Relative GDP Growth: base country GDP% vs quote country GDP%
2. bop   - Balance of Payments: base current account % of GDP vs quote current account % of GDP
3. rate  - Interest Rate Differential & Stance: base policy rate + direction (Hiking/Hold-Hawkish/Hold-Neutral/Hold-Dovish/Cutting) vs quote
4. equity - Stock Market Returns / Relative Wealth: base country's major index level vs its own 12-month high

Do NOT compute an overall total or bias yourself - only report the 4 individual driver_scores plus the raw facts. The total and bias are computed separately from your 4 scores, so it is essential that driver_scores are accurate individually.

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
      "driver_scores": {{"gdp": -1, "bop": 1, "rate": 0, "equity": -1}},
      "source_url": "https://..."
    }}
  ],
  "data_unavailable_flags": ["List any pairs you could not find data for, or empty list"]
}}

The rows array must contain exactly {len(pairs)} entries, one per pair listed above. You MUST write out the complete JSON object for EVERY pair in full - never use "...", "etc.", or any other abbreviation to skip or shorten repeated data, even if pairs share similar values. Each pair's full object must be written out explicitly. Use EXACTLY these field names in every object: pair, base_gdp, quote_gdp, base_current_account, quote_current_account, base_rate_direction, quote_rate_direction, base_index_level, base_index_12mo_high, driver_scores, source_url - do not add extra fields, do not rename any field, and never omit a field (use "N/A" as its value instead of leaving it out). driver_scores must always be an object with exactly these 4 keys: gdp, bop, rate, equity."""


def call_claude(prompt: str, max_uses: int = 20) -> dict:
    """
    Calls Claude with the server-side web_search tool. For large research tasks,
    Claude may return stop_reason='pause_turn' partway through - this means it is
    still actively researching and needs another turn to continue, NOT that it
    failed. We must continue the SAME conversation (resend its own content back)
    until it reaches a real stop (end_turn) with a final text answer.

    max_uses is a ceiling on searches for THIS call, not a target - Claude
    typically stops well under it. Callers pass a value sized to what that
    section actually needs (see run_* functions below) rather than one global
    number for every batch.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    messages = [{"role": "user", "content": prompt}]
    max_continuations = 6  # safety cap - each continuation lets it keep researching
    total_searches = 0

    for attempt in range(max_continuations):
        message = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses}],
            messages=messages,
        )

        # Real per-call telemetry - this is what should actually drive future
        # max_uses tuning, instead of guessing. server_tool_use may be absent
        # on SDK versions that predate this field, hence the defensive getattr.
        server_tool_use = getattr(message.usage, "server_tool_use", None)
        searches_this_call = getattr(server_tool_use, "web_search_requests", 0) or 0
        total_searches += searches_this_call

        print(f"[F4P Weekly Update] Attempt {attempt+1}: stop_reason={message.stop_reason}, "
              f"blocks={len(message.content)}, searches_this_call={searches_this_call}, "
              f"in_tokens={message.usage.input_tokens}, out_tokens={message.usage.output_tokens}")

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
            result = json.loads(json_candidate)
            print(f"[F4P Weekly Update] Call complete - total_searches_used={total_searches} "
                  f"(cap was {max_uses})")
            return result
        except json.JSONDecodeError:
            # This is the single most useful line in the whole log for diagnosing
            # a batch that goes stale on the sheet: if this prints, call_claude()
            # threw BEFORE the corresponding write_*() function ever ran, which
            # means that tab's row.keys() debug print (further down) never fired
            # this run. Check here first, not there, when a tab stops updating.
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
        print(f"[F4P Weekly Update] DEBUG Carry Trade row keys: {list(row.keys())}")
        values.append([
            row.get("funding", "N/A"),
            row.get("target", "N/A"),
            row.get("real_rate_differential", "N/A"),
            row.get("carry_score", "N/A"),
            row.get("funding_pressure", "N/A"),
            row.get("capital_flow", "N/A"),
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


def exogenous_bias_label(score: int) -> str:
    """Thresholds are a judgment call, not derived from anything - tune freely.
    Set at +/-3 (out of a possible +/-8) because Claude's own past self-reported
    bias labels weren't a consistent function of its own total_score (e.g. a
    +2 was once called 'Structurally Bullish' and a -2 'Mixed-Neutral' in the
    same week) - computing both deterministically in Python at least guarantees
    the label always means the same thing week to week."""
    if score >= 3:
        return "Structurally Bullish"
    if score <= -3:
        return "Structurally Bearish"
    return "Mixed-Neutral"


def write_exogenous(spreadsheet, data: dict, today: str):
    ws = get_or_create_tab(spreadsheet, EXOGENOUS_TAB_NAME, rows=30, cols=12)
    values = [EXOGENOUS_HEADER_ROW]
    for row in data["rows"]:
        driver_scores = row.get("driver_scores", {})
        if not isinstance(driver_scores, dict):
            driver_scores = {}

        print(f"[F4P Weekly Update] DEBUG Exogenous row keys: {list(row.keys())} | "
              f"driver_scores keys: {list(driver_scores.keys())}")

        # Sum whatever driver scores actually came back - matches compute_rankings()'s
        # approach for AI HUB DATA: arithmetic happens here in Python, not inside
        # Claude's response, so a missing/renamed key degrades gracefully (that
        # driver counts as 0) instead of corrupting the whole row's total.
        total = 0
        for key in ("gdp", "bop", "rate", "equity"):
            val = driver_scores.get(key, 0)
            try:
                total += int(val)
            except (TypeError, ValueError):
                pass

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
            total,
            exogenous_bias_label(total),
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


# ══════════════════════════════════════════════════════════════════════════
# SECTION RUNNERS - each is independently callable via --section, and each
# is now individually try/excepted by main() below. A total failure in one
# section (e.g. zero rows collected) can no longer take the other three
# sections down with it, which the previous single-main()-body version did:
# the old code's `raise RuntimeError(...)` on zero hub-data rows was outside
# any try/except, so a bad hub-data week meant Carry/Central Bank/Exogenous
# silently never ran either, with nothing in the log to say why.
# ══════════════════════════════════════════════════════════════════════════

def run_hub_data(spreadsheet, today: str) -> list:
    """AI HUB DATA + AI RANKINGS - 8 currency batches, 1 API call each."""
    all_rows = []
    all_flags = []
    batch_size = 1  # 1 currency per call (15 indicators) - most reliable, avoids pause_turn loops

    fred_facts = read_fred_auto_facts(spreadsheet)

    for batch_num, batch in enumerate(chunk(CURRENCIES, batch_size), start=1):
        print(f"[F4P Weekly Update] Batch {batch_num}: {batch}")
        known_facts = fred_facts if batch == ["USD"] else None
        prompt = build_prompt(today, batch, known_facts=known_facts)

        try:
            # USD gets a tighter search budget - up to 7 of its 15 indicators
            # may already have a verified current_value, so it needs less
            # searching than the other 7 currencies.
            data = call_claude(prompt, max_uses=12 if known_facts else 20)
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
        raise RuntimeError("All currency batches failed - no data collected at all. Not writing to sheet.")

    print(f"[F4P Weekly Update] Collected {len(all_rows)} total rows across all batches.")

    rankings = compute_rankings(all_rows)
    full_data = {"data_cutoff": f"Week of {today}", "rows": all_rows, "data_unavailable_flags": all_flags}

    print(f"[F4P Weekly Update] Writing {len(all_rows)} rows to '{SHEET_TAB_NAME}' tab...")
    write_hub_data(spreadsheet, full_data, today)

    print(f"[F4P Weekly Update] Writing rankings to '{RANKINGS_TAB_NAME}' tab...")
    write_rankings(spreadsheet, rankings)

    expected_total = len(CURRENCIES) * len(INDICATORS)
    if len(all_rows) < expected_total:
        print(f"[F4P Weekly Update] WARNING: only {len(all_rows)}/{expected_total} rows collected. "
              f"Glenise should verify missing currencies manually this week.")

    return all_flags


def run_carry_trade(spreadsheet, today: str) -> list:
    print("[F4P Weekly Update] Building Carry Trade batch...")
    carry_prompt = build_carry_prompt(today, CARRY_PAIRS)
    carry_data = call_claude(carry_prompt, max_uses=20)
    print(f"[F4P Weekly Update] Writing Carry Trade to '{CARRY_TAB_NAME}' tab...")
    write_carry_trade(spreadsheet, carry_data, today)
    return carry_data.get("data_unavailable_flags", [])


def run_central_bank(spreadsheet, today: str) -> list:
    print("[F4P Weekly Update] Building Central Bank batch...")
    cb_prompt = build_central_bank_prompt(today, CENTRAL_BANKS)
    cb_data = call_claude(cb_prompt, max_uses=15)
    print(f"[F4P Weekly Update] Writing Central Bank to '{CENTRAL_BANK_TAB_NAME}' tab...")
    write_central_bank(spreadsheet, cb_data, today)
    return cb_data.get("data_unavailable_flags", [])


def run_exogenous(spreadsheet, today: str) -> list:
    print("[F4P Weekly Update] Building Exogenous batch...")
    exo_prompt = build_exogenous_prompt(today, EXOGENOUS_PAIRS)
    exo_data = call_claude(exo_prompt, max_uses=30)  # heaviest/most fact-dense batch - see Phase 2 note
    print(f"[F4P Weekly Update] Writing Exogenous to '{EXOGENOUS_TAB_NAME}' tab...")
    write_exogenous(spreadsheet, exo_data, today)
    return exo_data.get("data_unavailable_flags", [])


SECTIONS = {
    "hub": run_hub_data,
    "carry": run_carry_trade,
    "central_bank": run_central_bank,
    "exogenous": run_exogenous,
}


def main():
    parser = argparse.ArgumentParser(
        description="F4P weekly update - run the full pipeline, or one section in isolation."
    )
    parser.add_argument(
        "--section",
        choices=["all", *SECTIONS.keys()],
        default="all",
        help="Run only this section instead of the full 11-batch pipeline. Default: all.",
    )
    args = parser.parse_args()

    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    print(f"[F4P Weekly Update] Starting run for {today} (section={args.section})")

    print("[F4P Weekly Update] Connecting to Google Sheet...")
    spreadsheet = connect_sheet()

    all_flags = []
    names = list(SECTIONS.keys()) if args.section == "all" else [args.section]

    for name in names:
        try:
            flags = SECTIONS[name](spreadsheet, today)
            all_flags.extend(flags or [])
        except Exception as exc:
            print(f"[F4P Weekly Update] Section '{name}' FAILED: {exc}")
            all_flags.append(f"{name.upper()} SECTION FAILED: {exc}")

    if all_flags:
        print(f"[F4P Weekly Update] {len(all_flags)} item(s) flagged as unavailable/failed - check sheet.")

    print("[F4P Weekly Update] Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[F4P Weekly Update] FAILED: {exc}", file=sys.stderr)
        sys.exit(1)

    
