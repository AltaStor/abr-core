# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import datetime as dt
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from python.srv.services import tls_certificate_service as svc
from python.srv.services.api_errors import ApiError

NOW = dt.datetime.now(dt.timezone.utc)


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _usage(**overrides) -> x509.KeyUsage:
    fields = {
        "digital_signature": False,
        "content_commitment": False,
        "key_encipherment": False,
        "data_encipherment": False,
        "key_agreement": False,
        "key_cert_sign": False,
        "crl_sign": False,
        "encipher_only": False,
        "decipher_only": False,
    }
    fields.update(overrides)
    return x509.KeyUsage(**fields)


def _mint(
    name: str,
    key,
    *,
    issuer_name: str | None = None,
    issuer_key=None,
    ca: bool | None = False,
    key_usage: x509.KeyUsage | None = None,
    server_auth: bool = True,
    not_before: dt.datetime | None = None,
    not_after: dt.datetime | None = None,
) -> x509.Certificate:
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_name or name)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or NOW - dt.timedelta(days=1))
        .not_valid_after(not_after or NOW + dt.timedelta(days=365))
    )
    if ca is not None:
        builder = builder.add_extension(
            x509.BasicConstraints(ca=ca, path_length=None), critical=True
        )
    if key_usage is not None:
        builder = builder.add_extension(key_usage, critical=True)
    if server_auth:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
    return builder.sign(issuer_key or key, hashes.SHA256())


def _leaf_usage() -> x509.KeyUsage:
    return _usage(digital_signature=True, key_encipherment=True)


def _ca_usage() -> x509.KeyUsage:
    return _usage(key_cert_sign=True, crl_sign=True)


class ChainValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ca_key = _key()
        self.leaf_key = _key()

    def _ca(self, **overrides) -> x509.Certificate:
        options = {"ca": True, "key_usage": _ca_usage(), "server_auth": False}
        options.update(overrides)
        return _mint("Issuing CA", self.ca_key, **options)

    def _leaf(self, **overrides) -> x509.Certificate:
        options = {
            "issuer_name": "Issuing CA",
            "issuer_key": self.ca_key,
            "ca": False,
            "key_usage": _leaf_usage(),
        }
        options.update(overrides)
        return _mint("appliance.local", self.leaf_key, **options)

    def _refusal(self, chain: list[x509.Certificate]) -> ApiError:
        with self.assertRaises(ApiError) as caught:
            svc._validate_pair(chain, self.leaf_key)
        return caught.exception

    def test_a_leaf_with_its_valid_intermediate_is_accepted(self) -> None:
        svc._validate_pair([self._leaf(), self._ca()], self.leaf_key)

    def test_a_leaf_without_a_key_usage_extension_is_accepted(self) -> None:
        svc._validate_pair([self._leaf(key_usage=None), self._ca()], self.leaf_key)

    def test_a_leaf_that_only_signs_is_accepted(self) -> None:
        usage = _usage(digital_signature=True)
        svc._validate_pair([self._leaf(key_usage=usage), self._ca()], self.leaf_key)

    def test_a_leaf_that_only_enciphers_is_accepted(self) -> None:
        usage = _usage(key_encipherment=True)
        svc._validate_pair([self._leaf(key_usage=usage), self._ca()], self.leaf_key)

    def test_an_expired_intermediate_is_refused(self) -> None:
        expired = self._ca(
            not_before=NOW - dt.timedelta(days=800), not_after=NOW - dt.timedelta(days=10)
        )

        error = self._refusal([self._leaf(), expired])

        self.assertEqual(error.status_code, 422)
        self.assertEqual(error.code, "certificate_chain_expired")
        self.assertEqual(error.details.get("position"), 2)

    def test_an_intermediate_that_is_not_yet_valid_is_refused(self) -> None:
        future = self._ca(
            not_before=NOW + dt.timedelta(days=5), not_after=NOW + dt.timedelta(days=400)
        )

        error = self._refusal([self._leaf(), future])

        self.assertEqual(error.code, "certificate_chain_not_yet_valid")

    def test_an_issuer_that_is_not_a_ca_is_refused(self) -> None:
        not_a_ca = _mint(
            "Issuing CA", self.ca_key, ca=False, key_usage=_leaf_usage(), server_auth=False
        )

        error = self._refusal([self._leaf(), not_a_ca])

        self.assertEqual(error.status_code, 422)
        self.assertEqual(error.code, "certificate_issuer_not_a_ca")
        self.assertEqual(error.details.get("position"), 2)

    def test_an_issuer_with_no_basic_constraints_is_refused(self) -> None:
        bare = _mint("Issuing CA", self.ca_key, ca=None, key_usage=_ca_usage(), server_auth=False)

        error = self._refusal([self._leaf(), bare])

        self.assertEqual(error.code, "certificate_issuer_not_a_ca")

    def test_a_ca_certificate_used_as_the_server_certificate_is_refused(self) -> None:
        self.leaf_key = self.ca_key
        self_signed_ca = _mint("appliance.local", self.ca_key, ca=True, key_usage=_ca_usage())

        error = self._refusal([self_signed_ca])

        self.assertEqual(error.status_code, 422)
        self.assertEqual(error.code, "certificate_not_for_servers")
        self.assertIn("neither digitalSignature nor keyEncipherment", error.message)

    def test_a_leaf_that_may_only_sign_certificates_is_refused(self) -> None:
        error = self._refusal([self._leaf(key_usage=_ca_usage()), self._ca()])

        self.assertEqual(error.code, "certificate_not_for_servers")

    def test_an_expired_leaf_is_still_reported_as_before(self) -> None:
        stale = self._leaf(
            not_before=NOW - dt.timedelta(days=800), not_after=NOW - dt.timedelta(days=10)
        )

        error = self._refusal([stale, self._ca()])

        self.assertEqual(error.code, "certificate_expired")


if __name__ == "__main__":
    unittest.main()
