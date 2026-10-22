# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import asyncio
import logging
import os
import ssl
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from python.srv.core.logger_conf import close_crash_reporting, setup_logging

setup_logging(component="srv", file_level="INFO")

logger = logging.getLogger(__name__)


def _load_apiver(path: Path) -> tuple[int, int]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return int(values["ABR_API_MAJOR"]), int(values["ABR_API_MINOR"])


SERVER_API_MAJOR, SERVER_API_MINOR = _load_apiver(Path(__file__).parent / "APIVER")
API_VERSION_HEADER = "X-ABR-API-Version"

import uvicorn

from .api import audit as audit_api
from .api import auth as auth_api
from .api import backups as backups_api
from .api import inventories as inventories_api
from .api import jobs as jobs_api
from .api import repositories as repositories_api
from .api import restore as restore_api
from .api import roles as roles_api
from .api import sessions as sessions_api
from .api import system as system_api
from .api import users as users_api
from .api.dashboard import router as dashboard_router
from .api.schemas import HealthDTO
from .core import tls_renewal
from .core.job_scheduler import JobScheduler
from .core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    RESOURCES_BY_HV,
    SESSIONS,
    TASKS,
    USERS,
    close_database,
    load_backups,
    load_jobs,
    load_registered_hypervisors,
    load_repositories,
    load_resources,
    load_sessions,
    load_tasks,
    load_users,
)
from .services import auth_service, backup_catalog, clock_guard, tls_certificate_service
from .services.abr_v41 import PRODUCT_NAME, product_version
from .core.server_defaults import env_int
from .core.task_pool import TaskProcessPool
from .services.api_errors import (
    ApiError,
    api_error,
    normalize_http_exception,
    validation_error_payload,
)


# Total worker threads in the task pool. Backups among them are further capped by
# MAX_CONCURRENT_BACKUPS so a small, MongoDB-co-located host stays responsive.
WORKER_COUNT = env_int("BACKUP_WORKER_COUNT")
MAX_CONCURRENT_BACKUPS = env_int("MAX_CONCURRENT_BACKUPS")
TASK_POOL: TaskProcessPool | None = None
JOB_SCHEDULER: JobScheduler | None = None
CERT_RENEWAL_TASK: asyncio.Task | None = None


@dataclass
class TlsRuntime:
    cert_path: Path
    key_path: Path
    ssl_context: ssl.SSLContext | None


TLS_RUNTIME: TlsRuntime | None = None
APP_ROOT = Path("/opt/altastor-backup-server")
REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT_ENV_VAR = "ALTASTOR_WEB_ROOT"
RESERVED_WEB_PATHS = {"/api", "/docs", "/health", "/openapi.json", "/redoc"}


def _web_root_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured_root = os.environ.get(WEB_ROOT_ENV_VAR)

    if configured_root:
        candidates.append(Path(configured_root).expanduser())

    candidates.append(APP_ROOT / "web")
    candidates.append(REPO_ROOT / "web" / "dist")
    return candidates


def resolve_web_root() -> Path | None:
    seen: set[str] = set()
    for candidate in _web_root_candidates():
        candidate_key = str(candidate)

        if candidate_key in seen:
            continue

        seen.add(candidate_key)
        if (candidate / "index.html").is_file():
            return candidate.resolve()
        
    return None


def _is_reserved_web_path(path: str) -> bool:
    if path in RESERVED_WEB_PATHS:
        return True
    return any(path.startswith(f"{prefix}/") for prefix in RESERVED_WEB_PATHS)


