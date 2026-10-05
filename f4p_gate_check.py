"""
F4P Trade Decision Gate Check  (Hub enhancements #2, #3, #4 working together)

Pulls the exposure check, the freshness badge and the idea lifecycle into one
decision packet for the analyst to read BEFORE signing off on an idea.

CARDINAL RULE: the Gate Check flags; the analyst signs off. `blocks_trade` is
always False and nothing here places, vetoes or closes a trade. The packet
carries an `analyst_signoff` field that stays None until a person fills it in.

Usage (no live access needed):
    python f4p_gate_check.py --pair EUR/USD --direction SHORT \
        --open "GBP/USD:SHORT,USD/JPY:LONG" --html gate_check.html

Freshness comes from f4p_freshness_status.evaluate(). Pass its result into
build_gate_packet(); without it the badge is UNKNOWN, never green.
"""

import argparse
import html
from datetime import datetime, timezone

import f4p_exposure_check as ex
import f4p_idea_lifecycle as lc


def _warning_keys(result):
    return {(w['level'], w['currency'], w['message']) for w in result['warnings']}


def build_gate_packet(candidate, open_ideas=(), freshness=None, limits=None,
                      prices=None, close_rules=(), now=None):
    """
    candidate:   (pair, direction) being considered.
    open_ideas:  lifecycle.Idea objects already on the book (any state).
    freshness:   result of f4p_freshness_status.evaluate(), or None.
    prices:      {pair: current price} used only to show pips and close flags.
    close_rules: your own rules (see lifecycle.adverse_pips_rule); flag only.
    """
    now = now or datetime.now(timezone.utc)
    pair, direction = candidate
    ex.idea_legs(pair, direction)          # validates the candidate, raises ValueError

    held = [(i.pair, i.direction) for i in open_ideas if i.state != 'CLOSED']
    before = ex.check_exposure(held, limits)
    after = ex.check_exposure(held + [(pair, direction)], limits)
    seen = _warning_keys(before)
    new_flags = [w for w in after['warnings']
                 if (w['level'], w['currency'], w['message']) not in seen]

    fresh_overall = (freshness or {}).get('overall', 'UNKNOWN')
    prices = prices or {}
    book = []
    for i in open_ideas:
        if i.state == 'CLOSED':
            continue
        px = prices.get(i.pair)
        book.append({
            'pair': i.pair, 'direction': i.direction, 'state': i.state,
            'pips': i.pips(px) if px is not None else None,
            'close_flags': lc.check_close(i, px, close_rules) if px is not None else [],
        })

    checks = [
        {'item': 'Data freshness', 'status': fresh_overall,
         'ok': fresh_overall == 'CURRENT',
         'note': 'Worst feed decides. UNKNOWN means it could not be confirmed.'},
        {'item': 'Exposure after this idea', 'status': 'FLAGGED' if new_flags else 'CLEAR',
         'ok': not new_flags,
         'note': '; '.join(w['message'] for w in new_flags) or 'No new limit flags.'},
        {'item': 'Baseline plan', 'status': 'PENDING',
         'ok': True,
         'note': 'Baseline is the first verified quote after sign-off. It is never back-filled.'},
    ]
    flagged = [c for c in checks if not c['ok']]
    return {
        'generated_at': now.strftime('%Y-%m-%d %H:%M UTC'),
        'candidate': {'pair': pair, 'direction': direction.strip().upper()},
        'checks': checks,
        'summary': 'REVIEW %d FLAG(S) BEFORE SIGN-OFF' % len(flagged) if flagged
                   else 'NO FLAGS - ANALYST STILL SIGNS OFF',
        'exposure_after': after,
        'new_exposure_flags': new_flags,
        'freshness': freshness,
        'open_book': book,
        'blocks_trade': False,              # Cardinal Rule
        'analyst_signoff': None,            # a person fills this in, never the system
    }


def format_packet(p):
    lines = ['TRADE DECISION GATE CHECK  (%s)' % p['generated_at'],
             'Candidate: %s %s' % (p['candidate']['pair'], p['candidate']['direction']),
             p['summary'], '']
    for c in p['checks']:
        lines.append('  [%-8s] %s - %s' % (c['status'], c['item'], c['note']))
    lines += ['', ex.format_report(p['exposure_after']), '',
              'Analyst sign-off: ______  (the system never signs off)']
    return '\n'.join(lines)


def render_html(p):
    e = html.escape
    rows = ''.join(
        '<tr><td>%s</td><td class="%s">%s</td><td>%s</td></tr>' % (
            e(c['item']), 'ok' if c['ok'] else 'warn', e(c['status']), e(c['note']))
        for c in p['checks'])
    emap = ''.join(
        '<tr><td>%s</td><td>%d</td><td>%d</td><td>%+d</td><td>%.1f%%</td></tr>' % (
            e(ccy), v['long'], v['short'], v['net'], v['net_share_pct'])
        for ccy, v in sorted(p['exposure_after']['map'].items(), key=lambda kv: -abs(kv[1]['net'])))
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>F4P Gate Check</title><style>'
            'body{font-family:system-ui,sans-serif;max-width:720px;margin:24px auto;padding:0 16px;color:#1c1c1c}'
            'table{border-collapse:collapse;width:100%%;margin:12px 0}td,th{border:1px solid #ccc;padding:6px 8px;text-align:left}'
            '.ok{color:#146c2e;font-weight:600}.warn{color:#a15c00;font-weight:600}'
            '.sign{margin-top:20px;padding:12px;border:2px dashed #888}'
            '</style></head><body>'
            '<h1>Trade Decision Gate Check</h1>'
            '<p>%s %s &middot; %s</p><h2>%s</h2>'
            '<table><tr><th>Check</th><th>Status</th><th>Note</th></tr>%s</table>'
            '<h3>Currency exposure after this idea</h3>'
            '<table><tr><th>Ccy</th><th>Long</th><th>Short</th><th>Net</th><th>Share</th></tr>%s</table>'
            '<div class="sign">Analyst sign-off: ______________________<br>'
            'The Gate Check flags. It never blocks, signs off, or places a trade.</div>'
            '</body></html>') % (e(p['candidate']['pair']), e(p['candidate']['direction']),
                                 e(p['generated_at']), e(p['summary']), rows, emap)


def _parse_open(text):
    out = []
    for part in [x for x in (text or '').split(',') if x.strip()]:
        pair, direction = part.split(':')
        out.append(lc.Idea(pair.strip(), direction.strip(), datetime.now(timezone.utc).isoformat()))
    return out


def main():
    ap = argparse.ArgumentParser(description='F4P Trade Decision Gate Check (flags only)')
    ap.add_argument('--pair', required=True)
    ap.add_argument('--direction', required=True)
    ap.add_argument('--open', default='', help='open ideas, e.g. "GBP/USD:SHORT,USD/JPY:LONG"')
    ap.add_argument('--html', help='also write a standalone HTML page here')
    a = ap.parse_args()
    packet = build_gate_packet((a.pair, a.direction), _parse_open(a.open))
    print(format_packet(packet))
    if a.html:
        with open(a.html, 'w', encoding='utf-8') as f:
            f.write(render_html(packet))
        print('Wrote ' + a.html)


if __name__ == '__main__':
    main()
