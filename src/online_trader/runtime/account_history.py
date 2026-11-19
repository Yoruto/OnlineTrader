"""Paged read-only account observations retained by the original simulation owner."""

from pathlib import Path
import sqlite3

from online_trader.contracts.codec import canonical_bytes, strict_loads
from online_trader.domain.values import MAX_SEQUENCE, sequence
from online_trader.errors import TraderError
from online_trader.runtime.account_day_history import history_database


def account_history(ledger, account_key, *, before=None, limit=5):
    if type(limit) is not int or not 1 <= limit <= 20:
        raise TraderError('E_SCHEMA', 'History page size must be between 1 and 20')
    threshold = sequence(before, positive=True) - 1 if before is not None else MAX_SEQUENCE
    path = Path(ledger).resolve()
    if not path.is_file():
        raise TraderError('E_STORAGE', 'The selected account has no execution ledger')
    try:
        with history_database(path, account_key) as stream:
            stream.execute('BEGIN')
            scope = stream.execute('SELECT account_key, environment FROM ledger_scope').fetchall()
            if scope != [(account_key, 'ONLINE_SIM')]:
                raise TraderError('E_SCOPE', 'History does not belong to the selected simulation account')
            checkpoint = stream.execute('SELECT coalesce(max(event_seq),0) FROM event_journal').fetchone()[0]
            rows = stream.execute('''SELECT event_seq, observed_at_utc, payload_json
                FROM event_journal WHERE event_seq <= ? AND event_type IN
                ('ONLINE_SIMULATION_BASELINE','ONLINE_DSV_ACCOUNT_REFRESH')
                ORDER BY event_seq DESC LIMIT ?''', (threshold, limit + 1)).fetchall()
    except sqlite3.Error:
        raise TraderError('E_STORAGE', 'Cannot read the account history checkpoint') from None
    observations = []
    size = 0
    next_before = None
    for seq, observed, raw in rows:
        try:
            value = strict_loads(raw)['data']['snapshot']
            if type(value) is not dict or not {'account_key', 'trading_day', 'complete'} <= value.keys():
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise TraderError('E_STORAGE', 'The original broker observation is incomplete') from None
        if value['account_key'] != account_key:
            raise TraderError('E_SCOPE', 'A historical observation belongs to another account')
        item = {key: value.get(key) for key in ('trading_day', 'gateway_identity', 'source_epoch', 'complete',
                                           'funds', 'positions', 'orders', 'trades')}
        item.update(event_seq=str(seq), observed_at_utc=observed)
        size += len(canonical_bytes(item))
        if len(observations) == limit or size > 900_000:
            if not observations:
                raise TraderError('E_WORKBENCH_CAPACITY', 'The complete observation exceeds one history response')
            next_before = observations[-1]['event_seq']
            break
        observations.append(item)
    return {'account_key': account_key, 'read_at_event_seq': str(checkpoint), 'next_before': next_before,
            'coverage': 'OBSERVED_BROKER_SNAPSHOTS', 'observations': observations}
