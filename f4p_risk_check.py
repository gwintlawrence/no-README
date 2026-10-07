"""
F4P Risk Check  (Hub enhancement #3 companion: size, not just direction)

Checks the money at risk on each idea against Glenise's own rule:
standard risk is 2% of the account per trade (2% of $500 = $10).

FLAGS ONLY. Under the Cardinal Rule nothing here blocks, resizes or cancels
an order; `blocks_trade` is always False and the analyst decides.

Honest limits:
  * Risk is computed only when entry, stop and lots are all known. Anything
    missing is reported as NOT CHECKED, never treated as safe.
  * USD-quoted pairs (EUR/USD, GBP/USD, AUD/USD, NZD/USD) and USD-based pairs
    (USD/JPY, USD/CAD, USD/CHF, USD/NOK, USD/SEK) are supported. A cross such as
    EUR/GBP needs a conversion rate this module does not have, so it is
    reported as NOT CHECKED rather than guessed.
  * Pip value is a standard-lot approximation (1 lot = 100,000 units). Your
    broker's own contract size and spread can differ slightly.
"""

import f4p_exposure_check as ex
import f4p_idea_lifecycle as lc

LOT_UNITS = 100000

# Glenise's own rule. Change in one place, or pass your own.
DEFAULT_RISK = {
    'account_size_usd': 500.0,
    'max_risk_pct_per_trade': 2.0,
    'portfolio_budget_pct': 10.0,      # only this share of capital is ever put at risk in total
}


def pip_value_usd_per_lot(pair, price=None):
    """USD value of one pip on a 1.0 lot position."""
    base, quote = ex.parse_pair(pair)
    pip = lc.pip_size(pair)
    if quote == 'USD':
        return LOT_UNITS * pip
    if base == 'USD':
        if not price:
            raise ValueError('needs the entry price to convert the pip value')
        return LOT_UNITS * pip / float(price)
    raise ValueError('cross pair: needs a USD conversion rate, so risk is not computed')


def stop_distance_pips(pair, direction, entry, stop, filled=False):
    """
    Pips from entry to stop. Positive = stop still risks money.
    For a trade that has FILLED, a stop at or beyond the entry on the profit
    side (break-even or a trailed stop) is legitimate: zero or negative here
    means no open risk. For an unfilled order it is a mistake, so it is refused.
    """
    sign = 1 if direction.strip().upper() == 'LONG' else -1
    pips = round((float(entry) - float(stop)) / lc.pip_size(pair) * sign, 1)
    if pips <= 0 and not filled:
        raise ValueError('stop is on the wrong side of the entry for a %s' % direction.upper())
    return pips


def target_distance_pips(pair, direction, entry, target):
    sign = 1 if direction.strip().upper() == 'LONG' else -1
    return round((float(target) - float(entry)) / lc.pip_size(pair) * sign, 1)


def trade_risk(trade, limits=None):
    """
    trade: {'label','pair','direction','entry','stop','lots', 'target' (optional)}
    Returns a result dict; raises ValueError with a plain reason if it cannot
    be computed.
    """
    lim = dict(DEFAULT_RISK)
    lim.update(limits or {})
    for k in ('entry', 'stop', 'lots'):
        if trade.get(k) in (None, ''):
            raise ValueError('%s is missing' % {'entry': 'entry price', 'stop': 'stop price', 'lots': 'lot size'}[k])
    lots = float(trade['lots'])
    if lots <= 0:
        raise ValueError('lot size must be above zero')
    filled = bool(trade.get('filled'))
    pips = stop_distance_pips(trade['pair'], trade['direction'], trade['entry'], trade['stop'], filled)
    if pips <= 0:
        # Filled trade with the stop at break-even or better: nothing left at
        # risk (spread, slippage and gaps aside).
        return {'label': trade['label'], 'lots': lots, 'stop_pips': pips, 'risk_usd': 0.0,
                'risk_pct': 0.0, 'rr': None, 'locked_pips': -pips}
    per_pip = pip_value_usd_per_lot(trade['pair'], trade['entry']) * lots
    risk = round(pips * per_pip, 2)
    out = {'label': trade['label'], 'lots': lots, 'stop_pips': pips,
           'risk_usd': risk,
           'risk_pct': round(100.0 * risk / lim['account_size_usd'], 2),
           'rr': None, 'locked_pips': 0.0}
    if trade.get('target') not in (None, ''):
        out['rr'] = round(target_distance_pips(trade['pair'], trade['direction'],
                                               trade['entry'], trade['target']) / pips, 2)
    return out


