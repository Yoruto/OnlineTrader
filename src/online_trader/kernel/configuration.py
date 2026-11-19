"""Owner-side, append-only non-trading DSV reference/configuration catalog."""

from concurrent.futures import Future
import copy
from pathlib import Path
import sqlite3
import uuid

from online_trader.configuration.baseline import (
    check_reference_profile, load_confirmed_baseline, load_reference_approval,
)
from online_trader.contracts.codec import canonical_bytes, content_hash, strict_loads
from online_trader.errors import TraderError


KIND = "DSV_REFERENCE_PROFILE"
DECISION_REVISION = 2


def _ready(value):
    result = Future(); result.set_result(copy.deepcopy(value)); return result


class ConfigurationCatalog:
    """A saved reference is not the authority's active execution configuration."""

    def __init__(self, kernel):
        kernel._check_owner()
        self.kernel = kernel
        self._entries = {}
        self._baseline_entry = None
        self._reference_manifest = None
        self._pending = None
        rows = kernel.ledger.query("SELECT * FROM config_revisions WHERE json_extract(payload_json,'$.kind')=? ORDER BY config_revision", (KIND,))
        for row in rows:
            body = strict_loads(row["payload_json"])
            if content_hash(body) != row["payload_hash"] or row["effective_boundary"] != "NON_TRADING":
                raise TraderError("E_CONFIG", "Saved configuration integrity differs")
            manifest = self._manifest()
            saved = self._decode_profile(body["settings_record"])
            confirmed = saved.content_hash == manifest["profile_hash"]
            if (body.get("profile_hash") != saved.content_hash or body.get("runtime_activation") is not False
                    or body.get("reference_approval_id") != manifest["record_id"]
                    or body.get("matches_confirmed_baseline") is not confirmed
                    or row["rule_set_id"] != manifest["rule_set_id"]):
                raise TraderError("E_CONFIG", "Saved configuration scope differs")
            entry = self._entry(row["config_revision"], saved, manifest, row["created_event_seq"])
            self._entries[row["config_revision"]] = entry
            if body.get("source") == "CONFIRMED_DSV_BASELINE":
                if not confirmed or self._baseline_entry is not None:
                    raise TraderError("E_CONFIG", "Confirmed baseline identity differs")
                self._baseline_entry = entry
        if rows:
            if self._baseline_entry is None:
                raise TraderError("E_CONFIG", "Saved drafts have no confirmed baseline")
            self._verify_reference()

    @staticmethod
    def _decode_profile(record):
        from online_trader.configuration.dsv import DsvProfile
        return DsvProfile.from_record(record)

    def _manifest(self):
        if self._reference_manifest is None:
            self._reference_manifest = load_reference_approval()
        return self._reference_manifest

    def _verify_reference(self):
        self._verify_reference_query(self.kernel.ledger.query, self._manifest())

    @staticmethod
    def _verify_reference_query(query, manifest):
        rules = {"kind": "DSV_REFERENCE_RULESET", "rule_set_id": manifest["rule_set_id"], "reference_approval_id": manifest["record_id"],
                 "profile_hash": manifest["profile_hash"], "runtime_activation": False}
        sets = query("SELECT * FROM rule_sets WHERE rule_set_id=?", (manifest["rule_set_id"],))
        if len(sets) != 1 or strict_loads(sets[0]["payload_json"]) != rules or sets[0]["payload_hash"] != content_hash(rules):
            raise TraderError("E_CONFIG", "Stored reference ruleset differs")
        rows = query("SELECT * FROM approval_manifests WHERE approval_record_id=?", (manifest["record_id"],))
        if (len(rows) != 1 or strict_loads(rows[0]["manifest_json"]) != manifest
                or rows[0]["manifest_hash"] != content_hash(manifest) or rows[0]["rule_set_id"] != manifest["rule_set_id"]):
            raise TraderError("E_CONFIG", "Stored reference approval differs")
        decisions = query("SELECT * FROM rule_decisions WHERE rule_set_id=? ORDER BY b_code", (manifest["rule_set_id"],))
        items = query("SELECT * FROM approval_manifest_items WHERE approval_record_id=? ORDER BY b_code", (manifest["record_id"],))
        if len(decisions) != 20 or len(items) != 20:
            raise TraderError("E_CONFIG", "Stored reference does not cover twenty decisions")
        for decision, approved, item in zip(decisions, manifest["decisions"], items):
            if (decision["b_code"] != approved["b_code"] or decision["status"] != "APPROVED"
                    or decision["decision_revision"] != DECISION_REVISION
                    or strict_loads(decision["final_rule_json"]) != approved
                    or strict_loads(decision["effective_scope_json"]) != manifest["effective_scope"]
                    or decision["approver_reference"] != manifest["approver_reference"]
                    or decision["evidence_hash"] != manifest["source_evidence_hash"]
                    or decision["recorded_event_seq"] != rows[0]["recorded_event_seq"]
                    or item["b_code"] != approved["b_code"] or item["decision_revision"] != DECISION_REVISION
                    or item["rule_set_id"] != manifest["rule_set_id"]):
                raise TraderError("E_CONFIG", "Stored reference decision differs")

    @staticmethod
    def _entry(revision, profile, manifest, event_seq):
        confirmed = profile.content_hash == manifest["profile_hash"]
        return {"config_revision": revision, "rule_set_id": manifest["rule_set_id"],
            "profile_hash": profile.content_hash, "confirmation_status": "CONFIRMED_DSV_REFERENCE" if confirmed else "UNCONFIRMED_DRAFT",
            "matches_confirmed_baseline": confirmed, "effective_boundary": "NON_TRADING",
            "reference_approval_id": manifest["record_id"], "saved_event_seq": str(event_seq),
            "settings_record": profile.to_record(), "summary": profile.summary(), "diagnostics": profile.diagnostics(),
            "runtime_activation": False}

    @property
    def current_revision(self):
        self.kernel._check_owner()
        return max(self._entries, default=0)

    def get(self, revision=None):
        self.kernel._check_owner()
        if revision is None:
            revision = self.current_revision
            if not revision: return None
        if type(revision) is not int or revision <= 0 or revision not in self._entries:
            raise TraderError("E_CONFIG", "Unknown configuration catalog revision")
        return copy.deepcopy(self._entries[revision])

    def list_revisions(self):
        self.kernel._check_owner()
        return [copy.deepcopy({k: v for k, v in entry.items() if k not in {"settings_record", "summary", "diagnostics"}})
                for _, entry in sorted(self._entries.items())]

    def poll(self):
        self.kernel._check_owner()
        if self._pending is not None and self.kernel._failed:
            result = self._pending["result"]
            if not result.done(): result.set_exception(TraderError("E_STORAGE", "Required configuration transaction failed"))
            self._pending = None

    def _boundary(self):
        self.kernel._writable(); self.poll()
        if self._pending is not None or self.kernel._pending:
            raise TraderError("E_FACT_BARRIER", "Configuration writes require an accepted owner boundary")
        if self.kernel._command_binding is not None:
            raise TraderError("E_SCOPE", "Configuration catalog is not an operator command action")

    def register_confirmed_baseline(self, profile=None):
        self.kernel._writable(); self.poll()
        manifest = self._manifest()
        if profile is not None:
            check_reference_profile(profile, manifest)
        if self._baseline_entry is not None:
            return _ready(self._receipt(self._baseline_entry, replayed=True))
        if self._pending is not None and self._pending["source"] == "CONFIRMED_DSV_BASELINE":
            return self._pending["result"]
        self._boundary()
        if profile is None:
            profile, _ = load_confirmed_baseline()
        if self.kernel.ledger.query("SELECT 1 FROM rule_sets WHERE rule_set_id=?", (manifest["rule_set_id"],)):
            raise TraderError("E_CONFIG", "Reference identity exists without a complete catalog baseline")
        return self._save(profile, manifest, "CONFIRMED_DSV_BASELINE", register=True)

    def save(self, profile, expected_revision):
        self._boundary()
        if type(expected_revision) is not int or expected_revision < 0:
            raise TraderError("E_REVISION_CONFLICT", "Expected catalog revision must be a nonnegative integer")
        if expected_revision != self.current_revision:
            raise TraderError("E_REVISION_CONFLICT", "Configuration catalog revision changed")
        if self._baseline_entry is None:
            raise TraderError("E_NOT_READY", "Register the confirmed DSV baseline before saving a draft")
        # Round-trip through the strict domain so callers cannot supply an arbitrary duck type.
        from online_trader.configuration.dsv import DsvProfile
        if not isinstance(profile, DsvProfile): raise TraderError("E_CONFIG", "Expected a validated DSV profile")
        profile = self._decode_profile(profile.to_record())
        current = self._entries[self.current_revision]
        if profile.content_hash == current["profile_hash"]:
            return _ready(self._receipt(current, replayed=True))
        manifest = self._manifest()
        return self._save(profile, manifest, "LOCAL_DRAFT", register=False)

    def _receipt(self, entry, *, tx_id=None, replayed=False):
        return {k: copy.deepcopy(v) for k, v in entry.items() if k not in {"settings_record", "summary", "diagnostics"}} | {
            "status": "COMMITTED", "tx_id": tx_id or self.kernel.ledger.query("SELECT tx_id FROM event_journal WHERE event_seq=?", (int(entry["saved_event_seq"]),))[0]["tx_id"],
            "event_seq": entry["saved_event_seq"], "durable_seq": entry["saved_event_seq"], "replayed": replayed}

    def _save(self, profile, manifest, source, *, register):
        revision = self.kernel.ledger.query("SELECT COALESCE(MAX(config_revision),0)+1 AS n FROM config_revisions")[0]["n"]
        seq = self.kernel._sequence + 1; tx_id = "tx-config-" + uuid.uuid4().hex
        body = {"kind": KIND, "source": source, "settings_record": profile.to_record(), "profile_hash": profile.content_hash,
            "reference_approval_id": manifest["record_id"], "matches_confirmed_baseline": profile.content_hash == manifest["profile_hash"],
            "runtime_activation": False}
        entry = self._entry(revision, profile, manifest, seq)
        result = Future(); self._pending = {"result": result, "source": source}
        def mutate(conn, actual_seq):
            next_revision = conn.execute("SELECT COALESCE(MAX(config_revision),0)+1 FROM config_revisions").fetchone()[0]
            if next_revision != revision: raise TraderError("E_REVISION_CONFLICT", "Configuration revision allocation changed")
            if register:
                rules = {"kind": "DSV_REFERENCE_RULESET", "rule_set_id": manifest["rule_set_id"], "reference_approval_id": manifest["record_id"],
                         "profile_hash": manifest["profile_hash"], "runtime_activation": False}
                conn.execute("INSERT INTO rule_sets VALUES(?,?,?,?)", (manifest["rule_set_id"], canonical_bytes(rules).decode(), content_hash(rules), actual_seq))
                for decision in manifest["decisions"]:
                    conn.execute("INSERT INTO rule_decisions VALUES(?,?,?,'APPROVED',?,?,?,?,?)", (manifest["rule_set_id"], decision["b_code"], DECISION_REVISION,
                        canonical_bytes(decision).decode(), manifest["approver_reference"], manifest["source_evidence_hash"], canonical_bytes(manifest["effective_scope"]).decode(), actual_seq))
                conn.execute("INSERT INTO approval_manifests VALUES(?,?,?,?,?)", (manifest["record_id"], manifest["rule_set_id"], canonical_bytes(manifest).decode(), content_hash(manifest), actual_seq))
                for decision in manifest["decisions"]:
                    conn.execute("INSERT INTO approval_manifest_items VALUES(?,?,?,?)", (manifest["record_id"], manifest["rule_set_id"], decision["b_code"], DECISION_REVISION))
            conn.execute("INSERT INTO config_revisions VALUES(?,?,?,?,'NON_TRADING',?)", (revision, manifest["rule_set_id"], canonical_bytes(body).decode(), content_hash(body), actual_seq))
        def accepted():
            self._entries[revision] = entry
            if register: self._baseline_entry = entry
            self._pending = None
            result.set_result(self._receipt(entry, tx_id=tx_id))
        try:
            write = self.kernel._submit("TX_CONFIG", "DSV_REFERENCE_REGISTERED" if register else "DSV_CONFIGURATION_SAVED",
                {"configuration_catalog": body, "config_revision": str(revision)}, mutate, after=accepted, tx_id=tx_id)
        except BaseException:
            self._pending = None
            raise
        def failed(future):
            try: error = future.exception()
            except BaseException: error = True
            if error is not None and not result.done():
                result.set_exception(TraderError("E_STORAGE", "Configuration could not be committed"))
        write.add_done_callback(failed)
        return result


