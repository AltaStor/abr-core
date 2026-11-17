# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import fcntl
import grp
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
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

DESCRIPTION = "Root broker that adds and removes the Samba shares of AltaStor Backup repositories, one request per run."
SMB_CONF = Path("/etc/samba/smb.conf")
SHARES_CONF = Path("/etc/altastor-backup/samba-shares.conf")
LOCK_FILE = Path("/run/lock/altastor-backup-shares.lock")
SHARES_CONF_MODE = 0o640
FOLDER_MODE = 0o755
ACCOUNT_COMMENT = "AltaStor Backup SMB share"
OUR_ACCOUNT_COMMENTS = frozenset({ACCOUNT_COMMENT, "AltaStor Backup Hyper-V share"})
SHARE_PATTERN = re.compile(r"abr-[a-z0-9]{8}\$")
USER_PATTERN = re.compile(r"abr-smb-[a-z0-9]{8}")
PASSWORD_PATTERN = re.compile(r"[A-Za-z0-9]{32,128}")
SAMBA_TOOLS = ("smbd", "testparm", "smbpasswd", "pdbedit", "smbcontrol")
COMMAND_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
COMMAND_ENVIRONMENT = {"PATH": COMMAND_PATH, "LC_ALL": "C"}
COMMAND_TIMEOUT_SECONDS = 60
SHARE_SETTINGS = {
    "browseable": "no",
    "read only": "no",
    "guest ok": "no",
    "create mask": "0640",
    "directory mask": "0750",
    "server smb encrypt": "required",
}
ADD_FIELDS = ("share", "user", "password", "path")
REMOVE_FIELDS = ("share", "user")
CLOSED = 0
CLOSE_FAILED = 1
CLOSED_WITHOUT_RELOAD = 2


@dataclass(frozen=True, slots=True)
class Samba:
    service_user: str
    smb_conf: Path = SMB_CONF
    shares_conf: Path = SHARES_CONF
    lock_file: Path = LOCK_FILE
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    which: Callable[[str], str | None] = field(default=lambda tool: shutil.which(tool, path=COMMAND_PATH))
    chown: Callable[[str, int, int], None] = os.chown

    @property
    def include_line(self) -> str:
        return f"include = {self.shares_conf}"


