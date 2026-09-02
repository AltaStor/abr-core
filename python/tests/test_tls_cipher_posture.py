# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import os
import socket
import ssl
import tempfile
import threading
import unittest
import warnings
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

from uvicorn.config import SSL_PROTOCOL_VERSION, create_ssl_context

from python.srv.core.tls_bootstrap import (
    TLS_CIPHERS_ENV_VAR,
    ensure_self_signed_cert,
    resolve_tls_ciphers,
)

REQUIRED_AEAD_SUITES = (
    "ECDHE-RSA-AES256-GCM-SHA384",
    "ECDHE-RSA-AES128-GCM-SHA256",
    "ECDHE-RSA-CHACHA20-POLY1305",
)
MUST_BE_REFUSED = (
    "ECDHE-RSA-AES256-SHA",
    "ECDHE-RSA-AES128-SHA",
    "AES256-GCM-SHA384",
    "AES128-SHA256",
)


class _TlsListener:
    def __init__(self, context: ssl.SSLContext) -> None:
        self._context = context
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(16)
        self.port = self._socket.getsockname()[1]
        self._closing = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._closing.is_set():
            try:
                raw, _ = self._socket.accept()
            except OSError:
                return
            try:
                with self._context.wrap_socket(raw, server_side=True) as tls:
                    tls.recv(1)
            except (ssl.SSLError, OSError):
                with suppress(OSError):
                    raw.close()

    def close(self) -> None:
        self._closing.set()
        with suppress(OSError):
            self._socket.close()
        self._thread.join(timeout=5)


class Tls12CipherPostureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cert = Path(cls._tmp.name) / "server.crt"
        key = Path(cls._tmp.name) / "server.key"
        ensure_self_signed_cert(cert, key)
        context = create_ssl_context(
            certfile=str(cert),
            keyfile=str(key),
            password=None,
            ssl_version=SSL_PROTOCOL_VERSION,
            cert_reqs=ssl.CERT_NONE,
            ca_certs=None,
            ciphers=resolve_tls_ciphers(),
        )
        cls._listener = _TlsListener(context)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._listener.close()
        cls._tmp.cleanup()

    def _negotiates(self, suite: str, *, version: ssl.TLSVersion) -> bool:
        client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client.check_hostname = False
        client.verify_mode = ssl.CERT_NONE
        client.minimum_version = version
        client.maximum_version = version
        try:
            client.set_ciphers(suite)
        except ssl.SSLError:
            self.skipTest(f"this OpenSSL cannot offer {suite}")
        try:
            with socket.create_connection(("127.0.0.1", self._listener.port), timeout=5) as raw:
                with client.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
                    return tls.cipher() is not None
        except (ssl.SSLError, OSError):
            return False

    def test_the_aead_suites_the_ticket_asks_for_are_offered(self) -> None:
        for suite in REQUIRED_AEAD_SUITES:
            with self.subTest(suite=suite):
                self.assertTrue(
                    self._negotiates(suite, version=ssl.TLSVersion.TLSv1_2),
                    f"{suite} should be negotiable on TLS 1.2",
                )

    def test_sha1_mac_and_non_forward_secret_suites_are_refused(self) -> None:
        for suite in MUST_BE_REFUSED:
            with self.subTest(suite=suite):
                self.assertFalse(
                    self._negotiates(suite, version=ssl.TLSVersion.TLSv1_2),
                    f"{suite} should not be negotiable on TLS 1.2",
                )

    def test_tls13_is_not_governed_by_the_tls12_cipher_string(self) -> None:
        client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client.check_hostname = False
        client.verify_mode = ssl.CERT_NONE
        with socket.create_connection(("127.0.0.1", self._listener.port), timeout=5) as raw:
            with client.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
                self.assertEqual(tls.version(), "TLSv1.3")
                self.assertIn("GCM", tls.cipher()[0])

    def test_tls10_and_tls11_stay_refused(self) -> None:
        for version in (ssl.TLSVersion.TLSv1, ssl.TLSVersion.TLSv1_1):
            with self.subTest(version=version):
                client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                client.check_hostname = False
                client.verify_mode = ssl.CERT_NONE
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", DeprecationWarning)
                        client.minimum_version = version
                        client.maximum_version = version
                except ValueError:
                    self.skipTest(f"this OpenSSL refuses to offer {version!r} at all")
                try:
                    with socket.create_connection(("127.0.0.1", self._listener.port), timeout=5) as raw:
                        with client.wrap_socket(raw, server_hostname="127.0.0.1"):
                            negotiated = True
                except (ssl.SSLError, OSError):
                    negotiated = False
                self.assertFalse(negotiated, f"{version!r} should be refused")


