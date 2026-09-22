# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from uuid import uuid4

from .server_defaults import env_int
from .subprocess_util import CompletedProcess, ProcessCancelled, run_capped

APP_ROOT = Path("/opt/altastor-backup")
DISK_TOOL_LAUNCHER = "abr-vmdk-tool.sh"
REPO_ROOT = Path(__file__).resolve().parents[3]
CREDENTIAL_FIELD_NAMES = frozenset({"password", "token", "encryption_key"})
SECRET_MASK = "********"
_SAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
VERIFY_PROGRESS_POLL_SECONDS = 0.25
IMMUTABILITY_MODES = ("none", "governance", "compliance")
IMMUTABILITY_RETENTION_SCOPES = ("job_retention", "minimum_only")
_IMMUTABILITY_RANKS = {"none": 0, "governance": 1, "compliance": 2}
IMMUTABILITY_PROBE_FOLDER = ".abr-immutability-probe"
IMMUTABILITY_PROBE_NAME_PREFIX = "immutability-probe-"
IMMUTABILITY_PROBE_RETENTION_DAYS = 1


class StorageToolError(RuntimeError):
    pass


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def normalize_s3_prefix(value: Any) -> str:
    prefix = str(value or "").strip().replace("\\", "/")
    return prefix.strip("/")


def join_s3_prefix(*parts: Any) -> str:
    normalized = [normalize_s3_prefix(part) for part in parts if str(part or "").strip()]
    return "/".join(part for part in normalized if part)


def build_s3_location(bucket: str, prefix: Any = None) -> str:
    normalized_prefix = normalize_s3_prefix(prefix)
    return f"s3://{bucket}/{normalized_prefix}" if normalized_prefix else f"s3://{bucket}"


def normalize_immutability_mode(value: Any) -> str:
    return str(value or "none").strip().lower() or "none"


def immutability_rank(mode: Any) -> int:
    return _IMMUTABILITY_RANKS.get(normalize_immutability_mode(mode), 0)


