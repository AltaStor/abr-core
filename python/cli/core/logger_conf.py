# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import atexit
import faulthandler
import logging.config
import os
import re
import sys
import tempfile
import traceback
from datetime import datetime, timezone

SYSTEM_LOG_DIR = "/var/log/altastor-backup"
COMPONENT_LOG_SUBDIRS = {"srv": "server", "cli": "cli"}

_REDACTED_ENV_KEYS = ("ABR_BOOTSTRAP_ADMIN_PASSWORD",)
_MONGODB_CREDENTIALS_PATTERN = re.compile(r"(mongodb(?:\+srv)?://)[^/@\s]+@")
REDACTED_VALUE = "********"

_SECRET_KEY_NAMES = (
    "password",
    "passwd",
    "secret",
    "secret_key",
    "access_key",
    "encryption_key",
    "api_key",
    "apikey",
    "token",
)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    rf"(?i)(\b(?:{'|'.join(_SECRET_KEY_NAMES)})\b[\"']?\s*[:=]\s*[\"']?)([^\s\"',;}}\])]+)"
)
_AUTH_HEADER_PATTERN = re.compile(
    r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)(?:bearer\s+)?[^\s\"',;}\])]+"
)
_BEARER_TOKEN_PATTERN = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+")


def redact_text(text: str) -> str:
    for key in _REDACTED_ENV_KEYS:
        text = re.sub(rf"(?m)^(\s*(?:export\s+)?{key}\s*=\s*)(\S+)", rf"\1{REDACTED_VALUE}", text)
    text = _MONGODB_CREDENTIALS_PATTERN.sub(rf"\1{REDACTED_VALUE}:{REDACTED_VALUE}@", text)
    text = _AUTH_HEADER_PATTERN.sub(rf"\1{REDACTED_VALUE}", text)
    text = _BEARER_TOKEN_PATTERN.sub(rf"\1{REDACTED_VALUE}", text)
    return _SECRET_ASSIGNMENT_PATTERN.sub(rf"\1{REDACTED_VALUE}", text)


def redact_log_bytes(raw: bytes) -> bytes:
    text = raw.decode("utf-8", errors="surrogateescape")
    return redact_text(text).encode("utf-8", errors="surrogateescape")


def is_worth_collecting(path) -> bool:
    if not str(path).endswith(".fault"):
        return True
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _prepare_log_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        fd, test_path = tempfile.mkstemp(prefix=".write-test-", dir=path)
        os.close(fd)
        os.remove(test_path)
        return True
    except OSError:
        return False


def _user_state_log_dir() -> str | None:
    xdg_state = os.getenv("XDG_STATE_HOME")
    if xdg_state:
        return os.path.join(xdg_state, "altastor-backup", "logs")
    
    home = os.path.expanduser("~")
    if not home or home == "~":
        return None
    
    return os.path.join(home, ".local", "state", "altastor-backup", "logs")


def _shared_tmp_fallback_dir() -> str:
    parent = os.path.join("/tmp", "altastor-logs")
    try:
        os.makedirs(parent, exist_ok=True)
        os.chmod(parent, 0o1777)
    except OSError:
        pass

    fallback = os.path.join(parent, str(os.getuid()))
    try:
        os.makedirs(fallback, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot create log directory {fallback!r}: {exc}. "
            f"{parent!r} is likely owned by another OS user from before this "
            f"fix; ask an administrator to run `sudo rm -rf {parent}`, or set "
            f"ALTASTOR_LOG_DIR to a writable path."
        ) from exc
    return fallback


