"""
f4p_equities_weekly_update.py

Phase 1 of the F4P Equities & Options weekly pipeline.
Pulls a field-verified subset of indicators directly from Alpha Vantage's
REST API and writes flat +/-2 scored rows into the EQUITIES HUB DATA tab.

Covers 17 of the planned 18-indicator framework - all with schemas
confirmed live on 2026-08-25, plus indicators 6/7 (Forward Guidance,
Catalyst Pipeline) added separately via f4p_equities_qualitative_update.py:
  1.  EPS Surprise                    (Company Endogenous)
  2.  Revenue Surprise                (Company Endogenous)
  3.  Gross Margin Trend              (Company Endogenous, QoQ, cost mix)
  4.  Analyst Estimate Revisions      (Company Endogenous, 90-day)
  5.  Operating Margin Trend          (Company Endogenous, YoY, opex leverage)
  8.  Free Cash Flow Margin           (Company Endogenous, FCF/revenue)
  9.  Balance Sheet Quality           (Company Endogenous, current ratio +
                                        debt/equity context in notes)
  10. IV / Historical Vol Spread      (Sector/Relative Strength - options-
                                        pricing context, always scores 0
                                        since it isn't stock-directional;
                                        HV computed from the same daily
                                        series already fetched for
                                        indicator 11, no extra API call)
  11. Relative Strength vs SPY        (Sector/Relative Strength, 21-trading-day,
                                        adjusted close, sector ETF context in notes)
  12. Peer Relative Strength          (Sector/Relative Strength, 21-trading-day,
                                        single closest competitor per ticker -
                                        see PEER_MAP; QQQ has none by design)
  13. Sector Money-Flow (MFI-14)      (Sector/Relative Strength, sector ETF's
                                        Money Flow Index; QQQ uses its own MFI)
  14. Institutional Holdings Sentiment (Confirmation layer)
  15. Put/Call Ratio                  (Confirmation layer)
  16. Insider Activity                (Confirmation layer, 90-day, priced
                                        transactions only, client-side date
                                        filtered - from_date param confirmed
                                        NOT honored server-side on 2026-08-25)
  18. Price Momentum Pulse            (Phase 1 stand-in for full Technical
                                        Setup - flagged in the Tag column)

Only IV Rank (originally slot 17, now living as a weekly-accumulating
snapshot log in OPTIONS FLOW & IV rather than a HUB DATA row) remains -
it's time-gated, not code-gated: needs ~13 weeks of snapshots before a
real percentile rank means anything.

Indicators 8 and 9 map to two gaps identified against a coach-provided
target framework (Anton's 15-indicator Endogenous scorecard, built with
ChatGPT, shared 2026-08-25) - Free Cash Flow and Balance Sheet Quality.
That framework is a target shape to build toward, not verified ground
truth; its numbers have not been cross-checked against live data the
way everything in this pipeline has been.

Also populates the EARNINGS CALENDAR tab (next report date, timing,
consensus EPS estimate, days to event) - this is the raw data behind
the "Catalyst" column in STRATEGY DASHBOARD.

Also appends weekly ATM IV snapshots to OPTIONS FLOW & IV (accumulating
history toward a real IV Rank later - see that tab's own notes). This
one is heavy: HISTORICAL_OPTIONS returns the entire chain across all
expirations (confirmed 128,000+ tokens for a single ticker on
2026-08-25), so this run will be noticeably slower than prior ones.

Note: indicators 3 and 5 look similar at a glance (both "margin trend") but
measure genuinely different things - verified against NVDA's actual filed
numbers on 2026-08-25 after a discrepancy was flagged and traced by hand.
Gross Margin Trend is sequential (QoQ) and reflects direct cost mix.
Operating Margin Trend is year-over-year and reflects operating leverage
(revenue growth outpacing opex growth). Both are legitimate signals; they
are not redundant with each other despite the similar name.

Still outstanding (Phase 3): forward guidance, catalyst pipeline,
peer relative strength, sector money-flow, macro overlay (4
indicators - should cross-reference the FX Hub's existing tabs
rather than re-fetch), IV rank, IV/HV spread. Forward guidance and
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

# Single closest direct competitor per ticker, for Peer Relative Strength.
# GOOGL and META map to each other - the caching helper means fetching one
# of their series serves both, no duplicate API call.
PEER_MAP = {
    "NVDA": "AMD",
    "AAPL": "MSFT",
    "AMZN": "MSFT",  # cloud/AWS angle, not retail - confirmed 2026-08-27
    "GOOGL": "META",
    "TSLA": "RIVN",
    "META": "GOOGL",
    "COIN": "HOOD",
    "NFLX": "DIS",
    "QQQ": None,  # index fund - no single-name peer makes sense
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
            # Alpha Vantage sometimes returns a JSON error/rate-limit message
            # even when CSV was requested. Confirmed happening on 2026-08-26:
            # GLOBAL_QUOTE silently failed for all 9 tickers in one run because
            # this path parsed the rate-limit message as if it were empty CSV
            # instead of detecting and retrying it.
            stripped = resp.text.lstrip()
            if stripped.startswith("{"):
                try:
                    data = json.loads(resp.text)
                except json.JSONDecodeError:
                    data = {}
                if "Note" in data or "Information" in data:
                    print(f"[RATE LIMIT] {params.get('function')} (csv path) - "
                          f"{data}. Retrying in 15s...")
                    time.sleep(15)
                    continue
                raise RuntimeError(
                    f"Unexpected JSON response for CSV-format request: {data}"
                )
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


def compute_historical_volatility(daily_rows, window=21):
    """Annualized historical volatility from daily log returns over the
    trailing `window` trading days. Reuses the same adjusted-close series
    already fetched for Relative Strength - no extra API call needed.
    daily_rows must be ordered most-recent-first (Alpha Vantage default)."""
    import math
    if len(daily_rows) < window + 1:
        return None
    try:
        closes = [float(r["adjusted_close"]) for r in daily_rows[:window + 1]]
    except (KeyError, ValueError, TypeError):
        return None
    log_returns = []
    for i in range(len(closes) - 1):
        if closes[i] <= 0 or closes[i + 1] <= 0:
            continue
        log_returns.append(math.log(closes[i] / closes[i + 1]))
    if len(log_returns) < 2:
        return None
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    return (variance ** 0.5) * (252 ** 0.5)  # annualized


def score_iv_hv_spread(iv, hv):
    """IV/HV spread is an options-pricing signal, not a stock-direction
    one - unlike everything else feeding Total Score, this doesn't
    assert bullish or bearish. Scored 0 always (like Catalyst Pipeline)
    so it informs STRATEGY DASHBOARD context without skewing Bias on a
    metric that isn't actually directional."""
    if iv is None or hv is None or hv == 0:
        return 0, "N/A - insufficient data for IV/HV comparison"
    ratio = iv / hv
    if ratio >= 1.3:
        return 0, f"IV/HV {ratio:.2f} - options pricing more vol than realized (premium-selling context)"
    if ratio <= 0.7:
        return 0, f"IV/HV {ratio:.2f} - options pricing less vol than realized (premium-buying context)"
    return 0, f"IV/HV {ratio:.2f} - options roughly fairly priced vs realized volatility"


