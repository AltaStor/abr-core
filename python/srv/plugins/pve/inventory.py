# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""Registering a Proxmox VE cluster as an inventory, checking it, and letting it go.

An inventory is added with the address of one node and the root password, which sets the cluster up (setup.py); an
edit with the root password sets it up again. Every other check uses what the setup left: the API token and the SSH
key. The password is not kept.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ...core.mongo_db_wrapper import HYPERVISORS, catalog_records
from .pve import PLATFORM, PveError, PveNode, access_from_record
from .pve_api import PveApiError, PveAuthError, PveCertificateError, PvePermissionError, PveUnreachableError
from .setup import PveSetupError, cluster_view, package_path, set_up_cluster, tear_down, token_name
from .ssh import NodeShell, SshAuthError, SshError, SshHostKeyError, SshUnreachableError

logger = logging.getLogger(__name__)

ROOT_USERS = ("root", "root@pam")


class PveAlreadyRegisteredError(RuntimeError):
    def __init__(self, *, scope: str, subject: str, existing_id: str, existing_name: str) -> None:
        what = f"Proxmox VE cluster {subject!r}" if scope == "cluster" else f"Proxmox VE node {subject!r}"
        super().__init__(
            f"{what} is already registered as inventory {existing_name!r}. Edit that inventory instead of adding a "
            "second one: two registrations of one cluster would protect the same VMs twice"
        )
        self.scope = scope
        self.subject = subject
        self.existing_id = existing_id
        self.existing_name = existing_name


class PveRegistrationError(RuntimeError):
    """Why a cluster cannot be registered or reached; reason is auth, host_key, certificate, permission, unreachable
    or setup."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _metadata(record: Mapping[str, Any]) -> dict[str, Any]:
    return dict(record.get("metadata")) if isinstance(record.get("metadata"), dict) else {}


def root_password(record: Mapping[str, Any]) -> str:
    credentials = _metadata(record).get("credentials")
    return str((credentials or {}).get("password") or "") if isinstance(credentials, dict) else ""


def check_root_user(username: str) -> None:
    if str(username or "").strip() not in ROOT_USERS:
        raise ValueError(
            "A Proxmox VE inventory is added with root's password (user root@pam), used once to set the cluster up "
            "for AltaStor; AltaStor then uses its own SSH key and API token"
        )


def assert_cluster_is_new(record: Mapping[str, Any], cluster: str | None, nodes: list[str]) -> None:
    """Refuses a cluster, or a single node, that another inventory has registered already."""
    record_id = str(record.get("id") or "")
    wanted_nodes = {name.lower() for name in nodes}
    for existing in catalog_records(HYPERVISORS):
        if str(existing.get("id") or "") == record_id or str(existing.get("kind") or existing.get("type")) != PLATFORM:
            continue
        metadata = _metadata(existing)
        existing_cluster = str(metadata.get("cluster_name") or "")
        pve = metadata.get("pve") if isinstance(metadata.get("pve"), dict) else {}
        existing_nodes = {
            str(node.get("name") or "").lower() for node in pve.get("nodes") or [] if isinstance(node, dict)
        }
        same_cluster = bool(cluster) and existing_cluster.lower() == str(cluster).lower()
        shared_nodes = sorted(wanted_nodes & existing_nodes)
        if same_cluster or (not cluster and not existing_cluster and shared_nodes):
            raise PveAlreadyRegisteredError(
                scope="cluster" if cluster else "node",
                subject=str(cluster or shared_nodes[0]),
                existing_id=str(existing.get("id") or ""),
                existing_name=str(existing.get("name") or ""),
            )


def _registration_error(exc: BaseException, address: str) -> PveRegistrationError:
    if isinstance(exc, SshAuthError):
        return PveRegistrationError(f"{exc}. Give the root password of the node at {address}", reason="auth")
    if isinstance(exc, SshHostKeyError):
        return PveRegistrationError(str(exc), reason="host_key")
    if isinstance(exc, (SshUnreachableError, PveUnreachableError)):
        return PveRegistrationError(str(exc), reason="unreachable")
    if isinstance(exc, PveCertificateError):
        return PveRegistrationError(str(exc), reason="certificate")
    if isinstance(exc, PveAuthError):
        return PveRegistrationError(str(exc), reason="auth")
    if isinstance(exc, PvePermissionError):
        return PveRegistrationError(
            f"{exc}. AltaStor's PVE user lacks a privilege; run inventory edit with the root password to set its role "
            "up again",
            reason="permission",
        )
    return PveRegistrationError(str(exc), reason="setup")


def register(record: Mapping[str, Any]) -> dict[str, Any]:
    """Sets the cluster up when the record carries the root password, and checks the stored access otherwise.

    Returns the proof of connection the inventory service applies: the cluster's identity, and after a setup what the
    record keeps from it.
    """
    metadata = _metadata(record)
    address = str(metadata.get("host") or "")
    password = root_password(record)
    if not password:
        return {"identity": test_connection(record)}
    previous = metadata.get("pve") if isinstance(metadata.get("pve"), dict) else None
    try:
        setup = set_up_cluster(
            inventory_id=str(record.get("id") or ""),
            inventory_name=str(record.get("name") or ""),
            address=address,
            root_password=password,
            api_port=int(metadata.get("port") or 8006),
            previous=previous,
            check_new=lambda cluster, nodes: assert_cluster_is_new(record, cluster, nodes),
            package=package_path(),
        )
    except PveAlreadyRegisteredError:
        raise
    except (SshError, PveApiError, PveSetupError, PveError) as exc:
        raise _registration_error(exc, address) from exc
    for warning in setup["warnings"]:
        logger.warning("Proxmox VE inventory %s: %s", record.get("name"), warning)
    nodes = list(setup["nodes"])
    return {
        "identity": {"cluster": setup["cluster_name"], "computer": nodes[0] if nodes else "", "nodes": nodes},
        "setup": setup,
    }


def apply_setup(record: Mapping[str, Any], proof: Mapping[str, Any]) -> dict[str, Any]:
    """The record with what a setup left in it, and without the root password."""
    setup = proof.get("setup") if isinstance(proof.get("setup"), dict) else None
    if setup is None:
        return dict(record)
    updated = dict(record)
    metadata = _metadata(record)
    credentials = dict(metadata.get("credentials")) if isinstance(metadata.get("credentials"), dict) else {}
    credentials["password"] = None
    metadata["credentials"] = credentials
    metadata["token"] = setup["token"]
    metadata["pve"] = setup["pve"]
    metadata["verify_ssl"] = True
    updated["metadata"] = metadata
    return updated


def test_connection(record: Mapping[str, Any]) -> dict[str, Any]:
    """Checks the API with the token and SSH with the key on every node that is up; returns the cluster's identity."""
    metadata = _metadata(record)
    address = str(metadata.get("host") or "")
    try:
        with PveNode.from_record(record) as node:
            identity = node.identity()
            online = {
                str(item.get("name"))
                for item in node.cluster_status()
                if item.get("type") == "node" and item.get("online")
            }
            for name in sorted(online):
                node.shell(name).run(["true"])
    except (SshError, PveApiError, PveError) as exc:
        raise _registration_error(exc, address) from exc
    return identity


