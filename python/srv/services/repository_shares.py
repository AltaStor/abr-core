# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import logging
import os
import pwd
import secrets
import shutil
import socket
import string
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from python.srv.core import broker_client
from python.srv.core.credential_encryption import (
    CredentialKeyUnavailable,
    credential_key_problem,
    decrypt_secret,
    is_encrypted,
)
from python.srv.core.mongo_db_wrapper import (
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    TASKS,
    catalog_records,
    persist_repository,
)
from python.srv.core.server_defaults import env_int
from python.srv.core.utils import utc_now
from python.srv.plugins.hyperv.vhdx_tool import (
    VhdxToolError,
    appliance_address_toward,
    configured_share_server,
)
from python.srv.services import abr_v41
from python.srv.services.api_errors import ApiError, api_error

logger = logging.getLogger(__name__)

SOCKET_ENV = "ABR_SHARES_BROKER_SOCKET"
DEFAULT_SOCKET = "/run/altastor-backup-shares.sock"
TIMEOUT_ENV = "ABR_SHARES_BROKER_TIMEOUT_SECONDS"
SHARES_CONF_ENV = "ABR_SAMBA_SHARES_CONF"
DEFAULT_SHARES_CONF = "/etc/altastor-backup/samba-shares.conf"
SMB_CONF = Path("/etc/samba/smb.conf")
SHARE_MARK = "support_hyperv"
SHARE_FIELDS = ("server", "share", "username", "password")
NAME_ALPHABET = string.ascii_lowercase + string.digits
PASSWORD_ALPHABET = string.ascii_letters + string.digits
PASSWORD_LENGTH = 40
ACTIVE_STATUSES = frozenset({"queued", "pending", "running", "stopping"})
CHECK_TIMEOUT_SECONDS = 60
SOURCE_DEPTH = 2
BROKER_UNIT = "altastor-backup-shares.socket"
SERVER_CACHE_SECONDS = 60
UNROUTED_PROBE = "192.0.2.1"

LOCK = threading.RLock()
_SERVER_CACHE: dict[str, Any] = {}


def broker_socket() -> str:
    return os.environ.get(SOCKET_ENV, "").strip() or DEFAULT_SOCKET


def _metadata(record: dict[str, Any]) -> dict[str, Any]:
    return dict(record.get("metadata") if isinstance(record.get("metadata"), dict) else {})


def is_shared(record: dict[str, Any] | None) -> bool:
    return isinstance(record, dict) and bool(_metadata(record).get(SHARE_MARK))


def _hyperv_hosts() -> list[str]:
    hosts: list[str] = []
    for record in sorted(catalog_records(HYPERVISORS), key=lambda item: str(item.get("name") or item.get("id") or "")):
        if not abr_v41._inventory_is_hyperv(record.get("id")):
            continue
        host = str(_metadata(record).get("host") or "").strip()
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def _default_route_address() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((UNROUTED_PROBE, 445))
            return str(probe.getsockname()[0])
    except OSError:
        return None


def _find_share_server() -> str:
    for host in _hyperv_hosts():
        try:
            return appliance_address_toward(host)
        except VhdxToolError:
            continue
    return _default_route_address() or socket.getfqdn()


def share_server() -> str:
    try:
        configured = configured_share_server()
    except VhdxToolError as exc:
        raise api_error(409, "invalid_share_server_name", str(exc)) from exc
    if configured:
        return configured
    now = time.monotonic()
    cached = _SERVER_CACHE.get("server")
    if cached and now - float(_SERVER_CACHE.get("at") or 0) < SERVER_CACHE_SECONDS:
        return str(cached)
    server = _find_share_server()
    _SERVER_CACHE.update({"server": server, "at": now})
    return server


def network_path(record: dict[str, Any]) -> str | None:
    metadata = _metadata(record)
    if not is_shared(record) or not metadata.get("share"):
        return None
    return f"\\\\{share_server()}\\{metadata.get('share')}"


def stored_repository(repository_id: str) -> dict[str, Any]:
    record = REPOSITORIES.get(str(repository_id))
    if not isinstance(record, dict):
        raise api_error(404, "repository_not_found", "Repository not found", record_id=repository_id)
    return record


def _name(record: dict[str, Any]) -> str:
    return str(record.get("name") or record.get("id") or "the repository")


