# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

import os
import pwd
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from python.srv.brokers import broker_core
from python.srv.brokers.broker_core import (
    FAILED,
    INVALID_REQUEST,
    REFUSED,
    BrokerError,
    Operation,
)

DESCRIPTION = "Root broker that mounts and unmounts the remote SMB shares of AltaStor Backup repositories, one request per run."
MOUNTS_ROOT = Path("/run/altastor-backup-mounts")
MOUNTINFO = Path("/proc/self/mountinfo")
POINT_MODE = 0o555
TRIAL_SUFFIX = ".trial"
BUSY = "busy"
CIFS_FILESYSTEMS = frozenset({"cifs", "smb3"})
COMMAND_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
COMMAND_TIMEOUT_SECONDS = 90
REPOSITORY_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
HOST_NAME = re.compile(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")
IPV6 = re.compile(r"[0-9A-Fa-f:.]{2,45}")
DOMAIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,254})")
FORBIDDEN_IN_OPTIONS = frozenset(",=")
FORBIDDEN_IN_NAMES = frozenset('/\\:*?"<>|')
MAX_PASSWORD_LENGTH = 256
MOUNT_FIELDS = ("repository_id", "server", "share", "username", "password")
OPTIONAL_MOUNT_FIELDS = ("subfolder", "domain", "trial")
UNMOUNT_FIELDS = ("repository_id",)
OPTIONAL_UNMOUNT_FIELDS = ("trial",)
UNMOUNTED = 0
UNMOUNT_FAILED = 1


@dataclass(frozen=True, slots=True)
class Mount:
    point: Path
    fstype: str
    source: str


@dataclass(frozen=True, slots=True)
class System:
    service_user: str
    root: Path = MOUNTS_ROOT
    mountinfo: Path = MOUNTINFO
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    pipe: Callable[[], tuple[int, int]] = field(default=os.pipe)
    geteuid: Callable[[], int] = field(default=os.geteuid)
    root_uid: int = 0


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def read_mounts(system: System) -> list[Mount]:
    mounts = []
    for line in system.mountinfo.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split()
        if "-" not in fields:
            continue
        separator = fields.index("-")
        if len(fields) < separator + 3 or len(fields) < 5:
            continue
        mounts.append(Mount(Path(_unescape(fields[4])), fields[separator + 1], _unescape(fields[separator + 2])))
    return mounts


def mount_at(system: System, point: Path) -> Mount | None:
    found = [mount for mount in read_mounts(system) if mount.point == point]
    return found[-1] if found else None


def source_of(server: str, share: str, subfolder: str) -> str:
    return "/".join(["/", server, share, *([subfolder] if subfolder else [])])


def same_source(first: str, second: str) -> bool:
    def normalized(value: str) -> list[str]:
        return [part.lower() for part in value.replace("\\", "/").split("/") if part]

    return normalized(first) == normalized(second)


def _text(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name, "")
    if not isinstance(value, str):
        raise BrokerError(INVALID_REQUEST, f"{name} must be a text")
    return value


def _arguments(arguments: Mapping[str, Any], required: Sequence[str], optional: Sequence[str]) -> dict[str, Any]:
    unknown = sorted(set(arguments) - set(required) - set(optional))
    missing = sorted(set(required) - set(arguments))
    if unknown or missing:
        raise BrokerError(
            INVALID_REQUEST,
            f"the arguments must be {', '.join(required)}" + (f", and may be {', '.join(optional)}" if optional else "")
            + (f"; unknown: {', '.join(unknown)}" if unknown else "") + (f"; missing: {', '.join(missing)}" if missing else ""),
        )
    if "trial" in arguments and not isinstance(arguments["trial"], bool):
        raise BrokerError(INVALID_REQUEST, "trial must be true or false")
    return dict(arguments)


