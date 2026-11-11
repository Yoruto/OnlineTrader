from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import threading
import unittest
import uuid

from online_trader.errors import TraderError
from online_trader.transport.named_pipe import PipeListener, connect, current_sid


def hello() -> dict:
    return {"protocol_version": "1.0", "type": "Hello", "message_id": "hello-1", "connection_id": None,
            "correlation_id": None, "sent_at_utc": "2026-11-09T00:00:00Z", "kernel_boot_id": None,
            "account_key": None, "body": {"supported_versions": ["1.0"], "requested_role": "DESKTOP",
                                          "client_instance_id": "desktop-test", "requested_account_key": None,
                                          "client_build_id": "build-test"}}


@unittest.skipUnless(os.name == "nt", "Windows named-pipe integration")
class NativePipeTests(unittest.TestCase):
    def setUp(self):
        self.name = "online-trader-test-" + uuid.uuid4().hex

    def test_real_pipe_identity_round_trip_and_listener_reconnect(self):
        with PipeListener(self.name) as listener, ThreadPoolExecutor(max_workers=1) as pool:
            self.assertNotIn(";;;WD", listener.security_sddl)
            self.assertIn(current_sid(), listener.security_sddl)
            for _ in range(2):
                accepted = pool.submit(listener.accept, timeout=3)
                with connect(self.name, timeout=3, expected_server_pid=os.getpid()) as client:
                    with accepted.result(timeout=4) as server:
                        self.assertEqual(server.peer_pid, os.getpid())
                        self.assertEqual(server.peer_sid, current_sid())
                        client.send(hello())
                        received = server.recv(timeout=2)
                        self.assertEqual(received, hello())
                        server.send(received)
                        self.assertEqual(client.recv(timeout=2), hello())

    def test_duplicate_listener_and_wrong_registered_server_rejected(self):
        with PipeListener(self.name) as listener, ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(TraderError) as caught:
                PipeListener(self.name)
            self.assertEqual(caught.exception.code, "E_PIPE_IN_USE")
            accepted = pool.submit(listener.accept, timeout=3)
            with self.assertRaises(TraderError) as caught:
                connect(self.name, timeout=3, expected_server_pid=os.getpid() + 1)
            self.assertEqual(caught.exception.code, "E_AUTH")
            accepted.result(timeout=4).close()

    def test_accept_and_idle_read_deadlines_do_not_destroy_listener(self):
        with PipeListener(self.name) as listener, ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(TraderError) as caught:
                listener.accept(timeout=0.03)
            self.assertEqual(caught.exception.code, "E_TIMEOUT")
            accepted = pool.submit(listener.accept, timeout=3)
            with connect(self.name, timeout=3) as client, accepted.result(timeout=4) as server:
                with self.assertRaises(TraderError) as caught:
                    server.recv(timeout=0.03)
                self.assertEqual(caught.exception.code, "E_TIMEOUT")
                self.assertFalse(server.closed)
                client.send(hello())
                self.assertEqual(server.recv(timeout=2), hello())

    def test_listener_close_interrupts_pending_accept(self):
        listener = PipeListener(self.name)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                started = threading.Event()
                def wait():
                    started.set()
                    return listener.accept(timeout=3)
                pending = pool.submit(wait)
                self.assertTrue(started.wait(1))
                listener.close()
                with self.assertRaises(TraderError):
                    pending.result(timeout=4)
        finally:
            listener.close()


if __name__ == "__main__":
    unittest.main()
