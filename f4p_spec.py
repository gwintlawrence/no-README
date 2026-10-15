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
