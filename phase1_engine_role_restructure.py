#!/usr/bin/env python3
"""
F4P Equities & Options Hub — PHASE 1: Architecture & Classification
=====================================================================
Implements CLAUDE_BUILD_INSTRUCTIONS_PHASES_1_AND_2_GWL_F4P_HUB.docx, Phase 1 only.

What this does:
  1. Adds an "ENGINE / ROLE" column to EQUITIES HUB DATA (new column — the
     existing "Tag" column is left untouched, per "do not delete existing data").
  2. Classifies every existing indicator (1-16, 18) into one of the 7 engine
     categories from the build doc.
  3. Computes, per ticker, a Fundamental/Endogenous Score using ONLY
     FUNDAMENTAL-tagged indicators (company operating condition), and a
     qualitative Confirmation state (POSITIVE / MIXED / NEGATIVE) from
     CONFIRMATION-tagged indicators. Neither is a "universal" score — they
     are kept as separate fields, never summed together.
  4. Rebuilds STRATEGY DASHBOARD with the separated-engine header row the
     build doc specifies, replacing the single "Total Score" column.
  5. Does NOT touch EQUITY RANKINGS (not named in the build doc — left as a
     legacy/audit view of the old universal-score methodology; see report).
  6. Does NOT invent any technical/ITPM logic, IV Rank, options-strategy
     logic, or portfolio/position-sizing formulas (explicitly out of scope
     for Phase 1-2).
  7. Writes "N/A" rather than fabricating wherever an engine has no data yet
     (Expectations, Risk/Reward, Portfolio Fit).
  8. Market Environment: reads the five Phase 2 environment tabs (SECTOR &
     MACRO OVERLAY, RATES ENVIRONMENT, CREDIT ENVIRONMENT, EQUITY MARKET
     REGIME, VIX ENVIRONMENT) and writes a qualitative synthesis
     (Risk-On / Risk-Off / Mixed) — never a number, per the same
     no-universal-score rule as Fundamental vs. Confirmation.

What this does NOT do (by design — see build doc "STOP" instructions):
  - Does not build the Phase 2 Market Environment engine.
  - Does not auto-generate a "Decision" (EXECUTE/WAIT/MONITOR/PASS) — that
    field is left manual for the Sunday review, per the build doc's rule
    that EXECUTE must never be generated from a number alone.

Run via GitHub Actions (workflow_dispatch or as a new step in
f4p_equities_master_update.yml), same auth pattern as the existing pipeline.
"""

import gspread
from google.oauth2.service_account import Credentials
import json
import os
import sys
from datetime import datetime, timezone

