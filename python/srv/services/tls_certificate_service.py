# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""The HTTPS certificate the appliance serves the console and API with.

Out of the box that is the self-signed certificate tls_bootstrap generates and
renews. An administrator can replace it with their own -- CA-issued or
self-signed -- through the API; it is validated here before anything on disk is
touched, written over the same certificate/key files the server started with,
and loaded into the running TLS context so new connections get it without a
restart. A marker next to the certificate records that it was installed on
purpose, which is what keeps the renewal check from ever regenerating over it.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import socket
import ssl
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from python.srv.core import tls_bootstrap
from python.srv.core.tls_renewal import load_into_live_context
from python.srv.core.server_defaults import env_int
from python.srv.core.utils import utc_now
from python.srv.services.api_errors import api_error

logger = logging.getLogger(__name__)

SOURCE_GENERATED = "generated"
SOURCE_ADMINISTRATOR = "administrator"

MIN_RSA_KEY_BITS = 2048
# Browsers only accept these curves for a server certificate.
SUPPORTED_EC_CURVES = frozenset({"secp256r1", "secp384r1", "secp521r1"})
MAX_CHAIN_CERTIFICATES = 10
# A certificate dated a few minutes into the future is clock skew between the
# CA and this host, not a certificate that is genuinely not valid yet.
_NOT_BEFORE_TOLERANCE = timedelta(minutes=5)


@dataclass
class _LiveTls:
    cert_path: Path
    key_path: Path
    ssl_context: ssl.SSLContext | None


_LIVE_TLS: _LiveTls | None = None


def register_live_tls(cert_path: Path, key_path: Path, ssl_context: ssl.SSLContext | None) -> None:
    global _LIVE_TLS
    _LIVE_TLS = _LiveTls(cert_path=cert_path, key_path=key_path, ssl_context=ssl_context)


def clear_live_tls() -> None:
    global _LIVE_TLS
    _LIVE_TLS = None


def _active_tls() -> _LiveTls:
    if _LIVE_TLS is not None:
        return _LIVE_TLS
    # Not running under the HTTPS entry point (bare uvicorn, tests): the files
    # are still the ones the next HTTPS start will serve, there is just no live
    # context to reload them into.
    cert_path, key_path = tls_bootstrap.default_cert_paths()
    return _LiveTls(cert_path=cert_path, key_path=key_path, ssl_context=None)


# --------------------------------------------------------------------- read --


def _fingerprint(cert: x509.Certificate) -> str:
    digest = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def _key_description(public_key: Any) -> str:
    if isinstance(public_key, rsa.RSAPublicKey):
        return f"RSA {public_key.key_size}"
    if isinstance(public_key, ec.EllipticCurvePublicKey):
        return f"EC {public_key.curve.name}"
    return type(public_key).__name__


def _common_name(name: x509.Name) -> str | None:
    attributes = name.get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(attributes[0].value) if attributes else None


def _subject_alt_names(cert: x509.Certificate) -> tuple[list[str], list[str]]:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return [], []
    return (
        [str(name) for name in san.get_values_for_type(x509.DNSName)],
        [str(ip) for ip in san.get_values_for_type(x509.IPAddress)],
    )


def _is_self_signed(cert: x509.Certificate) -> bool:
    if cert.issuer != cert.subject:
        return False
    try:
        cert.verify_directly_issued_by(cert)
    except (ValueError, TypeError, InvalidSignature, UnsupportedAlgorithm):
        return False
    return True


def _dns_name_matches(pattern: str, host: str) -> bool:
    pattern, host = pattern.lower().rstrip("."), host.lower().rstrip(".")
    if pattern.startswith("*."):
        head, _, tail = host.partition(".")
        return bool(head) and tail == pattern[2:]
    return pattern == host


def _appliance_names() -> list[str]:
    names: set[str] = set()
    try:
        for name in (socket.gethostname(), socket.getfqdn()):
            if name and name != "localhost":
                names.add(name)
    except OSError:
        pass
    names.update(tls_bootstrap._detect_local_ips())
    return sorted(names)


def _covers(dns_names: list[str], ip_addresses: list[str], name: str) -> bool:
    try:
        wanted = ipaddress.ip_address(name)
    except ValueError:
        return any(_dns_name_matches(pattern, name) for pattern in dns_names)
    return any(ipaddress.ip_address(ip) == wanted for ip in ip_addresses)


