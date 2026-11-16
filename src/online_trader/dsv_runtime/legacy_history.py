"""Read-only native history export; no SDK connection or owner is created."""

from online_trader.contracts.codec import strict_loads
from online_trader.domain.values import decimal_value, quantity
from online_trader.errors import TraderError
from online_trader.gateway.ports import OnlineSimulationPort


def _invalid(message):
    raise TraderError('E_DSV_MIGRATION_HISTORY', message)


def _native(value):
    if type(value) is not str or not value.strip() or len(value) > 512 or not value.isprintable():
        _invalid('Historical native identity is missing or invalid')
    return value


def _codec(db):
    scope = db.execute('SELECT account_key,environment,broker_identity FROM ledger_scope WHERE singleton=1').fetchone()
    prefixes = {'TQKQ': ('SIM:KQ:', 'kq-'), 'TQ_SIMNOW': ('SIM:SIMNOW:', 'sn-')}
    if scope is None or scope['environment'] != 'ONLINE_SIM' or scope['broker_identity'] not in prefixes:
        _invalid('Native history requires the original supported simulation provider')
    account_prefix, trade_prefix = prefixes[scope['broker_identity']]
    if not isinstance(scope['account_key'], str) or not scope['account_key'].startswith(account_prefix):
        _invalid('Historical account and provider disagree')
    codec = OnlineSimulationPort()  # Identity codec only; never bound to a sender.
    codec.trade_prefix = trade_prefix
    return codec


def _trade_id(codec, fact):
    key = fact['trade_key']
    if type(key) is not str or not codec.is_native_trade_key(key):
        _invalid('Historical trade key is outside the original provider namespace')
    native = _native(codec.native_trade_id(fact))
    # Legacy decoding recovers from the key. A supplied original field must not
    # contradict that value; encoded IDs must round-trip to their exact key.
    if codec.trade_key(native) != key or ('native_trade_id' in fact and fact['native_trade_id'] != native):
        _invalid('Historical trade key does not bind its original native identity')
    return native


def selected_native_history(db, contracts, *, recovery_event):
    """Export complete selected orders, rejecting ambiguous or incomplete fills.

    The caller has checked the ledger schema, account binding, journal hashes
    and cold-state conditions. This additional check never repairs source data.
    """
    try:
        return _selected(db, contracts, recovery_event)
    except TraderError as error:
        if error.code == 'E_DSV_MIGRATION_HISTORY':
            raise
        raise TraderError('E_DSV_MIGRATION_HISTORY', 'Historical native fact cannot be decoded exactly') from error
    except (KeyError, TypeError, ValueError) as error:
        raise TraderError('E_DSV_MIGRATION_HISTORY', 'Malformed historical native fact') from error


def _selected(db, contracts, recovery_event):
    codec = _codec(db)
    orders, history, owners = {}, {}, {}
    for event_type, raw in db.execute('SELECT event_type,payload_json FROM event_journal ORDER BY event_seq'):
        data = strict_loads(raw)['data']
        for cycle in ([data['cycle']] if data.get('cycle') else []) + data.get('cycles', []):
            if cycle['plan']['contract_id'] not in contracts:
                continue
            for child, native in cycle.get('native_order_keys', {}).items():
                _native(native)
                if child in orders and orders[child] != native:
                    _invalid('A historical child changed its native order identity')
                if native in owners and owners[native] != child:
                    _invalid('Different historical children share a native order identity')
                orders[child], owners[native] = native, child
        if event_type == recovery_event:
            review = data['allocation_review']
            order, trade = review['external_order'], review['external_trade']
            if order['contract_id'] not in contracts:
                continue
            native, trade_id = _native(order['order_id']), _native(trade['trade_id'])
            row = {key: order[key] for key in ('contract_id', 'direction', 'limit_price', 'quantity', 'offset')}
            row.update(filled=quantity(trade['quantity'], positive=True),
                       fills={trade_id: {'quantity': trade['quantity'], 'price': trade['price']}})
            if native in history and history[native] != row:
                _invalid('Reviewed external native history conflicts with an earlier review')
            history[native] = row
    for order in db.execute('SELECT c.child_order_id,p.contract_id,c.confirmed_fill_qty,c.limit_price,'
                            'c.original_qty,c.offset_kind,p.direction,i.effect FROM child_orders c '
                            'JOIN order_intents i USING(intent_id) JOIN position_cycles p USING(position_cycle_id) '
                            "WHERE EXISTS (SELECT 1 FROM send_attempts WHERE child_order_id=c.child_order_id "
                            "AND send_state='CONFIRMED')"):
        if order['contract_id'] not in contracts:
            continue
        child = order['child_order_id']
        if child not in orders:
            _invalid('Confirmed historical child lacks its native order binding')
        native = orders[child]
        if native in history:
            _invalid('Owned and reviewed external orders share a native identity')
        side = 'BUY' if (order['direction'] == 'LONG') == (order['effect'] == 'OPEN') else 'SELL'
        history[native] = {'contract_id': order['contract_id'], 'filled': order['confirmed_fill_qty'],
            'direction': side, 'limit_price': order['limit_price'], 'quantity': order['original_qty'],
            'fills': {}, 'offset': {'CLOSE_TODAY': 'CLOSETODAY', 'CLOSE_YESTERDAY': 'CLOSE'}.get(
                order['offset_kind'], order['offset_kind'])}
    for (raw,) in db.execute('SELECT raw_fact_json FROM fill_facts'):
        fact = strict_loads(raw)
        native = orders.get(fact['child_order_id'])
        if native not in history:
            continue
        trade = _trade_id(codec, fact)
        if trade in history[native]['fills']:
            _invalid('Repeated historical native trade identity must not overwrite a fill')
        history[native]['fills'][trade] = {'quantity': quantity(fact['quantity'], positive=True), 'price': fact['price']}
    trade_owners = {}
    for native, row in history.items():
        total = 0
        for trade, fill in row['fills'].items():
            if trade in trade_owners and trade_owners[trade] != native:
                _invalid('Different historical orders share a native trade identity')
            trade_owners[trade] = native
            total += quantity(fill['quantity'], positive=True)
            decimal_value(fill['price'], positive=True)
        decimal_value(row['limit_price'], positive=True)
        if total != quantity(row['filled']) or total > quantity(row['quantity'], positive=True):
            _invalid('Historical native fill quantities do not match the confirmed order total')
    return history
