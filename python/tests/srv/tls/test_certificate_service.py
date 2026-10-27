# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import socket
import ssl
import stat
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi.testclient import TestClient

from python.srv.backup_server import app
from python.srv.core import tls_bootstrap
from python.srv.core.mongo_db_wrapper import SESSIONS, USERS
from python.srv.core.tls_bootstrap import ensure_self_signed_cert
from python.srv.services import auth_service, tls_certificate_service
from python.srv.services.api_errors import ApiError

BOOTSTRAP_ENV = {
    "ABR_BOOTSTRAP_ADMIN_USER": "admin",
    "ABR_BOOTSTRAP_ADMIN_PASSWORD": "Sup3rSecretPW",
}


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _key_pem(key, passphrase: str | None = None) -> str:
    encryption = (
        serialization.BestAvailableEncryption(passphrase.encode())
        if passphrase
        else serialization.NoEncryption()
    )
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
    ).decode()


class _Pki:
    """A root CA, an intermediate, and a leaf for `backup.example.com`."""

    def __init__(
        self,
        *,
        leaf_names: list[x509.GeneralName] | None = None,
        not_before: datetime | None = None,
        not_after: datetime | None = None,
        usages: list[x509.ObjectIdentifier] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.root_key = ec.generate_private_key(ec.SECP256R1())
        self.root = self._build(_name("Test Root CA"), _name("Test Root CA"), self.root_key, self.root_key, ca=True)
        self.intermediate_key = ec.generate_private_key(ec.SECP256R1())
        self.intermediate = self._build(
            _name("Test Issuing CA"), self.root.subject, self.intermediate_key, self.root_key, ca=True
        )
        self.leaf_key = ec.generate_private_key(ec.SECP256R1())
        builder = (
            x509.CertificateBuilder()
            .subject_name(_name("backup.example.com"))
            .issuer_name(self.intermediate.subject)
            .public_key(self.leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before or now - timedelta(days=1))
            .not_valid_after(not_after or now + timedelta(days=200))
            .add_extension(
                x509.ExtendedKeyUsage(usages or [ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
            )
        )
        names = [x509.DNSName("backup.example.com")] if leaf_names is None else leaf_names
        if names:
            builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=False)
        self.leaf = builder.sign(self.intermediate_key, hashes.SHA256())

    @staticmethod
    def _build(subject, issuer, key, signing_key, *, ca: bool) -> x509.Certificate:
        now = datetime.now(timezone.utc)
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=10))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
            .sign(signing_key, hashes.SHA256())
        )

    @property
    def chain_pem(self) -> str:
        return _pem(self.leaf) + _pem(self.intermediate)


def _served_certificate(
    context: ssl.SSLContext, version: ssl.TLSVersion = ssl.TLSVersion.TLSv1_3
) -> x509.Certificate:
    """Complete one real handshake against `context` and return what it served."""
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve() -> None:
        conn, _ = listener.accept()
        try:
            with context.wrap_socket(conn, server_side=True):
                pass
        except (ssl.SSLError, OSError):
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.check_hostname = False
    client.verify_mode = ssl.CERT_NONE
    client.minimum_version = client.maximum_version = version
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            with client.wrap_socket(sock) as tls_sock:
                der = tls_sock.getpeercert(binary_form=True)
    finally:
        thread.join(timeout=5)
        listener.close()
    return x509.load_der_x509_certificate(der)


class _TlsDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cert_path = Path(self._tmp.name) / "server.crt"
        self.key_path = Path(self._tmp.name) / "server.key"
        ensure_self_signed_cert(self.cert_path, self.key_path)
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(str(self.cert_path), str(self.key_path))
        tls_certificate_service.register_live_tls(self.cert_path, self.key_path, self.context)
        self.addCleanup(tls_certificate_service.clear_live_tls)

    def assertApiError(self, code: str, status: int = 422):
        test = self

        class _Expect:
            def __enter__(self):
                self.before = (test.cert_path.read_bytes(), test.key_path.read_bytes())
                return self

            def __exit__(self, exc_type, exc, tb):
                test.assertIsNotNone(exc, f"expected ApiError {code}")
                test.assertIsInstance(exc, ApiError)
                test.assertEqual(exc.code, code, exc.message)
                test.assertEqual(exc.status_code, status)
                test.assertEqual((test.cert_path.read_bytes(), test.key_path.read_bytes()), self.before)
                return True

        return _Expect()