def get_atm_iv(ticker, current_price, api_key):
    """Finds the nearest-to-spot strike within the nearest expiration
    that's at least 14 days out (avoiding weeklies skewed by imminent
    events), and returns the average call+put implied volatility at
    that strike. Returns (iv, expiration, strike) - any of which may
    be None if unavailable."""
    try:
        chain = av_request({"function": "HISTORICAL_OPTIONS", "symbol": ticker}, api_key)
        contracts = chain.get("data") or []
        if not contracts or current_price is None:
            return None, None, None

        today_date = time.strftime("%Y-%m-%d")
        candidate_expirations = sorted({c["expiration"] for c in contracts if c.get("expiration")})
        chosen_exp = None
        for exp in candidate_expirations:
            try:
                days_out = (
                    time.mktime(time.strptime(exp, "%Y-%m-%d"))
                    - time.mktime(time.strptime(today_date, "%Y-%m-%d"))
                ) / 86400
            except ValueError:
                continue
            if days_out >= 14:
                chosen_exp = exp
                break
        if chosen_exp is None and candidate_expirations:
            chosen_exp = candidate_expirations[-1]
        if chosen_exp is None:
            return None, None, None

        exp_contracts = [c for c in contracts if c.get("expiration") == chosen_exp]

        def strike_diff(c):
            try:
                return abs(float(c.get("strike", 0)) - current_price)
            except (ValueError, TypeError):
                return float("inf")

        exp_contracts.sort(key=strike_diff)
        if not exp_contracts:
            return None, chosen_exp, None
        nearest_strike = exp_contracts[0].get("strike")

        ivs = []
        for c in exp_contracts:
            if c.get("strike") == nearest_strike:
                try:
                    iv = float(c.get("implied_volatility"))
                    if iv > 0:
                        ivs.append(iv)
                except (ValueError, TypeError):
                    continue
        if not ivs:
            return None, chosen_exp, nearest_strike
        return sum(ivs) / len(ivs), chosen_exp, nearest_strike
    except Exception as e:
        print(f"[FAIL] {ticker} ATM IV calc: {e}")
        return None, None, None


