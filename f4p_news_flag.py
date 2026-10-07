"""
F4P News-day flag (Hub improvement).

Flags when a market-moving release is close to a planned entry, so the analyst
can decide. Flags only: `blocks_trade` is always False.

Where the events come from: a tab called 'NEWS CALENDAR' that YOU keep, one
event per row:   DATE (YYYY-MM-DD) | TIME UTC (HH:MM) | CURRENCY | EVENT | IMPACT (optional)
Only events for the two currencies of the pair are considered. Nothing is
invented: if the tab is missing, empty, or has nothing upcoming, the result is
NOT CHECKED, never "clear". The one built-in rule is the usual US jobs report
(first Friday of the month), shown as a reminder for pairs with USD.

    python f4p_news_flag.py --pair USD/JPY [--hours 24]
"""

import argparse
from datetime import date, datetime, timedelta, timezone

import f4p_exposure_check as ex

NEWS_TAB = 'NEWS CALENDAR'


def _event_time(d, t):
    """Date as text (2026-10-09) or a Sheets date number; time as 13:30 or a Sheets time fraction."""
    if isinstance(d, (int, float)):
        dt = datetime(1899, 12, 30) + timedelta(days=int(d))
    else:
        dt = datetime.strptime(str(d).strip()[:10], '%Y-%m-%d')
    hh, mm = 0, 0
    if isinstance(t, (int, float)):
        mins = int(round(float(t) * 1440))
        hh, mm = divmod(mins, 60)
    elif t not in (None, ''):
        hh, mm = [int(x) for x in str(t).strip().split(':')[:2]]
    return dt.replace(hour=hh % 24, minute=mm, tzinfo=timezone.utc)


def parse_events(rows):
    """Returns (events, problems). A row that cannot be read is reported, never skipped silently."""
    events, problems = [], []
    for n, r in enumerate(rows, start=2):
        r = list(r) + [''] * (5 - len(r))
        if not any(str(c).strip() for c in r):
            continue
        try:
            ccy = str(r[2]).strip().upper()
            if len(ccy) != 3:
                raise ValueError('currency must be 3 letters')
            events.append({'when': _event_time(r[0], r[1]), 'currency': ccy,
                           'event': str(r[3]).strip() or '(unnamed event)',
                           'impact': str(r[4]).strip()})
        except (ValueError, TypeError) as e:
            problems.append('NEWS CALENDAR row %d not read (%s)' % (n, e))
    return events, problems


def first_friday(year, month):
    d = date(year, month, 1)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def check_news(pair, now=None, events=None, problems=(), hours_ahead=24, hours_back=2):
    """status: CLEAR | FLAGGED | NOT CHECKED. blocks_trade is always False."""
    now = now or datetime.now(timezone.utc)
    base, quote = ex.parse_pair(pair)
    ccys = {base, quote}
    hits = []
    for e in (events or []):
        if e['currency'] in ccys and now - timedelta(hours=hours_back) <= e['when'] <= now + timedelta(hours=hours_ahead):
            hits.append('%s %s %s UTC%s' % (e['currency'], e['event'], e['when'].strftime('%a %d %b %H:%M'),
                                            ' [' + e['impact'] + ']' if e['impact'] else ''))
    reminders = []
    if 'USD' in ccys:
        for d in (now.date(), now.date() + timedelta(days=1)):
            if d == first_friday(d.year, d.month):
                reminders.append('%s is the first Friday: the US jobs report is usually released that day '
                                 '(check the time and add it to NEWS CALENDAR).' % d.strftime('%a %d %b'))
    upcoming = [e for e in (events or []) if e['when'] >= now]
    notes = list(problems)
    if hits or reminders:
        status = 'FLAGGED'
    elif not events or not upcoming:
        status = 'NOT CHECKED'
        notes.append('NEWS CALENDAR has nothing upcoming, so news risk is unknown, not clear.')
    else:
        status = 'CLEAR'
    return {'status': status, 'events': hits, 'reminders': reminders, 'notes': notes,
            'hours_ahead': hours_ahead, 'blocks_trade': False}


def describe(result):
    parts = result['events'] + result['reminders'] + result['notes']
    if result['status'] == 'CLEAR':
        return 'No listed %s-hour events for this pair.' % result['hours_ahead']
    return ' | '.join(parts)


def read_events(svc, spreadsheet_id):
    """Read only. Returns (events, problems)."""
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range="'" + NEWS_TAB + "'!A2:E300",
            valueRenderOption='UNFORMATTED_VALUE', dateTimeRenderOption='SERIAL_NUMBER').execute()
    except Exception as e:
        return [], ['Could not read the %s tab (%s).' % (NEWS_TAB, type(e).__name__)]
    return parse_events(resp.get('values', []))


def init_calendar(svc, spreadsheet_id):
    """Creates the empty NEWS CALENDAR tab with headers. Never touches an existing tab."""
    meta = svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    if NEWS_TAB in [sh['properties']['title'] for sh in meta['sheets']]:
        return 'The %s tab already exists. Left untouched.' % NEWS_TAB
    svc.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={
        'requests': [{'addSheet': {'properties': {'title': NEWS_TAB}}}]}).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id, range="'" + NEWS_TAB + "'!A1", valueInputOption='RAW',
        body={'values': [['DATE (YYYY-MM-DD)', 'TIME UTC (HH:MM)', 'CURRENCY', 'EVENT', 'IMPACT (optional)'],
                         ['Add one release per row, for example: 2026-10-14 | 12:30 | USD | CPI | high', '', '', '', '']]}).execute()
    return 'Created the %s tab.' % NEWS_TAB


def main():
    ap = argparse.ArgumentParser(description='F4P news-day flag (flags only)')
    ap.add_argument('--pair')
    ap.add_argument('--init', action='store_true', help='create the empty NEWS CALENDAR tab')
    ap.add_argument('--hours', type=int, default=24)
    a = ap.parse_args()
    import f4p_idea_book as ib
    if a.init:
        print(init_calendar(ib._service(), ib.SPREADSHEET_ID))
        return
    if not a.pair:
        ap.error('--pair is required')
    events, problems = read_events(ib._service(), ib.SPREADSHEET_ID)
    r = check_news(a.pair, events=events, problems=problems, hours_ahead=a.hours)
    print('NEWS CHECK %s: %s\n%s' % (a.pair, r['status'], describe(r)))


if __name__ == '__main__':
    main()
