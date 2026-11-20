# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""A Proxmox VE cluster, or a single PVE node, as an AltaStor inventory.

abr-core reaches the cluster in two ways, both set up once from the root password (see setup.py):

- the REST API with the token of the PVE user abr@pve, for the inventory and for backup and restore tasks;
- SSH as root with the inventory's own key, only for run files, the package and setup.

The inventory lists the cluster's nodes, the storages each node can put VM disks on, the bridges VMs connect to, and
every VM with its disks and network adapters, read from the VMs' configurations. VM IDs are unique in a cluster, so a
VM's ID is its external ID, and a disk is named by its VM and its drive key, such as 9002:scsi0.
"""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping
from urllib.parse import quote

from ..hypervisor import Hypervisor, ProgressCallback
from .pve_api import DEFAULT_PORT, PveApi, PveApiError
from .ssh import DEFAULT_PORT as SSH_PORT
from .ssh import NodeShell

logger = logging.getLogger(__name__)

PLATFORM = "proxmox-ve"
CONFIG_READERS = 8

DISK_KEY = re.compile(r"^(?:(?:ide|sata|scsi|virtio)\d+|efidisk0|tpmstate0)$")
NET_KEY = re.compile(r"^net\d+$")
NIC_MODELS = (
    "virtio",
    "e1000",
    "e1000e",
    "rtl8139",
    "vmxnet3",
    "i82551",
    "i82557b",
    "i82559er",
    "ne2k_isa",
    "ne2k_pci",
    "pcnet",
)
SIZE = re.compile(r"^(\d+(?:\.\d+)?)([KMGT]?)$")
SIZE_UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}

GUEST_OS = {
    "l26": "Linux 2.6 - 6.x kernel",
    "l24": "Linux 2.4 kernel",
    "win11": "Microsoft Windows 11/2022/2025",
    "win10": "Microsoft Windows 10/2016/2019",
    "win8": "Microsoft Windows 8.x/2012/2012r2",
    "win7": "Microsoft Windows 7/2008r2",
    "w2k8": "Microsoft Windows Vista/2008",
    "wvista": "Microsoft Windows Vista",
    "w2k3": "Microsoft Windows 2003",
    "wxp": "Microsoft Windows XP",
    "w2k": "Microsoft Windows 2000",
    "solaris": "Solaris kernel",
    "other": "Other",
}


class PveError(RuntimeError):
    pass


# ---- VM configuration ------------------------------------------------------------------------------------------------


# PVE's dns-name format, which a VM's name must follow.
VM_NAME = re.compile(r"^(?=.{1,63}$)[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$")


def vm_name_problem(name: str) -> str | None:
    """Why PVE would not take a VM name, or None when it would."""
    if VM_NAME.match(name):
        return None
    return (
        f"{name!r} cannot name a Proxmox VE VM: PVE takes letters, digits and hyphens, in parts separated by dots, up "
        "to 63 characters"
    )


def parse_property_string(value: str, default_key: str = "") -> dict[str, str]:
    """A PVE property string such as "local-lvm:vm-9002-disk-0,size=32G".

    A value without a key belongs to default_key.
    """
    properties: dict[str, str] = {}
    for part in str(value or "").split(","):
        if not part:
            continue
        key, separator, item = part.partition("=")
        if separator:
            properties[key.strip()] = item.strip()
        elif default_key:
            properties[default_key] = part.strip()
    return properties


def parse_size(value: str | None) -> int:
    """A PVE size, such as 32G or 528K, in bytes; 0 for none."""
    match = SIZE.match(str(value or "").strip())
    if not match:
        return 0
    return int(float(match.group(1)) * SIZE_UNITS[match.group(2)])


def volume_storage(volume: str) -> str:
    """The storage a volume ID such as local-lvm:vm-9002-disk-0 is on; empty for a path such as /dev/sdb."""
    storage, separator, _name = str(volume or "").partition(":")
    return storage if separator and not storage.startswith("/") else ""


def volume_format(volume: str, properties: Mapping[str, str]) -> str:
    if properties.get("format"):
        return str(properties["format"])
    name = str(volume or "").rsplit("/", 1)[-1]
    for suffix in ("qcow2", "raw", "vmdk"):
        if name.endswith(f".{suffix}"):
            return suffix
    return "raw"


def vm_disks(vm_id: str, config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The VM's disks as vzdump sees them: CD-ROM drives and empty drives are not disks."""
    disks = []
    for key in sorted(config, key=_drive_order):
        if not DISK_KEY.match(key):
            continue
        properties = parse_property_string(str(config[key]), "file")
        volume = properties.get("file", "")
        if properties.get("media") == "cdrom" or not volume or volume == "none":
            continue
        size = parse_size(properties.get("size"))
        disks.append(
            {
                "external_id": f"{vm_id}:{key}",
                "name": key,
                "vm_id": vm_id,
                "drive": key,
                "drive_id": f"drive-{key}",
                "capacity_bytes": size,
                "capacity_gb": round(size / (1 << 30), 2),
                "datastore_path": volume,
                "storage": volume_storage(volume),
                "format": volume_format(volume, properties),
                # vzdump leaves out a disk marked backup=0.
                "backup": properties.get("backup", "1") != "0",
                "uuid": "",
                "controller": key,
            }
        )
    return disks


