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

Columns (row 1 is the header, ideas start on row 2). Times are UTC:
Trinidad and Tobago is UTC-4, so add 4 hours to your local time.
    A PAIR            e.g. USD/JPY
    B DIRECTION       LONG or SHORT
    C SELECTED AT     when you decided, e.g. 2026-10-06 19:30
    D BASELINE PRICE  first verified quote AFTER you signed off
    E BASELINE TIME   when that quote was taken
    F CLOSE PRICE     blank while the idea is open
    G CLOSE TIME      blank while the idea is open
    H NOTES           your decision notes
"""

import json
import os
from datetime import datetime, timezone

import f4p_exposure_check as ex
import f4p_idea_lifecycle as lc

SPREADSHEET_ID = '18ZgUq7uvyodHSQvreVNoCQFiS7Ks6Gp89CBbIItcPO0'   # Master Macro Scorecard
BOOK_TAB = 'IDEA BOOK'
RESULT_TAB = 'GATE CHECK'
HEADERS = ['PAIR', 'DIRECTION', 'SELECTED AT (UTC)', 'BASELINE PRICE', 'BASELINE TIME (UTC)',
           'CLOSE PRICE', 'CLOSE TIME (UTC)', 'NOTES']
HELP_TEXT = ('One row per idea. Times are UTC (Trinidad = UTC-4, add 4 hours). '
             'Baseline = first verified quote AFTER you sign off; never back-fill it. '
             'Leave CLOSE blank while the idea is open.')


def _cell(row, i):
    return str(row[i]).strip() if i < len(row) and row[i] is not None else ''


def _time(text):
    """'2026-10-06 19:30' -> ISO text lifecycle accepts. UTC if no zone given."""
    dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _price(text, label):
    try:
        return float(text.replace(',', ''))
    except ValueError:
        raise ValueError(label + ' is not a number: ' + repr(text))


def parse_book_rows(rows):
    """
    rows: list of lists as returned by the Sheets API for A2:H (no header).
    Returns (ideas, problems). A problem is a plain-English string naming the
    sheet row. Rows with a problem are NOT included in ideas.
    """
    ideas, problems = [], []
    for n, row in enumerate(rows, start=2):
        cells = [_cell(row, i) for i in range(8)]
        if not any(cells):
            continue
        where = 'IDEA BOOK row %d' % n
        try:
            pair, direction, sel = cells[0], cells[1], cells[2]
            if not pair or not direction:
                raise ValueError('PAIR and DIRECTION are both needed')
            ex.parse_pair(pair)
            if not sel:
                raise ValueError('SELECTED AT is missing (not guessed)')
            idea = lc.Idea(pair, direction, _time(sel), notes=cells[7])
            if cells[3] or cells[4]:
                if not (cells[3] and cells[4]):
                    raise ValueError('BASELINE PRICE and BASELINE TIME must be filled together')
                idea.record_baseline(_price(cells[3], 'BASELINE PRICE'), _time(cells[4]))
            if cells[5] or cells[6]:
                if not (cells[5] and cells[6]):
                    raise ValueError('CLOSE PRICE and CLOSE TIME must be filled together')
                idea.close(_price(cells[5], 'CLOSE PRICE'), _time(cells[6]))
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
        spreadsheetId=SPREADSHEET_ID, range="'" + BOOK_TAB + "'!J1", valueInputOption='RAW',
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
            spreadsheetId=SPREADSHEET_ID, range="'" + BOOK_TAB + "'!A2:H500").execute()
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