class CertificateRenewalKeepsThePostureTests(unittest.TestCase):
    def test_reloading_a_renewed_certificate_keeps_the_pinned_ciphers(self) -> None:
        from python.srv.core.tls_renewal import check_and_renew

        with tempfile.TemporaryDirectory() as tmp:
            cert, key = Path(tmp) / "server.crt", Path(tmp) / "server.key"
            ensure_self_signed_cert(cert, key)
            context = create_ssl_context(
                certfile=str(cert),
                keyfile=str(key),
                password=None,
                ssl_version=SSL_PROTOCOL_VERSION,
                cert_reqs=ssl.CERT_NONE,
                ca_certs=None,
                ciphers=resolve_tls_ciphers(),
            )
            before = {c["name"] for c in context.get_ciphers()}

            with patch(
                "python.srv.core.tls_renewal.ensure_self_signed_cert", return_value=True
            ):
                self.assertTrue(check_and_renew(cert, key, context))

            after = {c["name"] for c in context.get_ciphers()}

        self.assertEqual(before, after, "renewal must not change the offered suites")
        for suite in (c for c in context.get_ciphers() if c["protocol"] != "TLSv1.3"):
            with self.subTest(suite=suite["name"]):
                self.assertIn("AEAD", suite["description"])


class TlsCipherResolutionTests(unittest.TestCase):
    def test_the_shipped_list_is_aead_only(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(TLS_CIPHERS_ENV_VAR, None)
            context.set_ciphers(resolve_tls_ciphers())
        tls12 = [c for c in context.get_ciphers() if c["protocol"] != "TLSv1.3"]
        self.assertTrue(tls12, "the string should still select TLS 1.2 suites")
        for suite in tls12:
            with self.subTest(suite=suite["name"]):
                self.assertTrue(suite["name"].startswith("ECDHE-"), "must be forward secret")
                self.assertTrue(
                    suite["name"].endswith(("-GCM-SHA384", "-GCM-SHA256", "CHACHA20-POLY1305")),
                    "must be an AEAD suite",
                )

    def test_a_valid_override_is_honoured(self) -> None:
        with patch.dict(os.environ, {TLS_CIPHERS_ENV_VAR: "ECDHE-RSA-AES128-GCM-SHA256"}):
            self.assertEqual(resolve_tls_ciphers(), "ECDHE-RSA-AES128-GCM-SHA256")

    def test_an_unusable_override_falls_back_instead_of_failing_to_boot(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(TLS_CIPHERS_ENV_VAR, None)
            shipped = resolve_tls_ciphers()
        for bad in ("", "   ", "NOT-A-CIPHER-@@@", "aNULL:!aNULL"):
            with self.subTest(value=bad):
                with patch.dict(os.environ, {TLS_CIPHERS_ENV_VAR: bad}):
                    with self.assertLogs("python.srv.core.tls_bootstrap", level="WARNING"):
                        self.assertEqual(resolve_tls_ciphers(), shipped)

    def test_an_unset_value_is_not_reported_as_a_misconfiguration(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(TLS_CIPHERS_ENV_VAR, None)
            with self.assertNoLogs("python.srv.core.tls_bootstrap", level="WARNING"):
                self.assertTrue(resolve_tls_ciphers())


if __name__ == "__main__":
    unittest.main()
