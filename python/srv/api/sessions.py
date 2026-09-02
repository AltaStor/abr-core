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
) -> SessionList:
    """Newest-first page of sessions.

    ``limit``/``offset`` page the result; omitting ``limit`` still returns the
    whole list, so existing clients (and `abr session list`) keep working. The
    envelope always reports ``total``, so a caller that does page knows how far
    to walk. Items carry no ``logs`` -- GET /sessions/{id}/log serves those.
    """
    return abr_v41.list_sessions_page(job_name=job, limit=limit, offset=offset)


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
