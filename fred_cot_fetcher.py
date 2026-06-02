    """
F4P MACRO DATA FETCHER
GWL Trading Team — Fishin4Pips System
GitHub Actions script — runs daily (FRED) and weekly Friday (COT)

WHAT THIS SCRIPT DOES:
- Fetches 10 macro indicators from FRED API
- Fetches COT positioning data from CFTC (Fridays only)
- Writes all data to Google Sheet tab: FRED AUTO
- Never touches any other tab in the Sheet

FRED INDICATORS FETCHED:
  1. Federal Funds Rate        — FEDFUNDS
  2. CPI YoY %                — CPIAUCSL (calculated)
  3. Core CPI YoY %           — CPILFESL (calculated)
  4. PPI YoY % Headline       — PPIACO (calculated)
  5. Core PPI YoY %           — PPIFES (calculated)
  6. NFP (Nonfarm Payrolls)   — PAYEMS (MoM change)
  7. Unemployment Rate        — UNRATE
  8. Building Permits         — PERMIT
  9. Michigan Consumer Sent.  — UMCSENT
 10. GDP Growth Rate          — A191RL1Q225SBEA

COT CURRENCIES (Fridays only):
  USD, EUR, GBP, JPY, AUD, CAD, NZD, CHF

SHEET TARGET:
  Spreadsheet ID: 1fhYqdylvYWzU0cdiM2qrkMbv-UeOkWfM
  Tab name: FRED AUTO
  Never writes to any other tab.
"""

import os
import json
import requests
import time
import datetime
import gzip
import csv
import io
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── CONFIGURATION ─────────────────────────────────────────────
FRED_API_KEY       = os.environ['FRED_API_KEY']
GOOGLE_CREDS_JSON  = os.environ['GOOGLE_CREDENTIALS']
SPREADSHEET_ID     = '1fhYqdylvYWzU0cdiM2qrkMbv-UeOkWfM'
TAB_NAME           = 'FRED AUTO'

# ── FRED SERIES MAP ───────────────────────────────────────────
# key: (series_id, calculation, label, row_in_sheet)
# calculation: 'latest' = most recent value
#              'yoy'    = year-over-year % change (current vs 12m ago)
#              'mom'    = month-over-month change (current minus prior)
FRED_SERIES = [
    ('FEDFUNDS',        'latest', 'Federal Funds Rate %',          4),
    ('CPIAUCSL',        'yoy',    'CPI YoY %',                     5),
    ('CPILFESL',        'yoy',    'Core CPI YoY %',                6),
    ('PPIACO',          'yoy',    'PPI Headline YoY %',            7),
    ('PPIFES',          'yoy',    'Core PPI YoY %',                8),
    ('PAYEMS',          'mom',    'NFP MoM Change (000s)',          9),
    ('UNRATE',          'latest', 'Unemployment Rate %',           10),
    ('PERMIT',          'latest', 'Building Permits (000s)',        11),
    ('UMCSENT',         'latest', 'Michigan Consumer Sentiment',   12),
    ('A191RL1Q225SBEA', 'latest', 'GDP Growth Rate %',             13),
]

# ── COT CURRENCY CODES ────────────────────────────────────────
# CFTC legacy futures-only report codes for major FX currencies
COT_CODES = {
    'EUR': '099741',
    'GBP': '096742',
    'JPY': '097741',
    'AUD': '232741',
    'CAD': '090741',
    'NZD': '112741',
    'CHF': '092741',
    'USD': '098662',  # USD Index
}
COT_START_ROW = 17  # COT section starts at row 17 in FRED AUTO tab