SHEET_ID = os.environ["EQUITIES_SHEET_ID"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HUB_DATA_TAB = "EQUITIES HUB DATA"
STRATEGY_DASHBOARD_TAB = "STRATEGY DASHBOARD"

# ---------------------------------------------------------------------------
# ENGINE / ROLE classification (Phase 1, Section 1)
# ---------------------------------------------------------------------------
# Keyed by Indicator # as it appears in EQUITIES HUB DATA.
#
# Sourced directly from the build doc's explicit instructions:
#   - "Keep appropriate company variables such as EPS, revenue, estimate
#      revisions, margins, free cash flow/cash conversion, balance-sheet
#      quality, forward guidance..." -> FUNDAMENTAL (1,2,3,4,5,6,8,9)
#   - "REMOVE...FROM THE FUNDAMENTAL SCORE ONLY": Put/Call (15) ->
#      CONFIRMATION, Insider Activity (16) -> CONTEXT, Institutional
#      Holdings (14) -> CONFIRMATION, Relative Strength (11) ->
#      CONFIRMATION (doc says "MARKET CONFIRMATION"), IV/HV (10) ->
#      VOLATILITY / OPTIONS
#   - "Existing technical placeholders must be clearly labelled
#      UNVERIFIED/PLACEHOLDER" -> Price Momentum Pulse (18) -> MANUAL/UNVERIFIED
#
# CONFIRMED BY COACH 2026-09-07 (previously flagged [ASSUMPTION]):
#   - Peer Relative Strength (12): CONFIRMATION — same character as
#     indicator 11, confirmed as-is.
#   - Catalyst Pipeline (7): EXPECTATIONS — forward-looking event list,
#     belongs with the forward-expectations layer, not CONTEXT.
#   - Sector Money-Flow / MFI (13): Coach's rule was "CONFIRMATION if
#     sector capital-flow data; MANUAL/UNVERIFIED if technical MFI index."
#     Checked f4p_equities_weekly_update.py: get_or_fetch_mfi() pulls
#     Alpha Vantage's MFI technical function on the sector ETF — that's
#     the technical-index case, so MANUAL/UNVERIFIED (alongside 18).
ENGINE_ROLE_MAP = {
    1:  "FUNDAMENTAL",           # EPS Surprise
    2:  "FUNDAMENTAL",           # Revenue Surprise
    3:  "FUNDAMENTAL",           # Gross Margin Trend (QoQ)
    4:  "FUNDAMENTAL",           # Analyst Estimate Revisions (90-day)
    5:  "FUNDAMENTAL",           # Operating Margin Trend (YoY)
    6:  "FUNDAMENTAL",           # Forward Guidance
    7:  "EXPECTATIONS",          # Catalyst Pipeline            [CONFIRMED]
    8:  "FUNDAMENTAL",           # Free Cash Flow Margin
    9:  "FUNDAMENTAL",           # Balance Sheet Quality
    10: "VOLATILITY / OPTIONS",  # IV / Historical Vol Spread
    11: "CONFIRMATION",          # Relative Strength vs SPY
    12: "CONFIRMATION",          # Peer Relative Strength       [CONFIRMED]
    13: "MANUAL / UNVERIFIED",   # Sector Money-Flow (MFI-14)   [CONFIRMED — technical MFI]
    14: "CONFIRMATION",          # Institutional Holdings Sentiment
    15: "CONFIRMATION",          # Put/Call Ratio
    16: "CONTEXT",               # Insider Activity
    18: "MANUAL / UNVERIFIED",   # Price Momentum Pulse (Technical-Placeholder)
    # 17 (IV Rank) does not exist yet in the Hub — no entry needed.
}

FUNDAMENTAL_INDICATORS = {k for k, v in ENGINE_ROLE_MAP.items() if v == "FUNDAMENTAL"}
CONFIRMATION_INDICATORS = {k for k, v in ENGINE_ROLE_MAP.items() if v == "CONFIRMATION"}


# Thresholds — reused from the FX Hub's existing "+/-3 point real shift"
# convention for Fundamental Predisposition, scaled down for Confirmation's
# narrower 4-indicator range. Both are explicit, tunable constants, not
# invented technical rules. FLAG FOR COACH REVIEW.
FUNDAMENTAL_BULLISH_THRESHOLD = 3
FUNDAMENTAL_BEARISH_THRESHOLD = -3
CONFIRMATION_POSITIVE_THRESHOLD = 2
CONFIRMATION_NEGATIVE_THRESHOLD = -2


def get_client():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_json, scopes=SCOPES)
    return gspread.authorize(creds)


def col_index_by_header(header_row, name):
    """Find a column's 1-indexed position by exact header text.
    Fails loudly (raises) rather than silently misaligning columns —
    matches the qualitative-script fix from 2026-08-29."""
    try:
        return header_row.index(name) + 1
    except ValueError:
        raise RuntimeError(
            f"[FAIL] Expected header '{name}' not found in {HUB_DATA_TAB}. "
            f"Found headers: {header_row}"
        )


