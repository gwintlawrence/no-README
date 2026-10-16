"""
F4P data spec reader.

f4p_data_spec.json is the single list of what the Hub collects, from which official source,
and how each reading is scored. This module only reads it. It has no third-party imports so
the tests and the prompt-pack builder can use it anywhere.

Safety rule: reading the spec must never break the weekly run. Any problem (file missing,
bad JSON, a currency not in it) returns None / an empty string, and the pipeline then behaves
exactly as it did before the spec existed.
"""

import json
import os

SPEC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "f4p_data_spec.json")

_cache = {}


def load_spec(path=None):
    path = path or SPEC_PATH
    if path in _cache:
        return _cache[path]
    try:
        with open(path, encoding="utf-8") as f:
            spec = json.load(f)
        if not isinstance(spec.get("endogenous", {}).get("currencies"), dict):
            spec = None
    except Exception:
        spec = None
    _cache[path] = spec
    return spec


def indicator_definitions_block(spec, currencies):
    """Text block that tells the AI exactly which measure, source and score guide to use for
    every indicator of the given currencies. Returns "" when the spec cannot supply all of them."""
    if not spec:
        return ""
    table = spec.get("endogenous", {}).get("currencies", {})
    if any(c not in table for c in currencies):
        return ""

    lines = [
        "",
        "INDICATOR DEFINITIONS - for each currency use exactly the measure named below, take it from "
        "the official source given, and score it with the score guide given. Where a score guide is "
        "given, the guide decides the score (use the general scale above only to interpret a guide "
        "that does not cover the reading). In your answer, the \"indicator\" field must still be the "
        "generic indicator name from the numbered list above.",
        "SOURCE RULE - use the named source and series for every indicator. If that source has not yet "
        "published the newest release, report the latest value it does show, give its date, and say so in "
        "the analysis. Do not substitute a different series, a weekly or not-seasonally-adjusted version, "
        "or an estimate from another site, because the same indicator must be measured the same way every "
        "week and by every person who checks it.",
        "FRESHNESS RULE - if the page you read shows a daily or weekly figure (a yield, a rate, a central-bank "
        "balance sheet) that is more than 7 days old, look for the newer value of that same series on the "
        "central bank's own site or the source's data page, and always give the date of the value you use.",
        "RECENT RELEASE RULE - before you settle on a figure, check whether the publisher has put out a newer "
        "release in the last two days (for example today's or yesterday's data release or policy decision). If "
        "it has, use the newer release instead of the older one and give its release date.",
        "NO BLANKS RULE - \"data unavailable\" is the last resort, not the first. If the named page cannot be read "
        "or does not show the figure, open the same series on the publisher's own data page, statistical tables "
        "or latest press release, use the latest figure that shows it with its date, and say which page you "
        "used. Write \"data unavailable\" (score 0) only when no official page shows it, and say in the analysis "
        "what you tried.",
        "YEAR-ON-YEAR RULE - where a measure or score guide says year-on-year, annual or \"over the last year\", "
        "compare the latest period with the same period one year earlier (same month, quarter or date) and give "
        "both figures and both dates. Do not use the change from the previous month or quarter.",
        "BOUNDARY RULE - in a score guide, \"above\" and \"below\" are strict (a reading equal to the number is "
        "neither above nor below it). If a reading sits exactly on a number shared by two bands (for example "
        "\"from 2% to 3%\" and \"from 3% to 4%\" and the reading is 3.0%), use the milder score, the one closer "
        "to 0. Use the figure exactly as the source publishes it; do not round it or recalculate it unless the "
        "guide asks you to.",
        "CALCULATION RULE - when a guide needs a change or a rate that the source does not print, work it out "
        "yourself from the two published figures as (latest - earlier) / earlier x 100, and show both figures, "
        "both dates and the result to one decimal. Do not take a rate from a news article instead of the source.",
        "SCORE CHECK - after you choose each score, read that indicator's score guide again and check that the "
        "figure you reported sits inside the band you chose. If it does not, correct the score before you answer. "
        "The score comes from the guide's bands, not from the tone of the news.",
    ]
    for code in currencies:
        lines.append(f"{code} ({table[code].get('name', code)}):")
        for ind in table[code]["indicators"]:
            parts = [f"{ind['num']}. {ind['hub_name']} = {ind['measure']}.",
                     f"Source: {ind['source_url']}.",
                     f"Where to find it: {ind['where_to_find']}"]
            series = ind.get("hub_series")
            if series:
                parts.append(f"Data series: FRED {series['series']} - {series['calc']}.")
            parts.append(f"Score guide: {ind['scoring_guide']}")
            lines.append("  " + " ".join(parts))
    lines.append("")
    return "\n".join(lines)
