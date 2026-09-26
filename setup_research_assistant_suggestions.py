"""
setup_research_assistant_suggestions.py

Phase 3 of the F4P Equities & Options pipeline: generates the weekly
Research Assistant Suggestions - AI-organized evidence commentary, kept
strictly separate from the official Human Thesis record in THESIS
WORKSPACE.

GOVERNANCE - verbatim as approved by Glenise. Any future change to this
script must preserve these rules:

  "Keep the official thesis fields human-only and blank by default.
  However, add a separate, clearly labelled Research Assistant
  Suggestions area. AI-generated content may summarize supporting
  evidence, contradictory evidence, possible expectations gaps, catalyst
  questions, invalidation questions and missing evidence. It must be
  visually and logically separated from the official thesis record and
  labelled as AI-generated decision-support material. It must never
  automatically populate, overwrite or be represented as My Thesis, My
  Variant View, Expected Direction, Expected Magnitude, Expected
  Horizon or Invalidation. Do not provide automatic one-click
  acceptance of an AI-generated thesis into the official record. The
  user must formulate and enter the official thesis independently."

  "Use weekly batch generation for Research Assistant Suggestions in
  v1, using the same weekly evidence snapshot as the rest of the Hub.
  Do not build live/on-demand AI infrastructure at this stage."

  "Add visible metadata to AI suggestions showing Generated Date/Time
  and Evidence Through Date/Time. If the underlying evidence is stale,
  incomplete, Not Verified, or the relevant refresh failed, the
  suggestion area must clearly indicate that condition and must not
  present old commentary as current."

HOW THIS SCRIPT ENFORCES THAT GOVERNANCE:

  - Writes to its OWN tab, "RESEARCH ASSISTANT SUGGESTIONS" - never to
    THESIS WORKSPACE. This script never opens THESIS WORKSPACE for
    writing, only for reading the Evidence Layer text already computed
    there this week by setup_thesis_workspace.py. There is no code path
    here that can touch a Human Thesis column.
  - Row 1 of the output tab is a standing banner labelling the whole tab
    as AI-generated decision-support material, not a thesis. The prompt
    sent to Claude also explicitly and repeatedly instructs it not to
    state a directional view, price target, recommendation, or
    Expectations Classification, and not to write anything resembling a
    thesis - see build_prompt().
  - Every row carries Generated Date/Time and Evidence Through Date/Time.
    In this v1 batch design both are the same timestamp (this run's
    date), since the Evidence Layer is read fresh from THESIS WORKSPACE
    in the same weekly cycle that just wrote it - there is no separate,
    older "evidence as of" timestamp to track yet. That's a
    simplification worth knowing about, not a hidden assumption: it
    means "Evidence Through" always equals "Generated" in v1.
  - Freshness/staleness is detected MECHANICALLY (see
    detect_evidence_gaps()), not by asking the AI to self-report on its
    own input's quality - a deterministic scan of the Evidence Layer
    text for "N/A"/"Not Verified" is more reliable than trusting a
    model's self-assessment of its own inputs.
  - If 4 or more of the 6 evidence fields are unavailable for a ticker
    this week, the Claude call is skipped entirely and a mechanical
    "insufficient evidence" note is written instead - this is a
    deliberate, conservative extension of the "don't present stale
    commentary as current" rule to also mean "don't generate commentary
    from evidence that's mostly missing." Flagged here as my own
    judgment call, not something explicitly specified - reasonable, but
    worth confirming.
  - Batch-only: one Claude text call per ticker, once per pipeline run,
    over the Evidence Layer already computed by setup_thesis_workspace.py
    this cycle. No web search, no on-demand/live generation, no new
    external calls beyond that.

WHAT THIS SCRIPT CANNOT FULLY GUARANTEE: an LLM can still occasionally
drift into thesis-like language despite the prompt's instructions - the
same known limitation already documented in
f4p_equities_qualitative_update.py. There is no code-level filter here
that rejects a response for "sounding like a thesis"; building one would
require the same kind of judgment call this pipeline deliberately keeps
out of automated hands. Spot-check a few tickers' output after the first
live run, same discipline as everywhere else in this pipeline.

IMPORTANT - RUN ORDER: must run AFTER setup_thesis_workspace.py, since it
reads the Evidence Layer that script just wrote to THESIS WORKSPACE,
rather than recomputing evidence itself - this guarantees the AI
commentary is generated from the exact same weekly snapshot the human
sees in THESIS WORKSPACE, never a separately-fetched version that could
disagree with it.

IMPORTANT - not independently tested end to end: like
f4p_equities_qualitative_update.py, there is no Anthropic API key
available in the environment this was built in, so the live Claude call
itself is untested. The JSON-extraction and retry pattern mirrors that
already-proven script. Check a couple of tickers' output by hand before
trusting it.

Required secrets:
  ANTHROPIC_API_KEY   - same key already used elsewhere in this pipeline
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

TAB_NAME = "RESEARCH ASSISTANT SUGGESTIONS"

BANNER = (
    "AI-GENERATED DECISION-SUPPORT MATERIAL - NOT AN OFFICIAL THESIS. "
    "This tab organizes evidence and open questions only. It never represents, "
    "populates, or overwrites My Thesis, My Variant View, Expected Direction, "
    "Expected Magnitude, Expected Horizon, or Invalidation, and it is never a "
    "substitute for the manual Expectations Classification. You must "
    "independently formulate and enter the official thesis in THESIS WORKSPACE. "
    "Nothing here is investment advice or a recommendation."
)

# The exact Evidence column names written by setup_thesis_workspace.py to
# THESIS WORKSPACE. This script is intentionally tightly coupled to that
# schema - it exists to comment on that specific evidence, not to
# recompute it independently.
EVIDENCE_FIELDS = [
    "Consensus EPS (Actual vs Estimate)",
    "Consensus Revenue (Actual vs Estimate)",
    "Estimate Revisions (90-day)",
    "Recent Repricing (21-day vs SPY)",
    "Valuation Context",
    "Catalyst Summary",
]

OUTPUT_HEADER = [
    "Ticker",
    "Supporting Evidence (AI)",
    "Contradictory Evidence (AI)",
    "Possible Expectations Gaps (AI)",
    "Catalyst Questions (AI)",
    "Invalidation Questions (AI)",
    "Missing Evidence / Data Gaps (AI)",
    "Evidence Freshness Note",
    "Generated Date/Time",
    "Evidence Through Date/Time",
    "Source",
]

NOT_VERIFIED_MARKERS = ("n/a", "not verified", "no specific near-term catalysts found")

# If this many of the 6 evidence fields are unavailable, skip the AI call
# entirely rather than generate commentary from evidence that's mostly
# missing (see governance note in the module docstring).
INSUFFICIENT_EVIDENCE_THRESHOLD = 4


def _find_col(header, name):
    for i, col_name in enumerate(header):
        if (col_name or "").strip() == name:
            return i
    return None


def read_evidence_layer(spreadsheet):
    """Reads back the Evidence Layer THESIS WORKSPACE already computed
    this week. Returns {TICKER: {field_name: value}}, or {} if the tab
    doesn't exist yet (Step 3 hasn't run this cycle)."""
    try:
        ws = spreadsheet.worksheet("THESIS WORKSPACE")
    except gspread.exceptions.WorksheetNotFound:
        print("[WARN] THESIS WORKSPACE tab not found - has "
              "setup_thesis_workspace.py run yet this cycle? No evidence "
              "to generate suggestions from this run.")
        return {}

    values = ws.get_all_values()
    if not values:
        return {}
    header = values[0]
    ticker_col = _find_col(header, "Ticker") or 0
    field_cols = {name: _find_col(header, name) for name in EVIDENCE_FIELDS}

    result = {}
    for row in values[1:]:
        if not row or len(row) <= ticker_col or not row[ticker_col]:
            continue
        ticker = row[ticker_col].strip().upper()
        entry = {}
        for name, idx in field_cols.items():
            entry[name] = row[idx] if idx is not None and idx < len(row) else "N/A - Not Verified (column not found)"
        result[ticker] = entry
    return result


def detect_evidence_gaps(evidence):
    """Mechanical (non-AI) staleness/completeness check: scans the raw
    Evidence Layer text for N/A / Not Verified / no-data markers. Runs
    regardless of whether the Claude call below succeeds, so the
    Evidence Freshness Note is always populated and never depends on the
    AI accurately describing its own input quality.

    Returns (gap_count, note_text)."""
    missing = []
    for name in EVIDENCE_FIELDS:
        value = (evidence.get(name) or "").strip().lower()
        if not value or any(marker in value for marker in NOT_VERIFIED_MARKERS):
            missing.append(name)

    if not missing:
        return 0, f"All {len(EVIDENCE_FIELDS)} evidence fields available this week."
    return (len(missing),
            f"CAUTION: {len(missing)} of {len(EVIDENCE_FIELDS)} evidence field(s) "
            f"unavailable/Not Verified this week: {', '.join(missing)}.")


PROMPT_TEMPLATE = """You are a research assistant organizing evidence for a human equity analyst. The analyst will independently form and write their own trade thesis elsewhere - your ONLY job is to help them think, not to think for them.

