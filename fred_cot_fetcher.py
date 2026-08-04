import os
import json
import requests
import time
import datetime
import io
import csv
import zipfile
import argparse
from google.oauth2 import service_account
from googleapiclient.discovery import build

FRED_API_KEY      = os.environ['FRED_API_KEY']
GOOGLE_CREDS_JSON = os.environ['GOOGLE_CREDENTIALS']
SPREADSHEET_ID    = '18ZgUq7uvyodHSQvreVNoCQFiS7Ks6Gp89CBbIItcPO0'
TAB_NAME          = 'FRED AUTO'

FRED_SERIES = [
    ('FEDFUNDS',        'latest', 'Federal Funds Rate %',        4),
    ('CPIAUCSL',        'yoy',    'CPI YoY %',                   5),
    ('CPILFESL',        'yoy',    'Core CPI YoY %',              6),
    ('PPIACO',          'yoy',    'PPI Headline YoY %',          7),
    ('PPIFES',          'yoy',    'Core PPI YoY %',              8),
    ('PAYEMS',          'mom',    'NFP MoM Change (000s)',        9),
    ('UNRATE',          'latest', 'Unemployment Rate %',         10),
    ('PERMIT',          'latest', 'Building Permits (000s)',      11),
    ('UMCSENT',         'latest', 'Michigan Consumer Sentiment', 12),
    ('A191RL1Q225SBEA', 'latest', 'GDP Growth Rate %',           13),
]

COT_CODES = {
    'USD': '098662',
    'EUR': '099741',
    'GBP': '096742',
    'JPY': '097741',
    'AUD': '232741',
    'CAD': '090741',
    'NZD': '112741',
    'CHF': '092741',
}
COT_START_ROW = 17


