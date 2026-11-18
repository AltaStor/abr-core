# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import json
import os
import socket
import threading
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from python.srv.brokers import broker_core
from python.srv.brokers.broker_core import BrokerError
from python.tests.srv.brokers.fake_samba import current_user

SECRET = "Pa55wordThatMustNeverBeLogged0123456789"


def _echo(arguments: dict) -> dict:
    return {"echo": arguments}


def _refuse(_arguments: dict) -> dict:
    raise BrokerError(broker_core.REFUSED, "smbd is not running")


def _crash(_arguments: dict) -> dict:
    raise OSError(5, "Input/output error")


OPERATIONS = {"echo": _echo, "refuse": _refuse, "crash": _crash}


class _ServeCase(unittest.TestCase):
    def exchange(self, payload: bytes, *, service_user: str | None = None, close_after_send: bool = False) -> tuple[int, dict, str]:
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        client.sendall(payload)
        if close_after_send:
            client.shutdown(socket.SHUT_WR)
        log = io.StringIO()
        with redirect_stderr(log):
            code = broker_core.serve(OPERATIONS, service_user=service_user or current_user(), connection=server)
        server.shutdown(socket.SHUT_WR)
        reply = json.loads(client.makefile("rb").readline())
        return code, reply, log.getvalue()

    def request(self, operation: str, arguments: dict | None = None) -> bytes:
        return json.dumps({"operation": operation, "arguments": arguments or {}}).encode() + b"\n"


class ServeTests(_ServeCase):
    def test_an_allowed_client_gets_the_operation_result(self) -> None:
        code, reply, log = self.exchange(self.request("echo", {"share": "abr-k3j9x2qa$"}))

        self.assertEqual((code, reply), (0, {"ok": True, "result": {"echo": {"share": "abr-k3j9x2qa$"}}}))
        self.assertIn(f"echo for uid {os.getuid()} (pid {os.getpid()}): ok", log)

    def test_a_client_that_is_neither_root_nor_the_service_user_is_refused_before_reading(self) -> None:
        code, reply, log = self.exchange(self.request("echo"), service_user="abr-nobody-here")

        self.assertEqual((code, reply["ok"], reply["code"]), (1, False, broker_core.NOT_PERMITTED))
        self.assertIn(f"uid {os.getuid()} may not use this broker; only root and abr-nobody-here may", reply["message"])
        self.assertIn("- for uid", log)

    def test_an_operation_refusal_is_answered_with_its_code(self) -> None:
        code, reply, _log = self.exchange(self.request("refuse"))

        self.assertEqual((code, reply), (1, {"ok": False, "code": "refused", "message": "smbd is not running"}))

    def test_an_unexpected_failure_is_answered_not_dropped(self) -> None:
        _code, reply, _log = self.exchange(self.request("crash"))

        self.assertEqual((reply["code"], reply["message"]), ("failed", "OSError: [Errno 5] Input/output error"))

    def test_an_operation_outside_the_list_is_refused_and_names_the_list(self) -> None:
        _code, reply, _log = self.exchange(self.request("mount", {"path": "/"}))

        self.assertEqual(reply["code"], broker_core.INVALID_REQUEST)
        self.assertIn("unknown operation 'mount'; this broker runs crash, echo, refuse", reply["message"])

    def test_requests_that_are_not_one_json_object_on_one_line_are_refused(self) -> None:
        cases = {
            b"not json\n": "not JSON",
            b"[1, 2]\n": "exactly an operation",
            b'{"operation": "echo"}\n': "exactly an operation",
            b'{"operation": "echo", "arguments": {}, "extra": 1}\n': "exactly an operation",
            b'{"operation": 7, "arguments": {}}\n': "must be a name",
            b'{"operation": "echo", "arguments": []}\n': "must be a name",
        }
        for payload, expected in cases.items():
            with self.subTest(payload=payload):
                _code, reply, _log = self.exchange(payload)
                self.assertEqual(reply["code"], broker_core.INVALID_REQUEST)
                self.assertIn(expected, reply["message"])

    def test_a_request_without_its_line_end_is_refused(self) -> None:
        _code, reply, _log = self.exchange(self.request("echo").rstrip(b"\n"), close_after_send=True)

        self.assertIn("one JSON object on one line", reply["message"])

    def test_a_request_longer_than_the_limit_is_refused(self) -> None:
        payload = self.request("echo", {"path": "x" * broker_core.MAX_REQUEST_BYTES})
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        sender = threading.Thread(target=lambda: client.sendall(payload))
        sender.start()
        with redirect_stderr(io.StringIO()):
            broker_core.serve(OPERATIONS, service_user=current_user(), connection=server)
        sender.join()
        reply = json.loads(client.makefile("rb").readline())

        self.assertIn(f"longer than {broker_core.MAX_REQUEST_BYTES} bytes", reply["message"])

    def test_a_client_that_sends_nothing_gets_an_answer_after_the_read_timeout(self) -> None:
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        with patch.object(broker_core, "READ_TIMEOUT_SECONDS", 0.2), redirect_stderr(io.StringIO()):
            broker_core.serve(OPERATIONS, service_user=current_user(), connection=server)
        reply = json.loads(client.makefile("rb").readline())

        self.assertIn("no request arrived within 0.2 s", reply["message"])

    def test_the_arguments_and_their_secrets_are_never_logged(self) -> None:
        _code, _reply, log = self.exchange(self.request("echo", {"password": SECRET}))

        self.assertNotIn(SECRET, log)

    def test_a_client_gone_before_the_reply_is_reported_and_fails(self) -> None:
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        client.sendall(self.request("echo"))
        client.close()
        log = io.StringIO()
        with redirect_stderr(log), patch.object(broker_core, "send_reply", side_effect=BrokenPipeError(32, "Broken pipe")):
            code = broker_core.serve(OPERATIONS, service_user=current_user(), connection=server)

        self.assertEqual(code, 1)
        self.assertIn("the reply could not be sent", log.getvalue())


class AllowedUidTests(unittest.TestCase):
    def test_root_is_always_allowed_and_the_service_user_when_it_exists(self) -> None:
        self.assertEqual(broker_core.allowed_uids(current_user()), frozenset({0, os.getuid()}))
        self.assertEqual(broker_core.allowed_uids("abr-nobody-here"), frozenset({0}))

    def test_the_service_user_option_defaults_to_altastor_backup(self) -> None:
        self.assertEqual(broker_core.parse_arguments([], description="x").service_user, "altastor-backup")
        self.assertEqual(broker_core.parse_arguments(["--service-user", "as"], description="x").service_user, "as")


if __name__ == "__main__":
    unittest.main()