def _plain(value: str, what: str, *, extra: frozenset[str] = frozenset(), required: bool = True) -> str:
    if not value:
        if required:
            raise BrokerError(INVALID_REQUEST, f"the {what} must be given")
        return value
    if value != value.strip() or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise BrokerError(INVALID_REQUEST, f"the {what} must not start or end with blanks or hold control characters")
    bad = sorted((FORBIDDEN_IN_OPTIONS | extra) & set(value))
    if bad:
        raise BrokerError(INVALID_REQUEST, f"the {what} must not hold {' '.join(bad)}")
    return value


def _repository_id(value: str) -> str:
    if not REPOSITORY_ID.fullmatch(value):
        raise BrokerError(INVALID_REQUEST, "the repository id must be a repository's UUID")
    return value


def _server(value: str) -> str:
    _plain(value, "server")
    bare = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    if not (HOST_NAME.fullmatch(bare) or (":" in bare and IPV6.fullmatch(bare))):
        raise BrokerError(INVALID_REQUEST, "the server must be a host name or an IP address")
    return bare


def _subfolder(value: str) -> str:
    _plain(value.replace("\\", "/"), "subfolder", extra=frozenset(':*?"<>|'), required=False)
    parts = [part for part in value.replace("\\", "/").split("/") if part]
    if any(part in (".", "..") or part != part.strip() for part in parts):
        raise BrokerError(INVALID_REQUEST, "the subfolder must be a path inside the share, without . or .. parts")
    return "/".join(parts)


def _domain(value: str) -> str:
    _plain(value, "domain", required=False)
    if value and not DOMAIN.fullmatch(value):
        raise BrokerError(INVALID_REQUEST, "the domain must be a domain or workgroup name")
    return value


def _password(value: str) -> str:
    if not value:
        raise BrokerError(INVALID_REQUEST, "the password must be given")
    if "\x00" in value or len(value) > MAX_PASSWORD_LENGTH:
        raise BrokerError(INVALID_REQUEST, f"the password must be at most {MAX_PASSWORD_LENGTH} characters without NUL")
    return value


def _service_ids(system: System) -> tuple[int, int]:
    try:
        account = pwd.getpwnam(system.service_user)
    except KeyError as exc:
        raise BrokerError(REFUSED, f"there is no account {system.service_user} for the mounted files to belong to") from exc
    return account.pw_uid, account.pw_gid


def _point(system: System, repository_id: str, trial: bool) -> Path:
    return system.root / f"{repository_id}{TRIAL_SUFFIX if trial else ''}"


def _make_point(system: System, point: Path) -> None:
    try:
        root = os.lstat(system.root)
    except FileNotFoundError as exc:
        raise BrokerError(REFUSED, f"{system.root} does not exist; the package creates it at boot") from exc
    if not stat.S_ISDIR(root.st_mode) or root.st_uid != system.root_uid or root.st_mode & 0o022:
        raise BrokerError(REFUSED, f"{system.root} must be a folder of root that only root can write to")
    try:
        os.mkdir(point, POINT_MODE)
    except FileExistsError:
        pass
    status = os.lstat(point)
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != system.root_uid:
        raise BrokerError(REFUSED, f"{point} exists but is not a folder of root; it is left as it is")
    if os.listdir(point):
        raise BrokerError(REFUSED, f"{point} is not empty; it is left as it is")
    os.chmod(point, POINT_MODE)


def _remove_point(point: Path) -> None:
    try:
        os.rmdir(point)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"mounts_broker: {point} was not removed: {exc.strerror or exc}", file=sys.stderr, flush=True)


def _options(system: System, username: str, domain: str) -> str:
    uid, gid = _service_ids(system)
    options = [
        "vers=3.1.1",
        "seal",
        f"uid={uid}",
        f"gid={gid}",
        "file_mode=0640",
        "dir_mode=0750",
        "iocharset=utf8",
        "nosuid",
        "nodev",
        "noexec",
        "soft",
        f"username={username}",
    ]
    if domain:
        options.append(f"domain={domain}")
    return ",".join(options)


def _last_line(done: subprocess.CompletedProcess[str]) -> str:
    lines = [line.strip() for line in (done.stderr or "").splitlines() + (done.stdout or "").splitlines() if line.strip()]
    return lines[-1] if lines else f"exit status {done.returncode}"


