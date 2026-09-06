# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from typing import Any

from fastapi import Body, Query

from python.srv.services import abr_v41

from ._routing import build_router, register
from .schemas import SessionDTO, SessionList, StopSessionRequest, unset_excluded

router = build_router("/sessions", tag="sessions")


def list_sessions(
    job: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=abr_v41.SESSION_PAGE_LIMIT_MAX),
    offset: int = Query(default=0, ge=0),
    page: int | None = Query(default=None, ge=1),
    per_page: int | None = Query(default=None, ge=1, le=abr_v41.SESSION_PAGE_LIMIT_MAX),
    since: str | None = Query(default=None),
) -> SessionList:
    """Newest-first page of sessions.

    Two spellings of the same slice: ``limit``/``offset``, or ``page``/
    ``per_page`` for callers that count in pages. Passing ``page`` or
    ``per_page`` wins over the offset pair; ``since`` (an ISO timestamp) narrows
    the match set to runs started at or after it, which is how a client asks for
    only the window it renders instead of walking the whole history.

    Omitting all of them still returns the whole list, so existing clients (and
    `abr session list`) keep working. The envelope always reports ``total`` plus
    ``page``/``pages`` and ``next``/``prev`` links. Items carry no ``logs`` --
    GET /sessions/{id}/log serves those.
    """
    if page is not None or per_page is not None:
        limit = per_page or limit or abr_v41.SESSION_PAGE_DEFAULT
        offset = ((page or 1) - 1) * limit
    return abr_v41.list_sessions_page(job_name=job, limit=limit, offset=offset, since=since)


def get_session(session_id: str) -> SessionDTO:
    return abr_v41.get_session(session_id)


def get_session_log(session_id: str) -> dict[str, Any]:
    return abr_v41.get_session_log(session_id)


def stop_session(
    session_id: str, payload: StopSessionRequest = Body(default_factory=StopSessionRequest)
) -> dict[str, Any]:
    return abr_v41.stop_session(session_id, force=bool(unset_excluded(payload).get("force", False)))


register(router, [
    ("", list_sessions, ["GET"]),
    ("/{session_id}", get_session, ["GET"]),
    ("/{session_id}/log", get_session_log, ["GET"]),
    ("/{session_id}/stop", stop_session, ["POST"]),
])
