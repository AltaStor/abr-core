#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

import os
import secrets
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROGRAM = "abr_smb_share.py"


def app_root():
    configured = os.environ.get("ABR_APP_ROOT")
    if configured:
        return configured
    parents = Path(__file__).resolve().parents
    if len(parents) > 4 and (parents[4] / "python" / "srv" / "services" / "abr_v41.py").is_file():
        return str(parents[4])
    return "/opt/altastor-backup"


APP_ROOT = app_root()
SERVER_CONF = Path(os.environ.get("ABR_SERVER_CONF", "/etc/altastor-backup/server.conf"))
UNIT = os.environ.get("ABR_UNIT", "altastor-backup-server")
SMB_CONF = Path("/etc/samba/smb.conf")
SHARES_CONF = Path("/etc/samba/altastor-backup-shares.conf")
INCLUDE_LINE = f"include = {SHARES_CONF}"
ACCOUNT_COMMENT = "AltaStor Backup Hyper-V share"
SAMBA_TOOLS = ("smbd", "testparm", "smbpasswd", "pdbedit", "smbcontrol")
ACTIVE_STATES = {"queued", "pending", "running", "stopping"}
USAGE = f"""usage: sudo python3 {PROGRAM} show | add <path> | del <path> | check <path>
  show          list the local repositories of AltaStor Backup as <path> : <share name>|none
  add <path>    share the repository at <path> over SMB for Hyper-V hosts: a hidden share and its own Samba account,
                stored encrypted in AltaStor Backup
  del <path>    remove that share and account; the repository is a local one again
  check <path>  write, read back and delete a test file through the share with the stored account"""


class Refused(Exception):
    pass


def run(argv, *, stdin=None, check=True):
    done = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise Refused(f"{' '.join(argv[:2])} failed: {detail[-1] if detail else f'exit status {done.returncode}'}")
    return done


def load_abr():
    sys.dont_write_bytecode = True
    sys.path.insert(0, APP_ROOT)
    if not Path(APP_ROOT, "python", "srv", "services", "abr_v41.py").is_file():
        raise Refused(f"AltaStor Backup is not installed in {APP_ROOT} (set ABR_APP_ROOT)")
    from python.srv.core.server_defaults import parse_defaults_env

    if SERVER_CONF.is_file():
        os.environ.update(parse_defaults_env(SERVER_CONF))
    os.environ.setdefault("MONGODB_URI", "mongodb://127.0.0.1:27018")

    from python.srv.core import credential_encryption, mongo_db_wrapper
    from python.srv.services import abr_v41

    if not credential_encryption.credential_key_path().is_file():
        raise Refused(f"there is no credential key at {credential_encryption.credential_key_path()}: start the AltaStor Backup server once first")
    if mongo_db_wrapper.get_database() is None:
        raise Refused("the AltaStor Backup catalog database cannot be reached (MONGODB_URI)")
    for store, load in (
        (mongo_db_wrapper.HYPERVISORS, mongo_db_wrapper.load_registered_hypervisors),
        (mongo_db_wrapper.JOBS, mongo_db_wrapper.load_jobs),
        (mongo_db_wrapper.REPOSITORIES, mongo_db_wrapper.load_repositories),
        (mongo_db_wrapper.TASKS, mongo_db_wrapper.load_tasks),
    ):
        store.clear()
        store.update(load())
    return mongo_db_wrapper, credential_encryption, abr_v41


def metadata(record):
    return record.get("metadata") if isinstance(record.get("metadata"), dict) else {}


def local_repositories(catalog):
    found = []
    for record in catalog.catalog_records(catalog.REPOSITORIES):
        if record.get("type") == "local" or metadata(record).get("support_hyperv"):
            found.append(record)
    return sorted(found, key=lambda record: str(record.get("path") or ""))


def repository_at(catalog, raw_path):
    wanted = os.path.realpath(os.path.abspath(raw_path))
    for record in local_repositories(catalog):
        if os.path.realpath(str(record.get("path") or "")) == wanted:
            return record
    raise Refused(f"{wanted} is not a local repository of AltaStor Backup; sudo python3 {PROGRAM} show lists them")