def _wants_html_response(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept or "application/xhtml+xml" in accept


def _resolve_web_file(web_root: Path, path: str) -> Path | None:
    relative_path = path.lstrip("/")
    if not relative_path:
        return None

    try:
        candidate = (web_root / relative_path).resolve()
        candidate.relative_to(web_root)
    except (OSError, ValueError):
        return None

    return candidate if candidate.is_file() else None


def configure_web_ui(app: FastAPI, web_root: Path | None = None) -> None:
    resolved_web_root = (
        web_root.resolve() if web_root is not None else resolve_web_root()
    )
    if resolved_web_root is None:
        logger.warning("Web UI index.html not found; API will run without web UI")
        return

    index_file = resolved_web_root / "index.html"
    if not index_file.is_file():
        logger.warning(
            "Web UI index.html not found at %s; API will run without web UI", index_file
        )
        return

    logger.info("Serving web UI from %s", resolved_web_root)
    assets_dir = resolved_web_root / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    # Browser navigations should land on the SPA even when legacy JSON routes
    # still exist at the same path for non-HTML API clients.
    @app.middleware("http")
    async def spa_route_middleware(request: Request, call_next):
        if request.method in {"GET", "HEAD"} and not _is_reserved_web_path(
            request.url.path
        ):
            requested_file = _resolve_web_file(resolved_web_root, request.url.path)
            if requested_file is not None:
                return FileResponse(requested_file)
            
            if request.method == "HEAD" or _wants_html_response(request):
                return FileResponse(index_file)
            
        return await call_next(request)

    @app.get("/", include_in_schema=False)
    async def serve_web_index() -> FileResponse:
        return FileResponse(index_file)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str, request: Request) -> FileResponse:
        path = f"/{full_path}" if full_path else "/"
        if _is_reserved_web_path(path):
            raise HTTPException(status_code=404, detail="Not found")

        requested_file = _resolve_web_file(resolved_web_root, path)
        if requested_file is not None:
            return FileResponse(requested_file)
        
        if _wants_html_response(request):
            return FileResponse(index_file)
        
        raise HTTPException(status_code=404, detail="Not found")


@asynccontextmanager
async def lifespan(_: FastAPI):
    global TASK_POOL
    global JOB_SCHEDULER
    global CERT_RENEWAL_TASK
    logger.info("BackupServer is starting up. Initializing resources...")

    try:
        HYPERVISORS.clear()
        HYPERVISORS.update(load_registered_hypervisors())
        JOBS.clear()
        JOBS.update(load_jobs())
        REPOSITORIES.clear()
        REPOSITORIES.update(load_repositories())
        try:
            typed = backup_catalog.backfill_smb_repository_types()
            if typed:
                logger.info("Turned %d local repository(ies) with an SMB share into SMB repositories.", typed)
        except Exception:
            logger.exception("SMB repository type backfill failed; continuing startup without it.")
        BACKUPS.clear()
        BACKUPS.update(load_backups())
        try:
            backfilled = backup_catalog.backfill_backup_detail_fields()
            if backfilled:
                logger.info("Backfilled compression/encryption details for %d backup point(s).", backfilled)
        except Exception:
            logger.exception("Backup detail backfill failed; continuing startup without it.")
        try:
            keyed = backup_catalog.backfill_backup_encryption_keys()
            if keyed:
                logger.info("Backfilled the encryption key onto %d encrypted backup point(s).", keyed)
        except Exception:
            logger.exception("Encryption key backfill failed; continuing startup without it.")
        try:
            listed = backup_catalog.backfill_job_encryption_keys()
            if listed:
                logger.info("Converted %d job(s) to the numbered encryption key list.", listed)
        except Exception:
            logger.exception("Job encryption key list backfill failed; continuing startup without it.")
        try:
            identified = backup_catalog.backfill_backup_encryption_key_ids()
            if identified:
                logger.info("Recorded the encryption key id on %d backup point(s).", identified)
        except Exception:
            logger.exception("Encryption key id backfill failed; continuing startup without it.")
        TASKS.clear()
        TASKS.update(load_tasks())
        try:
            abandoned = backup_catalog.fail_interrupted_tasks()
            if abandoned:
                logger.warning(
                    "Marked %d task(s) as failed: they were still running when the server last stopped.",
                    abandoned,
                )
        except Exception:
            logger.exception("Could not reconcile tasks left running by an earlier run; continuing startup.")
        RESOURCES_BY_HV.clear()
        RESOURCES_BY_HV.update(load_resources())
        USERS.clear()
        USERS.update(load_users())
        try:
            roled = auth_service.backfill_user_roles()
            if roled:
                logger.info("Gave %d user(s) from before roles existed the Administrator role.", roled)
        except Exception:
            logger.exception("User role backfill failed; continuing startup without it.")
        SESSIONS.clear()
        SESSIONS.update(load_sessions())

        for hypervisor_id in HYPERVISORS:
            RESOURCES_BY_HV.setdefault(hypervisor_id, {})
        logger.info(
            "Loaded %d hypervisors, %d repositories, %d backups, %d jobs, %d tasks, %d users from database.",
            len(HYPERVISORS),
            len(REPOSITORIES),
            len(BACKUPS),
            len(JOBS),
            len(TASKS),
            len(USERS),
        )

        auth_service.bootstrap_admin_if_needed()

        try:
            clock_guard.load()
        except Exception:
            logger.exception("Could not check the server clock against its last known time; continuing startup.")

        TASK_POOL = TaskProcessPool(
            WORKER_COUNT,
            max_concurrent_backups=MAX_CONCURRENT_BACKUPS,
        )
        TASK_POOL.start()
        logger.info(
            "Background task pool started with %d workers (max %d concurrent backups).",
            WORKER_COUNT,
            MAX_CONCURRENT_BACKUPS,
        )

        JOB_SCHEDULER = JobScheduler()
        JOB_SCHEDULER.start()
        logger.info("Background job scheduler started.")

        if TLS_RUNTIME is not None:
            tls_certificate_service.register_live_tls(
                TLS_RUNTIME.cert_path, TLS_RUNTIME.key_path, TLS_RUNTIME.ssl_context
            )
            CERT_RENEWAL_TASK = asyncio.create_task(
                tls_renewal.periodic_renewal_loop(
                    TLS_RUNTIME.cert_path, TLS_RUNTIME.key_path, TLS_RUNTIME.ssl_context
                )
            )
            logger.info("Background TLS certificate renewal check started.")

        # Discover VMs in the background as soon as an inventory is added, so VM
        # names are usable in `job create` without a manual `inventory rescan`.
        # Best-effort; disable with ABR_AUTO_DISCOVER_ON_ADD=0.
        from python.srv.services import abr_v41

        abr_v41.AUTO_DISCOVER_ON_INVENTORY_ADD = os.environ["ABR_AUTO_DISCOVER_ON_ADD"] != "0"
        logger.info(
            "Auto-discovery on inventory add: %s",
            "enabled" if abr_v41.AUTO_DISCOVER_ON_INVENTORY_ADD else "disabled",
        )
        yield
    except Exception as exc:
        logger.exception("Critical error during application lifespan: %s", exc)
        raise
    finally:
        logger.info("BackupServer is shutting down. Cleaning up resources...")
        # Reset the process-global toggle so it does not leak across app
        # instances (e.g. TestClient lifespans sharing one interpreter).
        from python.srv.services import abr_v41

        abr_v41.AUTO_DISCOVER_ON_INVENTORY_ADD = False
        if CERT_RENEWAL_TASK:
            logger.debug("Stopping TLS certificate renewal check...")
            CERT_RENEWAL_TASK.cancel()
            with suppress(asyncio.CancelledError):
                await CERT_RENEWAL_TASK
            CERT_RENEWAL_TASK = None
        tls_certificate_service.clear_live_tls()
        if JOB_SCHEDULER:
            logger.debug("Stopping job scheduler...")
            JOB_SCHEDULER.stop()
            JOB_SCHEDULER = None
        clock_guard.forget()
        if TASK_POOL:
            logger.debug("Stopping task pool...")
            TASK_POOL.stop(wait=True)
            TASK_POOL = None
        logger.debug("Closing database connection...")
        close_database()
        logger.info("Shutdown complete.")
        close_crash_reporting()


