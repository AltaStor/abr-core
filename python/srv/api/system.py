# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from fastapi import Body, Query, Request

from python.srv.services import (
    abr_v41,
    clock_guard,
    support_bundle_service,
    system_log_service,
    tls_certificate_service,
)

from ._routing import build_router, register
from .schemas import (
    CatalogRestoreRequest,
    CatalogRestoreResult,
    CatalogSnapshotRequest,
    CatalogSnapshotResult,
    SupportBundleResult,
    SystemClockDTO,
    SystemHealthDTO,
    SystemInfoDTO,
    SystemLogsDTO,
    SystemVersionDTO,
    TlsCertificateChangeDTO,
    TlsCertificateDTO,
    TlsCertificateInstallRequest,
    unset_excluded,
)

router = build_router("/system", tag="system")


def system_version() -> SystemVersionDTO:
    return abr_v41.system_version()


def system_info() -> SystemInfoDTO:
    return abr_v41.system_info()


def system_health() -> SystemHealthDTO:
    """Report per-component health.

    Always answers 200, including while a dependency is down: `status` is "ok"
    or "degraded", and `components` names the state of each part (the catalog
    database reports "ok", "unavailable", or "not_configured"). Monitoring can
    branch on `status` alone; a non-200 from this endpoint means the API itself
    is unreachable, never that a dependency is.
    """
    return abr_v41.system_health()


def system_clock() -> SystemClockDTO:
    return clock_guard.status()


def confirm_system_clock(request: Request) -> SystemClockDTO:
    result = clock_guard.confirm(getattr(request.state, "user_name", None))
    audit = getattr(request.state, "audit", None)
    if audit is not None:
        audit.details["was_blocked"] = result["was_blocked"]
        audit.details["confirmed_time"] = result["server_time"]
    return result


def system_logs(
    limit: int | None = Query(default=None, ge=1, le=system_log_service.MAX_LIMIT),
    level: str | None = Query(default=None, pattern="^(debug|info|warn|error)$"),
) -> SystemLogsDTO:
    """Newest-first tail of the appliance's own server log.

    This is the server's log (`server.log`), not a session's: a session's lines
    come from GET /sessions/{id}/log. `level` is a floor, so `warn` answers
    warnings and errors together. The response names the files it actually read
    and, when nothing was found, the directories it looked in -- a log the API
    cannot reach is a deployment fact worth reporting rather than an empty list.
    """
    return system_log_service.read_system_log(limit=limit, level=level)


def export_catalog_snapshot(
    payload: CatalogSnapshotRequest = Body(default_factory=CatalogSnapshotRequest),
) -> CatalogSnapshotResult:
    return abr_v41.export_catalog_snapshot(unset_excluded(payload))


def import_catalog_snapshot(
    payload: CatalogRestoreRequest = Body(default_factory=CatalogRestoreRequest),
) -> CatalogRestoreResult:
    return abr_v41.import_catalog_snapshot(unset_excluded(payload))


def get_support_bundle() -> SupportBundleResult:
    return support_bundle_service.generate_support_bundle()


def get_tls_certificate() -> TlsCertificateDTO:
    """The certificate the console and API are served over HTTPS with.

    `source` is "generated" for the appliance's own self-signed certificate
    (renewed automatically) or "administrator" for one installed with PUT or
    placed over the certificate files by hand (never renewed or replaced by the
    appliance). `warnings` lists what a browser would object to, such as an
    approaching expiry or none of the appliance's names being in the certificate.
    """
    return tls_certificate_service.get_certificate()


def install_tls_certificate(
    request: Request,
    payload: TlsCertificateInstallRequest = Body(default_factory=TlsCertificateInstallRequest),
) -> TlsCertificateChangeDTO:
    """Replace the HTTPS certificate and private key.

    Everything is validated before anything on disk changes: the PEM parses, the
    key belongs to the certificate, the certificate is currently valid and
    issued for TLS servers, and any intermediates are in order. The new pair is
    loaded into the running server, so new connections get it without a
    restart (`reloaded`). The passphrase of an encrypted key is used once to
    read it and is not stored.
    """
    body = unset_excluded(payload)
    return tls_certificate_service.install_certificate(
        body.get("certificate"),
        body.get("private_key"),
        body.get("passphrase"),
        installed_by=getattr(request.state, "user_name", None),
    )


def revert_tls_certificate(request: Request) -> TlsCertificateChangeDTO:
    """Remove the administrator's certificate and go back to a self-signed one.

    A new key and self-signed certificate are generated and loaded into the
    running server. Answers with the current certificate unchanged when it is
    already the appliance's own.
    """
    return tls_certificate_service.revert_to_generated_certificate(
        requested_by=getattr(request.state, "user_name", None),
    )


register(router, [
    ("/version", system_version, ["GET"], "system.view"),
    ("/info", system_info, ["GET"], "system.view"),
    ("/health", system_health, ["GET"], "system.view"),
    ("/clock", system_clock, ["GET"], "system.view"),
    ("/clock/confirm", confirm_system_clock, ["POST"], "system.clock_confirm"),
    ("/logs", system_logs, ["GET"], "system.logs"),
    ("/catalog-snapshot", export_catalog_snapshot, ["POST"], "system.export"),
    ("/catalog-restore", import_catalog_snapshot, ["POST"], "system.import"),
    ("/support-bundle", get_support_bundle, ["POST"], "system.support_bundle"),
    ("/tls-certificate", get_tls_certificate, ["GET"], "tls.view"),
    ("/tls-certificate", install_tls_certificate, ["PUT"], "tls.edit"),
    ("/tls-certificate", revert_tls_certificate, ["DELETE"], "tls.edit"),
])
