"""
setup_thesis_workspace.py

Phase 3 of the F4P Equities & Options pipeline: builds/refreshes the
THESIS WORKSPACE tab - one row per ticker, combining an Evidence layer
(pulled automatically from data already in the Hub) with a Human Thesis
layer (entered manually, never touched by this script beyond carrying
forward what's already there).

GOVERNANCE - these rules are non-negotiable and were explicitly approved
by Glenise. Any future change to this script must preserve them:

  - The 10 Human Thesis columns (My Thesis, Market Appears to Believe,
    My Variant View, Catalyst (Thesis), Expected Direction, Expected
    Magnitude, Expected Horizon, Invalidation, Contradictory Evidence,
    Thesis Notes) are blank by default and populated ONLY by a human,
    directly in the Sheet. This script never writes a value into them -
    it only reads back whatever a human already entered and carries it
    forward across this rebuild, using the same preserve-across-rebuild
    pattern already used for Capital Deployment/Decision in STRATEGY
    DASHBOARD (see phase1_engine_role_restructure.py's carry_forward()),
    except matched by COLUMN NAME rather than position (see
    read_preserved_by_name()) so a future column addition/rename/
    reorder can never misalign or silently drop a human's entries.
  - Expectations Classification (UNDERPRICED / FAIR / CROWDED / NO EDGE)
    is MANUAL. This script never calculates or selects it - it is
    preserved the same way as the Human Thesis fields.
  - THESIS DOCUMENTATION GATE (Thesis Documentation Status column): a
    purely mechanical completeness check - see
    derive_thesis_documentation_status(). It reads what a human has
    already written and reports COMPLETE / INCOMPLETE / NO EDGE based
    only on whether required fields are filled in and what
    classification was manually selected. It NEVER assesses whether a
    thesis is correct or well-reasoned, NEVER generates a thesis or
    conclusion, NEVER overwrites a human entry, and NEVER authorizes
    EXECUTE - Capital Deployment/Decision in STRATEGY DASHBOARD remain
    entirely separate, human-only fields this script never touches.
    Named "Thesis Documentation Status", not "Thesis Quality", because
    COMPLETE means the required fields are documented, not that the
    thesis has been independently validated.
  - Valuation Context is raw Expectations evidence only - the published
    trailing/forward P/E from Alpha Vantage COMPANY_OVERVIEW, with
    source and freshness shown. No thresholds, no cheap/expensive
    scoring, no fair-value estimate, no proprietary valuation model, and
    no automatic UNDERPRICED/FAIR/CROWDED classification is derived from
    it anywhere in this script.
  - Research Assistant Suggestions (weekly-batch AI commentary) are a
    SEPARATE, later Phase 3 step - not built here. This script only
    produces the Evidence layer, preserves the Human Thesis layer, and
    mechanically checks its documentation completeness.

THESIS DOCUMENTATION GATE - governance rules, verbatim as approved:
  1. INCOMPLETE: one or more required Human Thesis fields are missing,
     Expectations Classification has not been selected, or the
     classification remains Not Verified.
  2. COMPLETE: all required fields are documented AND the analyst has
     manually selected UNDERPRICED, FAIR or CROWDED.
  3. NO EDGE: the analyst explicitly selects NO EDGE and provides a
     written rationale (read from My Thesis). Does not require an
     invented Expected Magnitude, Expected Horizon or Catalyst when the
     analyst has concluded there is no identifiable opportunity.
  Required for COMPLETE: My Thesis, Market Appears to Believe, My
  Variant View, Catalyst (Thesis) (or explicitly "No Identified
  Catalyst"), Expected Direction, Expected Magnitude, Expected Horizon,
  Invalidation, Contradictory Evidence, Expectations Classification. A
  documented "Not Verified" entry is permitted for Market Appears to
  Believe when evidence is unavailable, but that limitation is surfaced
  in the Documentation Gate Notes column rather than hidden.

IMPORTANT - RUN ORDER: this script must run AFTER both:
  1. f4p_equities_weekly_update.py   - writes indicators 1 (EPS), 2
                                        (Revenue), 4 (Estimate Revisions),
                                        11 (Relative Strength) to
                                        EQUITIES HUB DATA.
  2. f4p_equities_qualitative_update.py - writes the structured
                                        THESIS WORKSPACE CATALYSTS tab
                                        this script reads from.
If either hasn't run yet this cycle, the affected Evidence field will
read as "N/A - Not Verified" rather than showing stale data from a
previous week - it will not silently show old numbers as current.

NOTE ON COLUMN MATCHING: EQUITIES HUB DATA's columns are located by
header name (see get_hub_data_indicator()) rather than a fixed position,
since this script reads a table it doesn't own. The first live run
should be checked by hand against the actual Sheet to confirm the
Evidence columns are populating correctly - same discipline as
everywhere else in this pipeline: verify against real data, don't trust
a green checkmark.

Required secrets:
  GOOGLE_CREDENTIALS     - same service account used by the rest of the Hub
  EQUITIES_SHEET_ID      - F4P Equities & Options Scorecard file ID
  ALPHA_VANTAGE_API_KEY  - premium tier, same key used elsewhere in this pipeline
"""