def _call_broker(operation: str, arguments: dict[str, str], *, what: str) -> dict[str, Any]:
    try:
        return broker_client.call(broker_socket(), operation, arguments, timeout=env_int(TIMEOUT_ENV))
    except broker_client.BrokerUnavailable as exc:
        raise api_error(
            503,
            "repository_share_unavailable",
            f"{what}: the shares broker could not be reached: {exc}. It is the {BROKER_UNIT} unit of the "
            "altastor-backup-server package",
        ) from exc
    except broker_client.BrokerRefused as exc:
        if exc.code == "refused":
            raise api_error(409, "repository_share_refused", f"{what}: {exc.message}") from exc
        if exc.code == "not_permitted":
            raise api_error(503, "repository_share_unavailable", f"{what}: the shares broker refused this server: {exc.message}") from exc
        raise api_error(500, "repository_share_failed", f"{what}: {exc.message}") from exc


def _shares_conf() -> Path:
    return Path(os.environ.get(SHARES_CONF_ENV, "").strip() or DEFAULT_SHARES_CONF)


def _read_configured_shares() -> dict[str, dict[str, str]] | None:
    try:
        text = _shares_conf().read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning("The Samba shares in %s cannot be read: %s", _shares_conf(), exc)
        return None
    return _parse_sections(text)


def _configured_shares() -> dict[str, dict[str, str]]:
    return _read_configured_shares() or {}


def include_problem() -> str | None:
    try:
        lines = [line.strip() for line in SMB_CONF.read_text().splitlines()]
    except OSError:
        return None
    if f"include = {_shares_conf()}" in lines:
        return None
    return f"{SMB_CONF} no longer includes {_shares_conf()}"


def samba_serving_problem(record: dict[str, Any]) -> str | None:
    if not is_shared(record):
        return None
    share = str(_metadata(record).get("share") or "")
    problem = include_problem()
    if problem is None:
        configured = _read_configured_shares()
        if configured is None or not share or share in configured:
            return None
        problem = f"{_shares_conf()} has no share {share}"
    return f"Samba no longer serves {network_path(record)}: {problem}. The server puts it back when it starts"


def _parse_sections(text: str) -> dict[str, dict[str, str]]:
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


def _remove_left_behind_share(folder: str, *, what: str) -> None:
    recorded = {str(_metadata(record).get("share") or "") for record in catalog_records(REPOSITORIES) if is_shared(record)}
    for share, values in _configured_shares().items():
        if values.get("path") != folder or share in recorded or not values.get("valid users"):
            continue
        logger.warning(
            "Samba still shares %s as %s for account %s, which no repository records (a share made while the server stopped); "
            "it is removed first.",
            folder,
            share,
            values["valid users"],
        )
        _call_broker("remove-share", {"share": share, "user": values["valid users"]}, what=what)


def _token(alphabet: str, length: int) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _folder(record: dict[str, Any]) -> str:
    return os.path.realpath(str(record.get("path") or ""))


def _job_repositories(job_id: str, depth: int = 0) -> set[str]:
    job = JOBS.get(job_id) or {}
    found = {str(job.get("repo_id") or ""), *(str(value) for value in job.get("source_repository_ids") or [])}
    sources = [*(job.get("source_job_ids") or []), (job.get("replication") or {}).get("source_job_id")]
    for source in sources:
        if source and depth < SOURCE_DEPTH:
            found |= _job_repositories(str(source), depth + 1)
    return found - {""}


def _values(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_values(item) for item in value.values())) if value else set()
    if isinstance(value, (list, tuple, set)):
        return set().union(*(_values(item) for item in value)) if value else set()
    return {str(value)}


def sessions_using(record: dict[str, Any]) -> list[dict[str, Any]]:
    repository_id = str(record.get("id") or "")
    return [
        task
        for task in catalog_records(TASKS)
        if str(task.get("status") or "") in ACTIVE_STATUSES
        and (
            repository_id in _values(task.get("parameters") or {})
            or repository_id in _job_repositories(str(task.get("job_id") or ""))
        )
    ]


def refuse_while_sessions_use(record: dict[str, Any], *, action: str) -> None:
    busy = sessions_using(record)
    if busy:
        raise api_error(
            409,
            "repository_share_busy",
            f"{_name(record)} cannot be {action} while {len(busy)} session(s) use it; try again when they finish",
            sessions=[str(task.get("id") or "") for task in busy],
        )


