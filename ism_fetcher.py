"""
FISHIN4PIPS — Macro Data Auto-Fetcher via FRED API
Fully automated: Fed Rate, GDP, Unemployment, Building Permits, Michigan Sentiment
Manual update: ISM Manufacturing PMI, ISM Services PMI (once a month, 2 minutes)
"""

import os
import csv
import json
import logging
import requests
from datetime import datetime
from pathlib import Path

# ── CONFIG ─────────────────────────────────────────────────────
FRED_API_KEY  = os.environ.get("FRED_API_KEY", "")
FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

# FRED series — all verified working with free API key
FRED_SERIES = {
    "FED_RATE":        {"series_id": "FEDFUNDS",        "label": "Federal Funds Rate",         "unit": "%"},
    "GDP":             {"series_id": "A191RL1Q225SBEA", "label": "GDP Growth Rate",             "unit": "% SAAR"},
    "UNEMPLOYMENT":    {"series_id": "UNRATE",           "label": "Unemployment Rate",           "unit": "%"},
    "BUILDING_PERMITS":{"series_id": "PERMIT",           "label": "Building Permits",            "unit": "K SAAR"},
    "UMICH":           {"series_id": "UMCSENT",          "label": "Michigan Consumer Sentiment", "unit": "Index"},
    "GOV_SPENDING":    {"series_id": "FGEXPND",          "label": "Government Spending",         "unit": "$B"},
}

# ISM — manual update only (no public API available on free tier)
# !! UPDATE THESE TWO VALUES EACH MONTH !!
ISM_MANUAL = {
    "ISM_MFG": {
        "label":         "ISM Manufacturing PMI",
        "latest_value":  52.7,          # <-- UPDATE after 1st business day
        "previous_value":52.7,
        "latest_date":   "2026-05-01",  # <-- UPDATE date
        "source":        "ISM World (Manual)",
    },
    "ISM_SVC": {
        "label":         "ISM Services PMI",
        "latest_value":  53.6,          # <-- UPDATE after 3rd business day
        "previous_value":54.0,
        "latest_date":   "2026-05-05",  # <-- UPDATE date
        "source":        "ISM World (Manual)",
    },
}

BASE_DIR   = Path(__file__).parent
DATA_FILE  = BASE_DIR / "data" / "ism_data.csv"
LOG_FILE   = BASE_DIR / "logs" / "fetcher.log"
STATE_FILE = BASE_DIR / "data" / "last_fetch_state.json"

BASE_DIR.joinpath("logs").mkdir(parents=True, exist_ok=True)
BASE_DIR.joinpath("data").mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("F4P-MACRO")


# ── FRED FETCH ──────────────────────────────────────────────────
def fetch_fred(series_id: str) -> dict | None:
    if not FRED_API_KEY:
        log.error("FRED_API_KEY not set")
        return None

    params = {
        "series_id": series_id,
        "api_key":   FRED_API_KEY,
        "file_type": "json",
    }

    try:
        resp = requests.get(FRED_BASE_URL, params=params, timeout=15)
        log.info(f"FRED {series_id}: HTTP {resp.status_code}")

        if resp.status_code != 200:
            log.error(f"FRED {series_id} error: {resp.text[:200]}")
            return None

        data = resp.json()
        obs  = [o for o in data.get("observations", []) if o["value"] != "."]
        if not obs:
            log.warning(f"No data for {series_id}")
            return None

        obs.sort(key=lambda x: x["date"], reverse=True)
        latest   = float(obs[0]["value"])
        previous = float(obs[1]["value"]) if len(obs) > 1 else None
        mom      = round(latest - previous, 2) if previous else None

        log.info(f"FRED {series_id}: latest={latest} ({obs[0]['date']})")
        return {
            "latest_value":   latest,
            "previous_value": previous,
            "mom_change":     mom,
            "latest_date":    obs[0]["date"],
            "previous_date":  obs[1]["date"] if len(obs) > 1 else None,
            "status":         "AUTO",
        }

    except Exception as e:
        log.error(f"FRED fetch error {series_id}: {e}")
        return None


