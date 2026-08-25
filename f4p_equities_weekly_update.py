"""
f4p_equities_weekly_update.py

Phase 1 of the F4P Equities & Options weekly pipeline.
Pulls a field-verified subset of indicators directly from Alpha Vantage's
REST API and writes flat +/-2 scored rows into the EQUITIES HUB DATA tab.

Covers 10 of the planned 18-indicator framework - all with schemas
confirmed live on 2026-08-25:
  1.  EPS Surprise                    (Company Endogenous)
  2.  Revenue Surprise                (Company Endogenous)
  3.  Gross Margin Trend              (Company Endogenous, QoQ, cost mix)
  4.  Analyst Estimate Revisions      (Company Endogenous, 90-day)
  5.  Operating Margin Trend          (Company Endogenous, YoY, opex leverage)
  11. Relative Strength vs SPY        (Sector/Relative Strength, 21-trading-day,
                                        adjusted close, sector ETF context in notes)
  14. Institutional Holdings Sentiment (Confirmation layer)
  15. Put/Call Ratio                  (Confirmation layer)
  16. Insider Activity                (Confirmation layer, 90-day, priced
                                        transactions only, client-side date
                                        filtered - from_date param confirmed
                                        NOT honored server-side on 2026-08-25)
  18. Price Momentum Pulse            (Phase 1 stand-in for full Technical
                                        Setup - flagged in the Tag column)

Note: indicators 3 and 5 look similar at a glance (both "margin trend") but
measure genuinely different things - verified against NVDA's actual filed
numbers on 2026-08-25 after a discrepancy was flagged and traced by hand.
Gross Margin Trend is sequential (QoQ) and reflects direct cost mix.
Operating Margin Trend is year-over-year and reflects operating leverage
(revenue growth outpacing opex growth). Both are legitimate signals; they
are not redundant with each other despite the similar name.

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
from datetime import datetime, timedelta
import requests
import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

WATCHLIST = ["NVDA", "AAPL", "AMZN", "GOOGL", "TSLA", "META", "COIN", "NFLX", "QQQ"]

# QQQ maps to None deliberately - it's itself an index (Nasdaq-100), so it's
# compared directly to SPY rather than to a sector layer on top of that.
SECTOR_ETF_MAP = {
    "NVDA": "XLK",   # Technology
    "AAPL": "XLK",   # Technology
    "AMZN": "XLY",   # Consumer Discretionary
    "GOOGL": "XLC",  # Communication Services
    "TSLA": "XLY",   # Consumer Discretionary
    "META": "XLC",   # Communication Services
    "COIN": "XLF",   # Financials (closest GICS fit for a crypto exchange)
    "NFLX": "XLC",   # Communication Services
    "QQQ": None,
}

AV_BASE = "https://www.alphavantage.co/query"


def av_request(params, api_key, retries=3, csv_all_rows=False):
    """Isolated request wrapper - backs off on Alpha Vantage rate-limit
    notes rather than treating them as hard failures.
    csv_all_rows=True returns every parsed row (for time series);
    otherwise only the first row is returned (for single-row responses
    like GLOBAL_QUOTE)."""
    query = {**params, "apikey": api_key}
    for attempt in range(retries):
        resp = requests.get(AV_BASE, params=query, timeout=30)
        resp.raise_for_status()
        if query.get("datatype") == "csv":
            reader = csv.DictReader(io.StringIO(resp.text))
            rows = list(reader)
            if csv_all_rows:
                return rows
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
        return 2, f"Operating margin expanding: {margin_change_pts:+.1f}pts YoY (opex leverage)"
    if margin_change_pts >= 0.5:
        return 1, f"Operating margin mildly expanding: {margin_change_pts:+.1f}pts YoY (opex leverage)"
    if margin_change_pts <= -2:
        return -2, f"Operating margin compressing: {margin_change_pts:+.1f}pts YoY (opex leverage)"
    if margin_change_pts <= -0.5:
        return -1, f"Operating margin mildly compressing: {margin_change_pts:+.1f}pts YoY (opex leverage)"
    return 0, f"Operating margin stable: {margin_change_pts:+.1f}pts YoY (opex leverage)"


def score_gross_margin_qoq(margin_change_pts):
    """Gross margin moves in much smaller increments than operating margin,
    so it uses its own tighter thresholds - the CPI 'is it alarming' logic
    scaled down to this metric's normal range."""
    if margin_change_pts is None:
        return 0, "N/A - insufficient margin history"
    if margin_change_pts >= 1:
        return 2, f"Gross margin expanding: {margin_change_pts:+.2f}pts QoQ (cost mix)"
    if margin_change_pts >= 0.25:
        return 1, f"Gross margin mildly expanding: {margin_change_pts:+.2f}pts QoQ (cost mix)"
    if margin_change_pts <= -1:
        return -2, f"Gross margin compressing: {margin_change_pts:+.2f}pts QoQ (cost mix)"
    if margin_change_pts <= -0.25:
        return -1, f"Gross margin mildly compressing: {margin_change_pts:+.2f}pts QoQ (cost mix)"
    return 0, f"Gross margin stable: {margin_change_pts:+.2f}pts QoQ (cost mix)"