def refuse_while_hyperv_jobs_use(record: dict[str, Any]) -> None:
    repository_id = str(record.get("id") or "")
    jobs = sorted(
        str(job.get("name") or job.get("id"))
        for job in catalog_records(JOBS)
        if str(job.get("repo_id") or "") == repository_id and abr_v41._inventory_is_hyperv(job.get("inventory_id"))
    )
    if jobs:
        raise api_error(
            409,
            "repository_share_used_by_hyperv_jobs",
            f"Hyper-V job(s) {', '.join(jobs)} back up into {_name(record)}, and their hosts reach it only through its share. "
            "Move them to another S3 or SMB repository (abr job edit --repository), or delete them, then remove the share",
            jobs=jobs,
        )


def _refuse_unshareable(record: dict[str, Any]) -> None:
    if str(record.get("type") or "").lower() != "local":
        raise api_error(
            409,
            "repository_not_local",
            f"{_name(record)} is a {record.get('type')} repository; only a local repository is shared from this server",
        )


def share_repository(record: dict[str, Any]) -> dict[str, Any]:
    with LOCK:
        if is_shared(record):
            return record
        _refuse_unshareable(record)
        refuse_while_sessions_use(record, action="shared")

        share = f"abr-{_token(NAME_ALPHABET, 8)}$"
        user = f"abr-smb-{_token(NAME_ALPHABET, 8)}"
        password = _token(PASSWORD_ALPHABET, PASSWORD_LENGTH)
        key_problem = credential_key_problem()
        if key_problem is not None:
            raise api_error(503, "credential_key_unavailable", f"{key_problem}. The share of {_name(record)} is not created.")

        server = share_server()
        folder = _folder(record)
        what = f"The share of {_name(record)} could not be created"
        _remove_left_behind_share(folder, what=what)
        _call_broker("add-share", {"share": share, "user": user, "password": password, "path": folder}, what=what)

        updated = dict(record)
        metadata = _metadata(record)
        metadata.update({"server": server, "share": share, "username": user, "password": password, SHARE_MARK: True})
        updated.update({"type": "smb", "metadata": metadata, "updated_at": utc_now()})
        try:
            persist_repository(updated)
        except Exception:
            _call_broker("remove-share", {"share": share, "user": user}, what=f"The share of {_name(record)} could not be removed again")
            raise

        logger.info("%s is shared as %s for account %s from %s.", _name(record), network_path(updated), user, folder)
        return updated


def share_new_repository(created: dict[str, Any]) -> dict[str, Any]:
    """Share a local repository right after it is added, as every local repository is unless asked not to be.
    When that fails the repository stays, unshared, and the reason comes back as share_error (and a warning)."""
    repository_id = str(created.get("id") or "")
    stored = REPOSITORIES.get(repository_id)
    if not isinstance(stored, dict) or str(stored.get("type") or "").lower() != "local":
        return created
    try:
        share_repository(stored)
    except ApiError as exc:
        logger.warning("%s is added but not shared: %s", _name(stored), exc.message)
        warning = f"{exc.message}. {_name(stored)} is added as a local repository; share it later (abr repo share add)"
        return {**created, "share_error": exc.message, "warnings": [*(created.get("warnings") or []), warning]}
    return {**abr_v41.resolve_repository(record_id=repository_id), "warnings": created.get("warnings") or []}


def unshare_repository(record: dict[str, Any]) -> dict[str, Any]:
    with LOCK:
        if not is_shared(record):
            return record
        refuse_while_hyperv_jobs_use(record)
        refuse_while_sessions_use(record, action="unshared")
        return _remove_share(record)


def _remove_share(record: dict[str, Any]) -> dict[str, Any]:
    metadata = _metadata(record)
    share, user = str(metadata.get("share") or ""), str(metadata.get("username") or "")
    if share and user:
        result = _call_broker("remove-share", {"share": share, "user": user}, what=f"The share of {_name(record)} could not be removed")
        if isinstance(result, dict) and result.get("account_error"):
            logger.warning("The share %s of %s is removed, but %s", share, _name(record), result["account_error"])

    updated = _clear_share(record)
    logger.info("The share %s of %s and its account %s are removed.", share, _name(record), user)
    return updated


def remove_repository_share(record: dict[str, Any]) -> None:
    if is_shared(record):
        with LOCK:
            _remove_share(record)


