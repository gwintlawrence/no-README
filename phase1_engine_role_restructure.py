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
