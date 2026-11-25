# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

import logging
import os
import re
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from python.srv.core import broker_client
from python.srv.core.credential_encryption import is_encrypted
from python.srv.core.mongo_db_wrapper import REPOSITORIES, catalog_records
from python.srv.core.server_defaults import env_int
from python.srv.core.utils import utc_now
from python.srv.services.api_errors import ApiError, api_error
from python.srv.services.share_mount import (
    SMB_FILESYSTEMS,
    Mount,
    read_mounts,
    same_server,
    unc_parts,
)

SOCKET_ENV = "ABR_MOUNTS_BROKER_SOCKET"
DEFAULT_SOCKET = "/run/altastor-backup-mounts.sock"
TIMEOUT_ENV = "ABR_MOUNTS_BROKER_TIMEOUT_SECONDS"
MOUNTS_ROOT = "/run/altastor-backup-mounts"
DOMAIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,254})")
NOT_IN_ACCOUNT = frozenset(',=/:*?"<>|')


def broker_socket() -> str:
    return os.environ.get(SOCKET_ENV, "").strip() or DEFAULT_SOCKET


def mount_point(repository_id: str, *, trial: bool = False) -> str:
    return f"{MOUNTS_ROOT}/{repository_id}{'.trial' if trial else ''}"


def share_and_subfolder(share: str) -> tuple[str, str]:
    parts = unc_parts(share)
    if not parts:
        raise api_error(422, "invalid_repository", "Give the share as \\\\server\\share, optionally with a folder inside it")
    if any(part in (".", "..") for part in parts[1:]):
        raise api_error(422, "invalid_repository", "The folder inside the share must not hold . or .. parts", share=share)
    return parts[0], "/".join(parts[1:])


def _refuse_account(username: str, reason: str) -> None:
    raise api_error(
        422,
        "invalid_repository",
        f"The account {username!r} {reason}. Give it as user, DOMAIN\\user or user@domain",
        username=username,
    )


def account_and_domain(username: str) -> tuple[str, str]:
    text = str(username or "").strip()
    if not text:
        _refuse_account(text, "is empty")
    if "\\" in text and "@" in text:
        _refuse_account(text, "names a domain twice, with \\ and with @")
    if text.count("\\") > 1 or text.count("@") > 1:
        _refuse_account(text, "holds more than one \\ or @")
    if "\\" in text:
        domain, _, user = text.partition("\\")
    elif "@" in text:
        user, _, domain = text.partition("@")
    else:
        user, domain = text, ""
    user, domain = user.strip(), domain.strip()
    if not user:
        _refuse_account(text, "has no user name")
    if ("\\" in text or "@" in text) and not domain:
        _refuse_account(text, "has no domain after the \\ or @")
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        _refuse_account(text, "holds control characters")
    bad = sorted(NOT_IN_ACCOUNT & set(user))
    if bad:
        _refuse_account(text, f"holds {' '.join(bad)}, which a mount cannot take")
    if domain and not DOMAIN.fullmatch(domain):
        _refuse_account(text, f"has the domain {domain!r}; a domain may only hold Latin letters, digits, dots, - and _")
    return user, domain


def mount_arguments(repository_id: str, server: str, share: str, username: str, password: str, *, trial: bool = False) -> dict[str, Any]:
    share_name, subfolder = share_and_subfolder(share)
    user, domain = account_and_domain(username)
    arguments: dict[str, Any] = {
        "repository_id": repository_id,
        "server": str(server or "").strip(),
        "share": share_name,
        "username": user,
        "password": password,
    }
    if subfolder:
        arguments["subfolder"] = subfolder
    if domain:
        arguments["domain"] = domain
    if trial:
        arguments["trial"] = True
    return arguments


