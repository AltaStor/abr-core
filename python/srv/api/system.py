# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from typing import Any

from fastapi import Body, Query

from python.srv.services import abr_v41, support_bundle_service, system_log_service

from ._routing import body_dict, build_router, register

router = build_router("/system", tag="system")


def system_version() -> dict[str, Any]:
    return abr_v41.system_version()


def system_info() -> dict[str, Any]:
    return abr_v41.system_info()


def system_health() -> dict[str, Any]:
    """Report per-component health.

    Always answers 200, including while a dependency is down: `status` is "ok"
    or "degraded", and `components` names the state of each part (the catalog
    database reports "ok", "unavailable", or "not_configured"). Monitoring can
    branch on `status` alone; a non-200 from this endpoint means the API itself
    is unreachable, never that a dependency is.
    """
    return abr_v41.system_health()


def system_logs(
    limit: int | None = Query(default=None, ge=1, le=system_log_service.MAX_LIMIT),
    level: str | None = Query(default=None, pattern="^(debug|info|warn|error)$"),
) -> dict[str, Any]:
    """Newest-first tail of the appliance's own server log.

    This is the server's log (`server.log`), not a session's: a session's lines
    come from GET /sessions/{id}/log. `level` is a floor, so `warn` answers
    warnings and errors together. The response names the files it actually read
    and, when nothing was found, the directories it looked in -- a log the API
    cannot reach is a deployment fact worth reporting rather than an empty list.
    """
    return system_log_service.read_system_log(limit=limit, level=level)


def export_catalog_snapshot(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return abr_v41.export_catalog_snapshot(body_dict(payload))


def import_catalog_snapshot(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return abr_v41.import_catalog_snapshot(body_dict(payload))


def get_support_bundle() -> dict[str, Any]:
    return support_bundle_service.generate_support_bundle()


register(router, [
    ("/version", system_version, ["GET"]),
    ("/info", system_info, ["GET"]),
    ("/health", system_health, ["GET"]),
    ("/logs", system_logs, ["GET"]),
    ("/catalog-snapshot", export_catalog_snapshot, ["POST"]),
    ("/catalog-restore", import_catalog_snapshot, ["POST"]),
    ("/support-bundle", get_support_bundle, ["POST"]),
])
