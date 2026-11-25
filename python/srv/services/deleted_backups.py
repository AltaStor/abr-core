# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from python.srv.core.mongo_db_wrapper import (
    load_system_state,
    persist_backup,
    persist_system_state,
)
from python.srv.core.utils import utc_now
from python.srv.services.api_errors import api_error

LOGGER = logging.getLogger(__name__)

SETTINGS_ID = "deleted_backups"
POINT_IN_BIN = "deleted"
MAX_KEEP_DAYS = 365
BIN_FIELDS = ("deleted_at", "deleted_by", "purge_after", "status_before_delete")


def keep_days() -> int:
    state = load_system_state(SETTINGS_ID) or {}
    try:
        return max(0, int(state.get("keep_days") or 0))
    except (TypeError, ValueError):
        return 0


def settings_view() -> dict[str, Any]:
    state = load_system_state(SETTINGS_ID) or {}
    return {"keep_days": keep_days(), "updated_at": state.get("updated_at"), "updated_by": state.get("updated_by")}


def set_keep_days(value: Any, *, user: str | None = None) -> dict[str, Any]:
    raw = value
    whole = isinstance(raw, int) and not isinstance(raw, bool)
    valid = whole or (isinstance(raw, str) and raw.strip().isdigit())
    days = int(raw) if valid else -1
    if not 0 <= days <= MAX_KEEP_DAYS:
        raise api_error(
            422,
            "invalid_deleted_backups_settings",
            f"keep_days must be a whole number from 0 to {MAX_KEEP_DAYS}: the days a deleted backup stays in the recycle bin, 0 to delete at once",
            keep_days=raw,
        )
    persist_system_state({"id": SETTINGS_ID, "keep_days": days, "updated_at": utc_now(), "updated_by": user})
    return settings_view()


def in_bin(point: dict[str, Any]) -> bool:
    return str(point.get("status") or "") == POINT_IN_BIN


def _parse(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def move_to_bin(points: list[dict[str, Any]], days: int, *, user: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    purge_after = (now + timedelta(days=days)).isoformat()
    for point in points:
        if in_bin(point):
            continue
        persist_backup(
            {
                **point,
                "status_before_delete": point.get("status"),
                "status": POINT_IN_BIN,
                "deleted_at": now.isoformat(),
                "deleted_by": user,
                "purge_after": purge_after,
            }
        )
    return purge_after


def bring_back(points: list[dict[str, Any]]) -> int:
    brought = 0
    for point in points:
        if not in_bin(point):
            continue
        persist_backup({**point, **dict.fromkeys(BIN_FIELDS), "status": point.get("status_before_delete") or "available"})
        brought += 1
    return brought


def bin_view(points: list[dict[str, Any]]) -> dict[str, Any] | None:
    binned = [point for point in points if in_bin(point)]
    if not binned:
        return None
    return {
        "restore_points": len(binned),
        "deleted_at": min(str(point.get("deleted_at") or "") for point in binned),
        "deleted_by": next((point.get("deleted_by") for point in binned if point.get("deleted_by")), None),
        "purge_after": min(str(point.get("purge_after") or "") for point in binned),
        "whole_chain": len(binned) == len(points),
    }


def purge_due(point: dict[str, Any], now: datetime) -> bool:
    purge_after = _parse(point.get("purge_after"))
    return in_bin(point) and purge_after is not None and purge_after <= now


def restore_refusal(point: dict[str, Any]) -> str:
    return (
        f"Restore point is in the recycle bin: abr backup delete moved it there on {point.get('deleted_at')}"
        + (f" ({point.get('deleted_by')})" if point.get("deleted_by") else "")
        + f", and it is deleted for good after {point.get('purge_after')}. abr backup undelete --id "
        f"{point.get('chain_id')} brings it back for restore"
    )
