# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""The one-time setup of a Proxmox VE cluster for AltaStor, from the root password.

The admin gives the address of one node and its root password. abr-core logs in with the password once and:

1. checks that the node runs PVE 9 and that its cluster is not registered already;
2. installs the inventory's SSH key in root's authorized_keys, which PVE shares across the cluster through /etc/pve,
   limited to abr-core's address and to commands and file transfers;
3. pins what the cluster tells about itself over that connection: every node's SSH host keys and the certificates of
   the REST API, the cluster's root CA and any certificate a node has in place of the one the CA issued;
4. creates the PVE user abr@pve with its own role, granted on /, and an API token for this inventory;
5. installs the abr-pve-backup package on every node with SSH and the new key, then adds the "abr" storage, in this
   order, since a node without the package complains about an unknown storage type on every command;
6. checks the token through the API.

The password is not kept. Setting up again, with inventory edit and the root password, keeps the key and the pinned
host keys, replaces the token, and brings the role, the package and the storage up to date.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ...core.storage_tool import APP_ROOT, REPO_ROOT
from ...core.utils import utc_now
from .pve_api import DEFAULT_PORT, PveApi, PveApiError
from .ssh import DEFAULT_PORT as SSH_PORT
from .ssh import (
    NodeShell,
    SshError,
    normalized_key,
    generate_key_pair,
    key_fingerprint,
    public_key_line,
)

logger = logging.getLogger(__name__)

USER = "abr@pve"
ROLE = "AltaStorBackup"
STORAGE = "abr"
PACKAGE = "abr-pve-backup"
SUPPORTED_MAJOR_VERSION = 9
# What backups, restores, the inventory and the checks around them need, found the smallest set on PVE 9: restores
# create VMs and edit their configuration and network, and reading other users' tasks needs Sys.Audit.
PRIVILEGES = (
    "Datastore.AllocateSpace",
    "Datastore.Audit",
    "SDN.Use",
    "Sys.Audit",
    "VM.Allocate",
    "VM.Audit",
    "VM.Backup",
    "VM.Config.Network",
    "VM.Config.Options",
    "VM.PowerMgmt",
)
KEY_MARK = "altastor-inventory"
AUTHORIZED_KEYS = "/root/.ssh/authorized_keys"
REMOTE_PACKAGE_FOLDER = "/var/tmp"
INSTALL_TIMEOUT_SECONDS = 900
PACKAGE_NAME = re.compile(r"^abr-pve-backup_([0-9][A-Za-z0-9.+~-]*)_([a-z0-9]+)\.deb$")


class PveSetupError(RuntimeError):
    pass


Progress = Callable[[str], None]


def _say(progress: Progress | None, message: str) -> None:
    logger.info("%s", message)
    if progress is not None:
        progress(message)


# ---- names and lines -----------------------------------------------------------------------------------------------


def token_name(inventory_id: str) -> str:
    """The API token of one inventory; another inventory or server keeps its own token on the same user."""
    return "altastor-" + re.sub(r"[^A-Za-z0-9]", "", str(inventory_id))[:12]


def key_comment(inventory_id: str) -> str:
    return f"{KEY_MARK}:{inventory_id}"


def authorized_key_line(private_key: str, source: str, inventory_id: str) -> str:
    """The key, usable only from abr-core's address and without forwarding or a terminal; commands and SFTP work."""
    options = f'from="{source}",restrict ' if source else "restrict "
    return options + public_key_line(private_key, key_comment(inventory_id))


def with_authorized_key(text: str, line: str, inventory_id: str) -> str:
    """authorized_keys with this inventory's line in place of any earlier one, every other line as it was."""
    kept = without_authorized_key(text, inventory_id)
    return (kept + ("\n" if kept and not kept.endswith("\n") else "") + line + "\n") if line else kept


def without_authorized_key(text: str, inventory_id: str) -> str:
    mark = key_comment(inventory_id)
    lines = [line for line in str(text or "").splitlines() if not line.rstrip().endswith(" " + mark)]
    return "\n".join(lines) + ("\n" if lines else "")


