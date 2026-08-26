"""
f4p_equities_qualitative_update.py

Phase 3 of the F4P Equities & Options pipeline: the two indicators that
genuinely need Claude, not a numeric Alpha Vantage threshold:

  6. Forward Guidance   - did the company raise/lower/maintain guidance
                           on their last earnings call? Scored -2 to +2.
  7. Catalyst Pipeline   - up to 3 real, dated upcoming events (product
                           launches, regulatory decisions, etc). Purely
                           informational - always scored 0. This feeds
                           the "Catalyst" column context in STRATEGY
                           DASHBOARD alongside the EARNINGS CALENDAR data.

Uses the same mechanism as the FX Hub's f4p_weekly_update.py: Claude
with web search, since this needs current news a structured API can't
give a threshold on.

IMPORTANT - RUN ORDER: this script must run AFTER
f4p_equities_weekly_update.py each week. That script's write_rows()
clears the full EQUITIES HUB DATA range (A2:L) before rewriting its own
14 indicators - if this script ran first, its rows would be wiped by
the next Alpha Vantage run. Run this one second, every week.

IMPORTANT - not independently tested: unlike every other script in this
pipeline, I could not run a live test call against this before handing
it over - I have no Anthropic API key available in my own environment.
The code pattern mirrors the FX Hub's already-proven Claude+web-search
usage, but the first real run here is the actual first test. Check the
JSON output for a couple of tickers by hand before trusting the scores.

Required secrets:
  ANTHROPIC_API_KEY   - same key already used by the FX pipeline
  GOOGLE_CREDENTIALS  - same service account used by the rest of the Hub
  EQUITIES_SHEET_ID   - F4P Equities & Options Scorecard file ID
"""

import os
import json
import time
import gspread
from google.oauth2.service_account import Credentials
import anthropic

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

WATCHLIST = ["NVDA", "AAPL", "AMZN", "GOOGL", "TSLA", "META", "COIN", "NFLX", "QQQ"]

MODEL = "claude-sonnet-5"

PROMPT_TEMPLATE = """You are a professional equity research analyst. Research {ticker} using web search and respond with ONLY valid JSON - no markdown code fences, no commentary before or after the JSON.

Find:
1. The company's most recent forward guidance (from their last earnings call or press release). Did they raise, lower, maintain, or not provide clear guidance for the next quarter/year?
2. Up to 3 specific, real, dated (or approximately dated) upcoming catalysts in the next 90 days - product launches, regulatory decisions, court rulings, major conferences, etc. Only include real, sourced items you actually found - do not invent generic placeholders. If you find fewer than 3 real catalysts, return fewer - do not pad the list.

If {ticker} is an ETF (like QQQ) rather than a single company, guidance_direction should be "none" and catalysts should be empty - ETFs don't issue guidance or have company-specific catalysts.

Respond with exactly this JSON structure and nothing else. Do not truncate with "..." - always return the complete, valid structure:
{{
  "guidance_direction": "raised" | "lowered" | "maintained" | "none",
  "guidance_score": <integer from -2 to 2>,
  "guidance_summary": "<one sentence, under 200 characters>",
  "guidance_source": "<url or empty string if none>",
  "catalysts": [
    {{"event": "<short description>", "approx_date": "<YYYY-MM-DD or e.g. 'Q4 2026'>", "source": "<url>"}}
  ]
}}"""


def get_qualitative_data(ticker, client):
    """Calls Claude with web search, returns the parsed dict or None on failure."""
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": PROMPT_TEMPLATE.format(ticker=ticker)}],
        )
        # Concatenate all text blocks - web search responses can span multiple blocks
        text = "".join(block.text for block in response.content if block.type == "text")
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        return json.loads(cleaned.strip())
    except json.JSONDecodeError as e:
        print(f"[FAIL] {ticker} JSON parse error: {e}. Raw text: {text[:500]!r}")
        return None
    except Exception as e:
        print(f"[FAIL] {ticker} Claude call failed: {e}")
        return None


