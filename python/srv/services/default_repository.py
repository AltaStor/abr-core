# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import logging
import os
import secrets
import socket
import string
import subprocess
import threading
from pathlib import Path
from typing import Any

from python.srv.core.credential_encryption import (
    CredentialKeyUnavailable,
    encrypt_secret,
)
from python.srv.core.mongo_db_wrapper import (
    REPOSITORIES,
    catalog_records,
    get_database,
    persist_repository,
)
from python.srv.core.storage_tool import APP_ROOT, REPO_ROOT
from python.srv.core.utils import utc_now
from python.srv.services import abr_v41
from python.srv.services.api_errors import ApiError, api_error

logger = logging.getLogger(__name__)

DEFAULT_REPOSITORY_NAME = "Default Repo"
DEFAULT_REPOSITORY_PATH = "/var/lib/altastor-backup/repository"
PATH_ENV = "ABR_DEFAULT_REPOSITORY_PATH"
DEFAULT_MARK = "default_repository"
HYPERV_MARK = "support_hyperv"
DESCRIPTION = "Created automatically by AltaStor Backup & Replication"
HELPER_ENV = "ABR_SAMBA_HELPER"
DEFAULT_HELPER = str(Path(__file__).resolve().parents[1] / "plugins" / "hyperv" / "smb_share" / "abr-samba-helper.sh")
HELPER_TIMEOUT_SECONDS = 120
SHARE_FIELDS = ("server", "share", "username", "password")
_NAME_ALPHABET = string.ascii_lowercase + string.digits
_PASSWORD_ALPHABET = string.ascii_letters + string.digits

_LOCK = threading.Lock()


def default_repository_path() -> Path:
    path = Path(os.environ.get(PATH_ENV, "").strip() or DEFAULT_REPOSITORY_PATH).expanduser()
    if _can_create(path) or _is_installed():
        return path
    fallback = REPO_ROOT / "backups" / "default-repo"
    logger.warning(
        "The %s folder %s cannot be created by this user; using %s instead. Expected in a dev checkout; "
        "a real install gives the service user /var/lib/altastor-backup.",
        DEFAULT_REPOSITORY_NAME,
        path,
        fallback,
    )
    return fallback


def _can_create(path: Path) -> bool:
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    return existing.is_dir() and os.access(existing, os.W_OK | os.X_OK)


def _is_installed() -> bool:
    return REPO_ROOT.resolve() == APP_ROOT.resolve()


def is_default_repository(record: dict[str, Any] | None) -> bool:
    metadata = record.get("metadata") if isinstance(record, dict) and isinstance(record.get("metadata"), dict) else {}
    return bool(metadata.get(DEFAULT_MARK))


def find_default_repository() -> dict[str, Any] | None:
    for record in catalog_records(REPOSITORIES):
        if is_default_repository(record):
            return record
    return None


def get_default_repository() -> dict[str, Any]:
    record = find_default_repository()
    if record is None:
        raise api_error(
            404,
            "default_repository_missing",
            f"There is no {DEFAULT_REPOSITORY_NAME}. Create it with POST /api/v1/repositories/default, "
            "or restart the server, which creates it when it is missing",
        )
    return abr_v41.normalize_repository_record(record)


def _named_default(path: Path) -> dict[str, Any] | None:
    for record in catalog_records(REPOSITORIES):
        if str(record.get("name") or "").lower() != DEFAULT_REPOSITORY_NAME.lower():
            continue
        same_folder = str(record.get("type") or "").lower() == "local" and _same_path(record.get("path"), path)
        if not same_folder:
            raise api_error(
                409,
                "default_repository_name_taken",
                f"A repository named {DEFAULT_REPOSITORY_NAME} already exists and stores to "
                f"{record.get('path') or record.get('location')}, not to {path}. Rename it, then create the "
                f"default repository again",
                repository_id=record.get("id"),
            )
        return record
    return None


