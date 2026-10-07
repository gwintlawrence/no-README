"""
F4P Freshness Status  (Hub enhancement #2)

Gives every data feed an honest badge: CURRENT, DELAYED, STALE or UNKNOWN,
based on the timestamp the pipeline itself wrote. The overall Hub badge is
the WORST component, so a green badge can never sit over a warning.

Writes a 'HUB STATUS' tab to the Master Macro Scorecard that the Hub front
end can read. It does NOT run on a schedule by itself: wire it into an
existing workflow step (never add a new Saturday cron; see the race-condition
fix in f4p_equities_master_update.yml).

Usage:   python f4p_freshness_status.py            # read, classify, write tab
         python f4p_freshness_status.py --dry-run  # print only, no write
Env:     GOOGLE_CREDENTIALS (same secret the other scripts use)

Only feeds whose layout is confirmed in the repo are listed in SOURCES.

Equities: the weekly Alpha Vantage step appends a dated row per ticker to the
OPTIONS FLOW & IV tab of the separate Equities spreadsheet (secret
EQUITIES_SHEET_ID), so the NEWEST date in its column A is when that step last
ran. If EQUITIES_SHEET_ID is not available the feed is UNKNOWN, never CURRENT.
The Claude research step (qualitative) has no timestamp cell of its own and is
not covered.
"""

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone

SPREADSHEET_ID = '18ZgUq7uvyodHSQvreVNoCQFiS7Ks6Gp89CBbIItcPO0'   # Master Macro Scorecard
STATUS_TAB = 'HUB STATUS'

RANK = {'CURRENT': 0, 'DELAYED': 1, 'UNKNOWN': 2, 'STALE': 3}

# kind 'run_stamp': text containing "Last run: YYYY-MM-DD HH:MM UTC" in one cell.
# kind 'as_of_dates': a column of dates; the OLDEST one decides the status.
SOURCES = [
    {'name': 'FRED macro data', 'tab': 'FRED AUTO', 'range': 'A1', 'kind': 'run_stamp',
     'current_hours': 36, 'stale_hours': 72},                 # fetched daily
    {'name': 'COT positioning', 'tab': 'FRED AUTO', 'range': 'E18:E25', 'kind': 'as_of_dates',
     'current_hours': 10 * 24, 'stale_hours': 17 * 24},       # weekly, as-of is the prior Tuesday
    # kind 'latest_date': history accumulates down the column; the NEWEST date decides.
    {'name': 'Equities weekly run', 'tab': 'OPTIONS FLOW & IV', 'range': 'A2:A5000',
     'kind': 'latest_date', 'spreadsheet_env': 'EQUITIES_SHEET_ID',
     'current_hours': 9 * 24, 'stale_hours': 16 * 24},        # runs Saturdays
]

# The FX decision screens (Gate Check) only care about the FX feeds, which all
# live in the Master Macro Scorecard. A stale equities feed must not mark an FX
# idea, but it does count toward the overall Hub badge.
FX_SOURCES = [s for s in SOURCES if 'spreadsheet_env' not in s]

# '%y%m%d' is the COT AS OF DATE format on FRED AUTO (e.g. 260922 = 2026-09-22).
_DATE_FORMATS = ('%Y-%m-%d', '%m/%d/%Y', '%d %b %Y', '%Y/%m/%d', '%y%m%d')