def _clear_share(record: dict[str, Any]) -> dict[str, Any]:
    updated = dict(record)
    metadata = _metadata(record)
    for field in SHARE_FIELDS:
        metadata[field] = None
    metadata[SHARE_MARK] = False
    updated.update({"type": "local", "metadata": metadata, "updated_at": utc_now()})
    persist_repository(updated)
    return updated


def _forget_unreadable_share(record: dict[str, Any], share: str, user: str) -> None:
    try:
        _call_broker("remove-share", {"share": share, "user": user}, what=f"The account of the share of {_name(record)} could not be removed")
    except ApiError as exc:
        logger.warning("%s; the account %s is left in Samba.", exc.message, user)
    _clear_share(record)
    logger.warning(
        "The share %s of %s is not in Samba and its stored password cannot be decrypted with the credential key, so it is "
        "not restored: %s is a local repository again. Share it again with abr repo share add.",
        share,
        _name(record),
        _name(record),
    )


def _restore_share(record: dict[str, Any]) -> str:
    metadata = _metadata(record)
    share, user = str(metadata.get("share") or ""), str(metadata.get("username") or "")
    password = decrypt_secret(metadata.get("password"))
    if not isinstance(password, str) or not password or is_encrypted(password):
        _forget_unreadable_share(record, share, user)
        return "reset"

    try:
        _call_broker(
            "add-share",
            {"share": share, "user": user, "password": password, "path": _folder(record)},
            what=f"The share {share} of {_name(record)} could not be restored",
        )
    except ApiError as exc:
        logger.warning("%s; it is tried again when the server starts next time.", exc.message)
        return "kept"
    logger.info("Restored the share %s of %s for account %s: Samba no longer had it.", network_path(record), _name(record), user)
    return "restored"


def restore_shares() -> dict[str, list[str]]:
    outcome: dict[str, list[str]] = {"restored": [], "reset": [], "kept": []}
    recorded = [
        record
        for record in catalog_records(REPOSITORIES)
        if is_shared(record) and _metadata(record).get("share") and _metadata(record).get("username")
    ]
    if not recorded:
        return outcome

    configured = _read_configured_shares()
    if configured is None:
        logger.warning("The shares of %d repositories are not checked against Samba, since its shares file cannot be read.", len(recorded))
        return outcome

    with LOCK:
        for record in recorded:
            if str(_metadata(record).get("share")) in configured:
                continue
            outcome[_restore_share(record)].append(_name(record))
        _restore_include()
    return outcome


def _restore_include() -> None:
    problem = include_problem()
    if problem is None:
        return
    try:
        result = _call_broker("include-shares", {}, what=f"The shares of the repositories could not be included again ({problem})")
    except ApiError as exc:
        logger.warning("%s; Samba does not serve them until it is. It is tried again when the server starts next time.", exc.message)
        return
    if result.get("included"):
        logger.warning("%s, so Samba served none of the shares of the repositories; the line is put back: %s.", problem, ", ".join(result.get("shares") or []))


def restore_shares_in_background() -> threading.Thread:
    thread = threading.Thread(target=_restore_quietly, name="restore-shares", daemon=True)
    thread.start()
    return thread


def _restore_quietly() -> None:
    try:
        restore_shares()
    except Exception:
        logger.exception("Could not check the shares of the repositories against Samba")


def refuse_managed_share_edit(record: dict[str, Any], payload: dict[str, Any]) -> None:
    if not is_shared(record):
        return
    touched = sorted(field for field in (*SHARE_FIELDS, "address", "path", "type") if payload.get(field) is not None)
    if touched:
        raise api_error(
            409,
            "repository_share_managed",
            f"The share of {_name(record)} is made and kept by this server, so {', '.join(touched)} cannot be edited. "
            "Remove the share and share the repository again to get a new share and account",
            fields=touched,
        )


def share_view(record: dict[str, Any]) -> dict[str, Any]:
    metadata = _metadata(record)
    shared = is_shared(record)
    return {
        "id": str(record.get("id") or ""),
        "name": record.get("name"),
        "path": record.get("path"),
        "default": bool(metadata.get("default_repository")),
        "shared": shared,
        "network_path": network_path(record),
        "share": metadata.get("share") if shared else None,
        "username": metadata.get("username") if shared else None,
    }


def list_shares() -> dict[str, Any]:
    records = [
        record
        for record in catalog_records(REPOSITORIES)
        if str(record.get("type") or "").lower() == "local" or is_shared(record)
    ]
    return {"items": [share_view(record) for record in sorted(records, key=lambda item: str(item.get("path") or ""))]}


