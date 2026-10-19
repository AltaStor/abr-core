# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

import base64
import functools
import ipaddress
import json
import logging
import re
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from collections.abc import Callable
from typing import Any, Dict, List, Tuple

from ...core.credential_encryption import StoredCredentialUnreadable, is_encrypted, unreadable_credential_message
from ...core.mongo_db_wrapper import HYPERVISORS, update_hypervisor_fields
from ...core.storage_tool import mask_secret_values, redact_disk_tool_command, split_disk_tool_secrets
from ..hypervisor import Hypervisor, ProgressCallback
from . import vhdx_tool

logger = logging.getLogger(__name__)

HTTPS_PORT = 5986
HTTP_PORT = 5985
TOOL_SECRET_PARAMETERS = {
    "key": "Key",
    "secret-key": "SecretKey",
    "source-key": "SourceKey",
    "source-secret-key": "SourceSecretKey",
}
WINRM_PORTS = {"https": HTTPS_PORT, "http": HTTP_PORT}
DEFAULT_WINRM_PROTOCOL = "https"
PROBE_TIMEOUT_SECONDS = 5
OPERATION_TIMEOUT_SECONDS = 120
READ_TIMEOUT_SECONDS = 180
NODE_SNAPSHOT_TIMEOUT_SECONDS = 90
SCRIPT_DIR = Path(__file__).resolve().parent / "scripts"


class HyperVError(Exception):
    pass


class HyperVUnreachableError(HyperVError):
    def __init__(self, host: str, ports: List[int], detail: str = "", protocol: str | None = None) -> None:
        tried = ", ".join(str(port) for port in ports)
        over = f" over {protocol.upper()}" if protocol else ""
        message = f"Hyper-V host {host} did not answer WinRM{over} on port {tried}"
        if detail:
            message = f"{message}: {detail}"
        if protocol == "https":
            message = f"{message}. Check that a WinRM HTTPS listener is set up on that port"
        super().__init__(message)
        self.host = host
        self.ports = list(ports)
        self.detail = detail
        self.protocol = protocol


class HyperVCertificateError(HyperVError):
    def __init__(self, host: str, port: int, detail: str = "") -> None:
        message = f"The TLS certificate of Hyper-V host {host}:{port} was not trusted"
        if detail:
            message = f"{message} ({detail})"
        message = f"{message}. Install a certificate the backup server trusts, or skip TLS verification for this inventory (--insecure in the CLI)"
        super().__init__(message)
        self.host = host
        self.port = port
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


class HyperVVmNotFoundError(HyperVError):
    pass


class HyperVVmStateError(HyperVError):
    pass


class HyperVCheckpointError(HyperVError):
    pass


class HyperVPermissionError(HyperVError):
    def __init__(self, host: str, username: str, detail: str = "") -> None:
        message = f"{username} may sign in to {host} but lacks the rights this needs"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)
        self.host = host
        self.username = username
        self.detail = detail


_CERTIFICATE_REASON = re.compile(r"certificate verify failed:\s*([^()'\]]+)", re.IGNORECASE)
_GUID_PATTERN = re.compile(r"^\{?[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\}?$")

# VM states a checkpoint can be taken in. A running VM gets an
# application-consistent one when its guest allows; the others have nothing
# running inside to quiesce, so their disks are consistent as they stand.
BACKUP_READY_STATES = frozenset({"Running", "Off", "Saved", "Paused", "FastSaved"})
QUIESCABLE_STATES = frozenset({"Running"})
# Work Hyper-V is in the middle of that a checkpoint must not race with.
BUSY_OPERATIONS = frozenset(
    {
        "ApplyingSnapshot",
        "CreatingSnapshot",
        "DeletingSnapshot",
        "MergingDisks",
        "ExportingVirtualMachine",
        "MigratingVirtualMachine",
        "BackingUpVirtualMachine",
        "StorageMigrationPhaseOne",
        "StorageMigrationPhaseTwo",
        "MigratingPlannedVm",
    }
)
CONSISTENCY_LEVELS = {"application": 1, "crash": 2}


def is_guid(value: str) -> bool:
    return bool(_GUID_PATTERN.match(str(value or "").strip()))


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@functools.lru_cache(maxsize=None)
def load_script(name: str) -> str:
    path = SCRIPT_DIR / name
    if not path.is_file():
        raise HyperVError(f"Hyper-V script {name} is missing from {SCRIPT_DIR}")
    return path.read_text(encoding="utf-8")


def _bundle_script_block(folder: str, name: str) -> str:
    path = folder + "\\scripts\\" + name
    return f"([ScriptBlock]::Create([IO.File]::ReadAllText({_ps_quote(path)})))"


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


