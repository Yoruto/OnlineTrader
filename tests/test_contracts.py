from __future__ import annotations

import copy
import hashlib
import struct
import unittest

from online_trader.contracts.codec import (
    FrameDecoder, MAX_FRAME_BYTES, canonical_bytes, content_hash, encode_frame,
    payload_hash, strict_loads,
)
from online_trader.contracts.context import ContextRegistry
from online_trader.contracts.validation import validate_message, validate_plan
from online_trader.errors import TraderError


def command() -> dict:
    return {
        "protocol_version": "1.0", "type": "OperatorCommand", "message_id": "msg-fixture-001",
        "connection_id": "conn-fixture-001", "correlation_id": None,
        "sent_at_utc": "2026-11-09T05:00:00Z", "kernel_boot_id": "boot-fixture-001",
        "account_key": "acct-fixture-001", "body": {
            "command_id": "cmd-fixture-pause-001", "context_id": "ctx-fixture-001", "kernel_epoch": "1",
            "expected_revisions": {"state_revision": "1", "risk_revision": "1", "config_revision": "1", "authority_revision": "1"},
            "operation": "PAUSE_NEW_ENTRIES", "scope": {"scope_type": "ACCOUNT", "scope_ids": []},
            "parameters": {"cancel_owned_entry_remainders": True},
            "payload_hash": "410e714c98c53559b78467065e6e8c6a2b72370adeae49f3d2e8df7a3e1b0a74",
        },
    }


class CodecTests(unittest.TestCase):
    def assert_refused(self, fn, *args, code=None):
        with self.assertRaises(TraderError) as caught:
            fn(*args)
        if code:
            self.assertEqual(caught.exception.code, code)

    def test_canonical_golden_vectors(self):
        vectors = [
            (None, b"null", "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"),
            ({"b": "中", "a": 1}, '{"a":1,"b":"中"}'.encode(), "2831299868169bc527f55f88ebbdcd8b785d78d9e7dc64e6887dfbd2825dd247"),
            ("a\nb", b'"a\\u000ab"', "b1a5ad2996a5f67a85d8398185b98b8819c580bfb3ce2e2f2c887cd3ccd6d93e"),
        ]
        for value, raw, digest in vectors:
            with self.subTest(value=value):
                self.assertEqual(canonical_bytes(value), raw)
                self.assertEqual(content_hash(value), digest)

    def test_scalars_keep_unicode_and_escape_all_c0(self):
        self.assertEqual(canonical_bytes('"\\\t/\u2028'), b'"\\"\\\\\\u0009/' + '\u2028'.encode() + b'"')
        self.assertNotEqual(content_hash("é"), content_hash("e\u0301"))

    def test_payload_hash_preserves_business_and_ignores_transport(self):
        original = command()
        self.assertEqual(payload_hash(original), original["body"]["payload_hash"])
        retry = copy.deepcopy(original)
        retry.update(message_id="retry", connection_id="another", sent_at_utc="2026-11-10T00:00:00Z")
        retry["body"]["context_id"] = "renewed-context"
        self.assertEqual(payload_hash(original), payload_hash(retry))
        retry["body"]["parameters"]["cancel_owned_entry_remainders"] = False
        self.assertNotEqual(payload_hash(original), payload_hash(retry))

    def test_invalid_json_is_rejected_without_numeric_coercion(self):
        cases = [b'{"a":1,"a":2}', b'{"n":1.0}', b'{"n":1e2}', b'{"n":-0}', b'{"n":-1}',
                 b'{"n":2147483648}', b'{"n":NaN}', b'{"n":Infinity}', b'"\\ud800"', b'"\xff"',
                 b'\xef\xbb\xbf{}', b'{} garbage', b'{"n":01}']
        for raw in cases:
            with self.subTest(raw=raw):
                self.assert_refused(strict_loads, raw)

    def test_python_values_cannot_bypass_json_rules(self):
        for value in [1.0, float("nan"), -1, {1: "bad"}, {"x": "\udfff"}, (1, 2)]:
            with self.subTest(kind=type(value).__name__):
                self.assert_refused(canonical_bytes, value)

    def test_depth_limit(self):
        value = 0
        for _ in range(34):
            value = [value]
        self.assert_refused(canonical_bytes, value)
        self.assert_refused(strict_loads, "[" * 34 + "0" + "]" * 34)

    def test_split_and_coalesced_frames_preserve_utf8(self):
        first, second = {"value": "中"}, {"value": 2}
        frame = encode_frame(first) + encode_frame(second)
        decoder = FrameDecoder()
        messages = []
        for offset in range(0, len(frame), 3):
            messages.extend(decoder.feed(frame[offset:offset + 3]))
        self.assertEqual(messages, [first, second])
        self.assertEqual(decoder.pending_bytes, 0)

    def test_header_rejected_before_large_body_and_decoder_stays_invalid(self):
        for header in [struct.pack("<4sHHI", b"OTIP", 1, 0, 0),
                       struct.pack("<4sHHI", b"OTIP", 1, 0, MAX_FRAME_BYTES + 1),
                       struct.pack("<4sHHI", b"OTIP", 2, 0, 2),
                       struct.pack("<4sHHI", b"BAD!", 1, 0, 2)]:
            with self.subTest(header=header):
                decoder = FrameDecoder()
                self.assert_refused(decoder.feed, header)
                self.assert_refused(decoder.feed, encode_frame({}), code="E_FRAME")

    def test_non_object_frame_rejected(self):
        raw = b"[]"
        self.assert_refused(FrameDecoder().feed, struct.pack("<4sHHI", b"OTIP", 1, 0, len(raw)) + raw)