def get_fred_value(series_id, calculation):
    """Fetch a value from FRED API."""
    url = (
        f'https://api.stlouisfed.org/fred/series/observations'
        f'?series_id={series_id}'
        f'&api_key={FRED_API_KEY}'
        f'&sort_order=desc'
        f'&limit=14'  # enough for YoY (13 months)
        f'&file_type=json'
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    observations = resp.json().get('observations', [])

    # Filter out missing values
    valid = [o for o in observations if o['value'] != '.']
    if not valid:
        return None, None

    latest = valid[0]
    latest_val = float(latest['value'])
    latest_date = latest['date']

    if calculation == 'latest':
        return round(latest_val, 2), latest_date

    elif calculation == 'yoy':
        # Need value from 12 months ago
        if len(valid) < 13:
            return None, latest_date
        prior_year = valid[12]
        prior_val = float(prior_year['value'])
        yoy = ((latest_val - prior_val) / prior_val) * 100
        return round(yoy, 2), latest_date

    elif calculation == 'mom':
        if len(valid) < 2:
            return None, latest_date
        prior = valid[1]
        prior_val = float(prior['value'])
        change = latest_val - prior_val
        return round(change, 1), latest_date

    return None, latest_date


def get_cot_data():
    """
    Fetch latest COT data from CFTC.
    Returns dict: { 'EUR': {'net': +12345, 'long': 45000, 'short': 32655}, ... }
    CFTC publishes every Friday ~3:30pm ET.
    We fetch the current year's legacy futures-only CSV.
    """
    year = datetime.datetime.now().year
    url = f'https://www.cftc.gov/files/dea/history/fut_fin_xls_{year}.zip'

    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()

        # The zip contains a CSV
        import zipfile
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        csv_name = [n for n in zf.namelist() if n.endswith('.csv') or n.endswith('.CSV')][0]
        raw = zf.read(csv_name).decode('utf-8', errors='replace')

        reader = csv.DictReader(io.StringIO(raw))
        rows = list(reader)

        results = {}
        for ccy, code in COT_CODES.items():
            # Find the most recent row for this contract code
            matching = [r for r in rows if r.get('CFTC_Contract_MarketCode', '').strip() == code
                       or r.get('Market_and_Exchange_Names', '').find(code) >= 0]

            # Sort by date descending and take latest
            matching_sorted = sorted(
                matching,
                key=lambda r: r.get('As_of_Date_In_Form_YYMMDD', '000000'),
                reverse=True
            )

            if matching_sorted:
                row = matching_sorted[0]
                try:
                    # Column names from CFTC legacy format
                    long_pos  = int(row.get('NonComm_Positions_Long_All',  0))
                    short_pos = int(row.get('NonComm_Positions_Short_All', 0))
                    net       = long_pos - short_pos
                    as_of     = row.get('As_of_Date_In_Form_YYMMDD', '')
                    results[ccy] = {
                        'net':   net,
                        'long':  long_pos,
                        'short': short_pos,
                        'date':  as_of
                    }
                except (ValueError, KeyError):
                    results[ccy] = None
            else:
                results[ccy] = None

        return results

    except Exception as e:
        print(f'COT fetch error: {e}')
        return {}


def get_sheets_service():
    """Authenticate with Google Sheets API."""
    creds_info = json.loads(GOOGLE_CREDS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_info,
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    return build('sheets', 'v4', credentials=creds)


def ensure_tab_exists(service):
    """Create FRED AUTO tab if it doesn't exist."""
    sheet = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    existing = [s['properties']['title'] for s in sheet.get('sheets', [])]

    if TAB_NAME not in existing:
        print(f'Creating tab: {TAB_NAME}')
        body = {'requests': [{'addSheet': {'properties': {'title': TAB_NAME}}}]}
        service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID, body=body
        ).execute()
        print(f'Tab created: {TAB_NAME}')
    else:
        print(f'Tab exists: {TAB_NAME}')


def write_headers(service):
    """Write column headers to FRED AUTO tab."""
    timestamp = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    header_data = [
        [f'F4P MACRO DATA — AUTO FETCHED — Last run: {timestamp}'],
        [''],
        ['#', 'INDICATOR', 'VALUE', 'RELEASE DATE', 'SOURCE', 'STATUS'],
    ]
    range_ref = f'{TAB_NAME}!A1:F3'
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=range_ref,
        valueInputOption='RAW',
        body={'values': header_data}
    ).execute()


