# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import hashlib
import json
import os
import socket
import ssl
import stat
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from .errors import CliStructuredError
from .exit_codes import GENERAL_ERROR
from .token_store import CONFIG_DIR_ENV_VAR


def _config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".altastor-backup"


def _trust_store_path() -> Path:
    return _config_dir() / "trusted_certs.json"


def _load_all() -> dict[str, str]:
    path = _trust_store_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_all(data: dict[str, str]) -> None:
    path = _trust_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _fetch_fingerprint(host: str, port: int, timeout: float = 5.0) -> str:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls_sock:
            der_cert = tls_sock.getpeercert(binary_form=True)
    cert = x509.load_der_x509_certificate(der_cert)
    spki = cert.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(spki).hexdigest()


def ensure_trusted(base_url: str) -> None:
    parts = urlsplit(base_url)
    if parts.scheme != "https" or not parts.hostname:
        return

    host = parts.hostname
    port = parts.port or 443
    key = f"{host}:{port}"

    try:
        fingerprint = _fetch_fingerprint(host, port)
    except OSError:
        return

    known = _load_all()
    stored = known.get(key)
    if stored is None:
        known[key] = fingerprint
        _save_all(known)
        return
    
    if stored != fingerprint:
        raise CliStructuredError(
            code="tls_server_key_changed",
            message=(
                f"The public key presented by {key} does not match the one trusted on a previous "
                f"connection. A routine certificate renewal alone would not cause this (the same key "
                f"is reused across renewals, see python/srv/core/tls_bootstrap.py) -- this means the "
                f"server's private key itself changed, which could be a legitimate key rotation, or "
                f"traffic to it being intercepted. Refusing to connect.\n"
                f"If this change is expected, remove the \"{key}\" entry from {_trust_store_path()} "
                f"to trust the new key."
            ),
            exit_code=GENERAL_ERROR,
        )