def classify_hub_data(ws):
    """Phase 1, Section 1: add ENGINE / ROLE column to EQUITIES HUB DATA."""
    all_values = ws.get_all_values()
    header = all_values[0]

    ticker_col = col_index_by_header(header, "Ticker")
    indicator_num_col = col_index_by_header(header, "Indicator #")
    score_col = col_index_by_header(header, "F4P Score")

    if "ENGINE / ROLE" in header:
        engine_col = header.index("ENGINE / ROLE") + 1
        print("[INFO] ENGINE / ROLE column already present — updating in place.")
    else:
        engine_col = len(header) + 1
        if ws.col_count < engine_col:
            ws.resize(rows=ws.row_count, cols=engine_col)
            print(f"[INFO] Grid only had {ws.col_count} columns — resized to {engine_col} "
                  f"to make room for ENGINE / ROLE.")
        ws.update_cell(1, engine_col, "ENGINE / ROLE")
        print(f"[INFO] Added ENGINE / ROLE column at position {engine_col}.")

    updates = []
    missing_indicators = set()
    per_ticker_rows = {}  # ticker -> {indicator_num: (row_idx, score)}

    for row_idx, row in enumerate(all_values[1:], start=2):
        if not row or not row[0].strip():
            continue
        ticker = row[ticker_col - 1].strip()
        try:
            indicator_num = int(row[indicator_num_col - 1])
        except (ValueError, IndexError):
            continue

        role = ENGINE_ROLE_MAP.get(indicator_num)
        if role is None:
            missing_indicators.add(indicator_num)
            role = "N/A - Not Verified"

        updates.append({"range": gspread.utils.rowcol_to_a1(row_idx, engine_col),
                         "values": [[role]]})

        try:
            score = float(row[score_col - 1])
        except (ValueError, IndexError):
            score = None
        per_ticker_rows.setdefault(ticker, {})[indicator_num] = score

    if updates:
        ws.batch_update(updates, value_input_option="RAW")
        print(f"[INFO] Classified {len(updates)} indicator rows.")

    if missing_indicators:
        print(f"[SUSPICIOUS EMPTY] Indicator #s with no ENGINE/ROLE mapping: "
              f"{sorted(missing_indicators)} — written as 'N/A - Not Verified'. "
              f"Add these to ENGINE_ROLE_MAP once classified.")

    return per_ticker_rows


def compute_engine_summaries(per_ticker_rows):
    """Phase 1, Sections 2-3: per-ticker Fundamental Score + Confirmation state.
    These are two SEPARATE fields — never summed into one number."""
    summaries = {}
    for ticker, scores in per_ticker_rows.items():
        fundamental_scores = [scores[i] for i in FUNDAMENTAL_INDICATORS
                               if i in scores and scores[i] is not None]
        confirmation_scores = [scores[i] for i in CONFIRMATION_INDICATORS
                                if i in scores and scores[i] is not None]

        if fundamental_scores:
            fund_total = sum(fundamental_scores)
            if fund_total >= FUNDAMENTAL_BULLISH_THRESHOLD:
                fund_label = "Bullish"
            elif fund_total <= FUNDAMENTAL_BEARISH_THRESHOLD:
                fund_label = "Bearish"
            else:
                fund_label = "Neutral"
            fundamental_predisposition = f"{fund_total:+.0f} ({fund_label})"
        else:
            fundamental_predisposition = "N/A - Not Verified"

        if confirmation_scores:
            conf_total = sum(confirmation_scores)
            if conf_total >= CONFIRMATION_POSITIVE_THRESHOLD:
                conf_state = "POSITIVE"
            elif conf_total <= CONFIRMATION_NEGATIVE_THRESHOLD:
                conf_state = "NEGATIVE"
            else:
                conf_state = "MIXED"
        else:
            conf_state = "N/A - Not Verified"

        summaries[ticker] = {
            "fundamental_predisposition": fundamental_predisposition,
            "confirmation_state": conf_state,
        }
    return summaries


