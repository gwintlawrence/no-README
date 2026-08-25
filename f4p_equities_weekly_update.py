"""
f4p_equities_weekly_update.py

Phase 1 of the F4P Equities & Options weekly pipeline.
Pulls a field-verified subset of indicators directly from Alpha Vantage's
REST API and writes flat +/-2 scored rows into the EQUITIES HUB DATA tab.

Covers 7 of the planned 18-indicator framework - all with schemas
confirmed live on 2026-08-25:
  1.  EPS Surprise                    (Company Endogenous)
  2.  Revenue Surprise                (Company Endogenous)
  4.  Analyst Estimate Revisions      (Company Endogenous, 90-day)
  5.  Operating Margin Trend          (Company Endogenous, YoY)
  14. Institutional Holdings Sentiment (Confirmation layer)
  15. Put/Call Ratio                  (Confirmation layer)
  18. Price Momentum Pulse            (Phase 1 stand-in for full Technical
                                        Setup - flagged in the Tag column)

Still outstanding (Phase 3): forward guidance, catalyst pipeline,
insider activity (endpoint returned repeated errors during testing -
needs a fresh look), macro overlay (4 indicators - should cross-reference
the FX Hub's existing tabs rather than re-fetch), sector/peer relative
strength (3 indicators), IV rank, IV/HV spread. Forward guidance and
catalyst pipeline need Claude for qualitative synthesis rather than a
numeric threshold - different mechanism from everything else here.

Required secrets:
  GOOGLE_CREDENTIALS      - same service account used by the FX pipeline
  EQUITIES_SHEET_ID       - F4P Equities & Options Scorecard file ID
  ALPHA_VANTAGE_API_KEY   - premium key
"""

import os
import json
import csv
import io
import time
import requests
import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

WATCHLIST = ["NVDA", "AAPL", "AMZN", "GOOGL", "TSLA", "META", "COIN", "NFLX", "QQQ"]

AV_BASE = "https://www.alphavantage.co/query"


def av_request(params, api_key, retries=3):
    """Isolated request wrapper - backs off on Alpha Vantage rate-limit
    notes rather than treating them as hard failures."""
    query = {**params, "apikey": api_key}
    for attempt in range(retries):
        resp = requests.get(AV_BASE, params=query, timeout=30)
        resp.raise_for_status()
        if query.get("datatype") == "csv":
            reader = csv.DictReader(io.StringIO(resp.text))
            rows = list(reader)
            return rows[0] if rows else {}
        data = resp.json()
        if "Note" in data or "Information" in data:
            print(f"[RATE LIMIT] {params.get('function')} - {data}. Retrying in 15s...")
            time.sleep(15)
            continue
        return data
    raise RuntimeError(f"Alpha Vantage request failed after {retries} retries: {params}")


# ---- Scoring functions: flat +/-2 scale, same "is it alarming" logic ----
# ---- established for CPI scoring in the FX framework                 ----

def score_eps_surprise(surprise_pct):
    if surprise_pct is None:
        return 0, "N/A - no estimate on record"
    if surprise_pct >= 10:
        return 2, f"Strong beat: {surprise_pct:+.1f}% EPS surprise"
    if surprise_pct >= 3:
        return 1, f"Modest beat: {surprise_pct:+.1f}% EPS surprise"
    if surprise_pct <= -10:
        return -2, f"Significant miss: {surprise_pct:+.1f}% EPS surprise"
    if surprise_pct <= -3:
        return -1, f"Modest miss: {surprise_pct:+.1f}% EPS surprise"
    return 0, f"In-line: {surprise_pct:+.1f}% EPS surprise"


def score_institutional_sentiment(increased, decreased):
    if increased is None or decreased is None or (increased + decreased) == 0:
        return 0, "N/A - no institutional holder data"
    net_ratio = (increased - decreased) / (increased + decreased)
    if net_ratio >= 0.15:
        return 2, f"Broad accumulation: {increased} holders adding vs {decreased} trimming"
    if net_ratio >= 0.05:
        return 1, f"Mild accumulation: {increased} holders adding vs {decreased} trimming"
    if net_ratio <= -0.15:
        return -2, f"Broad distribution: {decreased} holders trimming vs {increased} adding"
    if net_ratio <= -0.05:
        return -1, f"Mild distribution: {decreased} holders trimming vs {increased} adding"
    return 0, f"Balanced: {increased} adding vs {decreased} trimming"