MOUNT_ERROR = re.compile(r"mount error\((\d+)\):\s*(.*)")
MOUNT_HINTS = {
    2: "the share or the folder inside it does not exist",
    11: "the server does not know this account, or lets it in only as a guest, and a guest session cannot be encrypted",
    13: "the user name or password is wrong, or the account may not open this share",
    95: "the server does not offer SMB 3.1.1 with encryption",
    110: "the server did not answer; check its address and that port 445 is open",
    111: "the server refused the connection on port 445",
    112: "the server is down",
    113: "the server cannot be reached",
    115: "the server did not answer; check its address and that port 445 is open",
}


NO_ADDRESS = re.compile(r"(?<=\S)Unable to find suitable address\.?")
GLUED_SENTENCE = re.compile(r"(?<=[a-z0-9.)\]])(?=[A-Z][a-z]+ [a-z])")


def unglued(text: str) -> str:
    return GLUED_SENTENCE.sub("; ", NO_ADDRESS.sub(": no address answered", text)).strip()


def mount_failure(done: subprocess.CompletedProcess[str]) -> str:
    for line in (done.stderr or "").splitlines() + (done.stdout or "").splitlines():
        found = MOUNT_ERROR.search(line)
        if found:
            number, text = int(found.group(1)), unglued(found.group(2).strip())
            hint = MOUNT_HINTS.get(number)
            return f"mount error {number} ({text})" + (f": {hint}" if hint else "")
    return _last_line(done)