class SchemaTests(unittest.TestCase):
    def test_command_and_internal_ack_boundary(self):
        message = command()
        validate_message(message)
        message["type"] = "CommitAck"
        with self.assertRaises(TraderError):
            validate_message(message)

    def test_unknown_fields_revision_zero_and_overflow(self):
        for change in [lambda m: m["body"].update(extra=1),
                       lambda m: m["body"]["expected_revisions"].update(config_revision="0"),
                       lambda m: m["body"].update(kernel_epoch="9223372036854775808"),
                       lambda m: m.update(sent_at_utc="2026-99-99T00:00:00Z")]:
            message = command(); change(message)
            with self.assertRaises(TraderError):
                validate_message(message)

    def test_plan_does_not_accept_unknown_or_missing_contract(self):
        with self.assertRaises(TraderError):
            validate_plan({"schema_version": "1.0", "callback": "arbitrary"})


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.registry = ContextRegistry("boot-1", "account-1", clock=lambda: self.now)

    def issue(self, **kwargs):
        return self.registry.issue("peer-1", "OPERATOR_COMMAND", "PAUSE_NEW_ENTRIES", "account-1", **kwargs)

    def test_deadline_is_server_monotonic_and_not_transferred(self):
        context = self.issue(ttl_ms=1000)
        context["valid_for_ms"] = 30000
        self.now = 100.999
        self.registry.validate(context["context_id"], "peer-1", "OPERATOR_COMMAND", "PAUSE_NEW_ENTRIES", "account-1")
        self.now = 101.0
        with self.assertRaises(TraderError) as caught:
            self.registry.validate(context["context_id"], "peer-1", "OPERATOR_COMMAND", "PAUSE_NEW_ENTRIES", "account-1")
        self.assertEqual(caught.exception.code, "E_CONTEXT_EXPIRED")

    def test_peer_scope_boot_epoch_and_revisions_bound(self):
        context = self.issue()
        original = [context["context_id"], "peer-1", "OPERATOR_COMMAND", "PAUSE_NEW_ENTRIES", "account-1"]
        for index, replacement in [(1, "peer-2"), (2, "ENTRY_PROPOSAL"), (3, "ARM_ENTRIES"), (4, "account-2")]:
            args = original.copy(); args[index] = replacement
            with self.assertRaises(TraderError):
                self.registry.validate(*args)
        for kwargs in [{"kernel_boot_id": "boot-2"}, {"account_key": "account-2"}, {"kernel_epoch": "2"},
                       {"revisions": {"state_revision": "2"}}]:
            with self.assertRaises(TraderError):
                self.registry.validate(*original, **kwargs)

    def test_revoke_peer_and_restart_invalidate_contexts(self):
        context = self.issue()
        self.registry.revoke_peer("peer-1")
        with self.assertRaises(TraderError):
            self.registry.validate(context["context_id"], "peer-1", "OPERATOR_COMMAND", "PAUSE_NEW_ENTRIES", "account-1")
        context = self.issue()
        restarted = ContextRegistry("boot-2", "account-1")
        with self.assertRaises(TraderError):
            restarted.validate(context["context_id"], "peer-1", "OPERATOR_COMMAND", "PAUSE_NEW_ENTRIES", "account-1")

    def test_returned_revisions_cannot_mutate_internal_context(self):
        context = self.issue()
        context["related_revisions"]["state_revision"] = "999"
        result = self.registry.validate(context["context_id"], "peer-1", "OPERATOR_COMMAND", "PAUSE_NEW_ENTRIES", "account-1")
        self.assertEqual(result["related_revisions"]["state_revision"], "1")

    def test_capacity_is_bounded_and_expired_entries_reclaimed(self):
        registry = ContextRegistry("boot", "account", clock=lambda: self.now, capacity=1)
        registry.issue("peer", "OPERATOR_COMMAND", "PAUSE_NEW_ENTRIES", "account", ttl_ms=1)
        with self.assertRaises(TraderError) as caught:
            registry.issue("peer", "OPERATOR_COMMAND", "PAUSE_NEW_ENTRIES", "account")
        self.assertEqual(caught.exception.code, "E_QUEUE_OVERFLOW")
        self.now += 0.002
        registry.issue("peer", "OPERATOR_COMMAND", "PAUSE_NEW_ENTRIES", "account")


if __name__ == "__main__":
    unittest.main()