def _is_certificate_failure(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "certificate_verify_failed" in text or "certificate verify failed" in text


def _certificate_reason(exc: BaseException) -> str:
    match = _CERTIFICATE_REASON.search(str(exc))
    return match.group(1).strip().rstrip(".") if match else "certificate verify failed"


def _answers_without_tls(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "wrong_version_number" in text or "wrong version number" in text or "record layer failure" in text


def winrm_protocol(value: Any) -> str | None:
    protocol = str(value or "").strip().lower()
    if not protocol:
        return None
    if protocol not in WINRM_PORTS:
        raise ValueError(f"WinRM protocol must be one of: {', '.join(WINRM_PORTS)}")
    return protocol


def node_options(metadata: dict[str, Any]) -> dict[str, Any]:
    protocol = winrm_protocol(metadata.get("protocol"))
    port = metadata.get("port")
    explicit = protocol is not None or bool(metadata.get("port_explicit"))
    return {
        "port": int(port) if port and explicit else None,
        "verify_ssl": bool(metadata.get("verify_ssl", False)),
        "protocol": protocol,
    }


def with_protocol(metadata: dict[str, Any], protocol: str) -> dict[str, Any]:
    updated = dict(metadata)
    updated["protocol"] = protocol
    if not updated.get("port_explicit") or updated.get("port") in WINRM_PORTS.values():
        updated["port"] = WINRM_PORTS[protocol]
    return updated


def protocol_recorder(hypervisor_id: str) -> Callable[[str], None] | None:
    if not hypervisor_id:
        return None
    return lambda protocol: record_detected_protocol(hypervisor_id, protocol)


def record_detected_protocol(hypervisor_id: str, protocol: str) -> None:
    current = HYPERVISORS.get(hypervisor_id)
    if not isinstance(current, dict):
        return
    metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
    if metadata.get("protocol"):
        return
    try:
        update_hypervisor_fields(hypervisor_id, {"metadata": with_protocol(metadata, protocol)})
    except Exception as exc:
        logger.warning("Could not save the WinRM protocol of Hyper-V inventory %s: %s", current.get("name"), exc)
        return
    log_detected_protocol(str(current.get("name") or hypervisor_id), protocol)


def log_detected_protocol(name: str, protocol: str) -> None:
    if protocol == "http":
        logger.warning(
            "Hyper-V inventory %s answers WinRM only over HTTP, so it stays on HTTP from now on. "
            "Set up a WinRM HTTPS listener on the host and switch the inventory to HTTPS",
            name,
        )
        return
    logger.info("Hyper-V inventory %s answers WinRM over HTTPS and stays on HTTPS from now on", name)


class HyperVNode(Hypervisor):
    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        port: int | None = None,
        verify_ssl: bool = False,
        protocol: str | None = None,
        on_protocol_detected: Callable[[str], None] | None = None,
    ) -> None:
        self.host = str(host or "").strip()
        self.user = str(user or "")
        self.password = str(password or "")
        self.port = int(port) if port else None
        self.verify_ssl = bool(verify_ssl)
        self.protocol = winrm_protocol(protocol)
        self._on_protocol_detected = on_protocol_detected
        self._client: Any = None
        self._clients: Dict[str, Any] = {}
        self._resolved_port: int | None = None
        self._cluster_name: str | None = None
        self._nodes: List[str] = []
        self._node_addresses: Dict[str, List[str]] = {}
        self._owners: Dict[str, str] = {}
        self._snapshots: Dict[str, Dict[str, Any]] = {}
        self._shared_stores: List[Dict[str, Any]] = []
        self._node_resolved: Dict[str, str] = {}
        self._vm_nodes: Dict[str, str] = {}
        self._vm_states: Dict[str, Dict[str, Any]] = {}
        self._tool_paths: Dict[str, str] = {}
        self._bundles: Dict[str, str] = {}
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
        with self._lock:
            self._bundles.clear()
            self._tool_paths.clear()
        self._snapshots.clear()
        self._vm_nodes.clear()
        self._vm_states.clear()
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
        pool = ThreadPoolExecutor(max_workers=len(missing))
        futures = {pool.submit(self._node_snapshot, node): node for node in missing}
        done, pending = wait(futures, timeout=NODE_SNAPSHOT_TIMEOUT_SECONDS)
        pool.shutdown(wait=False, cancel_futures=True)
        for future in done:
            future.result()
        if pending and len(pending) == len(self._nodes):
            raise HyperVError(
                f"No Hyper-V node of {self.host} listed its VMs within {NODE_SNAPSHOT_TIMEOUT_SECONDS}s "
                f"({', '.join(sorted(futures[future] for future in pending))})"
            )
        for future in pending:
            node = futures[future]
            logger.warning(
                "Hyper-V node %s did not list its VMs within %ds; treating it as unavailable for now",
                node,
                NODE_SNAPSHOT_TIMEOUT_SECONDS,
            )
            with self._lock:
                self._snapshots.setdefault(
                    node.lower(),
                    {
                        "host": {**self._host_record(node, {}), "status": "unavailable"},
                        "virtual_machines": [],
                        "disks": [],
                        "network_adapters": [],
                    },
                )

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
            "status": "connected",
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

    def _candidates(self) -> List[Tuple[int, str]]:
        if self.protocol:
            return [(self.port or WINRM_PORTS[self.protocol], self.protocol)]
        if self.port:
            return [(self.port, "https" if self.port == HTTPS_PORT else "http")]
        return [(HTTPS_PORT, "https"), (HTTP_PORT, "http")]

    def _candidate_ports(self) -> List[int]:
        return [port for port, _ in self._candidates()]

    def _build_client(self, host: str, port: int, protocol: str) -> Any:
        if is_encrypted(self.password):
            raise StoredCredentialUnreadable(unreadable_credential_message(f"for Hyper-V host {self.host}"))
        try:
            from pypsrp.client import Client
        except ModuleNotFoundError as exc:
            raise HyperVError("Hyper-V support requires pypsrp to be installed") from exc

        return Client(
            host,
            username=self.user,
            password=self.password,
            port=port,
            ssl=protocol == "https",
            cert_validation=self.verify_ssl,
            auth="ntlm",
            operation_timeout=OPERATION_TIMEOUT_SECONDS,
            read_timeout=READ_TIMEOUT_SECONDS,
        )

    def _open(self, host: str) -> Any:
        candidates = self._candidates()
        configured = self.protocol
        ports = [port for port, _ in candidates]
        reachable = [(port, protocol) for port, protocol in candidates if _port_is_open(host, port)]
        if not reachable:
            logger.warning(
                "Hyper-V host %s does not answer WinRM on port(s) %s",
                host,
                ", ".join(str(port) for port in ports),
            )
            raise HyperVUnreachableError(host, ports, protocol=configured)

        last_error: str = ""
        for port, protocol in reachable:
            client = self._build_client(host, port, protocol)
            try:
                client.execute_ps("$env:COMPUTERNAME")
            except Exception as exc:
                first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                if _is_auth_failure(exc):
                    logger.warning(
                        "Hyper-V host %s rejected the credentials of %s on WinRM port %d: %s",
                        host,
                        self.user,
                        port,
                        first_line,
                    )
                    raise HyperVAuthError(host, self.user, first_line) from exc
                if _is_permission_failure(exc):
                    logger.warning(
                        "Hyper-V host %s authenticated %s on WinRM port %d but refused the access it needs: %s",
                        host,
                        self.user,
                        port,
                        first_line,
                    )
                    raise HyperVPermissionError(host, self.user, first_line) from exc
                if configured == "https" and _is_certificate_failure(exc):
                    logger.warning(
                        "Hyper-V host %s offered a TLS certificate on WinRM port %d that was not trusted: %s",
                        host,
                        port,
                        first_line,
                    )
                    raise HyperVCertificateError(host, port, _certificate_reason(exc)) from exc

                logger.warning("Hyper-V host %s did not accept WinRM on port %d: %s", host, port, exc)
                if protocol == "https" and _answers_without_tls(exc):
                    last_error = "the port answers without TLS, the way an HTTP listener does"
                else:
                    last_error = str(exc)[:200]
                continue

            self._resolved_port = port
            logger.info(
                "Connected to Hyper-V host %s over WinRM port %d (%s) as %s",
                host,
                port,
                protocol.upper(),
                self.user,
            )
            if not configured:
                self.protocol = protocol
                if self._on_protocol_detected is not None:
                    self._on_protocol_detected(protocol)
            return client

        raise HyperVUnreachableError(host, [port for port, _ in reachable], last_error, protocol=configured)

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
                self._node_resolved[key] = address
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

    def _run_json(self, script: str, node: str | None = None, depth: int = 6) -> List[Dict[str, Any]]:
        text = self._run(f"@({script}) | ConvertTo-Json -Depth {depth} -Compress", node).strip()
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

    # ------------------------------------------------------------------
    # Backup and restore. These follow the Hypervisor interface the vCenter
    # plugin implements, in Hyper-V terms: a "snapshot" is a recovery
    # checkpoint, a disk key or label is the drive's controller slot
    # ("SCSI0:1"), a "datastore" is a volume, Cluster Shared Volume or share.
    # Every VM argument takes the VM's id (a GUID) or, when it is unique, its
    # name: Hyper-V does not keep names unique, so the engines always pass ids.
    # The disks themselves are read and written by abr-vhdx-tool on the host
    # (run_disk_tool), which also asks resilient change tracking (RCT) what
    # changed; a backup's checkpoint ends as a reference point that keeps the
    # tracking position the next incremental starts from.
    # ------------------------------------------------------------------

    def _run_script(
        self,
        name: str,
        args: Dict[str, Any],
        node: str | None = None,
        *,
        depth: int = 6,
        inline: bool = False,
    ) -> List[Dict[str, Any]]:
        payload = json.dumps(args, ensure_ascii=True)
        folder = None if inline else self._bundle_folder(node)
        if folder:
            body = f". {_bundle_script_block(folder, 'common.ps1')}\n. {_bundle_script_block(folder, name)}"
        else:
            body = load_script("common.ps1") + "\n" + load_script(name)
        script = f"$AbrArgs = {_ps_quote(payload)} | ConvertFrom-Json\n& {{\n{body}\n}}"
        return self._run_json(script, node, depth=depth)

    def node_address(self, node: str) -> str:
        """The address this server reaches a node at over WinRM."""
        key = str(node or "").lower()
        if not key or key == self.host.lower():
            return self.host
        resolved = self._node_resolved.get(key)
        if resolved:
            return resolved
        candidates = self._node_addresses.get(key) or []
        if len(self._nodes) == 1 and key == self._nodes[0].lower():
            return self.host
        return candidates[0] if candidates else node

    def resolve_node(self, name: str) -> str:
        """The cluster node (or the standalone host) a host name refers to."""
        wanted = str(name or "").strip().lower()
        for node in self.nodes:
            if node.lower() == wanted or node.lower().split(".")[0] == wanted.split(".")[0]:
                return node
        if len(self.nodes) == 1 and wanted in (self.host.lower(), ""):
            return self.nodes[0]
        raise HyperVError(f"{name} is not a node of the Hyper-V inventory at {self.host} ({', '.join(self.nodes)})")

    def _vm_id(self, vm: str) -> str:
        value = str(vm or "").strip()
        if is_guid(value):
            return value.strip("{}").lower()
        matches: Dict[str, Dict[str, Any]] = {}
        for node in self.nodes:
            for row in self._run_script("vm_by_name.ps1", {"name": value}, node):
                matches[str(row.get("vm_id") or "").lower()] = row
        if not matches:
            raise HyperVVmNotFoundError(f"No virtual machine named {value!r} on {self.host}")
        if len(matches) > 1:
            raise HyperVVmNotFoundError(
                f"{len(matches)} virtual machines on {self.host} are named {value!r}; use the VM id"
            )
        vm_id, row = next(iter(matches.items()))
        self._vm_nodes[vm_id] = str(row.get("computer") or "")
        return vm_id

    def locate_vm(self, vm: str, *, hint: str | None = None) -> Dict[str, Any]:
        """Find the node running a VM and report its state.

        A clustered VM moves between nodes, so the node it was discovered on
        is only the first place to look.
        """
        vm_id = self._vm_id(vm)
        tried: set[str] = set()
        candidates = [self._vm_nodes.get(vm_id), hint, *self.nodes]
        for node in candidates:
            if not node or node.lower() in tried:
                continue
            tried.add(node.lower())
            try:
                rows = self._run_script("vm_state.ps1", {"vm_id": vm_id}, node)
            except HyperVUnreachableError:
                logger.warning("Hyper-V node %s is unreachable while looking for VM %s", node, vm_id)
                continue
            if rows:
                state = rows[0]
                self._vm_nodes[vm_id] = str(state.get("computer") or node)
                self._vm_states[vm_id] = state
                return state
        raise HyperVVmNotFoundError(
            f"Virtual machine {vm} is not on any node of {self.host} ({', '.join(sorted(tried)) or 'none reachable'})"
        )

    def _vm_node(self, vm_id: str) -> str:
        node = self._vm_nodes.get(vm_id)
        if node:
            return node
        return str(self.locate_vm(vm_id).get("computer") or "")

    def _run_vm_script(self, vm: str, name: str, args: Dict[str, Any], **kwargs: Any) -> List[Dict[str, Any]]:
        """Run a script on the node that has the VM, following it if it moved."""
        vm_id = self._vm_id(vm)
        payload = {"vm_id": vm_id, **args}
        node = self._vm_node(vm_id)
        try:
            return self._run_script(name, payload, node, **kwargs)
        except HyperVError as exc:
            if "ABR_VM_NOT_FOUND" not in str(exc):
                raise
        self._vm_nodes.pop(vm_id, None)
        node = self._vm_node(vm_id)
        return self._run_script(name, payload, node, **kwargs)

    def ensure_vm_host_connected(self, vm_name: str, *, hint: str | None = None) -> Dict[str, Any]:
        """Refuse up front when the VM cannot be checkpointed right now.

        Mirrors the vCenter check for a disconnected ESXi host: a VM that is
        mid-transition, in a critical state, or busy with another storage
        operation fails the checkpoint with an opaque WMI error, so the reason
        is spelled out before anything is changed on the host.
        """
        state = self.locate_vm(vm_name, hint=hint)
        name = str(state.get("name") or vm_name)
        current = str(state.get("state") or "")
        if current.endswith("Critical"):
            raise HyperVVmStateError(
                f"VM {name} is in the {current} state: Hyper-V has lost access to its storage or "
                "configuration. Fix the VM on the host before backing it up"
            )
        if current not in BACKUP_READY_STATES:
            raise HyperVVmStateError(
                f"VM {name} is {current or 'in an unknown state'}; a backup can start once it is "
                "running, off, saved or paused. Retry when the transition has finished"
            )
        busy = sorted(BUSY_OPERATIONS.intersection(state.get("operational_status") or []))
        if busy:
            raise HyperVVmStateError(
                f"VM {name} is busy ({', '.join(busy)}); a checkpoint cannot be taken until Hyper-V "
                "has finished. Retry later"
            )
        return state

    def ensure_cbt_enabled(self, vm_name: str) -> bool:
        """Nothing to enable: taking a recovery checkpoint turns resilient
        change tracking on for the VM's disks. Returns False ("nothing had to
        be enabled"), as the vCenter plugin does when CBT is on."""
        return False

    def create_recovery_checkpoint(self, vm_name: str, consistency: str = "application") -> Dict[str, Any]:
        level = CONSISTENCY_LEVELS.get(consistency)
        if level is None:
            raise HyperVCheckpointError(f"Unknown checkpoint consistency {consistency!r}")
        rows = self._run_vm_script(vm_name, "create_checkpoint.ps1", {"consistency_level": level})
        if not rows or not rows[0].get("checkpoint_id"):
            raise HyperVCheckpointError(f"Hyper-V created no recovery checkpoint for VM {vm_name}")
        result = dict(rows[0])
        result["consistency"] = consistency
        logger.info(
            "Created %s-consistent recovery checkpoint %s for VM %s on %s",
            consistency,
            result["checkpoint_id"],
            vm_name,
            result.get("computer"),
        )
        return result

    def create_backup_snapshot(self, vm_name: str) -> str:
        """An application-consistent recovery checkpoint when the guest can
        quiesce, a crash-consistent one otherwise; returns its id."""
        state = self.locate_vm(vm_name)
        if str(state.get("state") or "") not in QUIESCABLE_STATES:
            return str(self.create_recovery_checkpoint(vm_name, "crash")["checkpoint_id"])
        try:
            return str(self.create_recovery_checkpoint(vm_name, "application")["checkpoint_id"])
        except HyperVError as exc:
            logger.warning(
                "Application-consistent checkpoint of VM %s failed (%s); taking a crash-consistent one",
                vm_name,
                exc,
            )
            return str(self.create_recovery_checkpoint(vm_name, "crash")["checkpoint_id"])

    def get_backup_parameters(self, vm_name: str, snapshot_name: str) -> Dict[str, str]:
        vm_id = self._vm_id(vm_name)
        node = self._vm_node(vm_id)
        return {
            "vm_id": vm_id,
            "checkpoint_id": str(snapshot_name),
            "node": node,
            "node_address": self.node_address(node),
        }

    def checkpoint_disks(self, vm_name: str, snapshot_name: str) -> List[Dict[str, Any]]:
        """Every hard disk of the checkpoint, frozen file first, with its
        chain; disks a backup cannot read say why."""
        rows = self._run_vm_script(vm_name, "checkpoint_disks.ps1", {"checkpoint_id": str(snapshot_name)})
        disks = [self._disk_entry(row) for row in rows]
        disks.sort(key=lambda item: _slot_sort_key(item["slot"]))
        return disks

    def get_snapshot_change_ids(self, vm_name: str, snapshot_name: str) -> List[Dict[str, Any]]:
        """The checkpoint's disks. Their RCT ids come from abr-vhdx-tool, which
        reports each disk's id (``rct_id``) as it backs the disk up."""
        return [
            {
                "disk_label": disk["slot"],
                "datastore_path": disk["datastore_path"],
                "checkpoint_path": disk["checkpoint_path"],
                "change_id": "",
                "uuid": disk["uuid"],
                "key": disk["slot"],
                "capacity_bytes": disk["capacity_bytes"],
            }
            for disk in self.checkpoint_disks(vm_name, snapshot_name)
            if disk["supported"]
        ]

    def query_changed_disk_areas(
        self,
        vm_name: str,
        snapshot_name: str,
        disk_key: Any,
        change_id: str,
    ) -> List[Dict[str, int]]:
        raise NotImplementedError(
            "abr-vhdx-tool asks resilient change tracking for the changed areas itself (--change-id)"
        )

    def get_vm_disks(self, vm_name: str) -> List[Dict[str, Any]]:
        disks = [self._disk_entry(row) for row in self._run_vm_script(vm_name, "live_disks.ps1", {})]
        disks.sort(key=lambda item: _slot_sort_key(item["slot"]))
        for disk in disks:
            disk["current_datastore_path"] = disk.pop("checkpoint_path")
        return disks

    def get_vm_restore_metadata(self, vm_name: str) -> Dict[str, Any]:
        rows = self._run_vm_script(vm_name, "vm_config.ps1", {})
        if not rows:
            raise HyperVError(f"Hyper-V returned no configuration for VM {vm_name}")
        config = dict(rows[0])
        config.setdefault("guest_id", None)
        config.setdefault("scsi_controller_type", None)
        return config

    def remove_snapshot(self, vm_name: str, snapshot_name: str) -> None:
        """Remove a recovery checkpoint; a checkpoint already gone is not an error."""
        rows = self._run_vm_script(vm_name, "remove_checkpoint.ps1", {"checkpoint_id": str(snapshot_name)})
        outcome = rows[0] if rows else {}
        if outcome.get("removed"):
            logger.info("Removed recovery checkpoint %s of VM %s", snapshot_name, vm_name)
        else:
            logger.info(
                "Recovery checkpoint %s of VM %s was already gone (%s)",
                snapshot_name,
                vm_name,
                outcome.get("reason") or "unknown",
            )

    def convert_to_reference_point(self, vm_name: str, snapshot_name: str) -> Dict[str, Any]:
        """Merge a backup's recovery checkpoint back into the VM, keeping the
        change tracking position it marked as a reference point."""
        rows = self._run_vm_script(vm_name, "reference_point.ps1", {"checkpoint_id": str(snapshot_name)})
        point = dict(rows[0]) if rows else {}
        logger.info(
            "Recovery checkpoint %s of VM %s is now reference point %s",
            snapshot_name,
            vm_name,
            point.get("reference_point_id") or "(not reported)",
        )
        return point

    def remove_reference_point(self, vm_name: str, reference_point_id: str) -> bool:
        """Remove a reference point an earlier backup left; one already gone is
        not an error. Returns whether one was removed."""
        rows = self._run_vm_script(
            vm_name, "remove_reference_point.ps1", {"reference_point_id": str(reference_point_id)}
        )
        removed = bool((rows[0] if rows else {}).get("removed"))
        logger.info(
            "Reference point %s of VM %s %s",
            reference_point_id,
            vm_name,
            "removed" if removed else "was already gone",
        )
        return removed

    # -- abr-vhdx-tool ------------------------------------------------------

    def _bundle_folder(self, node: str | None) -> str | None:
        with self._lock:
            return self._bundles.get(str(node or self.host).lower())

    def ensure_host_bundle(self, node: str) -> str:
        """The folder on a node that holds this appliance's abr-vhdx-tool and
        Hyper-V scripts, named by their SHA-256; uploaded first when it is
        missing there or a file in it differs."""
        key = str(node or self.host).lower()
        known = self._bundle_folder(node)
        if known:
            return known
        bundle = vhdx_tool.host_bundle(SCRIPT_DIR)
        folder = bundle.folder(vhdx_tool.remote_work_dir())
        expected = [{"name": name, "sha256": digest} for name, _path, digest in bundle.files]
        state = (self._run_script("bundle_check.ps1", {"folder": folder, "expected": expected}, node, inline=True) or [{}])[0]
        if state.get("complete"):
            logger.info("Hyper-V node %s already holds this appliance's files in %s", node, folder)
        else:
            self._deploy_bundle(node, bundle, folder, expected, state)
        with self._lock:
            self._bundles[key] = folder
        return folder

    def _deploy_bundle(
        self,
        node: str,
        bundle: "vhdx_tool.HostBundle",
        folder: str,
        expected: List[Dict[str, str]],
        state: Dict[str, Any],
    ) -> None:
        staging = f"{vhdx_tool.remote_work_dir()}\\.staging-{bundle.bundle_id[:12]}-{uuid.uuid4().hex[:8]}"
        texts = [
            {"name": name, "content": base64.b64encode(path.read_bytes()).decode("ascii")}
            for name, path, _digest in bundle.files
            if name.startswith("scripts\\")
        ]
        logger.info(
            "Uploading this appliance's files to %s on Hyper-V node %s (%s)",
            folder,
            node,
            "some differ there" if state.get("exists") else "it is not there",
        )
        started = time.monotonic()
        self._run_script("stage_bundle.ps1", {"staging": staging, "files": texts}, node, inline=True)
        client = self._client_for(node) if node else self._client
        for name, path, _digest in bundle.files:
            if not name.startswith("scripts\\"):
                client.copy(str(path), f"{staging}\\{name}")
        result = (
            self._run_script(
                "finalize_bundle.ps1", {"staging": staging, "folder": folder, "expected": expected}, node, inline=True
            )
            or [{}]
        )[0]
        logger.info(
            "Hyper-V node %s: %s %s in %.1fs",
            node,
            folder,
            "was put in place by another run meanwhile" if result.get("status") == "present" else "uploaded",
            time.monotonic() - started,
        )

    def ensure_disk_tool(self, node: str) -> str:
        """The path of abr-vhdx-tool.exe on a node: the copy in this
        appliance's file folder there, or the one installed on the host when
        the appliance has none."""
        key = str(node or self.host).lower()
        with self._lock:
            known = self._tool_paths.get(key)
        if known:
            return known
        folder = self.ensure_host_bundle(node)
        remote = f"{folder}\\{vhdx_tool.TOOL_NAME}" if vhdx_tool.local_tool_path() is not None else vhdx_tool.remote_tool_path()
        info = (self._run_script("tool_info.ps1", {"path": remote}, node) or [{}])[0]
        if not info.get("exists"):
            raise HyperVError(
                f"abr-vhdx-tool is not installed on {node} at {remote}. Install it there, or set "
                "ALTASTOR_VHDX_TOOL_PATH on this appliance to the .exe so it is copied to every host"
            )
        logger.info("abr-vhdx-tool %s is ready on Hyper-V node %s at %s", info.get("version") or "", node, remote)
        with self._lock:
            self._tool_paths[key] = remote
        return remote

    def run_disk_tool(
        self,
        node: str,
        args: List[str],
        *,
        env: Dict[str, str] | None = None,
        on_progress: Any = None,
        should_cancel: Any = None,
        timeout: float | None = None,
    ) -> Dict[str, Any]:
        """Run abr-vhdx-tool on a node and wait for it, relaying its
        ``NN%`` lines to ``on_progress``.

        Returns ``exit_code``, the ``--json`` ``result`` and the other
        ``lines`` it printed. A stop request or a timeout stops the run,
        which ends the tool; it discards what it had written itself, and no
        virtual disk stays attached once it is gone.
        """
        from pypsrp.complex_objects import ObjectMeta, PSInvocationState
        from pypsrp.powershell import PowerShell, RunspacePool

        from ...core.subprocess_util import ProcessCancelled

        tool = self.ensure_disk_tool(node)
        client = self._client_for(node) if node else self._client
        if client is None:
            raise HyperVError("Hyper-V connection is not open")
        logger.info("Running abr-vhdx-tool on %s: %s", node, " ".join(redact_disk_tool_command([tool, *args])))
        tool_args, secrets = split_disk_tool_secrets(list(args))
        unknown = [name for name, _ in secrets if name not in TOOL_SECRET_PARAMETERS]
        if unknown:
            raise HyperVError(f"abr-vhdx-tool takes no {', '.join(unknown)} on its standard input")
        env = dict(env or {})
        share_password = env.pop("ABR_SMB_PASSWORD", None)
        if env.get("ABR_SMB_SHARE") and not env.get("ABR_SMB_USER"):
            env["ABR_SMB_USER"], share_password = self.user, self.password
        secure = {TOOL_SECRET_PARAMETERS[name]: value for name, value in secrets}
        if env.get("ABR_SMB_SHARE") and share_password:
            secure["SharePassword"] = share_password
        started = time.monotonic()
        output = _ToolOutput(on_progress, [value for _, value in secrets] + ([share_password] if share_password else []))
        with RunspacePool(client.wsman) as pool:
            shell = PowerShell(pool)
            folder = self._bundle_folder(node)
            if folder:
                names = ["SharePassword", *TOOL_SECRET_PARAMETERS.values()]
                shell.add_script(
                    "param([string]$Tool, [string[]]$ToolArgs, [hashtable]$ToolEnv, "
                    + ", ".join(f"[securestring]${name}" for name in names)
                    + ")\n"
                    + f"& {_bundle_script_block(folder, 'run_tool.ps1')} -Tool $Tool -ToolArgs $ToolArgs -ToolEnv $ToolEnv "
                    + " ".join(f"-{name} ${name}" for name in names)
                )
            else:
                shell.add_script(load_script("run_tool.ps1"))
            shell.add_parameters({"Tool": tool, "ToolArgs": tool_args, "ToolEnv": env})
            if secure:
                pool.exchange_keys()
                for name, value in secure.items():
                    shell.add_parameter(name, pool.serialize(value, ObjectMeta("SS")))
            shell.begin_invoke()
            seen = 0
            while shell.state == PSInvocationState.RUNNING:
                if should_cancel is not None and should_cancel():
                    shell.stop()
                    raise ProcessCancelled("cancellation requested")
                if timeout and time.monotonic() - started > timeout:
                    shell.stop()
                    raise HyperVError(f"abr-vhdx-tool on {node} did not finish within {int(timeout)}s")
                shell.poll_invoke(timeout=2)
                for item in shell.output[seen:]:
                    output.feed(str(item))
                seen = len(shell.output)
            for item in shell.output[seen:]:
                output.feed(str(item))
            if shell.had_errors and output.exit_code is None:
                messages = "; ".join(str(record).strip() for record in shell.streams.error)[:400]
                raise HyperVError(f"abr-vhdx-tool could not be started on {node}: {messages}")
        result = vhdx_tool.parse_result(output.lines)
        logger.info(
            "abr-vhdx-tool %s on %s exited %s after %.1fs",
            args[0] if args else "",
            node,
            output.exit_code,
            time.monotonic() - started,
        )
        return {"exit_code": output.exit_code, "result": result, "lines": output.lines}

    def _disk_entry(self, row: Dict[str, Any]) -> Dict[str, Any]:
        chain = [item for item in (row.get("chain") or []) if isinstance(item, dict)]
        top = chain[0] if chain else {}
        base = chain[-1] if chain else {}
        capacity = int(top.get("size") or 0)
        base_type = str(base.get("type") or "")
        return {
            "slot": str(row.get("slot") or ""),
            "disk_label": str(row.get("slot") or ""),
            "controller_type": str(row.get("controller_type") or ""),
            "controller_number": int(row.get("controller_number") or 0),
            "controller_location": int(row.get("controller_location") or 0),
            "supported": bool(row.get("supported")),
            "unsupported_reason": str(row.get("unsupported_reason") or ""),
            "checkpoint_path": str(row.get("path") or ""),
            "datastore_path": str(base.get("path") or row.get("path") or ""),
            "chain_paths": [str(item.get("path") or "") for item in chain],
            "uuid": str(base.get("disk_identifier") or ""),
            "capacity_bytes": capacity,
            "capacity_gb": round(capacity / (1024 ** 3), 2),
            "logical_sector_size": int(top.get("logical_sector_size") or 512),
            "physical_sector_size": int(top.get("physical_sector_size") or 4096),
            "block_size": int(base.get("block_size") or 0),
            "vhd_format": str(base.get("format") or ""),
            "vhd_type": base_type,
            "source_disk_type_code": str(base.get("format") or ""),
            "source_disk_type_name": base_type,
            "source_disk_eager_zeroed": base_type.lower() == "fixed",
        }

    # -- restore side -------------------------------------------------------

    def host_resources(self, node: str) -> Dict[str, Any]:
        """What a restore can use on one node: volumes and Cluster Shared
        Volumes with their free space, virtual switches, the names of the VMs
        already there, and the host's default paths."""
        rows = self._run_script("host_resources.ps1", {}, node)
        raw = rows[0] if rows else {}
        return {
            "computer": str(raw.get("computer") or node),
            "vm_path": str(raw.get("vm_path") or ""),
            "vhd_path": str(raw.get("vhd_path") or ""),
            "volumes": [item for item in raw.get("volumes") or [] if isinstance(item, dict)],
            "switches": [item for item in raw.get("switches") or [] if isinstance(item, dict)],
            "vm_names": [str(item) for item in raw.get("vm_names") or []],
            "versions": [str(item) for item in raw.get("versions") or []],
        }

    def prepare_folders(self, node: str, folders: List[str], files: List[str]) -> List[str]:
        """Create ``folders`` on a node; return those of ``files`` that already exist."""
        return self.create_folders(node, folders, files)[0]

    def create_folders(self, node: str, folders: List[str], files: List[str]) -> tuple[List[str], List[str]]:
        rows = self._run_script("prepare_folders.ps1", {"folders": folders, "files": files}, node)
        row = rows[0] if rows else {}
        return (
            [str(item) for item in (row.get("existing") or [])],
            [str(item) for item in (row.get("created") or [])],
        )

    def remove_empty_folders(self, node: str, folders: List[str]) -> List[str]:
        if not folders:
            return []
        rows = self._run_script("remove_empty_folders.ps1", {"folders": folders}, node)
        return [str(item) for item in ((rows[0] if rows else {}).get("kept") or [])]

    def create_vm_shell(
        self,
        *,
        vm_name: str,
        host_name: str,
        datastore_name: str,
        config_metadata: Dict[str, Any],
        disk_layout: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        """Create the VM a restore attaches its disks to.

        ``datastore_name`` is the folder the VM's configuration goes under,
        ``config_metadata`` the backup's ``vm_config`` with each network
        adapter's ``target_switch`` filled in (empty leaves it unconnected).
        ``disk_layout`` only decides how many SCSI controllers to add; the
        disks are attached afterwards with attach_existing_disk.
        """
        node = self.resolve_node(host_name)
        scsi_needed = 0
        for disk in disk_layout or []:
            if str(disk.get("controller_type") or "").upper() == "SCSI":
                scsi_needed = max(scsi_needed, int(disk.get("controller_number") or 0) + 1)
        config = config_metadata
        args = {
            "name": vm_name,
            "path": datastore_name,
            "generation": int(config.get("generation") or (2 if config.get("firmware") == "efi" else 1)),
            "num_cpu": int(config.get("num_cpu") or 1),
            "memory_mb": int(config.get("memory_mb") or 1024),
            "dynamic_memory": bool(config.get("dynamic_memory")),
            "memory_minimum_mb": int(config.get("memory_minimum_mb") or 0),
            "memory_maximum_mb": int(config.get("memory_maximum_mb") or 0),
            "memory_buffer": int(config.get("memory_buffer") or 0),
            "secure_boot_enabled": config.get("secure_boot_enabled"),
            "secure_boot_template": str(config.get("secure_boot_template") or ""),
            "tpm_enabled": bool(config.get("tpm_enabled")),
            "notes": str(config.get("notes") or ""),
            "scsi_controllers": max(scsi_needed, len(config.get("scsi_controllers") or [])),
            "network_adapters": [
                {
                    "name": str(adapter.get("label") or "Network Adapter"),
                    "switch": str(adapter.get("target_switch") or ""),
                    "vlan_id": int(adapter.get("vlan_id") or 0) if str(adapter.get("vlan_mode") or "") == "Access" else 0,
                }
                for adapter in config.get("network_adapters") or []
                if isinstance(adapter, dict) and not adapter.get("legacy")
            ],
        }
        rows = self._run_script("create_vm.ps1", args, node)
        created = dict(rows[0]) if rows else {}
        vm_id = str(created.get("vm_id") or "").lower()
        if not vm_id:
            raise HyperVError(f"Hyper-V did not report the VM {vm_name} it was asked to create on {node}")
        self._vm_nodes[vm_id] = node
        logger.info("Created VM %s (%s) on %s under %s", vm_name, vm_id, node, datastore_name)
        return {"vm_id": vm_id, "name": vm_name, "node": node, "path": str(created.get("path") or "")}

    def attach_existing_disk(
        self,
        vm_name: str,
        datastore_path: str,
        disk_label: str | None = None,
    ) -> Dict[str, Any]:
        """Attach a VHDX to a VM at a controller slot (``disk_label``, like
        "SCSI0:1"); a drive already in that slot is pointed at the file."""
        match = re.match(r"^([A-Za-z]+)(\d+):(\d+)$", str(disk_label or "SCSI0:0"))
        if not match:
            raise HyperVError(f"{disk_label!r} is not a controller slot such as SCSI0:1")
        rows = self._run_vm_script(
            vm_name,
            "attach_disk.ps1",
            {
                "path": datastore_path,
                "controller_type": match.group(1).upper(),
                "controller_number": int(match.group(2)),
                "controller_location": int(match.group(3)),
            },
        )
        attached = dict(rows[0]) if rows else {}
        logger.info("Attached %s to VM %s at %s", datastore_path, vm_name, disk_label)
        return attached

    def set_boot_disk(self, vm_name: str, slot: str, boot_order: List[Dict[str, Any]] | None = None) -> None:
        self._run_vm_script(vm_name, "set_boot.ps1", {"slot": slot, "boot_order": boot_order or []})

    def set_vm_hardware(self, vm_name: str, config: Dict[str, Any]) -> None:
        self._run_vm_script(
            vm_name,
            "set_hardware.ps1",
            {
                "num_cpu": int(config.get("num_cpu") or 0),
                "memory_mb": int(config.get("memory_mb") or 0),
                "dynamic_memory": bool(config.get("dynamic_memory")),
                "memory_minimum_mb": int(config.get("memory_minimum_mb") or 0),
                "memory_maximum_mb": int(config.get("memory_maximum_mb") or 0),
            },
        )

    def connect_vm_adapters(self, vm_name: str, adapters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows = self._run_vm_script(vm_name, "connect_adapters.ps1", {"adapters": adapters})
        return [dict(item) for item in rows]

    def replace_vm_disks(self, vm_name: str, disks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Point a VM's drives at other files, slot by slot; returns each
        slot's previous file so the swap can be undone."""
        rows = self._run_vm_script(vm_name, "replace_disks.ps1", {"disks": disks})
        return [dict(item) for item in rows]

    def power_on_vm(self, vm_name: str, vm_moref: str | None = None) -> Dict[str, Any]:
        rows = self._run_vm_script(vm_moref or vm_name, "vm_power.ps1", {"action": "start"})
        return dict(rows[0]) if rows else {}

    def power_off_vm(self, vm_name: str, vm_moref: str | None = None) -> Dict[str, Any]:
        rows = self._run_vm_script(vm_moref or vm_name, "vm_power.ps1", {"action": "turnoff"})
        return dict(rows[0]) if rows else {}

    def destroy_vm(self, vm_name: str, vm_moref: str | None = None) -> None:
        self._run_vm_script(vm_moref or vm_name, "remove_vm.ps1", {})
        logger.info("Removed VM %s", vm_moref or vm_name)

    def convert_disk(self, node: str, source: str, destination: str, disk_type: str) -> None:
        self._run_script(
            "convert_disk.ps1", {"source": source, "destination": destination, "type": disk_type}, node
        )

    def remove_files(self, node: str, paths: List[str]) -> List[str]:
        """Delete files on a node; returns the ones that could not be removed."""
        if not paths:
            return []
        rows = self._run_script("remove_files.ps1", {"paths": paths}, node)
        return [str(item) for item in ((rows[0] if rows else {}).get("failed") or [])]

    def backup(
        self,
        task: dict[str, Any],
        progress_callback: ProgressCallback = None,
    ) -> dict[str, Any]:
        from .hyperv_backup import run_hyperv_backup

        return run_hyperv_backup(task, progress_callback=progress_callback)

    def restore(
        self,
        task: dict[str, Any],
        progress_callback: ProgressCallback = None,
    ) -> dict[str, Any]:
        from .hyperv_restore import run_hyperv_restore

        return run_hyperv_restore(task, progress_callback=progress_callback)

class _ToolOutput:
    """Sorts what abr-vhdx-tool prints: ``NN%`` progress lines, the exit code
    the runner script appends, and everything else."""

    def __init__(self, on_progress: Any = None, secrets: List[str] | None = None) -> None:
        self.on_progress = on_progress
        self.lines: List[str] = []
        self.exit_code: int | None = None
        self._secrets = [secret for secret in secrets or [] if secret]

    def feed(self, text: str) -> None:
        if self._secrets:
            text = mask_secret_values(text, self._secrets)
        for line in text.splitlines() or [""]:
            if line.startswith("ABR_TOOL_EXIT="):
                try:
                    self.exit_code = int(line.split("=", 1)[1].strip())
                except ValueError:
                    self.exit_code = -1
                continue
            percent = vhdx_tool.progress_percent(line)
            if percent is not None:
                if self.on_progress is not None:
                    self.on_progress(percent)
                continue
            self.lines.append(line)


_CONTROLLER_ORDER = {"ide": 0, "scsi": 1}


def _slot_sort_key(slot: str) -> tuple[int, int, int, str]:
    """IDE before SCSI, then controller number and location: the order a
    VM's disks keep from one backup to the next."""
    match = re.match(r"^([A-Za-z]+)(\d+):(\d+)$", str(slot or ""))
    if not match:
        return (9, 0, 0, str(slot))
    return (_CONTROLLER_ORDER.get(match.group(1).lower(), 5), int(match.group(2)), int(match.group(3)), "")
