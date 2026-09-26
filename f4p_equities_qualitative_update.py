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

PHASE 3 ADDITION (Thesis Workspace evidence layer): each catalyst is now
also captured in structured form (Type, Description, Expected Date,
Status, Source) and written to a dedicated "THESIS WORKSPACE CATALYSTS"
tab, in addition to the existing flat-text Catalyst Pipeline column in
EQUITIES HUB DATA (indicator #7), which is unchanged. setup_thesis_workspace.py
reads the structured tab to build the Catalyst Summary evidence field -
this script only produces the raw evidence, it does not touch the
Thesis Workspace's human thesis fields.

IMPORTANT - RUN ORDER: this script must run AFTER
f4p_equities_weekly_update.py each week. That script's write_rows()
clears the full EQUITIES HUB DATA range (A2:L) before rewriting its own
14 indicators - if this script ran first, its rows would be wiped by
the next Alpha Vantage run. Run this one second, every week. It must
also run BEFORE setup_thesis_workspace.py, since that script reads the
THESIS WORKSPACE CATALYSTS tab this script writes.

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
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
import anthropic

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

WATCHLIST = ["NVDA", "AAPL", "AMZN", "GOOGL", "TSLA", "META", "COIN", "NFLX", "QQQ"]

MODEL = "claude-sonnet-5"

CATALYST_TYPES = [
    "Earnings", "Product Launch", "Regulatory Decision", "M&A",
    "Legal/Litigation", "Macro/Sector Event", "Conference/Investor Day", "Other",
]

PROMPT_TEMPLATE = """You are a professional equity research analyst. Research {ticker} using web search and respond with ONLY valid JSON - no markdown code fences, no commentary before or after the JSON.

Find:
1. The company's most recent forward guidance (from their last earnings call or press release). Did they raise, lower, maintain, or not provide clear guidance for the next quarter/year?
2. Up to 3 specific, real, dated (or approximately dated) upcoming catalysts in the next 90 days - product launches, regulatory decisions, court rulings, major conferences, etc. Only include real, sourced items you actually found - do not invent generic placeholders. If you find fewer than 3 real catalysts, return fewer - do not pad the list. For each catalyst, classify it into exactly one of these types: Earnings, Product Launch, Regulatory Decision, M&A, Legal/Litigation, Macro/Sector Event, Conference/Investor Day, Other.

If {ticker} is an ETF (like QQQ) rather than a single company, guidance_direction should be "none" and catalysts should be empty - ETFs don't issue guidance or have company-specific catalysts.

Respond with exactly this JSON structure and nothing else. Do not truncate with "..." - always return the complete, valid structure:
{{
  "guidance_direction": "raised" | "lowered" | "maintained" | "none",
  "guidance_score": <integer from -2 to 2>,
  "guidance_summary": "<one sentence, under 200 characters>",
  "guidance_source": "<url or empty string if none>",
  "catalysts": [
    {{"event": "<short description>", "approx_date": "<YYYY-MM-DD or e.g. 'Q4 2026'>", "type": "<one of: Earnings, Product Launch, Regulatory Decision, M&A, Legal/Litigation, Macro/Sector Event, Conference/Investor Day, Other>", "source": "<url>"}}
  ]
}}"""


def get_qualitative_data(ticker, client):
    """Calls Claude with web search, returns the parsed dict or None on failure."""
    text = ""
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": PROMPT_TEMPLATE.format(ticker=ticker)}],
        )
        # Concatenate all text blocks - web search responses can span multiple blocks
        text = "".join(block.text for block in response.content if block.type == "text")

        # Claude sometimes adds a lead-in sentence before the JSON despite
        # explicit instructions not to - confirmed happening for COIN on
        # 2026-08-27 ("Now I have enough info to compile the final
        # answer." prefixed the actual JSON object, which made json.loads
        # fail immediately at char 0). Extract the {...} span directly
        # instead of assuming the whole response is clean JSON - this
        # handles a leading/trailing sentence with or without markdown
        # fences also being present.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            print(f"[FAIL] {ticker} no JSON object found in response. Raw text: {text[:500]!r}")
            return None
        return json.loads(text[start:end + 1])
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


