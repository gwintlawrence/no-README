"""
F4P COT Re-check  (Saturday safety net for the weekly COT data)

The CFTC publishes COT every Friday about 3:30pm Eastern, for positions as of
the TUESDAY before. On one Friday the fetch ran before the file was updated and
the Hub kept showing the previous week's numbers. This script checks, after the
fact, that the COT as-of dates on FRED AUTO are the ones the latest Friday
release should have produced, and says so plainly if they are not.

READ ONLY. It never writes to the Sheet. The workflow decides what to do next
(re-run the fetch, then check again). Nothing here places, blocks or changes a
trade; the analyst decides.

    python f4p_cot_recheck.py            # exit 0 = current, 3 = stale or unreadable
    python f4p_cot_recheck.py --strict   # exit 1 instead of 3 (final check)

Honest limits:
  * In a week where Friday is a US federal holiday the CFTC publishes on
    Monday. A Saturday check then finds the data genuinely not out yet, and
    reports that (it does not invent data).
  * An unreadable or empty date is reported as UNKNOWN, never as current.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone

import f4p_freshness_status as fs

# Same cell range the freshness badge reads.
COT_SOURCE = [s for s in fs.SOURCES if s['name'] == 'COT positioning'][0]
RELEASE_HOUR_UTC = 21        # matches the Friday fetch (5pm Eastern); after the ~3:30pm release


def expected_cot_date(now):
    """
    The Tuesday as-of date that the latest completed Friday release should carry.
    A Friday only counts once RELEASE_HOUR_UTC has passed.
    """
    now = now.astimezone(timezone.utc)
    day = now.date()
    # walk back to the most recent Friday whose release time has passed
    for back in range(0, 8):
        d = day - timedelta(days=back)
        if d.weekday() != 4:
            continue
        release = datetime(d.year, d.month, d.day, RELEASE_HOUR_UTC, tzinfo=timezone.utc)
        if now >= release:
            return (d - timedelta(days=3))          # Friday minus 3 = Tuesday
    raise RuntimeError('no Friday found')           # unreachable: 8 days always contain one


def cot_status(cells, now=None):
    """
    cells: raw strings from FRED AUTO E18:E25.
    Returns {'status': CURRENT|STALE|UNKNOWN, 'expected', 'oldest', 'message'}.
    """
    now = now or datetime.now(timezone.utc)
    expected = expected_cot_date(now)
    values = [c for c in cells if c and str(c).strip()]
    parsed = [fs.parse_date(c) for c in values]
    if not parsed or any(p is None for p in parsed):
        return {'status': 'UNKNOWN', 'expected': expected, 'oldest': None,
                'message': 'COT as-of dates are empty or unreadable. Not treated as current.'}
    oldest = min(parsed).date()
    if oldest >= expected:
        return {'status': 'CURRENT', 'expected': expected, 'oldest': oldest,
                'message': 'COT is current: oldest as-of %s, expected %s.' % (oldest, expected)}
    return {'status': 'STALE', 'expected': expected, 'oldest': oldest,
            'message': ('COT is STALE: oldest as-of %s but the latest Friday release should be '
                        'as of %s. If this Friday was a US holiday, the CFTC publishes on Monday.'
                        % (oldest, expected))}


def main():
    ap = argparse.ArgumentParser(description='F4P COT re-check (read only)')
    ap.add_argument('--strict', action='store_true',
                    help='final check: exit 1 (fail the run) if the data is still not current')
    a = ap.parse_args()
    svc = fs._service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=fs.SPREADSHEET_ID,
        range="'" + COT_SOURCE['tab'] + "'!" + COT_SOURCE['range']).execute()
    cells = [c for row in resp.get('values', []) for c in row]
    result = cot_status(cells)
    print('COT RE-CHECK: ' + result['status'])
    print('  ' + result['message'])
    if result['status'] == 'CURRENT':
        return 0
    return 1 if a.strict else 3


if __name__ == '__main__':
    sys.exit(main())
