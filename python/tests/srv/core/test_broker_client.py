# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path

from python.srv.core import broker_client


class _Server:
    def __init__(self, path: Path, answer: Callable[[socket.socket, bytes], None]) -> None:
        self.path = path
        self.answer = answer
        self.requests: list[dict] = []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        connection, _address = self.listener.accept()
        with connection:
            line = connection.makefile("rb").readline()
            self.requests.append(json.loads(line))
            self.answer(connection, line)

    def close(self) -> None:
        self.listener.close()
        self.thread.join(timeout=5)


class BrokerClientTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix="abr-broker-client-")
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "broker.sock"

    def serve(self, answer: Callable[[socket.socket, bytes], None]) -> _Server:
        server = _Server(self.path, answer)
        self.addCleanup(server.close)
        return server

    def reply(self, body) -> Callable[[socket.socket, bytes], None]:
        return lambda connection, _line: connection.sendall(json.dumps(body).encode() + b"\n")

    def call(self, timeout: float = 5) -> dict:
        return broker_client.call(str(self.path), "add-share", {"share": "abr-k3j9x2qa$"}, timeout=timeout)

    def test_the_request_is_one_json_line_and_the_result_comes_back(self) -> None:
        server = self.serve(self.reply({"ok": True, "result": {"share": "abr-k3j9x2qa$"}}))

        self.assertEqual(self.call(), {"share": "abr-k3j9x2qa$"})
        self.assertEqual(server.requests, [{"operation": "add-share", "arguments": {"share": "abr-k3j9x2qa$"}}])

    def test_a_refusal_carries_its_code_and_message(self) -> None:
        self.serve(self.reply({"ok": False, "code": "refused", "message": "smbd is not running"}))

        with self.assertRaises(broker_client.BrokerRefused) as caught:
            self.call()

        self.assertEqual((caught.exception.code, caught.exception.message), ("refused", "smbd is not running"))

    def test_no_broker_on_the_socket_path_is_unavailable(self) -> None:
        with self.assertRaises(broker_client.BrokerUnavailable) as caught:
            self.call()

        self.assertIn(f"nothing listens on {self.path}", str(caught.exception))

    def test_a_socket_this_server_may_not_open_is_unavailable(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root may open any socket")
        self.serve(self.reply({"ok": True, "result": {}}))
        self.path.chmod(0)

        with self.assertRaises(broker_client.BrokerUnavailable) as caught:
            self.call()

        self.assertIn("may not connect", str(caught.exception))
        self.path.chmod(0o600)
        self.assertEqual(self.call(), {})

    def test_a_broker_that_does_not_answer_in_time_is_unavailable(self) -> None:
        release = threading.Event()
        self.serve(lambda _connection, _line: release.wait(5))
        self.addCleanup(release.set)

        with self.assertRaises(broker_client.BrokerUnavailable) as caught:
            self.call(timeout=0.3)

        self.assertIn("did not answer within 0.3 s", str(caught.exception))

    def test_answers_that_are_cut_or_not_json_or_of_an_unknown_form_are_unavailable(self) -> None:
        cases = {
            "without a complete answer": lambda connection, _line: connection.sendall(b'{"ok": tr'),
            "other than JSON": lambda connection, _line: connection.sendall(b"Traceback (most recent call last)\n"),
            "a form this server does not know": self.reply({"ok": True, "result": "done"}),
        }
        for expected, answer in cases.items():
            with self.subTest(expected=expected):
                self.path.unlink(missing_ok=True)
                server = _Server(self.path, answer)
                try:
                    with self.assertRaises(broker_client.BrokerUnavailable) as caught:
                        self.call()
                finally:
                    server.close()
                self.assertIn(expected, str(caught.exception))

    def test_a_refusal_without_a_code_or_message_still_says_so(self) -> None:
        self.serve(self.reply({"ok": False}))

        with self.assertRaises(broker_client.BrokerRefused) as caught:
            self.call()

        self.assertEqual((caught.exception.code, caught.exception.message), ("failed", "no reason given"))


if __name__ == "__main__":
    unittest.main()