def write_fred_data(service, results):
    """Write all FRED indicator rows."""
    rows = []
    for i, (series_id, calc, label, sheet_row) in enumerate(FRED_SERIES):
        val, date = results.get(series_id, (None, None))
        status = 'OK' if val is not None else 'FETCH FAILED — enter manually'
        display_val = str(val) if val is not None else 'N/A'
        rows.append([i+1, label, display_val, date or '', f'FRED:{series_id}', status])

    range_ref = f'{TAB_NAME}!A4:F13'
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=range_ref,
        valueInputOption='RAW',
        body={'values': rows}
    ).execute()


def write_cot_data(service, cot_results):
    """Write COT section."""
    today = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    is_friday = datetime.datetime.utcnow().weekday() == 4

    # Section header
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f'{TAB_NAME}!A15:F16',
        valueInputOption='RAW',
        body={'values': [
            [''],
            ['COT POSITIONING (CFTC — Updated Fridays)', '', f'Last COT fetch: {today}', '', '', '']
        ]}
    ).execute()

    if not is_friday:
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f'{TAB_NAME}!A17:F17',
            valueInputOption='RAW',
            body={'values': [['NOTE: COT only fetches on Fridays. Showing last available data above.']]}
        ).execute()
        return

    if not cot_results:
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f'{TAB_NAME}!A17:F17',
            valueInputOption='RAW',
            body={'values': [['COT FETCH FAILED — update manually from cftc.gov or barchart.com/cot']]}
        ).execute()
        return

    # Headers
    cot_header = [['CURRENCY', 'NET POSITION (Large Specs)', 'LONG', 'SHORT', 'AS OF DATE', 'SIGNAL']]
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f'{TAB_NAME}!A{COT_START_ROW}:F{COT_START_ROW}',
        valueInputOption='RAW',
        body={'values': cot_header}
    ).execute()

    cot_rows = []
    for ccy in ['USD', 'EUR', 'GBP', 'JPY', 'AUD', 'CAD', 'NZD', 'CHF']:
        data = cot_results.get(ccy)
        if data:
            net = data['net']
            signal = 'NET LONG' if net > 0 else 'NET SHORT'
            cot_rows.append([ccy, net, data['long'], data['short'], data['date'], signal])
        else:
            cot_rows.append([ccy, 'N/A', 'N/A', 'N/A', '', 'FETCH FAILED'])

    range_ref = f"{TAB_NAME}!A{COT_START_ROW+1}:F{COT_START_ROW+len(cot_rows)}"
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=range_ref,
        valueInputOption='RAW',
        body={'values': cot_rows}
    ).execute()


def main():
    run_time = datetime.datetime.utcnow()
    is_friday = run_time.weekday() == 4
    print(f'F4P Data Fetcher starting — {run_time.strftime("%Y-%m-%d %H:%M UTC")}')
    print(f'Day: {"FRIDAY — COT will also fetch" if is_friday else run_time.strftime("%A — FRED only")}')

    # ── STEP 1: FRED ────────────────────────────────────────────
    fred_results = {}
    for series_id, calc, label, _ in FRED_SERIES:
        print(f'  Fetching FRED: {series_id} ({calc})...')
        try:
            val, date = get_fred_value(series_id, calc)
            fred_results[series_id] = (val, date)
            print(f'    → {val} ({date})')
        except Exception as e:
            print(f'    → FAILED: {e}')
            fred_results[series_id] = (None, None)
        time.sleep(1)  # Avoid FRED rate limiting

    # ── STEP 2: COT (Fridays only) ───────────────────────────────
    cot_results = {}
    if is_friday:
        print('  Fetching COT from CFTC...')
        cot_results = get_cot_data()
        for ccy, data in cot_results.items():
            if data:
                print(f'    {ccy}: net {data["net"]:+,}')
            else:
                print(f'    {ccy}: FAILED')

    # ── STEP 3: WRITE TO SHEET ───────────────────────────────────
    print('Writing to Google Sheet...')
    service = get_sheets_service()
    ensure_tab_exists(service)
    write_headers(service)
    write_fred_data(service, fred_results)
    write_cot_data(service, cot_results)

    print('Done. All data written to FRED AUTO tab.')
    print('Sheet: https://docs.google.com/spreadsheets/d/1fhYqdylvYWzU0cdiM2qrkMbv-UeOkWfM')


if __name__ == '__main__':
    main()

    
