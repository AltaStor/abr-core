# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""The one-time setup of a Proxmox VE cluster for AltaStor, from the root password.

The admin gives the address of one node and its root password. abr-core logs in with the password once and:

1. checks that the node runs PVE 9 and that its cluster is not registered already;
2. writes abr-node-helper to /etc/pve/priv, and installs the inventory's SSH key in root's authorized_keys, which PVE
   shares across the cluster through /etc/pve, limited to abr-core's address and to the helper as its forced command
   (see node_helper.py);
3. pins what the cluster tells about itself over that connection: every node's SSH host keys and the certificates of
   the REST API, the cluster's root CA and any certificate a node has in place of the one the CA issued;
4. creates the PVE user abr@pve with its own role, granted on /, and an API token for this inventory;
5. installs the abr-pve-backup package on every node with SSH, the new key and the helper, then adds the "abr"
   storage, in this order, since a node without the package complains about an unknown storage type on every command;
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
from .node_helper import (
    FORCED_COMMAND,
    HELPER_PATH,
    NodeHelperError,
    NodeHelperMissing,
    helper_from_package,
    helper_version,
    version_key,
)
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
# Where the package puts its copy of the helper, for a setup on an appliance without the package.
INSTALLED_HELPER = "/usr/lib/abr/abr-node-helper"
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
    """The key, usable only from abr-core's address, without forwarding or a terminal, and only for the node helper,
    which sshd runs whatever the client asks for."""
    options = (f'from="{source}",' if source else "") + f'restrict,command="{FORCED_COMMAND}" '
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


def ensure_helper(shell: NodeShell, package: Path | None) -> dict[str, Any]:
    """The helper's answer to version, after the helper in /etc/pve was replaced by the package's when that one is
    newer. One copy serves the whole cluster, so the first node to see a newer package brings it to every node."""
    helper = shell.helper
    state = helper.version()
    if package is not None:
        text = helper_from_package(package)
        if version_key(helper_version(text)) > version_key(str(state.get("helper") or "0")):
            helper.update(text)
            state = helper.version()
    return state


def ensure_package(shell: NodeShell, node: str, package: Path | None, progress: Progress | None = None) -> str:
    """Installs the package through the node helper on a node that lacks it or has an older one, with apt so that its
    dependencies come from the node's Debian repositories; returns the version the node has."""
    state = ensure_helper(shell, package)
    if package is None:
        if state.get("package"):
            return str(state["package"])
        raise PveSetupError(
            f"Node {node} does not have the {PACKAGE} package, and this appliance has none to install. Install "
            f"{PACKAGE} on every node, or put it on this appliance in tools/abr-pve-backup/ or at "
            "ALTASTOR_PVE_PACKAGE_PATH"
        )
    version = package_version(package)
    installed, current = shell.helper.package_status(version)
    if current and installed:
        return installed
    _say(
        progress, f"Installing {PACKAGE} {version} on node {node}" + (f" in place of {installed}" if installed else "")
    )
    try:
        now = shell.helper.install_package(package)
    except NodeHelperError as exc:
        raise PveSetupError(
            f"apt could not install {PACKAGE} {version} on node {node}: {exc}. "
            "Its dependencies (nbdkit, nbd-client, cifs-utils) come from the node's Debian repositories, which the "
            "node must reach"
        ) from exc
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


