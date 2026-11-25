# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

"""Running Apprise, the program the messaging platforms (Telegram, Slack,
Discord, ntfy) go out through.

Apprise (https://github.com/caronc/apprise, BSD-2-Clause) is not imported: it
runs as a program of its own, the Debian package's ``/usr/bin/apprise``
(``ABR_APPRISE`` names another one), once per message and target. So its
dependencies stay out of the server, and a run that hangs is stopped with
whatever it started.

A target's URL holds its secrets (a bot token, a workflow's signature), so it
never goes on a command line, where any local user could read it: it is written
to a private configuration file for the run, which is removed afterwards. Apprise
is kept from loading anything else: no plugins from its plugin folders, no
configuration from ``/etc``, no ``include``, no cache on disk, and an environment
built from nothing. Apprise prints secrets in its debug output, so only its
warnings and errors are read, with every secret of the target masked.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from python.srv.core.logger_conf import redact_text
from python.srv.core.subprocess_util import run_capped

from .notification_events import DeliveryError

LOGGER = logging.getLogger(__name__)

PROGRAM_ENV = "ABR_APPRISE"
DEFAULT_PROGRAM = "/usr/bin/apprise"
RUN_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SEND_TIMEOUT_SECONDS = 60
CHECK_TIMEOUT_SECONDS = 20
MAX_CAPTURE_BYTES = 256 * 1024
MAX_REASON_LENGTH = 600
MASK = "•••"

_LOG_LINE = re.compile(r"^\S+ \S+ - (WARNING|ERROR|DEBUG|INFO|TRACE) - (.*)$")
_HTTP_STATUS = re.compile(r"error=(\d{3})")
_HTTP_STATUS_TAIL = re.compile(r"[.,;]?\s*error=\d{3}\.?\s*$")
_SEND_FAILED = re.compile(r"^Failed to send [^:]*?notification(?: to [^:]+)?:\s*")
_VERSION = re.compile(r"Apprise v(\S+)")
# The HTTP answers that can pass: a timeout, too early, too many requests.
_PASSING_STATUSES = {408, 425, 429}

_versions: dict[tuple[str, float], str | None] = {}


@dataclass(frozen=True, slots=True)
class Target:
    """Where one message goes. `url` is the Apprise URL, a secret; `key` names
    it in the delivery history (a chat ID); `label` in messages ("chat
    -1001234567890"); `secrets` are the strings to mask in anything kept."""

    url: str
    key: str
    label: str
    secrets: tuple[str, ...]


def program() -> str | None:
    """The Apprise program, or None when it is not installed."""
    path = os.environ.get(PROGRAM_ENV, "").strip() or DEFAULT_PROGRAM
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def version() -> str | None:
    """Apprise's version, such as 1.9.3; asked once per installed program."""
    path = program()
    if path is None:
        return None
    try:
        key = (path, os.stat(path).st_mtime)
    except OSError:
        return None
    if key not in _versions:
        found = None
        try:
            completed = run_capped(
                [path, "--version"], timeout=CHECK_TIMEOUT_SECONDS, nice=False, env=_environment(tempfile.gettempdir())
            )
            match = _VERSION.search(f"{completed.stdout}\n{completed.stderr}")
            found = match.group(1) if match else None
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.warning("Could not ask %s for its version: %s", path, exc)
        _versions[key] = found
    return _versions[key]


def availability() -> dict[str, object]:
    return {"available": program() is not None, "version": version()}


def missing_error() -> DeliveryError:
    return DeliveryError(
        "apprise_missing",
        f"Apprise is not installed on the appliance ({DEFAULT_PROGRAM} from the apprise package): ask for it to be installed",
        stage="start",
        retryable=False,
    )


def _environment(run_directory: str) -> dict[str, str]:
    return {"PATH": RUN_PATH, "LANG": "C.UTF-8", "HOME": run_directory, "TMPDIR": run_directory}


def _mask(text: str, secrets: tuple[str, ...]) -> str:
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        text = text.replace(secret, MASK)
    return redact_text(text)


def _run(target: Target, arguments: list[str], *, stdin_text: str, timeout: float) -> tuple[int, list[tuple[str, str]]]:
    """Run Apprise on `target` alone. Answers its exit code and the log lines
    worth reading, as (level, text) with the secrets masked."""
    path = program()
    if path is None:
        raise missing_error()
    run_directory = tempfile.mkdtemp(prefix="abr-apprise-")
    try:
        plugins = Path(run_directory) / "plugins"
        plugins.mkdir(mode=0o700)
        config = Path(run_directory) / "apprise.txt"
        descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(target.url + "\n")
        command = [
            path,
            "-vvv",
            "--recursion-depth", "0",
            "--plugin-path", str(plugins),
            "--storage-mode", "memory",
            "--config", str(config),
            *arguments,
        ]
        completed = run_capped(
            command,
            timeout=timeout,
            nice=False,
            max_capture_bytes=MAX_CAPTURE_BYTES,
            stdin_text=stdin_text,
            env=_environment(run_directory),
            cwd=run_directory,
            own_process_group=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise DeliveryError(
            "timed_out",
            f"Sending to {target.label} did not finish within {timeout:g} seconds and was stopped",
            stage="send",
            retryable=True,
        ) from exc
    except OSError as exc:
        raise DeliveryError(
            "apprise_not_started", f"Apprise could not be started: {exc.strerror or exc}", stage="start", retryable=False
        ) from exc
    finally:
        shutil.rmtree(run_directory, ignore_errors=True)
    return completed.returncode, _log_lines(f"{completed.stdout or ''}\n{completed.stderr or ''}", target.secrets)


def _log_lines(output: str, secrets: tuple[str, ...]) -> list[tuple[str, str]]:
    """Apprise's warnings and errors, and the one debug line that says why a
    connection failed. Its other debug lines carry the URL and the message."""
    lines: list[tuple[str, str]] = []
    for raw in output.splitlines():
        match = _LOG_LINE.match(raw.strip())
        if not match:
            continue
        level, text = match.groups()
        if level in ("WARNING", "ERROR") or (level == "DEBUG" and text.startswith("Socket Exception:")):
            lines.append((level, _mask(text, secrets)))
    return lines


def check(target: Target) -> str | None:
    """Why Apprise cannot read `target`'s URL, or None when it can. Sends
    nothing."""
    code, lines = _run(target, ["--dry-run", "--title", "check", "--body", "check"], stdin_text="", timeout=CHECK_TIMEOUT_SECONDS)
    if code == 0:
        return None
    return _reason(lines) or f"Apprise ended with exit code {code}"


def send(target: Target, *, service: str, title: str, body: str, kind: str) -> None:
    """Send one message to `target`, or raise DeliveryError saying why it did
    not go. `kind` is info, success, warning or failure."""
    code, lines = _run(
        target,
        ["--title", title, "--notification-type", kind, "--input-format", "text"],
        stdin_text=body,
        timeout=SEND_TIMEOUT_SECONDS,
    )
    if code == 0:
        return
    raise _failure(code, lines, target, service)


def _reason(lines: list[tuple[str, str]]) -> str:
    """What Apprise said: its first error, or else its last warning. The
    warning that it could not load the run's own file says nothing more."""
    errors = [text for level, text in lines if level == "ERROR"]
    warnings = [text for level, text in lines if level == "WARNING" and not text.startswith("Failed to load Apprise configuration")]
    said = errors[0] if errors else (warnings[-1] if warnings else "")
    return said[:MAX_REASON_LENGTH]


def named() -> str:
    """Apprise with its version, for messages."""
    found = version()
    return f"Apprise {found}" if found else "Apprise"


def _failure(code: int, lines: list[tuple[str, str]], target: Target, service: str) -> DeliveryError:
    reason = _reason(lines)
    if code == 3:
        return DeliveryError(
            "apprise_url_refused",
            f"{named()} cannot read the settings for {target.label}: {reason or 'unparseable URL'}",
            stage="start",
            retryable=False,
        )
    if code != 1:
        return DeliveryError(
            "apprise_failed", f"Apprise ended with exit code {code}: {reason or 'no reason given'}", stage="start", retryable=False
        )
    status = next((int(match.group(1)) for _level, text in reversed(lines) if (match := _HTTP_STATUS.search(text))), None)
    if status is not None:
        passing = status >= 500 or status in _PASSING_STATUSES
        # "Failed to send Telegram notification to X: Bad Request: chat not found, error=400." says
        # "Bad Request: chat not found".
        answer = _SEND_FAILED.sub("", _HTTP_STATUS_TAIL.sub("", reason)).strip() or f"HTTP {status}"
        return DeliveryError(
            "delivery_failed" if passing else "delivery_refused",
            f"{service} {'did not take' if passing else 'refused'} the message for {target.label} (HTTP {status}): {answer}",
            stage="send",
            retryable=passing,
        )
    exception = next((text for level, text in reversed(lines) if level == "DEBUG"), "")
    return _connection_failure(exception, reason, target, service)


def _connection_failure(exception: str, reason: str, target: Target, service: str) -> DeliveryError:
    host = re.search(r"host='([^']+)'", exception)
    where = f"{service} ({host.group(1)})" if host else service
    if "CERTIFICATE_VERIFY_FAILED" in exception or "certificate verify failed" in exception:
        detail = re.search(r"certificate verify failed: ([^(\"')]+)", exception)
        return DeliveryError(
            "tls_failed",
            f"The certificate of {where} is not trusted"
            + (f": {detail.group(1).strip()}" if detail else "")
            + ". A proxy or firewall that inspects TLS needs its certificate in the appliance's trust store",
            stage="tls",
            retryable=False,
        )
    if "SSLError" in exception:
        return DeliveryError("tls_failed", f"The TLS handshake with {where} failed", stage="tls", retryable=True)
    if any(marker in exception for marker in ("NameResolutionError", "Failed to resolve", "Name or service not known", "name resolution")):
        return DeliveryError(
            "connection_failed",
            f"The name of {where} does not resolve: check the appliance's DNS settings",
            stage="connect",
            retryable=True,
        )
    if "Connection refused" in exception or "ConnectionRefusedError" in exception:
        return DeliveryError("connection_failed", f"{where} refused the connection", stage="connect", retryable=True)
    if "timed out" in exception or "Timeout" in exception:
        return DeliveryError("connection_failed", f"{where} did not answer in time", stage="connect", retryable=True)
    if "Network is unreachable" in exception or "No route to host" in exception:
        return DeliveryError(
            "connection_failed",
            f"The appliance cannot reach {where}: no route to it. Outgoing HTTPS (port 443) must be allowed",
            stage="connect",
            retryable=True,
        )
    return DeliveryError(
        "connection_failed",
        f"Could not send to {target.label}: {reason or 'Apprise gave no reason'}",
        stage="connect",
        retryable=True,
    )


def elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
