# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""abr-vhdx-tool, the data mover for Hyper-V disks.

The Windows counterpart of abr-vmdk-tool (the abr-vhdx-tools repository): the
same commands, options and ``--json`` results, and the same repository format.
It runs on the Hyper-V host itself, where it opens a checkpoint's disk through
the Windows virtual disk service, so the backup server starts it there over
WinRM (HyperVNode.run_disk_tool) and copies it to each host when it is missing
or out of date.

Both sides have to reach the repository: the tool on the host writes and reads
the disks, and the backup server keeps the manifests, merges chains, applies
retention and verifies with abr-vmdk-tool. An S3 repository is the one both
can reach; a folder on this appliance is not visible from Windows.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from ...core.storage_tool import APP_ROOT, REPO_ROOT, disk_tool_storage_args, is_s3_repository

TOOL_NAME = "abr-vhdx-tool.exe"
DEFAULT_REMOTE_TOOL_PATH = r"C:\Program Files\AltaStor\abr-vhdx-tool.exe"

HYPERV_REPOSITORY_MESSAGE = (
    "Hyper-V disks are written and read by abr-vhdx-tool on the Hyper-V host, which can reach S3 "
    "repositories only: a folder on this appliance is not visible from Windows. Use an S3 repository"
)

_PROGRESS_LINE = re.compile(r"^\s*(\d{1,3})%\s*$")
# What the tool says when it cannot take an incremental against the given RCT
# id (vhdx_disk.cpp, collectChanges). It refuses before anything is written,
# so the run can start over as a full backup.
_CHANGE_TRACKING_LOST = re.compile(
    r"resilient change tracking|failed to query what changed|start a new chain", re.IGNORECASE
)
# The S3 keys go to the tool as the AWS SDK's environment variables rather
# than on its command line, where every process on the host could read them.
_CREDENTIAL_FLAGS = {"--access-key": "AWS_ACCESS_KEY_ID", "--secret-key": "AWS_SECRET_ACCESS_KEY"}


class VhdxToolError(RuntimeError):
    pass


def local_tool_path() -> Path | None:
    """The appliance's copy of abr-vhdx-tool.exe, which is deployed to the
    hosts; None when there is none, and the hosts must have it already."""
    configured = os.environ.get("ALTASTOR_VHDX_TOOL_PATH", "").strip()
    candidates = [Path(configured).expanduser()] if configured else []
    candidates += [APP_ROOT / "tools" / "abr-vhdx-tool" / TOOL_NAME, REPO_ROOT / "tools" / "abr-vhdx-tool" / TOOL_NAME]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def remote_tool_path() -> str:
    """Where the tool lives on every Hyper-V host."""
    return os.environ.get("ALTASTOR_HYPERV_TOOL_PATH", "").strip() or DEFAULT_REMOTE_TOOL_PATH


_hashes: dict[tuple[str, int, int], str] = {}


def file_sha256(path: Path) -> str:
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _hashes:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        _hashes[key] = digest.hexdigest().upper()
    return _hashes[key]


def ensure_hyperv_repository(repository: dict[str, Any] | None) -> None:
    if not is_s3_repository(repository):
        raise VhdxToolError(HYPERV_REPOSITORY_MESSAGE)


def storage_invocation(
    repository: dict[str, Any], *, abr_path: str | None, create_folder: bool = False
) -> tuple[list[str], dict[str, str]]:
    """The tool's storage options for a repository, and the environment that
    carries its credentials."""
    ensure_hyperv_repository(repository)
    args = disk_tool_storage_args(repository, abr_path=abr_path, create_folder=create_folder)
    options: list[str] = []
    env: dict[str, str] = {}
    index = 0
    while index < len(args):
        flag = args[index]
        if flag in _CREDENTIAL_FLAGS and index + 1 < len(args):
            env[_CREDENTIAL_FLAGS[flag]] = args[index + 1]
            index += 2
            continue
        options.append(flag)
        index += 1
    return options, env


def progress_percent(line: str) -> int | None:
    match = _PROGRESS_LINE.match(line)
    return int(match.group(1)) if match else None


def parse_result(lines: Iterable[str]) -> dict[str, Any] | None:
    """The ``--json`` document the tool ends with: everything from the first
    line that opens it."""
    collected: list[str] = []
    for line in lines:
        if not collected and not line.lstrip().startswith("{"):
            continue
        collected.append(line)
    if not collected:
        return None
    try:
        parsed = json.loads("\n".join(collected))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def failure_message(result: dict[str, Any] | None, lines: Iterable[str], exit_code: int | None) -> str:
    if result and result.get("error"):
        return str(result["error"])
    errors = [line.strip() for line in lines if line.strip().startswith("Error:")]
    if errors:
        return "; ".join(error[len("Error:"):].strip() for error in errors)
    return f"exit code {exit_code}"


def change_tracking_lost(message: str) -> bool:
    return bool(_CHANGE_TRACKING_LOST.search(message or ""))
