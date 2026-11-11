from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from online_trader.errors import TraderError
from online_trader.ledger import Ledger


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "account.db"
        self.ledgers: list[Ledger] = []

    def tearDown(self) -> None:
        for ledger in reversed(self.ledgers):
            ledger.close()
        self.temporary.cleanup()

    def open(self, *, account_key: str = "DEV:test:account-1", environment: str = "ONLINE_SIM", boot: str | None = "boot-one") -> Ledger:
        ledger = Ledger(self.path, account_key, environment)
        self.ledgers.append(ledger)
        if boot:
            ledger.bootstrap(boot, "0" * 64)
        return ledger

    def assert_code(self, code: str, operation) -> None:
        with self.assertRaises(TraderError) as raised:
            operation()
        self.assertEqual(code, raised.exception.code)

    def test_schema_durable_modes_and_unapproved_bootstrap(self) -> None:
        ledger = self.open()
        self.assertEqual(37, ledger.query("SELECT count(*) AS n FROM sqlite_master WHERE type='table'")[0]["n"])
        self.assertEqual(1, ledger.query("PRAGMA foreign_keys")[0]["foreign_keys"])
        self.assertEqual("wal", ledger.query("PRAGMA journal_mode")[0]["journal_mode"])
        self.assertEqual(2, ledger.query("PRAGMA synchronous")[0]["synchronous"])
        self.assertEqual([], ledger.query("PRAGMA foreign_key_check"))
        decisions = ledger.query("SELECT status,count(*) AS n FROM rule_decisions GROUP BY status")
        self.assertEqual([{"status": "PROPOSED", "n": 20}], decisions)
        authority = ledger.query("SELECT * FROM authority")[0]
        self.assertEqual("DISARMED", authority["new_open_status"])
        self.assertIsNone(authority["grant_id"])

    def test_scope_and_environment_are_immutable(self) -> None:
        self.open().close()
        self.assert_code("E_SCOPE", lambda: Ledger(self.path, "DEV:another-account"))
        self.assert_code("E_SCOPE", lambda: Ledger(self.path, "DEV:test:account-1", "LIVE"))

    def test_second_writer_to_same_file_is_refused(self) -> None:
        self.open()
        self.assert_code("E_LEDGER_IN_USE", lambda: Ledger(self.path, "DEV:test:account-1"))

    def test_read_api_refuses_writes_and_write_pragmas(self) -> None:
        ledger = self.open()
        for sql in ["DELETE FROM authority", "PRAGMA foreign_keys=OFF", "WITH x AS (SELECT 1) DELETE FROM authority", "PRAGMA user_version=500"]:
            self.assert_code("E_AUTH", lambda sql=sql: ledger.query(sql))
        self.assertEqual(1, ledger.query("PRAGMA foreign_keys")[0]["foreign_keys"])
        self.assertEqual(1, ledger.query("SELECT count(*) AS n FROM authority")[0]["n"])

    def test_ack_only_after_committed_mutation_and_writer_serializes(self) -> None:
        ledger = self.open()
        entered, release = threading.Event(), threading.Event()
        writer_ids: list[int] = []
        def first(conn, seq):
            writer_ids.append(threading.get_ident())
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test release timed out")
        first_future = ledger.submit("TX_OBSERVATION", "FIRST", {}, first)
        self.assertTrue(entered.wait(5))
        try:
            second_future = ledger.submit("TX_OBSERVATION", "SECOND", {}, lambda conn, seq: writer_ids.append(threading.get_ident()))
            self.assertFalse(first_future.done())
            self.assertFalse(second_future.done())
        finally:
            release.set()
        self.assertEqual(2, first_future.result(5).durable_seq)
        self.assertEqual(3, second_future.result(5).durable_seq)
        self.assertEqual(1, len(set(writer_ids)))
        self.assertNotEqual(threading.get_ident(), writer_ids[0])

    def test_failure_rolls_back_and_stops_dependent_writes(self) -> None:
        ledger = self.open()
        def fail(conn, seq):
            conn.execute("INSERT INTO day_blocks VALUES ('block','ACCOUNT','2026-11-09','TEST','{}','ACTIVE',?,NULL)", (seq,))
            raise RuntimeError("Injected mutation failure")
        with self.assertRaises(RuntimeError):
            ledger.submit("TX_AUTH", "FAIL", {}, fail, event_seq=2).result(5)
        self.assertEqual([], ledger.query("SELECT * FROM day_blocks"))
        self.assertEqual(1, ledger.query("SELECT MAX(event_seq) AS n FROM event_journal")[0]["n"])
        self.assertTrue(ledger.failed)
        self.assert_code("E_STORAGE_BLOCKED", lambda: ledger.submit("TX_OBSERVATION", "SKIP_FAILED", {}, event_seq=3).result(5))
        ledger.close()
        reopened = self.open(boot="boot-after-failure")
        self.assertEqual(2, reopened.query("SELECT MAX(event_seq) AS n FROM event_journal")[0]["n"])
        self.assertEqual("DISARMED", reopened.query("SELECT new_open_status FROM authority")[0]["new_open_status"])

    def test_duplicate_transaction_never_repeats_mutation(self) -> None:
        ledger = self.open()
        mutations: list[int] = []
        arguments = dict(tx_id="tx-idempotent", state_revision=7)
        first = ledger.submit("TX_OBSERVATION", "OBSERVE", {"value": "123.4"}, lambda conn, seq: mutations.append(seq), **arguments).result(5)
        second = ledger.submit("TX_OBSERVATION", "OBSERVE", {"value": "123.4"}, lambda conn, seq: mutations.append(seq), **arguments).result(5)
        self.assertEqual(first, second)
        self.assertEqual([2], mutations)
        ledger.close()
        reopened = self.open(boot="boot-two")
        replay = reopened.submit("TX_OBSERVATION", "OBSERVE", {"value": "123.4"}, lambda conn, seq: self.fail("Duplicate mutation ran after restart"), **arguments).result(5)
        self.assertEqual(first, replay)
        self.assertEqual("boot-one", replay.kernel_boot_id)
        self.assertEqual(7, replay.state_revision)

    def test_duplicate_conflict_is_not_storage_failure(self) -> None:
        ledger = self.open()
        ledger.submit("TX_OBSERVATION", "OBSERVE", {"value": 1}, tx_id="tx-conflict").result(5)
        self.assert_code("E_DUPLICATE_CONFLICT", lambda: ledger.submit("TX_OBSERVATION", "OBSERVE", {"value": 2}, tx_id="tx-conflict").result(5))
        self.assertFalse(ledger.failed)
        self.assertEqual(3, ledger.submit("TX_OBSERVATION", "AFTER_CONFLICT", {}).result(5).event_seq)

    def test_wrong_boot_and_sequence_cannot_commit(self) -> None:
        ledger = self.open()
        self.assert_code("E_BOOT_MISMATCH", lambda: ledger.submit("TX_OBSERVATION", "STALE", {}, expected_boot_id="old-boot").result(5))
        self.assert_code("E_SEQUENCE", lambda: ledger.submit("TX_OBSERVATION", "GAP", {}, event_seq=3).result(5))
        self.assertEqual(2, ledger.submit("TX_OBSERVATION", "CONTIGUOUS", {}, event_seq=2, expected_boot_id="boot-one").result(5).durable_seq)

    @staticmethod
    def seed_unresolved(conn: sqlite3.Connection, seq: int) -> None:
        conn.execute("INSERT INTO position_cycles VALUES ('cycle','DEV.TEST','LONG','2026-11-09','MANAGED','ENTRY_PENDING',0,0,0,0,NULL,1,?,?)", (seq, seq))
        conn.execute("INSERT INTO order_intents VALUES ('intent','cycle','decision','OPEN','STRATEGY',3,NULL,'ACTIVE','[]',1,NULL,1,?,?)", (seq, seq))
        conn.execute("INSERT INTO child_orders VALUES ('child','intent',1,'OPEN','1000',3,0,'NOT_SEEN',NULL,1,?)", (seq,))
        conn.execute("INSERT INTO send_attempts VALUES ('attempt','child',1,'SEND_POSSIBLE',?,'boot-one',NULL,?,?)", ("0" * 64, seq, seq))
        conn.execute("INSERT INTO reservations VALUES ('reserve','intent','child','MARGIN','300',NULL,'HELD',1,?)", (seq,))
        conn.execute("UPDATE authority SET new_open_status='ARMED',grant_id='test-unapproved-state' WHERE singleton=1")

    def test_restart_retains_unknown_send_occupancy_and_revokes_authority(self) -> None:
        ledger = self.open()
        ledger.submit("TX_INTENT", "PRE_SEND", {}, self.seed_unresolved).result(5)
        ledger.close()
        reopened = self.open(boot="boot-two")
        self.assertEqual(2, reopened.kernel_epoch)
        self.assertEqual("UNKNOWN", reopened.query("SELECT send_state FROM send_attempts")[0]["send_state"])
        self.assertEqual("UNKNOWN", reopened.query("SELECT status FROM order_intents")[0]["status"])
        reservation = reopened.query("SELECT status,amount FROM reservations")[0]
        self.assertEqual({"status": "UNKNOWN", "amount": "300"}, reservation)
        authority = reopened.query("SELECT new_open_status,grant_id,kernel_boot_id FROM authority")[0]
        self.assertEqual("DISARMED", authority["new_open_status"])
        self.assertIsNone(authority["grant_id"])
        self.assertEqual("boot-two", authority["kernel_boot_id"])

    def test_boot_identity_cannot_be_reused(self) -> None:
        ledger = self.open()
        self.assert_code("E_BOOT_REUSED", lambda: ledger.bootstrap("boot-one", "0" * 64))
        ledger.close()
        unopened = self.open(boot=None)
        self.assert_code("E_BOOT_REUSED", lambda: unopened.bootstrap("boot-one", "0" * 64))

    def test_payload_is_frozen_when_submitted(self) -> None:
        ledger = self.open()
        entered, release = threading.Event(), threading.Event()
        first = ledger.submit("TX_OBSERVATION", "WAIT", {}, lambda conn, seq: (entered.set(), release.wait(5)))
        self.assertTrue(entered.wait(5))
        payload = {"nested": {"value": "1"}}
        queued = ledger.submit("TX_OBSERVATION", "FROZEN", payload)
        payload["nested"]["value"] = "999"
        release.set()
        first.result(5)
        ack = queued.result(5)
        saved = ledger.query("SELECT payload_json FROM event_journal WHERE event_seq=?", (ack.event_seq,))[0]
        self.assertEqual("1", json.loads(saved["payload_json"])["data"]["nested"]["value"])

    def test_backup_includes_current_committed_wal_facts(self) -> None:
        ledger = self.open()
        latest = ledger.submit("TX_OBSERVATION", "BEFORE_BACKUP", {"key": "committed"}).result(5)
        destination = Path(self.temporary.name) / "snapshot.db"
        ledger.backup(destination)
        with closing(sqlite3.connect(destination)) as backup:
            self.assertEqual(latest.event_seq, backup.execute("SELECT MAX(event_seq) FROM event_journal").fetchone()[0])
            self.assertEqual("ok", backup.execute("PRAGMA integrity_check").fetchone()[0])
        self.assert_code("E_STORAGE", lambda: ledger.backup(destination))

    def test_partial_schema_is_not_silently_replaced(self) -> None:
        with closing(sqlite3.connect(self.path)) as database:
            database.execute("CREATE TABLE foreign_database(value TEXT)")
            database.commit()
        self.assert_code("E_SCHEMA_MISMATCH", lambda: Ledger(self.path, "DEV:test:account-1"))
        with closing(sqlite3.connect(self.path)) as database:
            self.assertEqual([("foreign_database",)], database.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())

    def test_composite_fk_rejects_mismatched_boot_epoch_atomically(self) -> None:
        ledger = self.open()
        def mismatch(conn, seq):
            conn.execute("UPDATE authority SET kernel_epoch=99 WHERE singleton=1")
        with self.assertRaises(sqlite3.IntegrityError):
            ledger.submit("TX_AUTH", "BAD_BINDING", {}, mismatch).result(5)
        self.assertEqual(1, ledger.query("SELECT kernel_epoch FROM authority")[0]["kernel_epoch"])
        self.assertEqual(1, ledger.query("SELECT MAX(event_seq) AS n FROM event_journal")[0]["n"])

    def test_mutation_cannot_commit_before_writer_ack(self) -> None:
        ledger = self.open()
        def premature_commit(conn, seq):
            conn.execute("INSERT INTO day_blocks VALUES ('block','ACCOUNT','2026-11-09','TEST','{}','ACTIVE',?,NULL)", (seq,))
            conn.commit()
        with self.assertRaises(sqlite3.DatabaseError):
            ledger.submit("TX_AUTH", "EARLY_COMMIT", {}, premature_commit).result(5)
        self.assertEqual([], ledger.query("SELECT * FROM day_blocks"))
        self.assertEqual(1, ledger.query("SELECT MAX(event_seq) AS n FROM event_journal")[0]["n"])

    def test_changed_schema_with_same_table_names_is_refused(self) -> None:
        self.open().close()
        with closing(sqlite3.connect(self.path)) as database:
            database.execute("DROP TRIGGER protected_cannot_clear")
            database.commit()
        self.assert_code("E_SCHEMA_MISMATCH", lambda: Ledger(self.path, "DEV:test:account-1"))

    def test_close_is_idempotent_and_refuses_new_work(self) -> None:
        ledger = self.open()
        ledger.close()
        ledger.close()
        self.assert_code("E_STORAGE_CLOSED", lambda: ledger.submit("TX_OBSERVATION", "CLOSED", {}).result(5))
        self.assert_code("E_STORAGE_CLOSED", lambda: ledger.query("SELECT 1"))


if __name__ == "__main__":
    unittest.main()
