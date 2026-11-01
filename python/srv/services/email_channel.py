# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""E-mail delivery for notifications: the SMTP settings, their validation, and
sending one message through the configured server.

The settings are one record in the catalog's ``system_state`` collection (id
``email_notifications``); the SMTP password, the OAuth 2.0 client secret and
refresh token are kept encrypted with the server's credential key, and never
leave the server: the DTO says only whether each one is set.

Authentication is a username and password (SMTP AUTH LOGIN/PLAIN, whichever the
server offers), OAuth 2.0 (SASL XOAUTH2 with an access token from the
provider's token endpoint, by the client-credentials or the refresh-token
grant), or none for a relay that trusts the appliance. The connection is
STARTTLS, implicit TLS ("ssl"), or plain; certificates are verified against the
system trust store unless verification is switched off.

Delivery problems are raised as ``DeliveryError`` with a stable code and the
stage they happened at, so the test e-mail, the delivery history and the
server log all say the same thing.

As a channel of the notification service this module offers ``wants``,
``describe`` and ``send``; ``script_channel`` offers the same three.
"""

from __future__ import annotations

import logging
import re
import smtplib
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Any

import requests

from python.srv.core.credential_encryption import decrypt_secret, encrypt_secret, is_encrypted
from python.srv.core.mongo_db_wrapper import load_system_state, persist_system_state
from python.srv.core.utils import utc_now

from .api_errors import api_error
from .notification_events import DeliveryError, Notification, event_chosen, merged_selection

LOGGER = logging.getLogger(__name__)

ID = "email"
LABEL = "E-mail"
# A message that failed for a reason that can pass is tried again after these.
RETRY_DELAYS_SECONDS = (30, 120)
PRODUCT_NAME = "AltaStor Backup & Replication"
STATE_ID = "email_notifications"

SECURITY_MODES = ("starttls", "ssl", "none")
AUTH_METHODS = ("password", "oauth2", "none")
OAUTH_PROVIDERS = ("microsoft", "google", "custom")
OAUTH_GRANTS = ("client_credentials", "refresh_token")
DEFAULT_PORTS = {"starttls": 587, "ssl": 465, "none": 25}
DEFAULT_TIMEOUT_SECONDS = 30
MAX_RECIPIENTS = 50
DEFAULT_SPACE_THRESHOLD = 85

# Where each provider issues tokens, and the scope that lets the token send
# mail over SMTP. Microsoft's endpoint names the tenant.
MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
MICROSOFT_SCOPE_CLIENT_CREDENTIALS = "https://outlook.office365.com/.default"
MICROSOFT_SCOPE_REFRESH = "https://outlook.office365.com/SMTP.Send offline_access"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPE = "https://mail.google.com/"

# An address as people write them: something@domain.tld, no spaces, no display name.
ADDRESS_PATTERN = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")
HOST_PATTERN = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$|^\[?[0-9A-Fa-f:.]+\]?$")

_lock = threading.RLock()


# ---------------------------------------------------------------- settings --

def _default_settings() -> dict[str, Any]:
    return {
        "id": STATE_ID,
        "enabled": False,
        "smtp": {
            "host": "",
            "port": DEFAULT_PORTS["starttls"],
            "security": "starttls",
            "verify_tls": True,
            "auth": "password",
            "username": "",
            "password": "",
            "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
            "oauth2": {
                "provider": "microsoft",
                "tenant_id": "",
                "client_id": "",
                "client_secret": "",
                "token_url": "",
                "scope": "",
                "grant": "client_credentials",
                "refresh_token": "",
            },
        },
        "sender": {"address": "", "name": "AltaStor Backup & Replication"},
        "recipients": [],
        "events": {},
        "repository_space_threshold_percent": DEFAULT_SPACE_THRESHOLD,
        "updated_at": None,
        "updated_by": None,
    }


def _merged(base: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in stored.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            out[key] = _merged(base[key], value)
        else:
            out[key] = value
    return out


def load_settings() -> dict[str, Any]:
    """The stored settings over the defaults, secrets still encrypted."""
    try:
        stored = load_system_state(STATE_ID) or {}
    except Exception as exc:
        LOGGER.warning("Could not read the e-mail notification settings from the catalog: %s", exc)
        stored = {}
    stored.pop("_id", None)
    return _merged(_default_settings(), stored)


def _secret(value: Any) -> str:
    """A stored secret in the clear; '' when it can't be decrypted."""
    plain = decrypt_secret(value) if value else ""
    return "" if is_encrypted(plain) else str(plain or "")