class AutoRenewalLeavesAdministratorCertificatesAloneTests(unittest.TestCase):
    def _write(self, cert_path: Path, key_path: Path, cert: x509.Certificate, key) -> None:
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_text(_key_pem(key))

    def test_a_ca_issued_certificate_dropped_in_by_hand_is_not_regenerated(self) -> None:
        pki = _Pki()
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = Path(tmp) / "server.crt", Path(tmp) / "server.key"
            self._write(cert_path, key_path, pki.leaf, pki.leaf_key)
            before = cert_path.read_bytes()

            self.assertFalse(ensure_self_signed_cert(cert_path, key_path))
            self.assertEqual(cert_path.read_bytes(), before)

    def test_an_installed_self_signed_localhost_certificate_is_kept_because_of_the_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = Path(tmp) / "server.crt", Path(tmp) / "server.key"
            ensure_self_signed_cert(cert_path, key_path)
            tls_bootstrap.write_administrator_marker(cert_path, {"installed_by": "admin"})
            before = cert_path.read_bytes()

            with patch.object(tls_bootstrap, "_detect_local_ips", return_value=["198.51.100.7"]):
                self.assertFalse(ensure_self_signed_cert(cert_path, key_path))
            self.assertEqual(cert_path.read_bytes(), before)

    def test_an_expiring_administrator_certificate_is_logged_not_replaced(self) -> None:
        pki = _Pki(not_after=datetime.now(timezone.utc) + timedelta(days=3))
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = Path(tmp) / "server.crt", Path(tmp) / "server.key"
            self._write(cert_path, key_path, pki.leaf, pki.leaf_key)

            with self.assertLogs(tls_bootstrap.logger, level="WARNING") as logs:
                self.assertFalse(ensure_self_signed_cert(cert_path, key_path))
            self.assertIn("not renewed automatically", "\n".join(logs.output))

    def test_an_unreadable_certificate_is_regenerated_and_its_stale_marker_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = Path(tmp) / "server.crt", Path(tmp) / "server.key"
            ensure_self_signed_cert(cert_path, key_path)
            tls_bootstrap.write_administrator_marker(cert_path, {})
            cert_path.write_text("not a certificate")

            self.assertTrue(ensure_self_signed_cert(cert_path, key_path))
            self.assertIsNone(tls_bootstrap.read_administrator_marker(cert_path))
            regenerated = x509.load_pem_x509_certificate(cert_path.read_bytes())
            self.assertTrue(tls_bootstrap.is_appliance_generated(regenerated))


