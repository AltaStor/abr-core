# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

import functools
import ipaddress
import json
import logging
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List

from ..hypervisor import Hypervisor, ProgressCallback

logger = logging.getLogger(__name__)

HTTPS_PORT = 5986
HTTP_PORT = 5985
PROBE_TIMEOUT_SECONDS = 5
OPERATION_TIMEOUT_SECONDS = 120
READ_TIMEOUT_SECONDS = 180
SCRIPT_DIR = Path(__file__).resolve().parent / "scripts"


class HyperVError(Exception):
    pass


class HyperVUnreachableError(HyperVError):
    def __init__(self, host: str, ports: List[int], detail: str = "") -> None:
        tried = ", ".join(str(port) for port in ports)
        message = f"Hyper-V host {host} did not answer WinRM on port {tried}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)
        self.host = host
        self.ports = list(ports)
        self.detail = detail


class HyperVAuthError(HyperVError):
    def __init__(self, host: str, username: str, detail: str = "") -> None:
        message = f"Hyper-V host {host} rejected the credentials for {username}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)
        self.host = host
        self.username = username
        self.detail = detail


class HyperVPermissionError(HyperVError):
    def __init__(self, host: str, username: str, detail: str = "") -> None:
        message = f"{username} may sign in to {host} but lacks the rights this needs"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)
        self.host = host
        self.username = username
        self.detail = detail


@functools.lru_cache(maxsize=None)
def load_script(name: str) -> str:
    path = SCRIPT_DIR / name
    if not path.is_file():
        raise HyperVError(f"Hyper-V script {name} is missing from {SCRIPT_DIR}")
    return path.read_text(encoding="utf-8")