def _same_path(value: Any, path: Path) -> bool:
    try:
        return Path(str(value or "")).expanduser().resolve() == path.resolve()
    except OSError:
        return False


def _create_folder(path: Path) -> None:
    try:
        path.mkdir(mode=0o750, parents=True, exist_ok=True)
    except OSError as exc:
        raise api_error(
            422,
            "default_repository_path_unavailable",
            f"The folder of the {DEFAULT_REPOSITORY_NAME}, {path}, could not be created: {exc.strerror or exc}. "
            f"Set {PATH_ENV} to a folder this server can write to",
            path=str(path),
        ) from exc


def _mark(record: dict[str, Any]) -> dict[str, Any]:
    marked = dict(record)
    metadata = dict(marked.get("metadata") if isinstance(marked.get("metadata"), dict) else {})
    metadata[DEFAULT_MARK] = True
    metadata.setdefault(HYPERV_MARK, False)
    marked["metadata"] = metadata
    marked["updated_at"] = utc_now()
    persist_repository(marked)
    return marked


def ensure_default_repository() -> tuple[dict[str, Any], bool]:
    with _LOCK:
        existing = find_default_repository()
        if existing is not None:
            return abr_v41.normalize_repository_record(existing), False
        path = default_repository_path()
        adopted = _named_default(path)
        if adopted is not None:
            logger.info("Repository %s at %s is now the default repository.", adopted.get("name"), path)
            return abr_v41.normalize_repository_record(_mark(adopted)), False
        _create_folder(path)
        created = abr_v41.create_repository_record(
            {"name": DEFAULT_REPOSITORY_NAME, "type": "local", "path": str(path), "description": DESCRIPTION},
            metadata_flags={DEFAULT_MARK: True, HYPERV_MARK: False},
        )
        logger.info("Created the %s at %s.", DEFAULT_REPOSITORY_NAME, path)
        return created, True


def create_default_repository_at_startup() -> None:
    if get_database() is None:
        logger.warning(
            "The catalog database is unavailable, so the %s is not checked at startup; it is created at the "
            "next start, or with POST /api/v1/repositories/default",
            DEFAULT_REPOSITORY_NAME,
        )
        return
    try:
        ensure_default_repository()
    except ApiError as exc:
        logger.warning("The %s was not created: %s", DEFAULT_REPOSITORY_NAME, exc.message)


def _token(alphabet: str, length: int) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _helper_failure(action: str, completed: subprocess.CompletedProcess[str], helper: str) -> ApiError:
    detail = next((line for line in reversed((completed.stderr or "").splitlines()) if line.strip()), "")
    detail = detail.removeprefix("abr-samba-helper: ").strip() or f"exit status {completed.returncode}"
    if "password is required" in detail or "not allowed" in detail or "sudo:" in detail:
        return api_error(
            503,
            "hyperv_share_not_permitted",
            f"The Hyper-V share of the {DEFAULT_REPOSITORY_NAME} could not be {action}: this server may not run "
            f"{helper} as root ({detail}). The altastor-backup package installs the sudo rule for it",
        )
    return api_error(
        500,
        "hyperv_share_failed",
        f"The Hyper-V share of the {DEFAULT_REPOSITORY_NAME} could not be {action}: {detail}",
    )