def cluster_view(shell: NodeShell, known: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """The cluster's nodes, with their addresses and SSH host keys, and the API certificates to trust, as the node at
    the other end tells them from /etc/pve.

    /etc/pve is the cluster's shared file system, so a node AltaStor has pinned vouches for what it says of the others.
    A node AltaStor knows keeps its pinned keys, with any the cluster lists for it added. The node helper reads it all.
    """
    info = shell.helper.cluster_info()
    try:
        members = json.loads(str(info.get("members") or "{}"))
    except ValueError as exc:
        raise PveSetupError(f"{shell.address} has an /etc/pve/.members that is not JSON") from exc
    nodelist = members.get("nodelist") if isinstance(members.get("nodelist"), dict) else {}
    listed_nodes = info.get("nodes") if isinstance(info.get("nodes"), dict) else {}
    pinned = {str(node.get("name")): node for node in known}
    nodes = []
    for name in sorted(nodelist) or sorted(pinned):
        entry = nodelist.get(name) if isinstance(nodelist.get(name), dict) else {}
        files = listed_nodes.get(name) if isinstance(listed_nodes.get(name), dict) else {}
        keys = [str(key) for key in pinned.get(name, {}).get("host_keys") or []]
        listed = known_host_keys(str(files.get("ssh_known_hosts") or ""))
        keys += [key for key in listed if key not in keys]
        address = str(entry.get("ip") or pinned.get(name, {}).get("address") or name)
        nodes.append({"name": name, "address": address, "host_keys": keys})
    certificates = [str(info.get("root_ca") or "")]
    if not certificates[0].strip():
        raise PveSetupError(f"{shell.address} has no cluster root CA in /etc/pve/pve-root-ca.pem")
    for node in nodes:
        files = listed_nodes.get(node["name"]) if isinstance(listed_nodes.get(node["name"]), dict) else {}
        own = str(files.get("pveproxy_ssl_pem") or "")
        if own.strip():
            certificates.append(own)
    return nodes, certificates


def _pinned_keys(address: str, host_keys: str, presented: str, extra: Sequence[str] = ()) -> list[str]:
    """Every host key the node has, from the text of its .pub files; the key it presented must be one of them."""
    keys = known_host_keys(host_keys)
    for key in extra:
        if key not in keys:
            keys.append(key)
    if normalized_key(presented) not in keys:
        raise PveSetupError(
            f"{address} presented SSH host key {key_fingerprint(presented)}, which is not one of its own host keys"
        )
    return keys


def _install_helper(shell: NodeShell, package: Path | None) -> str:
    """Writes the node helper to /etc/pve/priv through the root password's session, from this appliance's package or
    else from the one installed on the node; a newer helper there, which another appliance may have put, stays.
    Returns the version /etc/pve has."""
    if package is not None:
        text = helper_from_package(package)
    else:
        text = shell.read_file(INSTALLED_HELPER) or b""
        if not text:
            raise PveSetupError(
                f"{shell.address} has no {PACKAGE} package, and this appliance has none either: AltaStor needs it for "
                "its node helper. Put the package on this appliance in tools/abr-pve-backup/ or at "
                "ALTASTOR_PVE_PACKAGE_PATH"
            )
    current = shell.read_file(HELPER_PATH)
    if current and version_key(helper_version(current)) >= version_key(helper_version(text)):
        return helper_version(current)
    shell.write_file(HELPER_PATH, text, mode=None)
    return helper_version(text)


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
    helper = _install_helper(first, package)
    _say(progress, f"Wrote AltaStor's node helper {helper} to {HELPER_PATH}")
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
    own_keys = first.run("cat /etc/ssh/ssh_host_*_key.pub", check=False).text
    nodes[0]["host_keys"] = _pinned_keys(address, own_keys, first.host_key, nodes[0]["host_keys"])

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
                    own_keys = str(shell.helper.cluster_info().get("host_keys") or "")
                    node["host_keys"] = _pinned_keys(node["address"], own_keys, shell.host_key, node["host_keys"])
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


def restrict_key(shell: NodeShell, *, ssh_key: str, source: str, inventory_id: str, package: Path | None) -> bool:
    """Moves an inventory that was set up before the node helper onto it, through its own key, which still runs any
    command: writes the helper to /etc/pve/priv and gives the key the helper as its forced command. Returns whether it
    did; a key the helper runs as its forced command already is left as it is."""
    try:
        if shell.helper.version().get("restricted"):
            return False
    except NodeHelperMissing:
        pass
    _install_helper(shell, package)
    _install_key(shell, authorized_key_line(ssh_key, source, inventory_id), inventory_id)
    return True


def tear_down(shell: NodeShell, inventory_id: str) -> list[str]:
    """Removes what one inventory alone used, through the node helper: its API token and its SSH key. The user, the
    role, the package, the helper and the storage stay for other inventories and servers; returns what was removed."""
    removed = []
    if shell.helper.remove_token(inventory_id):
        removed.append(f"API token {USER}!{token_name(inventory_id)}")
    # The key is removed last: the connection that removes it uses it.
    if shell.helper.remove_key(inventory_id):
        removed.append("SSH key in /etc/pve/priv/authorized_keys")
    return removed
