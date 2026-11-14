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
    'CREATE TABLE native_orders (child TEXT PRIMARY KEY, native_id TEXT NOT NULL UNIQUE, payload BLOB NOT NULL)',
    'CREATE TABLE native_trades (native_order TEXT, native_trade TEXT, payload BLOB NOT NULL, PRIMARY KEY(native_order,native_trade))',
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
                    self._db.execute('PRAGMA user_version=1')
                    self._db.execute('INSERT INTO scope VALUES(1,?)', (canonical_bytes(scope),))
                    self._db.execute('INSERT INTO checkpoint VALUES(1,?)', (canonical_bytes(initial),))
                elif application != _APPLICATION_ID or version != 1:
                    raise TraderError('E_DSV_STORE_VERSION', 'Expected a new database or DSV fact store version 1')
                if self._read('SELECT payload FROM scope WHERE id=1') != scope:
                    raise TraderError('E_DSV_SCOPE', 'Fact store belongs to a different account, provider, day or profile')
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

    def record_order(self, child, native_id, fact):
        try:
            self._db.execute('INSERT INTO native_orders VALUES(?,?,?) ON CONFLICT(child) DO UPDATE SET payload=excluded.payload',
                             (child, native_id, canonical_bytes(fact)))
        except sqlite3.IntegrityError as error:
            raise TraderError('E_DSV_NATIVE_ID', 'A native order ID is already bound to another child') from error

    def record_trade(self, native_order, native_trade, fact):
        identity = (native_order, native_trade)
        stored = self._read('SELECT payload FROM native_trades WHERE native_order=? AND native_trade=?', identity)
        if stored is not None:
            if stored != fact:
                raise TraderError('E_DSV_DUPLICATE_CONFLICT', 'Native trade identity has contradictory facts')
            return False
        self._db.execute('INSERT INTO native_trades VALUES(?,?,?)', (*identity, canonical_bytes(fact)))
        return True

    def prepare(self, effect):
        self._db.execute('INSERT INTO effects VALUES(?,?,?,?,NULL)',
                         (effect['id'], effect['child_order_id'], effect['kind'], canonical_bytes(effect)))

    def resolve(self, effect, result):
        self._db.execute('UPDATE effects SET result=? WHERE id=?', (result, effect['id']))

    def event(self, kind, value):
        self._db.execute('INSERT INTO events(kind,payload) VALUES(?,?)', (kind, canonical_bytes(value)))

    def close(self):
        self._db.close()