class _SmbclientFailed(Exception):
    pass


def _smbclient(target: str, auth: Path, command: str) -> None:
    try:
        done = subprocess.run(
            ["smbclient", target, "-A", str(auth), "-c", command],
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _check_failed(f"smbclient could not be run: {exc}") from exc
    if done.returncode != 0 or "NT_STATUS_" in done.stdout:
        lines = [line for line in (done.stderr + done.stdout).splitlines() if line.strip()]
        raise _SmbclientFailed(f"smbclient {target} failed: {lines[-1].strip() if lines else done.returncode}")


def _check_failed(message: str) -> ApiError:
    return api_error(409, "repository_share_check_failed", message)


def service_account() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return f"uid {os.geteuid()}"


def _owner_of(folder: Path) -> str | None:
    try:
        uid = folder.stat().st_uid
    except OSError:
        return None
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid {uid}"


def _smbclient_failure(record: dict[str, Any], folder: Path, failure: str) -> ApiError:
    metadata = _metadata(record)
    account = service_account()
    owner = _owner_of(folder)
    if owner is not None and owner != account:
        return _check_failed(
            f"{folder} belongs to {owner}, not to {account}: Samba writes into the share as {account}, so nothing can be "
            f"written through {network_path(record)} and Hyper-V backups into it fail. Give the folder back with "
            f"chown -R {account} {folder} ({failure})"
        )
    if "NT_STATUS_BAD_NETWORK_NAME" in failure:
        return _check_failed(f"Samba on this server does not serve the share {metadata.get('share')}, so {network_path(record)} cannot be opened ({failure})")
    if "NT_STATUS_CONNECTION_REFUSED" in failure or "NT_STATUS_IO_TIMEOUT" in failure or "NT_STATUS_HOST_UNREACHABLE" in failure:
        return _check_failed(f"Samba (smbd) on this server does not answer at {share_server()}, so {network_path(record)} cannot be reached ({failure})")
    if "tree connect failed" in failure or "NT_STATUS_LOGON_FAILURE" in failure:
        return _check_failed(
            f"Samba refuses the share's account {metadata.get('username')} for {network_path(record)}: the account is no longer "
            f"in Samba's passdb (pdbedit -L lists them) or its password was changed outside AltaStor ({failure})"
        )
    return _check_failed(failure)


def check_share(record: dict[str, Any]) -> dict[str, Any]:
    if not is_shared(record):
        raise api_error(409, "repository_not_shared", f"{_name(record)} is not shared; share it first")
    if shutil.which("smbclient") is None:
        raise api_error(409, "smbclient_missing", "smbclient is not installed on this server, so the share cannot be checked")

    problem = samba_serving_problem(record)
    if problem:
        raise _check_failed(problem)

    metadata = _metadata(record)
    try:
        password = decrypt_secret(metadata.get("password"))
    except CredentialKeyUnavailable as exc:
        raise api_error(503, "credential_key_unavailable", f"{exc}. The share of {_name(record)} cannot be checked.") from exc

    folder = Path(_folder(record))
    target = f"//{share_server()}/{metadata.get('share')}"
    name = f"abr-share-check-{int(time.time())}-{secrets.token_hex(4)}.txt"
    with tempfile.TemporaryDirectory(prefix="abr-share-check-") as work:
        auth, sent, received = Path(work, "auth"), Path(work, "sent"), Path(work, "received")
        auth.write_text(f"username={metadata.get('username')}\npassword={password}\n")
        auth.chmod(0o600)
        sent.write_text(f"AltaStor Backup share check {utc_now()}\n")

        try:
            _smbclient(target, auth, f'put "{sent}" "{name}"')
            if not (folder / name).exists():
                raise _check_failed(f"the test file written through {network_path(record)} did not appear in {folder}")

            _smbclient(target, auth, f'get "{name}" "{received}"; del "{name}"')
        except _SmbclientFailed as exc:
            raise _smbclient_failure(record, folder, str(exc)) from exc
        if not received.is_file() or received.read_text() != sent.read_text():
            raise _check_failed(f"the test file read back through {network_path(record)} differs from the one written")
        if (folder / name).exists():
            raise _check_failed(f"the test file is still in {folder} after it was deleted through the share")

    return {**share_view(record), "checked": True, "message": f"{network_path(record)} as {metadata.get('username')}: write, read back and delete passed"}