import os
import json
import time
import requests
import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

WATCHLIST = ["NVDA", "AAPL", "AMZN", "GOOGL", "TSLA", "META", "COIN", "NFLX", "QQQ"]

HEADER = [
    "Ticker",
    # --- Evidence (auto-populated, read-only for humans) ---
    "Consensus EPS (Actual vs Estimate)",
    "Consensus Revenue (Actual vs Estimate)",
    "Estimate Revisions (90-day)",
    "Recent Repricing (21-day vs SPY)",
    "Valuation Context",
    "Catalyst Summary",
    # --- Human Thesis (manual only - never written by this script) ---
    "My Thesis",
    "Market Appears to Believe",
    "My Variant View",
    "Catalyst (Thesis)",
    "Expected Direction",
    "Expected Magnitude",
    "Expected Horizon",
    "Invalidation",
    "Contradictory Evidence",
    "Thesis Notes",
    # --- Manual classification (human-entered) ---
    "Expectations Classification",
    # --- Thesis Documentation Gate output (COMPUTED every run - never
    #     carried forward, never hand-edited) ---
    "Thesis Documentation Status",
    "Documentation Gate Notes",
]

# The Human Thesis columns plus the one manual classification field -
# these are the only columns read back from the prior run and carried
# forward. Matched by NAME (see read_preserved_by_name()), not position,
# so this list is also the single source of truth for what "human-owned"
# means. Thesis Documentation Status and Documentation Gate Notes are
# deliberately excluded - they're gate OUTPUT, recomputed fresh every run.
HUMAN_THESIS_FIELDS = [
    "My Thesis",
    "Market Appears to Believe",
    "My Variant View",
    "Catalyst (Thesis)",
    "Expected Direction",
    "Expected Magnitude",
    "Expected Horizon",
    "Invalidation",
    "Contradictory Evidence",
    "Thesis Notes",
]
PRESERVED_FIELDS = HUMAN_THESIS_FIELDS + ["Expectations Classification"]

# Required for a COMPLETE Thesis Documentation Status (Expectations
# Classification is checked separately in derive_thesis_documentation_status).
REQUIRED_FOR_COMPLETE = [
    "My Thesis",
    "Market Appears to Believe",
    "My Variant View",
    "Catalyst (Thesis)",
    "Expected Direction",
    "Expected Magnitude",
    "Expected Horizon",
    "Invalidation",
    "Contradictory Evidence",
]

VALID_CLASSIFICATIONS = {"UNDERPRICED", "FAIR", "CROWDED", "NO EDGE"}
NOT_VERIFIED_VALUES = {"not verified", "n/a - not verified", "n/a", "na"}


def _is_blank(value):
    return not value or not str(value).strip()


def _is_not_verified(value):
    return str(value).strip().lower() in NOT_VERIFIED_VALUES


