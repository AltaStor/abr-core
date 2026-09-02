# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import socket
import zipfile
from pathlib import Path
from typing import Any

from python.srv.core.logger_conf import COMPONENT_LOG_FILENAMES, _resolve_log_dir
from python.srv.core.logger_conf import is_worth_collecting as _is_worth_collecting
from python.srv.core.logger_conf import redact_log_bytes as _redact_log_bytes
from python.srv.core.logger_conf import redact_text as _redact_config_text
from python.srv.core.server_defaults import DEFAULTS_ENV_PATH
from python.srv.core.utils import utc_now

from . import abr_v41


def _server_configuration_file_candidates() -> list[Path]:
    override = os.environ.get(abr_v41.SERVER_DEFAULTS_FILE_ENV_VAR)
    if override:
        return [Path(override)]
    return [
        Path("/etc/altastor-backup/server.conf"),
        abr_v41.DEFAULT_SERVER_DEFAULTS_FILE,
        DEFAULTS_ENV_PATH,
    ]


def _mongodb_defaults_file_candidates() -> list[Path]:
    return [
        Path("/etc/altastor-backup/mongod.conf"),
        Path("/etc/default/altastor-backup-mongodb"),
    ]


def _mongodb_log_path() -> str:
    for candidate in _mongodb_defaults_file_candidates():
        if candidate.is_file():
            values = abr_v41._read_env_defaults_file(candidate)
            log_path = values.get("MONGO_LOGPATH")
            if log_path:
                return log_path
            break
    return "/var/log/altastor-backup/mongodb/mongod.log"


def _collect_logs(warnings: list[str]) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []

    server_dir = Path(_resolve_log_dir("srv"))
    server_logs = sorted(
        p for p in server_dir.glob(COMPONENT_LOG_FILENAMES["srv"] + "*") if _is_worth_collecting(p)
    )
    if server_logs:
        for log_file in server_logs:
            try:
                files.append((f"logs/server/{log_file.name}", _redact_log_bytes(log_file.read_bytes())))
            except OSError as exc:
                warnings.append(f"Failed to read {log_file}: {exc}")
    else:
        warnings.append(f"No server logs found under {server_dir}")

    cli_dir = Path(_resolve_log_dir("cli"))
    cli_logs = sorted(
        p for p in cli_dir.glob(COMPONENT_LOG_FILENAMES["cli"] + "*") if _is_worth_collecting(p)
    )
    if cli_logs:
        for log_file in cli_logs:
            try:
                files.append((f"logs/cli/{log_file.name}", _redact_log_bytes(log_file.read_bytes())))
            except OSError as exc:
                warnings.append(f"Failed to read {log_file}: {exc}")
    else:
        warnings.append(f"No CLI logs found under {cli_dir}")

    mongodb_log = Path(_mongodb_log_path())
    try:
        if mongodb_log.is_file():
            files.append((f"logs/mongodb/{mongodb_log.name}", _redact_log_bytes(mongodb_log.read_bytes())))
        else:
            warnings.append(f"MongoDB log not found at {mongodb_log}")
    except OSError as exc:
        warnings.append(f"Failed to read MongoDB log at {mongodb_log}: {exc}")

    return files


def _collect_configuration(warnings: list[str]) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []

    found = False
    unreadable = False
    for candidate in _server_configuration_file_candidates():
        try:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            warnings.append(f"Failed to read server configuration at {candidate}: {exc}")
            unreadable = True
            continue
        files.append((f"config/{candidate.name}", _redact_config_text(text).encode("utf-8")))
        found = True
        break
    if not found and not unreadable:
        warnings.append("Server configuration file not found")

    return files


def _disk_usage_dict(path: str) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
        return {
            "path": path,
            "capacity_bytes": int(usage.total),
            "used_bytes": int(usage.used),
            "free_bytes": int(usage.free),
        }
    except OSError as exc:
        return {"path": path, "error": str(exc)}


def _collect_system_info() -> dict[str, Any]:
    info = dict(abr_v41.system_info())
    info["disk"] = {
        "storage_root": _disk_usage_dict(os.environ.get("BACKUPSERVER_STORAGE_ROOT", "/var/lib/altastor-backup/storage")),
        "log_dir": _disk_usage_dict(_resolve_log_dir("srv")),
    }
    return info


def _json_dumps(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


_SESSION_HISTORY_LIMIT = 200


def _collect_catalog(warnings: list[str]) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []

    sources: list[tuple[str, Any]] = [
        ("sessions", lambda: abr_v41.list_sessions()[:_SESSION_HISTORY_LIMIT]),
        ("jobs", abr_v41.list_jobs),
        ("repositories", abr_v41.list_repository_records),
        ("inventories", abr_v41.list_inventory_records),
        ("restore-points", abr_v41.list_restore_points),
        ("health", abr_v41.system_health),
    ]

    for name, source in sources:
        try:
            payload = source()
        except Exception as exc:
            warnings.append(f"Failed to collect {name} for the bundle: {exc}")
            continue
        files.append((f"catalog/{name}.json", _json_dumps(payload).encode("utf-8")))

    return files


def generate_support_bundle() -> dict[str, Any]:
    warnings: list[str] = []

    collected_files = [
        *_collect_logs(warnings),
        *_collect_configuration(warnings),
        *_collect_catalog(warnings),
    ]
    system_info = _collect_system_info()
    included_files = [arcname for arcname, _ in collected_files] + ["system-info.json"]

    manifest = {
        "product": abr_v41.PRODUCT_NAME,
        "product_version": abr_v41.PRODUCT_VERSION,
        "generated_at": utc_now(),
        "hostname": socket.gethostname() or None,
        "files": included_files,
        "warnings": warnings,
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for arcname, content in collected_files:
            archive.writestr(arcname, content)
        archive.writestr("system-info.json", _json_dumps(system_info))
        archive.writestr("manifest.json", _json_dumps(manifest))

    return {
        "manifest": manifest,
        "archive_base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
    }
