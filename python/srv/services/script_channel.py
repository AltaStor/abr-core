# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""The administrator's own notification script: a program uploaded through the
Web UI, the CLI or the API that the appliance runs when a chosen event happens,
to reach whatever e-mail does not (a chat system, a ticket queue, a pager).

A script runs as the server's service user, so it can do what that user can:
read the catalog and the credential key, and write to the repositories.
Uploading one therefore takes its own function, ``notification.script``, which
only the Administrator holds, and ``ABR_NOTIFICATION_SCRIPTS=0`` switches the
feature off for an appliance where that is too much.

What limits it: the content is text with a ``#!`` line, kept encrypted in the
catalog and written to a private folder only for a run; the run gets a minimal
environment, so none of the server's own variables reach it; it has a time limit
that ends the script and whatever it started; only the end of its output is
kept, passed through the log redaction. A run that fails is not repeated: a
script need not be safe to run twice.

The script gets the event as JSON on standard input and in the file named by
``ABR_EVENT_FILE``, and the main fields in ``ABR_*`` variables. Everything in it
can come from outside (a VM's name, for one), so a script must treat it as data.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from python.srv.core.credential_encryption import decrypt_secret, encrypt_secret, is_encrypted
from python.srv.core.logger_conf import redact_text
from python.srv.core.mongo_db_wrapper import load_system_state, persist_system_state
from python.srv.core.subprocess_util import describe_process_end, run_capped
from python.srv.core.utils import utc_now

from .api_errors import api_error
from .notification_events import EVENTS_BY_ID, TEST_EVENT, DeliveryError, Notification, event_chosen, merged_selection

LOGGER = logging.getLogger(__name__)

ID = "script"
LABEL = "Script"
# Not repeated: a script need not be safe to run twice.
RETRY_DELAYS_SECONDS: tuple[int, ...] = ()
STATE_ID = "notification_script"
ENABLE_ENV = "ABR_NOTIFICATION_SCRIPTS"
RUN_ROOT = Path("/var/lib/altastor-backup/notification-runs")
RUN_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

MAX_SCRIPT_BYTES = 256 * 1024
MIN_TIMEOUT_SECONDS = 5
MAX_TIMEOUT_SECONDS = 600
DEFAULT_TIMEOUT_SECONDS = 60
MAX_CAPTURE_BYTES = 64 * 1024
MAX_KEPT_OUTPUT = 4000
MAX_BODY_IN_ENVIRONMENT = 32 * 1024
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$")
_UNREADABLE = "The stored script cannot be decrypted with this server's credential key: upload it again"

_lock = threading.Lock()


def available() -> bool:
    return os.environ.get(ENABLE_ENV, "1").strip() != "0"


def _require_available() -> None:
    if not available():
        raise api_error(
            403,
            "notification_scripts_disabled",
            f"Notification scripts are switched off on this appliance ({ENABLE_ENV}=0)",
        )


# ---------------------------------------------------------------- settings --

def _default_settings() -> dict[str, Any]:
    return {
        "id": STATE_ID,
        "enabled": False,
        "events": {},
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "file": None,
        "updated_at": None,
        "updated_by": None,
    }


def load_settings() -> dict[str, Any]:
    try:
        stored = load_system_state(STATE_ID) or {}
    except Exception as exc:
        LOGGER.warning("Could not read the notification script settings from the catalog: %s", exc)
        stored = {}
    stored.pop("_id", None)
    return {**_default_settings(), **stored}


def _file_dto(settings: dict[str, Any]) -> dict[str, Any] | None:
    stored = settings.get("file")
    return {key: value for key, value in stored.items() if key != "content"} if stored else None


def settings_dto(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """The settings as the API shows them; the script itself only through
    `content`, under its own function."""
    current = settings or load_settings()
    return {
        "available": available(),
        "enabled": bool(current.get("enabled")),
        "timeout_seconds": current.get("timeout_seconds"),
        "file": _file_dto(current),
        "updated_at": current.get("updated_at"),
        "updated_by": current.get("updated_by"),
    }


def _refuse(problems: dict[str, str]):
    first = next(iter(problems.items()))
    return api_error(422, "invalid_notification_script", f"{first[0]}: {first[1]}", fields=problems)


def _save(settings: dict[str, Any], updated_by: str | None) -> None:
    settings["updated_at"] = utc_now()
    settings["updated_by"] = updated_by
    persist_system_state(settings)


def update_settings(patch: dict[str, Any], *, updated_by: str | None) -> tuple[dict[str, Any], list[str]]:
    """Change whether the script runs, on which events and for how long, or
    refuse all of it (422 invalid_notification_script, problems by field)."""
    _require_available()
    with _lock:
        current = load_settings()
        updated = dict(current)
        problems: dict[str, str] = {}
        if patch.get("enabled") is not None:
            if not isinstance(patch["enabled"], bool):
                problems["enabled"] = "Must be true or false"
            else:
                updated["enabled"] = patch["enabled"]
        if patch.get("timeout_seconds") is not None:
            value = patch["timeout_seconds"]
            if isinstance(value, bool) or not isinstance(value, int) or not MIN_TIMEOUT_SECONDS <= value <= MAX_TIMEOUT_SECONDS:
                problems["timeout_seconds"] = f"Must be from {MIN_TIMEOUT_SECONDS} to {MAX_TIMEOUT_SECONDS} seconds"
            else:
                updated["timeout_seconds"] = value
        if patch.get("events") is not None:
            updated["events"] = merged_selection(current.get("events"), patch["events"], problems)
        if updated["enabled"] and not updated.get("file"):
            problems["enabled"] = "Upload a script first"
        if problems:
            raise _refuse(problems)
        changed = [key for key in ("enabled", "events", "timeout_seconds") if updated.get(key) != current.get(key)]
        _save(updated, updated_by)
        return settings_dto(updated), changed


def _interpreter_problem(first_line: str) -> str | None:
    """Why the `#!` line cannot run here, or None. Checked on upload, so a
    script for a language the appliance lacks is refused now, not at 3 a.m."""
    words = first_line[2:].split()
    if not words:
        return "The #! line names no interpreter"
    interpreter = words[0]
    if not interpreter.startswith("/"):
        return f"The #! line must give the interpreter's full path, not {interpreter}"
    if not os.access(interpreter, os.X_OK):
        return f"The interpreter {interpreter} does not exist on the appliance"
    if Path(interpreter).name == "env":
        program = next((word for word in words[1:] if not word.startswith("-") and "=" not in word), None)
        if program is None:
            return "The #! line names no program for env to run"
        if shutil.which(program, path=RUN_PATH) is None:
            return f"The interpreter {program} does not exist on the appliance"
    return None


def _checked_content(raw: Any) -> tuple[str | None, str | None]:
    """The script as it will run, or why not: text with a `#!` line. Windows
    line ends and a byte-order mark are removed, as either stops the `#!` line
    working."""
    if not isinstance(raw, str) or not raw.strip():
        return None, "Choose a script to upload"
    content = raw.removeprefix("﻿").replace("\r\n", "\n")
    if len(content.encode("utf-8")) > MAX_SCRIPT_BYTES:
        return None, f"The script is larger than {MAX_SCRIPT_BYTES // 1024} KiB"
    if "\x00" in content:
        return None, "The script must be text, not a binary file"
    first_line = content.split("\n", 1)[0]
    if not first_line.startswith("#!"):
        return None, "The script must start with a #! line naming its interpreter, such as #!/bin/sh"
    problem = _interpreter_problem(first_line)
    return (None, problem) if problem else (content, None)


def upload(payload: dict[str, Any], *, updated_by: str | None) -> dict[str, Any]:
    """Store a script in place of any earlier one. A replacement keeps the
    enabled state, so the next event runs the new script."""
    _require_available()
    name = Path(str(payload.get("name") or "").strip().replace("\\", "/")).name
    problems: dict[str, str] = {}
    if not NAME_PATTERN.match(name):
        problems["name"] = "Use letters, digits, spaces, dots, dashes and underscores, at most 128 characters"
    content, problem = _checked_content(payload.get("script_content"))
    if problem:
        problems["script_content"] = problem
    if problems:
        raise _refuse(problems)
    encoded = content.encode("utf-8")
    with _lock:
        settings = load_settings()
        settings["file"] = {
            "name": name,
            "size": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "uploaded_at": utc_now(),
            "uploaded_by": updated_by,
            "content": encrypt_secret(content),
        }
        _save(settings, updated_by)
        return settings_dto(settings)


def remove(*, updated_by: str | None) -> dict[str, Any]:
    """Delete the script, which also stops it running."""
    with _lock:
        settings = load_settings()
        settings.update(file=None, enabled=False)
        _save(settings, updated_by)
        return settings_dto(settings)


def _content_of(settings: dict[str, Any]) -> str | None:
    stored = settings.get("file")
    if not stored:
        return None
    content = decrypt_secret(stored.get("content"))
    return None if is_encrypted(content) or not content else str(content)


def content() -> dict[str, Any]:
    """The stored script, to review it."""
    settings = load_settings()
    file = _file_dto(settings)
    if file is None:
        raise api_error(404, "notification_script_not_found", "No notification script has been uploaded")
    text = _content_of(settings)
    if text is None:
        raise api_error(503, "notification_script_unreadable", _UNREADABLE)
    return {**file, "script_content": text}


# --------------------------------------------------------------- the channel --

def wants(notification: Notification) -> bool:
    settings = load_settings()
    return (
        available()
        and bool(settings.get("enabled"))
        and bool(settings.get("file"))
        and event_chosen(settings.get("events"), notification.event)
    )


def describe(notification: Notification) -> dict[str, Any]:
    file = load_settings().get("file") or {}
    return {"subject": notification.subject, "script": file.get("name"), "sha256": file.get("sha256")}


def test_notification() -> Notification:
    """A sample event for a test run. A test works while the script is
    switched off, which is how to try one before it goes live."""
    _require_available()
    if not load_settings().get("file"):
        raise api_error(404, "notification_script_not_found", "No notification script has been uploaded")
    return Notification(
        TEST_EVENT,
        "Test of the notification script",
        "This is a test run of the notification script, not a real event.\n",
        details={"test": True},
    )


def _run_directory() -> str:
    """A private folder for one run: under the service's state folder, whose
    file system a hardened appliance leaves executable, or the system's
    temporary folder in a development checkout."""
    try:
        RUN_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        return tempfile.mkdtemp(prefix="run-", dir=RUN_ROOT)
    except OSError:
        return tempfile.mkdtemp(prefix="abr-notification-")


def _event_document(notification: Notification, name: str) -> dict[str, Any]:
    from . import abr_v41

    return {
        "event": notification.event,
        "name": name,
        "severity": notification.severity,
        "subject": notification.subject,
        "body": notification.body,
        "time": utc_now(),
        "host": socket.gethostname(),
        "version": abr_v41.product_version(),
        "details": dict(notification.details),
    }


def _environment(document: dict[str, Any], run_directory: str, event_file: str) -> dict[str, str]:
    """Everything the script gets from the server. Built from nothing, so none
    of the server's own variables (the database URI, a bootstrap password) go
    with it."""
    return {
        "PATH": RUN_PATH,
        "LANG": "C.UTF-8",
        "HOME": run_directory,
        "TMPDIR": run_directory,
        "ABR_EVENT": document["event"],
        "ABR_EVENT_NAME": document["name"],
        "ABR_EVENT_FILE": event_file,
        "ABR_SEVERITY": document["severity"],
        "ABR_SUBJECT": document["subject"],
        "ABR_BODY": document["body"][:MAX_BODY_IN_ENVIRONMENT],
        "ABR_TIME": document["time"],
        "ABR_HOST": document["host"],
        "ABR_VERSION": document["version"],
    }


def _kept_output(stdout: Any, stderr: Any) -> str:
    """The end of what the script printed, as the log redaction leaves it."""
    text = (
        (stdout.decode("utf-8", "replace") if isinstance(stdout, bytes) else str(stdout or ""))
        + (stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else str(stderr or ""))
    )
    return redact_text(text.strip())[-MAX_KEPT_OUTPUT:]


def send(notification: Notification, entry: dict[str, Any]) -> dict[str, Any]:
    """Run the script on `notification`, or raise DeliveryError. Returns what
    the history records: its exit code, how long it took and the end of its
    output."""
    settings = load_settings()
    file = settings.get("file") or {}
    entry.update(script=file.get("name"), sha256=file.get("sha256"))
    text = _content_of(settings)
    if text is None:
        raise DeliveryError("script_unreadable", _UNREADABLE, stage="start", retryable=False)
    name = str(file.get("name") or "script")
    timeout = int(settings.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    run_directory = _run_directory()
    started = time.monotonic()
    try:
        script = Path(run_directory) / "script"
        script.write_text(text, encoding="utf-8")
        script.chmod(0o700)
        event_name = _event_name(notification)
        document = _event_document(notification, event_name)
        event_file = Path(run_directory) / "event.json"
        event_file.write_text(json.dumps(document, default=str), encoding="utf-8")
        with event_file.open("rb") as event_input:
            completed = run_capped(
                [str(script)],
                timeout=timeout,
                nice=False,
                max_capture_bytes=MAX_CAPTURE_BYTES,
                stdin=event_input,
                env=_environment(document, run_directory, str(event_file)),
                cwd=run_directory,
                own_process_group=True,
            )
    except subprocess.TimeoutExpired as exc:
        extra = {"exit_code": None, "duration_ms": _elapsed_ms(started), "output": _kept_output(exc.output, exc.stderr)}
        raise DeliveryError(
            "script_timed_out", f"{name} did not finish within {timeout} seconds and was stopped", stage="run", retryable=False, extra=extra
        ) from exc
    except OSError as exc:
        raise DeliveryError(
            "script_not_started",
            f"{name} could not be started: {exc.strerror or exc}. The appliance's folder for runs may not allow programs to run",
            stage="start",
            retryable=False,
            extra={"duration_ms": _elapsed_ms(started)},
        ) from exc
    finally:
        shutil.rmtree(run_directory, ignore_errors=True)
    result = {
        "exit_code": completed.returncode,
        "duration_ms": _elapsed_ms(started),
        "output": _kept_output(completed.stdout, completed.stderr),
    }
    if completed.returncode != 0:
        raise DeliveryError(
            "script_failed", describe_process_end(name, completed.returncode), stage="run", retryable=False, extra=result
        )
    return result


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _event_name(notification: Notification) -> str:
    event = EVENTS_BY_ID.get(notification.event)
    return event.name if event else "Test"