SECURITY_HEADER_KEYS: tuple[tuple[str, str], ...] = (
    ("X-Frame-Options", "ABR_SECURITY_HEADER_FRAME_OPTIONS"),
    ("Content-Security-Policy", "ABR_SECURITY_HEADER_CSP"),
    ("X-Content-Type-Options", "ABR_SECURITY_HEADER_CONTENT_TYPE_OPTIONS"),
    ("Referrer-Policy", "ABR_SECURITY_HEADER_REFERRER_POLICY"),
    ("Cache-Control", "ABR_SECURITY_HEADER_CACHE_CONTROL"),
)
HSTS_HEADER_KEY = ("Strict-Transport-Security", "ABR_SECURITY_HEADER_HSTS")


def security_headers_for(scheme: str) -> dict[str, str]:
    headers = {
        header: value
        for header, env_key in SECURITY_HEADER_KEYS
        if (value := os.environ.get(env_key, "").strip())
    }
    if scheme == "https":
        header, env_key = HSTS_HEADER_KEY
        if value := os.environ.get(env_key, "").strip():
            headers[header] = value
    return headers


app = FastAPI(
    title=PRODUCT_NAME,
    version=product_version(),
    description=(
        f"{PRODUCT_NAME} {product_version()}. Backup, replication and restore of virtual "
        f"machines. API v{SERVER_API_MAJOR}.{SERVER_API_MINOR}, served under "
        f"/api/v{SERVER_API_MAJOR} and answered with the {API_VERSION_HEADER} header."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def api_version_middleware(request: Request, call_next):
    client_header = request.headers.get(API_VERSION_HEADER)
    if client_header:
        try:
            client_major_str, client_minor_str = client_header.split(".", 1)
            client_major, client_minor = int(client_major_str), int(client_minor_str)
        except ValueError:
            client_major = client_minor = None
        if client_major is not None and client_major != SERVER_API_MAJOR:
            error = api_error(
                400,
                "api_version_incompatible",
                f"Incompatible API version: server speaks {SERVER_API_MAJOR}.{SERVER_API_MINOR}, "
                f"client speaks {client_major}.{client_minor}. Major versions must match.",
                server_version=f"{SERVER_API_MAJOR}.{SERVER_API_MINOR}",
                client_version=f"{client_major}.{client_minor}",
            )
            response = JSONResponse(status_code=error.status_code, content=error.to_payload())
            response.headers[API_VERSION_HEADER] = f"{SERVER_API_MAJOR}.{SERVER_API_MINOR}"
            return response
        if client_major is not None and client_minor != SERVER_API_MINOR:
            direction = "client should upgrade" if SERVER_API_MINOR > client_minor else "server should upgrade"
            logger.warning(
                "API minor version mismatch: server=%s.%s client=%s.%s (%s)",
                SERVER_API_MAJOR, SERVER_API_MINOR, client_major, client_minor, direction,
            )

    response = await call_next(request)
    response.headers[API_VERSION_HEADER] = f"{SERVER_API_MAJOR}.{SERVER_API_MINOR}"
    return response


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload())


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
    normalized = normalize_http_exception(exc)
    detail = (
        normalized.detail
        if isinstance(normalized.detail, dict)
        else {"error": {"code": "runtime_error", "message": str(normalized.detail)}}
    )
    return JSONResponse(status_code=normalized.status_code, content=detail)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(status_code=422, content=validation_error_payload(exc))


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "Unhandled error while serving %s %s: %s",
        request.method,
        request.url.path,
        exc,
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": (
                    "The server hit an unexpected error. See the server log for the "
                    "traceback: /var/log/altastor-backup/server/server.log."
                ),
            }
        },
    )


