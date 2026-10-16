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
from googleapiclient.errors import HttpError

FRED_API_KEY      = os.environ['FRED_API_KEY']
GOOGLE_CREDS_JSON = os.environ['GOOGLE_CREDENTIALS']
SPREADSHEET_ID    = '18ZgUq7uvyodHSQvreVNoCQFiS7Ks6Gp89CBbIItcPO0'
TAB_NAME          = 'FRED AUTO'

FRED_SERIES = [
    ('FEDFUNDS',        'latest', 'Federal Funds Rate %',        4),
    ('CPIAUCSL',        'yoy',    'CPI YoY %',                   5),
    ('CPILFESL',        'yoy',    'Core CPI YoY %',              6),
    ('PPIFIS',          'yoy',    'PPI Headline YoY %',          7),
    ('WPSFD49116',      'yoy',    'Core PPI YoY %',              8),
    ('PAYEMS',          'mom',    'NFP MoM Change (000s)',        9),
    ('UNRATE',          'latest', 'Unemployment Rate %',         10),
    ('PERMIT',          'latest', 'Building Permits (000s)',      11),
    ('UMCSENT',         'latest', 'Michigan Consumer Sentiment', 12),
    ('A191RL1Q225SBEA', 'latest', 'GDP Growth Rate %',           13),
    # Same series as row 11, but as the change against the same month a year earlier. The Building Permits
    # score guide scores that change, and without it the weekly run only had this month's level to go on
    # and sometimes scored the month-over-month dip instead (15 Oct 2026: +1 by hand, -1 from the Hub).
    ('PERMIT',          'yoy',    'Building Permits YoY %',       14),
]

# Source lineage (Hub enhancement #1): the publishing agency behind each FRED
# series, shown beside every value so a reader can see where it came from.
# FRED itself is only the delivery channel, not the origin of the number.
FRED_SOURCE_AGENCY = {
    'FEDFUNDS':        'Board of Governors of the Federal Reserve System',
    'CPIAUCSL':        'U.S. Bureau of Labor Statistics',
    'CPILFESL':        'U.S. Bureau of Labor Statistics',
    'PPIFIS':          'U.S. Bureau of Labor Statistics',
    'WPSFD49116':      'U.S. Bureau of Labor Statistics',
    'PAYEMS':          'U.S. Bureau of Labor Statistics',
    'UNRATE':          'U.S. Bureau of Labor Statistics',
    'PERMIT':          'U.S. Census Bureau',
    'UMCSENT':         'University of Michigan',
    'A191RL1Q225SBEA': 'U.S. Bureau of Economic Analysis',
}

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