def derive_thesis_documentation_status(fields):
    """The Thesis Documentation Gate - purely mechanical. Reads back what
    a human has already written in `fields` (a dict keyed by the exact
    Human Thesis column names, plus "Expectations Classification") and
    reports completeness only. See the module docstring's governance
    section for the full rules and why this is not called a "quality"
    gate. Returns (status, notes) - notes is always populated when
    status isn't a clean COMPLETE, so a reviewer can see why without
    opening every column."""
    classification_raw = fields.get("Expectations Classification") or ""
    classification = classification_raw.strip().upper()

    if _is_blank(classification_raw):
        return "INCOMPLETE", "Expectations Classification not yet selected."
    if _is_not_verified(classification_raw):
        return ("INCOMPLETE",
                "Expectations Classification is Not Verified - a manual "
                "UNDERPRICED / FAIR / CROWDED / NO EDGE selection is required.")
    if classification not in VALID_CLASSIFICATIONS:
        return ("INCOMPLETE",
                f"Expectations Classification value \"{classification_raw.strip()}\" "
                f"is not one of UNDERPRICED / FAIR / CROWDED / NO EDGE - check for a typo.")

    if classification == "NO EDGE":
        rationale = fields.get("My Thesis") or ""
        if _is_blank(rationale):
            return ("INCOMPLETE",
                    "NO EDGE selected but My Thesis is blank - a written rationale "
                    "for why no edge was identified is required.")
        return ("NO EDGE",
                "Analyst concluded no identifiable opportunity; written rationale "
                "documented in My Thesis. Expected Magnitude/Horizon/Catalyst are "
                "not required for this classification.")

    # UNDERPRICED / FAIR / CROWDED - full documentation required.
    missing = [f for f in REQUIRED_FOR_COMPLETE if _is_blank(fields.get(f))]
    if missing:
        return "INCOMPLETE", "Missing required field(s): " + ", ".join(missing) + "."

    notes = []
    if _is_not_verified(fields.get("Market Appears to Believe")):
        notes.append(
            "Market Appears to Believe marked Not Verified - evidence for "
            "consensus expectations was unavailable or incomplete; documented "
            "and permitted, but the gap is real and should be weighed accordingly."
        )
    if not notes:
        notes.append(f"All required fields documented. Classification: {classification}.")
    return "COMPLETE", " ".join(notes)


# ---------------------------------------------------------------------
# EQUITIES HUB DATA lookups
# ---------------------------------------------------------------------

def _find_col(header, *keywords):
    """Returns the 0-based index of the first header cell containing any
    of the given keywords (case-insensitive substring match), or None."""
    for i, col_name in enumerate(header):
        name = (col_name or "").strip().lower()
        for kw in keywords:
            if kw in name:
                return i
    return None


def _num(value):
    """Formats a raw (unformatted) numeric cell value for display.
    Used so a display-format quirk in EQUITIES HUB DATA can't leak into
    the evidence text - confirmed 2026-09-24: TSLA's Analyst Estimate
    Revisions Current Value cell displays as "$0" (currency format, zero
    decimals) while the real underlying value is ~0.49."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def get_hub_data_indicator(hub_rows, hub_header, ticker, indicator_num, raw_rows=None):
    """Looks up one indicator's row for one ticker in EQUITIES HUB DATA.

    Columns are matched by header name. Confirmed against the live Sheet
    on 2026-09-24, the header is: Ticker | Indicator # | Indicator |
    Current Value | Prior Value | Forecast | Surprise | Release Date |
    Tag | F4P Score | Institutional Analysis | Source/Audit Link |
    ENGINE / ROLE.

    raw_rows (optional) is the same grid read with UNFORMATTED_VALUE, used
    for Current/Prior so cell display formatting can't distort numbers.

    Returns a dict {current, prior, surprise, date, source} of strings,
    or None if this ticker/indicator isn't present this run."""
    if not hub_header:
        return None

    ticker_col = _find_col(hub_header, "ticker")
    if ticker_col is None:
        ticker_col = 0
    indicator_col = _find_col(hub_header, "indicator #", "indicator", "ind #")
    current_col = _find_col(hub_header, "current")
    prior_col = _find_col(hub_header, "prior", "previous")
    surprise_col = _find_col(hub_header, "surprise")
    date_col = _find_col(hub_header, "release date", "date")
    source_col = _find_col(hub_header, "source")

    if indicator_col is None:
        return None

    for idx, row in enumerate(hub_rows):
        if len(row) <= max(ticker_col, indicator_col):
            continue
        if row[ticker_col].strip().upper() != ticker.upper():
            continue
        if str(row[indicator_col]).strip() != str(indicator_num):
            continue

        raw_row = raw_rows[idx] if raw_rows is not None and idx < len(raw_rows) else None

        def cell(col_idx, prefer_raw=False):
            if col_idx is None or len(row) <= col_idx:
                return "N/A"
            if prefer_raw and raw_row is not None and len(raw_row) > col_idx:
                formatted = _num(raw_row[col_idx])
                if formatted is not None:
                    return formatted
            return row[col_idx] or "N/A"

        return {
            "current": cell(current_col, prefer_raw=True),
            "prior": cell(prior_col, prefer_raw=True),
            "surprise": cell(surprise_col),
            "date": cell(date_col),
            "source": cell(source_col),
        }
    return None