def refuse_while_sessions_run(catalog):
    active = [task for task in catalog.catalog_records(catalog.TASKS) if str(task.get("status") or "") in ACTIVE_STATES]
    if active:
        raise Refused(f"{len(active)} session(s) are queued or running and the server restarts at the end; run this again once they have finished")


def samba_sections(text):
    sections, name = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1]
            sections[name] = {}
        elif name is not None and "=" in line and not line.startswith(("#", ";")):
            key, _, value = line.partition("=")
            sections[name][key.strip().lower()] = value.strip()
    return sections


def all_shares():
    return samba_sections(run(["testparm", "-s"], check=False).stdout)


def our_shares():
    return samba_sections(SHARES_CONF.read_text()) if SHARES_CONF.is_file() else {}


def preflight():
    missing = [tool for tool in SAMBA_TOOLS if shutil.which(tool) is None]
    if missing or not SMB_CONF.is_file():
        raise Refused(f"Samba is not installed (missing: {' '.join(missing) or SMB_CONF}); run sudo bash install-samba.sh first")
    if run(["systemctl", "is-active", "--quiet", "smbd"], check=False).returncode != 0:
        raise Refused("smbd is installed but not running; start it with: sudo systemctl enable --now smbd")
    if run(["testparm", "-s", str(SMB_CONF)], check=False).returncode != 0:
        raise Refused(f"the existing Samba configuration does not validate (testparm -s {SMB_CONF}); it is left as it is")
    role = run(["testparm", "-s", "--parameter-name=server role"], check=False).stdout.lower()
    backend = run(["testparm", "-s", "--parameter-name=config backend"], check=False).stdout.lower()
    passdb = run(["testparm", "-s", "--parameter-name=passdb backend"], check=False).stdout.strip().lower()
    if "active directory domain controller" in role:
        raise Refused("this Samba is an Active Directory domain controller; its configuration is left as it is")
    if "registry" in backend:
        raise Refused("this Samba keeps its configuration in the registry; it is left as it is")
    if not passdb.startswith("tdbsam"):
        raise Refused(f"this Samba keeps its accounts in '{passdb}', not tdbsam; no account is added to it")


def write_file(path, text, *, like=None):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)
    source = like if like is not None and like.exists() else None
    if source is not None:
        shutil.copystat(source, temporary)
        os.chown(temporary, source.stat().st_uid, source.stat().st_gid)
    else:
        os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def shares_text(sections):
    blocks = []
    for name, values in sections.items():
        blocks.append(f"[{name}]\n" + "".join(f"    {key} = {value}\n" for key, value in values.items()))
    return "\n".join(blocks)


def reload_samba():
    if run(["smbcontrol", "smbd", "reload-config"], check=False).returncode != 0:
        run(["systemctl", "reload", "smbd"])
    if run(["systemctl", "is-active", "--quiet", "smbd"], check=False).returncode != 0:
        raise Refused("smbd stopped after reloading its configuration")


def account_exists(user):
    return run(["getent", "passwd", user], check=False).returncode == 0


def remove_account(user):
    if run(["pdbedit", "-L", "-u", user], check=False).returncode == 0:
        run(["smbpasswd", "-x", user])
    entry = run(["getent", "passwd", user], check=False).stdout.split(":")
    if len(entry) > 4 and entry[4] == ACCOUNT_COMMENT:
        run(["userdel", user])


def token(alphabet, length):
    return "".join(secrets.choice(alphabet) for _ in range(length))


def new_names():
    taken = {name.lower() for name in all_shares()}
    while True:
        share = f"abr-{token(string.ascii_lowercase + string.digits, 8)}$"
        user = f"abr-smb-{token(string.ascii_lowercase + string.digits, 8)}"
        if share.lower() not in taken and not account_exists(user):
            return share, user, token(string.ascii_letters + string.digits, 40)