def parse_run_stamp(text):
    m = re.search(r'Last run:\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*UTC', text or '')
    if not m:
        return None
    return datetime.strptime(m.group(1) + ' ' + m.group(2), '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc)


_SHEETS_EPOCH = datetime(1899, 12, 30)


def parse_date(text):
    # Sheets date cells read unformatted arrive as serial numbers (locale-proof).
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        if 40000 <= text < 80000:
            return (_SHEETS_EPOCH + timedelta(days=float(text))).replace(
                hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        return None
    t = (text or '').strip()
    if re.fullmatch(r'\d{5}(\.\d+)?', t):
        return parse_date(float(t))
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def classify(timestamp, now, current_hours, stale_hours):
    """CURRENT / DELAYED / STALE, or UNKNOWN when there is no usable timestamp."""
    if timestamp is None:
        return 'UNKNOWN'
    age_h = (now - timestamp).total_seconds() / 3600.0
    if age_h <= current_hours:
        return 'CURRENT'
    if age_h <= stale_hours:
        return 'DELAYED'
    return 'STALE'


def overall(statuses):
    """Worst component wins. Empty input is UNKNOWN, never CURRENT."""
    if not statuses:
        return 'UNKNOWN'
    return max(statuses, key=lambda s: RANK[s])


def source_timestamp(source, values):
    """values: list of cell strings from the source's range."""
    cells = [c for c in values if c is not None and str(c).strip()]
    if source['kind'] == 'run_stamp':
        return parse_run_stamp(cells[0]) if cells else None
    if source['kind'] == 'latest_date':
        # Unreadable cells are ignored: that can only make the feed look OLDER
        # (stale), never fresher than it is. No readable date at all is UNKNOWN.
        good = [d for d in (parse_date(c) for c in cells) if d is not None]
        return max(good) if good else None
    dates = [parse_date(c) for c in cells]
    if not dates or any(d is None for d in dates):
        return None            # one unreadable date makes the whole feed UNKNOWN
    return min(dates)          # the oldest date decides


def evaluate(sources, read_range, now=None):
    """read_range(tab, a1_range) -> flat list of cell strings."""
    now = now or datetime.now(timezone.utc)
    rows = []
    for s in sources:
        try:
            if s.get('spreadsheet_env'):
                cells = read_range(s['tab'], s['range'], s['spreadsheet_env'])
            else:
                cells = read_range(s['tab'], s['range'])
            ts = source_timestamp(s, cells)
        except Exception:
            ts = None
        rows.append({
            'name': s['name'],
            'status': classify(ts, now, s['current_hours'], s['stale_hours']),
            'as_of': ts.strftime('%Y-%m-%d %H:%M UTC') if ts else '',
            'tab': s['tab'],
        })
    return {'overall': overall([r['status'] for r in rows]),
            'checked_at': now.strftime('%Y-%m-%d %H:%M UTC'), 'sources': rows}


def _service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ['GOOGLE_CREDENTIALS']),
        scopes=['https://www.googleapis.com/auth/spreadsheets'])
    return build('sheets', 'v4', credentials=creds)


def main():
    ap = argparse.ArgumentParser(description='F4P Hub freshness status')
    ap.add_argument('--dry-run', action='store_true', help='print the result, write nothing')
    args = ap.parse_args()

    svc = _service()

    def read_range(tab, a1, spreadsheet_env=None):
        if spreadsheet_env:
            sheet_id = os.environ.get(spreadsheet_env, '').strip()
            if not sheet_id:
                raise RuntimeError(spreadsheet_env + ' is not set')       # evaluate() reports UNKNOWN
            resp = svc.spreadsheets().values().get(
                spreadsheetId=sheet_id, range="'" + tab + "'!" + a1,
                valueRenderOption='UNFORMATTED_VALUE', dateTimeRenderOption='SERIAL_NUMBER').execute()
        else:
            resp = svc.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID, range="'" + tab + "'!" + a1).execute()
        return [c for row in resp.get('values', []) for c in row]

    result = evaluate(SOURCES, read_range)
    print('HUB STATUS: ' + result['overall'] + '  (checked ' + result['checked_at'] + ')')
    for r in result['sources']:
        print('  %-18s %-8s as of %s' % (r['name'], r['status'], r['as_of'] or 'n/a'))
    if args.dry_run:
        return

    existing = [s['properties']['title'] for s in
                svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute().get('sheets', [])]
    if STATUS_TAB not in existing:
        svc.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={
            'requests': [{'addSheet': {'properties': {'title': STATUS_TAB}}}]}).execute()
    values = [['F4P HUB STATUS', 'Overall: ' + result['overall'], 'Checked: ' + result['checked_at']],
              [''],
              ['FEED', 'STATUS', 'AS OF', 'SOURCE TAB']]
    values += [[r['name'], r['status'], r['as_of'], r['tab']] for r in result['sources']]
    svc.spreadsheets().values().clear(spreadsheetId=SPREADSHEET_ID, range="'" + STATUS_TAB + "'!A1:D50").execute()
    svc.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID, range="'" + STATUS_TAB + "'!A1",
        valueInputOption='RAW', body={'values': values}).execute()
    print('Wrote ' + STATUS_TAB + ' tab.')


if __name__ == '__main__':
    main()