def get_or_fetch_mfi(symbol, mfi_cache, api_key):
    """Returns the most recent MFI(14) reading for `symbol`, cached so
    a sector ETF shared by multiple tickers (e.g. XLK for NVDA and AAPL)
    only gets fetched once per run."""
    if symbol in mfi_cache:
        return mfi_cache[symbol]
    try:
        row = av_request(
            {"function": "MFI", "symbol": symbol, "interval": "daily",
             "time_period": 14, "datatype": "csv"},
            api_key,
        )
        raw_mfi = row.get("MFI")
        mfi = float(raw_mfi) if raw_mfi not in (None, "None") else None
    except Exception as e:
        print(f"[FAIL] {symbol} MFI fetch: {e}")
        mfi = None
    mfi_cache[symbol] = mfi
    return mfi


def get_or_fetch_return(symbol, series_cache, api_key, raw_series_cache=None):
    """Returns the 21-day return for `symbol`, using a shared cache dict so
    a ticker that's both in the main watchlist AND someone else's peer
    (GOOGL and META map to each other) only gets fetched once per run.
    Also stashes the raw daily series in raw_series_cache if provided,
    so Historical Volatility can reuse it without a second API call."""
    if symbol in series_cache:
        return series_cache[symbol]
    try:
        series = av_request(
            {"function": "TIME_SERIES_DAILY_ADJUSTED", "symbol": symbol,
             "outputsize": "compact", "datatype": "csv"},
            api_key, csv_all_rows=True,
        )
        ret = compute_21d_return(series)
        if raw_series_cache is not None:
            raw_series_cache[symbol] = series
    except Exception as e:
        print(f"[FAIL] {symbol} daily series fetch: {e}")
        ret = None
    series_cache[symbol] = ret
    return ret


def score_sector_money_flow(mfi):
    """MFI as a flow-confirmation signal, not a contrarian reversal
    predictor - higher reading means more money flowing into the sector
    right now, consistent with how Put/Call Ratio is scored elsewhere
    in this framework."""
    if mfi is None:
        return 0, "N/A - insufficient data for MFI"
    if mfi >= 70:
        return 2, f"Strong money flow into sector: MFI {mfi:.1f}"
    if mfi >= 55:
        return 1, f"Positive money flow into sector: MFI {mfi:.1f}"
    if mfi <= 30:
        return -2, f"Strong money flow out of sector: MFI {mfi:.1f}"
    if mfi <= 45:
        return -1, f"Negative money flow out of sector: MFI {mfi:.1f}"
    return 0, f"Neutral money flow: MFI {mfi:.1f}"


