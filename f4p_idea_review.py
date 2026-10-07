"""
F4P Idea Review  (the track record: did the ideas, and the discipline, work?)

Reads your IDEA BOOK and, for every CLOSED idea, works out the result in pips
and dollars, the result as a multiple of what you risked (R), and whether the
size was within your 2% rule. Then totals them.

READ ONLY on the IDEA BOOK. It writes one result tab, IDEA REVIEW, which it
clears and rewrites each run. Nothing here places, blocks or changes a trade.

Nothing is guessed. A closed row that is missing something it needs is listed
under NOT COMPUTED with the reason, and is left out of the totals.

How each number is worked out
  pips       (close price - baseline price) in the idea's direction. Baseline is
             the first verified fill; it is never back-filled.
  dollars    pips x dollar value of one pip on your lot size. USD-quoted pairs
             exact. USD-base pairs (USD/JPY...) converted at the CLOSE price.
             Crosses are not computed (no conversion rate here).
  R          pips / the pips you risked, measured from the baseline to the
             STOP PRICE recorded in the IDEA BOOK. Keep your ORIGINAL stop in
             STOP PRICE and write a moved stop in NOTES; if the stop was moved
             to break-even or better, R is not computed rather than invented.
  size       the money the original stop risked, against your 2% rule.

Partial closes: one row = one position with one close price. If you close part
of a trade, add a second row for the closed part (same pair, those lots, that
close price) and keep the rest open on the original row.

    python f4p_idea_review.py --dry-run     # print only
    python f4p_idea_review.py               # print and write the IDEA REVIEW tab
"""

import argparse

import f4p_idea_lifecycle as lc
import f4p_risk_check as rk

REVIEW_TAB = 'IDEA REVIEW'


def review_idea(idea, limits=None):
    """One closed idea -> dict. Never raises: problems go in 'not_computed'."""
    lim = dict(rk.DEFAULT_RISK)
    lim.update(limits or {})
    out = {'label': '%s %s' % (idea.pair, idea.direction), 'pips': idea.result_pips(),
           'usd': None, 'r': None, 'risk_usd': None, 'risk_pct': None, 'over_rule': False,
           'notes': idea.notes, 'not_computed': []}
    if idea.lots in (None, ''):
        out['not_computed'].append('lot size is missing, so dollars and size cannot be worked out')
    else:
        try:
            per_pip = rk.pip_value_usd_per_lot(idea.pair, idea.closed_price) * float(idea.lots)
            out['usd'] = round(out['pips'] * per_pip, 2)
        except ValueError as e:
            out['not_computed'].append('dollars: ' + str(e))
    if idea.stop_price in (None, ''):
        out['not_computed'].append('stop price is missing, so R and the size check cannot be worked out')
    else:
        stop_pips = rk.stop_distance_pips(idea.pair, idea.direction, idea.baseline_price,
                                          idea.stop_price, filled=True)
        if stop_pips <= 0:
            out['not_computed'].append('the recorded stop is at or past the entry (break-even or better), '
                                       'so R is not computed. Keep your original stop in STOP PRICE.')
        else:
            out['r'] = round(out['pips'] / stop_pips, 2)
            if idea.lots not in (None, '') and out['usd'] is not None:
                per_pip = rk.pip_value_usd_per_lot(idea.pair, idea.baseline_price) * float(idea.lots)
                out['risk_usd'] = round(stop_pips * per_pip, 2)
                out['risk_pct'] = round(100.0 * out['risk_usd'] / lim['account_size_usd'], 2)
                out['over_rule'] = out['risk_pct'] > lim['max_risk_pct_per_trade'] + 1e-9
    return out