def get_fred_value(series_id, calculation):
    url = (
        'https://api.stlouisfed.org/fred/series/observations'
        '?series_id=' + series_id +
        '&api_key=' + FRED_API_KEY +
        '&sort_order=desc&limit=14&file_type=json'
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    observations = resp.json().get('observations', [])
    valid = [o for o in observations if o['value'] != '.']
    if not valid:
        return None, None
    latest_val = float(valid[0]['value'])
    latest_date = valid[0]['date']
    if calculation == 'latest':
        return round(latest_val, 2), latest_date
    elif calculation == 'yoy':
        if len(valid) < 13:
            return None, latest_date
        prior_val = float(valid[12]['value'])
        yoy = ((latest_val - prior_val) / prior_val) * 100
        return round(yoy, 2), latest_date
    elif calculation == 'mom':
        if len(valid) < 2:
            return None, latest_date
        prior_val = float(valid[1]['value'])
        return round(latest_val - prior_val, 1), latest_date
    return None, latest_date


def get_cot_data():
    year = datetime.datetime.now().year
    url = 'https://www.cftc.gov/files/dea/history/fut_fin_xls_' + str(year) + '.zip'
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        # Was: [n for n in zf.namelist() if n.lower().endswith('.csv')][0]
        # That raised "list index out of range" every Friday - CFTC's zip
        # apparently no longer contains a file ending in exactly '.csv'.
        # These bulk files always contain exactly one data file regardless
        # of its extension, so check both known extensions and fall back to
        # "whatever's actually in there" rather than assuming one name.
        data_files = [n for n in zf.namelist() if n.lower().endswith(('.csv', '.txt'))]
        if not data_files:
            data_files = zf.namelist()
        csv_name = data_files[0]
        raw = zf.read(csv_name).decode('utf-8', errors='replace')
        reader = csv.DictReader(io.StringIO(raw))
        rows = list(reader)
        results = {}
        for ccy, code in COT_CODES.items():
            matching = [r for r in rows if code in r.get('CFTC_Contract_MarketCode', '')]
            matching_sorted = sorted(matching, key=lambda r: r.get('As_of_Date_In_Form_YYMMDD', ''), reverse=True)
            if matching_sorted:
                row = matching_sorted[0]
                try:
                    long_pos = int(row.get('NonComm_Positions_Long_All', 0))
                    short_pos = int(row.get('NonComm_Positions_Short_All', 0))
                    net = long_pos - short_pos
                    results[ccy] = {'net': net, 'long': long_pos, 'short': short_pos, 'date': row.get('As_of_Date_In_Form_YYMMDD', '')}
                except (ValueError, KeyError):
                    results[ccy] = None
            else:
                results[ccy] = None
        return results
    except Exception as e:
        print('COT fetch error: ' + str(e))
        return {}


def get_sheets_service():
    creds_info = json.loads(GOOGLE_CREDS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_info,
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    return build('sheets', 'v4', credentials=creds)


def ensure_tab_exists(service):
    sheet = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    existing = [s['properties']['title'] for s in sheet.get('sheets', [])]
    if TAB_NAME not in existing:
        body = {'requests': [{'addSheet': {'properties': {'title': TAB_NAME}}}]}
        service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body=body).execute()
        print('Tab created: ' + TAB_NAME)
    else:
        print('Tab exists: ' + TAB_NAME)


def write_to_sheet(service, fred_results, cot_results, is_friday):
    timestamp = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')

    # Headers
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=TAB_NAME + '!A1:F3',
        valueInputOption='RAW',
        body={'values': [
            ['F4P MACRO DATA — AUTO FETCHED — Last run: ' + timestamp],
            [''],
            ['#', 'INDICATOR', 'VALUE', 'RELEASE DATE', 'SOURCE', 'STATUS']
        ]}
    ).execute()

    # FRED rows
    rows = []
    for i, (series_id, calc, label, sheet_row) in enumerate(FRED_SERIES):
        val, date = fred_results.get(series_id, (None, None))
        status = 'OK' if val is not None else 'FETCH FAILED - enter manually'
        display_val = str(val) if val is not None else 'N/A'
        rows.append([i + 1, label, display_val, date or '', 'FRED:' + series_id, status])

    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=TAB_NAME + '!A4:F13',
        valueInputOption='RAW',
        body={'values': rows}
    ).execute()

    # COT section header
    today = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=TAB_NAME + '!A15:F16',
        valueInputOption='RAW',
        body={'values': [
            [''],
            ['COT POSITIONING (CFTC - Updated Fridays)', '', 'Last COT fetch: ' + today, '', '', '']
        ]}
    ).execute()

    if not is_friday:
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=TAB_NAME + '!A17:F17',
            valueInputOption='RAW',
            body={'values': [['NOTE: COT only fetches on Fridays. Showing last available data above.']]}
        ).execute()
        return

    if not cot_results:
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=TAB_NAME + '!A17:F17',
            valueInputOption='RAW',
            body={'values': [['COT FETCH FAILED - update manually from cftc.gov or barchart.com/cot']]}
        ).execute()
        return

    cot_header = [['CURRENCY', 'NET POSITION', 'LONG', 'SHORT', 'AS OF DATE', 'SIGNAL']]
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=TAB_NAME + '!A' + str(COT_START_ROW) + ':F' + str(COT_START_ROW),
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

    end_row = COT_START_ROW + len(cot_rows)
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=TAB_NAME + '!A' + str(COT_START_ROW + 1) + ':F' + str(end_row),
        valueInputOption='RAW',
        body={'values': cot_rows}
    ).execute()


def main():
    parser = argparse.ArgumentParser(description="F4P free macro + COT data fetcher.")
    parser.add_argument(
        "--force-cot", action="store_true",
        help="Attempt the COT fetch regardless of what day it is - lets you test a COT-related "
             "fix immediately instead of waiting for a real Friday. Normal scheduled runs never "
             "need this; it's a manual testing aid only.",
    )
    args = parser.parse_args()

    run_time = datetime.datetime.utcnow()
    is_friday = run_time.weekday() == 4 or args.force_cot
    print('F4P Data Fetcher starting: ' + run_time.strftime('%Y-%m-%d %H:%M UTC')
          + (' (COT forced on)' if args.force_cot and run_time.weekday() != 4 else ''))

    fred_results = {}
    for series_id, calc, label, _ in FRED_SERIES:
        print('Fetching FRED: ' + series_id + ' (' + calc + ')...')
        try:
            val, date = get_fred_value(series_id, calc)
            fred_results[series_id] = (val, date)
            print('  -> ' + str(val) + ' (' + str(date) + ')')
        except Exception as e:
            print('  -> FAILED: ' + str(e))
            fred_results[series_id] = (None, None)
        time.sleep(1)

    cot_results = {}
    if is_friday:
        print('Fetching COT from CFTC...')
        cot_results = get_cot_data()

    print('Writing to Google Sheet...')
    service = get_sheets_service()
    ensure_tab_exists(service)
    write_to_sheet(service, fred_results, cot_results, is_friday)
    print('Done. Check FRED AUTO tab in your Sheet.')


main()

    