class DescribeCertificateTests(_TlsDirTestCase):
    def test_describes_the_generated_certificate(self) -> None:
        result = tls_certificate_service.get_certificate()

        self.assertEqual(result["source"], "generated")
        self.assertTrue(result["auto_renew"])
        self.assertTrue(result["self_signed"])
        self.assertEqual(result["common_name"], "localhost")
        self.assertIn("127.0.0.1", result["ip_addresses"])
        self.assertTrue(result["live_reload"])
        self.assertEqual(result["chain_length"], 1)
        self.assertRegex(result["fingerprint_sha256"], r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")

    def test_warns_when_the_certificate_names_none_of_the_appliance_addresses(self) -> None:
        pki = _Pki()
        tls_certificate_service.install_certificate(pki.chain_pem, _key_pem(pki.leaf_key))

        with patch.object(tls_certificate_service, "_appliance_names", return_value=["10.0.0.5", "vault01"]):
            codes = [w["code"] for w in tls_certificate_service.get_certificate()["warnings"]]
        self.assertIn("appliance_names_not_covered", codes)

        with patch.object(
            tls_certificate_service, "_appliance_names", return_value=["backup.example.com"]
        ):
            codes = [w["code"] for w in tls_certificate_service.get_certificate()["warnings"]]
        self.assertNotIn("appliance_names_not_covered", codes)

    def test_wildcards_cover_exactly_one_label(self) -> None:
        self.assertTrue(tls_certificate_service._dns_name_matches("*.example.com", "vault.example.com"))
        self.assertFalse(tls_certificate_service._dns_name_matches("*.example.com", "a.b.example.com"))
        self.assertFalse(tls_certificate_service._dns_name_matches("*.example.com", "example.com"))

    def test_warns_about_missing_subject_alternative_names_and_near_expiry(self) -> None:
        pki = _Pki(leaf_names=[], not_after=datetime.now(timezone.utc) + timedelta(days=5))
        tls_certificate_service.install_certificate(pki.chain_pem, _key_pem(pki.leaf_key))

        codes = [w["code"] for w in tls_certificate_service.get_certificate()["warnings"]]
        self.assertEqual(codes, ["expires_soon", "no_subject_alternative_names"])

    def test_warns_when_the_server_certificate_comes_without_its_issuer(self) -> None:
        pki = _Pki()
        tls_certificate_service.install_certificate(_pem(pki.leaf), _key_pem(pki.leaf_key))

        warnings = {w["code"]: w for w in tls_certificate_service.get_certificate()["warnings"]}
        self.assertIn("chain_incomplete", warnings)
        self.assertIn("Test Issuing CA", warnings["chain_incomplete"]["message"])
        self.assertEqual(warnings["chain_incomplete"]["issuer"], "CN=Test Issuing CA")

        tls_certificate_service.install_certificate(pki.chain_pem, _key_pem(pki.leaf_key))
        codes = [w["code"] for w in tls_certificate_service.get_certificate()["warnings"]]
        self.assertNotIn("chain_incomplete", codes)

    def test_a_self_signed_certificate_alone_is_a_complete_chain(self) -> None:
        codes = [w["code"] for w in tls_certificate_service.get_certificate()["warnings"]]
        self.assertNotIn("chain_incomplete", codes)


class InstallCertificateTests(_TlsDirTestCase):
    def test_a_refused_certificate_is_logged_with_the_reason(self) -> None:
        pki = _Pki()
        with (
            self.assertLogs(tls_certificate_service.logger, "WARNING") as logs,
            self.assertApiError("certificate_key_mismatch"),
        ):
            tls_certificate_service.install_certificate(
                pki.chain_pem, _key_pem(pki.intermediate_key), installed_by="admin"
            )

        self.assertEqual(len(logs.records), 1)
        self.assertIn("from admin refused", logs.output[0])
        self.assertIn("certificate_key_mismatch", logs.output[0])
        self.assertIn("The private key does not belong to the certificate", logs.output[0])

    def test_a_certificate_that_cannot_be_saved_is_logged(self) -> None:
        pki = _Pki()
        real_write = tls_certificate_service._atomic_write
        failures = [OSError("No space left on device")]

        def fail_on_the_certificate(path, data, mode):
            if path == self.cert_path and failures:
                raise failures.pop()
            real_write(path, data, mode)

        with (
            patch.object(tls_certificate_service, "_atomic_write", side_effect=fail_on_the_certificate),
            self.assertLogs(tls_certificate_service.logger, "ERROR") as logs,
            self.assertApiError("tls_certificate_write_failed", status=500),
        ):
            tls_certificate_service.install_certificate(
                pki.chain_pem, _key_pem(pki.leaf_key), installed_by="admin"
            )

        self.assertIn("No space left on device", logs.output[0])
        self.assertIn("current certificate stays in use", logs.output[0])

    def test_an_installed_certificate_logs_its_warnings(self) -> None:
        pki = _Pki()
        with self.assertLogs(tls_certificate_service.logger, "WARNING") as logs:
            tls_certificate_service.install_certificate(_pem(pki.leaf), _key_pem(pki.leaf_key))

        self.assertTrue(
            any("Only the server certificate is installed" in line for line in logs.output), logs.output
        )

    def test_installs_a_ca_chain_and_serves_it_without_a_restart(self) -> None:
        pki = _Pki()

        result = tls_certificate_service.install_certificate(
            pki.chain_pem, _key_pem(pki.leaf_key), installed_by="admin"
        )

        self.assertTrue(result["reloaded"])
        self.assertEqual(result["source"], "administrator")
        self.assertFalse(result["auto_renew"])
        self.assertFalse(result["self_signed"])
        self.assertEqual(result["issuer_common_name"], "Test Issuing CA")
        self.assertEqual(result["dns_names"], ["backup.example.com"])
        self.assertEqual(result["chain_length"], 2)
        self.assertEqual(result["installed_by"], "admin")
        self.assertEqual(_served_certificate(self.context), pki.leaf)
        self.assertEqual(stat.S_IMODE(self.key_path.stat().st_mode), 0o600)
        self.assertIsNotNone(tls_bootstrap.read_administrator_marker(self.cert_path))
        # ...and the renewal check now leaves it where it is.
        self.assertFalse(ensure_self_signed_cert(self.cert_path, self.key_path))

    def test_installs_a_self_signed_certificate(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(_name("vault.lab"))
            .issuer_name(_name("vault.lab"))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("vault.lab")]), critical=False)
            .sign(key, hashes.SHA256())
        )

        result = tls_certificate_service.install_certificate(_pem(cert), _key_pem(key))

        self.assertTrue(result["self_signed"])
        self.assertEqual(result["key_type"], "RSA 2048")
        self.assertEqual(_served_certificate(self.context), cert)

    def test_an_encrypted_key_needs_its_passphrase_which_is_not_stored(self) -> None:
        pki = _Pki()
        encrypted = _key_pem(pki.leaf_key, passphrase="correct horse")

        with self.assertApiError("private_key_passphrase_required"):
            tls_certificate_service.install_certificate(pki.chain_pem, encrypted)
        with self.assertApiError("private_key_passphrase_incorrect"):
            tls_certificate_service.install_certificate(pki.chain_pem, encrypted, "wrong")

        tls_certificate_service.install_certificate(pki.chain_pem, encrypted, "correct horse")
        stored = self.key_path.read_text()
        self.assertNotIn("ENCRYPTED", stored)
        serialization.load_pem_private_key(stored.encode(), password=None)

    def test_rejects_a_key_that_does_not_belong_to_the_certificate(self) -> None:
        pki = _Pki()
        with self.assertApiError("certificate_key_mismatch"):
            tls_certificate_service.install_certificate(pki.chain_pem, _key_pem(pki.intermediate_key))

    def test_rejects_an_expired_certificate(self) -> None:
        now = datetime.now(timezone.utc)
        pki = _Pki(not_before=now - timedelta(days=30), not_after=now - timedelta(days=1))
        with self.assertApiError("certificate_expired"):
            tls_certificate_service.install_certificate(pki.chain_pem, _key_pem(pki.leaf_key))

    def test_rejects_a_certificate_that_is_not_valid_yet(self) -> None:
        now = datetime.now(timezone.utc)
        pki = _Pki(not_before=now + timedelta(days=2), not_after=now + timedelta(days=30))
        with self.assertApiError("certificate_not_yet_valid"):
            tls_certificate_service.install_certificate(pki.chain_pem, _key_pem(pki.leaf_key))

    def test_rejects_a_chain_in_the_wrong_order(self) -> None:
        pki = _Pki()
        with self.assertApiError("certificate_key_mismatch"):
            tls_certificate_service.install_certificate(
                _pem(pki.intermediate) + _pem(pki.leaf), _key_pem(pki.leaf_key)
            )
        with self.assertApiError("certificate_chain_invalid"):
            tls_certificate_service.install_certificate(
                _pem(pki.leaf) + _pem(pki.root), _key_pem(pki.leaf_key)
            )

    def test_rejects_a_certificate_not_issued_for_servers(self) -> None:
        pki = _Pki(usages=[ExtendedKeyUsageOID.CLIENT_AUTH])
        with self.assertApiError("certificate_not_for_servers"):
            tls_certificate_service.install_certificate(pki.chain_pem, _key_pem(pki.leaf_key))

    def test_rejects_weak_and_unsupported_keys(self) -> None:
        pki = _Pki()
        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        with self.assertApiError("private_key_too_weak"):
            tls_certificate_service.install_certificate(pki.chain_pem, _key_pem(weak))
        odd_curve = ec.generate_private_key(ec.SECP256K1())
        with self.assertApiError("private_key_unsupported"):
            tls_certificate_service.install_certificate(pki.chain_pem, _key_pem(odd_curve))

    def test_names_swapped_and_missing_fields(self) -> None:
        pki = _Pki()
        with self.assertApiError("certificate_contains_private_key"):
            tls_certificate_service.install_certificate(_key_pem(pki.leaf_key), pki.chain_pem)
        with self.assertApiError("certificate_required"):
            tls_certificate_service.install_certificate(None, _key_pem(pki.leaf_key))
        with self.assertApiError("private_key_not_pem") as _:
            tls_certificate_service.install_certificate(pki.chain_pem, pki.chain_pem)
        with self.assertApiError("certificate_invalid"):
            tls_certificate_service.install_certificate(
                "-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----", _key_pem(pki.leaf_key)
            )

    def test_a_failed_reload_restores_the_previous_certificate(self) -> None:
        pki = _Pki()
        before = (self.cert_path.read_bytes(), self.key_path.read_bytes())

        with patch.object(self.context, "load_cert_chain", side_effect=ssl.SSLError("boom")):
            with self.assertRaises(ApiError) as caught:
                tls_certificate_service.install_certificate(pki.chain_pem, _key_pem(pki.leaf_key))

        self.assertEqual(caught.exception.code, "tls_certificate_reload_failed")
        self.assertEqual((self.cert_path.read_bytes(), self.key_path.read_bytes()), before)
        self.assertIsNone(tls_bootstrap.read_administrator_marker(self.cert_path))

    def test_without_a_live_server_the_files_change_for_the_next_start(self) -> None:
        tls_certificate_service.register_live_tls(self.cert_path, self.key_path, None)
        pki = _Pki()

        result = tls_certificate_service.install_certificate(pki.chain_pem, _key_pem(pki.leaf_key))

        self.assertFalse(result["reloaded"])
        self.assertFalse(result["live_reload"])
        self.assertEqual(x509.load_pem_x509_certificates(self.cert_path.read_bytes())[0], pki.leaf)