def _mount_cifs(system: System, source: str, point: Path, options: str, password: str) -> None:
    reader, writer = system.pipe()
    try:
        os.write(writer, password.encode())
    finally:
        os.close(writer)
    try:
        done = system.run(
            ["mount.cifs", source, str(point), "-o", options],
            capture_output=True,
            text=True,
            env={"PATH": COMMAND_PATH, "LC_ALL": "C", "PASSWD_FD": str(reader)},
            pass_fds=(reader,),
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BrokerError(FAILED, f"mount.cifs could not mount {source}: {exc}") from exc
    finally:
        os.close(reader)
    if done.returncode != 0:
        raise BrokerError(REFUSED, f"{source} could not be mounted: {mount_failure(done)}")


def mount_share(system: System, arguments: Mapping[str, Any]) -> dict[str, Any]:
    given = _arguments(arguments, MOUNT_FIELDS, OPTIONAL_MOUNT_FIELDS)
    repository_id = _repository_id(_text(given, "repository_id"))
    server = _server(_text(given, "server"))
    share = _plain(_text(given, "share"), "share", extra=FORBIDDEN_IN_NAMES)
    subfolder = _subfolder(_text(given, "subfolder"))
    username = _plain(_text(given, "username"), "username", extra=frozenset('/:*?"<>|'))
    domain = _domain(_text(given, "domain"))
    password = _password(_text(given, "password"))
    trial = bool(given.get("trial", False))
    point = _point(system, repository_id, trial)
    source = source_of(server, share, subfolder)

    existing = mount_at(system, point)
    if existing is not None:
        if existing.fstype in CIFS_FILESYSTEMS and same_source(existing.source, source):
            return {"mount_point": str(point), "source": existing.source, "already_mounted": True}
        raise BrokerError(REFUSED, f"{existing.source} is already mounted at {point}; unmount it first")

    options = _options(system, username, domain)
    _make_point(system, point)
    try:
        _mount_cifs(system, source, point, options, password)
    except BrokerError:
        _remove_point(point)
        raise
    mounted = mount_at(system, point)
    if mounted is None or mounted.fstype not in CIFS_FILESYSTEMS:
        raise BrokerError(FAILED, f"mount.cifs reported success, but nothing is mounted at {point}")
    return {"mount_point": str(point), "source": mounted.source, "already_mounted": False}


def _umount(system: System, point: Path, *, lazy: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return system.run(
            ["umount", *(["-l"] if lazy else []), str(point)],
            capture_output=True,
            text=True,
            env={"PATH": COMMAND_PATH, "LC_ALL": "C"},
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BrokerError(FAILED, f"umount could not unmount {point}: {exc}") from exc


def unmount_share(system: System, arguments: Mapping[str, Any]) -> dict[str, Any]:
    given = _arguments(arguments, UNMOUNT_FIELDS, OPTIONAL_UNMOUNT_FIELDS)
    repository_id = _repository_id(_text(given, "repository_id"))
    point = _point(system, repository_id, bool(given.get("trial", False)))
    existing = mount_at(system, point)
    if existing is None:
        _remove_point(point)
        return {"mount_point": str(point), "unmounted": False}
    done = _umount(system, point)
    if done.returncode != 0:
        reason = _last_line(done)
        if "busy" in reason.lower():
            raise BrokerError(BUSY, f"{existing.source} at {point} is in use, so it is not unmounted")
        raise BrokerError(FAILED, f"{existing.source} at {point} could not be unmounted: {reason}")
    if mount_at(system, point) is not None:
        raise BrokerError(FAILED, f"umount reported success, but {point} is still mounted")
    _remove_point(point)
    return {"mount_point": str(point), "source": existing.source, "unmounted": True}


def _ours(system: System, point: Path) -> bool:
    name = point.name.removesuffix(TRIAL_SUFFIX)
    return point.parent == system.root and bool(REPOSITORY_ID.fullmatch(name))


def unmount_all(system: System) -> dict[str, Any]:
    unmounted, lazily, failed = [], [], []
    for mount in reversed(read_mounts(system)):
        if not _ours(system, mount.point) or mount.point in [Path(item) for item in unmounted + lazily]:
            continue
        done = _umount(system, mount.point)
        if done.returncode == 0:
            unmounted.append(str(mount.point))
            continue
        done = _umount(system, mount.point, lazy=True)
        if done.returncode == 0:
            lazily.append(str(mount.point))
        else:
            failed.append(f"{mount.point}: {_last_line(done)}")
    if system.root.is_dir():
        for point in sorted(system.root.iterdir()):
            if _ours(system, point) and mount_at(system, point) is None:
                _remove_point(point)
    return {"unmounted": unmounted, "lazily": lazily, "failed": failed}


def unmount_from_command_line(system: System) -> int:
    if system.geteuid() != 0:
        print("mounts_broker: --unmount-all must run as root", file=sys.stderr)
        return UNMOUNT_FAILED
    try:
        result = unmount_all(system)
    except (BrokerError, OSError) as exc:
        print(f"mounts_broker: the shares could not be unmounted: {getattr(exc, 'message', exc)}", file=sys.stderr)
        return UNMOUNT_FAILED
    if not result["unmounted"] and not result["lazily"] and not result["failed"]:
        print("No AltaStor Backup shares to unmount.")
        return UNMOUNTED
    if result["unmounted"]:
        print(f"Unmounted {', '.join(result['unmounted'])}.")
    if result["lazily"]:
        print(f"Detached {', '.join(result['lazily'])}, which were in use; they go once nothing uses them.")
    if result["failed"]:
        print(f"mounts_broker: could not unmount {'; '.join(result['failed'])}", file=sys.stderr)
        return UNMOUNT_FAILED
    return UNMOUNTED


def operations(system: System) -> dict[str, Operation]:
    return {
        "mount": lambda arguments: mount_share(system, arguments),
        "unmount": lambda arguments: unmount_share(system, arguments),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = broker_core.argument_parser(DESCRIPTION)
    parser.add_argument(
        "--unmount-all",
        action="store_true",
        help=f"as root from the command line, without the socket: unmount every share under {MOUNTS_ROOT}, detaching those in use",
    )
    options = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    os.umask(0o077)
    system = System(options.service_user)
    if options.unmount_all:
        return unmount_from_command_line(system)
    return broker_core.serve(operations(system), service_user=options.service_user)


if __name__ == "__main__":
    sys.exit(main())
