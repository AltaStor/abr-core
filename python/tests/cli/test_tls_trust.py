# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import ipaddress
import json
import socket
import ssl
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from python.cli import tls_trust
from python.cli.errors import CliStructuredError
from python.cli.token_store import CONFIG_DIR_ENV_VAR

URL = "https://127.0.0.1:8443"
KEY = "127.0.0.1:8443"


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _certificate(subject, issuer, key, signing_key, *, ca: bool) -> x509.Certificate:
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(_name(issuer))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signing_key.public_key()), critical=False)
    )
    if ca:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    else:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False
        ).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    return builder.sign(signing_key, hashes.SHA256())


class _CorporateServer:
    def __init__(self, directory: Path) -> None:
        ca_key, self.server_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
        ca = _certificate("Corporate Root CA", "Corporate Root CA", ca_key, ca_key, ca=True)
        server = _certificate("backup", "Corporate Root CA", self.server_key, ca_key, ca=False)
        self.ca_file = directory / "corporate-ca.pem"
        self.ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        cert_file, key_file = directory / "server.crt", directory / "server.key"
        cert_file.write_bytes(server.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(
            self.server_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        self.fingerprint = tls_trust._spki_fingerprint(server.public_bytes(serialization.Encoding.DER))
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(str(cert_file), str(key_file))
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            try:
                with self.context.wrap_socket(connection, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass

    def close(self) -> None:
        self.listener.shutdown(socket.SHUT_RDWR)
        self.listener.close()
        self.thread.join(timeout=5)


class EnsureTrustedTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "trusted_certs.json"
        environment = patch.dict("os.environ", {CONFIG_DIR_ENV_VAR: tmp.name})
        environment.start()
        self.addCleanup(environment.stop)

    def _pins(self) -> dict[str, str]:
        return json.loads(self.store.read_text(encoding="utf-8"))

    def _presenting(self, fingerprint: str, verified: str | None = None):
        fetched = patch.object(tls_trust, "_fetch_fingerprint", return_value=fingerprint)
        checked = patch.object(tls_trust, "_ca_verified_fingerprint", return_value=verified)
        return fetched, checked

    def _ensure(self, fingerprint: str, verified: str | None = None) -> None:
        fetched, checked = self._presenting(fingerprint, verified)
        with fetched, checked:
            tls_trust.ensure_trusted(URL)

    def test_the_first_connection_pins_the_key(self) -> None:
        self._ensure("aa")

        self.assertEqual(self._pins(), {KEY: "aa"})

    def test_the_same_key_is_accepted_without_a_ca_check(self) -> None:
        self._ensure("aa")
        fetched, checked = self._presenting("aa")
        with fetched, checked as ca_check:
            tls_trust.ensure_trusted(URL)

        ca_check.assert_not_called()

    def test_a_new_key_on_a_certificate_a_trusted_ca_issued_replaces_the_pin(self) -> None:
        self._ensure("aa")

        self._ensure("bb", verified="bb")

        self.assertEqual(self._pins(), {KEY: "bb"})

    def test_a_new_key_no_trusted_ca_vouches_for_is_refused(self) -> None:
        self._ensure("aa")

        with self.assertRaises(CliStructuredError) as caught:
            self._ensure("bb", verified=None)

        self.assertEqual(caught.exception.code, "tls_server_key_changed")
        self.assertIn("SSL_CERT_FILE", caught.exception.message)
        self.assertIn(KEY, caught.exception.message)
        self.assertEqual(self._pins(), {KEY: "aa"})

    def test_a_verified_connection_that_shows_another_key_is_refused(self) -> None:
        self._ensure("aa")

        with self.assertRaises(CliStructuredError):
            self._ensure("bb", verified="cc")

        self.assertEqual(self._pins(), {KEY: "aa"})


class CaVerifiedFingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.server = _CorporateServer(Path(tmp.name))
        self.addCleanup(self.server.close)

    def test_a_certificate_from_a_ca_in_ssl_cert_file_is_verified(self) -> None:
        with patch.dict("os.environ", {"SSL_CERT_FILE": str(self.server.ca_file)}):
            fingerprint = tls_trust._ca_verified_fingerprint("127.0.0.1", self.server.port)

        self.assertEqual(fingerprint, self.server.fingerprint)
        self.assertEqual(tls_trust._fetch_fingerprint("127.0.0.1", self.server.port), self.server.fingerprint)

    def test_a_certificate_from_an_unknown_ca_is_not_verified(self) -> None:
        with (
            tempfile.NamedTemporaryFile(suffix=".pem") as empty,
            patch.dict("os.environ", {"SSL_CERT_FILE": empty.name, "SSL_CERT_DIR": empty.name}),
        ):
            self.assertIsNone(tls_trust._ca_verified_fingerprint("127.0.0.1", self.server.port))

    def test_a_certificate_for_another_host_is_not_verified(self) -> None:
        with patch.dict("os.environ", {"SSL_CERT_FILE": str(self.server.ca_file)}):
            self.assertIsNone(tls_trust._ca_verified_fingerprint("localhost", self.server.port))


if __name__ == "__main__":
    unittest.main()