def open_share(folder, share, user, password):
    owner, group = folder.owner(), folder.group()
    if folder.stat().st_uid == 0:
        raise Refused(f"{folder} belongs to root; only a folder of the AltaStor Backup service user is shared")
    smb_before = SMB_CONF.read_text()
    shares_before = SHARES_CONF.read_text() if SHARES_CONF.is_file() else None
    created = False
    try:
        sections = our_shares()
        sections[share] = {
            "path": str(folder),
            "browseable": "no",
            "read only": "no",
            "guest ok": "no",
            "valid users": user,
            "force user": owner,
            "force group": group,
            "create mask": "0640",
            "directory mask": "0750",
            "server smb encrypt": "required",
        }
        write_file(SHARES_CONF, shares_text(sections))
        if INCLUDE_LINE not in smb_before.splitlines():
            write_file(SMB_CONF, smb_before + ("" if smb_before.endswith("\n") else "\n") + INCLUDE_LINE + "\n", like=SMB_CONF)
        if run(["testparm", "-s", str(SMB_CONF)], check=False).returncode != 0 or share not in all_shares():
            raise Refused("the Samba configuration does not validate with the share added; the previous configuration is restored")
        run(["useradd", "--system", "--no-create-home", "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin",
             "--no-user-group", "--gid", "nogroup", "--comment", ACCOUNT_COMMENT, user])
        created = True
        run(["smbpasswd", "-s", "-a", user], stdin=f"{password}\n{password}\n")
        run(["smbpasswd", "-e", user])
        reload_samba()
    except BaseException:
        write_file(SMB_CONF, smb_before, like=SMB_CONF)
        if shares_before is None:
            SHARES_CONF.unlink(missing_ok=True)
        else:
            write_file(SHARES_CONF, shares_before)
        if created:
            remove_account(user)
        run(["smbcontrol", "smbd", "reload-config"], check=False)
        raise


def close_share(share, user):
    sections = our_shares()
    sections.pop(share, None)
    if sections:
        write_file(SHARES_CONF, shares_text(sections))
    else:
        SHARES_CONF.unlink(missing_ok=True)
        lines = SMB_CONF.read_text().splitlines()
        if INCLUDE_LINE in lines:
            write_file(SMB_CONF, "".join(f"{line}\n" for line in lines if line != INCLUDE_LINE), like=SMB_CONF)
    if user:
        remove_account(user)
    reload_samba()


def restart_server():
    if run(["systemctl", "is-active", "--quiet", UNIT], check=False).returncode == 0:
        run(["systemctl", "restart", UNIT])
        return f"Restarted {UNIT} so it uses the change."
    if run(["systemctl", "cat", UNIT], check=False).returncode == 0:
        return f"{UNIT} is not running; it uses the change when it starts."
    return f"There is no {UNIT} service here: restart the AltaStor Backup server yourself so it uses the change."


def show(catalog):
    for record in local_repositories(catalog):
        share = metadata(record).get("share") if metadata(record).get("support_hyperv") else None
        print(f"{record.get('path')} : {share or 'none'}")


def add(catalog, crypto, raw_path):
    record = repository_at(catalog, raw_path)
    folder = Path(os.path.realpath(str(record.get("path"))))
    if metadata(record).get("support_hyperv"):
        print(f"{folder} is already shared as {metadata(record).get('share')}; nothing to do")
        return
    refuse_while_sessions_run(catalog)
    preflight()
    if not folder.is_dir():
        raise Refused(f"{folder} is not a folder")
    for name, values in our_shares().items():
        if os.path.realpath(values.get("path", "")) == str(folder):
            raise Refused(f"Samba already shares {folder} as {name} for AltaStor Backup; it is left as it is")
    others = [name for name, values in all_shares().items() if os.path.realpath(values.get("path", "") or "/nonexistent") == str(folder)]
    if others:
        print(f"Note: Samba already shares this folder as {', '.join(others)}; that share is left as it is")
    share, user, password = new_names()
    open_share(folder, share, user, password)
    updated = dict(record)
    stored = dict(metadata(record))
    stored.update({"server": socket.getfqdn(), "share": share, "username": user, "password": crypto.encrypt_secret(password), "support_hyperv": True})
    updated["type"] = "smb"
    updated["metadata"] = stored
    try:
        catalog.persist_repository(updated)
    except BaseException:
        close_share(share, user)
        raise
    print(f"Shared {folder} as \\\\{socket.getfqdn()}\\{share} for Samba account {user}; Hyper-V jobs can back up into it.")
    print(restart_server())