def suggest_lots(pair, direction, entry, stop, other_risk_usd=0.0, limits=None):
    """
    Largest lot size (rounded DOWN to 0.01) that keeps this idea inside BOTH
    your rules: max risk per trade, and the portfolio budget left after the
    risk already on the book. Returns a dict; raises ValueError if it cannot
    be worked out. A suggestion for the analyst, never an order.
    """
    lim = dict(DEFAULT_RISK)
    lim.update(limits or {})
    pips = stop_distance_pips(pair, direction, entry, stop)
    per_pip = pip_value_usd_per_lot(pair, entry)
    acct = lim['account_size_usd']
    per_trade = acct * lim['max_risk_pct_per_trade'] / 100.0
    budget_left = max(0.0, acct * lim['portfolio_budget_pct'] / 100.0 - float(other_risk_usd))
    allowed = min(per_trade, budget_left)
    lots = int(allowed / (pips * per_pip) * 100 + 1e-9) / 100.0
    return {'lots': lots, 'stop_pips': pips, 'risk_usd': round(lots * pips * per_pip, 2),
            'per_trade_cap_usd': round(per_trade, 2), 'budget_left_usd': round(budget_left, 2),
            'limited_by': 'portfolio budget' if budget_left < per_trade else 'per-trade rule'}


def check_risk(trades, limits=None):
    """
    Returns {'rows': [...], 'flags': [...], 'not_checked': [...],
             'total_risk_usd', 'total_risk_pct', 'blocks_trade': False, 'limits'}.
    """
    lim = dict(DEFAULT_RISK)
    lim.update(limits or {})
    rows, flags, not_checked = [], [], []
    for t in trades:
        try:
            r = trade_risk(t, lim)
        except ValueError as e:
            not_checked.append('%s: %s' % (t.get('label', '?'), e))
            continue
        rows.append(r)
        if r['risk_pct'] > lim['max_risk_pct_per_trade']:
            flags.append('%s risks $%.2f (%.1f%% of the account), above your %.1f%% rule ($%.2f). '
                         'About %.2f lots would match the rule.' % (
                             r['label'], r['risk_usd'], r['risk_pct'], lim['max_risk_pct_per_trade'],
                             lim['account_size_usd'] * lim['max_risk_pct_per_trade'] / 100.0,
                             r['lots'] * lim['max_risk_pct_per_trade'] / r['risk_pct']))
    total = round(sum(r['risk_usd'] for r in rows), 2)
    return {'rows': rows, 'flags': flags, 'not_checked': not_checked,
            'total_risk_usd': total,
            'total_risk_pct': round(100.0 * total / lim['account_size_usd'], 2),
            'blocks_trade': False, 'limits': lim}


def format_risk(result):
    lines = ['RISK CHECK (flags only, analyst decides; rule %.1f%% of $%.0f)' % (
        result['limits']['max_risk_pct_per_trade'], result['limits']['account_size_usd'])]
    for r in result['rows']:
        if r['stop_pips'] <= 0:
            lines.append('  %-16s %.2f lots, stop at break-even or better (locks %.1f pips): no open risk' % (
                r['label'], r['lots'], r['locked_pips']))
            continue
        extra = ', reward:risk %.2f' % r['rr'] if r['rr'] is not None else ''
        lines.append('  %-16s %.2f lots, stop %.1f pips: $%.2f (%.1f%%)%s' % (
            r['label'], r['lots'], r['stop_pips'], r['risk_usd'], r['risk_pct'], extra))
    if result['rows']:
        lines.append('  If everything triggers and stops out: $%.2f (%.1f%% of the account)' % (
            result['total_risk_usd'], result['total_risk_pct']))
    lines += ['  [FLAG] ' + f for f in result['flags']]
    lines += ['  [NOT CHECKED] ' + n for n in result['not_checked']]
    return '\n'.join(lines)