def _run_helper(command: str, action: str, fields: dict[str, str]) -> None:
    helper = os.environ.get(HELPER_ENV, "").strip() or DEFAULT_HELPER
    request = "".join(f"{key}={value}\n" for key, value in fields.items())
    try:
        completed = subprocess.run(
            ["sudo", "-n", helper, command],
            input=request,
            capture_output=True,
            text=True,
            timeout=HELPER_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise api_error(
            503,
            "hyperv_share_not_permitted",
            f"The Hyper-V share of the {DEFAULT_REPOSITORY_NAME} could not be {action}: {helper} could not be run "
            f"through sudo ({exc})",
        ) from exc
    if completed.returncode != 0:
        raise _helper_failure(action, completed, helper)


def _share_metadata(record: dict[str, Any]) -> dict[str, Any]:
    return dict(record.get("metadata") if isinstance(record.get("metadata"), dict) else {})


def _without_share(record: dict[str, Any]) -> dict[str, Any]:
    updated = dict(record)
    metadata = _share_metadata(record)
    for field in SHARE_FIELDS:
        metadata[field] = None
    metadata[HYPERV_MARK] = False
    updated["type"] = "local"
    updated["metadata"] = metadata
    updated["updated_at"] = utc_now()
    return updated


def _enable_hyperv(record: dict[str, Any]) -> dict[str, Any]:
    share = f"abr-{_token(_NAME_ALPHABET, 8)}$"
    user = f"abr-smb-{_token(_NAME_ALPHABET, 8)}"
    password = _token(_PASSWORD_ALPHABET, 40)
    try:
        stored_password = encrypt_secret(password)
    except CredentialKeyUnavailable as exc:
        raise api_error(503, "credential_key_unavailable", f"{exc}. The Hyper-V share is not created.") from exc
    folder = str(Path(str(record.get("path") or "")).resolve())
    _run_helper("enable-share", "created", {"share": share, "user": user, "password": password, "path": folder})
    updated = dict(record)
    metadata = _share_metadata(record)
    metadata.update(
        {"server": socket.getfqdn(), "share": share, "username": user, "password": stored_password, HYPERV_MARK: True}
    )
    updated["type"] = "smb"
    updated["metadata"] = metadata
    updated["updated_at"] = utc_now()
    try:
        persist_repository(updated)
    except Exception:
        _run_helper("disable-share", "removed again", {"share": share, "user": user})
        raise
    logger.info(
        "Hyper-V support is on for the %s: share %s for account %s on %s.", DEFAULT_REPOSITORY_NAME, share, user, folder
    )
    return updated


def _disable_hyperv(record: dict[str, Any]) -> dict[str, Any]:
    metadata = _share_metadata(record)
    share, user = str(metadata.get("share") or ""), str(metadata.get("username") or "")
    if share and user:
        _run_helper("disable-share", "removed", {"share": share, "user": user})
    updated = _without_share(record)
    persist_repository(updated)
    logger.info("Hyper-V support is off for the %s: share %s and account %s removed.", DEFAULT_REPOSITORY_NAME, share, user)
    return updated


def set_hyperv_support(enabled: bool) -> tuple[dict[str, Any], bool]:
    with _LOCK:
        record = find_default_repository()
        if record is None:
            raise api_error(404, "default_repository_missing", f"There is no {DEFAULT_REPOSITORY_NAME} to change")
        if bool(_share_metadata(record).get(HYPERV_MARK)) == enabled:
            return abr_v41.normalize_repository_record(record), False
        updated = _enable_hyperv(record) if enabled else _disable_hyperv(record)
        return abr_v41.normalize_repository_record(updated), True


def refuse_managed_share_edit(record: dict[str, Any], payload: dict[str, Any]) -> None:
    metadata = _share_metadata(record)
    if not (metadata.get(DEFAULT_MARK) and metadata.get(HYPERV_MARK)):
        return
    touched = sorted(field for field in (*SHARE_FIELDS, "address", "path", "type") if payload.get(field) is not None)
    if touched:
        raise api_error(
            409,
            "default_repository_share_managed",
            f"The share of the {DEFAULT_REPOSITORY_NAME} is managed by its Hyper-V switch, so "
            f"{', '.join(touched)} cannot be edited. Turn Hyper-V support off and on to get a new share and account",
            fields=touched,
        )


def remove_managed_share(record: dict[str, Any]) -> None:
    metadata = _share_metadata(record)
    if metadata.get(DEFAULT_MARK) and metadata.get(HYPERV_MARK):
        with _LOCK:
            _disable_hyperv(record)
