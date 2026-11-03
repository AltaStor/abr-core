# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""Two-factor sign-in, the part every client shares: TOTP secrets (RFC 6238)
and the provisioning data an authenticator app reads, the policy a code is
validated against, and recovery codes.

The Web UI and the CLI reach it through ``auth_service``, whose sign-in,
enrollment and management flows add the password check and the lockout. What
leaves this module is a secret only in ``provisioning``, during an enrollment;
codes and secrets go into no log message and no error.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from typing import Any

import pyotp
import qrcode
from qrcode.image.svg import SvgPathFillImage

from python.srv.core.credential_encryption import is_encrypted
from python.srv.core.mongo_db_wrapper import USERS, persist_user
from python.srv.core.server_defaults import env_int

from .api_errors import api_error

# The settings both Google Authenticator and Microsoft Authenticator read
# reliably: SHA-1, 6 digits, 30-second steps. pyotp's defaults, so the
# provisioning URI names none of them.
CODE_PATTERN = re.compile(r"^[0-9]{6}$")
# A code this many steps older than the window is reported as expired rather
# than as invalid: five minutes, long enough to cover a code read off a phone
# and typed in late.
EXPIRED_LOOKBACK_STEPS = 10

RECOVERY_CODE_COUNT = 10
# Base 32 without the look-alikes I, O, 0 and 1: 16 characters are 80 bits,
# enough for the plain digest they are kept as.
RECOVERY_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
RECOVERY_CODE_LENGTH = 16

# Refusals of a code, as opposed to a code that could not be checked: each
# counts against the sign-in lockout.
REFUSALS = frozenset({"invalid_totp_code", "totp_code_expired", "totp_code_used", "invalid_recovery_code"})

# Explicit empty values, not removed keys: Database.upsert only sets what a
# record holds, so a dropped key would come back with the next restart.
CLEARED: dict[str, Any] = {
    "totp_enabled": False,
    "totp_secret": None,
    "totp_pending_secret": None,
    "totp_last_step": None,
    "totp_recovery": [],
}

_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def valid_window() -> int:
    """How many 30-second steps either side of the server's time a code is
    accepted, for a phone whose clock is a little off."""
    return env_int("ABR_TOTP_VALID_WINDOW", minimum=0)


def issuer() -> str:
    # A colon ends the issuer in the label an authenticator app reads.
    return os.environ["ABR_TOTP_ISSUER"].replace(":", " ").strip()


def new_secret() -> str:
    """160 random bits, the size RFC 4226 recommends, in base 32."""
    return pyotp.random_base32()


def provisioning(secret: str, account: str) -> dict[str, str]:
    """What an authenticator app needs: the otpauth:// URI, the same as a QR
    code (an SVG image in a data: URI), and the secret as text to type in."""
    uri = pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=issuer())
    # The fill factory paints a white background, so the code stays scannable
    # on a dark page, and needs no imaging library.
    image = qrcode.make(uri, image_factory=SvgPathFillImage)
    buffer = io.BytesIO()
    image.save(buffer)
    qr_code = "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    return {"secret": secret, "otpauth_uri": uri, "qr_code": qr_code}


def _refused(status: int, code: str) -> Exception:
    messages = {
        "invalid_totp_code": "That code isn't valid. Enter the current 6-digit code from your authenticator app.",
        "totp_code_expired": (
            "That code has expired. Enter the current code from your authenticator app; if this keeps happening, "
            "check that the clocks on the phone and on the server are right."
        ),
        "totp_code_used": "That code was already used. Wait for the next code from your authenticator app.",
        "invalid_recovery_code": "That recovery code isn't valid or was already used.",
    }
    return api_error(status, code, messages[code])


def check_totp(secret: Any, code: Any, *, last_step: int | None, status: int) -> int:
    """The 30-second step ``code`` belongs to, when it is valid for ``secret``
    now. A code no later than ``last_step``, the last one accepted, is refused:
    pyotp itself would take the same code again, and RFC 6238 says not to."""
    cleaned = re.sub(r"\s", "", str(code or ""))
    if not CODE_PATTERN.fullmatch(cleaned):
        raise _refused(status, "invalid_totp_code")
    if not secret or is_encrypted(secret):
        raise api_error(
            503,
            "totp_secret_unreadable",
            "This account's authenticator secret cannot be read on this server. Use a recovery code, "
            "or ask an Administrator to reset your two-factor sign-in.",
        )
    totp = pyotp.TOTP(secret)
    current = totp.timecode(_now())
    window = valid_window()
    for step in range(current - window, current + window + 1):
        if hmac.compare_digest(totp.generate_otp(step), cleaned):
            if last_step is not None and step <= last_step:
                raise _refused(status, "totp_code_used")
            return step
    for step in range(current - window - EXPIRED_LOOKBACK_STEPS, current - window):
        if hmac.compare_digest(totp.generate_otp(step), cleaned):
            raise _refused(status, "totp_code_expired")
    raise _refused(status, "invalid_totp_code")


def use_totp_code(user_id: str, code: Any, *, status: int) -> dict[str, Any]:
    """Accept a code from the user's authenticator app and use it up,
    answering the user as stored after that."""
    with _LOCK:
        user = USERS[user_id]
        step = check_totp(user.get("totp_secret"), code, last_step=user.get("totp_last_step"), status=status)
        updated = {**user, "totp_last_step": step}
        persist_user(updated)
    return updated


def _cleaned_recovery_code(code: Any) -> str:
    return re.sub(r"[\s-]", "", str(code or "")).upper()


def recovery_digest(user_id: str, code: Any) -> str:
    return hashlib.sha256(f"{user_id}:{_cleaned_recovery_code(code)}".encode("utf-8")).hexdigest()


def new_recovery_codes(user_id: str) -> tuple[list[str], list[str]]:
    """A new set of recovery codes, to show once, and the digests to keep in
    their place."""
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        characters = "".join(secrets.choice(RECOVERY_CODE_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))
        codes.append("-".join(characters[start:start + 4] for start in range(0, RECOVERY_CODE_LENGTH, 4)))
    return codes, [recovery_digest(user_id, code) for code in codes]


def use_recovery_code(user_id: str, code: Any, *, status: int) -> dict[str, Any]:
    """Accept an unused recovery code in place of a code from the app, and
    strike it off, answering the user as stored after that."""
    with _LOCK:
        user = USERS[user_id]
        digest = recovery_digest(user_id, code)
        stored = list(user.get("totp_recovery") or [])
        if not _cleaned_recovery_code(code) or digest not in stored:
            raise _refused(status, "invalid_recovery_code")
        updated = {**user, "totp_recovery": [item for item in stored if item != digest]}
        persist_user(updated)
    return updated
