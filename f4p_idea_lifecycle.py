"""
F4P Idea Lifecycle  (Hub enhancement #4, home: the Trading Journal)

Tracks one trade idea from selection to close so the Hub builds a real,
honest track record.

Rules this module enforces:
  * An idea is PENDING_BASELINE until a verified quote is recorded. The
    baseline is that first verified quote. It is never back-filled or
    guessed, so a result can never be flattered by a convenient entry price.
  * A quote older than `max_quote_age_min` is flagged DELAYED, never shown
    as current.
  * Close rules are chosen by Glenise. None are built in. Pass in the rules
    you decide on (see adverse_pips_rule for one example helper). Rules flag
    a close; the analyst closes the idea.

Pips: JPY-quoted pairs use 0.01, all other FX pairs use 0.0001.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional


def pip_size(pair):
    quote = pair.upper().replace('/', '')[3:]
    return 0.01 if quote == 'JPY' else 0.0001


def _parse_time(value):
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Idea:
    pair: str
    direction: str                      # LONG or SHORT
    selected_at: str                    # analyst decision time (ISO 8601)
    baseline_price: Optional[float] = None
    baseline_at: Optional[str] = None   # when that verified quote was taken
    closed_price: Optional[float] = None
    closed_at: Optional[str] = None
    notes: str = ''                     # analyst's decision notes
    entry_price: Optional[float] = None   # planned/pending order price (not a baseline)
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    lots: Optional[float] = None
    history: List[dict] = field(default_factory=list)

    def __post_init__(self):
        self.direction = self.direction.strip().upper()
        if self.direction not in ('LONG', 'SHORT'):
            raise ValueError('Direction must be LONG or SHORT')

    @property
    def sign(self):
        return 1 if self.direction == 'LONG' else -1

    @property
    def state(self):
        if self.closed_price is not None:
            return 'CLOSED'
        if self.baseline_price is None:
            return 'PENDING_BASELINE'
        return 'ACTIVE'

    def record_baseline(self, price, quote_time):
        """First verified quote only. A second call is refused, not overwritten."""
        if self.baseline_price is not None:
            raise ValueError('Baseline already recorded; it cannot be changed.')
        if _parse_time(quote_time) < _parse_time(self.selected_at):
            raise ValueError('Baseline quote predates the selection time.')
        self.baseline_price = float(price)
        self.baseline_at = str(quote_time)

    def pips(self, price):
        """Pips since baseline at `price` (positive = in the idea's favour)."""
        if self.baseline_price is None:
            return None
        return round((float(price) - self.baseline_price) / pip_size(self.pair) * self.sign, 1)

    def quote_status(self, quote_time, now=None, max_quote_age_min=30):
        """'CURRENT' or 'DELAYED' for a quote taken at quote_time."""
        now = _parse_time(now) if now else datetime.now(timezone.utc)
        age_min = (now - _parse_time(quote_time)).total_seconds() / 60.0
        return 'CURRENT' if age_min <= max_quote_age_min else 'DELAYED'

    def close(self, price, closed_at):
        if self.baseline_price is None:
            raise ValueError('Cannot close an idea that never had a baseline.')
        if self.closed_price is not None:
            raise ValueError('Idea is already closed.')
        self.closed_price = float(price)
        self.closed_at = str(closed_at)

    def result_pips(self):
        return None if self.closed_price is None else self.pips(self.closed_price)


# A close rule is any function (idea, price) -> reason string, or None.
CloseRule = Callable[[Idea, float], Optional[str]]


def adverse_pips_rule(limit_pips):
    """Example helper: flag when the idea is `limit_pips` or more against you."""
    def rule(idea, price):
        p = idea.pips(price)
        if p is not None and p <= -abs(limit_pips):
            return 'Adverse move of %.1f pips reached the %.0f pip limit' % (-p, abs(limit_pips))
        return None
    return rule


def check_close(idea, price, rules=()):
    """
    Returns the list of reasons the supplied rules flag a close. Empty list
    means nothing flagged. This only flags; the analyst closes the idea.
    """
    if idea.state != 'ACTIVE':
        return []
    return [r for r in (rule(idea, price) for rule in rules) if r]


def summarise(ideas):
    """Track-record totals over a list of Ideas (closed ideas only for results)."""
    closed = [i for i in ideas if i.state == 'CLOSED']
    results = [i.result_pips() for i in closed]
    wins = [r for r in results if r > 0]
    return {
        'total': len(ideas),
        'pending_baseline': sum(1 for i in ideas if i.state == 'PENDING_BASELINE'),
        'active': sum(1 for i in ideas if i.state == 'ACTIVE'),
        'closed': len(closed),
        'wins': len(wins),
        'win_rate_pct': round(100.0 * len(wins) / len(closed), 1) if closed else None,
        'net_pips': round(sum(results), 1) if closed else None,
    }
