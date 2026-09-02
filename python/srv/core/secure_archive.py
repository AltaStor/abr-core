# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import gzip
import json
import os
import struct
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

MAGIC = b"ABRX1\x00"
SALT_LEN = 16
NONCE_LEN = 12
KEY_LEN = 32
DEFAULT_ITERATIONS = 600_000


class ArchiveError(Exception):
    """Raised for malformed archives (wrong magic/truncated file)"""


class WrongPasswordError(Exception):
    """Raised when decryption fails auth (wrong password or corrupted file)"""


def _derive_key(password: str, salt: bytes, iterations: int) -> bytes:
    if not password:
        raise ValueError("password must not be empty")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=KEY_LEN, salt=salt, iterations=iterations
    )
    return kdf.derive(password.encode("utf-8"))


def encrypt_payload(
    payload: dict[str, Any],
    password: str,
    *,
    iterations: int = DEFAULT_ITERATIONS,
) -> bytes:
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    key = _derive_key(password, salt, iterations)

    plaintext = gzip.compress(json.dumps(payload).encode("utf-8"))
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, associated_data=MAGIC)

    return b"".join(
        [
            MAGIC,
            salt,
            struct.pack(">I", iterations),
            nonce,
            ciphertext,
        ]
    )


def decrypt_payload(blob: bytes, password: str) -> dict[str, Any]:
    header_len = len(MAGIC) + SALT_LEN + 4 + NONCE_LEN
    if len(blob) < header_len:
        raise ArchiveError("File is too short to be a valid export archive")

    magic = blob[: len(MAGIC)]
    if magic != MAGIC:
        raise ArchiveError("Not valid export archive (bad magic header)")

    offset = len(MAGIC)
    salt = blob[offset : offset + SALT_LEN]

    offset += SALT_LEN
    (iterations,) = struct.unpack(">I", blob[offset : offset + 4])

    offset += 4
    nonce = blob[offset : offset + NONCE_LEN]

    offset += NONCE_LEN
    ciphertext = blob[offset:]

    key = _derive_key(password, salt, iterations)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, associated_data=MAGIC)
    except InvalidTag as exc:
        raise WrongPasswordError(
            "Incorrect password, or the archive is corrupted/has been tampered with"
        ) from exc

    return json.loads(gzip.decompress(plaintext).decode("utf-8"))
