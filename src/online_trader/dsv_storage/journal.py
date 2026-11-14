"""One SQLite writer; round, checkpoint, facts and prepared effects commit together."""

from contextlib import contextmanager
import sqlite3

from online_trader.contracts.codec import canonical_bytes, strict_loads
from online_trader.errors import TraderError


_APPLICATION_ID = 0x44535631
_SCHEMA = (
    'CREATE TABLE scope (id INTEGER PRIMARY KEY CHECK(id=1), payload BLOB NOT NULL)',
    'CREATE TABLE checkpoint (id INTEGER PRIMARY KEY CHECK(id=1), payload BLOB NOT NULL)',
    'CREATE TABLE receipts (kind TEXT, id TEXT, digest TEXT NOT NULL, payload BLOB NOT NULL, PRIMARY KEY(kind,id))',
    'CREATE TABLE native_orders (child TEXT PRIMARY KEY, trading_day TEXT NOT NULL, native_id TEXT NOT NULL, payload BLOB NOT NULL, UNIQUE(trading_day,native_id))',
    'CREATE TABLE native_trades (trading_day TEXT NOT NULL, native_order TEXT, native_trade TEXT, payload BLOB NOT NULL, PRIMARY KEY(trading_day,native_order,native_trade))',
    'CREATE TABLE effects (id TEXT PRIMARY KEY, child TEXT NOT NULL, kind TEXT NOT NULL, request BLOB NOT NULL, result TEXT)',
    'CREATE TABLE events (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload BLOB NOT NULL)',
)


class Journal:
    def __init__(self, path, scope, initial):
        self._db = sqlite3.connect(path, isolation_level=None, timeout=2)
        try:
            with self.transaction():
                application = self._db.execute('PRAGMA application_id').fetchone()[0]
                version = self._db.execute('PRAGMA user_version').fetchone()[0]
                if application == version == 0 and not self._db.execute('SELECT 1 FROM sqlite_master').fetchone():
                    for statement in _SCHEMA:
                        self._db.execute(statement)
                    self._db.execute(f'PRAGMA application_id={_APPLICATION_ID}')
                    self._db.execute('PRAGMA user_version=2')
                    self._db.execute('INSERT INTO scope VALUES(1,?)', (canonical_bytes(scope),))
                    self._db.execute('INSERT INTO checkpoint VALUES(1,?)', (canonical_bytes(initial),))
                elif application != _APPLICATION_ID or version not in (1, 2):
                    raise TraderError('E_DSV_STORE_VERSION', 'Expected a new database or a supported DSV fact store')
                if self._read('SELECT payload FROM scope WHERE id=1') != scope:
                    raise TraderError('E_DSV_SCOPE', 'Fact store belongs to a different account, provider, day or profile')
                if version == 1:
                    self._upgrade_native_facts(scope['trading_day'])
            self._db.execute('PRAGMA synchronous=FULL')
        except BaseException:
            self.close()
            raise

    @contextmanager
    def transaction(self):
        self._db.execute('BEGIN IMMEDIATE')
        try:
            yield self
            self._db.execute('COMMIT')
        except BaseException:
            if self._db.in_transaction:
                self._db.execute('ROLLBACK')
            raise

    def _read(self, sql, args=()):
        row = self._db.execute(sql, args).fetchone()
        return None if row is None else strict_loads(row[0])

    def load(self):
        return self._read('SELECT payload FROM checkpoint WHERE id=1')

    def _upgrade_native_facts(self, day):
        for name, ddl in zip(('native_orders', 'native_trades'), _SCHEMA[3:5]):
            self._db.execute(f'ALTER TABLE {name} RENAME TO {name}_v1')
            self._db.execute(ddl)
        self._db.execute('INSERT INTO native_orders SELECT child,?,native_id,payload FROM native_orders_v1', (day,))
        self._db.execute('INSERT INTO native_trades SELECT ?,native_order,native_trade,payload FROM native_trades_v1', (day,))
        self._db.execute('DROP TABLE native_orders_v1')
        self._db.execute('DROP TABLE native_trades_v1')
        self._db.execute('PRAGMA user_version=2')

    def save(self, state):
        self._db.execute('UPDATE checkpoint SET payload=? WHERE id=1', (canonical_bytes(state),))

    def receipt(self, kind, identity, digest):
        row = self._db.execute('SELECT digest,payload FROM receipts WHERE kind=? AND id=?', (kind, identity)).fetchone()
        if row is None:
            return None
        if row[0] != digest:
            raise TraderError('E_DSV_DUPLICATE_CONFLICT', 'Identifier reused with different contents')
        return strict_loads(row[1])

    def record_receipt(self, kind, identity, digest, value):
        self._db.execute('INSERT INTO receipts VALUES(?,?,?,?)', (kind, identity, digest, canonical_bytes(value)))

    def command_receipt(self, identity):
        return self._read("SELECT payload FROM receipts WHERE kind='COMMAND' AND id=?", (identity,))

    def record_order(self, child, native_id, fact):
        try:
            self._db.execute('INSERT INTO native_orders VALUES(?,?,?,?) ON CONFLICT(child) DO UPDATE SET payload=excluded.payload',
                             (child, fact['native_trading_day'], native_id, canonical_bytes(fact)))
        except sqlite3.IntegrityError as error:
            raise TraderError('E_DSV_NATIVE_ID', 'A native order ID is already bound to another child') from error

    def record_trade(self, order_day, native_order, native_trade, fact):
        identity = (order_day, native_order, native_trade)
        stored = self._read('SELECT payload FROM native_trades WHERE trading_day=? AND native_order=? AND native_trade=?', identity)
        if stored is not None:
            if 'fill_day' not in stored:
                stored['fill_day'] = order_day
            if 'source_time_ns' in fact and 'source_time_ns' not in stored and stored == {
                    key: value for key, value in fact.items() if key != 'source_time_ns'}:
                self._db.execute('UPDATE native_trades SET payload=? WHERE trading_day=? AND native_order=? AND native_trade=?',
                                 (canonical_bytes(fact), *identity))
                self.event('NATIVE_TRADE_TIME_PROVED', fact)
                return False
            if stored != fact:
                raise TraderError('E_DSV_DUPLICATE_CONFLICT', 'Native trade identity has contradictory facts')
            return False
        self._db.execute('INSERT INTO native_trades VALUES(?,?,?,?)', (*identity, canonical_bytes(fact)))
        return True

    def prepare(self, effect):
        self._db.execute('INSERT INTO effects VALUES(?,?,?,?,NULL)',
                         (effect['id'], effect['child_order_id'], effect['kind'], canonical_bytes(effect)))

    def effect_is_prepared(self, effect):
        return self._read('SELECT request FROM effects WHERE id=? AND result IS NULL', (effect['id'],)) == effect

    def resolve(self, effect, result):
        self._db.execute('UPDATE effects SET result=? WHERE id=?', (result, effect['id']))

    def event(self, kind, value):
        self._db.execute('INSERT INTO events(kind,payload) VALUES(?,?)', (kind, canonical_bytes(value)))

    def close(self):
        self._db.close()