def compute_market_environment_summary(sh):
    """Phase 2 close-out: synthesizes the five environment tabs into one
    qualitative read for STRATEGY DASHBOARD's Market Environment column.
    Deliberately qualitative, not a number — per the build doc's rule
    against creating a universal score across different analytical
    engines, this never gets summed with Fundamental Predisposition or
    anything else; it's its own field, same as Confirmation is.

    Classification rule (this specific synthesis logic is new here, not
    named anywhere in the build doc — flagged for Coach/Glenise review
    same as the other judgment calls made during this build):
      - Equity regime: Bull if both S&P and Nasdaq read Bull, Bear if
        both read Bear, Mixed if they disagree.
      - Vol: Calm if VIX regime is Low/Normal, Stressed if Elevated/High.
      - Credit: Widening if the HY-IG spread moved wider on both WoW and
        MoM (>+0.05, i.e. >5bps, to filter noise), Narrowing if both
        moved tighter by the same margin, Stable otherwise.
      - Curve: Normal if 2s10s > 0, Inverted otherwise (the classic
        recession-signal reading).
      - Overall: Risk-On only if equity=Bull AND vol=Calm AND credit is
        not Widening AND curve is Normal. Risk-Off only if equity=Bear
        AND vol=Stressed AND (credit Widening OR curve Inverted).
        Everything else reads Mixed/Transitional — which is the most
        common real-world case, not a fallback for missing data.

    Fails soft (returns the old placeholder text) if any of the five
    tabs is missing or unreadable, rather than crashing the whole
    dashboard rebuild over one optional read.
    """
    try:
        def col(header, name):
            return header.index(name)

        def get_rows(tab_name):
            values = sh.worksheet(tab_name).get_all_values()
            return values[0], values[1:]

        regime_header, regime_rows = get_rows("EQUITY MARKET REGIME")
        regimes = [r[col(regime_header, "Regime")] for r in regime_rows if r and r[0].strip()]
        if len(regimes) >= 2 and all(r == "Bull" for r in regimes):
            equity_signal = "Bull"
        elif len(regimes) >= 2 and all(r == "Bear" for r in regimes):
            equity_signal = "Bear"
        else:
            equity_signal = "Mixed"

        vix_header, vix_rows = get_rows("VIX ENVIRONMENT")
        vix_row = vix_rows[0]
        vix_current = vix_row[col(vix_header, "Current")]
        vol_regime = vix_row[col(vix_header, "Vol Regime")]
        vol_signal = "Calm" if vol_regime in ("Low", "Normal") else "Stressed"

        credit_header, credit_rows = get_rows("CREDIT ENVIRONMENT")
        stress_row = next(r for r in credit_rows if r[0].startswith("HY minus IG"))
        wow = float(stress_row[col(credit_header, "WoW \u0394")])
        mom = float(stress_row[col(credit_header, "MoM \u0394")])
        if wow > 0.05 and mom > 0.05:
            credit_signal = "Widening"
        elif wow < -0.05 and mom < -0.05:
            credit_signal = "Narrowing"
        else:
            credit_signal = "Stable"

        rates_header, rates_rows = get_rows("RATES ENVIRONMENT")
        spread_row = next(r for r in rates_rows if r[0].startswith("2s10s"))
        spread_current = float(spread_row[col(rates_header, "Current")])
        curve_signal = "Normal" if spread_current > 0 else "Inverted"

        if equity_signal == "Bull" and vol_signal == "Calm" and credit_signal != "Widening" and curve_signal == "Normal":
            overall = "Risk-On"
        elif equity_signal == "Bear" and vol_signal == "Stressed" and (credit_signal == "Widening" or curve_signal == "Inverted"):
            overall = "Risk-Off"
        else:
            overall = "Mixed / Transitional"

        return (f"{overall} \u2014 Equity regime: {equity_signal}; "
                f"Vol: {vol_signal} (VIX {vix_current}); "
                f"Credit: {credit_signal}; Curve: {curve_signal} "
                f"(2s10s {spread_current:+.2f})")
    except Exception as e:
        print(f"[SUSPICIOUS EMPTY] Market Environment summary could not be computed: {e}. "
              f"Writing placeholder instead of a guess.")
        return "N/A - Not Verified (Market Environment tabs missing or unreadable)"