def _command(samba: Samba, argv: Sequence[str], *, stdin: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        done = samba.run(
            list(argv),
            input=stdin,
            capture_output=True,
            text=True,
            env=COMMAND_ENVIRONMENT,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BrokerError(FAILED, f"{argv[0]} could not be run: {exc}") from exc

    if check and done.returncode != 0:
        lines = [line for line in (done.stderr or done.stdout or "").splitlines() if line.strip()]
        raise BrokerError(FAILED, f"{' '.join(argv[:2])} failed: {lines[-1].strip() if lines else f'exit status {done.returncode}'}")
    return done


def _service_account(samba: Samba) -> pwd.struct_passwd:
    try:
        return pwd.getpwnam(samba.service_user)
    except KeyError as exc:
        raise BrokerError(REFUSED, f"there is no account {samba.service_user} for the shared folders to belong to") from exc


def _arguments(arguments: Mapping[str, Any], names: Sequence[str]) -> dict[str, str]:
    if set(arguments) != set(names) or not all(isinstance(arguments[name], str) for name in names):
        raise BrokerError(INVALID_REQUEST, f"the arguments must be exactly {', '.join(names)}, each a text")
    return {name: str(arguments[name]) for name in names}


def _check_name(value: str, pattern: re.Pattern[str], what: str, example: str) -> str:
    if not pattern.fullmatch(value):
        raise BrokerError(INVALID_REQUEST, f"the {what} must look like {example}")
    return value


def _check_folder(samba: Samba, raw: str) -> tuple[Path, str]:
    if not raw.startswith("/"):
        raise BrokerError(INVALID_REQUEST, "the folder must be an absolute path")
    if not os.path.isdir(raw):
        raise BrokerError(REFUSED, f"{raw} is not a folder")
    if os.path.realpath(raw) != raw:
        raise BrokerError(REFUSED, f"{raw} must be given without symbolic links or relative parts")

    status = os.stat(raw)
    if status.st_uid != _service_account(samba).pw_uid:
        owner = _user_name(status.st_uid)
        raise BrokerError(REFUSED, f"{raw} belongs to {owner}, not to {samba.service_user}; only a folder of {samba.service_user} is shared")
    return Path(raw), _group_name(status.st_gid)


def _user_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid {uid}"


def _group_name(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def parse_sections(text: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    name = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1]
            sections[name] = {}
        elif name is not None and "=" in line and not line.startswith(("#", ";")):
            key, _, value = line.partition("=")
            sections[name][key.strip().lower()] = value.strip()
    return sections


def render_sections(sections: Mapping[str, Mapping[str, str]]) -> str:
    blocks = [f"[{name}]\n" + "".join(f"    {key} = {value}\n" for key, value in values.items()) for name, values in sections.items()]
    return "\n".join(blocks)


def _our_sections(samba: Samba) -> dict[str, dict[str, str]]:
    return parse_sections(samba.shares_conf.read_text()) if samba.shares_conf.is_file() else {}


def _configured_names(samba: Samba) -> set[str]:
    return {name.lower() for name in parse_sections(_command(samba, ["testparm", "-s", str(samba.smb_conf)], check=False).stdout)}


def _parameter(samba: Samba, name: str) -> str:
    return _command(samba, ["testparm", "-s", f"--parameter-name={name}", str(samba.smb_conf)], check=False).stdout.strip().lower()


def smbd_running(samba: Samba) -> bool:
    return _command(samba, ["systemctl", "is-active", "--quiet", "smbd"], check=False).returncode == 0


def _preflight(samba: Samba) -> None:
    missing = [tool for tool in SAMBA_TOOLS if samba.which(tool) is None]
    if missing or not samba.smb_conf.is_file():
        raise BrokerError(REFUSED, f"Samba is not installed (missing: {' '.join(missing) or samba.smb_conf})")
    if not smbd_running(samba):
        raise BrokerError(
            REFUSED,
            "smbd is not running; this broker never starts it, since another SMB server may own port 445 on purpose. "
            "Start it with systemctl start smbd and try again",
        )
    if _command(samba, ["testparm", "-s", str(samba.smb_conf)], check=False).returncode != 0:
        raise BrokerError(REFUSED, f"the existing Samba configuration does not validate (testparm -s {samba.smb_conf}); it is left as it is")

    if "active directory domain controller" in _parameter(samba, "server role"):
        raise BrokerError(REFUSED, "this Samba is an Active Directory domain controller; its configuration is left as it is")
    if "registry" in _parameter(samba, "config backend"):
        raise BrokerError(REFUSED, "this Samba keeps its configuration in the registry (config backend = registry); it is left as it is")
    passdb = _parameter(samba, "passdb backend")
    if not passdb.startswith("tdbsam"):
        raise BrokerError(REFUSED, f"this Samba keeps its accounts in '{passdb}', not tdbsam; no account is added to it")


@contextmanager
def _locked(samba: Samba) -> Generator[None]:
    samba.lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(samba.lock_file, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _ensure_folder(folder: Path) -> None:
    if folder.is_dir():
        return
    _ensure_folder(folder.parent)
    folder.mkdir(exist_ok=True)
    os.chmod(folder, FOLDER_MODE)


def _write(samba: Samba, path: Path, text: str, *, mode: int, uid: int, gid: int) -> None:
    _ensure_folder(path.parent)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        samba.chown(temporary, uid, gid)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


@dataclass(slots=True)
class _Snapshot:
    smb_conf: str
    smb_conf_mode: int
    smb_conf_owner: tuple[int, int]
    shares_conf: str | None


def _snapshot(samba: Samba) -> _Snapshot:
    status = samba.smb_conf.stat()
    return _Snapshot(
        smb_conf=samba.smb_conf.read_text(),
        smb_conf_mode=status.st_mode & 0o7777,
        smb_conf_owner=(status.st_uid, status.st_gid),
        shares_conf=samba.shares_conf.read_text() if samba.shares_conf.is_file() else None,
    )


def _write_smb_conf(samba: Samba, snapshot: _Snapshot, text: str) -> None:
    uid, gid = snapshot.smb_conf_owner
    _write(samba, samba.smb_conf, text, mode=snapshot.smb_conf_mode, uid=uid, gid=gid)


def _write_shares(samba: Samba, sections: Mapping[str, Mapping[str, str]]) -> None:
    _write(samba, samba.shares_conf, render_sections(sections), mode=SHARES_CONF_MODE, uid=0, gid=_service_account(samba).pw_gid)


def _restore(samba: Samba, snapshot: _Snapshot) -> None:
    if samba.smb_conf.read_text() != snapshot.smb_conf:
        _write_smb_conf(samba, snapshot, snapshot.smb_conf)
    if snapshot.shares_conf is None:
        samba.shares_conf.unlink(missing_ok=True)
    elif not samba.shares_conf.is_file() or samba.shares_conf.read_text() != snapshot.shares_conf:
        _write(samba, samba.shares_conf, snapshot.shares_conf, mode=SHARES_CONF_MODE, uid=0, gid=_service_account(samba).pw_gid)


def _with_include(samba: Samba, text: str) -> str:
    if samba.include_line in text.splitlines():
        return text
    return text + ("" if not text or text.endswith("\n") else "\n") + samba.include_line + "\n"


def _without_include(samba: Samba, text: str) -> str:
    return "".join(line for line in text.splitlines(keepends=True) if line.rstrip("\r\n") != samba.include_line)


def _account_comment(samba: Samba, user: str) -> str | None:
    entry = _command(samba, ["getent", "passwd", user], check=False)
    if entry.returncode != 0:
        return None
    fields = entry.stdout.strip().split(":")
    return fields[4] if len(fields) > 4 else ""


def _samba_account_exists(samba: Samba, user: str) -> bool:
    if samba.which("pdbedit") is None:
        return False
    return _command(samba, ["pdbedit", "-L", "-u", user], check=False).returncode == 0


def _remove_account(samba: Samba, user: str) -> bool:
    removed = False
    if _samba_account_exists(samba, user):
        _command(samba, ["pdbedit", "-x", "-u", user])
        removed = True
    if _account_comment(samba, user) in OUR_ACCOUNT_COMMENTS:
        _command(samba, ["userdel", user])
        removed = True
    return removed


def _ensure_system_account(samba: Samba, user: str) -> bool:
    comment = _account_comment(samba, user)
    if comment is not None and comment not in OUR_ACCOUNT_COMMENTS:
        raise BrokerError(REFUSED, f"a system account named {user} already exists and is not one AltaStor Backup made")
    if comment is not None:
        return False

    _command(
        samba,
        [
            "useradd", "--system", "--no-create-home", "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin",
            "--no-user-group", "--gid", "nogroup", "--comment", ACCOUNT_COMMENT, user,
        ],
    )
    return True


def _set_samba_password(samba: Samba, user: str, password: str) -> None:
    _command(samba, ["smbpasswd", "-s", "-a", user], stdin=f"{password}\n{password}\n")
    _command(samba, ["smbpasswd", "-e", user])


def _reload(samba: Samba) -> None:
    _command(samba, ["smbcontrol", "smbd", "reload-config"])
    if not smbd_running(samba):
        raise BrokerError(FAILED, "smbd stopped after reloading its configuration")


def add_share(samba: Samba, arguments: Mapping[str, Any]) -> dict[str, Any]:
    request = _arguments(arguments, ADD_FIELDS)
    share = _check_name(request["share"], SHARE_PATTERN, "share name", "abr-xxxxxxxx$")
    user = _check_name(request["user"], USER_PATTERN, "account name", "abr-smb-xxxxxxxx")
    password = _check_name(request["password"], PASSWORD_PATTERN, "password", "32 to 128 letters and digits")
    folder, group = _check_folder(samba, request["path"])

    with _locked(samba):
        _preflight(samba)
        sections = _our_sections(samba)
        if share in sections:
            raise BrokerError(REFUSED, f"{share} is already one of the shares of AltaStor Backup")
        for name, values in sections.items():
            if values.get("path") == str(folder):
                raise BrokerError(REFUSED, f"{folder} is already shared as {name}")
            if values.get("valid users") == user:
                raise BrokerError(REFUSED, f"the account {user} already belongs to the share {name}")
        if share.lower() in _configured_names(samba):
            raise BrokerError(REFUSED, f"a share named {share} already exists in the Samba configuration; it is left as it is")

        snapshot = _snapshot(samba)
        created = False
        try:
            sections[share] = {"path": str(folder), **SHARE_SETTINGS, "valid users": user, "force user": samba.service_user, "force group": group}
            _write_shares(samba, sections)
            included = _with_include(samba, snapshot.smb_conf)
            if included != snapshot.smb_conf:
                _write_smb_conf(samba, snapshot, included)
            if _command(samba, ["testparm", "-s", str(samba.smb_conf)], check=False).returncode != 0:
                raise BrokerError(FAILED, "the Samba configuration does not validate with the share added")
            if share.lower() not in _configured_names(samba):
                raise BrokerError(FAILED, "Samba does not see the share after it was added")
            created = _ensure_system_account(samba, user)
            _set_samba_password(samba, user, password)
            _reload(samba)
        except BaseException as exc:
            _undo_add(samba, snapshot, user if created else None)
            if isinstance(exc, BrokerError):
                raise BrokerError(exc.code, f"{exc.message}; the previous Samba configuration is restored") from exc
            raise

    return {"share": share, "user": user, "path": str(folder)}


def _undo_add(samba: Samba, snapshot: _Snapshot, created_user: str | None) -> None:
    _restore(samba, snapshot)
    if created_user is not None:
        try:
            _remove_account(samba, created_user)
        except BrokerError as exc:
            print(f"the account {created_user} made for the share could not be removed again: {exc.message}", file=sys.stderr, flush=True)
    if smbd_running(samba):
        _command(samba, ["smbcontrol", "smbd", "reload-config"], check=False)


def remove_share(samba: Samba, arguments: Mapping[str, Any]) -> dict[str, Any]:
    request = _arguments(arguments, REMOVE_FIELDS)
    share = _check_name(request["share"], SHARE_PATTERN, "share name", "abr-xxxxxxxx$")
    user = _check_name(request["user"], USER_PATTERN, "account name", "abr-smb-xxxxxxxx")

    with _locked(samba):
        sections = _our_sections(samba)
        section = sections.pop(share, None)
        if section is not None and section.get("valid users") != user:
            raise BrokerError(REFUSED, f"{share} belongs to the account {section.get('valid users')}, not to {user}; it is left as it is")
        others = [name for name, values in sections.items() if values.get("valid users") == user]
        if others:
            raise BrokerError(REFUSED, f"the account {user} also belongs to the share {others[0]}; it is left as it is")
        comment = _account_comment(samba, user)
        if comment is not None and comment not in OUR_ACCOUNT_COMMENTS:
            raise BrokerError(REFUSED, f"the system account {user} is not one AltaStor Backup made; it is left as it is")

        reloaded = False
        if section is not None:
            snapshot = _snapshot(samba)
            try:
                if sections:
                    _write_shares(samba, sections)
                else:
                    samba.shares_conf.unlink(missing_ok=True)
                    _write_smb_conf(samba, snapshot, _without_include(samba, snapshot.smb_conf))
                if smbd_running(samba):
                    _reload(samba)
                    reloaded = True
            except BaseException as exc:
                _restore(samba, snapshot)
                if isinstance(exc, BrokerError):
                    raise BrokerError(exc.code, f"{exc.message}; the share is kept") from exc
                raise
        removed_account = _remove_account(samba, user)

    return {"share": share, "user": user, "removed_share": section is not None, "removed_account": removed_account, "reloaded": reloaded}


def close_shares(samba: Samba) -> dict[str, Any]:
    with _locked(samba):
        has_file = samba.shares_conf.is_file()
        has_include = samba.smb_conf.is_file() and samba.include_line in samba.smb_conf.read_text().splitlines()
        if not has_file and not has_include:
            return {"closed": False, "shares": [], "reload_error": None}

        shares = sorted(_our_sections(samba)) if has_file else []
        if has_include:
            snapshot = _snapshot(samba)
            _write_smb_conf(samba, snapshot, _without_include(samba, snapshot.smb_conf))
        samba.shares_conf.unlink(missing_ok=True)

        reload_error = None
        if samba.which("smbcontrol") is not None and smbd_running(samba):
            try:
                _reload(samba)
            except BrokerError as exc:
                reload_error = exc.message
    return {"closed": True, "shares": shares, "reload_error": reload_error}


def close_from_command_line(samba: Samba) -> int:
    if os.geteuid() != 0:
        print("shares_broker: --close-shares must run as root", file=sys.stderr)
        return CLOSE_FAILED
    try:
        result = close_shares(samba)
    except (BrokerError, OSError) as exc:
        print(f"shares_broker: the shares could not be closed: {getattr(exc, 'message', exc)}", file=sys.stderr)
        return CLOSE_FAILED

    if not result["closed"]:
        print("No AltaStor Backup shares to close.")
        return CLOSED
    print(f"Closed the AltaStor Backup shares ({', '.join(result['shares']) or 'none listed'}); their accounts are kept.")
    if result["reload_error"]:
        print(f"shares_broker: smbd did not reload its configuration ({result['reload_error']}); restart smbd to stop serving them", file=sys.stderr)
        return CLOSED_WITHOUT_RELOAD
    return CLOSED


def operations(samba: Samba) -> dict[str, Operation]:
    return {
        "add-share": lambda arguments: add_share(samba, arguments),
        "remove-share": lambda arguments: remove_share(samba, arguments),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = broker_core.argument_parser(DESCRIPTION)
    parser.add_argument(
        "--close-shares",
        action="store_true",
        help="as root from the command line, without the socket: close every share of AltaStor Backup and keep their accounts",
    )
    options = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    os.umask(0o077)
    samba = Samba(options.service_user)
    if options.close_shares:
        return close_from_command_line(samba)
    return broker_core.serve(operations(samba), service_user=options.service_user)


if __name__ == "__main__":
    sys.exit(main())