def score_peer_relative_strength(rel_pct):
    """Tighter thresholds than the SPY comparison - direct competitor
    moves are a more concentrated signal than broad-market comparison."""
    if rel_pct is None:
        return 0, "N/A - insufficient price history"
    if rel_pct >= 6:
        return 2, f"Strong outperformance vs peer: {rel_pct:+.1f}pts over 21 trading days"
    if rel_pct >= 2:
        return 1, f"Mild outperformance vs peer: {rel_pct:+.1f}pts over 21 trading days"
    if rel_pct <= -6:
        return -2, f"Strong underperformance vs peer: {rel_pct:+.1f}pts over 21 trading days"
    if rel_pct <= -2:
        return -1, f"Mild underperformance vs peer: {rel_pct:+.1f}pts over 21 trading days"
    return 0, f"Tracking peer: {rel_pct:+.1f}pts over 21 trading days"


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


def score_free_cash_flow(fcf_margin):
    if fcf_margin is None:
        return 0, "N/A - insufficient data for FCF margin"
    if fcf_margin >= 30:
        return 2, f"Very strong FCF generation: {fcf_margin:+.1f}% margin"
    if fcf_margin >= 15:
        return 1, f"Solid FCF generation: {fcf_margin:+.1f}% margin"
    if fcf_margin <= -10:
        return -2, f"Burning cash: {fcf_margin:+.1f}% FCF margin"
    if fcf_margin < 5:
        return -1, f"Weak FCF generation: {fcf_margin:+.1f}% margin"
    return 0, f"Moderate FCF generation: {fcf_margin:+.1f}% margin"


def score_balance_sheet_quality(current_ratio):
    if current_ratio is None:
        return 0, "N/A - insufficient balance sheet data"
    if current_ratio >= 2.0:
        return 2, f"Very strong liquidity: {current_ratio:.2f} current ratio"
    if current_ratio >= 1.5:
        return 1, f"Solid liquidity: {current_ratio:.2f} current ratio"
    if current_ratio < 1.0:
        return -2, f"Liquidity stress: {current_ratio:.2f} current ratio (below 1.0)"
    if current_ratio < 1.2:
        return -1, f"Thin liquidity cushion: {current_ratio:.2f} current ratio"
    return 0, f"Adequate liquidity: {current_ratio:.2f} current ratio"


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