def _resolve_log_dir(component: str = "srv", *, notes: list[str] | None = None) -> str:
    component_subdir = COMPONENT_LOG_SUBDIRS.get(component, component)
    repo_default = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "logs",
        component_subdir,
    )
    system_dir = os.path.join(SYSTEM_LOG_DIR, component_subdir)
    explicit = os.getenv("ALTASTOR_LOG_DIR")
    candidates = [explicit] if explicit else []
    candidates.extend([system_dir, repo_default])
    state_dir = _user_state_log_dir()
    if state_dir:
        candidates.append(state_dir)

    requested = candidates[0]

    seen: set[str] = set()
    for candidate in candidates:
        normalized = os.path.abspath(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        if _prepare_log_dir(candidate):
            if candidate != requested and notes is not None:
                notes.append(
                    f"log directory {requested!r} is not usable; using {candidate!r} instead"
                )
            return candidate

    fallback = _shared_tmp_fallback_dir()
    if notes is not None:
        notes.append(
            f"log directory {requested!r} is not usable; using shared fallback {fallback!r} instead"
        )
    return fallback


# Component ("srv" or "cli") that a caller passes to setup_logging() determines
# both the log file name and the logger namespace, so the server and the CLI
# each write to their own file instead of a shared one.
COMPONENT_LOGGER_NAMES = {"srv": "python.srv", "cli": "python.cli"}
COMPONENT_LOG_FILENAMES = {"srv": "server.log", "cli": "cli.log"}


def _build_logging_config(log_dir: str, component: str, file_level: str = "DEBUG"):
    component_logger_name = COMPONENT_LOGGER_NAMES.get(component, f"python.{component}")
    log_filename = COMPONENT_LOG_FILENAMES.get(component, f"{component}.log")
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "console_format": {
                "format": "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                "datefmt": "%H:%M:%S",
            },
            "file_format": {
                "format": "%(asctime)s | %(levelname)-8s | PID:%(process)d | %(name)s:%(funcName)s:%(lineno)d | %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": "INFO",
                "formatter": "console_format",
                "stream": "ext://sys.stdout",
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": file_level,
                "formatter": "file_format",
                "filename": os.path.join(log_dir, log_filename),
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 5,
                "encoding": "utf8",
            },
        },
        "loggers": {
            component_logger_name: {
                "level": file_level,
                "handlers": ["console", "file"],
                "propagate": False,
            },
            "pymongo": {
                "level": "WARNING",
                "handlers": ["console", "file"],
                "propagate": False,
            },
            "pyVmomi": {
                "level": "WARNING",
                "handlers": ["console", "file"],
                "propagate": False,
            },
        },
        "root": {
            "level": "INFO",
            "handlers": ["console", "file"],
        },
    }


_fault_file = None
_fault_path: str | None = None


def close_crash_reporting() -> None:
    if _fault_path is not None:
        _remove_fault_file_if_empty(_fault_path)


def _remove_fault_file_if_empty(fault_path: str) -> None:
    global _fault_file, _fault_path
    try:
        if _fault_file is not None:
            _fault_file.flush()
            _fault_file.close()
        if os.path.getsize(fault_path) == 0:
            os.remove(fault_path)
    except OSError:
        pass
    finally:
        _fault_file = None
        _fault_path = None


def _enable_crash_reporting(log_dir: str, component: str) -> None:
    global _fault_file, _fault_path
    if _fault_file is not None:
        return

    log_filename = COMPONENT_LOG_FILENAMES.get(component, f"{component}.log")
    fault_path = os.path.join(log_dir, f"{log_filename}.fault")
    try:
        _fault_file = open(fault_path, "a", encoding="utf-8")
    except OSError:
        return

    _fault_path = fault_path

    faulthandler.enable(file=_fault_file, all_threads=True)

    def _excepthook(exc_type, exc_value, exc_tb):
        if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            _fault_file.write(
                f"\n--- unhandled exception at {datetime.now(timezone.utc).isoformat()} ---\n"
            )
            traceback.print_exception(exc_type, exc_value, exc_tb, file=_fault_file)
            _fault_file.flush()
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook

    atexit.register(_remove_fault_file_if_empty, fault_path)


def setup_logging(
    *,
    component: str = "srv",
    console_stream: str = "stdout",
    console_level: str = "INFO",
    file_level: str = "DEBUG",
):
    normalized_file_level = str(file_level).upper().replace("WARN", "WARNING")
    notes: list[str] = []
    log_dir = _resolve_log_dir(component, notes=notes)
    config = _build_logging_config(log_dir, component, file_level=normalized_file_level)
    config["handlers"]["console"]["stream"] = f"ext://sys.{console_stream}"
    config["handlers"]["console"]["level"] = (
        str(console_level).upper().replace("WARN", "WARNING")
    )
    config["root"]["level"] = str(console_level).upper().replace("WARN", "WARNING")
    logging.config.dictConfig(config)

    _enable_crash_reporting(log_dir, component)

    component_logger_name = COMPONENT_LOGGER_NAMES.get(component, f"python.{component}")
    for note in notes:
        logging.getLogger(component_logger_name).warning(note)
