# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import base64
import gzip
import io
import json
import os
import re
import shutil
import socket
import zipfile
from pathlib import Path
from typing import Any

from python.srv.core.logger_conf import (
    COMPONENT_LOG_FILENAMES,
    COMPONENT_LOG_SUBDIRS,
    SYSTEM_LOG_DIR,
    _resolve_log_dir,
)
from python.srv.core.logger_conf import is_worth_collecting as _is_worth_collecting
from python.srv.core.logger_conf import redact_log_bytes as _redact_log_bytes
from python.srv.core.logger_conf import REDACTED_VALUE
from python.srv.core.logger_conf import redact_text as _redact_config_text
from python.srv.core.server_defaults import DEFAULTS_ENV_PATH
from python.srv.core.subprocess_util import run_capped
from python.srv.core.utils import utc_now

from . import abr_v41

_DEFAULT_MONGODB_LOG_PATH = "/var/log/mongodb/mongod.log"
_SYSTEMCTL_TIMEOUT_SECONDS = 10
_UNIT_SECRET_ENVIRONMENT = re.compile(
    r'(?im)^(\s*Environment="?[A-Za-z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|APIKEY)[A-Za-z0-9_]*\s*=\s*)([^"\s]+)'
)


def _server_configuration_file_candidates() -> list[Path]:
    override = os.environ.get(abr_v41.SERVER_DEFAULTS_FILE_ENV_VAR)
    if override:
        return [Path(override)]
    return [
        Path("/etc/altastor-backup/server.conf"),
        abr_v41.DEFAULT_SERVER_DEFAULTS_FILE,
        DEFAULTS_ENV_PATH,
    ]


def _run_tool(command: list[str]) -> str | None:
    try:
        result = run_capped(command, timeout=_SYSTEMCTL_TIMEOUT_SECONDS, nice=False)
    except Exception:
        return None
    output = result.stdout
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    return output or None


def _package_versions() -> dict[str, str]:
    output = _run_tool(["dpkg-query", "-W", "-f=${Package} ${Version}\\n", "altastor-*"])
    if not output:
        return {}

    versions: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].startswith("altastor"):
            versions[parts[0]] = parts[1]
    return versions


def _unit_property(unit: str, name: str) -> str:
    output = _run_tool(["systemctl", "show", unit, "-p", name, "--value"])
    return (output or "").strip()


def _systemd_unit_files(unit: str) -> list[Path]:
    paths: list[Path] = []
    fragment = _unit_property(unit, "FragmentPath")
    if fragment:
        paths.append(Path(fragment))
    paths.extend(Path(entry) for entry in _unit_property(unit, "DropInPaths").split())
    return paths


def _mongodb_configuration_from_systemd() -> Path | None:
    exec_start = _unit_property("mongod.service", "ExecStart")
    if not exec_start:
        return None

    tokens = exec_start.replace(";", " ").split()
    for index, token in enumerate(tokens):
        if token.startswith("--config="):
            return Path(token[len("--config="):])
        if token in ("--config", "-f") and index + 1 < len(tokens):
            return Path(tokens[index + 1])
    return None


def _mongodb_configuration_file() -> Path:
    from_systemd = _mongodb_configuration_from_systemd()
    if from_systemd is not None:
        return from_systemd

    fallbacks = (Path("/etc/altastor-backup/mongod.conf"), Path("/etc/mongod.conf"))
    for candidate in fallbacks:
        if candidate.is_file():
            return candidate
    return fallbacks[0]


def _scalar(value: str) -> str:
    value = value.strip()
    quote = value[:1]
    if quote in ("'", '"'):
        closing = value.find(quote, 1)
        if closing != -1:
            return value[1:closing]
    for comment in (" #", "\t#"):
        index = value.find(comment)
        if index != -1:
            value = value[:index]
    return value.strip()


def _mongodb_log_path_from_text(text: str) -> str | None:
    saw_system_log = False
    system_log_indent: int | None = None
    unscoped_path: str | None = None

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" \t"))

        if ":" not in stripped:
            continue

        key, _, value = stripped.partition(":")
        key = key.strip()
        value = _scalar(value)

        if key == "systemLog":
            saw_system_log = True
            system_log_indent = indent
            continue

        if system_log_indent is not None and indent <= system_log_indent:
            system_log_indent = None

        if key != "path" or not value:
            continue

        if system_log_indent is not None:
            return value
        if unscoped_path is None:
            unscoped_path = value

    return None if saw_system_log else unscoped_path


def _mongodb_log_path() -> str:
    candidate = _mongodb_configuration_file()
    try:
        if candidate.is_file():
            log_path = _mongodb_log_path_from_text(
                candidate.read_text(encoding="utf-8", errors="replace")
            )
            if log_path:
                return log_path
    except OSError:
        pass

    return _DEFAULT_MONGODB_LOG_PATH


def _process_user_name() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_name
    except (ImportError, KeyError, OSError):
        return str(os.geteuid()) if hasattr(os, "geteuid") else "unknown"


