# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .tls_bootstrap import _dev_tls_dir, _dir_is_writable

logger = logging.getLogger(__name__)

ENCRYPTED_PREFIX = "abr-enc:v1:"
KEY_FILE_ENV = "ABR_CREDENTIAL_KEY_FILE"
KEY_FILE_NAME = "credentials.key"
KEY_REPAIR_HINT = "Put back the key file this server created, readable by the server's user, and restart the server"

_CIPHER: Fernet | None = None
_CIPHER_LOCK = threading.Lock()


def credential_key_path() -> Path:
    dev_path = _dev_tls_dir() / KEY_FILE_NAME
    configured = os.environ.get(KEY_FILE_ENV, "").strip()
    if not configured:
        return dev_path
    path = Path(configured).expanduser()
    if path.is_file() or _dir_is_writable(path.parent):
        return path
    logger.warning(
        "Credential key directory %s is not usable (not writable); using %s instead. "
        "Expected in a dev checkout; a real install should have created it with the right ownership.",
        path.parent,
        dev_path,
    )
    return dev_path


class CredentialKeyUnavailable(RuntimeError):
    pass


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new_key(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(Fernet.generate_key())
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return
        except OSError:
            if os.path.lexists(path):
                return
            os.rename(temporary, path)
        _fsync_directory(path.parent)
        logger.info("Created the credential encryption key at %s.", path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_or_create_key(path: Path) -> Fernet:
    try:
        if not os.path.lexists(path):
            _write_new_key(path)
        content = path.read_bytes().strip()
    except OSError as exc:
        reason = (exc.strerror or str(exc)).lower()
        raise CredentialKeyUnavailable(f"The credential key at {path} cannot be used: {reason}") from exc
    if not content:
        raise CredentialKeyUnavailable(f"The credential key at {path} cannot be used: the file is empty")
    try:
        return Fernet(content)
    except ValueError as exc:
        raise CredentialKeyUnavailable(
            f"The credential key at {path} cannot be used: the file does not hold a valid key"
        ) from exc


def _cipher() -> Fernet:
    global _CIPHER
    with _CIPHER_LOCK:
        if _CIPHER is None:
            _CIPHER = _read_or_create_key(credential_key_path())
        return _CIPHER


def credential_key_problem() -> str | None:
    try:
        _cipher()
    except CredentialKeyUnavailable as exc:
        return str(exc)
    return None


def reset_cipher() -> None:
    global _CIPHER
    with _CIPHER_LOCK:
        _CIPHER = None


def is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(ENCRYPTED_PREFIX)


def encrypt_secret(value: Any) -> Any:
    if not isinstance(value, str) or not value or is_encrypted(value):
        return value
    return ENCRYPTED_PREFIX + _cipher().encrypt(value.encode("utf-8")).decode("ascii")


class StoredCredentialUnreadable(RuntimeError):
    pass


def decrypt_secret(value: Any) -> Any:
    if not is_encrypted(value):
        return value
    try:
        return _cipher().decrypt(value[len(ENCRYPTED_PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, CredentialKeyUnavailable):
        return value


def unreadable_credential_message(subject: str) -> str:
    problem = credential_key_problem()
    if problem:
        return (
            f"The stored password {subject} cannot be decrypted. {problem}. {KEY_REPAIR_HINT}, or move it "
            "away and enter the password again with abr inventory edit --name <inventory> --password."
        )
    return (
        f"The stored password {subject} cannot be decrypted with this server's credential key, so it is not "
        "tried. Enter the password again with abr inventory edit --name <inventory> --password."
    )


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_token_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
