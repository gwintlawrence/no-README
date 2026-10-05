"""
F4P Exposure Check  (Hub enhancement #3, feeds the Trade Decision Gate Check)

Turns a list of selected FX ideas into a currency exposure map and checks it
against two limits. It WARNS and never blocks: under the Cardinal Rule the
system flags and the analyst signs off, so `blocks_trade` is always False.

Every FX idea is two legs. LONG EUR/USD = long EUR, short USD. SHORT EUR/USD
= short EUR, long USD. Two ideas that load the same currency in the same
direction are duplicated macro exposure, even though the pairs differ.

LIMITS ARE PENDING GLENISE'S DECISION. The defaults below are starting values
only. Change them in one place (DEFAULT_LIMITS) or pass your own.
"""

from collections import defaultdict

CURRENCIES = ('USD', 'EUR', 'GBP', 'JPY', 'CHF', 'CAD', 'AUD', 'NZD', 'NOK', 'SEK')

DEFAULT_LIMITS = {
    # Most ideas allowed on the same side of one currency before it is a BREACH.
    'max_same_direction_per_currency': 2,
    # Share of total net exposure one currency may hold before it is a WATCH.
    'concentration_watch_pct': 35.0,
}


def parse_pair(pair):
    """'EUR/USD' or 'EURUSD' -> ('EUR', 'USD'). Raises ValueError if unknown."""
    p = pair.upper().replace('/', '').replace('-', '').replace(' ', '')
    if len(p) != 6:
        raise ValueError('Unrecognised pair: ' + repr(pair))
    base, quote = p[:3], p[3:]
    if base not in CURRENCIES or quote not in CURRENCIES or base == quote:
        raise ValueError('Unrecognised pair: ' + repr(pair))
    return base, quote


def idea_legs(pair, direction):
    """Return [(currency, +1 long | -1 short), ...] for one idea."""
    d = direction.strip().upper()
    if d not in ('LONG', 'SHORT'):
        raise ValueError('Direction must be LONG or SHORT: ' + repr(direction))
    base, quote = parse_pair(pair)
    s = 1 if d == 'LONG' else -1
    return [(base, s), (quote, -s)]


def exposure_map(ideas):
    """
    ideas: iterable of (pair, direction).
    Returns {ccy: {'long': n, 'short': n, 'net': n, 'net_share_pct': x,
                   'long_ideas': [...], 'short_ideas': [...]}}
    net_share_pct = |net| as a share of the sum of all |net|.
    """
    table = defaultdict(lambda: {'long': 0, 'short': 0, 'long_ideas': [], 'short_ideas': []})
    for pair, direction in ideas:
        label = parse_pair(pair)[0] + '/' + parse_pair(pair)[1] + ' ' + direction.strip().upper()
        for ccy, sign in idea_legs(pair, direction):
            side = 'long' if sign > 0 else 'short'
            table[ccy][side] += 1
            table[ccy][side + '_ideas'].append(label)
    total_abs_net = sum(abs(v['long'] - v['short']) for v in table.values())
    out = {}
    for ccy, v in table.items():
        net = v['long'] - v['short']
        out[ccy] = dict(v, net=net,
                        net_share_pct=round(100.0 * abs(net) / total_abs_net, 1) if total_abs_net else 0.0)
    return out


def check_exposure(ideas, limits=None):
    """
    Returns {'map': ..., 'warnings': [...], 'blocks_trade': False}.
    Each warning: {'level': 'BREACH'|'WATCH', 'currency', 'message'}.
    BREACH = more same-direction ideas on one currency than the limit.
    WATCH  = one currency holds more than the concentration share.
    """
    lim = dict(DEFAULT_LIMITS)
    lim.update(limits or {})
    ideas = list(ideas)
    emap = exposure_map(ideas)
    warnings = []
    for ccy in sorted(emap):
        v = emap[ccy]
        for side in ('long', 'short'):
            n = v[side]
            if n > lim['max_same_direction_per_currency']:
                warnings.append({
                    'level': 'BREACH', 'currency': ccy,
                    'message': '%d %s ideas on %s exceed the limit of %d: %s' % (
                        n, side, ccy, lim['max_same_direction_per_currency'],
                        ', '.join(v[side + '_ideas']))})
        if v['net_share_pct'] > lim['concentration_watch_pct']:
            warnings.append({
                'level': 'WATCH', 'currency': ccy,
                'message': '%s holds %.1f%% of net exposure (watch level %.0f%%)' % (
                    ccy, v['net_share_pct'], lim['concentration_watch_pct'])})
    # Cardinal Rule: flag only. The analyst decides.
    return {'map': emap, 'warnings': warnings, 'blocks_trade': False, 'limits': lim}


def format_report(result):
    """Plain-text summary for logs or a Sheet cell."""
    lines = ['EXPOSURE CHECK (warns only, analyst decides)']
    for ccy, v in sorted(result['map'].items(), key=lambda kv: -abs(kv[1]['net'])):
        lines.append('  %-3s long %d short %d net %+d  (%.1f%%)' % (
            ccy, v['long'], v['short'], v['net'], v['net_share_pct']))
    if result['warnings']:
        lines.append('FLAGS:')
        lines += ['  [%s] %s' % (w['level'], w['message']) for w in result['warnings']]
    else:
        lines.append('No flags.')
    return '\n'.join(lines)