def settings_dto(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """The settings as the API shows them: every secret replaced by whether it is set."""
    current = settings or load_settings()
    smtp = dict(current["smtp"])
    oauth = dict(smtp.pop("oauth2"))
    password = smtp.pop("password", "")
    client_secret = oauth.pop("client_secret", "")
    refresh_token = oauth.pop("refresh_token", "")
    oauth.update({"client_secret_set": bool(client_secret), "refresh_token_set": bool(refresh_token)})
    if oauth.get("provider") != "custom":
        oauth["effective_token_url"], oauth["effective_scope"] = _oauth_endpoint(current["smtp"]["oauth2"])
    smtp.update({"password_set": bool(password), "oauth2": oauth})
    return {
        "enabled": bool(current.get("enabled")),
        "smtp": smtp,
        "sender": dict(current["sender"]),
        "recipients": list(current.get("recipients") or []),
        "repository_space_threshold_percent": int(current.get("repository_space_threshold_percent") or DEFAULT_SPACE_THRESHOLD),
        "updated_at": current.get("updated_at"),
        "updated_by": current.get("updated_by"),
    }


# -------------------------------------------------------------- validation --

def _str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _apply_patch(current: dict[str, Any], patch: dict[str, Any], problems: dict[str, str]) -> dict[str, Any]:
    """`current` with `patch` laid over it. A secret left out (or null) is
    kept; an empty one clears it. Shape errors land in `problems`."""
    out = _merged(_default_settings(), current)
    if "enabled" in patch and patch["enabled"] is not None:
        if not isinstance(patch["enabled"], bool):
            problems["enabled"] = "Must be true or false"
        else:
            out["enabled"] = patch["enabled"]

    smtp_patch = patch.get("smtp") if isinstance(patch.get("smtp"), dict) else {}
    if patch.get("smtp") is not None and not isinstance(patch.get("smtp"), dict):
        problems["smtp"] = "Must be an object"
    smtp = out["smtp"]
    for key in ("host", "security", "auth", "username"):
        if smtp_patch.get(key) is not None:
            smtp[key] = _str(smtp_patch[key])
    for key in ("port", "timeout_seconds"):
        if smtp_patch.get(key) is not None:
            value = smtp_patch[key]
            if isinstance(value, bool) or not isinstance(value, int):
                problems[f"smtp.{key}"] = "Must be a whole number"
            else:
                smtp[key] = value
    if smtp_patch.get("verify_tls") is not None:
        if not isinstance(smtp_patch["verify_tls"], bool):
            problems["smtp.verify_tls"] = "Must be true or false"
        else:
            smtp["verify_tls"] = smtp_patch["verify_tls"]
    if smtp_patch.get("password") is not None:
        smtp["password"] = encrypt_secret(str(smtp_patch["password"])) if smtp_patch["password"] != "" else ""

    oauth_patch = smtp_patch.get("oauth2") if isinstance(smtp_patch.get("oauth2"), dict) else {}
    oauth = smtp["oauth2"]
    for key in ("provider", "tenant_id", "client_id", "token_url", "scope", "grant"):
        if oauth_patch.get(key) is not None:
            oauth[key] = _str(oauth_patch[key])
    for key in ("client_secret", "refresh_token"):
        if oauth_patch.get(key) is not None:
            oauth[key] = encrypt_secret(str(oauth_patch[key])) if oauth_patch[key] != "" else ""

    sender_patch = patch.get("sender") if isinstance(patch.get("sender"), dict) else {}
    for key in ("address", "name"):
        if sender_patch.get(key) is not None:
            out["sender"][key] = _str(sender_patch[key])

    if patch.get("recipients") is not None:
        raw = patch["recipients"]
        if isinstance(raw, str):
            raw = re.split(r"[,;\s]+", raw)
        if not isinstance(raw, list):
            problems["recipients"] = "Must be a list of e-mail addresses"
        else:
            seen: dict[str, str] = {}
            for item in raw:
                address = _str(item)
                if address and address.lower() not in seen:
                    seen[address.lower()] = address
            out["recipients"] = list(seen.values())

    if patch.get("events") is not None:
        out["events"] = merged_selection(out.get("events"), patch["events"], problems)

    if patch.get("repository_space_threshold_percent") is not None:
        value = patch["repository_space_threshold_percent"]
        if isinstance(value, bool) or not isinstance(value, int):
            problems["repository_space_threshold_percent"] = "Must be a whole number"
        else:
            out["repository_space_threshold_percent"] = value
    return out


def _oauth_endpoint(oauth: dict[str, Any]) -> tuple[str, str]:
    """The token URL and scope the OAuth settings resolve to."""
    provider = oauth.get("provider") or "custom"
    grant = oauth.get("grant") or "client_credentials"
    if provider == "microsoft":
        tenant = _str(oauth.get("tenant_id")) or "organizations"
        scope = MICROSOFT_SCOPE_CLIENT_CREDENTIALS if grant == "client_credentials" else MICROSOFT_SCOPE_REFRESH
        return MICROSOFT_TOKEN_URL.format(tenant=tenant), _str(oauth.get("scope")) or scope
    if provider == "google":
        return GOOGLE_TOKEN_URL, _str(oauth.get("scope")) or GOOGLE_SCOPE
    return _str(oauth.get("token_url")), _str(oauth.get("scope"))


def validate(settings: dict[str, Any], *, require_complete: bool) -> dict[str, str]:
    """Problems with `settings`, by field. Values are always checked;
    `require_complete` (notifications switched on, or a test) also asks for
    every setting delivery needs."""
    problems: dict[str, str] = {}
    smtp = settings["smtp"]
    oauth = smtp["oauth2"]

    host = smtp.get("host") or ""
    if host and not HOST_PATTERN.match(host):
        problems["smtp.host"] = "Not a host name or IP address"
    elif require_complete and not host:
        problems["smtp.host"] = "Enter the SMTP server's host name or IP address"
    port = smtp.get("port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        problems["smtp.port"] = "Must be a port number from 1 to 65535"
    if smtp.get("security") not in SECURITY_MODES:
        problems["smtp.security"] = f"Must be one of: {', '.join(SECURITY_MODES)}"
    timeout = smtp.get("timeout_seconds")
    if not isinstance(timeout, int) or not 5 <= timeout <= 300:
        problems["smtp.timeout_seconds"] = "Must be from 5 to 300 seconds"

    auth = smtp.get("auth")
    if auth not in AUTH_METHODS:
        problems["smtp.auth"] = f"Must be one of: {', '.join(AUTH_METHODS)}"
    elif auth == "password" and require_complete:
        if not smtp.get("username"):
            problems["smtp.username"] = "Enter the user name the SMTP server signs in"
        if not smtp.get("password"):
            problems["smtp.password"] = "Enter the password for that user"
    elif auth == "oauth2":
        provider = oauth.get("provider")
        grant = oauth.get("grant")
        if provider not in OAUTH_PROVIDERS:
            problems["smtp.oauth2.provider"] = f"Must be one of: {', '.join(OAUTH_PROVIDERS)}"
        if grant not in OAUTH_GRANTS:
            problems["smtp.oauth2.grant"] = f"Must be one of: {', '.join(OAUTH_GRANTS)}"
        if provider == "google" and grant == "client_credentials":
            problems["smtp.oauth2.grant"] = (
                "Google issues no client-credentials tokens for mail: use the refresh_token grant"
            )
        token_url = _oauth_endpoint(oauth)[0]
        if token_url and not re.match(r"^https://[^\s/]+", token_url):
            problems["smtp.oauth2.token_url"] = "Must be an https:// URL"
        if oauth.get("tenant_id") and not re.match(r"^[A-Za-z0-9.-]+$", oauth["tenant_id"]):
            problems["smtp.oauth2.tenant_id"] = "Must be a tenant ID (a GUID) or a domain name"
        if require_complete:
            if not smtp.get("username"):
                problems["smtp.username"] = "Enter the mailbox the token sends as"
            if not oauth.get("client_id"):
                problems["smtp.oauth2.client_id"] = "Enter the application (client) ID"
            if provider == "microsoft" and grant == "client_credentials" and not oauth.get("tenant_id"):
                problems["smtp.oauth2.tenant_id"] = "Enter the directory (tenant) ID"
            if provider == "custom" and not token_url:
                problems["smtp.oauth2.token_url"] = "Enter the token endpoint URL"
            if grant == "client_credentials" and not oauth.get("client_secret"):
                problems["smtp.oauth2.client_secret"] = "Enter the client secret"
            if grant == "refresh_token" and not oauth.get("refresh_token"):
                problems["smtp.oauth2.refresh_token"] = "Enter the refresh token"

    address = settings["sender"].get("address") or ""
    if address and not ADDRESS_PATTERN.match(address):
        problems["sender.address"] = "Not an e-mail address"
    elif require_complete and not address:
        problems["sender.address"] = "Enter the address notifications are sent from"
    if len(settings["sender"].get("name") or "") > 100:
        problems["sender.name"] = "At most 100 characters"

    recipients = settings.get("recipients") or []
    bad = [item for item in recipients if not ADDRESS_PATTERN.match(item)]
    if bad:
        problems["recipients"] = f"Not an e-mail address: {', '.join(bad)}"
    elif len(recipients) > MAX_RECIPIENTS:
        problems["recipients"] = f"At most {MAX_RECIPIENTS} recipients"
    elif require_complete and not recipients:
        problems["recipients"] = "Add at least one recipient"

    threshold = settings.get("repository_space_threshold_percent")
    if not isinstance(threshold, int) or not 50 <= threshold <= 99:
        problems["repository_space_threshold_percent"] = "Must be from 50 to 99 percent"
    return problems


def _refuse(problems: dict[str, str]):
    count = len(problems)
    first = next(iter(problems.items()))
    return api_error(
        422,
        "invalid_notification_settings",
        f"{first[0]}: {first[1]}" + (f" (and {count - 1} more)" if count > 1 else ""),
        fields=problems,
    )


def update_settings(patch: dict[str, Any], *, updated_by: str | None) -> tuple[dict[str, Any], list[str]]:
    """Save `patch` over the stored settings, or refuse the whole of it
    (422 invalid_notification_settings, with every problem by field). Returns
    the new DTO and the top-level fields that changed, for the audit log."""
    with _lock:
        current = load_settings()
        problems: dict[str, str] = {}
        updated = _apply_patch(current, patch, problems)
        problems.update(validate(updated, require_complete=bool(updated["enabled"])))
        if problems:
            raise _refuse(problems)
        changed = [key for key in ("enabled", "smtp", "sender", "recipients", "events", "repository_space_threshold_percent") if updated.get(key) != current.get(key)]
        updated["updated_at"] = utc_now()
        updated["updated_by"] = updated_by
        persist_system_state(updated)
        if updated["smtp"] != current["smtp"]:
            _TOKENS.clear()
        return settings_dto(updated), changed


def settings_for_test(patch: dict[str, Any] | None) -> dict[str, Any]:
    """The stored settings with unsaved changes from the form laid over them,
    complete enough to send, or 422 with what is missing."""
    problems: dict[str, str] = {}
    candidate = _apply_patch(load_settings(), patch or {}, problems)
    problems.update(validate(candidate, require_complete=True))
    if problems:
        raise _refuse(problems)
    return candidate


# --------------------------------------------------------------- delivery --

@dataclass
class _Token:
    value: str
    expires_at: float


_TOKENS: dict[tuple[str, str, str, str], _Token] = {}


def _access_token(settings: dict[str, Any]) -> str:
    """An OAuth 2.0 access token for SMTP, from the cache or the provider."""
    oauth = settings["smtp"]["oauth2"]
    token_url, scope = _oauth_endpoint(oauth)
    grant = oauth.get("grant") or "client_credentials"
    key = (token_url, oauth.get("client_id") or "", scope, grant)
    cached = _TOKENS.get(key)
    if cached and cached.expires_at - 60 > time.time():
        return cached.value

    form = {"grant_type": grant, "client_id": oauth.get("client_id") or "", "scope": scope}
    client_secret = _secret(oauth.get("client_secret"))
    if client_secret:
        form["client_secret"] = client_secret
    if grant == "refresh_token":
        form["refresh_token"] = _secret(oauth.get("refresh_token"))
    try:
        response = requests.post(token_url, data=form, timeout=settings["smtp"].get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise DeliveryError(
            "oauth_token_failed",
            f"Could not reach the OAuth 2.0 token endpoint {token_url}: {exc}",
            stage="oauth",
            retryable=True,
        ) from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or not body.get("access_token"):
        reason = body.get("error_description") or body.get("error") or response.text[:300] or f"HTTP {response.status_code}"
        raise DeliveryError(
            "oauth_token_failed",
            f"The OAuth 2.0 token endpoint refused the request (HTTP {response.status_code}): {reason}",
            stage="oauth",
            retryable=response.status_code >= 500,
        )
    token = _Token(str(body["access_token"]), time.time() + float(body.get("expires_in") or 3600))
    _TOKENS[key] = token
    rotated = body.get("refresh_token")
    if grant == "refresh_token" and rotated and rotated != form.get("refresh_token"):
        _store_rotated_refresh_token(str(rotated))
    return token.value


def _store_rotated_refresh_token(token: str) -> None:
    """Keep the refresh token a provider issued in place of the one it just
    accepted (Microsoft rotates them), so the next refresh still works."""
    with _lock:
        stored = load_settings()
        stored["smtp"]["oauth2"]["refresh_token"] = encrypt_secret(token)
        try:
            persist_system_state(stored)
        except Exception as exc:
            LOGGER.warning("Could not store the OAuth 2.0 refresh token the provider rotated: %s", exc)


def _tls_context(settings: dict[str, Any]) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not settings["smtp"].get("verify_tls", True):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _connect(settings: dict[str, Any]) -> smtplib.SMTP:
    smtp = settings["smtp"]
    host, port, security = smtp["host"], int(smtp["port"]), smtp["security"]
    timeout = smtp.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS
    where = f"{host}:{port}"
    stage = "connect"
    client: smtplib.SMTP | None = None
    try:
        if security == "ssl":
            stage = "tls"
            client = smtplib.SMTP_SSL(host, port, timeout=timeout, context=_tls_context(settings))
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
        stage = "connect"
        client.ehlo()
        if security == "starttls":
            stage = "tls"
            if not client.has_extn("starttls"):
                raise DeliveryError(
                    "smtp_tls_failed",
                    f"{where} does not offer STARTTLS: choose SSL/TLS (usually port 465), or a port that offers STARTTLS (usually 587)",
                    stage="tls",
                    retryable=False,
                )
            client.starttls(context=_tls_context(settings))
            client.ehlo()
        return client
    except DeliveryError:
        _quit(client)
        raise
    except ssl.SSLCertVerificationError as exc:
        _quit(client)
        raise DeliveryError(
            "smtp_tls_failed",
            f"The certificate of {where} is not trusted: {exc.verify_message or exc}. Install a certificate the "
            "appliance trusts on the SMTP server, or switch certificate verification off",
            stage="tls",
            retryable=False,
        ) from exc
    except ssl.SSLError as exc:
        _quit(client)
        raise DeliveryError(
            "smtp_tls_failed",
            f"The TLS handshake with {where} failed: {exc}. Check that the security setting matches the port "
            "(STARTTLS on 587, SSL/TLS on 465)",
            stage="tls",
            retryable=False,
        ) from exc
    except socket.gaierror as exc:
        _quit(client)
        raise DeliveryError(
            "smtp_connection_failed", f"The SMTP server name {host} does not resolve: {exc}", stage="connect", retryable=True
        ) from exc
    except (TimeoutError, socket.timeout) as exc:
        _quit(client)
        raise DeliveryError(
            "smtp_connection_failed",
            f"{where} did not answer within {timeout} seconds" + (" during the TLS handshake" if stage == "tls" else ""),
            stage=stage,
            retryable=True,
        ) from exc
    except (OSError, smtplib.SMTPException) as exc:
        _quit(client)
        raise DeliveryError(
            "smtp_connection_failed", f"Could not connect to {where}: {exc}", stage=stage, retryable=True
        ) from exc


def _quit(client: smtplib.SMTP | None) -> None:
    if client is None:
        return
    try:
        client.quit()
    except Exception:
        try:
            client.close()
        except Exception:
            pass


def _smtp_reply(exc: smtplib.SMTPResponseException) -> str:
    text = exc.smtp_error.decode("utf-8", "replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
    return f"{exc.smtp_code} {text}".strip()


def _authenticate(client: smtplib.SMTP, settings: dict[str, Any]) -> None:
    smtp = settings["smtp"]
    method = smtp.get("auth")
    if method == "none":
        return
    where = f"{smtp['host']}:{smtp['port']}"
    if smtp.get("security") == "none":
        LOGGER.warning("Signing in to SMTP server %s over a connection that is not encrypted.", where)
    try:
        if method == "password":
            password = _secret(smtp.get("password"))
            if not password:
                raise DeliveryError(
                    "smtp_auth_failed",
                    "The stored SMTP password cannot be decrypted with this server's credential key: enter it again",
                    stage="auth",
                    retryable=False,
                )
            client.login(smtp["username"], password)
            return
        token = _access_token(settings)
        payload = f"user={smtp['username']}\x01auth=Bearer {token}\x01\x01"
        client.auth("XOAUTH2", lambda challenge=None: payload if challenge is None else "", initial_response_ok=True)
    except DeliveryError:
        raise
    except smtplib.SMTPAuthenticationError as exc:
        hint = (
            "check the user name and password"
            if method == "password"
            else "check that the application may send as this mailbox over SMTP (SMTP AUTH enabled, SMTP.Send or "
            "mail.google.com permission granted)"
        )
        raise DeliveryError(
            "smtp_auth_failed", f"{where} refused the sign-in ({_smtp_reply(exc)}): {hint}", stage="auth", retryable=False
        ) from exc
    except smtplib.SMTPNotSupportedError as exc:
        raise DeliveryError(
            "smtp_auth_failed",
            f"{where} offers no sign-in on this connection ({exc}): it may need STARTTLS or SSL/TLS first, or "
            "accept mail without authentication",
            stage="auth",
            retryable=False,
        ) from exc
    except smtplib.SMTPException as exc:
        raise DeliveryError(
            "smtp_auth_failed", f"Signing in to {where} failed: {exc}", stage="auth", retryable=False
        ) from exc
    except OSError as exc:
        raise DeliveryError(
            "smtp_connection_failed", f"{where} closed the connection while signing in: {exc}", stage="auth", retryable=True
        ) from exc


def _footer() -> str:
    try:
        from . import abr_v41

        version = abr_v41.product_version()
    except Exception:
        version = "unknown version"
    return (
        "\n--\n"
        f"Sent by {PRODUCT_NAME} {version} on {socket.gethostname()}.\n"
        "Choose which events send e-mail in the web console (Settings > Notifications) or with "
        "`abr notification events set`.\n"
    )


def wants(notification: Notification) -> bool:
    settings = load_settings()
    return bool(settings.get("enabled")) and event_chosen(settings.get("events"), notification.event)


def describe(notification: Notification) -> dict[str, Any]:
    """What the delivery history shows about a message before it is sent."""
    prefix = {"error": "[AltaStor] FAILED: ", "warning": "[AltaStor] WARNING: "}.get(notification.severity, "[AltaStor] ")
    return {"subject": prefix + notification.subject, "recipients": list(load_settings().get("recipients") or [])}


def send(notification: Notification, entry: dict[str, Any]) -> dict[str, Any]:
    """Deliver `notification` with the settings as they are now, or raise
    DeliveryError. Returns what the delivery history records."""
    settings = load_settings()
    recipients = list(settings.get("recipients") or [])
    entry["recipients"] = recipients
    return {"refused": deliver(settings, recipients, entry["subject"], notification.body + _footer())}


def test_message(settings: dict[str, Any], recipients: list[str]) -> tuple[str, str]:
    """The subject and body of a test e-mail."""
    smtp = settings["smtp"]
    auth = {"password": f"user {smtp['username']}", "oauth2": f"OAuth 2.0 as {smtp['username']}", "none": "no sign-in"}[smtp["auth"]]
    return (
        "[AltaStor] Test e-mail",
        "This is a test message from AltaStor Backup & Replication.\n\n"
        "It reached you, so the appliance can send notifications with these settings:\n"
        f"  SMTP server:  {smtp['host']}:{smtp['port']} ({smtp['security']}, {auth})\n"
        f"  Sender:       {settings['sender']['address']}\n"
        f"  Recipients:   {', '.join(recipients)}\n" + _footer(),
    )


def build_message(settings: dict[str, Any], recipients: list[str], subject: str, body: str) -> EmailMessage:
    sender = settings["sender"]
    message = EmailMessage()
    message["From"] = formataddr((sender.get("name") or "", sender["address"]))
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=sender["address"].rsplit("@", 1)[-1])
    message["Auto-Submitted"] = "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"
    message.set_content(body)
    return message


def deliver(settings: dict[str, Any], recipients: list[str], subject: str, body: str) -> list[str]:
    """Send one message, or raise DeliveryError saying why it did not go.
    Returns the recipients the server refused while taking it for the rest."""
    message = build_message(settings, recipients, subject, body)
    client = _connect(settings)
    try:
        _authenticate(client, settings)
        refused = client.send_message(message, from_addr=settings["sender"]["address"], to_addrs=recipients)
    except DeliveryError:
        raise
    except smtplib.SMTPSenderRefused as exc:
        raise DeliveryError(
            "smtp_sender_refused",
            f"The SMTP server refused the sender {settings['sender']['address']} ({_smtp_reply(exc)}): use an "
            "address the signed-in account may send as",
            stage="send",
            retryable=False,
        ) from exc
    except smtplib.SMTPRecipientsRefused as exc:
        detail = "; ".join(f"{address}: {code} {reply.decode('utf-8', 'replace') if isinstance(reply, bytes) else reply}" for address, (code, reply) in exc.recipients.items())
        raise DeliveryError(
            "smtp_recipients_refused", f"The SMTP server refused every recipient: {detail}", stage="send", retryable=False
        ) from exc
    except smtplib.SMTPResponseException as exc:
        raise DeliveryError(
            "smtp_error",
            f"The SMTP server did not accept the message ({_smtp_reply(exc)})",
            stage="send",
            retryable=400 <= exc.smtp_code < 500,
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise DeliveryError(
            "smtp_connection_failed", f"The connection broke while sending: {exc}", stage="send", retryable=True
        ) from exc
    finally:
        _quit(client)
    if refused:
        LOGGER.warning(
            "The SMTP server accepted the message but refused some recipients: %s",
            ", ".join(f"{address} ({code})" for address, (code, _reply) in refused.items()),
        )
    return sorted(refused)