def derive_catalyst_status(approx_date_str, today_date):
    """Pure date-comparison, no scoring, no judgment: compares a
    catalyst's approx_date against today's date to derive a simple
    Passed/Upcoming status for the Thesis Workspace evidence layer.

    Many catalyst dates are genuinely vague this far out ("Q4 2026",
    "TBD", "Late 2026") - that's the best real information available,
    not a data-quality failure, so those return "Not Verified" rather
    than guessing a date, per the same N/A discipline used everywhere
    else in this pipeline.

    approx_date_str: the string Claude returned for a catalyst's date.
    today_date: a datetime.date object (not a string) for comparison.
    """
    if not approx_date_str or not isinstance(approx_date_str, str):
        return "Not Verified"
    approx_date_str = approx_date_str.strip()
    if not approx_date_str:
        return "Not Verified"
    try:
        parsed = datetime.strptime(approx_date_str, "%Y-%m-%d").date()
    except ValueError:
        return "Not Verified"
    if parsed < today_date:
        return "Passed"
    return "Upcoming"


def fetch_ticker_qualitative(ticker, client, today):
    """Returns (rows, structured_catalysts):
      rows                - the existing 2-row list for EQUITIES HUB DATA
                             (indicators 6 and 7), unchanged in shape.
      structured_catalysts - a list of dicts, one per catalyst (up to 3),
                             for the new THESIS WORKSPACE CATALYSTS tab.
    """
    rows = []
    structured_catalysts = []
    today_date = datetime.strptime(today, "%Y-%m-%d").date()
    data = get_qualitative_data(ticker, client)

    if data is None:
        rows.append([
            ticker, 6, "Forward Guidance", "N/A", "N/A", "N/A", "N/A", today,
            "Endogenous", 0, "N/A - Claude call or JSON parse failed this run",
            "Claude (web search)", "FUNDAMENTAL",
        ])
        rows.append([
            ticker, 7, "Catalyst Pipeline", "N/A", "N/A", "N/A", "N/A", today,
            "Endogenous", 0, "N/A - Claude call or JSON parse failed this run",
            "Claude (web search)", "EXPECTATIONS",
        ])
        return rows, structured_catalysts

    direction = data.get("guidance_direction", "none")
    score = score_guidance(direction, data.get("guidance_score", 0))
    summary = data.get("guidance_summary", "N/A")
    source = data.get("guidance_source", "N/A") or "N/A"
    rows.append([
        ticker, 6, "Forward Guidance", direction, "N/A", "N/A", "N/A", today,
        "Endogenous", score, summary, f"Claude (web search): {source}", "FUNDAMENTAL",
    ])

    catalysts = data.get("catalysts") or []
    if catalysts:
        catalyst_text = " | ".join(
            f"{c.get('event', '?')} ({c.get('approx_date', '?')})" for c in catalysts[:3]
        )
        sources = " | ".join(c.get("source", "") for c in catalysts[:3] if c.get("source"))
        for c in catalysts[:3]:
            approx_date = c.get("approx_date", "") or ""
            catalyst_type = c.get("type", "Other") or "Other"
            if catalyst_type not in CATALYST_TYPES:
                catalyst_type = "Other"
            structured_catalysts.append({
                "ticker": ticker,
                "type": catalyst_type,
                "description": c.get("event", "N/A") or "N/A",
                "expected_date": approx_date or "N/A",
                "status": derive_catalyst_status(approx_date, today_date),
                "source": c.get("source", "N/A") or "N/A",
            })
    else:
        catalyst_text = "No specific near-term catalysts found"
        sources = "N/A"
    rows.append([
        ticker, 7, "Catalyst Pipeline", catalyst_text, "N/A", "N/A", "N/A", today,
        "Endogenous", 0, catalyst_text, f"Claude (web search): {sources}", "EXPECTATIONS",
    ])

    return rows, structured_catalysts