def compute_21d_return(daily_rows):
    """daily_rows: parsed CSV rows from TIME_SERIES_DAILY_ADJUSTED, most
    recent first (Alpha Vantage's default order). Returns percent return
    over the trailing 21 trading days (~1 calendar month) using adjusted
    close, so dividend events don't distort the number. None if there
    isn't enough history."""
    if len(daily_rows) < 22:
        return None
    try:
        recent = float(daily_rows[0]["adjusted_close"])
        prior = float(daily_rows[20]["adjusted_close"])
        if prior == 0:
            return None
        return (recent - prior) / prior * 100
    except (KeyError, ValueError, TypeError):
        return None


def score_relative_strength(rel_pct):
    if rel_pct is None:
        return 0, "N/A - insufficient price history"
    if rel_pct >= 8:
        return 2, f"Strong outperformance vs SPY: {rel_pct:+.1f}pts over 21 trading days"
    if rel_pct >= 3:
        return 1, f"Mild outperformance vs SPY: {rel_pct:+.1f}pts over 21 trading days"
    if rel_pct <= -8:
        return -2, f"Strong underperformance vs SPY: {rel_pct:+.1f}pts over 21 trading days"
    if rel_pct <= -3:
        return -1, f"Mild underperformance vs SPY: {rel_pct:+.1f}pts over 21 trading days"
    return 0, f"Tracking SPY: {rel_pct:+.1f}pts over 21 trading days"


def score_insider_activity(net_ratio):
    if net_ratio is None:
        return 0, "N/A - no priced insider transactions in 90-day window"
    if net_ratio >= 0.5:
        return 2, f"Insider buying dominant: net ratio {net_ratio:+.2f}"
    if net_ratio >= 0.15:
        return 1, f"Mild insider buying lean: net ratio {net_ratio:+.2f}"
    if net_ratio <= -0.5:
        return -2, (f"Insider selling dominant: net ratio {net_ratio:+.2f} "
                     f"(note: often routine 10b5-1 plan activity at large caps, not necessarily bearish)")
    if net_ratio <= -0.15:
        return -1, (f"Mild insider selling lean: net ratio {net_ratio:+.2f} "
                     f"(note: often routine 10b5-1 plan activity, not necessarily bearish)")
    return 0, f"Balanced insider activity: net ratio {net_ratio:+.2f}"


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


