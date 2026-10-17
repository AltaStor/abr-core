# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import asyncio
import http.client
import logging
import os
import pathlib
import socket
import sys
import time
import unittest
from unittest.mock import patch

repo_root = pathlib.Path(__file__).resolve().parents[4]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

try:
    import uvicorn  # noqa: F401
except ModuleNotFoundError:
    venv_lib = repo_root / ".venv" / "lib"
    for site_packages in sorted(venv_lib.glob("python*/site-packages")):
        sys.path.insert(0, str(site_packages))

from python.srv.core.http_redirect import (
    REDIRECT_PORT_ENV_VAR,
    HostPolicy,
    build_redirect_app,
    host_key,
    normalise_host,
    redirect_target,
    server_host_policy,
    start_redirect_listener,
)

CANONICAL = "10.9.9.9"
POLICY = HostPolicy(
    allowed=frozenset({"backup01", "backup01.lan", "192.0.2.90", "[::1]", "[fe80::1]"}),
    canonical=CANONICAL,
)


def scope_for(host: str | bytes | None, path: str = "/", query: bytes = b"", method: str = "GET") -> dict:
    headers: list[tuple[bytes, bytes]] = [(b"user-agent", b"test")]
    if host is not None:
        headers.append((b"host", host if isinstance(host, bytes) else host.encode("latin-1")))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query,
        "headers": headers,
    }


def call_app(app, scope: dict) -> tuple[int, dict[str, str], bytes]:
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    if not messages:
        return 0, {}, b""
    start = messages[0]
    headers = {name.decode("latin-1").lower(): value.decode("latin-1") for name, value in start["headers"]}
    body = b"".join(message.get("body", b"") for message in messages[1:])
    return start["status"], headers, body


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class HostNormalisationTests(unittest.TestCase):
    def test_plain_host_names_and_addresses_survive(self) -> None:
        self.assertEqual(normalise_host("backup01"), "backup01")
        self.assertEqual(normalise_host("backup01.lan."), "backup01.lan.")
        self.assertEqual(normalise_host("192.0.2.90"), "192.0.2.90")

    def test_the_client_port_is_dropped(self) -> None:
        self.assertEqual(normalise_host("backup01:80"), "backup01")
        self.assertEqual(normalise_host(b"192.0.2.90:8080"), "192.0.2.90")

    def test_ipv6_literals_keep_their_brackets(self) -> None:
        self.assertEqual(normalise_host("[::1]"), "[::1]")
        self.assertEqual(normalise_host("[fe80::1]:8080"), "[fe80::1]")

    def test_values_that_are_not_a_host_are_refused(self) -> None:
        for value in (
            "",
            "   ",
            ":8443",
            "../evil",
            "evil.com/path",
            "https://evil.com",
            "evil.com\r\nX-Injected: 1",
            "backup01:notaport",
            "backup01:0",
            "backup01:99999",
            "[::1",
            "[not-an-address]",
            "fe80::1",
            "a" * 256,
        ):
            with self.subTest(value=value):
                self.assertIsNone(normalise_host(value))

    def test_non_ascii_is_refused(self) -> None:
        self.assertIsNone(normalise_host("бекап".encode("utf-8")))


class RedirectTargetTests(unittest.TestCase):
    def test_host_path_and_query_are_all_carried_over(self) -> None:
        target = redirect_target(
            scope_for("192.0.2.90:8080", "/jobs/42", b"state=running&page=2"), 8443, POLICY
        )
        self.assertEqual(target, "https://192.0.2.90:8443/jobs/42?state=running&page=2")

    def test_the_port_is_omitted_when_https_listens_on_443(self) -> None:
        self.assertEqual(
            redirect_target(scope_for("backup01"), 443, POLICY), "https://backup01/"
        )

    def test_percent_encoding_is_preserved(self) -> None:
        scope = scope_for("backup01", "/backups/name%20with%20space")
        scope["raw_path"] = b"/backups/name%20with%20space"
        self.assertEqual(
            redirect_target(scope, 8443, POLICY),
            "https://backup01:8443/backups/name%20with%20space",
        )

    def test_a_missing_host_yields_no_target(self) -> None:
        self.assertIsNone(redirect_target(scope_for(None), 8443, POLICY))


class RedirectAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = build_redirect_app(https_port=8443, policy=POLICY)

    def test_a_request_is_answered_with_a_permanent_redirect(self) -> None:
        status, headers, body = call_app(self.app, scope_for("192.0.2.90:80"))
        self.assertEqual(status, 308)
        self.assertEqual(headers["location"], "https://192.0.2.90:8443/")
        self.assertEqual(headers["content-length"], "0")
        self.assertEqual(body, b"")

    def test_308_is_used_so_a_post_keeps_its_method_and_body(self) -> None:
        status, headers, _ = call_app(
            self.app, scope_for("backup01", "/api/v1/sessions", method="POST")
        )
        self.assertEqual(status, 308)
        self.assertEqual(headers["location"], "https://backup01:8443/api/v1/sessions")

    def test_every_path_gets_the_same_treatment(self) -> None:
        for path in ("/", "/health", "/api/v1/jobs", "/assets/index.js", "/no-such-route"):
            with self.subTest(path=path):
                status, headers, _ = call_app(self.app, scope_for("backup01", path))
                self.assertEqual(status, 308)
                self.assertEqual(headers["location"], f"https://backup01:8443{path}")

    def test_a_missing_host_is_a_bad_request_not_a_guessed_redirect(self) -> None:
        status, headers, body = call_app(self.app, scope_for(None))
        self.assertEqual(status, 400)
        self.assertNotIn("location", headers)
        self.assertIn(b"Host", body)
        self.assertEqual(headers["content-length"], str(len(body)))

    def test_an_unusable_host_is_a_bad_request(self) -> None:
        status, headers, _ = call_app(self.app, scope_for("../evil"))
        self.assertEqual(status, 400)
        self.assertNotIn("location", headers)

    def test_security_headers_from_the_provider_are_attached(self) -> None:
        app = build_redirect_app(
            https_port=8443,
            policy=POLICY,
            security_headers=lambda: {"X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"},
        )
        _, headers, _ = call_app(app, scope_for("backup01"))
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["referrer-policy"], "no-referrer")

    def test_the_provider_is_consulted_per_request(self) -> None:
        values = iter(["DENY", "SAMEORIGIN"])
        app = build_redirect_app(
            https_port=8443,
            policy=POLICY,
            security_headers=lambda: {"X-Frame-Options": next(values)},
        )
        _, first, _ = call_app(app, scope_for("backup01"))
        _, second, _ = call_app(app, scope_for("backup01"))
        self.assertEqual(first["x-frame-options"], "DENY")
        self.assertEqual(second["x-frame-options"], "SAMEORIGIN")

    def test_a_non_http_scope_is_ignored(self) -> None:
        status, headers, _ = call_app(self.app, {"type": "websocket", "path": "/", "headers": []})
        self.assertEqual(status, 0)
        self.assertEqual(headers, {})


