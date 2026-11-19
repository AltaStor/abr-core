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
from python.srv.services import abr_v41, default_repository, repository_shares
from python.srv.services.api_errors import ApiError, api_error

logger = logging.getLogger(__name__)

INITIAL_REPOSITORY_NAME = "Initial Repo"
# What the repository was called until 26.12; a catalog that still has it under this name gets the new one at startup.
FORMER_NAME = "Default Repo"
DEFAULT_REPOSITORY_PATH = "/var/lib/altastor-backup/repository"
PATH_ENV = "ABR_DEFAULT_REPOSITORY_PATH"
INITIAL_MARK = abr_v41.INITIAL_REPOSITORY_MARK
DEFAULT_MARK = abr_v41.DEFAULT_REPOSITORY_MARK
HYPERV_MARK = repository_shares.SHARE_MARK
DESCRIPTION = "Created automatically by AltaStor Backup & Replication"

_LOCK = threading.Lock()


def initial_repository_path() -> Path:
    path = Path(os.environ.get(PATH_ENV, "").strip() or DEFAULT_REPOSITORY_PATH).expanduser()
    if _can_create(path) or _is_installed():
        return path
    fallback = REPO_ROOT / "backups" / "default-repo"
    logger.warning(
        "The %s folder %s cannot be created by this user; using %s instead. Expected in a dev checkout; "
        "a real install gives the service user /var/lib/altastor-backup.",
        INITIAL_REPOSITORY_NAME,
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


def is_initial_repository(record: dict[str, Any] | None) -> bool:
    metadata = record.get("metadata") if isinstance(record, dict) and isinstance(record.get("metadata"), dict) else {}
    return abr_v41.is_initial_repository_metadata(metadata)


def find_initial_repository() -> dict[str, Any] | None:
    for record in catalog_records(REPOSITORIES):
        if is_initial_repository(record):
            return record
    return None


def _named_initial(path: Path) -> dict[str, Any] | None:
    for record in catalog_records(REPOSITORIES):
        if str(record.get("name") or "").lower() != INITIAL_REPOSITORY_NAME.lower():
            continue
        same_folder = str(record.get("type") or "").lower() == "local" and _same_path(record.get("path"), path)
        if not same_folder:
            raise api_error(
                409,
                "initial_repository_name_taken",
                f"A repository named {INITIAL_REPOSITORY_NAME} already exists and stores to "
                f"{record.get('path') or record.get('location')}, not to {path}. Rename it, and the server creates "
                f"the {INITIAL_REPOSITORY_NAME} at its next start",
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
            "initial_repository_path_unavailable",
            f"The folder of the {INITIAL_REPOSITORY_NAME}, {path}, could not be created: {exc.strerror or exc}. "
            f"Set {PATH_ENV} to a folder this server can write to",
            path=str(path),
        ) from exc


def _mark(record: dict[str, Any]) -> dict[str, Any]:
    marked = dict(record)
    metadata = dict(marked.get("metadata") if isinstance(marked.get("metadata"), dict) else {})
    metadata[INITIAL_MARK] = True
    metadata.setdefault(DEFAULT_MARK, default_repository.find_default_repository() is None)
    metadata.setdefault(HYPERV_MARK, False)
    marked["metadata"] = metadata
    marked["updated_at"] = utc_now()
    persist_repository(marked)
    return marked


def _share(record: dict[str, Any]) -> None:
    """Share the new repository's folder like every local repository's; without Samba or its broker it stays a
    local repository, which can be shared later."""
    stored = REPOSITORIES.get(str(record.get("id") or "")) or record
    try:
        repository_shares.share_repository(stored)
    except ApiError as exc:
        logger.warning("The %s is not shared: %s", INITIAL_REPOSITORY_NAME, exc.message)


def ensure_initial_repository() -> tuple[dict[str, Any], bool]:
    with _LOCK:
        existing = find_initial_repository()
        if existing is not None:
            return abr_v41.normalize_repository_record(existing), False
        path = initial_repository_path()
        adopted = _named_initial(path)
        if adopted is not None:
            logger.info("Repository %s at %s is now the initial repository.", adopted.get("name"), path)
            return abr_v41.normalize_repository_record(_mark(adopted)), False
        _create_folder(path)
        created = abr_v41.create_repository_record(
            {"name": INITIAL_REPOSITORY_NAME, "type": "local", "path": str(path), "description": DESCRIPTION},
            metadata_flags={
                INITIAL_MARK: True,
                DEFAULT_MARK: default_repository.find_default_repository() is None,
                HYPERV_MARK: False,
            },
        )
        logger.info("Created the %s at %s.", INITIAL_REPOSITORY_NAME, path)
        _share(created)
        stored = REPOSITORIES.get(str(created.get("id") or ""))
        return (abr_v41.normalize_repository_record(stored) if stored else created), True


def rename_former_initial_repository() -> None:
    record = find_initial_repository()
    if record is None or str(record.get("name") or "").lower() != FORMER_NAME.lower():
        return
    if any(str(other.get("name") or "").lower() == INITIAL_REPOSITORY_NAME.lower() for other in catalog_records(REPOSITORIES)):
        logger.warning(
            "The %s keeps its name %s: another repository is already named %s.",
            INITIAL_REPOSITORY_NAME,
            FORMER_NAME,
            INITIAL_REPOSITORY_NAME,
        )
        return
    renamed = {**record, "name": INITIAL_REPOSITORY_NAME, "updated_at": utc_now()}
    persist_repository(renamed)
    logger.info("Renamed %s to %s.", FORMER_NAME, INITIAL_REPOSITORY_NAME)


def create_initial_repository_at_startup() -> None:
    if get_database() is None:
        logger.warning(
            "The catalog database is unavailable, so the %s is not checked at startup; it is created at the "
            "next start",
            INITIAL_REPOSITORY_NAME,
        )
        return
    with _LOCK:
        rename_former_initial_repository()
    try:
        ensure_initial_repository()
    except ApiError as exc:
        logger.warning("The %s was not created: %s", INITIAL_REPOSITORY_NAME, exc.message)
