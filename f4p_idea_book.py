"""
F4P Idea Book  (home of the idea lifecycle, Hub enhancement #4)

One tab, IDEA BOOK, in the Master Macro Scorecard where Glenise records her
ideas by hand. The Gate Check reads it so it always sees her real book.

Rules:
  * The script only READS the IDEA BOOK. It creates the empty tab once
    (init) and never writes into, edits or reorders her rows.
  * Nothing is guessed. A row it cannot read cleanly is reported as a
    PROBLEM and shown in the Gate Check, never silently skipped.
  * Closed ideas stay in the tab; they are the track record.

Columns (row 1 is the header, ideas start on row 2). Times are Trinidad and
Tobago time (UTC-4, no daylight saving): type what your own clock says.
    A PAIR            e.g. USD/JPY
    B DIRECTION       LONG or SHORT
    C SELECTED AT     when you decided, e.g. 2026-10-06 19:30
    D BASELINE PRICE  first verified quote AFTER you signed off
    E BASELINE TIME   when that quote was taken
    F CLOSE PRICE     blank while the idea is open
    G CLOSE TIME      blank while the idea is open
    H NOTES           your decision notes
  Optional, used by the risk check (blank = risk NOT CHECKED, never assumed safe):
    I ENTRY PRICE     your planned/pending order price
    J STOP PRICE
    K TARGET PRICE
    L LOTS
"""

import json
import os
from datetime import datetime, timedelta, timezone

import f4p_exposure_check as ex
import f4p_idea_lifecycle as lc

SPREADSHEET_ID = '18ZgUq7uvyodHSQvreVNoCQFiS7Ks6Gp89CBbIItcPO0'   # Master Macro Scorecard
BOOK_TAB = 'IDEA BOOK'
RESULT_TAB = 'GATE CHECK'
TT = timezone(timedelta(hours=-4))        # Trinidad and Tobago: UTC-4 all year
HEADERS = ['PAIR', 'DIRECTION', 'SELECTED AT (TT)', 'BASELINE PRICE', 'BASELINE TIME (TT)',
           'CLOSE PRICE', 'CLOSE TIME (TT)', 'NOTES',
           'ENTRY PRICE', 'STOP PRICE', 'TARGET PRICE', 'LOTS']
HELP_TEXT = ('One row per idea. Times are Trinidad time (TT): type date AND time, e.g. 2026-10-06 12:00. '
             'Baseline = first verified quote AFTER you sign off; never back-fill it. '
             'Leave CLOSE blank while the idea is open.')
_SHEETS_EPOCH = datetime(1899, 12, 30)
_MIN_SERIAL = 40000        # about 2009; anything smaller is a stray number like 12, not a date


def _cell(row, i):
    """Raw cell: numbers stay numbers (dates arrive as serial numbers), text is stripped."""
    v = row[i] if i < len(row) else ''
    if v is None:
        return ''
    return v.strip() if isinstance(v, str) else v