def rebuild_strategy_dashboard(ws_dashboard, ws_hubdata, summaries, market_environment_text):
    """Phase 1, Section 5-7: separated-engine Strategy Dashboard.
    Preserves existing Catalyst / Instrument content where it maps cleanly;
    everything not yet built is written as an explicit N/A, never fabricated."""
    NEW_HEADERS = [
        "Ticker", "Market Environment", "Fundamental Predisposition",
        "Expectations", "Confirmation", "Catalyst", "Capital Deployment",
        "Volatility", "Instrument", "Risk/Reward", "Portfolio Fit",
        "Decision", "Decision Reason",
    ]

    old_values = ws_dashboard.get_all_values()
    old_header = old_values[0] if old_values else []

    def old_col(name):
        return old_header.index(name) if name in old_header else None

    catalyst_i = old_col("Catalyst")
    instrument_i = old_col("Options Strategy Idea")
    ticker_i = old_col("Ticker")

    old_by_ticker = {}
    for row in old_values[1:]:
        if not row or not row[0].strip():
            continue
        t = row[ticker_i] if ticker_i is not None else row[0]
        old_by_ticker[t] = row

    hub_values = ws_hubdata.get_all_values()
    hub_header = hub_values[0]
    h_ticker = hub_header.index("Ticker")
    h_indnum = hub_header.index("Indicator #")
    h_current = hub_header.index("Current Value")
    h_analysis = hub_header.index("Institutional Analysis")

    iv_hv_by_ticker = {}
    for row in hub_values[1:]:
        if not row or not row[0].strip():
            continue
        try:
            if int(row[h_indnum]) == 10:
                iv_hv_by_ticker[row[h_ticker]] = row[h_analysis] or row[h_current]
        except (ValueError, IndexError):
            continue

    new_rows = [NEW_HEADERS]
    for ticker in sorted(old_by_ticker.keys()):
        old_row = old_by_ticker[ticker]
        summary = summaries.get(ticker, {
            "fundamental_predisposition": "N/A - Not Verified",
            "confirmation_state": "N/A - Not Verified",
        })
        catalyst = old_row[catalyst_i] if catalyst_i is not None and catalyst_i < len(old_row) else "N/A"
        instrument = old_row[instrument_i] if instrument_i is not None and instrument_i < len(old_row) else "N/A"
        volatility = iv_hv_by_ticker.get(ticker, "N/A - Not Verified")

        new_rows.append([
            ticker,
            market_environment_text,
            summary["fundamental_predisposition"],
            "N/A - no Expectations-layer indicator built yet",
            summary["confirmation_state"],
            catalyst,
            "",  # Capital Deployment — manual: READY / WAIT / BROKEN
            volatility,
            instrument,
            "N/A - not yet built",   # Risk/Reward
            "N/A - not yet built",   # Portfolio Fit (explicitly out of scope)
            "",  # Decision — manual: EXECUTE / WAIT / MONITOR / PASS
            "",  # Decision Reason — manual
        ])

    needed_cols = len(NEW_HEADERS)
    if ws_dashboard.col_count < needed_cols:
        ws_dashboard.resize(rows=max(ws_dashboard.row_count, len(new_rows)), cols=needed_cols)
        print(f"[INFO] STRATEGY DASHBOARD grid only had {ws_dashboard.col_count} columns "
              f"— resized to {needed_cols} for the new headers.")
    ws_dashboard.clear()
    ws_dashboard.update("A1", new_rows, value_input_option="RAW")
    print(f"[INFO] Rebuilt {STRATEGY_DASHBOARD_TAB} with {len(new_rows)-1} tickers, "
          f"{len(NEW_HEADERS)} columns.")


def main():
    report = {"timestamp": datetime.now(timezone.utc).isoformat(),
               "sections_run": [], "sections_failed": []}

    gc = get_client()
    sh = gc.open_by_key(SHEET_ID)

    try:
        ws_hubdata = sh.worksheet(HUB_DATA_TAB)
        per_ticker_rows = classify_hub_data(ws_hubdata)
        report["sections_run"].append("ENGINE/ROLE classification")
    except Exception as e:
        print(f"[FAIL] ENGINE/ROLE classification: {e}")
        report["sections_failed"].append(f"ENGINE/ROLE classification: {e}")
        sys.exit(1)  # downstream sections depend on this — stop here

    try:
        summaries = compute_engine_summaries(per_ticker_rows)
        report["sections_run"].append("Fundamental Score + Confirmation state")
    except Exception as e:
        print(f"[FAIL] Engine summaries: {e}")
        report["sections_failed"].append(f"Engine summaries: {e}")
        sys.exit(1)

    try:
        market_environment_text = compute_market_environment_summary(sh)
        report["sections_run"].append("Market Environment summary (Phase 2 close-out)")
    except Exception as e:
        # compute_market_environment_summary already fails soft internally,
        # so reaching here would be unexpected — but never let this one
        # optional field take down the whole dashboard rebuild.
        print(f"[FAIL] Market Environment summary: {e}")
        report["sections_failed"].append(f"Market Environment summary: {e}")
        market_environment_text = "N/A - Not Verified (Market Environment tabs missing or unreadable)"

    try:
        ws_dashboard = sh.worksheet(STRATEGY_DASHBOARD_TAB)
        rebuild_strategy_dashboard(ws_dashboard, ws_hubdata, summaries, market_environment_text)
        report["sections_run"].append("STRATEGY DASHBOARD rebuild")
    except Exception as e:
        print(f"[FAIL] STRATEGY DASHBOARD rebuild: {e}")
        report["sections_failed"].append(f"STRATEGY DASHBOARD rebuild: {e}")
        sys.exit(1)

    print("\n=== PHASE 1 RUN SUMMARY ===")
    print(json.dumps(report, indent=2))
    print("\nEQUITY RANKINGS tab was NOT modified (not named in the build doc — "
          "left as a legacy view of the prior universal-score methodology).")


if __name__ == "__main__":
    main()

    

    