def fetch_ticker_data(ticker, api_key, spy_return_21d, sector_return_21d, sector_etf_symbol,
                       peer_ticker, series_cache, mfi_cache, raw_series_cache):
    rows = []
    today = time.strftime("%Y-%m-%d")
    revenue_for_ratios = None  # set below in Revenue Surprise block, reused by FCF margin

    try:
        earnings = av_request({"function": "EARNINGS", "symbol": ticker}, api_key)
        raw_reports = earnings.get("quarterlyEarnings") or []
        if not raw_reports:
            # TEMPORARY DIAGNOSTIC - a well-known large-cap ticker returning
            # zero quarterly earnings is suspicious, not necessarily genuine.
            # Log the raw response so the next run tells us definitively
            # whether this is real emptiness or a disguised rate-limit.
            print(f"[SUSPICIOUS EMPTY] {ticker} EARNINGS returned no "
                  f"quarterlyEarnings. Raw response keys: {list(earnings.keys())}. "
                  f"Full response: {earnings}")
        # Skip any leading entry that hasn't actually been reported yet -
        # confirmed happening for NVDA on 2026-08-26 (reports tonight):
        # Alpha Vantage can place a pending quarter at index 0 with
        # reportedEPS=null but estimatedEPS populated, right around
        # earnings day. Taking index 0 blindly would silently show "no
        # surprise" instead of falling through to the real latest report.
        reported = [r for r in raw_reports if r.get("reportedEPS") not in (None, "None")]
        latest_q = reported[0] if reported else {}
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
            latest_q.get("reportedEPS") or "N/A", latest_q.get("estimatedEPS") or "N/A",
            "N/A", surprise_display,
            latest_q.get("reportedDate") or "N/A", "Endogenous", score, note,
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
                today, "Endogenous", score, note + f" (for fiscal quarter ending {latest_est.get('date', 'N/A')})",
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
        if not q_reports:
            print(f"[SUSPICIOUS EMPTY] {ticker} INCOME_STATEMENT returned no "
                  f"quarterlyReports. Raw response keys: {list(income.keys())}. "
                  f"Full response: {income}")
        if q_reports:
            latest_q_report = q_reports[0]
            actual_revenue = latest_q_report.get("totalRevenue")
            fiscal_date = latest_q_report.get("fiscalDateEnding")
            if actual_revenue not in (None, "None"):
                try:
                    revenue_for_ratios = float(actual_revenue)
                except (ValueError, TypeError):
                    revenue_for_ratios = None

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
        cash_flow = av_request({"function": "CASH_FLOW", "symbol": ticker}, api_key)
        cf_reports = cash_flow.get("quarterlyReports") or []
        if not cf_reports:
            print(f"[SUSPICIOUS EMPTY] {ticker} CASH_FLOW returned no "
                  f"quarterlyReports. Raw response keys: {list(cash_flow.keys())}. "
                  f"Full response: {cash_flow}")
        fcf = None
        fcf_margin = None
        if cf_reports:
            latest_cf = cf_reports[0]
            try:
                op_cf = float(latest_cf["operatingCashflow"])
                capex = float(latest_cf["capitalExpenditures"])
                fcf = op_cf - capex
                if revenue_for_ratios:
                    fcf_margin = fcf / revenue_for_ratios * 100
            except (KeyError, ValueError, TypeError):
                fcf = None
        score, note = score_free_cash_flow(fcf_margin)
        rows.append([
            ticker, 8, "Free Cash Flow Margin",
            f"${fcf:,.0f}" if fcf is not None else "N/A",
            "N/A", "N/A",
            f"'{fcf_margin:+.1f}%" if fcf_margin is not None else "N/A",
            today, "Endogenous", score, note,
            "Alpha Vantage: CASH_FLOW",
        ])
    except Exception as e:
        print(f"[FAIL] {ticker} Free Cash Flow: {e}")

    try:
        balance = av_request({"function": "BALANCE_SHEET", "symbol": ticker}, api_key)
        bs_reports = balance.get("quarterlyReports") or []
        if not bs_reports:
            print(f"[SUSPICIOUS EMPTY] {ticker} BALANCE_SHEET returned no "
                  f"quarterlyReports. Raw response keys: {list(balance.keys())}. "
                  f"Full response: {balance}")
        current_ratio = None
        debt_to_equity = None
        if bs_reports:
            latest_bs = bs_reports[0]
            try:
                current_assets = float(latest_bs["totalCurrentAssets"])
                current_liabilities = float(latest_bs["totalCurrentLiabilities"])
                if current_liabilities > 0:
                    current_ratio = current_assets / current_liabilities
                total_liabilities = float(latest_bs["totalLiabilities"])
                total_equity = float(latest_bs["totalShareholderEquity"])
                if total_equity > 0:
                    debt_to_equity = total_liabilities / total_equity
            except (KeyError, ValueError, TypeError):
                current_ratio = None
        score, note = score_balance_sheet_quality(current_ratio)
        if debt_to_equity is not None:
            note += f" | Debt/Equity: {debt_to_equity:.2f}"
        rows.append([
            ticker, 9, "Balance Sheet Quality (Current Ratio)",
            f"{current_ratio:.2f}" if current_ratio is not None else "N/A",
            "N/A", "N/A", "N/A",
            today, "Endogenous", score, note,
            "Alpha Vantage: BALANCE_SHEET",
        ])
    except Exception as e:
        print(f"[FAIL] {ticker} Balance Sheet Quality: {e}")

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
        own_return = get_or_fetch_return(ticker, series_cache, api_key, raw_series_cache)
        rel_vs_spy = (
            (own_return - spy_return_21d)
            if (own_return is not None and spy_return_21d is not None)
            else None
        )
        score, note = score_relative_strength(rel_vs_spy)
        if sector_return_21d is not None and own_return is not None:
            note += f" | vs {sector_etf_symbol} ({sector_return_21d:+.1f}%): {own_return - sector_return_21d:+.1f}pts"
        rel_display = f"'{rel_vs_spy:+.2f}pts" if rel_vs_spy is not None else "N/A"
        own_display = f"'{own_return:+.1f}%" if own_return is not None else "N/A"
        spy_display = f"'{spy_return_21d:+.1f}%" if spy_return_21d is not None else "N/A"
        rows.append([
            ticker, 11, "Relative Strength vs SPY (21-trading-day)",
            own_display, spy_display,
            "N/A", rel_display, today, "Sector/Relative Strength", score, note,
            "Alpha Vantage: TIME_SERIES_DAILY_ADJUSTED",
        ])
    except Exception as e:
        print(f"[FAIL] {ticker} Relative Strength: {e}")

    try:
        own_return = get_or_fetch_return(ticker, series_cache, api_key, raw_series_cache)
        if peer_ticker is None:
            rows.append([
                ticker, 12, "Peer Relative Strength (21-trading-day)",
                "N/A", "N/A", "N/A", "N/A", today, "Sector/Relative Strength", 0,
                "N/A - no single-name peer (index fund)",
                "Alpha Vantage: TIME_SERIES_DAILY_ADJUSTED",
            ])
        else:
            peer_return = get_or_fetch_return(peer_ticker, series_cache, api_key, raw_series_cache)
            rel_vs_peer = (
                (own_return - peer_return)
                if (own_return is not None and peer_return is not None)
                else None
            )
            score, note = score_peer_relative_strength(rel_vs_peer)
            own_display = f"'{own_return:+.1f}%" if own_return is not None else "N/A"
            peer_display = f"'{peer_return:+.1f}%" if peer_return is not None else "N/A"
            rel_display = f"'{rel_vs_peer:+.2f}pts" if rel_vs_peer is not None else "N/A"
            rows.append([
                ticker, 12, f"Peer Relative Strength vs {peer_ticker} (21-trading-day)",
                own_display, peer_display, "N/A", rel_display, today,
                "Sector/Relative Strength", score, note,
                "Alpha Vantage: TIME_SERIES_DAILY_ADJUSTED",
            ])
    except Exception as e:
        print(f"[FAIL] {ticker} Peer Relative Strength: {e}")

    try:
        flow_symbol = sector_etf_symbol if sector_etf_symbol else ticker
        mfi = get_or_fetch_mfi(flow_symbol, mfi_cache, api_key)
        score, note = score_sector_money_flow(mfi)
        note += f" (measured via {flow_symbol})"
        rows.append([
            ticker, 13, "Sector Money-Flow (MFI-14)",
            f"{mfi:.1f}" if mfi is not None else "N/A",
            "N/A", "N/A", "N/A", today, "Sector/Relative Strength", score, note,
            "Alpha Vantage: MFI",
        ])
    except Exception as e:
        print(f"[FAIL] {ticker} Sector Money-Flow: {e}")

    earnings_calendar_row = None
    try:
        calendar = av_request(
            {"function": "EARNINGS_CALENDAR", "symbol": ticker, "datatype": "csv"},
            api_key,
        )
        report_date = calendar.get("reportDate")
        # Confirmed on 2026-08-26: under burst-rate-limit conditions this
        # endpoint can return a malformed/truncated CSV that still parses
        # "successfully" but with garbage values (e.g. reportDate == "f").
        # Validate the shape before trusting it, rather than letting
        # strptime raise and silently drop the ticker from the tab.
        valid_date = (
            report_date
            and len(report_date) == 10
            and report_date[4] == "-"
            and report_date[7] == "-"
            and report_date[:4].isdigit()
        )
        if report_date and not valid_date:
            print(f"[SUSPICIOUS EMPTY] {ticker} EARNINGS_CALENDAR returned an "
                  f"unparseable reportDate. Raw response: {calendar}")
        if valid_date:
            report_dt = time.strptime(report_date, "%Y-%m-%d")
            days_to_event = (
                time.mktime(report_dt) - time.mktime(time.strptime(today, "%Y-%m-%d"))
            ) / 86400
            timing = calendar.get("timeOfTheDay") or "timing not yet confirmed"
            estimate = calendar.get("estimate") or "N/A"
            earnings_calendar_row = [
                ticker,
                f"{report_date} ({timing})",
                estimate,
                "N/A - not fiscal-quarter-matched, see Analyst Estimate Revisions row",
                "N/A - see Revenue Surprise row in EQUITIES HUB DATA",
                int(round(days_to_event)),
            ]
        else:
            # No earnings found in the horizon, OR the response was
            # malformed - either way, write this explicitly rather than
            # silently dropping the ticker from the tab.
            earnings_calendar_row = [
                ticker, "N/A - no earnings scheduled in 3-month horizon "
                        "(or response was malformed - check logs)",
                "N/A", "N/A", "N/A", "N/A",
            ]
    except Exception as e:
        print(f"[FAIL] {ticker} Earnings Calendar: {e}")

    iv_snapshot_row = None
    try:
        quote_for_iv = av_request(
            {"function": "GLOBAL_QUOTE", "symbol": ticker, "datatype": "csv"}, api_key
        )
        current_price = None
        raw_price = quote_for_iv.get("price")
        if raw_price:
            try:
                current_price = float(raw_price)
            except (ValueError, TypeError):
                current_price = None

        atm_iv, chosen_exp, nearest_strike = get_atm_iv(ticker, current_price, api_key)
        if atm_iv is not None:
            iv_snapshot_row = [
                today, ticker, f"{atm_iv * 100:.2f}%",
                chosen_exp or "N/A", nearest_strike or "N/A",
                "Snapshot only - accumulating weekly history for future IV Rank calc",
            ]
        else:
            iv_snapshot_row = [
                today, ticker, "N/A", chosen_exp or "N/A", nearest_strike or "N/A",
                "N/A - could not resolve an ATM contract this run",
            ]
    except Exception as e:
        print(f"[FAIL] {ticker} ATM IV Snapshot: {e}")
        iv_snapshot_row = [today, ticker, "N/A", "N/A", "N/A", f"N/A - fetch error: {e}"]
        atm_iv = None

    try:
        hv = compute_historical_volatility(raw_series_cache.get(ticker, []))
        score, note = score_iv_hv_spread(atm_iv, hv)
        rows.append([
            ticker, 10, "IV / Historical Vol Spread",
            f"{atm_iv * 100:.2f}%" if atm_iv is not None else "N/A",
            f"{hv * 100:.2f}%" if hv is not None else "N/A",
            "N/A", "N/A", today, "Sector/Relative Strength", score, note,
            "Alpha Vantage: HISTORICAL_OPTIONS + TIME_SERIES_DAILY_ADJUSTED",
        ])
    except Exception as e:
        print(f"[FAIL] {ticker} IV/HV Spread: {e}")

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
            quote.get("price") or "N/A", quote.get("previousClose") or "N/A", "N/A",
            change_display, quote.get("latestDay", today),
            "Technical-Placeholder", score, note,
            "Alpha Vantage: GLOBAL_QUOTE",
        ])
    except Exception as e:
        print(f"[FAIL] {ticker} Price Momentum: {e}")

    return rows, earnings_calendar_row, iv_snapshot_row