# ── SCORING ─────────────────────────────────────────────────────
def score_indicator(key: str, value: float) -> dict:
    if key == "ISM_MFG":
        if value >= 55:   return {"inf": "INFLATIONARY", "bias": "BULLISH",  "score": 6}
        elif value >= 50: return {"inf": "INFLATIONARY", "bias": "BULLISH",  "score": 4}
        elif value >= 48: return {"inf": "NEUTRAL",      "bias": "NEUTRAL",  "score": -1}
        elif value >= 45: return {"inf": "DEFLATIONARY", "bias": "BEARISH",  "score": -4}
        else:             return {"inf": "DEFLATIONARY", "bias": "BEARISH",  "score": -7}

    elif key == "ISM_SVC":
        if value >= 56:   return {"inf": "INFLATIONARY", "bias": "BULLISH",  "score": 6}
        elif value >= 52: return {"inf": "INFLATIONARY", "bias": "BULLISH",  "score": 4}
        elif value >= 50: return {"inf": "NEUTRAL",      "bias": "NEUTRAL",  "score": 2}
        elif value >= 47: return {"inf": "DEFLATIONARY", "bias": "BEARISH",  "score": -3}
        else:             return {"inf": "DEFLATIONARY", "bias": "BEARISH",  "score": -7}

    elif key == "FED_RATE":
        if value >= 5:    return {"inf": "INFLATIONARY", "bias": "BULLISH",  "score": 8}
        elif value >= 4:  return {"inf": "INFLATIONARY", "bias": "BULLISH",  "score": 6}
        elif value >= 3:  return {"inf": "NEUTRAL",      "bias": "BULLISH",  "score": 4}
        elif value >= 2:  return {"inf": "NEUTRAL",      "bias": "NEUTRAL",  "score": 1}
        else:             return {"inf": "DEFLATIONARY", "bias": "BEARISH",  "score": -4}

    elif key == "GDP":
        if value >= 4:    return {"inf": "INFLATIONARY", "bias": "BULLISH",  "score": 7}
        elif value >= 2.5:return {"inf": "INFLATIONARY", "bias": "BULLISH",  "score": 5}
        elif value >= 1.5:return {"inf": "NEUTRAL",      "bias": "BULLISH",  "score": 3}
        elif value >= 0:  return {"inf": "NEUTRAL",      "bias": "NEUTRAL",  "score": -1}
        else:             return {"inf": "DEFLATIONARY", "bias": "BEARISH",  "score": -6}

    elif key == "UNEMPLOYMENT":
        if value <= 3.5:  return {"inf": "INFLATIONARY", "bias": "BULLISH",  "score": 5}
        elif value <= 4.0:return {"inf": "NEUTRAL",      "bias": "BULLISH",  "score": 3}
        elif value <= 4.5:return {"inf": "NEUTRAL",      "bias": "NEUTRAL",  "score": 0}
        elif value <= 5.0:return {"inf": "DEFLATIONARY", "bias": "BEARISH",  "score": -3}
        else:             return {"inf": "DEFLATIONARY", "bias": "BEARISH",  "score": -6}

    elif key == "BUILDING_PERMITS":
        if value >= 1600: return {"inf": "INFLATIONARY", "bias": "BULLISH",  "score": 4}
        elif value >= 1400:return{"inf": "NEUTRAL",      "bias": "NEUTRAL",  "score": 2}
        elif value >= 1200:return{"inf": "NEUTRAL",      "bias": "NEUTRAL",  "score": 0}
        elif value >= 1000:return{"inf": "DEFLATIONARY", "bias": "NEUTRAL",  "score": -2}
        else:             return {"inf": "DEFLATIONARY", "bias": "BEARISH",  "score": -4}

    elif key == "UMICH":
        if value >= 90:   return {"inf": "INFLATIONARY", "bias": "BULLISH",  "score": 6}
        elif value >= 75: return {"inf": "NEUTRAL",      "bias": "BULLISH",  "score": 3}
        elif value >= 60: return {"inf": "NEUTRAL",      "bias": "NEUTRAL",  "score": 1}
        elif value >= 50: return {"inf": "NEUTRAL",      "bias": "NEUTRAL",  "score": -2}
        elif value >= 40: return {"inf": "DEFLATIONARY", "bias": "BEARISH",  "score": -5}
        else:             return {"inf": "DEFLATIONARY", "bias": "BEARISH",  "score": -8}

    elif key == "GOV_SPENDING":
        return {"inf": "INFLATIONARY", "bias": "NEUTRAL", "score": 1}

    return {"inf": "NEUTRAL", "bias": "NEUTRAL", "score": 0}