def vm_network_adapters(vm_id: str, config: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapters = []
    for key in sorted((key for key in config if NET_KEY.match(key)), key=lambda key: int(key[3:])):
        properties = parse_property_string(str(config[key]))
        model = next((name for name in NIC_MODELS if name in properties), "")
        adapters.append(
            {
                "external_id": f"{vm_id}:{key}",
                "name": key,
                "vm_id": vm_id,
                "model": model,
                "mac": properties.get(model, properties.get("macaddr", "")).upper(),
                "network": properties.get("bridge", ""),
                "vlan": int(properties["tag"]) if properties.get("tag", "").isdigit() else None,
                "connected": properties.get("link_down", "0") != "1",
                "firewall": properties.get("firewall", "0") == "1",
            }
        )
    return adapters


def restore_metadata(vm_id: str, name: str, node: str, config: Mapping[str, Any]) -> dict[str, Any]:
    """What a restore offers of a VM, in the terms the restore wizard uses for every platform, with its PVE config."""
    os_type = str(config.get("ostype") or "")
    memory = parse_property_string(str(config.get("memory") or ""), "current").get("current", "")
    cores = int(config.get("cores") or 1) * int(config.get("sockets") or 1)
    return {
        "vmid": str(vm_id),
        "name": name,
        "node": node,
        "source_host": node,
        "firmware": "efi" if config.get("bios") == "ovmf" else "bios",
        "num_cpu": int(config.get("vcpus") or cores),
        "memory_mb": int(memory) if memory.isdigit() else 512,
        "guest_os": GUEST_OS.get(os_type, os_type),
        "config": dict(config),
        "network_adapters": [
            {
                "label": adapter["name"],
                "network": adapter["network"],
                "mac_address": adapter["mac"],
                "connected": adapter["connected"],
                "model": adapter["model"],
                "vlan": adapter["vlan"],
            }
            for adapter in vm_network_adapters(str(vm_id), config)
        ],
    }


def _drive_order(key: str) -> tuple[int, int, str]:
    match = re.match(r"^(ide|sata|scsi|virtio|efidisk|tpmstate)(\d+)$", key)
    if not match:
        return (9, 0, key)
    order = ("scsi", "virtio", "sata", "ide", "efidisk", "tpmstate").index(match.group(1))
    return (order, int(match.group(2)), key)


# ---- access --------------------------------------------------------------------------------------------------------


@dataclass
class PveAccess:
    """What abr-core keeps to reach a cluster: the API's token and pinned certificates, and SSH's key and pinned host
    keys, per node."""

    host: str
    port: int = DEFAULT_PORT
    token_id: str = ""
    token_secret: str = ""
    certificates: list[str] = field(default_factory=list)
    ssh_key: str = ""
    ssh_port: int = SSH_PORT
    nodes: list[dict[str, Any]] = field(default_factory=list)
    storage: str = "abr"
    cluster_name: str = ""

    @property
    def api_addresses(self) -> list[str]:
        """The address the admin gave first, then every node's own address."""
        addresses = [self.host]
        for node in self.nodes:
            address = str(node.get("address") or "")
            if address and address not in addresses:
                addresses.append(address)
        return addresses

    def node(self, name: str) -> dict[str, Any]:
        for node in self.nodes:
            if str(node.get("name") or "").lower() == str(name or "").lower():
                return node
        raise PveError(
            f"Node {name} is not one AltaStor has set up in this cluster. If it joined the cluster since, run "
            "inventory rescan; a node AltaStor has not seen yet needs inventory edit with the root password"
        )


def access_from_record(record: Mapping[str, Any]) -> PveAccess:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    pve = metadata.get("pve") if isinstance(metadata.get("pve"), dict) else {}
    access = PveAccess(
        host=str(metadata.get("host") or ""),
        port=int(metadata.get("port") or DEFAULT_PORT),
        token_id=str(pve.get("token_id") or ""),
        token_secret=str(metadata.get("token") or ""),
        certificates=[str(item) for item in pve.get("certificates") or []],
        ssh_key=str(pve.get("ssh_key") or ""),
        ssh_port=int(pve.get("ssh_port") or SSH_PORT),
        nodes=[dict(item) for item in pve.get("nodes") or [] if isinstance(item, dict)],
        storage=str(pve.get("storage") or "abr"),
        cluster_name=str(metadata.get("cluster_name") or ""),
    )
    if not access.host or not access.token_id or not access.token_secret or not access.ssh_key:
        raise PveError(
            "This Proxmox VE inventory has not been set up: AltaStor has no API token or SSH key for it. Run "
            "inventory edit with the root password to set it up again"
        )
    return access


# ---- the cluster ---------------------------------------------------------------------------------------------------


class PveNode(Hypervisor):
    PLATFORM = PLATFORM

    def __init__(self, access: PveAccess, api: PveApi | None = None) -> None:
        self.access = access
        self._api: PveApi | None = api
        self._shells: dict[str, NodeShell] = {}
        self._lock = threading.Lock()
        self._status: list[dict[str, Any]] | None = None

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "PveNode":
        return cls(access_from_record(record))

    # ---- connections ------------------------------------------------------------------------------------------------

    def connect(self) -> None:
        if self._api is None:
            self._api = PveApi(
                self.access.api_addresses,
                port=self.access.port,
                token_id=self.access.token_id,
                token_secret=self.access.token_secret,
                trusted_certificates=self.access.certificates,
            )

    def disconnect(self) -> None:
        with self._lock:
            shells, self._shells = list(self._shells.values()), {}
        for shell in shells:
            shell.close()
        if self._api is not None:
            self._api.close()
            self._api = None

    @property
    def api(self) -> PveApi:
        self.connect()
        assert self._api is not None
        return self._api

    def shell(self, node: str) -> NodeShell:
        """SSH to a node as root with the inventory's key; the node must present a host key pinned for it."""
        entry = self.access.node(node)
        name = str(entry.get("name"))
        with self._lock:
            shell = self._shells.get(name)
            if shell is None:
                shell = NodeShell(
                    str(entry.get("address") or name),
                    port=self.access.ssh_port,
                    private_key=self.access.ssh_key,
                    host_keys=[str(key) for key in entry.get("host_keys") or []],
                )
                self._shells[name] = shell
        return shell

    def cluster_status(self, refresh: bool = False) -> list[dict[str, Any]]:
        if self._status is None or refresh:
            self._status = [dict(item) for item in self.api.get("/cluster/status") or []]
        return self._status

    @property
    def cluster_name(self) -> str | None:
        for item in self.cluster_status():
            if item.get("type") == "cluster":
                return str(item.get("name") or "") or None
        return None

    def node_names(self) -> list[str]:
        return sorted(str(item.get("name")) for item in self.cluster_status() if item.get("type") == "node")

    def get_thumbprint(self) -> str:
        return ""

    def get_service_type(self) -> str:
        return "cluster" if self.cluster_name else "node"

    def identity(self) -> Dict[str, Any]:
        nodes = self.node_names()
        return {"cluster": self.cluster_name, "computer": nodes[0] if nodes else "", "nodes": nodes}

    # ---- inventory --------------------------------------------------------------------------------------------------

    def _resources(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.api.get("/cluster/resources") or []]

    def list_hosts(self) -> List[Dict[str, Any]]:
        resources = self._resources()
        online = {
            str(item.get("name")): bool(item.get("online"))
            for item in self.cluster_status()
            if item.get("type") == "node"
        }
        addresses = {
            str(item.get("name")): str(item.get("ip") or "")
            for item in self.cluster_status()
            if item.get("type") == "node"
        }
        hosts = []
        for item in sorted(
            (item for item in resources if item.get("type") == "node"), key=lambda item: str(item.get("node"))
        ):
            name = str(item.get("node") or "")
            available = online.get(name, item.get("status") == "online")
            storages = {
                str(store.get("storage")): {
                    "capacity_bytes": int(store.get("maxdisk") or 0),
                    "free_bytes": max(int(store.get("maxdisk") or 0) - int(store.get("disk") or 0), 0),
                    "shared": bool(store.get("shared")),
                    "type": str(store.get("plugintype") or ""),
                    "content": sorted(str(store.get("content") or "").split(",")) if store.get("content") else [],
                    "active": store.get("status") == "available",
                }
                for store in resources
                if store.get("type") == "storage" and store.get("node") == name
            }
            images = sorted(store for store, info in storages.items() if "images" in info["content"])
            hosts.append(
                {
                    "name": name,
                    "dns_name": "",
                    "address": addresses.get(name, ""),
                    "cluster": self.cluster_name,
                    "datacenter": None,
                    "version": self._node_version(name) if available else "",
                    "datastores": images,
                    "datastore_capacity": {store: storages[store] for store in images},
                    "backup_storage_active": bool(storages.get(self.access.storage, {}).get("active")),
                    "networks": self.bridges(name) if available else [],
                    "cpu_count": int(item.get("maxcpu") or 0),
                    "memory_bytes": int(item.get("maxmem") or 0),
                    "status": "connected" if available else "unavailable",
                }
            )
        return hosts

    def _node_version(self, node: str) -> str:
        try:
            return str((self.api.get(f"/nodes/{quote(node, safe='')}/version") or {}).get("version") or "")
        except PveApiError as exc:
            logger.warning("Could not read the PVE version of node %s: %s", node, exc)
            return ""

    def bridges(self, node: str) -> list[str]:
        try:
            rows = self.api.get(f"/nodes/{quote(node, safe='')}/network", type="any_bridge") or []
        except PveApiError as exc:
            logger.warning("Could not list the bridges of node %s: %s", node, exc)
            return []
        return sorted(str(row.get("iface")) for row in rows if row.get("iface"))

    def image_storages(self, node: str) -> dict[str, dict[str, Any]]:
        """The enabled storages of a node that hold VM disks, by name, with whether they are active and their room."""
        rows = self.api.get(f"/nodes/{quote(node, safe='')}/storage", content="images", enabled=1) or []
        return {str(row.get("storage")): dict(row) for row in rows if row.get("storage")}

    def next_vm_id(self) -> str:
        return str(self.api.get("/cluster/nextid"))

    def vm_state(self, node: str, vm_id: str) -> str:
        """running or stopped as the VM's node tells it now; the cluster's resource list lags by several seconds."""
        current = self.api.get(f"/nodes/{quote(node, safe='')}/qemu/{quote(str(vm_id), safe='')}/status/current")
        return str((current or {}).get("status") or "")

    def firewall_digest(self, node: str, vm_id: str) -> str:
        """The digest of the VM's firewall settings, which change without changing its configuration's digest."""
        options = self.api.get(f"/nodes/{quote(node, safe='')}/qemu/{quote(str(vm_id), safe='')}/firewall/options")
        return str((options or {}).get("digest") or "")

    def vm_config(self, node: str, vm_id: str) -> dict[str, Any]:
        return dict(self.api.get(f"/nodes/{quote(node, safe='')}/qemu/{quote(str(vm_id), safe='')}/config") or {})

    def _vm_record(self, item: Mapping[str, Any], config: Mapping[str, Any] | None) -> dict[str, Any]:
        vm_id = str(item.get("vmid") or "")
        config = config or {}
        os_type = str(config.get("ostype") or "")
        return {
            "vm_id": vm_id,
            "name": str(item.get("name") or config.get("name") or f"VM {vm_id}"),
            "power_state": str(item.get("status") or ""),
            "num_cpu": int(item.get("maxcpu") or 0),
            "memory_mb": int(item.get("maxmem") or 0) // (1 << 20),
            "guest_os": GUEST_OS.get(os_type, os_type),
            "os_type": os_type,
            "ip_address": None,
            "firmware": "efi" if config.get("bios") == "ovmf" else "bios",
            "machine": str(config.get("machine") or ""),
            "host": str(item.get("node") or ""),
            "template": bool(item.get("template")),
            "lock": str(item.get("lock") or config.get("lock") or ""),
            "ha_state": str(item.get("hastate") or ""),
            "tags": [tag for tag in re.split(r"[;, ]+", str(item.get("tags") or "")) if tag],
            "config_digest": str(config.get("digest") or ""),
        }

    def get_inventory(self) -> Dict[str, List[Dict[str, Any]]]:
        vms = sorted(
            (item for item in self._resources() if item.get("type") == "qemu"),
            key=lambda item: int(item.get("vmid") or 0),
        )

        def read(item: Mapping[str, Any]) -> dict[str, Any] | None:
            try:
                return self.vm_config(str(item.get("node")), str(item.get("vmid")))
            except PveApiError as exc:
                # A VM on a node that is down keeps its configuration in /etc/pve, but the API reads it on that node.
                logger.warning(
                    "Could not read the configuration of VM %s on node %s: %s", item.get("vmid"), item.get("node"), exc
                )
                return None

        with ThreadPoolExecutor(max_workers=CONFIG_READERS) as pool:
            configs = list(pool.map(read, vms))
        records: Dict[str, List[Dict[str, Any]]] = {"virtual_machines": [], "disks": [], "network_adapters": []}
        for item, config in zip(vms, configs):
            record = self._vm_record(item, config)
            record["config_read"] = config is not None
            records["virtual_machines"].append(record)
            if config is not None:
                records["disks"].extend(vm_disks(record["vm_id"], config))
                records["network_adapters"].extend(vm_network_adapters(record["vm_id"], config))
        return records

    def get_all_vms(self) -> List[Dict[str, Any]]:
        return self.get_inventory()["virtual_machines"]

    def locate_vm(self, vm_id: str) -> dict[str, Any]:
        """Where a VM runs now, and its state: VMs move between nodes."""
        for item in self.api.get("/cluster/resources", type="vm") or []:
            if item.get("type") == "qemu" and str(item.get("vmid")) == str(vm_id):
                return dict(item)
        raise PveError(f"VM {vm_id} is not in the PVE cluster any more")

    def get_vm_disks(self, vm_name: str) -> List[Dict[str, Any]]:
        located = self.locate_vm(vm_name)
        return vm_disks(str(located.get("vmid")), self.vm_config(str(located.get("node")), str(located.get("vmid"))))

    def get_vm_restore_metadata(self, vm_name: str) -> Dict[str, Any]:
        located = self.locate_vm(vm_name)
        vm_id, node = str(located.get("vmid")), str(located.get("node") or "")
        config = self.vm_config(node, vm_id)
        metadata = restore_metadata(vm_id, str(located.get("name") or config.get("name") or ""), node, config)
        return {**metadata, "vm_id": vm_id, "disks": vm_disks(vm_id, config)}

    # ---- backup and restore -----------------------------------------------------------------------------------------

    def backup(self, task: dict[str, Any], progress_callback: ProgressCallback = None) -> dict[str, Any]:
        from .pve_backup import run_pve_backup

        return run_pve_backup(task, progress_callback=progress_callback)

    def restore(self, task: dict[str, Any], progress_callback: ProgressCallback = None) -> dict[str, Any]:
        from .pve_restore import run_pve_restore

        return run_pve_restore(task, progress_callback=progress_callback)