def _warnings(dns_names: list[str], ip_addresses: list[str], days_remaining: int) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if days_remaining < 0:
        warnings.append({"code": "expired", "message": "The certificate has expired; browsers refuse it."})
    elif days_remaining <= env_int("ABR_TLS_RENEWAL_THRESHOLD_DAYS", minimum=0):
        warnings.append(
            {
                "code": "expires_soon",
                "message": f"The certificate expires in {days_remaining} day(s).",
                "days_remaining": days_remaining,
            }
        )

    if not dns_names and not ip_addresses:
        warnings.append(
            {
                "code": "no_subject_alternative_names",
                "message": (
                    "The certificate lists no subject alternative names. Browsers ignore the "
                    "common name, so they will not accept it for any address."
                ),
            }
        )
        return warnings

    names = _appliance_names()
    if names and not any(_covers(dns_names, ip_addresses, name) for name in names):
        warnings.append(
            {
                "code": "appliance_names_not_covered",
                "message": (
                    "None of this appliance's own host names or addresses are in the certificate. "
                    "That is fine if the console is reached through a DNS name the certificate "
                    "lists; otherwise browsers will show a name-mismatch warning."
                ),
                "appliance_names": names,
            }
        )
    return warnings


def _describe(tls: _LiveTls) -> dict[str, Any]:
    try:
        chain = x509.load_pem_x509_certificates(tls.cert_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise api_error(
            503,
            "tls_certificate_unreadable",
            f"The TLS certificate at {tls.cert_path} could not be read: {exc}",
            path=str(tls.cert_path),
        ) from exc

    leaf = chain[0]
    marker = tls_bootstrap.read_administrator_marker(tls.cert_path)
    generated = marker is None and tls_bootstrap.is_appliance_generated(leaf)
    not_after = leaf.not_valid_after_utc
    days_remaining = (not_after - datetime.now(timezone.utc)).days
    dns_names, ip_addresses = _subject_alt_names(leaf)
    return {
        "source": SOURCE_GENERATED if generated else SOURCE_ADMINISTRATOR,
        "auto_renew": generated,
        "subject": leaf.subject.rfc4514_string(),
        "common_name": _common_name(leaf.subject),
        "issuer": leaf.issuer.rfc4514_string(),
        "issuer_common_name": _common_name(leaf.issuer),
        "self_signed": _is_self_signed(leaf),
        "dns_names": dns_names,
        "ip_addresses": ip_addresses,
        "serial_number": format(leaf.serial_number, "X"),
        "fingerprint_sha256": _fingerprint(leaf),
        "key_type": _key_description(leaf.public_key()),
        "not_before": leaf.not_valid_before_utc.isoformat(),
        "not_after": not_after.isoformat(),
        "days_remaining": days_remaining,
        "expired": days_remaining < 0,
        "chain_length": len(chain),
        "installed_at": (marker or {}).get("installed_at"),
        "installed_by": (marker or {}).get("installed_by"),
        "live_reload": tls.ssl_context is not None,
        "warnings": _warnings(dns_names, ip_addresses, days_remaining),
    }


def get_certificate() -> dict[str, Any]:
    with tls_bootstrap.TLS_FILES_LOCK:
        return _describe(_active_tls())


# ------------------------------------------------------------------ install --


def _invalid(code: str, message: str, **details: Any):
    return api_error(422, code, message, **details)


# Generous for a leaf plus intermediates (a few KB each); the cap only exists so
# a wrong file picked in the browser is refused rather than parsed.
MAX_PEM_CHARS = 100_000


def _pem_text(value: Any, field: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if len(text) > MAX_PEM_CHARS:
        raise _invalid(
            f"{field}_too_large",
            f"The {field.replace('_', ' ')} is {len(text)} characters; at most {MAX_PEM_CHARS} are accepted.",
            field=field,
        )
    return text


def _parse_chain(certificate_pem: Any) -> list[x509.Certificate]:
    text = _pem_text(certificate_pem, "certificate")
    if not text:
        raise _invalid("certificate_required", "Provide the certificate in PEM format.", field="certificate")
    if "PRIVATE KEY-----" in text:
        raise _invalid(
            "certificate_contains_private_key",
            "The certificate field contains a private key. Paste the private key into its own field.",
            field="certificate",
        )
    if "-----BEGIN CERTIFICATE-----" not in text:
        raise _invalid(
            "certificate_not_pem",
            "The certificate is not in PEM format; expected a '-----BEGIN CERTIFICATE-----' block.",
            field="certificate",
        )
    try:
        chain = x509.load_pem_x509_certificates(text.encode("utf-8"))
    except ValueError as exc:
        raise _invalid(
            "certificate_invalid", f"The certificate could not be parsed: {exc}", field="certificate"
        ) from exc
    if len(chain) > MAX_CHAIN_CERTIFICATES:
        raise _invalid(
            "certificate_chain_too_long",
            f"The certificate chain has {len(chain)} certificates; at most {MAX_CHAIN_CERTIFICATES} are accepted.",
            field="certificate",
        )
    return chain


def _parse_private_key(private_key_pem: Any, passphrase: Any) -> Any:
    text = _pem_text(private_key_pem, "private_key")
    if not text:
        raise _invalid("private_key_required", "Provide the private key in PEM format.", field="private_key")
    if "PRIVATE KEY-----" not in text:
        hint = (
            " It looks like a certificate -- paste that into the certificate field."
            if "-----BEGIN CERTIFICATE-----" in text
            else ""
        )
        raise _invalid(
            "private_key_not_pem",
            "The private key is not in PEM format; expected a '-----BEGIN ... PRIVATE KEY-----' block." + hint,
            field="private_key",
        )

    data = text.encode("utf-8")
    password = passphrase.encode("utf-8") if isinstance(passphrase, str) and passphrase else None
    try:
        key = serialization.load_pem_private_key(data, password=password)
    except TypeError as exc:
        if password is None:
            raise _invalid(
                "private_key_passphrase_required",
                "The private key is encrypted. Enter its passphrase.",
                field="passphrase",
            ) from exc
        # A passphrase for a key that has none: harmless, the key is usable.
        key = serialization.load_pem_private_key(data, password=None)
    except ValueError as exc:
        if password is not None and "ENCRYPTED" in text:
            raise _invalid(
                "private_key_passphrase_incorrect",
                "The private key could not be decrypted with that passphrase.",
                field="passphrase",
            ) from exc
        raise _invalid(
            "private_key_invalid", "The private key could not be parsed.", field="private_key"
        ) from exc
    except UnsupportedAlgorithm as exc:
        raise _invalid(
            "private_key_unsupported", f"The private key uses an unsupported algorithm: {exc}", field="private_key"
        ) from exc

    if isinstance(key, rsa.RSAPrivateKey):
        if key.key_size < MIN_RSA_KEY_BITS:
            raise _invalid(
                "private_key_too_weak",
                f"RSA keys must be at least {MIN_RSA_KEY_BITS} bits; this one is {key.key_size}.",
                field="private_key",
            )
    elif isinstance(key, ec.EllipticCurvePrivateKey):
        if key.curve.name not in SUPPORTED_EC_CURVES:
            raise _invalid(
                "private_key_unsupported",
                f"EC keys must use P-256, P-384 or P-521; this one uses {key.curve.name}.",
                field="private_key",
            )
    else:
        raise _invalid(
            "private_key_unsupported",
            "Only RSA and EC (P-256, P-384, P-521) private keys are supported.",
            field="private_key",
        )
    return key


def _spki(public_key: Any) -> bytes:
    return public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _validate_pair(chain: list[x509.Certificate], key: Any) -> None:
    leaf = chain[0]
    if _spki(leaf.public_key()) != _spki(key.public_key()):
        raise _invalid(
            "certificate_key_mismatch",
            "The private key does not belong to the certificate. If the file holds a chain, "
            "the server certificate has to come first.",
            field="private_key",
        )

    now = datetime.now(timezone.utc)
    if leaf.not_valid_after_utc <= now:
        raise _invalid(
            "certificate_expired",
            f"The certificate expired on {leaf.not_valid_after_utc.isoformat()}.",
            field="certificate",
            not_after=leaf.not_valid_after_utc.isoformat(),
        )
    if leaf.not_valid_before_utc > now + _NOT_BEFORE_TOLERANCE:
        raise _invalid(
            "certificate_not_yet_valid",
            f"The certificate is not valid until {leaf.not_valid_before_utc.isoformat()}. "
            "Check this appliance's clock if that looks wrong.",
            field="certificate",
            not_before=leaf.not_valid_before_utc.isoformat(),
        )

    try:
        usages = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound:
        usages = None
    if usages is not None and not (
        ExtendedKeyUsageOID.SERVER_AUTH in usages or ExtendedKeyUsageOID.ANY_EXTENDED_KEY_USAGE in usages
    ):
        raise _invalid(
            "certificate_not_for_servers",
            "The certificate is not issued for TLS servers (its extended key usage lacks serverAuth).",
            field="certificate",
        )

    # An absent keyUsage leaves the key unrestricted, which browsers accept; a
    # present one that allows neither signing nor key transport cannot carry a
    # TLS handshake, and they refuse it outright.
    try:
        key_usage = leaf.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        key_usage = None
    if key_usage is not None and not (key_usage.digital_signature or key_usage.key_encipherment):
        raise _invalid(
            "certificate_not_for_servers",
            "The certificate cannot be used by a TLS server: its key usage allows neither "
            "digitalSignature nor keyEncipherment. A certificate meant for signing other "
            "certificates cannot serve the web console.",
            field="certificate",
        )

    for position, certificate in enumerate(chain, start=1):
        if certificate.not_valid_after_utc <= now:
            raise _invalid(
                "certificate_chain_expired",
                f"Certificate {position} in the file expired on "
                f"{certificate.not_valid_after_utc.isoformat()}. Every certificate in the chain has "
                "to be current, or browsers refuse the whole chain.",
                field="certificate",
                position=position,
                not_after=certificate.not_valid_after_utc.isoformat(),
            )
        if certificate.not_valid_before_utc > now + _NOT_BEFORE_TOLERANCE:
            raise _invalid(
                "certificate_chain_not_yet_valid",
                f"Certificate {position} in the file is not valid until "
                f"{certificate.not_valid_before_utc.isoformat()}.",
                field="certificate",
                position=position,
                not_before=certificate.not_valid_before_utc.isoformat(),
            )

    for position, issuer in enumerate(chain[1:], start=2):
        try:
            constraints = issuer.extensions.get_extension_for_class(x509.BasicConstraints).value
            is_ca = bool(constraints.ca)
        except x509.ExtensionNotFound:
            is_ca = False
        if not is_ca:
            raise _invalid(
                "certificate_issuer_not_a_ca",
                f"Certificate {position} in the file signs certificate {position - 1} but is not a "
                "certificate authority (its basic constraints say CA:FALSE). Browsers refuse a chain "
                "built on it.",
                field="certificate",
                position=position,
            )

    for position, (child, parent) in enumerate(zip(chain, chain[1:]), start=1):
        try:
            child.verify_directly_issued_by(parent)
        except (ValueError, TypeError, InvalidSignature, UnsupportedAlgorithm) as exc:
            raise _invalid(
                "certificate_chain_invalid",
                f"Certificate {position} in the file is not issued by certificate {position + 1}. "
                "List the server certificate first, then each intermediate in order.",
                field="certificate",
                position=position,
            ) from exc


def _check_tls_accepts(cert_bytes: bytes, key_bytes: bytes) -> None:
    # The exact load the server performs, against a throwaway context, so a
    # pair OpenSSL refuses (e.g. under its security level) fails the request
    # instead of the reload.
    with tempfile.TemporaryDirectory(prefix="abr-tls-check-") as tmp:
        cert_file, key_file = Path(tmp) / "server.crt", Path(tmp) / "server.key"
        cert_file.write_bytes(cert_bytes)
        key_file.write_bytes(key_bytes)
        key_file.chmod(0o600)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            context.load_cert_chain(str(cert_file), str(key_file))
        except ssl.SSLError as exc:
            raise _invalid(
                "certificate_rejected_by_tls",
                f"The TLS library refused this certificate and key: {exc.reason or exc}",
                field="certificate",
            ) from exc


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


@dataclass
class _Snapshot:
    cert: bytes | None
    key: bytes | None
    marker: bytes | None


def _read_optional(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _snapshot(tls: _LiveTls) -> _Snapshot:
    return _Snapshot(
        cert=_read_optional(tls.cert_path),
        key=_read_optional(tls.key_path),
        marker=_read_optional(tls_bootstrap.administrator_marker_path(tls.cert_path)),
    )


def _restore(tls: _LiveTls, snapshot: _Snapshot) -> None:
    marker_path = tls_bootstrap.administrator_marker_path(tls.cert_path)
    for path, data, mode in (
        (tls.key_path, snapshot.key, 0o600),
        (tls.cert_path, snapshot.cert, 0o644),
        (marker_path, snapshot.marker, 0o644),
    ):
        if data is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write(path, data, mode)


def _reload(tls: _LiveTls, snapshot: _Snapshot) -> bool:
    if tls.ssl_context is None:
        return False
    try:
        load_into_live_context(tls.ssl_context, tls.cert_path, tls.key_path)
    except (OSError, ssl.SSLError) as exc:
        logger.exception("Loading the new TLS certificate into the running server failed; restoring the previous one.")
        _restore(tls, snapshot)
        raise api_error(
            500,
            "tls_certificate_reload_failed",
            f"The certificate was valid but the running server could not load it ({exc}). "
            "The previous certificate is still in use.",
        ) from exc
    return True


def install_certificate(
    certificate_pem: Any,
    private_key_pem: Any,
    passphrase: Any = None,
    *,
    installed_by: str | None = None,
) -> dict[str, Any]:
    chain = _parse_chain(certificate_pem)
    key = _parse_private_key(private_key_pem, passphrase)
    _validate_pair(chain, key)

    cert_bytes = b"".join(cert.public_bytes(serialization.Encoding.PEM) for cert in chain)
    # Stored unencrypted, like the generated key: the server has to load it
    # unattended at every start. The 0600 mode is what protects it.
    key_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _check_tls_accepts(cert_bytes, key_bytes)

    with tls_bootstrap.TLS_FILES_LOCK:
        tls = _active_tls()
        snapshot = _snapshot(tls)
        try:
            _atomic_write(tls.key_path, key_bytes, 0o600)
            _atomic_write(tls.cert_path, cert_bytes, 0o644)
            tls_bootstrap.write_administrator_marker(
                tls.cert_path, {"installed_at": utc_now(), "installed_by": installed_by}
            )
        except OSError as exc:
            _restore(tls, snapshot)
            raise api_error(
                500,
                "tls_certificate_write_failed",
                f"The certificate could not be saved to {tls.cert_path.parent}: {exc}",
                path=str(tls.cert_path.parent),
            ) from exc
        reloaded = _reload(tls, snapshot)
        result = _describe(tls)

    logger.warning(
        "TLS certificate replaced by %s: subject %s, issuer %s, SHA-256 %s, valid until %s (%s).",
        installed_by or "an unknown user",
        result["subject"],
        result["issuer"],
        result["fingerprint_sha256"],
        result["not_after"],
        "loaded into the running server" if reloaded else "used from the next start",
    )
    return {**result, "reloaded": reloaded}


def revert_to_generated_certificate(*, requested_by: str | None = None) -> dict[str, Any]:
    with tls_bootstrap.TLS_FILES_LOCK:
        tls = _active_tls()
        current = _describe(tls)
        if current["source"] == SOURCE_GENERATED:
            return {**current, "reloaded": False}

        snapshot = _snapshot(tls)
        try:
            # A fresh key as well: the administrator's belongs to their
            # certificate and should not outlive it on this appliance.
            tls_bootstrap.administrator_marker_path(tls.cert_path).unlink(missing_ok=True)
            tls.cert_path.unlink(missing_ok=True)
            tls.key_path.unlink(missing_ok=True)
            tls_bootstrap.ensure_self_signed_cert(tls.cert_path, tls.key_path)
        except OSError as exc:
            _restore(tls, snapshot)
            raise api_error(
                500,
                "tls_certificate_write_failed",
                f"A self-signed certificate could not be generated in {tls.cert_path.parent}: {exc}",
                path=str(tls.cert_path.parent),
            ) from exc
        reloaded = _reload(tls, snapshot)
        result = _describe(tls)

    logger.warning(
        "TLS certificate reverted to the appliance's self-signed certificate by %s (SHA-256 %s, %s).",
        requested_by or "an unknown user",
        result["fingerprint_sha256"],
        "loaded into the running server" if reloaded else "used from the next start",
    )
    return {**result, "reloaded": reloaded}