def delete(catalog, abr_v41, raw_path):
    record = repository_at(catalog, raw_path)
    folder = os.path.realpath(str(record.get("path")))
    stored = metadata(record)
    if not stored.get("support_hyperv"):
        print(f"{folder} is not shared; nothing to do")
        return
    share, user = str(stored.get("share") or ""), str(stored.get("username") or "")
    if share not in our_shares():
        raise Refused(f"the share {share} of {folder} was not made by this script (it comes from the Hyper-V switch of the Default Repo); turn it off with: abr repo default --no-support-hyperv")
    jobs = sorted(
        str(job.get("name") or job.get("id"))
        for job in catalog.catalog_records(catalog.JOBS)
        if str(job.get("repo_id") or "") == str(record.get("id") or "") and abr_v41._inventory_is_hyperv(job.get("inventory_id"))
    )
    if jobs:
        raise Refused(f"Hyper-V job(s) {', '.join(jobs)} back up into {folder} through its share; move them to another repository or delete them first")
    refuse_while_sessions_run(catalog)
    updated = dict(record)
    cleared = dict(stored)
    cleared.update({"server": None, "share": None, "username": None, "password": None, "support_hyperv": False})
    updated["type"] = "local"
    updated["metadata"] = cleared
    catalog.persist_repository(updated)
    try:
        close_share(share, user)
    except Refused as exc:
        raise Refused(f"AltaStor Backup no longer uses {share}, but Samba kept it: {exc}") from exc
    print(f"{folder} is no longer shared; the share {share} and the Samba account {user} are removed.")
    print(restart_server())


def check(catalog, crypto, raw_path):
    record = repository_at(catalog, raw_path)
    folder = Path(os.path.realpath(str(record.get("path"))))
    stored = metadata(record)
    if not stored.get("support_hyperv"):
        raise Refused(f"{folder} is not shared; run add first")
    if shutil.which("smbclient") is None:
        raise Refused("smbclient is missing: run sudo bash install-samba.sh first")
    share, user = stored.get("share"), stored.get("username")
    name = f"abr-share-check-{int(time.time())}.txt"
    with tempfile.TemporaryDirectory() as work:
        auth, sent, received = Path(work, "auth"), Path(work, "sent"), Path(work, "received")
        auth.write_text(f"username={user}\npassword={crypto.decrypt_secret(stored.get('password'))}\n")
        os.chmod(auth, 0o600)
        sent.write_text(f"AltaStor Backup share check {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
        target = f"//{stored.get('server') or socket.getfqdn()}/{share}"
        run(["smbclient", target, "-A", str(auth), "-c", f"put {sent} {name}"])
        written = folder / name
        owner = written.owner() if written.exists() else "missing"
        run(["smbclient", target, "-A", str(auth), "-c", f"get {name} {received}; del {name}"])
        if received.read_text() != sent.read_text():
            raise Refused(f"the test file read back through {share} differs from the one written")
        if written.exists():
            raise Refused(f"the test file is still in {folder} after it was deleted through the share")
    print(f"OK: wrote, read back and deleted a test file through {target} as {user}; in {folder} it belonged to {owner}.")


def main(argv):
    if not argv or argv[0] not in {"show", "add", "del", "check"} or (argv[0] == "show") != (len(argv) == 1) or len(argv) > 2:
        print(USAGE, file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print(f"Run it as root: sudo python3 {PROGRAM} {' '.join(argv)}", file=sys.stderr)
        return 1
    try:
        catalog, crypto, abr_v41 = load_abr()
        if argv[0] == "show":
            show(catalog)
        elif argv[0] == "add":
            add(catalog, crypto, argv[1])
        elif argv[0] == "del":
            delete(catalog, abr_v41, argv[1])
        else:
            check(catalog, crypto, argv[1])
    except Refused as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
