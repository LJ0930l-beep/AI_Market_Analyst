"""Exchange-discovered universe; ranking is input selection, never an entry signal."""
from datetime import datetime, timezone
import math
from ..providers.gateio_provider import GatePublicProvider


def _positive(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


class MarketUniverse:
    def __init__(self, provider_factory=None):
        self.provider_factory = provider_factory or (lambda testnet: GatePublicProvider(testnet=testnet))

    def select(self, config, *, testnet, scheduled_at, positions=(), limit=3, nofx_runtime=None):
        provider = self.provider_factory(testnet)
        contracts = provider.list_active_usdt_contracts(None)
        active = {row['symbol'] for row in contracts}
        contract_by_symbol = {row['symbol']: row for row in contracts}
        tickers = provider.list_contract_tickers()
        selected_scope = active if config['universe_mode'] == 'ALL' else active.intersection(config['symbols'])
        runtime = nofx_runtime if isinstance(nofx_runtime, dict) else {}
        excluded = {
            ''.join(char for char in str(item).upper() if char.isalnum())
            for item in runtime.get('excluded_symbols', [])
            if str(item).strip()
        } if isinstance(runtime.get('excluded_symbols'), (list, tuple)) else set()
        selected_scope = {
            symbol for symbol in selected_scope
            if ''.join(char for char in str(symbol).upper() if char.isalnum()) not in excluded
        }
        eligible = []
        for row in tickers:
            symbol = str(row.get('contract', '')).replace('_', '')
            volume = _positive(row.get('volume_24h_quote') or row.get('volume_24h_settle'))
            price = _positive(row.get('last'))
            if symbol in selected_scope and price and volume:
                high = _positive(row.get('high_24h'))
                low = _positive(row.get('low_24h'))
                change = _finite(row.get('change_percentage'))
                contract = contract_by_symbol[symbol]
                fee = contract.get('taker_fee_rate')
                try:
                    fee = float(fee) if fee is not None else None
                except (TypeError, ValueError):
                    fee = None
                if fee is not None and (not math.isfinite(fee) or fee < 0):
                    fee = None
                eligible.append({
                    'symbol': symbol,
                    'last': price,
                    'volume_24h_quote': volume,
                    'change_24h_pct': change,
                    'range_24h_pct': ((high - low) / price * 100.0) if high >= low > 0 else 0.0,
                    'taker_fee_rate': fee,
                    'contract_source': contract.get('source'),
                })
        # Gate occasionally emits a duplicate ticker while contracts roll.
        # Keep the most liquid observation and rank several distinct market
        # behaviours.  This ranking selects what the model analyses; it is never an
        # entry signal or a claimed win probability.
        by_symbol = {}
        for item in eligible:
            current = by_symbol.get(item['symbol'])
            if current is None or item['volume_24h_quote'] > current['volume_24h_quote']:
                by_symbol[item['symbol']] = item
        ranked = sorted(by_symbol.values(), key=lambda row: (-row['volume_24h_quote'], row['symbol']))
        held = list(dict.fromkeys(str(p.get('symbol') or p.get('instrument_id') or p.get('contract') or '').replace('/', '').split(':')[0].replace('_', '') for p in positions))
        # Existing positions remain reviewable even after a custom universe edit.
        chosen = [symbol for symbol in held if symbol in active][:limit]
        slot = int(scheduled_at.timestamp()) // (config['scan_interval_minutes'] * 60)
        # Keep the most liquid contract as an anchor. With the bounded model
        # budget, rotate one factor leader (up/down/range) and reserve one slot
        # for broad-market discovery. At larger limits all factor leaders stay
        # visible and the final slot still rotates.
        if ranked and len(chosen) < limit:
            anchor = ranked[0]['symbol']
            if anchor not in chosen:
                chosen.append(anchor)
        # Large percentage moves on thin contracts often have no usable closed
        # bars or executable depth. Keep the factor slot within the liquid
        # leaders; the rotating discovery slot still visits every active Gate
        # contract, and CUSTOM scope remains unrestricted.
        liquid_factor_pool = ranked[:max(3, min(24, len(ranked)))]
        factor_leaders = []
        for item in (
            sorted(liquid_factor_pool, key=lambda row: (-row['change_24h_pct'], -row['volume_24h_quote']))[:1]
            + sorted(liquid_factor_pool, key=lambda row: (row['change_24h_pct'], -row['volume_24h_quote']))[:1]
            + sorted(liquid_factor_pool, key=lambda row: (-row['range_24h_pct'], -row['volume_24h_quote']))[:1]
        ):
            if item['symbol'] not in {row['symbol'] for row in factor_leaders}:
                factor_leaders.append(item)
        if factor_leaders:
            factor_offset = slot % len(factor_leaders)
            factor_leaders = factor_leaders[factor_offset:] + factor_leaders[:factor_offset]
        reserve_discovery = 1 if len(ranked) > limit else 0
        factor_slots = max(0, limit - len(chosen) - reserve_discovery)
        for item in factor_leaders[:factor_slots]:
            if item['symbol'] not in chosen and len(chosen) < limit:
                chosen.append(item['symbol'])
        rest = [item['symbol'] for item in ranked if item['symbol'] not in chosen]
        if rest and len(chosen) < limit:
            slots = limit - len(chosen)
            offset = (slot * slots) % len(rest)
            chosen.extend((rest + rest)[offset:offset + min(slots, len(rest))])
        return {
            'status': 'READY' if chosen else 'EMPTY',
            'environment': 'TESTNET' if testnet else 'LIVE',
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'contract_count': len(active), 'eligible_count': len(ranked),
            'selected_symbols': chosen, 'mode': config['universe_mode'],
            'selection': 'positions_then_liquidity_up_momentum_down_momentum_range_and_rotation',
            'candidate_metrics': [by_symbol[symbol] for symbol in chosen if symbol in by_symbol],
            'candidate_sources': list(runtime.get('candidate_sources') or ['gate_active_usdt_perpetuals']),
            'excluded_symbols': sorted(excluded),
            'unsupported_sources': list(runtime.get('unsupported_sources') or []),
        }