def refresh_access(record: dict[str, Any]) -> list[str]:
    """Brings the inventory's nodes and API certificates up to date, read over SSH from the first node AltaStor has
    pinned that answers: a node that joined gets its address and host keys, a node that left goes, and renewed
    certificates replace the old ones. No password is needed. Changes the record's metadata in place, for the caller
    to save; returns what changed."""
    access = access_from_record(record)
    failures = []
    for node in access.nodes:
        address = str(node.get("address") or node.get("name"))
        try:
            with NodeShell(
                address,
                port=access.ssh_port,
                private_key=access.ssh_key,
                host_keys=[str(key) for key in node.get("host_keys") or []],
            ) as shell:
                nodes, certificates = cluster_view(shell, access.nodes)
            break
        except (SshError, PveSetupError, ValueError) as exc:
            failures.append(f"{node.get('name')}: {exc}")
    else:
        raise PveError("No node of the cluster could tell its nodes and certificates: " + "; ".join(failures))

    known = {str(node.get("name")): node for node in access.nodes}
    current = {node["name"]: node for node in nodes}
    changes = [f"node {name} at {node['address']} joined" for name, node in current.items() if name not in known]
    changes += [f"node {name} left" for name in known if name not in current]
    changes += [
        f"node {name} has a new address, {node['address']}"
        for name, node in current.items()
        if name in known and known[name].get("address") and known[name].get("address") != node["address"]
    ]
    changes += [
        f"node {name} has new SSH host keys"
        for name, node in current.items()
        if name in known and node["host_keys"] != list(known[name].get("host_keys") or [])
    ]
    if certificates != access.certificates:
        changes.append("the API certificates changed")
    if changes:
        metadata = dict(_metadata(record))
        metadata["pve"] = {**(metadata.get("pve") or {}), "nodes": nodes, "certificates": certificates}
        record["metadata"] = metadata
        logger.info("Proxmox VE inventory %s: %s", record.get("name"), "; ".join(changes))
    return changes


def release(record: Mapping[str, Any]) -> list[str]:
    """Removes the inventory's token and SSH key from the cluster, as far as a node answers; returns warnings."""
    try:
        access = access_from_record(record)
    except PveError:
        return []
    inventory_id = str(record.get("id") or "")
    failures = []
    for node in access.nodes:
        try:
            with NodeShell(
                str(node.get("address") or node.get("name")),
                port=access.ssh_port,
                private_key=access.ssh_key,
                host_keys=[str(key) for key in node.get("host_keys") or []],
            ) as shell:
                removed = tear_down(shell, inventory_id)
        except (SshError, PveSetupError) as exc:
            failures.append(f"{node.get('name')}: {exc}")
            continue
        logger.info("Proxmox VE inventory %s: removed %s", record.get("name"), ", ".join(removed) or "nothing")
        return []
    warning = (
        f"Could not remove AltaStor's API token and SSH key from the Proxmox VE cluster ({'; '.join(failures)}). "
        f"Remove the token abr@pve!{token_name(inventory_id)} and the root SSH key marked {inventory_id} by hand"
    )
    logger.warning("%s", warning)
    return [warning]