def build_review(ideas, limits=None):
    lim = dict(rk.DEFAULT_RISK)
    lim.update(limits or {})
    closed = [i for i in ideas if i.state == 'CLOSED']
    rows = [review_idea(i, lim) for i in closed]
    usd = [r['usd'] for r in rows if r['usd'] is not None]
    pips = [r['pips'] for r in rows if r['usd'] is not None]
    rs = [r['r'] for r in rows if r['r'] is not None]
    wins = [u for u in usd if u > 0]
    losses = [u for u in usd if u < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    t = {
        'closed': len(closed),
        'counted': len(usd),
        'wins': len(wins),
        'win_rate_pct': round(100.0 * len(wins) / len(usd), 1) if usd else None,
        'net_pips': round(sum(pips), 1) if usd else None,
        'net_usd': round(sum(usd), 2) if usd else None,
        'net_pct_of_account': round(100.0 * sum(usd) / lim['account_size_usd'], 2) if usd else None,
        'avg_win_usd': round(gross_win / len(wins), 2) if wins else None,
        'avg_loss_usd': round(-gross_loss / len(losses), 2) if losses else None,
        'profit_factor': round(gross_win / gross_loss, 2) if gross_loss else None,
        'avg_r': round(sum(rs) / len(rs), 2) if rs else None,
        'worst_loss_usd': round(min(usd), 2) if usd and min(usd) < 0 else None,
        'over_rule': sum(1 for r in rows if r['over_rule']),
        'not_computed': sum(1 for r in rows if r['not_computed']),
        'open_or_pending': sum(1 for i in ideas if i.state != 'CLOSED'),
    }
    return {'rows': rows, 'totals': t, 'limits': lim, 'blocks_trade': False}


def _n(v, fmt):
    return 'n/a' if v is None else fmt % v


def format_review(rv):
    t, lim = rv['totals'], rv['limits']
    lines = ['IDEA REVIEW  (the record of your closed ideas; flags only, analyst decides)',
             'Account $%.0f, rule %.1f%% per trade.' % (lim['account_size_usd'], lim['max_risk_pct_per_trade']), '']
    if not rv['rows']:
        lines.append('No closed ideas yet. Fill CLOSE PRICE and CLOSE TIME in the IDEA BOOK when an idea ends.')
        lines.append('Still open or pending: %d' % t['open_or_pending'])
        return '\n'.join(lines)
    for r in rv['rows']:
        bits = ['%+.1f pips' % r['pips']]
        bits.append(_n(r['usd'], '$%+.2f'))
        bits.append('R ' + _n(r['r'], '%+.2f'))
        if r['risk_usd'] is not None:
            bits.append('risked $%.2f (%.1f%%)%s' % (r['risk_usd'], r['risk_pct'],
                                                      ' OVER YOUR RULE' if r['over_rule'] else ''))
        lines.append('  %-16s ' % r['label'] + ', '.join(bits))
        for nc in r['not_computed']:
            lines.append('      [NOT COMPUTED] ' + nc)
    lines += ['',
              'Closed ideas: %d (counted in dollars: %d, not computed: %d). Open or pending: %d' % (
                  t['closed'], t['counted'], t['not_computed'], t['open_or_pending']),
              'Win rate: %s   Net: %s pips, %s (%s of the account)' % (
                  _n(t['win_rate_pct'], '%.1f%%'), _n(t['net_pips'], '%+.1f'),
                  _n(t['net_usd'], '$%+.2f'), _n(t['net_pct_of_account'], '%+.2f%%')),
              'Average win: %s   Average loss: %s   Profit factor: %s   Average R: %s' % (
                  _n(t['avg_win_usd'], '$%.2f'), _n(t['avg_loss_usd'], '$%.2f'),
                  _n(t['profit_factor'], '%.2f'), _n(t['avg_r'], '%+.2f')),
              'Worst loss: %s   Trades sized OVER your rule: %d' % (_n(t['worst_loss_usd'], '$%.2f'), t['over_rule'])]
    if t['counted'] < 20:
        lines.append('Note: %d counted trades is a small sample. Treat the percentages as a first look, not a verdict.'
                     % t['counted'])
    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser(description='F4P Idea Review (reads the IDEA BOOK)')
    ap.add_argument('--dry-run', action='store_true', help='print only, write nothing')
    a = ap.parse_args()
    import f4p_idea_book as ib
    svc = ib._service()
    ideas, problems = ib.read_book(svc)
    text = format_review(build_review(ideas))
    if problems:
        text += '\n\nIDEA BOOK rows that could not be read (left out):\n' + '\n'.join('  ' + p for p in problems)
    print(text)
    if a.dry_run:
        return
    if REVIEW_TAB not in ib._tab_ids(svc):
        svc.spreadsheets().batchUpdate(spreadsheetId=ib.SPREADSHEET_ID, body={
            'requests': [{'addSheet': {'properties': {'title': REVIEW_TAB}}}]}).execute()
    svc.spreadsheets().values().clear(
        spreadsheetId=ib.SPREADSHEET_ID, range="'" + REVIEW_TAB + "'!A1:A120").execute()
    svc.spreadsheets().values().update(
        spreadsheetId=ib.SPREADSHEET_ID, range="'" + REVIEW_TAB + "'!A1", valueInputOption='RAW',
        body={'values': [[line] for line in text.split('\n')]}).execute()
    print('Wrote the ' + REVIEW_TAB + ' tab.')


if __name__ == '__main__':
    main()