def score_put_call_ratio(ratio):
    """Thresholds per Alpha Vantage's own docs: <=0.6 bullish, >=1.0 bearish."""
    if ratio is None:
        return 0, "N/A - no options chain data"
    if ratio <= 0.4:
        return 2, f"Strongly bullish positioning: P/C {ratio:.2f}"
    if ratio <= 0.6:
        return 1, f"Bullish tilt: P/C {ratio:.2f}"
    if ratio >= 1.3:
        return -2, f"Strongly bearish positioning: P/C {ratio:.2f}"
    if ratio >= 1.0:
        return -1, f"Bearish tilt: P/C {ratio:.2f}"
    return 0, f"Neutral: P/C {ratio:.2f}"


def score_revenue_surprise(surprise_pct):
    if surprise_pct is None:
        return 0, "N/A - no revenue estimate available"
    if surprise_pct >= 5:
        return 2, f"Strong revenue beat: {surprise_pct:+.1f}% vs estimate"
    if surprise_pct >= 1:
        return 1, f"Modest revenue beat: {surprise_pct:+.1f}% vs estimate"
    if surprise_pct <= -5:
        return -2, f"Significant revenue miss: {surprise_pct:+.1f}% vs estimate"
    if surprise_pct <= -1:
        return -1, f"Modest revenue miss: {surprise_pct:+.1f}% vs estimate"
    return 0, f"In-line revenue: {surprise_pct:+.1f}% vs estimate"


def score_estimate_revision(revision_pct):
    if revision_pct is None:
        return 0, "N/A - insufficient estimate history"
    if revision_pct >= 3:
        return 2, f"Estimates raised {revision_pct:+.1f}% over 90 days"
    if revision_pct >= 1:
        return 1, f"Estimates mildly raised {revision_pct:+.1f}% over 90 days"
    if revision_pct <= -3:
        return -2, f"Estimates cut {revision_pct:+.1f}% over 90 days"
    if revision_pct <= -1:
        return -1, f"Estimates mildly cut {revision_pct:+.1f}% over 90 days"
    return 0, f"Estimates stable: {revision_pct:+.1f}% over 90 days"


def score_margin_trend(margin_change_pts):
    if margin_change_pts is None:
        return 0, "N/A - insufficient margin history"
    if margin_change_pts >= 2:
        return 2, f"Operating margin expanding: {margin_change_pts:+.1f}pts YoY"
    if margin_change_pts >= 0.5:
        return 1, f"Operating margin mildly expanding: {margin_change_pts:+.1f}pts YoY"
    if margin_change_pts <= -2:
        return -2, f"Operating margin compressing: {margin_change_pts:+.1f}pts YoY"
    if margin_change_pts <= -0.5:
        return -1, f"Operating margin mildly compressing: {margin_change_pts:+.1f}pts YoY"
    return 0, f"Operating margin stable: {margin_change_pts:+.1f}pts YoY"


def score_momentum(change_pct):
    """Temporary stand-in for full Technical Setup. A single day's change
    is a weak proxy on its own - Phase 2 replaces this with MA structure
    and RSI. Tagged 'Technical-Placeholder' so it's never confused for
    the real Cardinal Rule technical layer."""
    if change_pct is None:
        return 0, "N/A - no quote data"
    if change_pct >= 3:
        return 2, f"Strong daily momentum: {change_pct:+.2f}%"
    if change_pct >= 1:
        return 1, f"Positive momentum: {change_pct:+.2f}%"
    if change_pct <= -3:
        return -2, f"Sharp daily weakness: {change_pct:+.2f}%"
    if change_pct <= -1:
        return -1, f"Negative momentum: {change_pct:+.2f}%"
    return 0, f"Flat: {change_pct:+.2f}%"