class ListenerStartupTests(unittest.TestCase):
    def test_an_empty_port_switches_the_listener_off(self) -> None:
        with patch.dict(os.environ, {REDIRECT_PORT_ENV_VAR: ""}):
            self.assertIsNone(start_redirect_listener(host="127.0.0.1", https_port=8443))

    def test_an_unparsable_port_does_not_stop_the_server(self) -> None:
        for value in ("nope", "0", "70000", "-1"):
            with self.subTest(value=value), patch.dict(os.environ, {REDIRECT_PORT_ENV_VAR: value}):
                self.assertIsNone(start_redirect_listener(host="127.0.0.1", https_port=8443))

    def test_the_https_port_is_refused(self) -> None:
        with patch.dict(os.environ, {REDIRECT_PORT_ENV_VAR: "8443"}):
            self.assertIsNone(start_redirect_listener(host="127.0.0.1", https_port=8443))

    def test_a_port_already_in_use_is_a_warning_not_a_failure(self) -> None:
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            port = taken.getsockname()[1]
            with patch.dict(os.environ, {REDIRECT_PORT_ENV_VAR: str(port)}):
                self.assertIsNone(start_redirect_listener(host="127.0.0.1", https_port=8443))

    def test_starting_the_listener_leaves_the_access_logger_alone(self) -> None:
        access_logger = logging.getLogger("uvicorn.access")
        marker = logging.NullHandler()
        access_logger.addHandler(marker)
        port = free_port()
        try:
            with patch.dict(os.environ, {REDIRECT_PORT_ENV_VAR: str(port)}):
                listener = start_redirect_listener(host="127.0.0.1", https_port=8443)
            self.assertIsNotNone(listener)
            listener.stop()
            self.assertIn(marker, access_logger.handlers)
            self.assertTrue(access_logger.propagate or access_logger.handlers)
        finally:
            access_logger.removeHandler(marker)

    def test_the_listener_serves_redirects_and_stops_cleanly(self) -> None:
        port = free_port()
        with patch.dict(os.environ, {REDIRECT_PORT_ENV_VAR: str(port)}):
            listener = start_redirect_listener(host="127.0.0.1", https_port=8443)
        self.assertIsNotNone(listener)
        try:
            deadline = 50
            while deadline and not listener.server.started:
                deadline -= 1
                time.sleep(0.1)
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/jobs?state=running")
            response = connection.getresponse()
            self.assertEqual(response.status, 308)
            self.assertEqual(
                response.getheader("Location"),
                "https://127.0.0.1:8443/jobs?state=running",
            )
            connection.close()
        finally:
            listener.stop()
        self.assertFalse(listener.thread.is_alive())


class ForeignHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = build_redirect_app(https_port=8443, policy=POLICY)

    def test_a_foreign_host_is_never_echoed_into_the_location(self) -> None:
        for value in ("evil.example.com", "attacker.test", "127.0.0.1", "10.0.0.1"):
            with self.subTest(value=value):
                status, headers, _ = call_app(self.app, scope_for(value, "/api/v1/auth/login"))
                self.assertEqual(status, 308)
                self.assertEqual(
                    headers["location"], f"https://{CANONICAL}:8443/api/v1/auth/login"
                )
                self.assertNotIn(value, headers["location"])

    def test_a_client_port_cannot_smuggle_a_foreign_host_through(self) -> None:
        _, headers, _ = call_app(self.app, scope_for("evil.example.com:8080"))
        self.assertEqual(headers["location"], f"https://{CANONICAL}:8443/")

    def test_the_path_and_query_survive_the_rewrite(self) -> None:
        _, headers, _ = call_app(
            self.app, scope_for("evil.example.com", "/jobs/42", b"state=running")
        )
        self.assertEqual(
            headers["location"], f"https://{CANONICAL}:8443/jobs/42?state=running"
        )

    def test_a_foreign_host_keeps_the_permanent_status_for_a_post(self) -> None:
        status, headers, _ = call_app(
            self.app, scope_for("evil.example.com", "/api/v1/sessions", method="POST")
        )
        self.assertEqual(status, 308)
        self.assertEqual(headers["location"], f"https://{CANONICAL}:8443/api/v1/sessions")

    def test_an_allowed_host_is_left_alone(self) -> None:
        _, headers, _ = call_app(self.app, scope_for("backup01", "/jobs"))
        self.assertEqual(headers["location"], "https://backup01:8443/jobs")

    def test_the_match_ignores_case(self) -> None:
        _, headers, _ = call_app(self.app, scope_for("BACKUP01"))
        self.assertEqual(headers["location"], "https://BACKUP01:8443/")

    def test_a_trailing_root_dot_still_matches(self) -> None:
        _, headers, _ = call_app(self.app, scope_for("backup01.lan."))
        self.assertEqual(headers["location"], "https://backup01.lan.:8443/")

    def test_an_expanded_ipv6_spelling_cannot_dodge_the_check(self) -> None:
        _, headers, _ = call_app(self.app, scope_for("[0:0:0:0:0:0:0:1]"))
        self.assertEqual(headers["location"], "https://[::1]:8443/")
        _, headers, _ = call_app(self.app, scope_for("[2001:db8::1]"))
        self.assertEqual(headers["location"], f"https://{CANONICAL}:8443/")

    def test_an_empty_allowlist_sends_everything_to_the_canonical_host(self) -> None:
        app = build_redirect_app(
            https_port=8443, policy=HostPolicy(allowed=frozenset(), canonical=CANONICAL)
        )
        for value in ("backup01", "localhost", "127.0.0.1"):
            with self.subTest(value=value):
                _, headers, _ = call_app(app, scope_for(value))
                self.assertEqual(headers["location"], f"https://{CANONICAL}:8443/")

    def test_a_missing_host_is_still_a_bad_request(self) -> None:
        status, headers, body = call_app(self.app, scope_for(None))
        self.assertEqual(status, 400)
        self.assertNotIn("location", headers)
        self.assertIn(b"Missing or unusable Host", body)

    def test_a_rewritten_redirect_still_carries_the_protective_headers(self) -> None:
        app = build_redirect_app(
            https_port=8443,
            policy=POLICY,
            security_headers=lambda: {"X-Frame-Options": "DENY"},
        )
        status, headers, _ = call_app(app, scope_for("evil.example.com"))
        self.assertEqual(status, 308)
        self.assertEqual(headers["x-frame-options"], "DENY")