def _months_before(date_str, months):
    """'2026-09-01' shifted back by `months` calendar months -> '2025-09-01' (FRED monthly dates fall on the 1st)."""
    y, m, d = (int(x) for x in date_str.split('-'))
    total = y * 12 + (m - 1) - months
    return '%04d-%02d-%02d' % (total // 12, total % 12 + 1, d)


def calculate_from_observations(observations, calculation):
    """
    Pure calculation on a FRED observations list (newest first). Returns (value, latest_date).

    Year-over-year and month-over-month changes are taken against the reading dated exactly 12 (or 1)
    calendar months earlier, never against "the 13th (or 2nd) reading back". Those are different when a
    month is missing: the BLS never published October 2025 CPI (government shutdown), so FRED has no
    2025-10-01 value, and counting 12 readings back from September 2026 lands on August 2025, a
    13-month change that overstated CPI by about 0.3 points. If the exact month is absent the result is
    None (blank), not a guess.
    """
    valid = [o for o in observations if o.get('value') not in (None, '.')]
    if not valid:
        return None, None
    latest_val = float(valid[0]['value'])
    latest_date = valid[0]['date']
    if calculation == 'latest':
        return round(latest_val, 2), latest_date
    by_date = {o['date']: float(o['value']) for o in valid}
    if calculation == 'yoy':
        prior_val = by_date.get(_months_before(latest_date, 12))
        if prior_val is None or prior_val == 0:
            return None, latest_date
        return round(((latest_val - prior_val) / prior_val) * 100, 2), latest_date
    if calculation == 'mom':
        prior_val = by_date.get(_months_before(latest_date, 1))
        if prior_val is None:
            return None, latest_date
        return round(latest_val - prior_val, 1), latest_date
    return None, latest_date


def get_fred_value(series_id, calculation):
    url = (
        'https://api.stlouisfed.org/fred/series/observations'
        '?series_id=' + series_id +
        '&api_key=' + FRED_API_KEY +
        '&sort_order=desc&limit=24&file_type=json'
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return calculate_from_observations(resp.json().get('observations', []), calculation)


def get_fred_meta(series_id):
    """
    Lineage metadata for one FRED series: FRED's own last-updated timestamp.
    Purely additive - any failure returns None and must never fail the run
    or change a value the Hub already shows.
    """
    try:
        url = (
            'https://api.stlouisfed.org/fred/series'
            '?series_id=' + series_id +
            '&api_key=' + FRED_API_KEY +
            '&file_type=json'
        )
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        seriess = resp.json().get('seriess', [])
        if not seriess:
            return None
        return {'last_updated': seriess[0].get('last_updated', '')}
    except Exception as e:
        print('  -> lineage metadata unavailable for ' + series_id + ': ' + str(e))
        return None


def get_cot_data():
    year = datetime.datetime.now().year
    # Was 'fut_fin_xls_' - confirmed via live debug output (2026-08-04) that
    # this URL genuinely returns a binary Excel (.xls) file, not CSV/text -
    # that's why parsing produced 30,470 garbled rows instead of a real
    # error: csv.DictReader was splitting binary bytes on stray commas,
    # not real structure. CFTC offers the identical report as plain
    # delimited text at this URL instead - that's the one this script
    # actually needs.
    url = 'https://www.cftc.gov/files/dea/history/fut_fin_txt_' + str(year) + '.zip'
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
        # newline='' matters here: without it, StringIO's default newline
        # translation can conflict with how the csv module expects to see
        # embedded \r\n sequences inside quoted multi-line fields, producing
        # exactly the "new-line character seen in unquoted field" error hit
        # on the first real test of this fetch.
        reader = csv.DictReader(io.StringIO(raw, newline=''))
        rows = list(reader)

        # DIAGNOSTIC - every currency came back N/A on the first clean parse,
        # meaning the download+parse worked but COT_CODES matched nothing.
        # This tells us exactly why on the next run instead of guessing again.
        print('[COT DEBUG] Parsed file: ' + csv_name)
        print('[COT DEBUG] Total rows parsed: ' + str(len(rows)))
        # Confirmed via live debug output (2026-08-04): this is genuinely
        # the Traders in Financial Futures (TFF) report, which categorizes
        # by Dealer / Asset Manager / Leveraged Funds / Other Reportables -
        # NOT the Legacy report's NonComm/Comm split this code was
        # originally written against. The correct field for what this
        # system actually wants (matching the Terminal tool's own
        # methodology note: "COT data source upgraded to official CFTC
        # Leveraged Funds figures") is Lev_Money_Positions_*, not
        # NonComm_Positions_*. Also fixed: CFTC_Contract_Market_Code has an
        # underscore before "Code" that the original field name was missing.
        if rows:
            print('[COT DEBUG] Actual column names: ' + str(list(rows[0].keys())))
            sample_codes = [r.get('CFTC_Contract_Market_Code', '<column not found>') for r in rows[:5]]
            print('[COT DEBUG] First 5 CFTC_Contract_Market_Code values: ' + str(sample_codes))

        results = {}
        for ccy, code in COT_CODES.items():
            matching = [r for r in rows if code in r.get('CFTC_Contract_Market_Code', '')]
            matching_sorted = sorted(matching, key=lambda r: r.get('As_of_Date_In_Form_YYMMDD', ''), reverse=True)
            if matching_sorted:
                row = matching_sorted[0]
                try:
                    long_pos = int(row.get('Lev_Money_Positions_Long_All', 0))
                    short_pos = int(row.get('Lev_Money_Positions_Short_All', 0))
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


def write_to_sheet(service, fred_results, cot_results, is_friday, fred_meta=None):
    fred_meta = fred_meta or {}
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
        val, date = fred_results.get(label, (None, None))
        status = 'OK' if val is not None else 'FETCH FAILED - enter manually'
        display_val = str(val) if val is not None else 'N/A'
        rows.append([i + 1, label, display_val, date or '', 'FRED:' + series_id, status])

    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=TAB_NAME + '!A4:F14',
        valueInputOption='RAW',
        body={'values': rows}
    ).execute()

    # Source lineage columns (G:I), written beside the existing A:F block.
    # Columns A:F are untouched, so everything that reads them (the weekly
    # update, the macro overlay formulas) behaves exactly as before.
    # NOTE: the existing 'RELEASE DATE' column (D) holds the observation
    # period date from FRED, not the publication date; the new columns
    # below add the missing pieces rather than relabelling D.
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=TAB_NAME + '!G3:I3',
        valueInputOption='RAW',
        body={'values': [['SOURCE AGENCY', 'FRED LAST UPDATED', 'RETRIEVED (UTC)']]}
    ).execute()
    lineage_rows = []
    for series_id, calc, label, sheet_row in FRED_SERIES:
        meta = fred_meta.get(series_id) or {}
        lineage_rows.append([
            FRED_SOURCE_AGENCY.get(series_id, ''),
            meta.get('last_updated', ''),
            timestamp if fred_results.get(label, (None, None))[0] is not None else '',
        ])
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=TAB_NAME + '!G4:I14',
        valueInputOption='RAW',
        body={'values': lineage_rows}
    ).execute()

    # COT section header - only touches columns A/B here. Column C ("Last
    # COT fetch: <date>") used to get stamped with today's date on every
    # single run, Mon-Fri, regardless of whether COT was actually fetched -
    # so a Monday run could make 6-day-stale COT data (last real fetch was
    # Friday) look same-day-fresh. That's a Cardinal Rule risk: COT must
    # confirm before a trade, and a mislabeled date could pass as
    # confirmation when it isn't. Fix: the date only gets written further
    # down, and only on the code path where a fetch actually just succeeded.
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=TAB_NAME + '!A15:B16',
        valueInputOption='RAW',
        body={'values': [
            [''],
            ['COT POSITIONING (CFTC - Updated Fridays)', '']
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

    # Fetch actually succeeded (is_friday and cot_results both true) - safe
    # to stamp today's date now.
    today = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=TAB_NAME + '!C16',
        valueInputOption='RAW',
        body={'values': [['Last COT fetch: ' + today]]}
    ).execute()

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
    fred_meta = {}
    for series_id, calc, label, _ in FRED_SERIES:
        print('Fetching FRED: ' + series_id + ' (' + calc + ')...')
        try:
            val, date = get_fred_value(series_id, calc)
            fred_results[label] = (val, date)
            print('  -> ' + str(val) + ' (' + str(date) + ')')
        except Exception as e:
            print('  -> FAILED: ' + str(e))
            fred_results[label] = (None, None)
        time.sleep(1)
        if series_id not in fred_meta:  # PERMIT appears twice (level and year-over-year); one lookup is enough
            fred_meta[series_id] = get_fred_meta(series_id)
            time.sleep(1)

    cot_results = {}
    if is_friday:
        print('Fetching COT from CFTC...')
        cot_results = get_cot_data()

    print('Writing to Google Sheet...')
    service = get_sheets_service()

    # Google's Sheets API occasionally returns transient 5xx/429 errors that
    # have nothing to do with our data, auth, or permissions - just a brief
    # hiccup on Google's end (confirmed live: run #99 died on a clean 503
    # from spreadsheets().get() even though every FRED value had already
    # fetched fine). Retry with backoff instead of letting one blip fail
    # the whole day's write.
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        try:
            ensure_tab_exists(service)
            write_to_sheet(service, fred_results, cot_results, is_friday, fred_meta)
            break
        except HttpError as e:
            status = getattr(e.resp, 'status', None)
            retryable = status in (429, 500, 502, 503, 504)
            if retryable and attempt < max_attempts:
                wait = 10 * attempt
                print('  -> Sheets API error (' + str(status) + '), retrying in '
                      + str(wait) + 's (attempt ' + str(attempt) + '/' + str(max_attempts) + ')...')
                time.sleep(wait)
                continue
            raise

    print('Done. Check FRED AUTO tab in your Sheet.')


if __name__ == '__main__':
    main()
