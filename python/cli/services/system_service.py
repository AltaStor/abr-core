# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from python.cli.core.logger_conf import COMPONENT_LOG_FILENAMES, _resolve_log_dir
from python.cli.core.logger_conf import is_worth_collecting as _is_worth_collecting
from python.cli.core.logger_conf import redact_log_bytes as _redact_log_bytes

from ..cli_context import CliContext
from .server_defaults_template import render_recovered_server_defaults

_LOCAL_LOG_TAIL_BYTES = 2 * 1024 * 1024


def _default_support_bundle_filename() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"support-bundle-{timestamp}.zip"


def _is_writable_dir(directory: Path) -> bool:
    try:
        probe = directory / f".abr-write-test-{os.getpid()}"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def _default_support_bundle_destination() -> Path:
    candidates: list[Path] = []
    try:
        candidates.append(Path.cwd())
    except OSError:
        pass

    home = os.path.expanduser("~")
    if home and home != "~":
        candidates.append(Path(home))

    for directory in candidates:
        if _is_writable_dir(directory):
            return directory / _default_support_bundle_filename()

    return Path(tempfile.gettempdir()) / _default_support_bundle_filename()


def _find_local_cli_logs() -> list[Path]:
    log_dir = Path(_resolve_log_dir("cli"))
    pattern = COMPONENT_LOG_FILENAMES["cli"] + "*"
    try:
        return sorted(p for p in log_dir.glob(pattern) if p.is_file() and _is_worth_collecting(p))
    except OSError:
        return []


def _read_tail(path: Path, limit: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        return handle.read()


def _add_local_cli_logs(entries: dict[str, bytes], manifest: dict, log_files: list[Path]) -> bool:
    changed = False

    for log_file in log_files:
        try:
            content = _redact_log_bytes(_read_tail(log_file, _LOCAL_LOG_TAIL_BYTES))
        except OSError:
            continue

        if entries.get(f"logs/cli/{log_file.name}") == content:
            continue

        arcname = f"logs/cli-local/{log_file.name}"
        entries[arcname] = content
        manifest["files"].append(arcname)
        changed = True

    return changed


def _merge_local_diagnostics_into_bundle(
    archive_bytes: bytes, manifest: dict
) -> tuple[bytes, dict]:
    log_files = _find_local_cli_logs()
    if not log_files:
        return archive_bytes, manifest

    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as src:
            entries = {name: src.read(name) for name in src.namelist() if name != "manifest.json"}
    except (zipfile.BadZipFile, OSError) as exc:
        degraded = dict(manifest)
        degraded["warnings"] = [
            *manifest.get("warnings", []),
            f"Could not merge this machine's CLI logs into the bundle: {exc}",
        ]
        return archive_bytes, degraded

    merged_manifest = dict(manifest)
    merged_manifest["files"] = list(manifest.get("files", []))
    merged_manifest["warnings"] = list(manifest.get("warnings", []))

    if not _add_local_cli_logs(entries, merged_manifest, log_files):
        return archive_bytes, manifest

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as out:
        for arcname, content in entries.items():
            out.writestr(arcname, content)
        out.writestr(
            "manifest.json", json.dumps(merged_manifest, indent=2, sort_keys=True, default=str)
        )

    return buffer.getvalue(), merged_manifest


class SystemService:
    def __init__(self, context: CliContext) -> None:
        self.context = context

    def get_support_bundle(self, *, file: str | None) -> dict[str, Any]:
        result = self.context.api_client.post("api/v1/system/support-bundle")

        archive_bytes = base64.b64decode(result["archive_base64"])
        archive_bytes, manifest = _merge_local_diagnostics_into_bundle(
            archive_bytes, result.get("manifest", {})
        )

        destination = Path(file) if file else _default_support_bundle_destination()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(archive_bytes)

        return {
            "file": str(destination.resolve()),
            "size_bytes": len(archive_bytes),
            "manifest": manifest,
        }

    def export_catalog(self, *, file: str, password: str) -> dict[str, Any]:
        result = self.context.api_client.post(
            "api/v1/system/catalog-snapshot", json={"password": password}
        )

        destination = Path(file)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(base64.b64decode(result["archive_base64"]))

        return {"file": str(destination), "manifest": result.get("manifest", {})}

    def import_catalog(self, *, file: str, password: str) -> dict[str, Any]:
        archive_base64 = base64.b64encode(Path(file).read_bytes()).decode("ascii")

        result = self.context.api_client.post(
            "api/v1/system/catalog-restore",
            json={"password": password, "archive_base64": archive_base64},
        )
        configuration = (
            result.get("configuration") if isinstance(result, dict) else None
        )

        if configuration:
            recovered_path = Path(file).with_name(Path(file).name + "-default.env")
            recovered_path.write_text(
                render_recovered_server_defaults(configuration), encoding="utf-8"
            )
            result = {**result, "recovered_configuration_file": str(recovered_path)}

        return result