def call(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return broker_client.call(broker_socket(), operation, arguments, timeout=env_int(TIMEOUT_ENV))


MANAGED_MARK = "managed_mount"
RETRY_PAUSES_SECONDS = (30, 60, 120, 300, 600, 900)
SAMPLE_POINTS = 5
STATE: dict[str, dict[str, Any]] = {}
LOCK = threading.RLock()
logger = logging.getLogger(__name__)


def _metadata(record: dict[str, Any]) -> dict[str, Any]:
    return dict(record.get("metadata") if isinstance(record.get("metadata"), dict) else {})


def is_managed(record: dict[str, Any] | None) -> bool:
    return isinstance(record, dict) and str(record.get("type") or "").lower() == "smb" and bool(_metadata(record).get(MANAGED_MARK))


def expected_source(record: dict[str, Any]) -> str:
    metadata = _metadata(record)
    share, subfolder = share_and_subfolder(str(metadata.get("share") or ""))
    return "/".join(["/", str(metadata.get("server") or "").strip(), share, *([subfolder] if subfolder else [])])


def current_mount(record: dict[str, Any], *, trial: bool = False) -> Mount | None:
    point = Path(mount_point(str(record.get("id") or ""), trial=trial))
    found = [mount for mount in read_mounts() if mount.point == point]
    return found[-1] if found else None


def _same_source(mount: Mount, record: dict[str, Any]) -> bool:
    wanted = unc_parts(expected_source(record))
    have = unc_parts(mount.source)
    return (
        mount.fstype in SMB_FILESYSTEMS
        and len(wanted) == len(have)
        and bool(have)
        and same_server(have[0], wanted[0])
        and [part.lower() for part in have[1:]] == [part.lower() for part in wanted[1:]]
    )


def is_mounted(record: dict[str, Any]) -> bool:
    mount = current_mount(record)
    return mount is not None and _same_source(mount, record)


def problem(record_id: str) -> str | None:
    with LOCK:
        state = STATE.get(record_id) or {}
    return state.get("error")


def _note(record_id: str, error: str | None) -> None:
    with LOCK:
        if error is None:
            STATE.pop(record_id, None)
        else:
            STATE[record_id] = {**(STATE.get(record_id) or {}), "error": error, "at": utc_now()}


def _password(record: dict[str, Any]) -> str:
    password = str(_metadata(record).get("password") or "")
    if not password:
        raise api_error(422, "invalid_repository", f"Repository {record.get('name')} has no password to mount its share with; edit it with a password")
    if is_encrypted(password):
        raise api_error(
            503,
            "repository_password_unreadable",
            f"The password of repository {record.get('name')} cannot be decrypted with this server's credential key, so its "
            "share cannot be mounted; edit the repository with its password again",
        )
    return password


def _broker_error(record: dict[str, Any], exc: Exception, action: str) -> ApiError:
    name = record.get("name") or record.get("id")
    if isinstance(exc, broker_client.BrokerUnavailable):
        return api_error(
            503,
            "mounts_broker_unavailable",
            f"The share of repository {name} could not be {action}: the mounts broker is not reachable ({exc}). "
            "Check systemctl status altastor-backup-mounts.socket",
        )
    assert isinstance(exc, broker_client.BrokerRefused)
    if exc.code == "busy":
        return api_error(409, "repository_share_busy", f"The share of repository {name} is in use, so it is not {action}: {exc.message}")
    return api_error(422, "share_mount_failed", f"The share of repository {name} could not be {action}: {exc.message}")


def mount(record: dict[str, Any], *, trial: bool = False) -> dict[str, Any]:
    metadata = _metadata(record)
    arguments = mount_arguments(
        str(record.get("id") or ""),
        str(metadata.get("server") or ""),
        str(metadata.get("share") or ""),
        str(metadata.get("username") or ""),
        _password(record),
        trial=trial,
    )
    try:
        return call("mount", arguments)
    except (broker_client.BrokerUnavailable, broker_client.BrokerRefused) as exc:
        raise _broker_error(record, exc, "mounted") from exc


def unmount(record: dict[str, Any], *, trial: bool = False) -> dict[str, Any]:
    arguments: dict[str, Any] = {"repository_id": str(record.get("id") or "")}
    if trial:
        arguments["trial"] = True
    try:
        return call("unmount", arguments)
    except (broker_client.BrokerUnavailable, broker_client.BrokerRefused) as exc:
        raise _broker_error(record, exc, "unmounted") from exc


def ensure_mounted(record: dict[str, Any]) -> None:
    record_id = str(record.get("id") or "")
    with LOCK:
        if is_mounted(record):
            _note(record_id, None)
            return
        try:
            if current_mount(record) is not None:
                unmount(record)
            mount(record)
        except ApiError as exc:
            _note(record_id, exc.message)
            if exc.code != "repository_share_busy":
                mount_in_background()
            raise
        _note(record_id, None)


def refuse_path_edit(record: dict[str, Any], payload: dict[str, Any]) -> None:
    if is_managed(record) and payload.get("path") is not None and str(payload.get("path")).strip() != str(record.get("path") or ""):
        raise api_error(
            422,
            "invalid_repository",
            f"Repository {record.get('name')} is a share this server mounts itself at {record.get('path')}; it takes no path. "
            "Change address, username or password, or add the share mounted by hand as a new repository with path",
        )


def _mount_fields(record: dict[str, Any]) -> tuple[str, ...]:
    metadata = _metadata(record)
    return tuple(str(metadata.get(name) or "") for name in ("server", "share", "username", "password"))


def _relative_point_files(record: dict[str, Any]) -> list[Path]:
    from python.srv.services.abr_v41 import normalized_backup_points

    root = Path(str(record.get("path") or ""))
    found: list[Path] = []
    for point in normalized_backup_points():
        if str(point.get("repository_id") or "") != str(record.get("id") or ""):
            continue
        artifact = point.get("artifact") if isinstance(point.get("artifact"), dict) else {}
        for disk in artifact.get("disks") or []:
            location = Path(str((disk or {}).get("abr_path") or ""))
            if location.is_absolute() and root in location.parents:
                found.append(location.relative_to(root))
                break
        if len(found) >= SAMPLE_POINTS:
            break
    return found


def _holds_the_backups(record: dict[str, Any], trial_point: Path) -> bool:
    expected = _relative_point_files(record)
    return not expected or any((trial_point / relative).exists() for relative in expected)


def apply_change(before: dict[str, Any], after: dict[str, Any]) -> None:
    if not is_managed(after) or _mount_fields(before) == _mount_fields(after):
        return
    from python.srv.services.repository_shares import refuse_while_sessions_use

    refuse_while_sessions_use(before, action="moved to another share or account")
    with LOCK:
        result = mount(after, trial=True)
        try:
            trial_point = Path(str(result.get("mount_point") or mount_point(str(after.get("id") or ""), trial=True)))
            if not _holds_the_backups(before, trial_point):
                raise api_error(
                    409,
                    "repository_share_mismatch",
                    f"The share {expected_source(after)} does not hold the backups of repository {before.get('name')}: none of "
                    "its restore points is there. Give the share the repository is on, or add the new share as a new repository",
                )
        finally:
            unmount(after, trial=True)
        if current_mount(before) is not None:
            unmount(before)
        try:
            mount(after)
        except ApiError:
            try:
                mount(before)
            except ApiError:
                logger.exception("The share of repository %s could not be mounted again with its old settings", before.get("name"))
            raise
        _note(str(after.get("id") or ""), None)


def revert_change(before: dict[str, Any], after: dict[str, Any]) -> None:
    if not is_managed(after) or _mount_fields(before) == _mount_fields(after):
        return
    record_id = str(before.get("id") or "")
    with LOCK:
        try:
            if current_mount(after) is not None:
                unmount(after)
            mount(before)
        except ApiError as exc:
            _note(record_id, exc.message)
            logger.warning(
                "The edit of repository %s was refused, and its share could not be mounted again with its old settings: %s",
                before.get("name"),
                exc.message,
            )
            if exc.code != "repository_share_busy":
                mount_in_background()
            return
        _note(record_id, None)


def remove(record: dict[str, Any]) -> None:
    if not is_managed(record):
        return
    with LOCK:
        unmount(record)
        _note(str(record.get("id") or ""), None)


def _managed_records() -> list[dict[str, Any]]:
    return [record for record in catalog_records(REPOSITORIES) if is_managed(record)]


def mount_all_once() -> dict[str, str]:
    failures: dict[str, str] = {}
    for record in _managed_records():
        try:
            adopted = is_mounted(record)
            ensure_mounted(record)
            if adopted:
                logger.info("The share of repository %s was already mounted; it is kept", record.get("name"))
            else:
                logger.info("Mounted the share of repository %s at %s", record.get("name"), record.get("path"))
        except ApiError as exc:
            failures[str(record.get("id") or "")] = exc.message
            logger.warning("The share of repository %s is not mounted: %s", record.get("name"), exc.message)
    return failures


def _keep_mounting(sleep: Any = time.sleep) -> None:
    attempt = 0
    while True:
        failures = mount_all_once()
        if not failures:
            return
        pause = RETRY_PAUSES_SECONDS[min(attempt, len(RETRY_PAUSES_SECONDS) - 1)]
        next_at = datetime.now(timezone.utc) + timedelta(seconds=pause)
        with LOCK:
            for record_id in failures:
                STATE.setdefault(record_id, {})["next_attempt_at"] = next_at.isoformat(timespec="seconds")
        attempt += 1
        sleep(pause)


RETRIER: dict[str, threading.Thread] = {}


def _retry_until_mounted() -> None:
    try:
        _keep_mounting()
    finally:
        with LOCK:
            RETRIER.pop("thread", None)


def mount_in_background() -> threading.Thread:
    with LOCK:
        running = RETRIER.get("thread")
        if running is not None and running.is_alive():
            return running
        thread = threading.Thread(target=_retry_until_mounted, name="repository-mounts", daemon=True)
        RETRIER["thread"] = thread
    thread.start()
    return thread


SMB_PORTS = (445, 139)
REACH_SECONDS = 5


def server_answers(server: str, *, timeout: float = REACH_SECONDS) -> bool:
    for port in SMB_PORTS:
        try:
            with socket.create_connection((server, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def share_lost_message(repository: dict[str, Any] | None) -> str | None:
    if not isinstance(repository, dict) or str(repository.get("type") or "").lower() != "smb":
        return None
    server = str(_metadata(repository).get("server") or "").strip()
    if not server or server_answers(server):
        return None
    name = repository.get("name") or repository.get("id")
    return (
        f"Writing to repository {name!r} at {repository.get('path')} failed because the server {server} of its share stopped "
        f"answering (port {' or '.join(str(port) for port in SMB_PORTS)}). The write reported no space left, which is how the "
        "SMB client reports a lost server, not a full share; check the network and the server, then run it again"
    )