def _not_found(label):
    return f"N/A - Not Verified ({label} not found in this week's EQUITIES HUB DATA run)"


def format_actual_vs_estimate(d, metric_label):
    """EPS (#1) and Revenue (#2): Current Value is the reported actual,
    Prior Value is the consensus estimate (confirmed against the live
    Sheet - e.g. NVDA EPS 2.22 actual vs 2.09 estimate = +6.22%)."""
    if d is None:
        return _not_found(metric_label)
    return (f"Actual: {d['current']} | Estimate: {d['prior']} | "
            f"Surprise: {d['surprise']} | Reported: {d['date']} | Source: {d['source']}")


def format_revisions(d):
    """Estimate Revisions (#4): Current Value is today's consensus
    estimate, Prior Value is the estimate 90 days ago - NOT an actual."""
    if d is None:
        return _not_found("Analyst Estimate Revisions")
    return (f"Current estimate: {d['current']} | 90 days ago: {d['prior']} | "
            f"Revision: {d['surprise']} | As of: {d['date']} | Source: {d['source']}")


def format_repricing(d, ticker):
    """Recent Repricing from #11 (Relative Strength vs SPY, 21 trading
    days): Current Value is the ticker's own 21-day return, Prior Value
    is SPY's 21-day return over the same window (the same +0.1% appears
    for every ticker) - NOT a prior reading of the ticker."""
    if d is None:
        return _not_found("Relative Strength vs SPY")
    return (f"{ticker} 21-day return: {d['current']} | SPY 21-day return: {d['prior']} | "
            f"Relative: {d['surprise']} | As of: {d['date']} | Source: {d['source']}")


def format_catalyst_summary(catalyst_rows_for_ticker):
    """Formats the Catalyst Summary evidence field from this ticker's
    rows in the structured THESIS WORKSPACE CATALYSTS tab (written by
    f4p_equities_qualitative_update.py). Expected row shape:
    [Ticker, Type, Description, Expected Date, Status, Source]."""
    if not catalyst_rows_for_ticker:
        return "No specific near-term catalysts found"
    parts = []
    for row in catalyst_rows_for_ticker[:3]:
        row = list(row) + [""] * (6 - len(row))
        _, ctype, desc, expected_date, status, _source = row[:6]
        ctype = ctype or "Other"
        desc = desc or "N/A"
        expected_date = expected_date or "N/A"
        status = status or "Not Verified"
        parts.append(f"[{ctype}] {desc} ({expected_date}, {status})")
    return " | ".join(parts)


# ---------------------------------------------------------------------
# Valuation Context (Alpha Vantage COMPANY_OVERVIEW)
# ---------------------------------------------------------------------

