# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from python.srv.core.mongo_db_wrapper import (
    REPOSITORIES,
    catalog_records,
    get_database,
    persist_repository,
)
from python.srv.core.storage_tool import APP_ROOT, REPO_ROOT
from python.srv.core.utils import utc_now
from python.srv.services import abr_v41, repository_shares
from python.srv.services.api_errors import ApiError, api_error

logger = logging.getLogger(__name__)

DEFAULT_REPOSITORY_NAME = "Default Repo"
DEFAULT_REPOSITORY_PATH = "/var/lib/altastor-backup/repository"
PATH_ENV = "ABR_DEFAULT_REPOSITORY_PATH"
DEFAULT_MARK = "default_repository"
HYPERV_MARK = repository_shares.SHARE_MARK
DESCRIPTION = "Created automatically by AltaStor Backup & Replication"

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


def set_hyperv_support(enabled: bool) -> tuple[dict[str, Any], bool]:
    with _LOCK:
        record = find_default_repository()
        if record is None:
            raise api_error(404, "default_repository_missing", f"There is no {DEFAULT_REPOSITORY_NAME} to change")
        if repository_shares.is_shared(record) == enabled:
            return abr_v41.normalize_repository_record(record), False
        updated = repository_shares.share_repository(record) if enabled else repository_shares.unshare_repository(record)
        return abr_v41.normalize_repository_record(updated), True
