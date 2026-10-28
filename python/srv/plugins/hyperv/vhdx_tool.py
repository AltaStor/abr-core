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
retention and verifies with abr-vmdk-tool. Both reach an S3 repository. An SMB
repository is a share the appliance mounts at its path: its server and share
name the UNC path the hosts use for the same folder. A local repository is a
folder of the appliance alone, which the hosts cannot reach.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ...core.storage_tool import APP_ROOT, REPO_ROOT, RETENTION_TILL_OPTION, disk_tool_storage_args, is_s3_repository

TOOL_NAME = "abr-vhdx-tool.exe"
HOST_ARCHITECTURE = "x64"
DEFAULT_REMOTE_TOOL_PATH = r"C:\Program Files\AltaStor\abr-vhdx-tool.exe"
DEFAULT_REMOTE_WORK_DIR = r"C:\ProgramData\AltaStor\abr-hyperv"

HYPERV_REPOSITORY_MESSAGE = (
    "Hyper-V disks are written and read by abr-vhdx-tool on the Hyper-V host, which reaches S3 "
    "and SMB repositories only, not a folder of the appliance. Use an S3 repository, or an SMB "
    "repository (abr repo add --type smb --path --server --share)"
)

_PROGRESS_LINE = re.compile(r"^\s*(\d{1,3})%\s*$")
# What the tool says when it cannot take an incremental against the given RCT
# id (vhdx_disk.cpp, collectChanges). It refuses before anything is written,
# so the run can start over as a full backup.
_CHANGE_TRACKING_LOST = re.compile(
    r"resilient change tracking|failed to query what changed|start a new chain", re.IGNORECASE
)


class VhdxToolError(RuntimeError):
    pass


def local_tool_path() -> Path | None:
    """The appliance's copy of abr-vhdx-tool.exe, which is deployed to the
    hosts; None when there is none, and the hosts must have it already."""
    configured = os.environ.get("ALTASTOR_VHDX_TOOL_PATH", "").strip()
    candidates = [Path(configured).expanduser()] if configured else []
    for root in (APP_ROOT, REPO_ROOT):
        folder = root / "tools" / "abr-vhdx-tool"
        candidates += [folder / HOST_ARCHITECTURE / TOOL_NAME, folder / TOOL_NAME]
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


_retention_till_support: dict[tuple[str, int, int], bool] = {}