def read_catalog_file(path, account_key):
    """Read committed catalog facts without acquiring execution ownership or a new boot."""
    from online_trader.domain.values import identifier
    account_key = identifier(account_key)
    path = Path(path).resolve()
    if not path.is_file(): raise TraderError("E_CONFIG", "Configuration ledger does not exist")
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            def query(sql, params=()): return [dict(row) for row in connection.execute(sql, params)]
            scope = query("SELECT account_key FROM ledger_scope WHERE singleton=1")
            if len(scope) != 1 or scope[0]["account_key"] != account_key:
                raise TraderError("E_SCOPE", "Configuration ledger belongs to another account")
            rows = query("SELECT * FROM config_revisions WHERE json_extract(payload_json,'$.kind')=? ORDER BY config_revision", (KIND,))
            if not rows: return []
            manifest = load_reference_approval()
            ConfigurationCatalog._verify_reference_query(query, manifest)
            result = []; baseline_count = 0
            for row in rows:
                body = strict_loads(row["payload_json"])
                profile = ConfigurationCatalog._decode_profile(body["settings_record"])
                confirmed = profile.content_hash == manifest["profile_hash"]
                if (content_hash(body) != row["payload_hash"] or row["effective_boundary"] != "NON_TRADING"
                        or row["rule_set_id"] != manifest["rule_set_id"]
                        or body.get("reference_approval_id") != manifest["record_id"]
                        or body.get("runtime_activation") is not False
                        or body.get("profile_hash") != profile.content_hash
                        or body.get("matches_confirmed_baseline") is not confirmed):
                    raise TraderError("E_CONFIG", "Saved configuration scope or integrity differs")
                if body.get("source") == "CONFIRMED_DSV_BASELINE":
                    if not confirmed: raise TraderError("E_CONFIG", "Baseline content changed")
                    baseline_count += 1
                result.append(ConfigurationCatalog._entry(row["config_revision"], profile, manifest, row["created_event_seq"]))
            if baseline_count != 1: raise TraderError("E_CONFIG", "Catalog has no unique confirmed baseline")
            return result
        finally: connection.close()
    except TraderError: raise
    except (sqlite3.Error, KeyError, TypeError, ValueError) as error:
        raise TraderError("E_CONFIG", "Configuration ledger cannot be read safely") from error
