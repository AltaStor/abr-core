# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import os
import socket
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlparse

from python.srv.core.credential_encryption import StoredCredentialUnreadable
from python.srv.core.mongo_db_wrapper import HYPERVISORS, JOBS, TASKS, catalog_records, persist_hypervisor, replace_resources_for_hypervisor
from python.srv.core.server_defaults import env_int
from python.srv.core.utils import utc_now
from python.srv.services.discovery import build_vcenter_resources, generate_inventory_discovery

LIVE_INVENTORY_TYPES = ("vsphere", "hyper-v", "proxmox-ve", "xcp-ng")
SUPPORTED_INVENTORY_TYPES = (*LIVE_INVENTORY_TYPES, "mock")


def _mock_inventory_creation_enabled() -> bool:
    return os.environ["ABR_ENABLE_MOCK_INVENTORY"] == "1"


def creatable_inventory_types() -> tuple[str, ...]:
    if _mock_inventory_creation_enabled():
        return SUPPORTED_INVENTORY_TYPES
    return LIVE_INVENTORY_TYPES


class EsxHostInClusterError(ValueError):
    """A standalone ESXi host that belongs to a cluster was registered/used.

    Such a host is managed by a vCenter; backing it up via a direct connection
    is unreliable (DRS/vMotion can move its VMs away mid-job), so the managing
    vCenter must be registered instead.
    """

    def __init__(self, cluster_name: str) -> None:
        self.cluster_name = str(cluster_name or "cluster")
        super().__init__(
            f"This ESXi host belongs to cluster '{self.cluster_name}'. Hosts that "
            "are part of a cluster cannot be registered or backed up directly; "
            "register the managing vCenter instead."
        )

INVENTORY_KIND_BY_TYPE = {
    "vsphere": "vcenter",
    "hyper-v": "hyper-v",
    "proxmox-ve": "proxmox-ve",
    "xcp-ng": "xcp-ng",
    "mock": "mock",
}
INVENTORY_TYPE_BY_KIND = {value: key for key, value in INVENTORY_KIND_BY_TYPE.items()}