def fetch_ticker_data(ticker, api_key):
    rows = []
    today = time.strftime("%Y-%m-%d")

    try:
        earnings = av_request({"function": "EARNINGS", "symbol": ticker}, api_key)
        latest_q = (earnings.get("quarterlyEarnings") or [{}])[0]
        raw_surprise = latest_q.get("surprisePercentage")
        surprise_pct = float(raw_surprise) if raw_surprise not in (None, "None") else None
        score, note = score_eps_surprise(surprise_pct)
        # Leading apostrophe forces Sheets to store this as literal text
        # regardless of sign. Without it, Sheets auto-parses positive
        # percentage-looking strings into numbers (stripping the "%") while
        # leaving negative ones as text - same column, two different types,
        # silently breaks any SUMIF/comparison built on it later.
        surprise_display = f"'{surprise_pct:+.2f}%" if surprise_pct is not None else "N/A"
        rows.append([
            ticker, 1, "EPS Surprise",
            latest_q.get("reportedEPS", "N/A"), latest_q.get("estimatedEPS", "N/A"),
            "N/A", surprise_display,
            latest_q.get("reportedDate", "N/A"), "Endogenous", score, note,
            "Alpha Vantage: EARNINGS",
        ])
    except Exception as e:
        print(f"[FAIL] {ticker} EPS Surprise: {e}")

    # Revenue Surprise + Analyst Estimate Revisions both draw on
    # EARNINGS_ESTIMATES - fetch once, use for both.
    estimates = {}
    try:
        estimates = av_request({"function": "EARNINGS_ESTIMATES", "symbol": ticker}, api_key)
        quarterly_estimates = [
            e for e in (estimates.get("estimates") or []) if e.get("horizon") == "fiscal quarter"
        ]
        if quarterly_estimates:
            latest_est = quarterly_estimates[0]
            avg = latest_est.get("eps_estimate_average")
            avg_90 = latest_est.get("eps_estimate_average_90_days_ago")
            revision_pct = None
            if avg not in (None, "None") and avg_90 not in (None, "None") and float(avg_90) != 0:
                revision_pct = (float(avg) - float(avg_90)) / abs(float(avg_90)) * 100
            score, note = score_estimate_revision(revision_pct)
            revision_display = f"'{revision_pct:+.2f}%" if revision_pct is not None else "N/A"
            rows.append([
                ticker, 4, "Analyst Estimate Revisions (90-day)",
                avg or "N/A", avg_90 or "N/A", "N/A", revision_display,
                latest_est.get("date", today), "Endogenous", score, note,
                "Alpha Vantage: EARNINGS_ESTIMATES",
            ])
        else:
            rows.append([
                ticker, 4, "Analyst Estimate Revisions (90-day)",
                "N/A", "N/A", "N/A", "N/A", today, "Endogenous", 0,
                "N/A - no quarterly estimate history", "Alpha Vantage: EARNINGS_ESTIMATES",
            ])
    except Exception as e:
        print(f"[FAIL] {ticker} Estimate Revisions: {e}")

    try:
        income = av_request({"function": "INCOME_STATEMENT", "symbol": ticker}, api_key)
        q_reports = income.get("quarterlyReports") or []
        if q_reports:
            latest_q_report = q_reports[0]
            actual_revenue = latest_q_report.get("totalRevenue")
            fiscal_date = latest_q_report.get("fiscalDateEnding")

            est_match = next(
                (e for e in (estimates.get("estimates") or [])
                 if e.get("date") == fiscal_date and e.get("horizon") == "fiscal quarter"),
                None
            )
            revenue_surprise_pct = None
            est_revenue = est_match.get("revenue_estimate_average") if est_match else None
            if (actual_revenue not in (None, "None") and est_revenue not in (None, "None")
                    and float(est_revenue) != 0):
                revenue_surprise_pct = (float(actual_revenue) - float(est_revenue)) / float(est_revenue) * 100
            score, note = score_revenue_surprise(revenue_surprise_pct)
            rev_display = f"'{revenue_surprise_pct:+.2f}%" if revenue_surprise_pct is not None else "N/A"
            rows.append([
                ticker, 2, "Revenue Surprise",
                actual_revenue or "N/A", est_revenue or "N/A", "N/A", rev_display,
                fiscal_date or today, "Endogenous", score, note,
                "Alpha Vantage: INCOME_STATEMENT + EARNINGS_ESTIMATES",
            ])

            margin_change = None
            op_margin_now = None
            op_margin_prior = None
            if len(q_reports) > 4:
                prior_year_q = q_reports[4]
                try:
                    op_margin_now = float(latest_q_report["operatingIncome"]) / float(latest_q_report["totalRevenue"]) * 100
                    op_margin_prior = float(prior_year_q["operatingIncome"]) / float(prior_year_q["totalRevenue"]) * 100
                    margin_change = op_margin_now - op_margin_prior
                except (ValueError, ZeroDivisionError, KeyError, TypeError):
                    margin_change = None
            score, note = score_margin_trend(margin_change)
            margin_display = f"'{margin_change:+.2f}pts" if margin_change is not None else "N/A"
            rows.append([
                ticker, 5, "Operating Margin Trend (YoY)",
                f"{op_margin_now:.1f}%" if op_margin_now is not None else "N/A",
                f"{op_margin_prior:.1f}%" if op_margin_prior is not None else "N/A",
                "N/A", margin_display, fiscal_date or today, "Endogenous", score, note,
                "Alpha Vantage: INCOME_STATEMENT",
            ])
        else:
            rows.append([
                ticker, 2, "Revenue Surprise", "N/A", "N/A", "N/A", "N/A", today,
                "Endogenous", 0, "N/A - no income statement data",
                "Alpha Vantage: INCOME_STATEMENT",
            ])
    except Exception as e:
        print(f"[FAIL] {ticker} Revenue Surprise / Margin Trend: {e}")

    try:
        holdings = av_request({"function": "INSTITUTIONAL_HOLDINGS", "symbol": ticker}, api_key)
        increased = int(holdings.get("holders_with_increased_holdings", 0))
        decreased = int(holdings.get("holders_with_decreased_holdings", 0))
        score, note = score_institutional_sentiment(increased, decreased)
        rows.append([
            ticker, 14, "Institutional Holdings Sentiment",
            f"{increased} increased / {decreased} decreased", "N/A", "N/A", "N/A",
            today, "Confirmation", score, note,
            "Alpha Vantage: INSTITUTIONAL_HOLDINGS",
        ])
    except Exception as e:
        print(f"[FAIL] {ticker} Institutional Holdings: {e}")

    try:
        pcr = av_request({"function": "REALTIME_PUT_CALL_RATIO", "symbol": ticker}, api_key)
        raw_ratio = pcr.get("put_call_ratio_full_chain")
        ratio = float(raw_ratio) if raw_ratio not in (None, "None") else None
        score, note = score_put_call_ratio(ratio)
        rows.append([
            ticker, 15, "Put/Call Ratio",
            ratio if ratio is not None else "N/A", "N/A", "N/A", "N/A",
            today, "Confirmation", score, note,
            "Alpha Vantage: REALTIME_PUT_CALL_RATIO",
        ])
    except Exception as e:
        print(f"[FAIL] {ticker} Put/Call Ratio: {e}")

    try:
        quote = av_request(
            {"function": "GLOBAL_QUOTE", "symbol": ticker, "datatype": "csv"}, api_key
        )
        raw_change = quote.get("changePercent", "")
        change_pct = float(raw_change.replace("%", "")) if raw_change else None
        score, note = score_momentum(change_pct)
        change_display = f"'{raw_change}" if raw_change else "N/A"
        rows.append([
            ticker, 18, "Price Momentum Pulse (Phase 1 stand-in)",
            quote.get("price", "N/A"), quote.get("previousClose", "N/A"), "N/A",
            change_display, quote.get("latestDay", today),
            "Technical-Placeholder", score, note,
            "Alpha Vantage: GLOBAL_QUOTE",
        ])
    except Exception as e:
        print(f"[FAIL] {ticker} Price Momentum: {e}")

    return rows


def write_rows(spreadsheet, all_rows):
    ws = spreadsheet.worksheet("EQUITIES HUB DATA")
    existing = ws.get_all_values()
    if len(existing) > 1:
        ws.batch_clear([f"A2:L{len(existing)}"])
    if all_rows:
        ws.update("A2", all_rows, raw=False)
    print(f"[OK] Wrote {len(all_rows)} rows to EQUITIES HUB DATA")


def main():
    api_key = os.environ["ALPHA_VANTAGE_API_KEY"]
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(os.environ["EQUITIES_SHEET_ID"])

    all_rows = []
    for ticker in WATCHLIST:
        print(f"\n--- Fetching {ticker} ---")
        all_rows.extend(fetch_ticker_data(ticker, api_key))
        time.sleep(1)

    write_rows(spreadsheet, all_rows)
    print(f"\nDone. {len(all_rows)} total indicator rows across {len(WATCHLIST)} tickers.")


if __name__ == "__main__":
    main()

    
