# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""The default repository: where a new backup job stores when none is named. At most one repository is the
default; it is marked on its record (metadata.default_repository), and nothing is the default once it is
deleted, until another one is set."""

from __future__ import annotations

import threading
from typing import Any

from python.srv.core.mongo_db_wrapper import REPOSITORIES, catalog_entries, catalog_records, persist_repository
from python.srv.core.utils import utc_now
from python.srv.services import abr_v41

DEFAULT_MARK = abr_v41.DEFAULT_REPOSITORY_MARK
INITIAL_MARK = abr_v41.INITIAL_REPOSITORY_MARK

_LOCK = threading.Lock()


def _metadata(record: dict[str, Any]) -> dict[str, Any]:
    return dict(record.get("metadata") if isinstance(record.get("metadata"), dict) else {})


def is_default_repository(record: dict[str, Any] | None) -> bool:
    return isinstance(record, dict) and bool(_metadata(record).get(DEFAULT_MARK))


def find_default_repository() -> dict[str, Any] | None:
    for record in catalog_records(REPOSITORIES):
        if is_default_repository(record):
            return record
    return None


def _with_mark(record: dict[str, Any], default: bool) -> dict[str, Any]:
    metadata = _metadata(record)
    # Written down before the default mark changes: a record without its own initial mark is read from that one.
    metadata[INITIAL_MARK] = abr_v41.is_initial_repository_metadata(metadata)
    metadata[DEFAULT_MARK] = default
    return {**record, "metadata": metadata, "updated_at": utc_now()}


def set_default_repository(record_id: str, default: bool) -> dict[str, Any]:
    """Make the repository the default (moving the mark off any other), or stop it being the default."""
    with _LOCK:
        record = abr_v41._find_by_id(catalog_records(REPOSITORIES), record_id, label="Repository")
        repository_id = str(record.get("id") or record_id)
        if default:
            for other_id, other in catalog_entries(REPOSITORIES):
                if str(other_id) != repository_id and is_default_repository(other):
                    persist_repository(_with_mark(other, False))
        if is_default_repository(record) != default:
            persist_repository(_with_mark(record, default))
        return abr_v41.resolve_repository(record_id=repository_id)