def normalize_inventory_type(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in SUPPORTED_INVENTORY_TYPES:
        raise ValueError(
            f"Unsupported inventory type: {value}. Supported types: {', '.join(SUPPORTED_INVENTORY_TYPES)}"
        )
    return normalized


def inventory_type_from_record(record: dict[str, Any]) -> str:
    raw_type = str(record.get("type") or "").strip().lower()
    if raw_type in SUPPORTED_INVENTORY_TYPES:
        return raw_type
    return INVENTORY_TYPE_BY_KIND.get(str(record.get("kind") or "").strip().lower(), str(record.get("kind") or "unknown"))


def inventory_kind_for_type(inventory_type: str) -> str:
    return INVENTORY_KIND_BY_TYPE[normalize_inventory_type(inventory_type)]


def default_port_for_type(inventory_type: str) -> int:
    return {
        "vsphere": 443,
        "hyper-v": 5986,
        "proxmox-ve": 8006,
        "xcp-ng": 443,
        "mock": 0,
    }[inventory_type]


def extract_host(address: str) -> str:
    candidate = str(address or "").strip()
    if not candidate:
        return candidate
    if "://" not in candidate:
        return candidate.split("/", 1)[0].split(":", 1)[0]
    parsed = urlparse(candidate)
    return parsed.hostname or candidate


def _extract_port(address: str, inventory_type: str) -> int:
    candidate = str(address or "").strip()
    if candidate and "://" in candidate:
        parsed = urlparse(candidate)
        if parsed.port is not None:
            return parsed.port
    return default_port_for_type(inventory_type)


def _credentials_from_record(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    credentials = metadata.get("credentials")
    return dict(credentials) if isinstance(credentials, dict) else {}


def _summarize_resources(resources: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"vm_count": 0, "host_count": 0, "cluster_count": 0}
    for resource in resources:
        resource_type = str(resource.get("resource_type") or "")
        if resource_type == "virtual_machine":
            counts["vm_count"] += 1
        elif resource_type == "host":
            counts["host_count"] += 1
        elif resource_type == "cluster":
            counts["cluster_count"] += 1
    return counts


class InventoryProvider(ABC):
    @abstractmethod
    def test_connection(self, record: dict[str, Any]) -> str | None:
        pass

    @abstractmethod
    def rescan(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        pass


class VsphereInventoryProvider(InventoryProvider):
    def test_connection(self, record: dict[str, Any]) -> str | None:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        credentials = _credentials_from_record(record)
        host = metadata.get("host")
        port = int(metadata.get("port", 443))
        verify_ssl = bool(metadata.get("verify_ssl", False))
        username = credentials.get("username")
        password = credentials.get("password")
        if not host or not username or not password:
            raise ValueError("Saved vSphere inventory is missing host or credentials")

        try:
            from python.srv.plugins.vcenter.vcenter import VCenterNode
        except ModuleNotFoundError as exc:
            if exc.name in {"pyVim", "pyVmomi"}:
                raise RuntimeError("vSphere support requires pyVmomi/pyVim to be installed") from exc
            raise

        try:
            with VCenterNode(str(host), str(username), str(password), port=port, verify_ssl=verify_ssl) as node:
                # Reject a standalone ESXi host that has been added to a cluster:
                # it must be backed up through its managing vCenter, not directly.
                service_type = node.get_service_type()
                if service_type == "HostAgent":
                    cluster_name = node.clustered_host_name()
                    if cluster_name:
                        raise EsxHostInClusterError(cluster_name)
                return service_type
        except (EsxHostInClusterError, StoredCredentialUnreadable):
            raise
        except Exception as exc:
            raise RuntimeError("Unable to connect to virtualization environment") from exc

    def rescan(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        return build_vcenter_resources(record)


class _StubInventoryProvider(InventoryProvider):
    def test_connection(self, record: dict[str, Any]) -> None:
        if not str(record.get("address") or "").strip():
            raise ValueError("Inventory address is required")
        if not str(record.get("username") or "").strip():
            raise ValueError("Inventory username is required")
        if not str(_credentials_from_record(record).get("password") or "").strip():
            raise ValueError("Saved inventory credentials are missing")

    def rescan(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        return generate_inventory_discovery(
            inventory_type=inventory_type_from_record(record),
            hypervisor_id=str(record["id"]),
            hypervisor_name=str(record["name"]),
        )


class HyperVInventoryProvider(_StubInventoryProvider):
    pass


class ProxmoxInventoryProvider(_StubInventoryProvider):
    pass


class XcpNgInventoryProvider(_StubInventoryProvider):
    pass


class MockInventoryProvider(_StubInventoryProvider):
    pass


class InventoryProviderRegistry:
    _providers = {
        "vsphere": VsphereInventoryProvider(),
        "hyper-v": HyperVInventoryProvider(),
        "proxmox-ve": ProxmoxInventoryProvider(),
        "xcp-ng": XcpNgInventoryProvider(),
        "mock": MockInventoryProvider(),
    }

    @classmethod
    def resolve(cls, record: dict[str, Any]) -> InventoryProvider:
        inventory_type = inventory_type_from_record(record)
        provider = cls._providers.get(inventory_type)
        if provider is None:
            raise ValueError(f"Unsupported inventory type: {inventory_type}")
        return provider


def build_inventory_record(
    *,
    inventory_id: str,
    name: str,
    inventory_type: str,
    address: str,
    username: str,
    password: str,
    description: str | None = None,
    insecure: bool = False,
) -> dict[str, Any]:
    normalized_type = normalize_inventory_type(inventory_type)
    normalized_name = str(name or "").strip()
    normalized_address = str(address or "").strip()
    normalized_username = str(username or "").strip()
    normalized_password = str(password or "")
    if not normalized_name:
        raise ValueError("Inventory name must not be empty")
    if not normalized_address:
        raise ValueError("Inventory address must not be empty")
    if not normalized_username:
        raise ValueError("Inventory username must not be empty")
    if not normalized_password:
        raise ValueError("Inventory password must not be empty")

    created_at = utc_now()
    return {
        "id": inventory_id,
        "name": normalized_name,
        "type": normalized_type,
        "kind": inventory_kind_for_type(normalized_type),
        "address": normalized_address,
        "endpoint": normalized_address,
        "username": normalized_username,
        "status": "unknown",
        "description": str(description or ""),
        "created_at": created_at,
        "updated_at": created_at,
        "last_scan_at": None,
        "vm_count": 0,
        "host_count": 0,
        "cluster_count": 0,
        "metadata": {
            "host": extract_host(normalized_address),
            "port": _extract_port(normalized_address, normalized_type),
            "verify_ssl": not insecure,
            "credentials": {
                "username": normalized_username,
                "password": normalized_password,
            },
        },
    }


def apply_inventory_updates(record: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    updated = dict(record)
    metadata = dict(updated.get("metadata") if isinstance(updated.get("metadata"), dict) else {})
    credentials = dict(metadata.get("credentials") if isinstance(metadata.get("credentials"), dict) else {})
    changed = False

    if "name" in updates:
        value = str(updates["name"] or "").strip()
        if not value:
            raise ValueError("Inventory name must not be empty")
        updated["name"] = value
        changed = True
    if "address" in updates:
        value = str(updates["address"] or "").strip()
        if not value:
            raise ValueError("Inventory address must not be empty")
        updated["address"] = value
        updated["endpoint"] = value
        metadata["host"] = extract_host(value)
        metadata["port"] = _extract_port(value, inventory_type_from_record(record))
        changed = True
    if "port" in updates:
        try:
            port_value = int(updates["port"])
        except (TypeError, ValueError):
            raise ValueError("Inventory port must be a number")
        if not (1 <= port_value <= 65535):
            raise ValueError("Inventory port must be between 1 and 65535")
        # Applied after "address" so an explicit port always wins over one
        # implicitly re-derived from a simultaneously-edited address.
        metadata["port"] = port_value
        changed = True
    if "username" in updates:
        value = str(updates["username"] or "").strip()
        if not value:
            raise ValueError("Inventory username must not be empty")
        updated["username"] = value
        credentials["username"] = value
        changed = True
    if "password" in updates:
        value = str(updates["password"] or "")
        if not value:
            raise ValueError("Inventory password must not be empty")
        credentials["password"] = value
        changed = True
    if "description" in updates:
        updated["description"] = str(updates["description"] or "")
        changed = True

    if not changed:
        raise ValueError("inventory edit requires at least one editable option")

    if credentials:
        metadata["credentials"] = credentials
    updated["metadata"] = metadata
    updated["updated_at"] = utc_now()
    return updated


def inventory_connect_timeout() -> int:
    return env_int("ALTASTOR_INVENTORY_CONNECT_TIMEOUT_SECONDS")


def _endpoint_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    """Quick TCP reachability probe used to keep registration responsive.

    Avoids a slow, blocking ``SmartConnect`` against an unreachable endpoint
    (e.g. offline registration, or unit tests using placeholder addresses).
    """
    if not host:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


class EsxDirectConnectionUnsupported(ValueError):
    def __init__(self) -> None:
        super().__init__(
            "A direct ESXi connection is not supported. Register the vCenter Server that manages this host instead."
        )


class InventoryUnreachableError(RuntimeError):
    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _connection_failure(
    exc: BaseException, host: str, port: int, *, verify_ssl: bool = True
) -> InventoryUnreachableError:
    chain: list[BaseException] = []
    cursor: BaseException | None = exc
    while cursor is not None and cursor not in chain:
        chain.append(cursor)
        cursor = cursor.__cause__ or cursor.__context__
    rendered = " | ".join(str(item) for item in chain if str(item))
    lowered = rendered.lower()

    if "invalidlogin" in lowered or "incorrect user name or password" in lowered or "cannot complete login" in lowered:
        return InventoryUnreachableError(
            f"{host}:{port} rejected these credentials", reason="authentication"
        )
    if "certificate" in lowered or "ssl" in lowered or "tls" in lowered:
        if verify_ssl:
            return InventoryUnreachableError(
                f"The TLS certificate of {host}:{port} was refused. Pass --insecure to register it anyway",
                reason="certificate",
            )
        return InventoryUnreachableError(
            f"{host}:{port} answered, but not with the TLS a vSphere endpoint speaks. Check the "
            "address and port",
            reason="certificate",
        )
    detail = str(exc) or rendered or "no further detail"
    return InventoryUnreachableError(
        f"{host}:{port} answered, but the connection could not be completed: {detail}",
        reason="connection",
    )


def assert_inventory_registrable(record: dict[str, Any]) -> None:
    """Refuse an inventory whose endpoint cannot be reached and authenticated.

    This is a connectivity check, not discovery: it authenticates and asks the
    endpoint what it is, which is sub-second against a live vCenter. Walking the
    inventory still happens in the background after registration succeeds.
    """
    if inventory_type_from_record(record) != "vsphere":
        return
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    host = str(metadata.get("host") or "")
    port = int(metadata.get("port", 443) or 443)
    if not _endpoint_reachable(host, port, timeout=float(inventory_connect_timeout())):
        raise InventoryUnreachableError(
            f"{host}:{port} is not reachable from the backup server", reason="unreachable"
        )
    provider = InventoryProviderRegistry.resolve(record)
    try:
        service_type = provider.test_connection(record)
    except EsxHostInClusterError:
        raise
    except Exception as exc:
        raise _connection_failure(
            exc, host, port, verify_ssl=bool(metadata.get("verify_ssl", True))
        ) from exc
    if service_type == "HostAgent":
        raise EsxDirectConnectionUnsupported()


def test_inventory_connection(record: dict[str, Any]) -> dict[str, Any]:
    provider = InventoryProviderRegistry.resolve(record)
    try:
        provider.test_connection(record)
    except Exception:
        return {
            "status": "error",
            "inventory_id": record.get("id"),
            "name": record.get("name"),
            "type": inventory_type_from_record(record),
            "error": {
                "code": "INVENTORY_CONNECTION_FAILED",
                "message": "Unable to connect to virtualization environment",
            },
        }
    return {
        "status": "success",
        "inventory_id": record.get("id"),
        "name": record.get("name"),
        "type": inventory_type_from_record(record),
        "message": "Connection successful",
    }


def rescan_inventory(record: dict[str, Any]) -> dict[str, Any]:
    provider = InventoryProviderRegistry.resolve(record)
    try:
        resources = provider.rescan(record)
    except Exception:
        failed = dict(record)
        failed["status"] = "unavailable"
        failed["updated_at"] = utc_now()
        persist_hypervisor(failed)
        return {
            "status": "error",
            "inventory_id": record.get("id"),
            "name": record.get("name"),
            "type": inventory_type_from_record(record),
            "error": {
                "code": "INVENTORY_RESCAN_FAILED",
                "message": "Unable to refresh virtualization inventory",
            },
        }

    counts = _summarize_resources(resources)
    last_scan_at = utc_now()
    resources_to_store: list[dict[str, Any]] = []
    for resource in resources:
        item = dict(resource)
        item["discovered_at"] = last_scan_at
        resources_to_store.append(item)
    replace_resources_for_hypervisor(str(record["id"]), resources_to_store)

    updated = dict(record)
    updated.update(counts)
    updated["status"] = "available"
    updated["last_scan_at"] = last_scan_at
    updated["updated_at"] = last_scan_at
    persist_hypervisor(updated)
    return {
        "id": updated.get("id"),
        "name": updated.get("name"),
        "type": inventory_type_from_record(updated),
        "status": updated.get("status"),
        "last_scan_at": updated.get("last_scan_at"),
        **counts,
    }


def inventory_references(record_id: str) -> dict[str, Any]:
    jobs = [
        {
            "id": job.get("id"),
            "name": job.get("name") or ((job.get("metadata") or {}).get("name") if isinstance(job.get("metadata"), dict) else None),
        }
        for job in catalog_records(JOBS)
        if str(job.get("hypervisor_id") or job.get("inventory_id") or "") == record_id
        or (
            isinstance(job.get("metadata"), dict)
            and str(job["metadata"].get("inventory_id") or job["metadata"].get("hypervisor_id") or "") == record_id
        )
    ]
    tasks = [
        {"id": task.get("id"), "status": task.get("status")}
        for task in catalog_records(TASKS)
        if (
            str(task.get("hypervisor_id") or task.get("inventory_id") or "") == record_id
            and str(task.get("status") or "") in {"queued", "pending", "running", "stopping"}
        )
    ]
    return {
        "jobs": jobs,
        "tasks": tasks,
    }
