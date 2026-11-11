"""SQLite persistence owned by one bounded worker, never by an IPC client.

``mutate`` callbacks are trusted in-process transaction code.  They are not a
remote SQL API.  A failed mutation rolls back and stops further submissions until
a new boot, while read-only diagnostics remain available.
"""

from __future__ import annotations

from concurrent.futures import Future
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from queue import Full, Queue
import json
import re
import sqlite3
import threading
from typing import Any, Callable
from uuid import uuid4

from online_trader.errors import TraderError


MAX_SEQUENCE = 9_223_372_036_854_775_807
INITIAL_RULE_SET = "rules-proposed-fixture-1"
Mutation = Callable[[sqlite3.Connection, int], None]


@dataclass(frozen=True, slots=True)
class CommitAck:
    tx_id: str
    event_seq: int
    durable_seq: int
    kernel_boot_id: str
    state_revision: int
    outcome: str = "COMMITTED"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical(value: Any) -> bytes:
    # Lazy import lets ledger startup diagnostics work before contracts are loaded.
    from online_trader.contracts.codec import canonical_bytes

    return canonical_bytes(value)


def _positive(value: int, name: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_SEQUENCE:
        raise TraderError("E_SCHEMA", f"Invalid {name}")
    return value


class _FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path.with_name(path.name + ".writer.lock")
        self.handle = None

    def acquire(self) -> None:
        import os

        handle = self.path.open("a+b")
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise TraderError("E_LEDGER_IN_USE", "Ledger already has a writer") from exc
        self.handle = handle

    def release(self) -> None:
        import os

        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class Ledger:
    """One account-scoped file and one dedicated SQLite writer thread.

    This file lock only excludes writers to the same resolved file.  The kernel
    must additionally enforce the account-scoped cross-session instance lock.
    """

    def __init__(
        self,
        path: str | Path,
        account_key: str,
        environment: str = "ONLINE_SIM",
        broker_identity: str = "development",
        *,
        queue_capacity: int = 128,
        existing_only: bool = False,
    ) -> None:
        if not isinstance(account_key, str) or not account_key or len(account_key) > 128:
            raise TraderError("E_SCOPE", "Invalid account scope")
        if environment not in {"ONLINE_SIM", "LIVE"} or not broker_identity:
            raise TraderError("E_SCOPE", "Invalid ledger environment")
        if type(queue_capacity) is not int or queue_capacity < 1:
            raise TraderError("E_SCHEMA", "Invalid storage queue capacity")
        if type(existing_only) is not bool:raise TraderError('E_SCHEMA','Existing-ledger mode must be explicit')
        self.path = Path(path).resolve()
        self.account_key = account_key
        self.environment = environment
        self.broker_identity = broker_identity
        self._existing_only=existing_only
        self._queue: Queue[Any] = Queue(maxsize=queue_capacity)
        self._guard = threading.Lock()
        self._closed = False
        self._ready: Future[None] = Future()
        self._boot_id: str | None = None
        self._kernel_epoch = 0
        self._failed = False
        self._thread = threading.Thread(target=self._work, name="OnlineTrader-Ledger", daemon=False)
        self._thread.start()
        try:
            self._ready.result(timeout=15)
        except BaseException:
            self._closed = True
            self._thread.join(timeout=15)
            raise

    @property
    def kernel_boot_id(self) -> str | None:
        return self._boot_id

    @property
    def kernel_epoch(self) -> int:
        return self._kernel_epoch

    @property
    def failed(self) -> bool:
        return self._failed

    def _work(self) -> None:
        lock = _FileLock(self.path)
        conn: sqlite3.Connection | None = None
        try:
            if self._existing_only and not self.path.is_file():
                raise TraderError('E_STORAGE','An existing product ledger is required')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock.acquire()
            conn = (sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True,isolation_level=None,timeout=2)
                    if self._existing_only else sqlite3.connect(self.path,isolation_level=None,timeout=2))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise TraderError("E_STORAGE", "Foreign key enforcement is unavailable")
            if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                raise TraderError("E_STORAGE", "WAL mode is unavailable")
            if conn.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise TraderError("E_STORAGE", "Durable synchronous mode is unavailable")
            self._initialize(conn)
            self._ready.set_result(None)
            while True:
                job = self._queue.get()
                if job is None:
                    break
                operation, args, future = job
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(operation(conn, *args))
                except BaseException as exc:
                    future.set_exception(exc)
        except BaseException as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
        finally:
            if conn is not None:
                conn.close()
            lock.release()

    def _initialize(self, conn: sqlite3.Connection) -> None:
        schema = files("online_trader").joinpath("resources/schema.sql").read_text(encoding="utf-8")
        expected = set(re.findall(r"CREATE TABLE ([a-z_]+)", schema))
        actual = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if not actual:
            if self._existing_only:raise TraderError('E_SCHEMA_MISMATCH','An existing product schema is required')
            conn.executescript(schema)
        elif actual != expected:
            raise TraderError("E_SCHEMA_MISMATCH", "Ledger schema requires explicit migration")
        with closing(sqlite3.connect(":memory:", isolation_level=None)) as reference:
            reference.executescript(schema)
            schema_query = "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            intended = [tuple(row) for row in reference.execute(schema_query)]
            current = [tuple(row) for row in conn.execute(schema_query)]
            if current != intended:
                raise TraderError("E_SCHEMA_MISMATCH", "Ledger definition requires explicit migration")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise TraderError("E_STORAGE", "Ledger integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise TraderError("E_STORAGE", "Ledger foreign key check failed")
        scope = conn.execute("SELECT * FROM ledger_scope WHERE singleton=1").fetchone()
        if scope is None:
            if self._existing_only:raise TraderError('E_SCOPE','Existing-ledger mode cannot create a missing account scope')
            conn.execute(
                "INSERT INTO ledger_scope VALUES (1,?,?,?,?,?)",
                (self.account_key, self.environment, self.broker_identity, 1, _utc_now()),
            )
        elif (scope["account_key"], scope["environment"], scope["broker_identity"], scope["schema_version"]) != (
            self.account_key, self.environment, self.broker_identity, 1
        ):
            raise TraderError("E_SCOPE", "Ledger belongs to a different account or environment")

    def _enqueue(self, operation: Callable[..., Any], *args: Any) -> Future[Any]:
        future: Future[Any] = Future()
        with self._guard:
            if self._closed:
                future.set_exception(TraderError("E_STORAGE_CLOSED", "Ledger is closed"))
                return future
            try:
                self._queue.put_nowait((operation, args, future))
            except Full:
                future.set_exception(TraderError("E_QUEUE_OVERFLOW", "Storage queue is full"))
        return future

    def bootstrap(self, kernel_boot_id: str, executable_hash: str) -> CommitAck:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", kernel_boot_id):
            raise TraderError("E_SCHEMA", "Invalid kernel boot identity")
        if not re.fullmatch(r"[0-9a-f]{64}", executable_hash):
            raise TraderError("E_SCHEMA", "Invalid executable hash")
        return self._enqueue(self._bootstrap, kernel_boot_id, executable_hash).result()

    def _bootstrap(self, conn: sqlite3.Connection, boot_id: str, executable_hash: str) -> CommitAck:
        if self._boot_id is not None:
            raise TraderError("E_BOOT_REUSED", "This writer already has a boot identity")
        if conn.execute("SELECT 1 FROM kernel_boots WHERE kernel_boot_id=?", (boot_id,)).fetchone():
            raise TraderError("E_BOOT_REUSED", "A boot identity cannot be reused")
        epoch = _positive(conn.execute("SELECT COALESCE(MAX(kernel_epoch),0)+1 FROM kernel_boots").fetchone()[0], "kernel epoch")
        sequence = _positive(conn.execute("SELECT COALESCE(MAX(event_seq),0)+1 FROM event_journal").fetchone()[0], "event sequence")
        old_authority = conn.execute("SELECT * FROM authority WHERE singleton=1").fetchone()
        revision = 1 if old_authority is None else old_authority["state_revision"] + 1
        payload = {"account_key": self.account_key, "kernel_boot_id": boot_id, "kernel_epoch": str(epoch), "requires_reconciliation": True}
        raw, digest = self._journal_payload(payload, revision)
        tx_id = str(uuid4())
        now = _utc_now()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO kernel_boots(kernel_boot_id,account_key,kernel_epoch,executable_hash,started_at_utc) VALUES (?,?,?,?,?)", (boot_id, self.account_key, epoch, executable_hash, now))
            self._insert_event(conn, sequence, tx_id, "TX_BOOT", "KERNEL_BOOT", raw, digest, boot_id, None)
            if conn.execute("SELECT 1 FROM rule_sets WHERE rule_set_id=?", (INITIAL_RULE_SET,)).fetchone() is None:
                rule_json = _canonical({"status": "PROPOSED", "source": "G1_engineering"}).decode()
                conn.execute("INSERT INTO rule_sets VALUES (?,?,?,?)", (INITIAL_RULE_SET, rule_json, sha256(rule_json.encode()).hexdigest(), sequence))
                for index in range(1, 21):
                    conn.execute(
                        "INSERT INTO rule_decisions(rule_set_id,b_code,decision_revision,status,final_rule_json,effective_scope_json,recorded_event_seq) VALUES (?,?,1,'PROPOSED',?,?,?)",
                        (INITIAL_RULE_SET, f"B{index:02}", rule_json, _canonical({"environment": self.environment}).decode(), sequence),
                    )
            if conn.execute("SELECT 1 FROM config_revisions").fetchone() is None:
                config = _canonical({"allow_new_risk": False, "source": "G1_engineering"}).decode()
                conn.execute("INSERT INTO config_revisions VALUES (1,?,?,?,'NON_TRADING',?)", (INITIAL_RULE_SET, config, sha256(config.encode()).hexdigest(), sequence))
            config_revision = old_authority["config_revision"] if old_authority else conn.execute("SELECT MIN(config_revision) FROM config_revisions").fetchone()[0]
            risk_revision = 1 if old_authority is None else old_authority["risk_revision"] + 1
            authority_revision = 1 if old_authority is None else old_authority["authority_revision"] + 1
            policy = _canonical({"allowed_origins": [], "restart_requires_reconciliation": True}).decode()
            conn.execute(
                "INSERT INTO authority(singleton,kernel_boot_id,kernel_epoch,strategy_epoch,new_open_status,grant_id,grant_policy_json,config_revision,state_revision,risk_revision,authority_revision,updated_event_seq) "
                "VALUES (1,?,?,NULL,'DISARMED',NULL,?,?,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET kernel_boot_id=excluded.kernel_boot_id,kernel_epoch=excluded.kernel_epoch,strategy_epoch=NULL,new_open_status='DISARMED',grant_id=NULL,grant_policy_json=excluded.grant_policy_json,state_revision=excluded.state_revision,risk_revision=excluded.risk_revision,authority_revision=excluded.authority_revision,updated_event_seq=excluded.updated_event_seq",
                (boot_id, epoch, policy, config_revision, revision, risk_revision, authority_revision, sequence),
            )
            conn.execute("UPDATE strategy_instances SET status='REVOKED',state_revision=state_revision+1,updated_event_seq=? WHERE status IN ('ACTIVE','DRAINING')", (sequence,))
            conn.execute("UPDATE send_attempts SET send_state='UNKNOWN',updated_event_seq=? WHERE send_state IN ('SEND_POSSIBLE','CALL_RETURNED')", (sequence,))
            conn.execute("UPDATE cancel_actions SET status='UNKNOWN',updated_event_seq=? WHERE status IN ('SEND_POSSIBLE','CALL_RETURNED')", (sequence,))
            conn.execute("UPDATE order_intents SET status='UNKNOWN',state_revision=state_revision+1,updated_event_seq=? WHERE status IN ('PREPARED','ACTIVE') AND intent_id IN (SELECT o.intent_id FROM child_orders o JOIN send_attempts a ON a.child_order_id=o.child_order_id WHERE a.send_state='UNKNOWN')", (sequence,))
            conn.execute("UPDATE reservations SET status='UNKNOWN',state_revision=state_revision+1,updated_event_seq=? WHERE status='HELD' AND intent_id IN (SELECT intent_id FROM order_intents WHERE status='UNKNOWN')", (sequence,))
            conn.execute("UPDATE reconciliation_parts SET status='INVALIDATED' WHERE status='COMPLETE'")
            conn.execute("UPDATE reconciliation_runs SET status='INVALIDATED' WHERE status='COMPLETE'")
            conn.execute("UPDATE operator_commands SET status='NEEDS_RECONCILIATION',receipt_revision=receipt_revision+1,updated_event_seq=? WHERE status IN ('ACCEPTED_PENDING_COMMIT','COMMITTED','EXECUTING')", (sequence,))
            conn.commit()
        except BaseException:
            conn.rollback()
            self._failed = True
            raise
        self._boot_id, self._kernel_epoch = boot_id, epoch
        return CommitAck(tx_id, sequence, sequence, boot_id, revision)

    @staticmethod
    def _journal_payload(payload: dict[str, Any], state_revision: int) -> tuple[str, str]:
        raw = _canonical({"data": payload, "commit": {"state_revision": str(_positive(state_revision, "state revision"))}})
        return raw.decode("utf-8"), sha256(raw).hexdigest()

    @staticmethod
    def _insert_event(conn: sqlite3.Connection, sequence: int, tx_id: str, kind: str, event_type: str, raw: str, digest: str, boot_id: str, ingress_seq: int | None) -> None:
        now = _utc_now()
        conn.execute(
            "INSERT INTO event_journal(event_seq,tx_id,tx_kind,kernel_boot_id,ingress_seq,event_type,payload_json,payload_hash,observed_at_utc,committed_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sequence, tx_id, kind, boot_id, ingress_seq, event_type, raw, digest, now, now),
        )

    def submit(self, tx_kind: str, event_type: str, payload: dict[str, Any], mutate: Mutation | None = None, *, tx_id: str | None = None, event_seq: int | None = None, expected_boot_id: str | None = None, state_revision: int = 1, ingress_seq: int | None = None) -> Future[CommitAck]:
        if not isinstance(payload, dict):
            raise TraderError("E_SCHEMA", "Journal payload must be an object")
        if ingress_seq is not None and (type(ingress_seq) is not int or not 0 <= ingress_seq <= MAX_SEQUENCE):
            raise TraderError("E_SCHEMA", "Invalid ingress sequence")
        if event_seq is not None:
            _positive(event_seq, "event sequence")
        raw, digest = self._journal_payload(payload, state_revision)
        return self._enqueue(self._submit, tx_kind, event_type, raw, digest, mutate, tx_id or str(uuid4()), event_seq, expected_boot_id, state_revision, ingress_seq)

    def _submit(self, conn: sqlite3.Connection, kind: str, event_type: str, raw: str, digest: str, mutate: Mutation | None, tx_id: str, requested_seq: int | None, expected_boot_id: str | None, state_revision: int, ingress_seq: int | None) -> CommitAck:
        if self._boot_id is None:
            raise TraderError("E_NOT_READY", "Ledger has not bootstrapped")
        if expected_boot_id is not None and expected_boot_id != self._boot_id:
            raise TraderError("E_BOOT_MISMATCH", "Submission belongs to a different boot")
        existing = conn.execute("SELECT * FROM event_journal WHERE tx_id=?", (tx_id,)).fetchone()
        if existing is not None:
            if (existing["tx_kind"], existing["event_type"], existing["payload_hash"]) != (kind, event_type, digest) or (requested_seq is not None and requested_seq != existing["event_seq"]):
                raise TraderError("E_DUPLICATE_CONFLICT", "Transaction identity has conflicting content")
            original_revision = int(json.loads(existing["payload_json"])["commit"]["state_revision"])
            return CommitAck(tx_id, existing["event_seq"], existing["event_seq"], existing["kernel_boot_id"], original_revision)
        if self._failed:
            raise TraderError("E_STORAGE_BLOCKED", "Ledger requires a new boot after failed submission")
        sequence = _positive(conn.execute("SELECT COALESCE(MAX(event_seq),0)+1 FROM event_journal").fetchone()[0], "event sequence")
        if requested_seq is not None and requested_seq != sequence:
            raise TraderError("E_SEQUENCE", "Submission is not the next durable sequence")
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._insert_event(conn, sequence, tx_id, kind, event_type, raw, digest, self._boot_id, ingress_seq)
            if mutate is not None:
                # Callbacks can update business data, but cannot commit early,
                # disable enforcement or change the runtime database definition.
                forbidden = {
                    sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
                    sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
                    sqlite3.SQLITE_PRAGMA, sqlite3.SQLITE_ALTER_TABLE,
                    sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE,
                    sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_DROP_INDEX,
                    sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_DROP_TRIGGER,
                    sqlite3.SQLITE_CREATE_VIEW, sqlite3.SQLITE_DROP_VIEW,
                }
                conn.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action in forbidden else sqlite3.SQLITE_OK)
                try:
                    mutate(conn, sequence)
                finally:
                    conn.set_authorizer(None)
            conn.commit()
        except BaseException:
            conn.rollback()
            self._failed = True
            raise
        return CommitAck(tx_id, sequence, sequence, self._boot_id, state_revision)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return self._enqueue(self._query, sql, tuple(params)).result()

    def commit_statement_fees(self,payload,*,expected_sequence,boot_id,executable_hash):
        """One closed-account, non-trading maintenance transaction.

        No ordinary bootstrap, command execution or SDK is constructed. The
        caller holds the host account lock and has verified exact owned fills.
        This narrow writer can append fees and revoke entry authority only.
        """
        if not self._existing_only or self._boot_id is not None or self.environment!='ONLINE_SIM' or self.broker_identity!='development':
            raise TraderError('E_AUTH','Statement fees require an unbootstrapped existing development ledger')
        from online_trader.contracts.codec import canonical_bytes,strict_loads
        frozen=strict_loads(canonical_bytes(payload))
        if (type(expected_sequence) is not int or expected_sequence<1 or
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',boot_id) or
                not re.fullmatch('[0-9a-f]{64}',executable_hash)):
            raise TraderError('E_SCHEMA','Invalid statement maintenance identity')
        return self._enqueue(self._commit_statement_fees,frozen,expected_sequence,boot_id,executable_hash)

    def _commit_statement_fees(self,conn,payload,expected,boot_id,executable_hash):
        from online_trader.accounting.book import validate_fee
        if self._failed:raise TraderError('E_STORAGE_BLOCKED','The writer needs a fresh verified session after failure')
        fees=payload.get('money_fees');receipt=payload.get('fee_import')
        if (type(fees) is not list or not 1<=len(fees)<=20000 or type(receipt) is not dict or
                receipt.get('account_key')!=self.account_key or receipt.get('environment')!=self.environment):
            raise TraderError('E_SCOPE','Invalid statement fee scope')
        for fee in fees:
            validate_fee(fee)
            if fee['quality']!='CONFIRMED':raise TraderError('E_SCOPE','A statement registers confirmed fees only')
        tx_id=receipt['import_id'];sequence=_positive(expected+1,'statement event sequence')
        try:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT COALESCE(MAX(event_seq),0) FROM event_journal').fetchone()[0]!=expected:
                raise TraderError('E_FACT_BARRIER','Statement preview no longer matches the ledger checkpoint')
            # Recheck the concrete existing projections under the write lock.
            if conn.execute("SELECT 1 FROM position_cycles WHERE remaining_qty!=0 OR (lifecycle!='CLOSED' AND confirmed_open_qty>0) LIMIT 1").fetchone():
                raise TraderError('E_NOT_READY','Owned cycles must be closed before offline fee registration')
            if conn.execute("SELECT 1 FROM child_orders c JOIN send_attempts a ON a.child_order_id=c.child_order_id WHERE c.terminal_evidence_json IS NULL OR json_extract(c.terminal_evidence_json,'$.confirmed_fill_qty') IS NULL OR json_extract(c.terminal_evidence_json,'$.confirmed_fill_qty')!=c.confirmed_fill_qty OR a.send_state IN ('UNKNOWN','SEND_POSSIBLE','CALL_RETURNED') LIMIT 1").fetchone():
                raise TraderError('E_NOT_READY','Order facts are not terminal and fully reconciled')
            if conn.execute("SELECT 1 FROM cancel_actions WHERE status IN ('UNKNOWN','SEND_POSSIBLE','CALL_RETURNED') LIMIT 1").fetchone():
                raise TraderError('E_NOT_READY','Cancellation facts remain uncertain')
            authority=conn.execute('SELECT * FROM authority WHERE singleton=1').fetchone()
            if authority is None:raise TraderError('E_SCOPE','Existing authority scope is missing')
            epoch=conn.execute('SELECT COALESCE(MAX(kernel_epoch),0)+1 FROM kernel_boots').fetchone()[0]
            revision=authority['state_revision']+1;now=_utc_now()
            conn.execute('INSERT INTO kernel_boots VALUES(?,?,?,?,?,?)',
                (boot_id,self.account_key,epoch,executable_hash,now,now))
            raw,digest=self._journal_payload(payload,revision)
            self._insert_event(conn,sequence,tx_id,'TX_COUNTER','STATEMENT_FEES_REGISTERED',raw,digest,boot_id,None)
            for fee in fees:
                fill=conn.execute('SELECT fill_id FROM fill_facts WHERE trade_key=?',(fee['trade_key'],)).fetchone()
                if fill is None:raise TraderError('E_OWNERSHIP','Statement fee has no confirmed owned fill')
                conn.execute('INSERT INTO fee_observations VALUES(?,?,?,?,?,?,?,?)',
                    (fee['evidence_id'],fill['fill_id'],'STATEMENT','CONFIRMED',fee['amount'],'CNY',tx_id,sequence))
            conn.execute("UPDATE authority SET kernel_boot_id=?,kernel_epoch=?,strategy_epoch=NULL,new_open_status='DISARMED',grant_id=NULL,grant_policy_json=?,state_revision=?,risk_revision=risk_revision+1,authority_revision=authority_revision+1,updated_event_seq=? WHERE singleton=1",
                (boot_id,epoch,_canonical({'allowed_origins':[],'non_trading_fee_maintenance':True}).decode(),revision,sequence))
            conn.execute("UPDATE strategy_instances SET status='REVOKED',state_revision=state_revision+1,updated_event_seq=? WHERE status IN ('ACTIVE','DRAINING')",(sequence,))
            conn.commit()
        except BaseException:
            conn.rollback();self._failed=True;raise
        self._boot_id=boot_id;self._kernel_epoch=epoch
        return CommitAck(tx_id,sequence,sequence,boot_id,revision)

    @staticmethod
    def _query(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        if not re.match(r"\s*(SELECT|WITH|PRAGMA|EXPLAIN)\b", sql, re.IGNORECASE):
            raise TraderError("E_AUTH", "Only read-only ledger queries are allowed")
        read_pragmas = {"foreign_keys", "journal_mode", "synchronous", "foreign_key_check", "quick_check", "integrity_check", "table_info", "foreign_key_list", "index_list", "index_info"}
        parametrized_pragmas = {"table_info", "foreign_key_list", "index_list", "index_info"}
        def authorizer(action: int, first: str | None, second: str | None, database: str | None, trigger: str | None) -> int:
            if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE}:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and second not in {"load_extension", "writefile", "readfile"}:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_PRAGMA and first in read_pragmas and (second is None or first in parametrized_pragmas):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        conn.set_authorizer(authorizer)
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        except sqlite3.DatabaseError as exc:
            raise TraderError("E_AUTH", "Ledger read query was refused") from exc
        finally:
            conn.set_authorizer(None)

    def backup(self, destination: str | Path) -> None:
        target = Path(destination).resolve()
        if target == self.path or target.exists():
            raise TraderError("E_STORAGE", "Backup destination must be a new file")
        self._enqueue(self._backup, target).result()

    @staticmethod
    def _backup(conn: sqlite3.Connection, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Reserve a new path atomically; never replace an existing backup.
        with target.open("xb"):
            pass
        with closing(sqlite3.connect(target)) as backup:
            conn.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise TraderError("E_STORAGE", "Backup integrity check failed")

    def close(self) -> None:
        if threading.current_thread() is self._thread:
            raise TraderError("E_STORAGE", "Writer cannot close itself")
        with self._guard:
            if self._closed:
                return
            self._closed = True
        self._queue.put(None)
        self._thread.join(timeout=15)
        if self._thread.is_alive():
            raise TraderError("E_STORAGE", "Ledger writer did not stop")

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