def _ownership_hint(path: Path) -> str:
    try:
        stat_result = path.stat()
    except OSError:
        return f"; the support bundle runs as {_process_user_name()}"

    owner: Any = stat_result.st_uid
    group: Any = stat_result.st_gid
    try:
        import grp
        import pwd

        owner = pwd.getpwuid(stat_result.st_uid).pw_name
        group = grp.getgrgid(stat_result.st_gid).gr_name
    except (ImportError, KeyError, OSError):
        pass

    return (
        f"; it is mode {stat_result.st_mode & 0o777:04o} {owner}:{group} "
        f"and the support bundle runs as {_process_user_name()}"
    )


def _read_log_bytes(path: Path) -> bytes:
    if path.name.endswith(".gz"):
        with gzip.open(path, "rb") as handle:
            return handle.read()
    return path.read_bytes()


def _collected_log_name(path: Path) -> str:
    return path.name[: -len(".gz")] if path.name.endswith(".gz") else path.name


def _collect_log_file(
    path: Path, arc_dir: str, files: list[tuple[str, bytes]], warnings: list[str]
) -> None:
    try:
        content = _read_log_bytes(path)
    except PermissionError as exc:
        warnings.append(f"Failed to read {path}: {exc}{_ownership_hint(path)}")
        return
    except OSError as exc:
        warnings.append(f"Failed to read {path}: {exc}")
        return

    files.append((f"{arc_dir}/{_collected_log_name(path)}", _redact_log_bytes(content)))


def _rotated_siblings(path: Path) -> list[Path]:
    try:
        return sorted(p for p in path.parent.glob(path.name + "*") if p.is_file())
    except OSError:
        return []


def _component_log_dirs(component: str) -> list[Path]:
    dirs: list[Path] = []
    if component == "srv":
        explicit = os.environ.get("ALTASTOR_LOG_DIR")
        if explicit:
            dirs.append(Path(explicit))
    dirs.append(Path(SYSTEM_LOG_DIR) / COMPONENT_LOG_SUBDIRS.get(component, component))
    dirs.append(Path(_resolve_log_dir(component)))

    unique: list[Path] = []
    seen: set[str] = set()
    for directory in dirs:
        key = os.path.abspath(directory)
        if key not in seen:
            seen.add(key)
            unique.append(directory)
    return unique


def _component_log_files(component: str) -> tuple[list[Path], list[Path]]:
    pattern = COMPONENT_LOG_FILENAMES[component] + "*"
    searched = _component_log_dirs(component)
    for directory in searched:
        try:
            found = sorted(
                p for p in directory.glob(pattern) if p.is_file() and _is_worth_collecting(p)
            )
        except OSError:
            continue
        if found:
            return found, searched
    return [], searched


def _collect_logs(warnings: list[str]) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []

    for component, arc_dir, label in (("srv", "logs/server", "server"), ("cli", "logs/cli", "CLI")):
        found, searched = _component_log_files(component)
        if not found:
            looked_in = ", ".join(str(d) for d in searched)
            warnings.append(f"No {label} logs found under {looked_in}")
            continue
        for log_file in found:
            _collect_log_file(log_file, arc_dir, files, warnings)

    mongodb_log = Path(_mongodb_log_path())
    mongodb_logs = _rotated_siblings(mongodb_log)
    if mongodb_logs:
        for log_file in mongodb_logs:
            _collect_log_file(log_file, "logs/mongodb", files, warnings)
    elif mongodb_log.exists():
        _collect_log_file(mongodb_log, "logs/mongodb", files, warnings)
    else:
        warnings.append(f"MongoDB log not found at {mongodb_log}")

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


def _collect_systemd_units() -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    seen: set[str] = set()

    for unit in ("altastor-backup-server.service", "mongod.service"):
        for path in _systemd_unit_files(unit):
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            parent = path.parent.name
            arcname = (
                f"config/systemd/{parent}/{path.name}"
                if parent.endswith(".d")
                else f"config/systemd/{path.name}"
            )
            masked = _UNIT_SECRET_ENVIRONMENT.sub(rf"\1{REDACTED_VALUE}", text)
            files.append((arcname, _redact_config_text(masked).encode("utf-8")))

    return files


def _collect_mongodb_configuration(warnings: list[str]) -> list[tuple[str, bytes]]:
    candidate = _mongodb_configuration_file()
    try:
        if not candidate.is_file():
            return []
        text = candidate.read_text(encoding="utf-8")
    except PermissionError as exc:
        warnings.append(
            f"Failed to read MongoDB configuration at {candidate}: {exc}{_ownership_hint(candidate)}"
        )
        return []
    except OSError as exc:
        warnings.append(f"Failed to read MongoDB configuration at {candidate}: {exc}")
        return []

    return [(f"config/{candidate.name}", _redact_config_text(text).encode("utf-8"))]


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
        *_collect_mongodb_configuration(warnings),
        *_collect_systemd_units(),
        *_collect_catalog(warnings),
    ]
    system_info = _collect_system_info()
    included_files = [arcname for arcname, _ in collected_files] + [
        "system-info.json",
        "manifest.json",
    ]

    manifest = {
        "product": abr_v41.PRODUCT_NAME,
        "product_version": abr_v41.PRODUCT_VERSION,
        "generated_at": utc_now(),
        "hostname": socket.gethostname() or None,
        "files": included_files,
        "warnings": warnings,
    }
    package_versions = _package_versions()
    if package_versions:
        manifest["package_versions"] = package_versions

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