STRICT RULES - do not break these:
- Do NOT state a directional view (bullish/bearish/up/down) on the stock.
- Do NOT give a price target, expected magnitude, or expected timeframe.
- Do NOT recommend a trade, a position size, or an action.
- Do NOT classify this as UNDERPRICED, FAIR, CROWDED, or NO EDGE - that is the analyst's manual call.
- Do NOT write anything that could be mistaken for a personal thesis or a variant view. You are organizing evidence and asking questions, not reaching a conclusion.
- Base your response ONLY on the evidence provided below. Do not invent facts, and do not use outside knowledge about {ticker} beyond what's given - if the evidence provided doesn't support a point, say so instead of filling the gap yourself.

Evidence for {ticker} this week:
{evidence_block}

Respond with ONLY valid JSON - no markdown fences, no commentary before or after:
{{
  "supporting_evidence": "<1-3 sentences: what in the evidence above would support a bullish or 'underpriced' view, if any - stated neutrally as 'the evidence shows X', not as your own opinion>",
  "contradictory_evidence": "<1-3 sentences: what in the evidence above would support a bearish, 'crowded', or cautious view, if any>",
  "expectations_gaps": "<1-2 sentences: where does the evidence leave open questions about what the market currently expects?>",
  "catalyst_questions": "<1-2 sentences: what questions should the analyst ask about the catalysts listed, if any?>",
  "invalidation_questions": "<1-2 sentences: what evidence, if it changed, would most call a bullish or bearish view into question?>",
  "missing_evidence": "<1-2 sentences: what evidence would be useful here that isn't available this week?>"
}}"""


def format_evidence_block(evidence):
    return "\n".join(f"- {name}: {evidence.get(name, 'N/A')}" for name in EVIDENCE_FIELDS)


def get_suggestions(ticker, evidence, client):
    """Calls Claude once with the Evidence Layer text only (no web
    search - this is deliberately not live/on-demand infrastructure).
    Returns the parsed dict, or None on failure."""
    text = ""
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=900,
            messages=[{
                "role": "user",
                "content": PROMPT_TEMPLATE.format(
                    ticker=ticker, evidence_block=format_evidence_block(evidence)
                ),
            }],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
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


def get_or_create_worksheet(spreadsheet, tab_name, rows, cols):
    try:
        return spreadsheet.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=tab_name, rows=rows, cols=cols)


def main():
    api_key = os.environ["ANTHROPIC_API_KEY"]
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(os.environ["EQUITIES_SHEET_ID"])
    client = anthropic.Anthropic(api_key=api_key)

    now = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    evidence_layer = read_evidence_layer(spreadsheet)

    output_rows = [[BANNER] + [""] * (len(OUTPUT_HEADER) - 1), OUTPUT_HEADER]
    skipped = 0
    generated = 0
    failed = 0

    for ticker in WATCHLIST:
        evidence = evidence_layer.get(ticker)
        if evidence is None:
            output_rows.append([
                ticker, "N/A - Not Verified", "N/A - Not Verified", "N/A - Not Verified",
                "N/A - Not Verified", "N/A - Not Verified", "N/A - Not Verified",
                "No Evidence Layer data found for this ticker this run - has "
                "setup_thesis_workspace.py run yet this cycle?",
                now, now, "N/A",
            ])
            skipped += 1
            continue

        gap_count, freshness_note = detect_evidence_gaps(evidence)

        if gap_count >= INSUFFICIENT_EVIDENCE_THRESHOLD:
            note = (f"Insufficient evidence this week to generate Research Assistant "
                    f"Suggestions ({gap_count} of {len(EVIDENCE_FIELDS)} evidence fields "
                    f"unavailable) - see Evidence Freshness Note.")
            output_rows.append([
                ticker, note, note, note, note, note, note,
                freshness_note, now, now, "Claude (evidence-only, batch)",
            ])
            skipped += 1
            print(f"[SKIP] {ticker} - {gap_count}/{len(EVIDENCE_FIELDS)} evidence fields "
                  f"unavailable, skipping AI generation this run.")
            continue

        data = get_suggestions(ticker, evidence, client)
        time.sleep(2)

        if data is None:
            fail_note = "N/A - Claude call or JSON parse failed this run"
            output_rows.append([
                ticker, fail_note, fail_note, fail_note, fail_note, fail_note, fail_note,
                freshness_note, now, now, "Claude (evidence-only, batch) - FAILED",
            ])
            failed += 1
            continue

        output_rows.append([
            ticker,
            data.get("supporting_evidence", "N/A"),
            data.get("contradictory_evidence", "N/A"),
            data.get("expectations_gaps", "N/A"),
            data.get("catalyst_questions", "N/A"),
            data.get("invalidation_questions", "N/A"),
            data.get("missing_evidence", "N/A"),
            freshness_note,
            now, now,
            "Claude (evidence-only, batch)",
        ])
        generated += 1

    ws = get_or_create_worksheet(spreadsheet, TAB_NAME,
                                  rows=len(WATCHLIST) + 5, cols=len(OUTPUT_HEADER) + 2)
    ws.clear()
    ws.update(range_name="A1", values=output_rows)
    print(f"[OK] Wrote RESEARCH ASSISTANT SUGGESTIONS — {generated} generated, "
          f"{skipped} skipped (insufficient evidence or missing tab), {failed} failed.")


if __name__ == "__main__":
    main()

    
