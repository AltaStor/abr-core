# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from fastapi import Query

from python.srv.services import audit_service

from ._routing import build_router, register
from .schemas import AuditList

router = build_router("/audit", tag="audit")


def list_audit_records(
    limit: int | None = Query(default=None, ge=1, le=audit_service.LIST_LIMIT_MAX),
    offset: int = Query(default=0, ge=0),
    user: str | None = Query(default=None, description="Only records of this user name"),
    function: str | None = Query(
        default=None, description="A function ID, or an area with a trailing dot (job.) for all of its functions"
    ),
    result: str | None = Query(default=None, pattern="^(allowed|denied|failed)$"),
    since: str | None = Query(default=None, description="ISO 8601 timestamp; records at or after it"),
    until: str | None = Query(default=None, description="ISO 8601 timestamp; records at or before it"),
) -> AuditList:
    """Newest-first page of the audit log.

    One record per executed command and per user or role change, from every
    interface: who (user and the role they held then), what (function ID and
    target), the result (allowed, denied or failed), when (UTC) and, when the
    command started one, the session ID. Credentials are masked.
    """
    return audit_service.list_records(
        limit=limit, offset=offset, user=user, function=function, result=result, since=since, until=until
    )


register(router, [
    ("", list_audit_records, ["GET"], "audit.view"),
])