class RevertCertificateTests(_TlsDirTestCase):
    def test_reverts_to_a_fresh_self_signed_certificate_and_key(self) -> None:
        pki = _Pki()
        tls_certificate_service.install_certificate(pki.chain_pem, _key_pem(pki.leaf_key))
        administrator_key = self.key_path.read_bytes()

        result = tls_certificate_service.revert_to_generated_certificate(requested_by="admin")

        self.assertTrue(result["reloaded"])
        self.assertEqual(result["source"], "generated")
        self.assertIsNone(tls_bootstrap.read_administrator_marker(self.cert_path))
        self.assertNotEqual(self.key_path.read_bytes(), administrator_key)
        # The administrator's key was EC and the generated one is RSA: a context
        # that merely had the new pair loaded into it would still hand the EC
        # certificate to every client that prefers ECDSA.
        for version in (ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_3):
            served = _served_certificate(self.context, version)
            self.assertTrue(tls_bootstrap.is_appliance_generated(served), version)

    def test_a_failed_revert_is_logged_and_keeps_the_administrator_certificate(self) -> None:
        pki = _Pki()
        tls_certificate_service.install_certificate(pki.chain_pem, _key_pem(pki.leaf_key))
        before = (self.cert_path.read_bytes(), self.key_path.read_bytes())

        with (
            patch.object(tls_bootstrap, "ensure_self_signed_cert", side_effect=OSError("Read-only file system")),
            self.assertLogs(tls_certificate_service.logger, "ERROR") as logs,
            self.assertRaises(ApiError) as caught,
        ):
            tls_certificate_service.revert_to_generated_certificate(requested_by="admin")

        self.assertEqual(caught.exception.code, "tls_certificate_write_failed")
        self.assertIn("Read-only file system", logs.output[0])
        self.assertEqual((self.cert_path.read_bytes(), self.key_path.read_bytes()), before)
        self.assertIsNotNone(tls_bootstrap.read_administrator_marker(self.cert_path))

    def test_reverting_the_generated_certificate_changes_nothing(self) -> None:
        before = (self.cert_path.read_bytes(), self.key_path.read_bytes())

        result = tls_certificate_service.revert_to_generated_certificate()

        self.assertFalse(result["reloaded"])
        self.assertEqual((self.cert_path.read_bytes(), self.key_path.read_bytes()), before)