def av_request(params, api_key, retries=3):
    """JSON GET against Alpha Vantage with retry-on-rate-limit, mirroring
    the proven pattern used elsewhere in this pipeline."""
    call_params = dict(params)
    call_params["apikey"] = api_key
    for attempt in range(retries):
        try:
            resp = requests.get("https://www.alphavantage.co/query", params=call_params, timeout=30)
            data = resp.json()
        except Exception as e:
            if attempt == retries - 1:
                print(f"[FAIL] Alpha Vantage request failed after {retries} attempts: {e}")
                return {}
            time.sleep(15)
            continue

        if isinstance(data, dict) and ("Note" in data or "Information" in data):
            if attempt == retries - 1:
                print(f"[FAIL] Alpha Vantage rate-limited after {retries} attempts: "
                      f"{data.get('Note') or data.get('Information')}")
                return {}
            time.sleep(15)
            continue

        return data
    return {}


def clean(value):
    """Alpha Vantage represents missing numeric fields as the literal
    strings 'None' or '-' rather than an absent key or JSON null.
    Normalizes both (and blank strings) to a real None so downstream
    formatting doesn't print the word 'None' as if it were data."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() in ("None", "-", ""):
        return None
    return value


def fetch_valuation_context(ticker, api_key, today):
    """Pulls the published trailing P/E (and forward P/E, if Alpha
    Vantage publishes it directly) from COMPANY_OVERVIEW. Strictly raw
    Expectations evidence - see the governance note in the module
    docstring. No threshold, scoring, or valuation judgment of any kind
    is computed here."""
    data = av_request({"function": "OVERVIEW", "symbol": ticker}, api_key)

    if not data or not data.get("Symbol"):
        return (f"N/A - Not Verified (no COMPANY_OVERVIEW data for {ticker} - "
                f"expected for ETFs like QQQ, which don't have a P/E ratio)")

    trailing_pe = clean(data.get("PERatio"))
    forward_pe = clean(data.get("ForwardPE"))
    as_of = clean(data.get("LatestQuarter")) or "N/A - Not Verified"

    trailing_str = trailing_pe if trailing_pe is not None else "N/A - Not Verified"
    forward_str = forward_pe if forward_pe is not None else "N/A - Not Verified"

    return (f"Trailing P/E: {trailing_str} | Forward P/E: {forward_str} | "
            f"As of fiscal quarter: {as_of} | Fetched: {today} | "
            f"Source: Alpha Vantage COMPANY_OVERVIEW")


# ---------------------------------------------------------------------
# Sheet plumbing
# ---------------------------------------------------------------------

def get_or_create_worksheet(spreadsheet, tab_name, rows, cols):
    try:
        return spreadsheet.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=tab_name, rows=rows, cols=cols)


def read_preserved_by_name(existing_values):
    """Reads back existing manual entries (the Human Thesis fields plus
    Expectations Classification) by column NAME rather than position, so
    a schema change here - a renamed, added, removed or reordered column,
    like this run's addition of the Thesis Documentation Gate columns -
    can never misalign a human's entries into the wrong field or silently
    drop them. Thesis Documentation Status and Documentation Gate Notes
    are deliberately never read back here - they're gate output,
    recomputed fresh from the preserved fields every run, not carried
    forward themselves.

    Returns {TICKER: {field_name: value}}. A field name not present in
    the old header (because it didn't exist yet, or the tab is new)
    simply isn't in that ticker's dict - callers treat that as blank."""
    if not existing_values:
        return {}
    old_header = existing_values[0]
    name_to_idx = {name.strip(): i for i, name in enumerate(old_header) if name}
    ticker_idx = name_to_idx.get("Ticker", 0)

    preserved = {}
    for row in existing_values[1:]:
        if not row or len(row) <= ticker_idx or not row[ticker_idx]:
            continue
        ticker = row[ticker_idx].strip().upper()
        entry = {}
        for field in PRESERVED_FIELDS:
            idx = name_to_idx.get(field)
            if idx is not None and idx < len(row):
                entry[field] = row[idx]
        preserved[ticker] = entry
    return preserved


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(os.environ["EQUITIES_SHEET_ID"])
    api_key = os.environ["ALPHA_VANTAGE_API_KEY"]

    today = time.strftime("%Y-%m-%d")

    hub_ws = spreadsheet.worksheet("EQUITIES HUB DATA")
    hub_values = hub_ws.get_all_values()
    hub_header = hub_values[0] if hub_values else []
    hub_rows = hub_values[1:] if len(hub_values) > 1 else []

    # Second read of the same grid with raw values, so Current/Prior
    # numbers aren't distorted by cell display formatting (see _num()).
    # Falls back to formatted-only if this gspread version won't accept it.
    try:
        raw_values = hub_ws.get_all_values(value_render_option="UNFORMATTED_VALUE")
        raw_rows = raw_values[1:] if len(raw_values) > 1 else []
        if len(raw_rows) != len(hub_rows):
            print("[WARN] Raw and formatted EQUITIES HUB DATA reads differ in "
                  "length - using formatted values only this run.")
            raw_rows = None
    except Exception as e:
        print(f"[WARN] Unformatted read failed ({e}) - using formatted values only.")
        raw_rows = None

    try:
        cat_ws = spreadsheet.worksheet("THESIS WORKSPACE CATALYSTS")
        cat_values = cat_ws.get_all_values()
        cat_rows = cat_values[1:] if len(cat_values) > 1 else []
    except gspread.exceptions.WorksheetNotFound:
        print("[WARN] THESIS WORKSPACE CATALYSTS tab not found - has "
              "f4p_equities_qualitative_update.py run yet? Catalyst "
              "Summary will read N/A for all tickers this run.")
        cat_rows = []

    ws = get_or_create_worksheet(spreadsheet, "THESIS WORKSPACE",
                                  rows=len(WATCHLIST) + 5, cols=len(HEADER) + 2)

    # Preserve-across-rebuild: read whatever a human already entered
    # before this rebuild clears the tab, exactly the same pattern
    # already used for Capital Deployment/Decision in STRATEGY DASHBOARD.
    # Matched by column NAME (read_preserved_by_name()), not position, so
    # this run's new Thesis Documentation Gate columns don't trigger a
    # false "schema changed" alarm or risk misaligning existing entries.
    existing_values = ws.get_all_values()
    preserved = read_preserved_by_name(existing_values)

    output_rows = [HEADER]
    gate_counts = {"COMPLETE": 0, "INCOMPLETE": 0, "NO EDGE": 0}
    for ticker in WATCHLIST:
        eps = format_actual_vs_estimate(
            get_hub_data_indicator(hub_rows, hub_header, ticker, 1, raw_rows), "EPS Surprise")
        revenue = format_actual_vs_estimate(
            get_hub_data_indicator(hub_rows, hub_header, ticker, 2, raw_rows), "Revenue Surprise")
        revisions = format_revisions(
            get_hub_data_indicator(hub_rows, hub_header, ticker, 4, raw_rows))
        repricing = format_repricing(
            get_hub_data_indicator(hub_rows, hub_header, ticker, 11, raw_rows), ticker)

        valuation = fetch_valuation_context(ticker, api_key, today)
        time.sleep(1)  # pacing between the 9 sequential COMPANY_OVERVIEW calls

        ticker_catalysts = [r for r in cat_rows if r and r[0].strip().upper() == ticker.upper()]
        catalyst_summary = format_catalyst_summary(ticker_catalysts)

        entry = preserved.get(ticker.upper(), {})
        thesis_values = [entry.get(name, "") for name in HUMAN_THESIS_FIELDS]
        classification = entry.get("Expectations Classification", "")

        gate_fields = dict(zip(HUMAN_THESIS_FIELDS, thesis_values))
        gate_fields["Expectations Classification"] = classification
        status, notes = derive_thesis_documentation_status(gate_fields)
        gate_counts[status] = gate_counts.get(status, 0) + 1

        output_rows.append([
            ticker, eps, revenue, revisions, repricing, valuation, catalyst_summary,
            *thesis_values, classification, status, notes,
        ])

    ws.clear()
    ws.update(range_name="A1", values=output_rows)
    print(f"[OK] Wrote THESIS WORKSPACE — {len(WATCHLIST)} tickers. "
          f"{len(preserved)} ticker(s) had manual entries preserved across "
          f"this rebuild. Thesis Documentation Gate: {gate_counts}")


if __name__ == "__main__":
    main()

    
