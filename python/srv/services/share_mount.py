# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import re
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MOUNTINFO = Path("/proc/self/mountinfo")
SMB_FILESYSTEMS = frozenset({"cifs", "smb3", "smbfs"})
NFS_FILESYSTEMS = frozenset({"nfs", "nfs4"})
CHECK_NAME = "share_mount"


@dataclass(frozen=True)
class Mount:
    point: Path
    fstype: str
    source: str


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def read_mounts(mountinfo: Path = MOUNTINFO) -> list[Mount]:
    mounts: list[Mount] = []
    for line in mountinfo.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split()
        if "-" not in fields:
            continue
        separator = fields.index("-")
        if len(fields) < separator + 3 or len(fields) < 5:
            continue
        mounts.append(Mount(Path(_unescape(fields[4])), fields[separator + 1], _unescape(fields[separator + 2])))
    return mounts


def mount_holding(path: Path, mounts: list[Mount]) -> Mount | None:
    best: Mount | None = None
    for mount in mounts:
        if mount.point != path and mount.point not in path.parents:
            continue
        if best is None or len(mount.point.parts) >= len(best.point.parts):
            best = mount
    return best


def unc_parts(value: str) -> list[str]:
    return [part for part in value.replace("\\", "/").split("/") if part]


def _addresses(host: str) -> set[str]:
    try:
        return {str(entry[4][0]) for entry in socket.getaddrinfo(host, None)}
    except (OSError, UnicodeError):
        return set()


def same_server(first: str, second: str) -> bool:
    if first.strip().lower() == second.strip().lower():
        return True
    return bool(_addresses(first) & _addresses(second))


def is_this_appliance(host: str) -> bool:
    name = host.strip().lower()
    if name in {"localhost", socket.gethostname().lower(), socket.getfqdn().lower()}:
        return True
    for address in _addresses(host):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as probe:
                probe.bind((address, 0))
            return True
        except OSError:
            continue
    return False


def samba_share_path(name: str) -> str | None:
    testparm = shutil.which("testparm") or ("/usr/bin/testparm" if Path("/usr/bin/testparm").exists() else None)
    if testparm is None:
        raise FileNotFoundError("testparm is not installed")
    completed = subprocess.run(
        [testparm, "-s", f"--section-name={name}", "--parameter-name=path"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        return None
    return value


def _check(status: str, message: str) -> dict[str, Any]:
    return {"name": CHECK_NAME, "status": status, "message": message}


def share_mount_check(repository: dict[str, Any], mountinfo: Path = MOUNTINFO) -> dict[str, Any] | None:
    if str(repository.get("type") or "").strip().lower() != "smb":
        return None
    metadata = repository.get("metadata") if isinstance(repository.get("metadata"), dict) else {}
    if metadata.get("support_hyperv"):
        return _check("ok", "The share is this appliance's own share of the folder")
    server = str(repository.get("server") or metadata.get("server") or "").strip().strip("\\/")
    share = unc_parts(str(repository.get("share") or metadata.get("share") or ""))
    if not server or not share:
        return None
    unc = "\\\\" + "\\".join([server, *share])
    path = Path(str(repository.get("path") or "")).expanduser().resolve()
    try:
        mount = mount_holding(path, read_mounts(mountinfo))
    except OSError as exc:
        return _check("warning", f"Could not read what is mounted at {path} ({exc}), so it is not known to be {unc}")
    if mount is None:
        return _check("warning", f"Could not find what is mounted at {path}, so it is not known to be {unc}")
    relative = list(path.relative_to(mount.point).parts)

    if mount.fstype in SMB_FILESYSTEMS:
        source = unc_parts(mount.source)
        if not source:
            return _check("warning", f"{path} is on an SMB mount of {mount.source!r}, which does not name a share")
        location = [*source[1:], *relative]
        mounted = "\\\\" + "\\".join([source[0], *location])
        if same_server(source[0], server) and [part.lower() for part in location] == [part.lower() for part in share]:
            return _check("ok", f"{path} is {mounted}, mounted at {mount.point}")
        return _check(
            "failed",
            f"{path} is not the share {unc}: it is {mounted}, mounted at {mount.point}. Give the share that is "
            f"mounted there, or mount {unc} at {path}",
        )

    if mount.fstype in NFS_FILESYSTEMS:
        host = mount.source.split(":", 1)[0].strip("[]")
        if same_server(host, server):
            return _check(
                "warning",
                f"{path} is on the NFS export {mount.source} of the same server as {unc}; that the export and the "
                "share are the same folder cannot be checked from here",
            )
        return _check(
            "failed",
            f"{path} is not the share {unc}: it is on the NFS export {mount.source}, mounted at {mount.point}, "
            f"of another server. Mount {unc} at {path}",
        )

    if is_this_appliance(server):
        try:
            exported = samba_share_path(share[0])
        except (OSError, subprocess.SubprocessError) as exc:
            return _check(
                "warning",
                f"The share {unc} is on this appliance, but its Samba configuration could not be read ({exc}), "
                f"so it is not known to share {path}",
            )
        if exported is None:
            return _check("failed", f"This appliance has no Samba share named {share[0]}, so {unc} does not exist")
        served = Path(exported).expanduser().resolve().joinpath(*share[1:])
        if served == path:
            return _check("ok", f"The share {unc} is served by this appliance from {path}")
        return _check(
            "failed",
            f"{path} is not the share {unc}: this appliance shares {served} under that name. Give {served} as the "
            "folder, or the share of this folder",
        )
    return _check(
        "failed",
        f"{path} is not the share {unc}: it is a folder on this appliance's own {mount.fstype} filesystem at "
        f"{mount.point}, so the Hyper-V hosts would write to the share and this appliance to its own disk. "
        f"Mount the share at {path} first (for example mount -t cifs //{server}/{'/'.join(share)} {path}), "
        "then add the repository",
    )