def _port_is_open(host: str, port: int, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _is_auth_failure(exc: BaseException) -> bool:
    if type(exc).__name__ == "AuthenticationError":
        return True
    text = str(exc).lower()
    return "401" in text or "unauthorized" in text or "authenticate" in text


def _is_permission_failure(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "access is denied" in text or "accessdenied" in text or "403" in text


class HyperVNode(Hypervisor):
    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        port: int | None = None,
        verify_ssl: bool = False,
    ) -> None:
        self.host = str(host or "").strip()
        self.user = str(user or "")
        self.password = str(password or "")
        self.port = int(port) if port else None
        self.verify_ssl = bool(verify_ssl)
        self._client: Any = None
        self._clients: Dict[str, Any] = {}
        self._resolved_port: int | None = None
        self._cluster_name: str | None = None
        self._nodes: List[str] = []
        self._node_addresses: Dict[str, List[str]] = {}
        self._owners: Dict[str, str] = {}
        self._snapshots: Dict[str, Dict[str, Any]] = {}
        self._shared_stores: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def connect(self) -> None:
        if self._client is not None:
            return
        if not self.host:
            raise HyperVError("Hyper-V host address must not be empty")
        if not self.user or not self.password:
            raise HyperVError("Hyper-V credentials must not be empty")

        self._client = self._open(self.host)
        self._clients[self.host.lower()] = self._client

    def disconnect(self) -> None:
        self._clients.clear()
        self._snapshots.clear()
        self._nodes = []
        self._client = None

    @property
    def cluster_name(self) -> str | None:
        self._ensure_cluster()
        return self._cluster_name

    @property
    def nodes(self) -> List[str]:
        self._ensure_cluster()
        return list(self._nodes)

    def _ensure_cluster(self) -> None:
        if self._nodes:
            return
        if self._client is None:
            raise HyperVError("Hyper-V connection is not open")
        self._discover_cluster()

    @property
    def resolved_port(self) -> int | None:
        return self._resolved_port

    def get_service_type(self) -> str:
        self._ensure_cluster()
        return "Cluster" if self._cluster_name else "Host"

    def get_thumbprint(self) -> str:
        return ""

    def identity(self) -> Dict[str, str]:
        rows = self._run_json(load_script("identity.ps1"))
        raw = rows[0] if rows else {}
        return {
            "computer": str(raw.get("computer") or ""),
            "cluster": str(raw.get("cluster") or ""),
            "version": str(raw.get("version") or ""),
        }

    def list_hosts(self) -> List[Dict[str, Any]]:
        self._load_snapshots()
        return [self._snapshots[node.lower()]["host"] for node in self._nodes]

    def _load_snapshots(self) -> None:
        self._ensure_cluster()
        missing = [node for node in self._nodes if node.lower() not in self._snapshots]
        if not missing:
            return
        if len(missing) == 1:
            self._node_snapshot(missing[0])
            return
        with ThreadPoolExecutor(max_workers=len(missing)) as pool:
            list(pool.map(self._node_snapshot, missing))

    def get_all_vms(self) -> List[Dict[str, Any]]:
        return self.get_inventory()["virtual_machines"]

    def get_inventory(self) -> Dict[str, List[Dict[str, Any]]]:
        virtual_machines: List[Dict[str, Any]] = []
        disks: List[Dict[str, Any]] = []
        adapters: List[Dict[str, Any]] = []
        seen_vms: set[str] = set()
        seen_disks: set[str] = set()
        seen_nics: set[str] = set()

        self._load_snapshots()

        for node in self._nodes:
            snapshot = self._snapshots[node.lower()]

            for vm in snapshot["virtual_machines"]:
                if vm["vm_id"] in seen_vms:
                    continue
                seen_vms.add(vm["vm_id"])
                virtual_machines.append(vm)

            for disk in snapshot["disks"]:
                if disk["external_id"] in seen_disks:
                    continue
                seen_disks.add(disk["external_id"])
                disks.append(disk)

            for nic in snapshot["network_adapters"]:
                if nic["external_id"] in seen_nics:
                    continue
                seen_nics.add(nic["external_id"])
                adapters.append(nic)

        known = {vm["vm_id"] for vm in virtual_machines}

        return {
            "virtual_machines": virtual_machines,
            "disks": [disk for disk in disks if disk["vm_id"] in known],
            "network_adapters": [nic for nic in adapters if nic["vm_id"] in known],
        }

    def _node_snapshot(self, node: str) -> Dict[str, Any]:
        cached = self._snapshots.get(node.lower())
        if cached is not None:
            return cached

        rows = self._run_json(load_script("inventory.ps1"), node=node)
        raw = rows[0] if rows else {}
        snapshot = {
            "host": self._host_record(node, raw.get("host") or {}),
            "virtual_machines": [self._vm_record(node, row) for row in (raw.get("vms") or [])],
            "disks": [self._disk_record(row) for row in (raw.get("disks") or [])],
            "network_adapters": [self._nic_record(row) for row in (raw.get("nics") or [])],
        }
        with self._lock:
            self._snapshots[node.lower()] = snapshot

        return snapshot

    def _host_record(self, node: str, raw: Dict[str, Any]) -> Dict[str, Any]:
        local = [item for item in (raw.get("stores") or []) if isinstance(item, dict)]
        stores = self._shared_stores + local
        capacity = {
            str(item.get("name")): {
                "capacity_bytes": int(item.get("capacity") or 0),
                "free_bytes": int(item.get("free") or 0),
                "shared": bool(item.get("shared")),
            }
            for item in stores
            if item.get("name")
        }

        return {
            "name": str(raw.get("name") or node),
            "dns_name": str(raw.get("dns_name") or ""),
            "cluster": self._cluster_name,
            "datacenter": None,
            "version": str(raw.get("version") or ""),
            "datastores": sorted(capacity),
            "datastore_capacity": capacity,
            "networks": [str(name) for name in (raw.get("networks") or [])],
        }

    def _vm_record(self, node: str, raw: Dict[str, Any]) -> Dict[str, Any]:
        name = str(raw.get("name") or "")
        generation = int(raw.get("generation") or 1)

        return {
            "vm_id": str(raw.get("vm_id") or ""),
            "name": name,
            "power_state": str(raw.get("power_state") or ""),
            "num_cpu": int(raw.get("num_cpu") or 0),
            "memory_mb": int(raw.get("memory_mb") or 0),
            "guest_os": str(raw.get("guest_os") or ""),
            "firmware": "efi" if generation >= 2 else "bios",
            "generation": generation,
            "version": str(raw.get("version") or ""),
            "checkpoints": int(raw.get("checkpoints") or 0),
            "config_path": str(raw.get("config_path") or ""),
            "checkpoint_path": str(raw.get("checkpoint_path") or ""),
            "host": node,
            "clustered": name in self._owners,
            "owner_node": self._owners.get(name) or node,
        }

    def _disk_record(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        vm_id = str(raw.get("vm_id") or "")
        path = str(raw.get("path") or "")
        capacity_bytes = int(raw.get("capacity_bytes") or 0)

        return {
            "external_id": f"{vm_id}:{path}",
            "name": str(raw.get("name") or path),
            "vm_id": vm_id,
            "capacity_gb": round(capacity_bytes / (1024 ** 3), 2),
            "datastore_path": path,
            "uuid": str(raw.get("identifier") or ""),
            "source_disk_type_code": str(raw.get("format") or ""),
            "source_disk_type_name": str(raw.get("disk_type") or ""),
            "source_disk_eager_zeroed": str(raw.get("disk_type") or "").lower() == "fixed",
            "controller": str(raw.get("controller") or ""),
            "file_bytes": int(raw.get("file_bytes") or 0),
            "parent_path": str(raw.get("parent_path") or ""),
        }

    def _nic_record(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        vm_id = str(raw.get("vm_id") or "")

        return {
            "external_id": f"{vm_id}:{raw.get('id') or raw.get('mac')}",
            "name": str(raw.get("name") or ""),
            "vm_id": vm_id,
            "mac": str(raw.get("mac") or ""),
            "network": str(raw.get("network") or ""),
            "connected": bool(raw.get("connected")),
            "dynamic_mac": bool(raw.get("dynamic_mac")),
        }

    def _candidate_ports(self) -> List[int]:
        if self.port:
            return [self.port]
        return [HTTPS_PORT, HTTP_PORT]

    def _build_client(self, host: str, port: int) -> Any:
        try:
            from pypsrp.client import Client
        except ModuleNotFoundError as exc:
            raise HyperVError("Hyper-V support requires pypsrp to be installed") from exc

        return Client(
            host,
            username=self.user,
            password=self.password,
            port=port,
            ssl=port == HTTPS_PORT,
            cert_validation=self.verify_ssl,
            auth="ntlm",
            operation_timeout=OPERATION_TIMEOUT_SECONDS,
            read_timeout=READ_TIMEOUT_SECONDS,
        )

    def _open(self, host: str) -> Any:
        ports = self._candidate_ports()
        reachable = [port for port in ports if _port_is_open(host, port)]
        if not reachable:
            raise HyperVUnreachableError(host, ports)

        last_error: BaseException | None = None
        for port in reachable:
            client = self._build_client(host, port)
            try:
                client.execute_ps("$env:COMPUTERNAME")
            except Exception as exc:
                if _is_auth_failure(exc):
                    raise HyperVAuthError(host, self.user, str(exc).splitlines()[0]) from exc
                if _is_permission_failure(exc):
                    raise HyperVPermissionError(host, self.user, str(exc).splitlines()[0]) from exc

                logger.warning("Hyper-V host %s did not accept WinRM on port %d: %s", host, port, exc)
                last_error = exc
                continue

            self._resolved_port = port
            logger.info("Connected to Hyper-V host %s over WinRM port %d", host, port)
            return client

        raise HyperVUnreachableError(host, reachable, str(last_error or "")[:200])

    def _client_for(self, node: str) -> Any:
        key = str(node or "").lower()
        if not key or key == self.host.lower():
            return self._client

        with self._lock:
            existing = self._clients.get(key)
        if existing is not None:
            return existing

        last: BaseException | None = None
        for address in self._node_addresses.get(key) or [node]:
            try:
                client = self._open(address)
            except HyperVUnreachableError as exc:
                last = exc
                continue

            with self._lock:
                self._clients[key] = client
            return client

        raise last or HyperVUnreachableError(node, self._candidate_ports())

    def _run(self, script: str, node: str | None = None) -> str:
        client = self._client_for(node) if node else self._client
        if client is None:
            raise HyperVError("Hyper-V connection is not open")

        out, streams, had_errors = client.execute_ps(script)
        if had_errors:
            messages = "; ".join(str(record).strip() for record in streams.error)[:400]
            if _is_permission_failure(Exception(messages)):
                raise HyperVPermissionError(node or self.host, self.user, messages)
            raise HyperVError(f"PowerShell failed on {node or self.host}: {messages}")

        return out

    def _run_json(self, script: str, node: str | None = None) -> List[Dict[str, Any]]:
        text = self._run(f"@({script}) | ConvertTo-Json -Depth 6 -Compress", node).strip()
        if not text:
            return []

        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return [parsed]

        return [item for item in parsed if isinstance(item, dict)]

    def _discover_cluster(self) -> None:
        rows = self._run_json(load_script("cluster.ps1"))
        raw = rows[0] if rows else {}
        nodes = [row for row in (raw.get("nodes") or []) if isinstance(row, dict)]

        self._shared_stores = [
            item for item in (raw.get("shared_stores") or []) if isinstance(item, dict)
        ]
        self._owners = {
            str(row.get("name")): str(row.get("owner") or "")
            for row in (raw.get("owners") or [])
            if isinstance(row, dict) and row.get("name")
        }

        if nodes:
            self._cluster_name = str(raw.get("cluster") or "") or None
            self._nodes = [str(row.get("name")) for row in nodes if row.get("name")]
            self._node_addresses = {
                str(row.get("name")).lower(): self._node_candidates(row)
                for row in nodes
                if row.get("name")
            }
            logger.info(
                "Hyper-V host %s belongs to cluster %s with nodes %s",
                self.host,
                self._cluster_name,
                ", ".join(self._nodes),
            )
            return

        self._cluster_name = None
        self._nodes = [str(raw.get("computer") or "").strip() or self.host]
        self._node_addresses = {self._nodes[0].lower(): [self.host]}
        logger.info("Hyper-V host %s is standalone (node %s)", self.host, self._nodes[0])

    def _node_candidates(self, row: Dict[str, Any]) -> List[str]:
        raw = row.get("addresses")
        addresses = [str(value) for value in raw] if isinstance(raw, list) else ([str(raw)] if raw else [])
        candidates = self._rank_addresses(addresses)

        fqdn = str(row.get("fqdn") or "")
        if fqdn and fqdn not in candidates:
            candidates.append(fqdn)

        name = str(row.get("name") or "")
        if name and name not in candidates:
            candidates.append(name)

        return candidates

    def _rank_addresses(self, addresses: List[str]) -> List[str]:
        try:
            registered = ipaddress.ip_address(self.host)
        except ValueError:
            registered = None

        routable: List[str] = []
        for value in addresses:
            try:
                candidate = ipaddress.ip_address(value)
            except ValueError:
                continue
            if candidate.is_link_local or candidate.is_loopback:
                continue
            routable.append(value)

        if registered is None:
            return routable

        network = ipaddress.ip_network(f"{registered}/24", strict=False)
        same_network = [value for value in routable if ipaddress.ip_address(value) in network]
        rest = [value for value in routable if value not in same_network]

        return same_network + rest

    def get_backup_parameters(self, vm_name: str, snapshot_name: str) -> Dict[str, str]:
        raise NotImplementedError("Hyper-V backup parameters are not implemented yet")

    def create_backup_snapshot(self, vm_name: str) -> str:
        raise NotImplementedError("Hyper-V checkpoints are not implemented yet")

    def ensure_cbt_enabled(self, vm_name: str) -> bool:
        raise NotImplementedError("Hyper-V reference points are not implemented yet")

    def get_snapshot_change_ids(self, vm_name: str, snapshot_name: str) -> List[Dict[str, Any]]:
        raise NotImplementedError("Hyper-V reference points are not implemented yet")

    def query_changed_disk_areas(
        self,
        vm_name: str,
        snapshot_name: str,
        disk_key: int,
        change_id: str,
    ) -> List[Dict[str, int]]:
        raise NotImplementedError("Hyper-V changed block tracking is not implemented yet")

    def get_vm_disks(self, vm_name: str) -> List[Dict[str, Any]]:
        raise NotImplementedError("Hyper-V disk enumeration is not implemented yet")

    def get_vm_restore_metadata(self, vm_name: str) -> Dict[str, Any]:
        raise NotImplementedError("Hyper-V restore metadata is not implemented yet")

    def create_vm_shell(
        self,
        *,
        vm_name: str,
        host_name: str,
        datastore_name: str,
        config_metadata: Dict[str, Any],
        disk_layout: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError("Hyper-V VM creation is not implemented yet")

    def remove_snapshot(self, vm_name: str, snapshot_name: str) -> None:
        raise NotImplementedError("Hyper-V checkpoints are not implemented yet")

    def attach_existing_disk(
        self,
        vm_name: str,
        datastore_path: str,
        disk_label: str | None = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError("Hyper-V disk attach is not implemented yet")

    def backup(
        self,
        task: dict[str, Any],
        progress_callback: ProgressCallback = None,
    ) -> dict[str, Any]:
        raise NotImplementedError("Hyper-V backup is not implemented yet")

    def restore(
        self,
        task: dict[str, Any],
        progress_callback: ProgressCallback = None,
    ) -> dict[str, Any]:
        raise NotImplementedError("Hyper-V restore is not implemented yet")