def normalize_immutability_retention_scope(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    return normalized if normalized in IMMUTABILITY_RETENTION_SCOPES else IMMUTABILITY_RETENTION_SCOPES[0]


def normalize_immutability_config(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    try:
        retention_days = int(source.get("retention_days") or 0)
    except (TypeError, ValueError):
        retention_days = 0
    return {
        "mode": normalize_immutability_mode(source.get("mode")),
        "retention_days": max(retention_days, 0),
        "retention_scope": normalize_immutability_retention_scope(source.get("retention_scope")),
    }


def immutability_is_enabled(config: Any) -> bool:
    normalized = normalize_immutability_config(config)
    return normalized["mode"] != "none" and normalized["retention_days"] > 0


def mask_access_key(value: Any) -> str | None:
    token = str(value or "").strip()
    if not token:
        return None
    if len(token) <= 4:
        return "*" * len(token)
    if len(token) <= 8:
        return f"{token[:2]}{'*' * max(len(token) - 4, 1)}{token[-2:]}"
    return f"{token[:4]}{'*' * max(len(token) - 6, 4)}{token[-2:]}"


def mask_credential_fields(data: dict[str, Any]) -> dict[str, Any]:
    for key, value in data.items():
        if key in CREDENTIAL_FIELD_NAMES:
            if value:
                data[key] = SECRET_MASK
        elif isinstance(value, dict):
            mask_credential_fields(value)
    return data


def extract_s3_config(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    raw = record.get("s3") if isinstance(record.get("s3"), dict) else {}
    if not raw:
        raw = metadata.get("s3") if isinstance(metadata.get("s3"), dict) else {}

    bucket = str(raw.get("bucket") or record.get("bucket") or "").strip()
    prefix = normalize_s3_prefix(raw.get("prefix") if "prefix" in raw else record.get("prefix"))
    endpoint_url = str(raw.get("endpoint_url") or record.get("endpoint_url") or "").strip() or None
    region = str(raw.get("region") or record.get("region") or "").strip() or None
    access_key = str(raw.get("access_key") or "").strip() or None
    secret_key = str(raw.get("secret_key") or "") or None
    profile = str(raw.get("profile") or record.get("profile") or "").strip() or None
    path_style = _coerce_bool(
        raw.get("path_style") if "path_style" in raw else record.get("path_style"),
        default=bool(endpoint_url),
    )
    verify_tls = _coerce_bool(
        raw.get("verify_tls") if "verify_tls" in raw else record.get("verify_tls"),
        default=True,
    )
    description = str(raw.get("description") or metadata.get("description") or record.get("description") or "").strip() or None
    immutability = normalize_immutability_config(
        raw.get("immutability") if isinstance(raw.get("immutability"), dict) else record.get("immutability")
    )

    return {
        "bucket": bucket,
        "prefix": prefix,
        "region": region,
        "endpoint_url": endpoint_url,
        "access_key": access_key,
        "secret_key": secret_key,
        "profile": profile,
        "path_style": path_style,
        "verify_tls": verify_tls,
        "description": description,
        "immutability": immutability,
        "location": build_s3_location(bucket, prefix) if bucket else None,
    }


def sanitize_s3_config(config: dict[str, Any]) -> dict[str, Any]:
    sanitized = {
        "bucket": config.get("bucket"),
        "prefix": normalize_s3_prefix(config.get("prefix")),
        "region": config.get("region"),
        "endpoint_url": config.get("endpoint_url"),
        "profile": config.get("profile"),
        "path_style": bool(config.get("path_style")),
        "verify_tls": bool(config.get("verify_tls", True)),
        "description": config.get("description"),
        "access_key_masked": mask_access_key(config.get("access_key")),
        "has_secret_key": bool(str(config.get("secret_key") or "").strip()),
        "immutability": normalize_immutability_config(config.get("immutability")),
    }
    sanitized["location"] = build_s3_location(
        str(sanitized.get("bucket") or ""),
        sanitized.get("prefix"),
    ) if sanitized.get("bucket") else None
    return sanitized


def is_s3_repository(repository: dict[str, Any] | None) -> bool:
    if not isinstance(repository, dict):
        return False
    return str(repository.get("type") or "").strip().lower() == "s3"


def is_abr_repository(repository: dict[str, Any] | None) -> bool:
    """Repositories backed by the .abr format (both local-file and S3)."""
    if not isinstance(repository, dict):
        return False
    return str(repository.get("type") or "").strip().lower() in {"local", "s3"}


def s3_block_generation_days() -> int:
    return env_int("ALTASTOR_S3_BLOCK_GENERATION_DAYS")


def s3_endpoint_connect_timeout() -> int:
    return env_int("ALTASTOR_S3_CONNECT_TIMEOUT_SECONDS")


def s3_endpoint_connect_error(endpoint_url: str) -> str | None:
    parsed = urlparse(endpoint_url)
    host = parsed.hostname
    if not host:
        return "endpoint URL has no host"
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return "endpoint URL has an invalid port"

    deadline = time.monotonic() + s3_endpoint_connect_timeout()
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        return f"cannot resolve {host} ({exc})"

    last_error = "no usable address"
    for family, socket_type, proto, _canonical, address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            with socket.socket(family, socket_type, proto) as probe:
                probe.settimeout(remaining)
                probe.connect(address)
            return None
        except OSError as exc:
            last_error = str(exc) or exc.__class__.__name__
    return f"{host}:{port} ({last_error})"


def safe_path_component(value: Any, *, fallback: str = "item") -> str:
    cleaned = _SAFE_PATH_CHARS.sub("-", str(value or "").strip()).strip(".-")
    return cleaned or fallback


def ensure_private_directory(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        try:
            os.chmod(directory, 0o750)
        except OSError:
            pass


def repository_cache_root(base_root: Path, repository: dict[str, Any]) -> Path:
    repo_id = safe_path_component(repository.get("id"), fallback="unknown-repository")
    return base_root / ".catalog" / "repositories" / repo_id


def default_disk_tool_path() -> Path:
    """Locate the abr-vmdk-tool launcher.

    Always the wrapper script, never the binary beside it: the wrapper is what
    puts the VDDK on the library search path and supplies a writable TMPDIR.
    """
    candidates = []
    configured = os.environ.get("ALTASTOR_DISK_TOOL_PATH")
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(APP_ROOT / "tools" / "abr-vmdk-tool" / DISK_TOOL_LAUNCHER)
    candidates.append(REPO_ROOT / "tools" / "abr-vmdk-tool" / DISK_TOOL_LAUNCHER)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return candidates[-1]


def disk_tool_storage_args(
    repository: dict[str, Any],
    *,
    abr_path: str | None = None,
    create_folder: bool = False,
) -> list[str]:
    """Backup-storage options for abr-vmdk-tool (the --folder/--mode/--path form)."""
    repo_type = str(repository.get("type") or "").strip().lower()
    if repo_type == "local":
        folder = str(abr_path if abr_path is not None else repository.get("path") or "").strip()
        if not folder:
            raise StorageToolError("local repository is missing a path")
        # File-mode .abr backend: the mover writes backups directly under that
        # folder as <name>.abri/.abrd/.abrm, so the whole location is --folder
        # and there is no --path/credentials/region to pass.
        args = ["--mode", "file", "--folder", folder]
        if create_folder:
            args.append("--create-folder")
        return args
    if repo_type != "s3":
        raise StorageToolError(f"Unsupported ABR storage repository type: {repo_type or 'unknown'}")

    config = extract_s3_config(repository)
    bucket = str(config.get("bucket") or "").strip()
    if not bucket:
        raise StorageToolError("S3 repository is missing bucket configuration")

    effective_path = normalize_s3_prefix(abr_path if abr_path is not None else config.get("prefix"))
    args = ["--mode", "s3", "--folder", bucket, "--path", effective_path]
    if create_folder:
        args.append("--create-folder")
    if config.get("region"):
        args.extend(["--region", str(config["region"])])
    if config.get("endpoint_url"):
        args.extend(["--endpoint", str(config["endpoint_url"])])
    if config.get("access_key"):
        args.extend(["--access-key", str(config["access_key"])])
    if config.get("secret_key"):
        args.extend(["--secret-key", str(config["secret_key"])])
    if config.get("profile"):
        args.extend(["--aws-profile", str(config["profile"])])
    if bool(config.get("path_style")):
        args.append("--path-style")
    if not bool(config.get("verify_tls", True)):
        args.append("--no-verify-tls")
    return args


def disk_tool_immutability_args(repository: dict[str, Any]) -> list[str]:
    if not is_s3_repository(repository):
        return []
    config = normalize_immutability_config(extract_s3_config(repository).get("immutability"))
    if not immutability_is_enabled(config):
        return []
    return ["--immutable", config["mode"], "--retention", str(config["retention_days"])]


_SOURCE_STORAGE_FLAGS = {
    "--mode": "--source-mode",
    "--folder": "--source-folder",
    "--path": "--source-path",
    "--region": "--source-region",
    "--endpoint": "--source-endpoint",
    "--access-key": "--source-access-key",
    "--secret-key": "--source-secret-key",
    "--aws-profile": "--source-aws-profile",
    "--path-style": "--source-path-style",
    "--no-verify-tls": "--source-no-verify-tls",
}
_VALUELESS_STORAGE_FLAGS = frozenset({"--path-style", "--no-verify-tls", "--create-folder"})


def disk_tool_source_storage_args(repository: dict[str, Any], *, abr_path: str | None = None) -> list[str]:
    args = disk_tool_storage_args(repository, abr_path=abr_path)
    renamed: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        mapped = _SOURCE_STORAGE_FLAGS.get(token, token)
        renamed.append(mapped)
        if token in _VALUELESS_STORAGE_FLAGS:
            index += 1
            continue
        renamed.append(args[index + 1])
        index += 2
    if "--source-path" not in renamed:
        renamed.extend(["--source-path", ""])
    return renamed


def copy_disk_tool_chain(
    *,
    source_repository: dict[str, Any],
    source_abr_path: str | None,
    source_names: list[str],
    destination_repository: dict[str, Any],
    destination_abr_path: str | None,
    backup_name: str,
    compression_level: int | None = None,
    encryption_key: str = "",
    source_encryption_key: str = "",
    retention_days: int | None = None,
    timeout: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    if not source_names:
        raise StorageToolError("a synthetic full needs at least one source backup")
    command = [
        "copy",
        "--json",
        "--name",
        backup_name,
        *disk_tool_storage_args(
            destination_repository,
            abr_path=destination_abr_path,
            create_folder=not is_s3_repository(destination_repository),
        ),
        *disk_tool_source_storage_args(source_repository, abr_path=source_abr_path),
        "--source-names",
        ",".join(source_names),
    ]
    if compression_level is not None:
        command.extend(["--compression", str(compression_level)])
    if encryption_key:
        command.extend(["--key", encryption_key])
    if source_encryption_key:
        command.extend(["--source-key", source_encryption_key])
    immutability = disk_tool_immutability_args(destination_repository)
    if immutability and retention_days is not None:
        immutability = [immutability[0], immutability[1], "--retention", str(int(retention_days))]
    command.extend(immutability)

    completed = run_disk_tool(
        command,
        timeout=timeout,
        on_progress=on_progress,
        progress_poll_seconds=VERIFY_PROGRESS_POLL_SECONDS if on_progress else None,
        should_cancel=should_cancel,
    )
    return parse_disk_tool_json(completed.stdout)


def redact_disk_tool_command(command: list[str]) -> list[str]:
    redacted = list(command)
    for index, value in enumerate(redacted[:-1]):
        if value in {"--password", "--secret-key", "--key", "-k", "--source-secret-key", "--source-key"}:
            redacted[index + 1] = SECRET_MASK
        if value in {"--access-key", "--source-access-key"}:
            redacted[index + 1] = mask_access_key(redacted[index + 1]) or SECRET_MASK
    return redacted


def hydrate_manifest_via_disk_tool(
    repository: dict[str, Any],
    abr_path: str,
    manifest_path: Path,
    *,
    backup_name: str = "manifest",
    timeout: int = 30,
) -> bool:
    """Pull the manifest blob stored beside a backup back onto local disk."""
    try:
        run_disk_tool(
            [
                "meta_load",
                "--name",
                backup_name,
                *disk_tool_storage_args(repository, abr_path=abr_path),
                "--target",
                str(manifest_path),
            ],
            timeout=timeout,
        )
    except StorageToolError:
        return False
    return manifest_path.is_file()


def list_disk_tool_backups(
    repository: dict[str, Any],
    abr_path: str,
    *,
    timeout: int = 30,
) -> set[str]:
    """Names of the backups stored under abr_path.

    The tool prints a header line and then one indented name per backup, or a
    "No backups found" line when the location is empty; anything that is not
    indented is chatter, not a name.
    """
    listed = run_disk_tool(
        ["list", *disk_tool_storage_args(repository, abr_path=abr_path)],
        timeout=timeout,
    )
    return {
        line.strip()
        for line in listed.stdout.splitlines()
        if line.startswith((" ", "\t")) and line.strip()
    }


def verify_disk_tool_backup(
    repository: dict[str, Any],
    abr_path: str | None,
    backup_name: str,
    *,
    encryption_key: str = "",
    timeout: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    """Block-level integrity check of one stored backup. Raises on damage."""
    run_disk_tool(
        [
            "verify",
            "--name",
            backup_name,
            *(["--key", encryption_key] if encryption_key else []),
            *disk_tool_storage_args(repository, abr_path=abr_path),
        ],
        timeout=timeout,
        on_progress=on_progress,
        progress_poll_seconds=VERIFY_PROGRESS_POLL_SECONDS,
    )


def remove_disk_tool_backup(
    repository: dict[str, Any],
    abr_path: str | None,
    backup_name: str,
    *,
    timeout: int | None = None,
) -> None:
    """Delete one stored backup."""
    run_disk_tool(
        [
            "remove",
            "--name",
            backup_name,
            # Without --force the tool stops for a y/N on stdin, which a
            # subprocess with no stdin can never answer.
            "--force",
            *disk_tool_storage_args(repository, abr_path=abr_path),
        ],
        timeout=timeout,
    )


def probe_repository_access(
    repository: dict[str, Any],
    *,
    abr_path: str | None = None,
    backup_name: str,
    timeout: int = 120,
    create_folder: bool = True,
) -> None:
    """Prove read/write/delete access by round-tripping a throwaway backup.

    abr-vmdk-tool has no single validate command, so this is the sequence that
    stands in for it: back up a small probe file (creating the folder/bucket if
    it is missing), read every block back through verify, then delete it. Raises
    StorageToolError on the first step that fails, and leaves nothing behind
    unless the run dies between backup and remove.
    """
    with tempfile.NamedTemporaryFile(prefix="abr-probe-", suffix=".bin") as probe:
        # A non-zero pattern: an all-zero probe is elided as an empty block and
        # produces a backup with no index records to verify.
        probe.write(b"\xa5" * 512)
        probe.flush()
        run_disk_tool(
            [
                "backup",
                "--name",
                backup_name,
                *disk_tool_storage_args(repository, abr_path=abr_path, create_folder=create_folder),
                "--source",
                probe.name,
            ],
            timeout=timeout,
        )

    try:
        verify_disk_tool_backup(repository, abr_path, backup_name, timeout=timeout)
    except StorageToolError:
        # The read-back failure is the diagnostic worth reporting, so the
        # cleanup runs on a best-effort basis and does not mask it.
        try:
            remove_disk_tool_backup(repository, abr_path, backup_name, timeout=timeout)
        except StorageToolError:
            pass
        raise

    remove_disk_tool_backup(repository, abr_path, backup_name, timeout=timeout)


def condense_disk_tool_message(text: Any) -> str:
    lines: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if line.lower().startswith("error:"):
            line = line[len("error:"):].strip()
        if line and line not in lines:
            lines.append(line)
    return "; ".join(lines)


def parse_disk_tool_json(stdout: str) -> dict[str, Any]:
    start = stdout.find("{")
    if start < 0:
        raise StorageToolError("backup tool returned no JSON result document")
    try:
        document = json.loads(stdout[start:])
    except ValueError as exc:
        raise StorageToolError(f"backup tool returned a malformed JSON result document: {exc}") from exc
    if not isinstance(document, dict):
        raise StorageToolError("backup tool returned an unexpected JSON result document")
    return document


def run_disk_tool_json(args: list[str], *, timeout: int | None = None) -> dict[str, Any]:
    command = [args[0], "--json", *args[1:]] if args else ["--json"]
    return parse_disk_tool_json(run_disk_tool(command, timeout=timeout).stdout)


def read_storage_folder_info(
    repository: dict[str, Any],
    abr_path: str | None = None,
    *,
    timeout: int = 60,
) -> dict[str, Any]:
    return run_disk_tool_json(
        ["info", *disk_tool_storage_args(repository, abr_path=abr_path)],
        timeout=timeout,
    )


def read_storage_backup_info(
    repository: dict[str, Any],
    abr_path: str | None,
    backup_name: str,
    *,
    timeout: int = 60,
) -> dict[str, Any]:
    return run_disk_tool_json(
        ["info", "--name", backup_name, *disk_tool_storage_args(repository, abr_path=abr_path)],
        timeout=timeout,
    )


def set_disk_tool_backup_retention(
    repository: dict[str, Any],
    abr_path: str | None,
    backup_name: str,
    *,
    mode: str,
    retention_days: int,
    timeout: int = 120,
) -> dict[str, Any]:
    normalized_mode = normalize_immutability_mode(mode)
    if normalized_mode == "none":
        raise StorageToolError("Object Lock cannot release a backup that is already held")
    if int(retention_days) < 1:
        raise StorageToolError("Object Lock retention must be at least 1 day")
    return run_disk_tool_json(
        [
            "retention",
            "--name",
            backup_name,
            *disk_tool_storage_args(repository, abr_path=abr_path),
            "--immutable",
            normalized_mode,
            "--retention",
            str(int(retention_days)),
        ],
        timeout=timeout,
    )


def _is_absent_data_object_error(message: str, backup_name: str) -> bool:
    lowered = message.lower()
    return "nosuchkey" in lowered and f"{backup_name}.abrd" in lowered


def with_immutability_retention_days(repository: dict[str, Any], retention_days: int) -> dict[str, Any]:
    if not is_s3_repository(repository):
        return repository
    config = extract_s3_config(repository)
    if not immutability_is_enabled(config.get("immutability")):
        return repository
    effective = dict(repository)
    config["immutability"] = dict(config["immutability"], retention_days=max(int(retention_days), 1))
    effective["s3"] = config
    return effective


def apply_repository_immutability(
    repository: dict[str, Any],
    abr_path: str | None,
    backup_name: str,
    *,
    retention_days: int | None = None,
    timeout: int = 120,
) -> dict[str, Any] | None:
    config = normalize_immutability_config(extract_s3_config(repository).get("immutability"))
    if not immutability_is_enabled(config):
        return None
    try:
        return set_disk_tool_backup_retention(
            repository,
            abr_path,
            backup_name,
            mode=config["mode"],
            retention_days=config["retention_days"] if retention_days is None else retention_days,
            timeout=timeout,
        )
    except StorageToolError as exc:
        if not _is_absent_data_object_error(str(exc), backup_name):
            raise
        return {
            "command": "retention",
            "status": "ok",
            "name": backup_name,
            "immutability": config["mode"],
            "retention_days": config["retention_days"] if retention_days is None else retention_days,
            "metadata_only": True,
        }


def immutability_probe_path(prefix: Any) -> str:
    return join_s3_prefix(prefix, IMMUTABILITY_PROBE_FOLDER)


def sweep_immutability_probes(
    repository: dict[str, Any],
    probe_path: str,
    *,
    timeout: int = 60,
) -> int:
    try:
        leftovers = list_disk_tool_backups(repository, probe_path, timeout=timeout)
    except StorageToolError:
        return 0
    swept = 0
    for name in sorted(leftovers):
        if not name.startswith(IMMUTABILITY_PROBE_NAME_PREFIX):
            continue
        try:
            remove_disk_tool_backup(repository, probe_path, name, timeout=timeout)
        except StorageToolError:
            continue
        swept += 1
    return swept


def probe_repository_immutability(
    repository: dict[str, Any],
    *,
    prefix: Any,
    mode: str,
    retention_days: int,
    timeout: int = 180,
) -> dict[str, Any]:
    probe_path = immutability_probe_path(prefix)
    sweep_immutability_probes(repository, probe_path, timeout=timeout)
    backup_name = f"{IMMUTABILITY_PROBE_NAME_PREFIX}{uuid4().hex[:12]}"

    with tempfile.NamedTemporaryFile(prefix="abr-immutability-probe-", suffix=".bin") as probe:
        probe.write(b"\xa5" * 512)
        probe.flush()
        run_disk_tool(
            [
                "backup",
                "--name",
                backup_name,
                *disk_tool_storage_args(repository, abr_path=probe_path),
                "--source",
                probe.name,
                "--immutable",
                mode,
                "--retention",
                str(max(int(retention_days), 1)),
            ],
            timeout=timeout,
        )

    stored = read_storage_backup_info(repository, probe_path, backup_name, timeout=timeout)

    delete_rejected = True
    delete_error: str | None = None
    try:
        remove_disk_tool_backup(repository, probe_path, backup_name, timeout=timeout)
        delete_rejected = False
    except StorageToolError as exc:
        delete_error = str(exc)

    return {
        "backup_name": backup_name,
        "path": probe_path,
        "mode": normalize_immutability_mode(stored.get("immutability")),
        "retention_days": int(stored.get("retention_days") or 0),
        "supported_mode": normalize_immutability_mode(stored.get("immutability_supported")),
        "delete_rejected": delete_rejected,
        "delete_error": condense_disk_tool_message(delete_error) if delete_error else None,
    }


def run_disk_tool(
    args: list[str],
    *,
    tool_path: Path | None = None,
    timeout: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    progress_poll_seconds: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> CompletedProcess:
    """Run abr-vmdk-tool; args start with the subcommand, e.g. ["list", ...]."""
    resolved_tool_path = (tool_path or default_disk_tool_path()).expanduser()
    if not resolved_tool_path.is_file():
        raise StorageToolError(f"Backup tool not found: {resolved_tool_path}")

    try:
        progress_options: dict[str, Any] = {}
        if on_progress is not None:
            progress_options["on_progress"] = on_progress
            if progress_poll_seconds is not None:
                progress_options["progress_poll_seconds"] = progress_poll_seconds
        if should_cancel is not None:
            progress_options["should_cancel"] = should_cancel
        completed = run_capped(
            [str(resolved_tool_path), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            **progress_options,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = (exc.stderr or exc.output or "").strip() if isinstance(exc.stderr, str) else ""
        detail = f": {stderr}" if stderr else ""
        raise StorageToolError(f"backup tool timed out after {timeout}s{detail}") from exc

    if completed.returncode != 0:
        stderr = (completed.stderr or completed.stdout or "").strip()
        raise StorageToolError(stderr or f"backup tool failed with exit code {completed.returncode}")

    return completed