class TlsCertificateApiTests(unittest.TestCase):
    def setUp(self) -> None:
        USERS.clear()
        SESSIONS.clear()
        auth_service.reset_login_lockout_state_for_testing()
        env = patch.dict("os.environ", BOOTSTRAP_ENV)
        env.start()
        self.addCleanup(env.stop)
        client_cm = TestClient(app)
        self.client = client_cm.__enter__()
        self.addCleanup(client_cm.__exit__, None, None, None)
        login = self.client.post("/api/v1/auth/login", json={"name": "admin", "password": "Sup3rSecretPW"})
        self.headers = {"Authorization": f"Bearer {login.json()['token']}"}

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cert_path, self.key_path = Path(tmp.name) / "server.crt", Path(tmp.name) / "server.key"
        ensure_self_signed_cert(self.cert_path, self.key_path)
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(str(self.cert_path), str(self.key_path))
        tls_certificate_service.register_live_tls(self.cert_path, self.key_path, self.context)
        self.addCleanup(tls_certificate_service.clear_live_tls)
        self.addCleanup(USERS.clear)
        self.addCleanup(SESSIONS.clear)

    def test_requires_a_session(self) -> None:
        self.assertEqual(self.client.get("/api/v1/system/tls-certificate").status_code, 401)
        self.assertEqual(self.client.put("/api/v1/system/tls-certificate", json={}).status_code, 401)
        self.assertEqual(self.client.delete("/api/v1/system/tls-certificate").status_code, 401)

    def test_get_put_delete_round_trip(self) -> None:
        current = self.client.get("/api/v1/system/tls-certificate", headers=self.headers)
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.json()["source"], "generated")

        pki = _Pki()
        installed = self.client.put(
            "/api/v1/system/tls-certificate",
            headers=self.headers,
            json={"certificate": pki.chain_pem, "private_key": _key_pem(pki.leaf_key)},
        )
        self.assertEqual(installed.status_code, 200, installed.text)
        body = installed.json()
        self.assertEqual(body["source"], "administrator")
        self.assertEqual(body["installed_by"], "admin")
        self.assertTrue(body["reloaded"])
        self.assertNotIn("private_key", body)

        reverted = self.client.delete("/api/v1/system/tls-certificate", headers=self.headers)
        self.assertEqual(reverted.status_code, 200)
        self.assertEqual(reverted.json()["source"], "generated")

    def test_invalid_input_answers_422_with_the_offending_field(self) -> None:
        pki = _Pki()
        response = self.client.put(
            "/api/v1/system/tls-certificate",
            headers=self.headers,
            json={"certificate": pki.chain_pem, "private_key": _key_pem(pki.root_key)},
        )
        self.assertEqual(response.status_code, 422)
        error = response.json()["error"]
        self.assertEqual(error["code"], "certificate_key_mismatch")
        self.assertEqual(error["details"]["field"], "private_key")

    def test_openapi_documents_the_endpoints(self) -> None:
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertEqual(set(paths["/api/v1/system/tls-certificate"]), {"get", "put", "delete"})


if __name__ == "__main__":
    unittest.main()