def supports_retention_till() -> bool:
    tool = local_tool_path()
    if tool is None:
        return False
    stat = tool.stat()
    key = (str(tool), stat.st_size, stat.st_mtime_ns)
    if key not in _retention_till_support:
        needle = RETENTION_TILL_OPTION.encode("ascii")
        found = False
        tail = b""
        with open(tool, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                if needle in tail + block:
                    found = True
                    break
                tail = block[-len(needle):]
        _retention_till_support[key] = found
    return _retention_till_support[key]


def remote_work_dir() -> str:
    return (os.environ.get("ALTASTOR_HYPERV_WORK_DIR", "").strip() or DEFAULT_REMOTE_WORK_DIR).rstrip("\\")


@dataclass(frozen=True)
class HostBundle:
    bundle_id: str
    files: tuple[tuple[str, Path, str], ...]
    has_tool: bool

    def folder(self, root: str) -> str:
        return root.rstrip("\\") + "\\" + self.bundle_id


def host_bundle(script_dir: Path) -> HostBundle:
    files = [(f"scripts\\{path.name}", path, file_sha256(path)) for path in sorted(script_dir.glob("*.ps1"))]
    tool = local_tool_path()
    if tool is not None:
        files.append((TOOL_NAME, tool, file_sha256(tool)))
    files.sort(key=lambda item: item[0].lower())
    manifest = "".join(f"{name}\t{digest}\n" for name, _path, digest in files)
    return HostBundle(
        bundle_id=hashlib.sha256(manifest.encode("utf-8")).hexdigest(),
        files=tuple(files),
        has_tool=tool is not None,
    )


def _is_smb_repository(repository: dict[str, Any] | None) -> bool:
    return isinstance(repository, dict) and str(repository.get("type") or "").strip().lower() == "smb"


def _repository_field(repository: dict[str, Any], name: str) -> str:
    metadata = repository.get("metadata") if isinstance(repository.get("metadata"), dict) else {}
    return str(repository.get(name) or metadata.get(name) or "")


def windows_share(repository: dict[str, Any] | None) -> str | None:
    r"""The UNC path Hyper-V hosts reach an SMB repository's folder by,
    \\server\share[\folder], from its server and share; None without them."""
    if not _is_smb_repository(repository):
        return None
    server = _repository_field(repository, "server").strip().strip("\\/")
    share = _repository_field(repository, "share").strip().replace("/", "\\").strip("\\")
    if not server or not share:
        return None
    return f"\\\\{server}\\{share}"


def ensure_hyperv_repository(repository: dict[str, Any] | None) -> None:
    if not is_s3_repository(repository) and windows_share(repository) is None:
        raise VhdxToolError(HYPERV_REPOSITORY_MESSAGE)


def _share_invocation(
    repository: dict[str, Any], *, abr_path: str | None, create_folder: bool
) -> tuple[list[str], dict[str, str]]:
    """An SMB repository's folder as the hosts see it: the same place under
    its share, which run_tool.ps1 opens with the repository's account (a
    WinRM session has no network credentials of its own)."""
    share = windows_share(repository) or ""
    root = Path(str(repository.get("path") or "")).expanduser().resolve()
    target = Path(abr_path).expanduser().resolve() if abr_path else root
    try:
        relative = target.relative_to(root)
    except ValueError:
        raise VhdxToolError(
            f"{target} is outside the folder of repository {repository.get('name')} ({root}), "
            f"which is all the hosts reach through {share}"
        ) from None
    folder = "\\".join([share, *relative.parts])
    args = ["--mode", "file", "--folder", folder]
    if create_folder:
        args.append("--create-folder")
    server, share_name = share.lstrip("\\").split("\\")[:2]
    env = {"ABR_SMB_SHARE": f"\\\\{server}\\{share_name}"}
    username = _repository_field(repository, "username").strip()
    if username:
        env["ABR_SMB_USER"] = username
        env["ABR_SMB_PASSWORD"] = _repository_field(repository, "password")
    return args, env


def storage_invocation(
    repository: dict[str, Any], *, abr_path: str | None, create_folder: bool = False
) -> tuple[list[str], dict[str, str]]:
    """The tool's storage options for a repository, and the environment that
    carries its credentials."""
    ensure_hyperv_repository(repository)
    if not is_s3_repository(repository):
        return _share_invocation(repository, abr_path=abr_path, create_folder=create_folder)
    return disk_tool_storage_args(repository, abr_path=abr_path, create_folder=create_folder), {}


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


FAILURE_DETAIL_LINES = 5


def _tool_error(result: dict[str, Any] | None, lines: list[str], exit_code: int | None) -> str:
    if result and result.get("error"):
        return str(result["error"])
    errors = [line.strip() for line in lines if line.strip().startswith("Error:")]
    if errors:
        return "; ".join(error[len("Error:"):].strip() for error in errors)
    return f"exit code {exit_code}"


def failure_message(result: dict[str, Any] | None, lines: Iterable[str], exit_code: int | None) -> str:
    lines = list(lines)
    error = _tool_error(result, lines, exit_code)
    details: list[str] = []
    for line in lines:
        text = line.strip()
        if text.startswith("{"):
            break
        if text.startswith("Error:"):
            text = text[len("Error:"):].strip()
        if text and text not in error and text not in details:
            details.append(text)
    return "; ".join([*details[-FAILURE_DETAIL_LINES:], error])


def change_tracking_lost(result: dict[str, Any] | None, lines: Iterable[str], exit_code: int | None) -> bool:
    return bool(_CHANGE_TRACKING_LOST.search(_tool_error(result, list(lines), exit_code)))