class ServerHostPolicyTests(unittest.TestCase):
    def test_loopback_and_the_system_name_are_always_allowed(self) -> None:
        policy = server_host_policy()
        for value in ("localhost", "127.0.0.1", "[::1]"):
            self.assertIn(value, policy.allowed)
        self.assertIn(host_key(socket.gethostname()), policy.allowed)

    def test_the_canonical_host_is_a_routable_address_of_this_machine(self) -> None:
        with patch(
            "python.srv.core.tls_bootstrap._detect_local_ips",
            return_value=["127.0.0.1", "192.0.2.90", "10.0.0.5"],
        ):
            policy = server_host_policy()
        self.assertEqual(policy.canonical, "10.0.0.5")
        self.assertIn("192.0.2.90", policy.allowed)

    def test_ipv4_is_preferred_over_ipv6_for_the_canonical_host(self) -> None:
        with patch(
            "python.srv.core.tls_bootstrap._detect_local_ips",
            return_value=["2001:db8::5", "192.0.2.90"],
        ):
            policy = server_host_policy()
        self.assertEqual(policy.canonical, "192.0.2.90")

    def test_an_ipv6_only_machine_gets_a_bracketed_canonical_host(self) -> None:
        with patch(
            "python.srv.core.tls_bootstrap._detect_local_ips", return_value=["2001:db8::5"]
        ):
            policy = server_host_policy()
        self.assertEqual(policy.canonical, "[2001:db8::5]")

    def test_loopback_is_the_last_resort_canonical_host(self) -> None:
        with patch("python.srv.core.tls_bootstrap._detect_local_ips", return_value=[]):
            policy = server_host_policy()
        self.assertEqual(policy.canonical, "127.0.0.1")

    def test_a_detection_failure_still_yields_a_usable_policy(self) -> None:
        with patch(
            "python.srv.core.tls_bootstrap._detect_local_ips", side_effect=OSError("no interfaces")
        ):
            policy = server_host_policy()
        self.assertIn("localhost", policy.allowed)
        self.assertEqual(policy.canonical, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
