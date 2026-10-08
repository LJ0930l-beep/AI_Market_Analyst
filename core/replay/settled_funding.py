"""Point-in-time archive funding facts, explicitly distinct from live estimates.

This helper is not enabled in the running frozen replay. Integration requires
a new experiment and inclusion in its source fingerprint.
"""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from .ai_history import digest, utc

SOURCES = {'binance': 'binance_official_funding_archive', 'gate': 'gate_public_funding_history'}


def settled_funding_as_of(records, symbols, as_of, *, venue, default_publication_lag_seconds=60):
    """Use explicit availability, or a disclosed conservative research lag.

    Settlement timestamp is not an attested publication timestamp. Missing
    publication metadata therefore remains an assumption, not live feed proof.
    No future rate, predicted next rate, OI or other-venue rate is substituted.
    """
    if venue not in SOURCES or type(default_publication_lag_seconds) is not int or default_publication_lag_seconds < 0:
        raise ValueError('SETTLED_FUNDING_POLICY_INVALID')
    symbols = tuple(symbols)
    point, selected = utc(as_of), set(symbols)
    latest = {}
    for raw in records:
        symbol = raw.get('instrument_id') or raw.get('symbol')
        if symbol not in selected:
            continue
        payment = utc(raw.get('payment_time') or raw.get('timestamp'))
        if payment > point:
            continue  # Do not inspect a future value or export future counts.
        explicit = raw.get('available_at') or raw.get('known_at')
        known = utc(explicit) if explicit else payment + timedelta(seconds=default_publication_lag_seconds)
        if known < payment:
            raise ValueError('SETTLED_FUNDING_AVAILABILITY_BEFORE_PAYMENT')
        if known > point:
            continue
        if raw.get('source') != SOURCES[venue]:
            raise ValueError('SETTLED_FUNDING_SOURCE_VENUE_MISMATCH')
        try:
            if isinstance(raw.get('rate'), bool):
                raise ValueError('boolean rate')
            rate = Decimal(str(raw['rate']))
        except (KeyError, InvalidOperation, ValueError) as exc:
            raise ValueError('SETTLED_FUNDING_RATE_INVALID') from exc
        if not rate.is_finite():
            raise ValueError('SETTLED_FUNDING_RATE_INVALID')
        row = {'status': 'HISTORICAL_SETTLED_RATE', 'venue': venue, 'symbol': symbol,
            'source': raw['source'], 'payment_time': payment.isoformat(), 'available_at': known.isoformat(),
            'availability_basis': 'EXPLICIT_RECORD_METADATA' if explicit else 'PAYMENT_PLUS_RESEARCH_LAG_ASSUMPTION',
            'rate_fraction': str(rate), 'rate_percent': str(rate * 100),
            'age_since_payment_seconds': (point - payment).total_seconds(),
            'live_or_next_predicted_rate': False}
        row['fact_sha256'] = digest(row)
        previous = latest.get(symbol)
        if previous is not None and utc(previous['payment_time']) == payment:
            if previous != row:
                raise ValueError('SETTLED_FUNDING_CONFLICTING_KNOWN_RECORDS')
            continue
        if previous is None or payment > utc(previous['payment_time']):
            latest[symbol] = row
    return {'scope': 'HISTORICAL_SETTLED_FUNDING_NOT_CURRENT_PREDICTION_OR_GATE_FILL',
        'as_of': point.isoformat(), 'venue': venue, 'publication_lag_assumption_seconds': default_publication_lag_seconds,
        'rates': {symbol: latest.get(symbol, {'status': 'NO_KNOWN_SETTLED_RATE', 'venue': venue,
                                           'symbol': symbol, 'rate_fraction': None, 'rate_percent': None})
                  for symbol in symbols},
        'open_interest_status': 'UNAVAILABLE_NOT_INFERRED', 'private_exchange_calls': 0}