for domain in (
    auth_api,
    users_api,
    roles_api,
    audit_api,
    repositories_api,
    inventories_api,
    jobs_api,
    sessions_api,
    backups_api,
    restore_api,
    system_api,
):
    app.include_router(domain.router)
app.include_router(dashboard_router)


@app.get("/health", response_model=HealthDTO)
def health() -> dict[str, str]:
    return {"status": "ok"}


configure_web_ui(app)

@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    for name, value in security_headers_for(request.url.scheme).items():
        response.headers.setdefault(name, value)
    return response


if __name__ == "__main__":
    # Persist to MongoDB by default when launched directly. Without this, an
    # unset MONGODB_URI silently runs the server in-memory and every record is
    # lost on restart. Tests import `app` but never run this block, so they keep
    # the in-memory behaviour. An explicit MONGODB_URI (e.g. from the launch
    # scripts) still wins via setdefault.
    default_mongodb_uri = "mongodb://127.0.0.1:27018"
    if os.environ.setdefault("MONGODB_URI", default_mongodb_uri) == default_mongodb_uri:
        logger.info(
            "MONGODB_URI not set; defaulting to %s for persistence.",
            default_mongodb_uri,
        )
    logger.info("MongoDB persistence enabled via %s", os.environ["MONGODB_URI"])

    from python.srv.core.http_redirect import start_redirect_listener
    from python.srv.core.tls_bootstrap import (
        default_cert_paths,
        ensure_self_signed_cert,
        resolve_tls_ciphers,
    )

    cert_path, key_path = default_cert_paths()
    ensure_self_signed_cert(cert_path, key_path)

    config = uvicorn.Config(
        app,
        host=os.environ["ALTASTOR_BACKUP_HOST"],
        port=env_int("ALTASTOR_BACKUP_PORT"),
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
        ssl_ciphers=resolve_tls_ciphers(),
    )
    config.load()
    TLS_RUNTIME = TlsRuntime(cert_path=cert_path, key_path=key_path, ssl_context=config.ssl)

    redirect_listener = start_redirect_listener(
        host=os.environ["ALTASTOR_BACKUP_HOST"],
        https_port=env_int("ALTASTOR_BACKUP_PORT"),
        security_headers=lambda: security_headers_for("http"),
    )

    logger.info("Starting Uvicorn server (HTTPS, cert %s)...", cert_path)
    try:
        uvicorn.Server(config).run()
    finally:
        if redirect_listener is not None:
            redirect_listener.stop()