def append_qualitative_rows(spreadsheet, all_rows):
    """Appends rather than clears - this script runs second in the
    weekly sequence, adding to what f4p_equities_weekly_update.py just
    wrote. Removes any prior week's rows for indicators 6/7 first so
    re-runs don't accumulate duplicates.

    Then VERIFIES the write actually persisted. Confirmed necessary on
    2026-08-29: a run logged "Wrote 18 rows... Done" and reported
    success, but the rows never actually appeared in the Sheet - every
    ticker was missing indicators 6 and 7 entirely, silently, with no
    error anywhere. That's worse than every other failure mode in this
    pipeline, since those all showed up as a visible [FAIL] or a red X.
    This makes a repeat of that impossible to miss."""
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
    print(f"[OK] Appended {len(all_rows)} qualitative rows (indicators 6 & 7) "
          f"across {len(WATCHLIST)} tickers")

    def find_missing():
        verify_rows = ws.get_all_values()
        missing = []
        for ticker in WATCHLIST:
            found_6 = any(len(r) > 1 and r[0] == ticker and r[1] == "6" for r in verify_rows)
            found_7 = any(len(r) > 1 and r[0] == ticker and r[1] == "7" for r in verify_rows)
            if not found_6:
                missing.append(f"{ticker} indicator 6")
            if not found_7:
                missing.append(f"{ticker} indicator 7")
        return missing

    missing = find_missing()
    if missing:
        print(f"[VERIFY] {len(missing)} row(s) missing on first check - "
              f"retrying once after 5s in case of a Sheets API propagation "
              f"delay before treating this as a real failure...")
        time.sleep(5)
        missing = find_missing()

    if missing:
        print(f"[VERIFY FAILED] Still missing after retry: {missing}")
        raise RuntimeError(
            f"Verification failed: {len(missing)} expected row(s) missing after "
            f"write and retry. The append call reported success but the data did "
            f"not persist - exactly the failure mode caught on 2026-08-29. Check "
            f"GOOGLE_CREDENTIALS permissions and Google Sheets API status before "
            f"re-running."
        )
    print(f"[VERIFIED] All {len(WATCHLIST) * 2} qualitative rows confirmed "
          f"actually present in the Sheet, not just reported as written")


def write_catalyst_detail(spreadsheet, all_catalysts):
    """Writes the structured, per-field catalyst breakdown to a
    dedicated tab for the Phase 3 Thesis Workspace to read from. This
    is additive - the existing flat-text Catalyst Pipeline column in
    EQUITIES HUB DATA (indicator #7) is untouched and keeps feeding
    STRATEGY DASHBOARD exactly as before. This tab exists purely so
    setup_thesis_workspace.py has structured fields (Type, Status) to
    build the Catalyst Summary evidence field from, instead of
    re-parsing flat text.

    Full-clear-and-rewrite every run, same as the rest of this pipeline's
    non-manual tabs - there is no human-entered data in this tab to
    preserve."""
    try:
        ws = spreadsheet.worksheet("THESIS WORKSPACE CATALYSTS")
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="THESIS WORKSPACE CATALYSTS", rows=100, cols=6)

    header = ["Ticker", "Type", "Description", "Expected Date", "Status", "Source"]
    rows_out = [header]
    for c in all_catalysts:
        rows_out.append([
            c["ticker"], c["type"], c["description"], c["expected_date"],
            c["status"], c["source"],
        ])
    ws.update(range_name="A1", values=rows_out)
    print(f"[OK] Wrote {len(all_catalysts)} structured catalyst rows to "
          f"THESIS WORKSPACE CATALYSTS")


def main():
    api_key = os.environ["ANTHROPIC_API_KEY"]
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(os.environ["EQUITIES_SHEET_ID"])
    client = anthropic.Anthropic(api_key=api_key)

    today = time.strftime("%Y-%m-%d")
    all_rows = []
    all_catalysts = []
    for ticker in WATCHLIST:
        print(f"\n--- Researching {ticker} ---")
        rows, catalysts = fetch_ticker_qualitative(ticker, client, today)
        all_rows.extend(rows)
        all_catalysts.extend(catalysts)
        time.sleep(2)

    append_qualitative_rows(spreadsheet, all_rows)
    write_catalyst_detail(spreadsheet, all_catalysts)
    print(f"\nDone. {len(all_rows)} rows across {len(WATCHLIST)} tickers, "
          f"{len(all_catalysts)} structured catalysts captured.")


if __name__ == "__main__":
    main()

    