def score_guidance(direction, raw_score):
    """Validates Claude's self-reported score against its own stated
    direction - a form of the same defensive-parsing discipline used
    throughout the Alpha Vantage side of this pipeline, just applied to
    an LLM response instead of an API response."""
    try:
        score = int(raw_score)
        score = max(-2, min(2, score))  # clamp to the framework's range
    except (ValueError, TypeError):
        score = 0

    if direction == "raised" and score < 0:
        score = 1  # direction/score mismatch - trust direction, use a mild default
    if direction == "lowered" and score > 0:
        score = -1
    if direction in ("maintained", "none"):
        score = 0 if direction == "none" else score

    return score


def fetch_ticker_qualitative(ticker, client, today):
    rows = []
    data = get_qualitative_data(ticker, client)

    if data is None:
        rows.append([
            ticker, 6, "Forward Guidance", "N/A", "N/A", "N/A", "N/A", today,
            "Endogenous", 0, "N/A - Claude call or JSON parse failed this run",
            "Claude (web search)",
        ])
        rows.append([
            ticker, 7, "Catalyst Pipeline", "N/A", "N/A", "N/A", "N/A", today,
            "Endogenous", 0, "N/A - Claude call or JSON parse failed this run",
            "Claude (web search)",
        ])
        return rows

    direction = data.get("guidance_direction", "none")
    score = score_guidance(direction, data.get("guidance_score", 0))
    summary = data.get("guidance_summary", "N/A")
    source = data.get("guidance_source", "N/A") or "N/A"
    rows.append([
        ticker, 6, "Forward Guidance", direction, "N/A", "N/A", "N/A", today,
        "Endogenous", score, summary, f"Claude (web search): {source}",
    ])

    catalysts = data.get("catalysts") or []
    if catalysts:
        catalyst_text = " | ".join(
            f"{c.get('event', '?')} ({c.get('approx_date', '?')})" for c in catalysts[:3]
        )
        sources = " | ".join(c.get("source", "") for c in catalysts[:3] if c.get("source"))
    else:
        catalyst_text = "No specific near-term catalysts found"
        sources = "N/A"
    rows.append([
        ticker, 7, "Catalyst Pipeline", catalyst_text, "N/A", "N/A", "N/A", today,
        "Endogenous", 0, catalyst_text, f"Claude (web search): {sources}",
    ])

    return rows


def append_qualitative_rows(spreadsheet, all_rows):
    """Appends rather than clears - this script runs second in the
    weekly sequence, adding to what f4p_equities_weekly_update.py just
    wrote. Removes any prior week's rows for indicators 6/7 first so
    re-runs don't accumulate duplicates."""
    ws = spreadsheet.worksheet("EQUITIES HUB DATA")
    existing = ws.get_all_values()
    rows_to_delete = [
        i + 1 for i, row in enumerate(existing)
        if len(row) > 1 and row[1] in ("6", "7")
    ]
    for row_num in reversed(rows_to_delete):
        ws.delete_rows(row_num)
    if all_rows:
        ws.append_rows(all_rows, value_input_option="USER_ENTERED")
    print(f"[OK] Wrote {len(all_rows)} qualitative rows (indicators 6 & 7) "
          f"across {len(WATCHLIST)} tickers")


def main():
    api_key = os.environ["ANTHROPIC_API_KEY"]
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(os.environ["EQUITIES_SHEET_ID"])
    client = anthropic.Anthropic(api_key=api_key)

    today = time.strftime("%Y-%m-%d")
    all_rows = []
    for ticker in WATCHLIST:
        print(f"\n--- Researching {ticker} ---")
        all_rows.extend(fetch_ticker_qualitative(ticker, client, today))
        time.sleep(2)

    append_qualitative_rows(spreadsheet, all_rows)
    print(f"\nDone. {len(all_rows)} rows across {len(WATCHLIST)} tickers.")


if __name__ == "__main__":
    main()

    