def known_host_keys(text: str) -> list[str]:
    """The keys of a known_hosts or .pub text, as "type base64"."""
    keys = []
    for line in str(text or "").splitlines():
        parts = line.split()
        for index, part in enumerate(parts[:-1]):
            if part.startswith(("ssh-", "ecdsa-")):
                key = f"{part} {parts[index + 1]}"
                if key not in keys:
                    keys.append(key)
                break
    return keys


def pve_major_version(text: str) -> int | None:
    match = re.search(r"pve-manager/(\d+)\.", str(text or ""))
    return int(match.group(1)) if match else None


# ---- the package ---------------------------------------------------------------------------------------------------


def package_version(path: Path) -> str:
    match = PACKAGE_NAME.match(path.name)
    if not match:
        raise PveSetupError(
            f"{path} is not named as the abr-pve-backup package is: abr-pve-backup_<version>_<arch>.deb"
        )
    return match.group(1)


def package_path() -> Path | None:
    """This appliance's abr-pve-backup package, which abr-core installs on the nodes; None when it has none."""
    configured = os.environ.get("ALTASTOR_PVE_PACKAGE_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path.resolve() if path.is_file() else None
    found: list[Path] = []
    for root in (APP_ROOT, REPO_ROOT):
        folder = root / "tools" / "abr-pve-backup"
        if folder.is_dir():
            found += [path for path in folder.iterdir() if PACKAGE_NAME.match(path.name) and path.is_file()]
    if not found:
        return None
    # The newest by modification time: version strings do not sort as text.
    return max(found, key=lambda path: path.stat().st_mtime).resolve()


def installed_version(shell: NodeShell) -> str | None:
    result = shell.run(["dpkg-query", "-W", "-f", "${Status} ${Version}", PACKAGE], check=False)
    words = result.text.split()
    return words[-1] if result.status == 0 and words[:3] == ["install", "ok", "installed"] and len(words) == 4 else None


def ensure_package(shell: NodeShell, node: str, package: Path | None, progress: Progress | None = None) -> str:
    """Installs the package on a node that lacks it or has an older one, with apt so that its dependencies come from
    the node's Debian repositories; returns the version the node has."""
    installed = installed_version(shell)
    if package is None:
        if installed:
            return installed
        raise PveSetupError(
            f"Node {node} does not have the {PACKAGE} package, and this appliance has none to install. Install "
            f"{PACKAGE} on every node, or put it on this appliance in tools/abr-pve-backup/ or at "
            "ALTASTOR_PVE_PACKAGE_PATH"
        )
    version = package_version(package)
    if installed and shell.run(["dpkg", "--compare-versions", installed, "ge", version], check=False).status == 0:
        return installed
    remote = f"{REMOTE_PACKAGE_FOLDER}/{package.name}"
    _say(
        progress, f"Installing {PACKAGE} {version} on node {node}" + (f" in place of {installed}" if installed else "")
    )
    shell.upload_file(str(package), remote, mode=0o644)
    try:
        result = shell.run(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -q -o DPkg::Lock::Timeout=300 "
            f"--no-install-recommends {remote}",
            timeout=INSTALL_TIMEOUT_SECONDS,
            check=False,
        )
    finally:
        shell.remove_file(remote)
    if result.status != 0:
        lines = [line for line in result.error_text.splitlines() if line.strip() and not line.startswith("W:")]
        raise PveSetupError(
            f"apt could not install {PACKAGE} {version} on node {node}: {lines[-1] if lines else 'no error output'}. "
            "Its dependencies (nbdkit, nbd-client, cifs-utils) come from the node's Debian repositories, which the "
            "node must reach"
        )
    now = installed_version(shell)
    if now != version:
        raise PveSetupError(f"Node {node} has {PACKAGE} {now or 'not installed'} after installing {version}")
    return now


# ---- setup ---------------------------------------------------------------------------------------------------------


def _json(shell: NodeShell, command: Sequence[str]) -> Any:
    text = shell.run(list(command)).text
    try:
        return json.loads(text or "null")
    except ValueError as exc:
        raise PveSetupError(f"{' '.join(command)} on {shell.address} did not answer JSON: {text[:200]!r}") from exc


def _cluster(shell: NodeShell, address: str) -> tuple[str | None, list[dict[str, Any]]]:
    status = _json(shell, ["pvesh", "get", "/cluster/status", "--output-format", "json"]) or []
    cluster = next((str(item.get("name")) for item in status if item.get("type") == "cluster"), None)
    nodes = [
        {
            "name": str(item.get("name")),
            "address": str(item.get("ip") or (address if item.get("local") else "")),
            "online": bool(item.get("online", 1)),
            "local": bool(item.get("local")),
        }
        for item in status
        if item.get("type") == "node"
    ]
    if not nodes:
        raise PveSetupError(f"{address} answered without any PVE node in its cluster status")
    return cluster, sorted(nodes, key=lambda node: (not node["local"], node["name"]))


def _install_key(shell: NodeShell, line: str, inventory_id: str) -> str:
    path = shell.run(["readlink", "-f", AUTHORIZED_KEYS]).text.strip() or AUTHORIZED_KEYS
    current = (shell.read_file(path) or b"").decode()
    updated = with_authorized_key(current, line, inventory_id)
    if updated != current:
        shell.write_file(path, updated, mode=None if path.startswith("/etc/pve/") else 0o600)
    return path


def _ensure_access(shell: NodeShell, inventory_id: str, inventory_name: str) -> tuple[str, str]:
    """The role, the user and its ACL as AltaStor needs them, and a new token for this inventory."""
    privileges = ",".join(PRIVILEGES)
    roles = {
        str(item.get("roleid")) for item in _json(shell, ["pveum", "role", "list", "--output-format", "json"]) or []
    }
    shell.run(["pveum", "role", "modify" if ROLE in roles else "add", ROLE, "--privs", privileges])
    users = {
        str(item.get("userid")) for item in _json(shell, ["pveum", "user", "list", "--output-format", "json"]) or []
    }
    if USER not in users:
        shell.run(["pveum", "user", "add", USER, "--comment", "AltaStor Backup & Replication"])
    shell.run(["pveum", "acl", "modify", "/", "--users", USER, "--roles", ROLE, "--propagate", "1"])
    name = token_name(inventory_id)
    tokens = _json(shell, ["pveum", "user", "token", "list", USER, "--output-format", "json"]) or []
    if any(str(item.get("tokenid")) == name for item in tokens):
        shell.run(["pveum", "user", "token", "remove", USER, name])
    created = _json(
        shell,
        [
            "pveum",
            "user",
            "token",
            "add",
            USER,
            name,
            "--privsep",
            "0",
            "--comment",
            f"AltaStor inventory {inventory_name}",
            "--output-format",
            "json",
        ],
    )
    secret = str((created or {}).get("value") or "")
    if not secret:
        raise PveSetupError(f"PVE created token {USER}!{name} without telling its secret")
    return f"{USER}!{name}", secret


def _ensure_storage(shell: NodeShell) -> list[str]:
    """The "abr" storage, for backups only and on every node; returns what changed."""
    storages = {
        str(item.get("storage")): item
        for item in _json(shell, ["pvesh", "get", "/storage", "--output-format", "json"]) or []
    }
    existing = storages.get(STORAGE)
    if existing is None:
        shell.run(["pvesm", "add", "abr", STORAGE, "--content", "backup", "--prune-backups", "keep-all=1"])
        return [f"added the {STORAGE} storage"]
    if str(existing.get("type")) != "abr":
        raise PveSetupError(
            f"This cluster has a storage named {STORAGE} of type {existing.get('type')}. AltaStor needs that name for "
            "its own storage; rename or remove the other one"
        )
    command = ["pvesm", "set", STORAGE]
    changes = []
    if str(existing.get("content") or "") != "backup":
        command += ["--content", "backup"]
        changes.append(f"the {STORAGE} storage holds backups only")
    removed = [option for option in ("nodes", "disable") if existing.get(option) not in (None, "", 0, "0")]
    if removed:
        command += ["--delete", ",".join(removed)]
        changes += [
            (
                f"the {STORAGE} storage is available on every node"
                if option == "nodes"
                else f"the {STORAGE} storage is enabled"
            )
            for option in removed
        ]
    if changes:
        shell.run(command)
    return changes


def _pinned_keys(shell: NodeShell, presented: str, extra: Sequence[str] = ()) -> list[str]:
    """Every host key the node has; the key it presented must be one of them."""
    keys = known_host_keys(shell.run("cat /etc/ssh/ssh_host_*_key.pub", check=False).text)
    for key in extra:
        if key not in keys:
            keys.append(key)
    if normalized_key(presented) not in keys:
        raise PveSetupError(
            f"{shell.address} presented SSH host key {key_fingerprint(presented)}, "
            "which is not one of its own host keys"
        )
    return keys


def set_up_cluster(
    *,
    inventory_id: str,
    inventory_name: str,
    address: str,
    root_password: str,
    api_port: int = DEFAULT_PORT,
    ssh_port: int = SSH_PORT,
    previous: Mapping[str, Any] | None = None,
    check_new: Callable[[str | None, list[str]], None] | None = None,
    package: Path | None = None,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Sets the cluster up and returns what the inventory keeps: the "pve" metadata, the token's secret, the cluster's
    name and nodes, and warnings about nodes that could not be set up."""
    previous = previous or {}
    first_pins = [
        key
        for node in previous.get("nodes") or []
        if isinstance(node, dict) and address in (node.get("address"), node.get("name"))
        for key in node.get("host_keys") or []
    ]
    with NodeShell(address, port=ssh_port, password=root_password, host_keys=first_pins or None) as first:
        major = pve_major_version(first.run(["pveversion"]).text)
        if major != SUPPORTED_MAJOR_VERSION:
            raise PveSetupError(
                f"{address} runs Proxmox VE {major if major else 'of an unknown version'}; "
                f"AltaStor supports Proxmox VE {SUPPORTED_MAJOR_VERSION}"
            )
        cluster, nodes = _cluster(first, address)
        if check_new is not None:
            check_new(cluster, [node["name"] for node in nodes])
        subject = f"cluster {cluster}" if cluster else f"node {nodes[0]['name']}"
        _say(progress, f"Setting up {subject} for AltaStor through {address}")

        try:
            return _set_up(
                first,
                inventory_id=inventory_id,
                inventory_name=inventory_name,
                address=address,
                api_port=api_port,
                ssh_port=ssh_port,
                cluster=cluster,
                nodes=nodes,
                previous=previous,
                package=package,
                progress=progress,
            )
        except BaseException:
            # A first setup that fails takes back the key and the token it made, or each attempt would leave its own.
            if not previous.get("token_id"):
                try:
                    tear_down(first, inventory_id)
                except (SshError, PveSetupError) as exc:
                    logger.warning("Could not take back the partial setup of %s: %s", address, exc)
            raise


def _set_up(
    first: NodeShell,
    *,
    inventory_id: str,
    inventory_name: str,
    address: str,
    api_port: int,
    ssh_port: int,
    cluster: str | None,
    nodes: list[dict[str, Any]],
    previous: Mapping[str, Any],
    package: Path | None,
    progress: Progress | None,
) -> dict[str, Any]:
    previous_nodes = {str(node.get("name")): node for node in previous.get("nodes") or [] if isinstance(node, dict)}
    warnings: list[str] = []
    ssh_key = str(previous.get("ssh_key") or "") or generate_key_pair("")[0]
    source = (first.run('printf %s "$SSH_CLIENT"').text.split() or [""])[0]
    path = _install_key(first, authorized_key_line(ssh_key, source, inventory_id), inventory_id)
    _say(progress, f"Installed AltaStor's SSH key for root in {path}, usable from {source or 'any address'}")

    certificates = [(first.read_file("/etc/pve/pve-root-ca.pem") or b"").decode()]
    for node in nodes:
        node["host_keys"] = list(previous_nodes.get(node["name"], {}).get("host_keys") or [])
        own = (first.read_file(f"/etc/pve/nodes/{node['name']}/pveproxy-ssl.pem") or b"").decode()
        if own.strip():
            certificates.append(own)
        for key in known_host_keys((first.read_file(f"/etc/pve/nodes/{node['name']}/ssh_known_hosts") or b"").decode()):
            if key not in node["host_keys"]:
                node["host_keys"].append(key)
    if not certificates[0].strip():
        raise PveSetupError(f"{address} has no cluster root CA in /etc/pve/pve-root-ca.pem")
    nodes[0]["host_keys"] = _pinned_keys(first, first.host_key, nodes[0]["host_keys"])

    token_id, token_secret = _ensure_access(first, inventory_id, inventory_name)
    _say(progress, f"Created API token {token_id} with role {ROLE} on /")

    versions: dict[str, str] = {}
    for node in nodes:
        if not node["online"]:
            warnings.append(f"Node {node['name']} is offline; AltaStor installs {PACKAGE} there when it is back")
            continue
        try:
            with NodeShell(node["address"], port=ssh_port, private_key=ssh_key, host_keys=node["host_keys"]) as shell:
                if not node["local"]:
                    node["host_keys"] = _pinned_keys(shell, shell.host_key, node["host_keys"])
                versions[node["name"]] = ensure_package(shell, node["name"], package, progress)
        except SshError as exc:
            raise PveSetupError(f"Node {node['name']} at {node['address']}: {exc}") from exc
    for change in _ensure_storage(first):
        _say(progress, f"Changed: {change}")

    addresses = [address] + [node["address"] for node in nodes if node["address"] and node["address"] != address]
    try:
        with PveApi(
            addresses, port=api_port, token_id=token_id, token_secret=token_secret, trusted_certificates=certificates
        ) as api:
            api.get(f"/nodes/{nodes[0]['name']}/storage/{STORAGE}/status")
    except PveApiError as exc:
        raise PveSetupError(f"The new API token does not work: {exc}") from exc
    return {
        "pve": {
            "token_id": token_id,
            "ssh_key": ssh_key,
            "ssh_port": ssh_port,
            "key_source": source,
            "nodes": [
                {"name": node["name"], "address": node["address"], "host_keys": node["host_keys"]} for node in nodes
            ],
            "certificates": certificates,
            "storage": STORAGE,
            "package_versions": versions,
            "set_up_at": utc_now(),
        },
        "token": token_secret,
        "cluster_name": cluster,
        "nodes": [node["name"] for node in nodes],
        "warnings": warnings,
    }


def tear_down(shell: NodeShell, inventory_id: str) -> list[str]:
    """Removes what one inventory alone used: its API token and its SSH key. The user, the role, the package and the
    storage stay for other inventories and servers; returns what was removed."""
    removed = []
    name = token_name(inventory_id)
    tokens = _json(shell, ["pveum", "user", "token", "list", USER, "--output-format", "json"]) or []
    if any(str(item.get("tokenid")) == name for item in tokens):
        shell.run(["pveum", "user", "token", "remove", USER, name])
        removed.append(f"API token {USER}!{name}")
    path = shell.run(["readlink", "-f", AUTHORIZED_KEYS]).text.strip() or AUTHORIZED_KEYS
    current = (shell.read_file(path) or b"").decode()
    updated = without_authorized_key(current, inventory_id)
    # The key is removed last: the connection that removes it uses it.
    if updated != current:
        shell.write_file(path, updated, mode=None if path.startswith("/etc/pve/") else 0o600)
        removed.append(f"SSH key in {path}")
    return removed