def write_rows(spreadsheet, all_rows):
    ws = spreadsheet.worksheet("EQUITIES HUB DATA")
    existing = ws.get_all_values()
    if len(existing) > 1:
        ws.batch_clear([f"A2:L{len(existing)}"])
    if all_rows:
        ws.update("A2", all_rows, raw=False)
    print(f"[OK] Wrote {len(all_rows)} rows to EQUITIES HUB DATA")


def write_calendar_rows(spreadsheet, calendar_rows):
    ws = spreadsheet.worksheet("EARNINGS CALENDAR")
    existing = ws.get_all_values()
    if len(existing) > 1:
        ws.batch_clear([f"A2:F{len(existing)}"])
    if calendar_rows:
        ws.update("A2", calendar_rows, raw=False)
    print(f"[OK] Wrote {len(calendar_rows)} rows to EARNINGS CALENDAR")


def write_iv_snapshot_rows(spreadsheet, iv_rows):
    """Appends this week's IV snapshots rather than overwriting - the
    whole point is accumulating history over time to eventually compute
    a real IV Rank, not a fresh-each-run data dump like the other tabs.

    Repurposes this tab's original columns (Put/Call Ratio, Insider
    Activity, etc.) since those now live properly in EQUITIES HUB DATA
    indicators 15 and 16 - this tab was never populated under that old
    design."""
    ws = spreadsheet.worksheet("OPTIONS FLOW & IV")
    expected_headers = ["Date", "Ticker", "ATM IV", "Expiration Used", "Strike Used", "Notes"]
    existing = ws.get_all_values()
    has_stale_extra_columns = bool(existing) and len(existing[0]) > len(expected_headers)
    header_needs_fix = (not existing) or (existing[0][:6] != expected_headers) or has_stale_extra_columns
    if header_needs_fix:
        # Clear the FULL header row first - a partial A1:F1 write leaves
        # stale text in any columns from the tab's old 8-column design
        # (confirmed happening: "Insider Activity" and "Notes" lingered
        # in G1:H1 after the first run on 2026-08-26).
        if has_stale_extra_columns:
            ws.batch_clear([f"A1:{gspread.utils.rowcol_to_a1(1, len(existing[0]))}"])
        ws.update("A1", [expected_headers], raw=False)
        ws.format("A1:F1", {
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "backgroundColor": {"red": 0.09, "green": 0.13, "blue": 0.18},
        })
        ws.freeze(rows=1)
    if iv_rows:
        ws.append_rows(iv_rows, value_input_option="USER_ENTERED")
    print(f"[OK] Appended {len(iv_rows)} IV snapshot rows to OPTIONS FLOW & IV (accumulating history)")


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
    all_calendar_rows = []
    all_iv_rows = []
    series_cache = {}  # shared across all tickers - avoids re-fetching GOOGL/META
                        # twice since they're each other's peer
    mfi_cache = {}      # shared across tickers with the same sector ETF
    raw_series_cache = {}  # stores full daily series per symbol, for
                            # Historical Volatility to reuse without a
                            # second TIME_SERIES_DAILY_ADJUSTED call
    for ticker in WATCHLIST:
        print(f"\n--- Fetching {ticker} ---")
        sector_etf = SECTOR_ETF_MAP.get(ticker)
        sector_return = sector_return_cache.get(sector_etf) if sector_etf else None
        peer_ticker = PEER_MAP.get(ticker)
        ticker_rows, calendar_row, iv_row = fetch_ticker_data(
            ticker, api_key, spy_return_21d, sector_return, sector_etf,
            peer_ticker, series_cache, mfi_cache, raw_series_cache,
        )
        all_rows.extend(ticker_rows)
        if calendar_row:
            all_calendar_rows.append(calendar_row)
        if iv_row:
            all_iv_rows.append(iv_row)
        time.sleep(1)

    write_rows(spreadsheet, all_rows)
    write_calendar_rows(spreadsheet, all_calendar_rows)
    write_iv_snapshot_rows(spreadsheet, all_iv_rows)
    print(f"\nDone. {len(all_rows)} total indicator rows across {len(WATCHLIST)} tickers.")


if __name__ == "__main__":
    main()

    