def _time(value, label='time'):
    """
    Sheets date-times arrive as serial numbers (unambiguous). Typed ISO text is
    also accepted. Anything without BOTH a date and a time is refused, never
    guessed. Naive times are Trinidad time.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < _MIN_SERIAL or float(value) == int(value):
            raise ValueError(label + ' needs a full date AND time, e.g. 2026-10-06 12:00 (got %r)' % value)
        dt = _SHEETS_EPOCH + timedelta(days=float(value))
        dt = dt.replace(second=0, microsecond=0) if dt.second < 30 else \
            (dt + timedelta(minutes=1)).replace(second=0, microsecond=0)
        return dt.replace(tzinfo=TT).isoformat()
    text = str(value).strip().replace('Z', '+00:00')
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(label + ' needs a full date AND time, e.g. 2026-10-06 12:00 (got %r)' % value)
    if len(text) <= 10:
        raise ValueError(label + ' has a date but no time (got %r)' % value)
    return (dt if dt.tzinfo else dt.replace(tzinfo=TT)).isoformat()


def _price(value, label):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return float(str(value).replace(',', ''))
    except ValueError:
        raise ValueError(label + ' is not a number: ' + repr(value))


def parse_book_rows(rows):
    """
    rows: list of lists as returned by the Sheets API for A2:H (no header).
    Returns (ideas, problems). A problem is a plain-English string naming the
    sheet row. Rows with a problem are NOT included in ideas.
    """
    ideas, problems = [], []
    for n, row in enumerate(rows, start=2):
        cells = [_cell(row, i) for i in range(12)]
        if all(c == '' for c in cells):
            continue
        where = 'IDEA BOOK row %d' % n
        try:
            pair, direction, sel = cells[0], cells[1], cells[2]
            if pair == '' or direction == '':
                raise ValueError('PAIR and DIRECTION are both needed')
            pair, direction = str(pair), str(direction)
            ex.parse_pair(pair)
            if sel == '':
                raise ValueError('SELECTED AT is missing (not guessed)')
            idea = lc.Idea(pair, direction, _time(sel, 'SELECTED AT'), notes=str(cells[7]))
            if cells[3] != '' or cells[4] != '':
                if cells[3] == '' or cells[4] == '':
                    raise ValueError('BASELINE PRICE and BASELINE TIME must be filled together')
                idea.record_baseline(_price(cells[3], 'BASELINE PRICE'), _time(cells[4], 'BASELINE TIME'))
            if cells[5] != '' or cells[6] != '':
                if cells[5] == '' or cells[6] == '':
                    raise ValueError('CLOSE PRICE and CLOSE TIME must be filled together')
                idea.close(_price(cells[5], 'CLOSE PRICE'), _time(cells[6], 'CLOSE TIME'))
            for idx, attr, label in ((8, 'entry_price', 'ENTRY PRICE'), (9, 'stop_price', 'STOP PRICE'),
                                     (10, 'target_price', 'TARGET PRICE'), (11, 'lots', 'LOTS')):
                if cells[idx] != '':
                    setattr(idea, attr, _price(cells[idx], label))
            ideas.append(idea)
        except ValueError as e:
            problems.append(where + ': ' + str(e))
    return ideas, problems


def _service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ['GOOGLE_CREDENTIALS']),
        scopes=['https://www.googleapis.com/auth/spreadsheets'])
    return build('sheets', 'v4', credentials=creds)


def _tab_ids(svc):
    meta = svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    return {s['properties']['title']: s['properties']['sheetId'] for s in meta.get('sheets', [])}


def init_idea_book(svc):
    """Create the empty IDEA BOOK tab once. Never touches an existing tab."""
    tabs = _tab_ids(svc)
    if BOOK_TAB in tabs:
        return 'IDEA BOOK already exists. Left exactly as it is.'
    resp = svc.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={'requests': [
        {'addSheet': {'properties': {'title': BOOK_TAB, 'gridProperties': {'frozenRowCount': 1}}}}]}).execute()
    sid = resp['replies'][0]['addSheet']['properties']['sheetId']
    svc.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID, range="'" + BOOK_TAB + "'!A1", valueInputOption='RAW',
        body={'values': [HEADERS]}).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID, range="'" + BOOK_TAB + "'!N1", valueInputOption='RAW',
        body={'values': [[HELP_TEXT]]}).execute()
    svc.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={'requests': [
        {'setDataValidation': {
            'range': {'sheetId': sid, 'startRowIndex': 1, 'endRowIndex': 500,
                      'startColumnIndex': 1, 'endColumnIndex': 2},
            'rule': {'condition': {'type': 'ONE_OF_LIST',
                                   'values': [{'userEnteredValue': 'LONG'}, {'userEnteredValue': 'SHORT'}]},
                     'strict': True, 'showCustomUi': True}}},
        {'repeatCell': {'range': {'sheetId': sid, 'startRowIndex': 0, 'endRowIndex': 1},
                        'cell': {'userEnteredFormat': {'textFormat': {'bold': True}}},
                        'fields': 'userEnteredFormat.textFormat.bold'}}]}).execute()
    return 'Created the IDEA BOOK tab. It is empty; add your ideas from row 2.'


def read_book(svc):
    """Returns (ideas, problems). Read only."""
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="'" + BOOK_TAB + "'!A2:L500",
            valueRenderOption='UNFORMATTED_VALUE', dateTimeRenderOption='SERIAL_NUMBER').execute()
    except Exception as e:
        return [], ['Could not read the IDEA BOOK tab (' + type(e).__name__ + '). '
                    'Run the setup step first if the tab does not exist yet.']
    return parse_book_rows(resp.get('values', []))


def write_result_tab(svc, text):
    """Latest Gate Check result, one line per row, in its own GATE CHECK tab."""
    if RESULT_TAB not in _tab_ids(svc):
        svc.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={
            'requests': [{'addSheet': {'properties': {'title': RESULT_TAB}}}]}).execute()
    svc.spreadsheets().values().clear(
        spreadsheetId=SPREADSHEET_ID, range="'" + RESULT_TAB + "'!A1:A80").execute()
    svc.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID, range="'" + RESULT_TAB + "'!A1", valueInputOption='RAW',
        body={'values': [[line] for line in text.split('\n')]}).execute()