# ── CSV ─────────────────────────────────────────────────────────
CSV_HEADERS = [
    "indicator_key", "indicator_label", "latest_value", "previous_value",
    "mom_change", "latest_date", "previous_date", "inf_neu_def", "usd_bias",
    "score", "source", "series_id", "status", "fetch_timestamp",
]

def load_csv() -> dict:
    if not DATA_FILE.exists():
        return {}
    rows = {}
    with open(DATA_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows[row["indicator_key"]] = row
    return rows

def save_csv(data: dict) -> None:
    with open(DATA_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        w.writeheader()
        for row in data.values():
            w.writerow(row)
    log.info(f"CSV saved: {DATA_FILE}")


# ── MAIN ────────────────────────────────────────────────────────
def run_fetch() -> dict:
    log.info("=" * 60)
    log.info("F4P Macro Fetcher — starting run")
    log.info(f"Timestamp: {datetime.utcnow().isoformat()}")
    log.info("=" * 60)

    data_store = load_csv()
    summary    = {"updated": [], "skipped": [], "errors": []}
    ts         = datetime.utcnow().isoformat()

    # ── 1. FRED AUTO SERIES ──────────────────────────────────────
    for key, cfg in FRED_SERIES.items():
        log.info(f"--- Fetching {cfg['label']} ({cfg['series_id']}) ---")
        result = fetch_fred(cfg["series_id"])

        if not result:
            summary["errors"].append(key)
            continue

        existing_date = data_store.get(key, {}).get("latest_date", "")
        if existing_date == result["latest_date"]:
            log.info(f"{key}: No new data (still {result['latest_date']})")
            summary["skipped"].append(key)
            continue

        scored = score_indicator(key, result["latest_value"])
        data_store[key] = {
            "indicator_key":   key,
            "indicator_label": cfg["label"],
            "latest_value":    result["latest_value"],
            "previous_value":  result["previous_value"] or "",
            "mom_change":      result["mom_change"] or "",
            "latest_date":     result["latest_date"],
            "previous_date":   result["previous_date"] or "",
            "inf_neu_def":     scored["inf"],
            "usd_bias":        scored["bias"],
            "score":           scored["score"],
            "source":          f"FRED ({cfg['series_id']})",
            "series_id":       cfg["series_id"],
            "status":          "AUTO",
            "fetch_timestamp": ts,
        }
        log.info(f"UPDATED {cfg['label']}: {result['latest_value']} | {scored['bias']} | Score {scored['score']:+d}")
        summary["updated"].append(key)

    # ── 2. ISM MANUAL ENTRIES ────────────────────────────────────
    for key, cfg in ISM_MANUAL.items():
        log.info(f"--- Loading {cfg['label']} (MANUAL) ---")
        scored = score_indicator(key, cfg["latest_value"])

        existing_date = data_store.get(key, {}).get("latest_date", "")
        status = "MANUAL"
        if existing_date == cfg["latest_date"]:
            log.info(f"{key}: Manual value unchanged ({cfg['latest_date']})")
            summary["skipped"].append(key)
        else:
            summary["updated"].append(key)

        data_store[key] = {
            "indicator_key":   key,
            "indicator_label": cfg["label"],
            "latest_value":    cfg["latest_value"],
            "previous_value":  cfg["previous_value"],
            "mom_change":      round(cfg["latest_value"] - cfg["previous_value"], 2),
            "latest_date":     cfg["latest_date"],
            "previous_date":   "",
            "inf_neu_def":     scored["inf"],
            "usd_bias":        scored["bias"],
            "score":           scored["score"],
            "source":          cfg["source"],
            "series_id":       "MANUAL",
            "status":          "MANUAL",
            "fetch_timestamp": ts,
        }
        log.info(f"ISM {cfg['label']}: {cfg['latest_value']} | {scored['bias']} | Score {scored['score']:+d} | Status: MANUAL")

    save_csv(data_store)

    log.info("── RUN SUMMARY ──────────────────────────────────────")
    log.info(f"  AUTO Updated : {len([x for x in summary['updated'] if x not in ISM_MANUAL])}")
    log.info(f"  MANUAL Loaded: {len(ISM_MANUAL)}")
    log.info(f"  Skipped      : {len(summary['skipped'])}")
    log.info(f"  Errors       : {len(summary['errors'])}")
    log.info("=" * 60)
    return summary

if __name__ == "__main__":
    result = run_fetch()
    exit(1 if result.get("errors") else 0)