def fetch_ticker_data(ticker, api_key, spy_return_21d, sector_return_21d, sector_etf_symbol):
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
                ticker, 5, "Operating Margin Trend (YoY, opex leverage)",
                f"{op_margin_now:.1f}%" if op_margin_now is not None else "N/A",
                f"{op_margin_prior:.1f}%" if op_margin_prior is not None else "N/A",
                "N/A", margin_display, fiscal_date or today, "Endogenous", score, note,
                "Alpha Vantage: INCOME_STATEMENT",
            ])

            gross_margin_change = None
            gross_margin_now = None
            gross_margin_prior_q = None
            if len(q_reports) > 1:
                prior_quarter = q_reports[1]
                try:
                    gross_margin_now = float(latest_q_report["grossProfit"]) / float(latest_q_report["totalRevenue"]) * 100
                    gross_margin_prior_q = float(prior_quarter["grossProfit"]) / float(prior_quarter["totalRevenue"]) * 100
                    gross_margin_change = gross_margin_now - gross_margin_prior_q
                except (ValueError, ZeroDivisionError, KeyError, TypeError):
                    gross_margin_change = None
            score, note = score_gross_margin_qoq(gross_margin_change)
            gross_display = f"'{gross_margin_change:+.2f}pts" if gross_margin_change is not None else "N/A"
            rows.append([
                ticker, 3, "Gross Margin Trend (QoQ, cost mix)",
                f"{gross_margin_now:.1f}%" if gross_margin_now is not None else "N/A",
                f"{gross_margin_prior_q:.1f}%" if gross_margin_prior_q is not None else "N/A",
                "N/A", gross_display,
                prior_quarter.get("fiscalDateEnding", today) if len(q_reports) > 1 else today,
                "Endogenous", score, note, "Alpha Vantage: INCOME_STATEMENT",
            ])
        else:
            rows.append([
                ticker, 2, "Revenue Surprise", "N/A", "N/A", "N/A", "N/A", today,
                "Endogenous", 0, "N/A - no income statement data",
                "Alpha Vantage: INCOME_STATEMENT",
            ])
            rows.append([
                ticker, 3, "Gross Margin Trend (QoQ, cost mix)", "N/A", "N/A", "N/A", "N/A",
                today, "Endogenous", 0, "N/A - no income statement data",
                "Alpha Vantage: INCOME_STATEMENT",
            ])
            rows.append([
                ticker, 5, "Operating Margin Trend (YoY, opex leverage)", "N/A", "N/A", "N/A", "N/A",
                today, "Endogenous", 0, "N/A - no income statement data",
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
        lookback_date = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
        insider = av_request(
            {"function": "INSIDER_TRANSACTIONS", "symbol": ticker, "from_date": lookback_date},
            api_key,
        )
        # The from_date parameter is NOT reliably honored by the raw REST
        # API - confirmed via diagnostic logging on 2026-08-25, where a
        # 90-day request for NVDA returned 6,920 rows spanning 2003-2026.
        # Filtering client-side instead of trusting the server to scope it.
        transactions_raw = insider.get("data") or []
        transactions = [
            t for t in transactions_raw
            if t.get("transaction_date") and t["transaction_date"] >= lookback_date
        ]

        buy_value = 0.0
        sell_value = 0.0
        for t in transactions:
            try:
                price = float(t.get("share_price") or 0)
                shares = float(t.get("shares") or 0)
            except (ValueError, TypeError):
                continue
            if price <= 0:
                continue  # RSU vest/grant, not a market transaction - excluded
            direction = t.get("acquisition_or_disposal")
            if direction == "A":
                buy_value += shares * price
            elif direction == "D":
                sell_value += shares * price
        total = buy_value + sell_value
        net_ratio = (buy_value - sell_value) / total if total > 0 else None
        score, note = score_insider_activity(net_ratio)
        ratio_display = f"'{net_ratio:+.2f}" if net_ratio is not None else "N/A"
        rows.append([
            ticker, 16, "Insider Activity (90-day, priced transactions only)",
            f"${buy_value:,.0f} bought", f"${sell_value:,.0f} sold", "N/A", ratio_display,
            today, "Confirmation", score, note,
            "Alpha Vantage: INSIDER_TRANSACTIONS",
        ])
    except Exception as e:
        print(f"[FAIL] {ticker} Insider Activity: {e}")

    try:
        own_series = av_request(
            {"function": "TIME_SERIES_DAILY_ADJUSTED", "symbol": ticker,
             "outputsize": "compact", "datatype": "csv"},
            api_key, csv_all_rows=True,
        )
        own_return = compute_21d_return(own_series)
        rel_vs_spy = (
            (own_return - spy_return_21d)
            if (own_return is not None and spy_return_21d is not None)
            else None
        )
        score, note = score_relative_strength(rel_vs_spy)
        if sector_return_21d is not None and own_return is not None:
            note += f" | vs {sector_etf_symbol} ({sector_return_21d:+.1f}%): {own_return - sector_return_21d:+.1f}pts"
        rel_display = f"'{rel_vs_spy:+.2f}pts" if rel_vs_spy is not None else "N/A"
        rows.append([
            ticker, 11, "Relative Strength vs SPY (21-trading-day)",
            f"{own_return:+.1f}%" if own_return is not None else "N/A",
            f"{spy_return_21d:+.1f}%" if spy_return_21d is not None else "N/A",
            "N/A", rel_display, today, "Sector/Relative Strength", score, note,
            "Alpha Vantage: TIME_SERIES_DAILY_ADJUSTED",
        ])
    except Exception as e:
        print(f"[FAIL] {ticker} Relative Strength: {e}")

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

    print("\n--- Fetching shared market/sector benchmarks ---")
    spy_series = av_request(
        {"function": "TIME_SERIES_DAILY_ADJUSTED", "symbol": "SPY",
         "outputsize": "compact", "datatype": "csv"},
        api_key, csv_all_rows=True,
    )
    spy_return_21d = compute_21d_return(spy_series)
    print(f"[OK] SPY 21-day return: {spy_return_21d:+.2f}%"
          if spy_return_21d is not None else "[FAIL] SPY return unavailable")

    sector_return_cache = {}
    unique_etfs = sorted({etf for etf in SECTOR_ETF_MAP.values() if etf})
    for etf in unique_etfs:
        try:
            series = av_request(
                {"function": "TIME_SERIES_DAILY_ADJUSTED", "symbol": etf,
                 "outputsize": "compact", "datatype": "csv"},
                api_key, csv_all_rows=True,
            )
            sector_return_cache[etf] = compute_21d_return(series)
            print(f"[OK] {etf} 21-day return: {sector_return_cache[etf]:+.2f}%"
                  if sector_return_cache[etf] is not None else f"[FAIL] {etf} return unavailable")
        except Exception as e:
            print(f"[FAIL] {etf} sector benchmark fetch: {e}")
            sector_return_cache[etf] = None

    all_rows = []
    for ticker in WATCHLIST:
        print(f"\n--- Fetching {ticker} ---")
        sector_etf = SECTOR_ETF_MAP.get(ticker)
        sector_return = sector_return_cache.get(sector_etf) if sector_etf else None
        all_rows.extend(
            fetch_ticker_data(ticker, api_key, spy_return_21d, sector_return, sector_etf)
        )
        time.sleep(1)

    write_rows(spreadsheet, all_rows)
    print(f"\nDone. {len(all_rows)} total indicator rows across {len(WATCHLIST)} tickers.")


if __name__ == "__main__":
    main()

    
