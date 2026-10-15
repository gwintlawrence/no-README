#!/usr/bin/env python3
"""
Builds prompt_pack.json: the exact prompts the weekly Hub run sends to the AI, so a person can run the
same ones by hand and compare.

It calls the pipeline's own prompt builders (f4p_weekly_update.build_*_prompt) with a date placeholder, so
the pack cannot drift from what the Hub really asks. tests/test_data_spec.py fails if prompt_pack.json is
out of date. After changing f4p_data_spec.json or any prompt, run:

    python build_prompt_pack.py

The Data Hunt tool reads prompt_pack.json and f4p_data_spec.json when it is built.
"""
import json
import os
import sys

import f4p_weekly_update as w
from f4p_spec import load_spec

DATE_TOKEN = "{{DATE}}"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "prompt_pack.json")


def build():
    spec = load_spec()
    names = {c: spec["endogenous"]["currencies"][c]["name"] for c in w.CURRENCIES}
    prompts = []
    for code in w.CURRENCIES:  # the Hub sends one currency (15 indicators) per call
        prompts.append({
            "id": f"endo-{code}",
            "section": "Endogenous",
            "label": f"{code} - {names[code]} (15 indicators)",
            "currency": code,
            "text": w.build_prompt(DATE_TOKEN, [code]),
        })
    prompts.append({
        "id": "carry", "section": "Carry trade",
        "label": f"Carry trade ({len(w.CARRY_PAIRS)} pairs)", "currency": None,
        "text": w.build_carry_prompt(DATE_TOKEN, w.CARRY_PAIRS),
    })
    prompts.append({
        "id": "central_bank", "section": "Central banks",
        "label": f"Central banks ({len(w.CENTRAL_BANKS)} banks)", "currency": None,
        "text": w.build_central_bank_prompt(DATE_TOKEN, w.CENTRAL_BANKS),
    })
    prompts.append({
        "id": "exogenous", "section": "Exogenous drivers",
        "label": f"Exogenous drivers ({len(w.CURRENCIES)} currencies)", "currency": None,
        "text": w.build_exogenous_prompt(DATE_TOKEN, w.CURRENCIES),
    })
    return {
        "version": spec.get("version"),
        "date_token": DATE_TOKEN,
        "date_format": "%B %d, %Y",
        "note": ("These are the same prompts the weekly Hub run sends. Paste one into an AI that can search the web, "
                 "keep web search on, and ask it to follow the prompt exactly."),
        "prompts": prompts,
    }


def dumps(pack):
    return json.dumps(pack, indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    pack = build()
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(dumps(pack))
    print(f"wrote {OUT}: {len(pack['prompts'])} prompts")
