# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from typing import Any
from .helpers import deterministic_seed


def generate_discovery(hypervisor_id: str, hypervisor_name: str) -> list[dict[str, Any]]:
    return generate_inventory_discovery("generic", hypervisor_id, hypervisor_name)


def generate_inventory_discovery(
    inventory_type: str,
    hypervisor_id: str,
    hypervisor_name: str,
) -> list[dict[str, Any]]:
    seed = deterministic_seed(hypervisor_id)
    vm_count = (seed % 3) + 2
    resources: list[dict[str, Any]] = []
    host_count = 1 + (seed % 2)
    cluster_count = 1

    resources.append(
        {
            "resource_type": "hypervisor",
            "external_id": hypervisor_id,
            "name": hypervisor_name,
            "payload": {
                "inventory": inventory_type,
                "poll_metadata": {"seed": seed, "source": "poll_resources"},
            },
        }
    )

    for cluster_index in range(1, cluster_count + 1):
        cluster_name = f"{hypervisor_name}-cluster-{cluster_index}"
        resources.append(
            {
                "resource_type": "cluster",
                "external_id": f"{hypervisor_id}-cluster-{cluster_index}",
                "name": cluster_name,
                "payload": {
                    "inventory": inventory_type,
                },
            }
        )

    for host_index in range(1, host_count + 1):
        datastores = [f"datastore{host_index}", f"datastore{host_index + 1}"]
        # Every host carries the port groups its VMs' NICs use, plus one switch
        # of its own — so restore targets differ per host the way they do on real
        # hosts with standard (per-host) virtual switches.
        networks = ["prod-net", "backup-net", f"host{host_index}-local"]
        resources.append(
            {
                "resource_type": "host",
                "external_id": f"{hypervisor_id}-host-{host_index}",
                "name": f"{hypervisor_name}-host-{host_index}",
                "payload": {
                    "cluster_external_id": f"{hypervisor_id}-cluster-1",
                    "inventory": inventory_type,
                    "datastores": datastores,
                    "networks": networks,
                },
            }
        )

    for i in range(1, vm_count + 1):
        vm_id = f"{hypervisor_id}-vm-{i}"
        resources.append(
            {
                "resource_type": "virtual_machine",
                "external_id": vm_id,
                "name": f"vm-{i}",
                "payload": {
                    "vcpus": 2 + (i % 4),
                    "memory_gb": 4 + (i * 2),
                    "state": "running" if i % 2 == 0 else "stopped",
                    "related_metadata": {"discovered_from": hypervisor_name},
                    "host_external_id": f"{hypervisor_id}-host-{((i - 1) % host_count) + 1}",
                    "cluster_external_id": f"{hypervisor_id}-cluster-1",
                },
            }
        )

        disk_count = 1 + (i % 2)
        for d in range(1, disk_count + 1):
            resources.append(
                {
                    "resource_type": "disk",
                    "external_id": f"{vm_id}-disk-{d}",
                    "name": f"{vm_id}-disk-{d}",
                    "payload": {
                        "vm_id": vm_id,
                        "size_gb": 40 * d,
                        "bus": "scsi",
                        "related_metadata": {"attached_to_vm": vm_id},
                    },
                }
            )

        nic_count = 1 + ((seed + i) % 2)
        for n in range(1, nic_count + 1):
            resources.append(
                {
                    "resource_type": "network_adapter",
                    "external_id": f"{vm_id}-nic-{n}",
                    "name": f"{vm_id}-nic-{n}",
                    "payload": {
                        "vm_id": vm_id,
                        "network": "prod-net" if n == 1 else "backup-net",
                        "mac": f"02:00:00:{i:02x}:{n:02x}:{(seed % 255):02x}",
                        "related_metadata": {"adapter_index": n},
                    },
                }
            )

    return resources


def build_vcenter_resources(hv: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = hv.get("metadata", {})
    credentials = metadata.get("credentials", {})
    host = metadata.get("host")
    port = int(metadata.get("port", 443))
    verify_ssl = bool(metadata.get("verify_ssl", False))
    username = credentials.get("username")
    password = credentials.get("password")

    if not host or not username or not password:
        raise ValueError("Saved vCenter registration is missing host or credentials")

    try:
        from ..plugins.vcenter.vcenter import VCenterNode
    except ModuleNotFoundError as exc:
        if exc.name in {"pyVim", "pyVmomi"}:
            raise RuntimeError(
                "vCenter support requires pyVmomi/pyVim to be installed"
            ) from exc
        raise

    try:
        with VCenterNode(host, username, password, port=port, verify_ssl=verify_ssl) as node:
            inventory = node.get_inventory()
            hosts = node.list_hosts()
    except Exception as exc:
        raise RuntimeError(f"Unable to query vCenter inventory from {host}:{port}: {exc}") from exc

    resources: list[dict[str, Any]] = [
        {
            "resource_type": "hypervisor",
            "external_id": hv["id"],
            "name": hv["name"],
            "payload": {
                "inventory": "vcenter",
                "host": host,
                "endpoint": hv["endpoint"],
            },
        }
    ]

    # A VM's cluster is the cluster of the host it runs on, so carry it across
    # from the host list rather than re-querying vCenter per VM.
    cluster_by_host = {
        str(host_item.get("name")): host_item.get("cluster") for host_item in hosts
    }

    for host_item in hosts:
        cluster_name = host_item.get("cluster")
        resources.append(
            {
                "resource_type": "host",
                "external_id": str(host_item.get("name")),
                "name": str(host_item.get("name")),
                "payload": {
                    "datacenter": host_item.get("datacenter"),
                    "cluster_name": cluster_name,
                    "cluster_external_id": cluster_name,
                    # Display-only: hosts added by address are named after their
                    # IP, so show the friendly DNS name where vCenter knows one.
                    "dns_name": host_item.get("dns_name"),
                    "datastores": host_item.get("datastores", []),
                    # Capacity/free space per datastore name, straight from
                    # vCenter (VCenterNode.list_hosts) — restore-target
                    # capacity checks read this back via _build_restore_target_datastores.
                    "datastore_capacity": host_item.get("datastore_capacity", {}),
                    # Port groups this host's virtual switches expose — what a
                    # restore's network mapping is validated and scoped against.
                    "networks": host_item.get("networks", []),
                },
            }
        )

    for vm in inventory["virtual_machines"]:
        vm_id = vm["vm_id"]
        resources.append(
            {
                "resource_type": "virtual_machine",
                "external_id": vm_id,
                "name": vm["name"],
                "payload": {
                    "power_state": vm["power_state"],
                    "num_cpu": vm["num_cpu"],
                    "memory_mb": vm["memory_mb"],
                    "guest_os": vm["guest_os"],
                    "firmware": vm.get("firmware"),
                    # The inventory tree nests VMs under their host (and takes the
                    # datacenter from that host), so this linkage is what makes a
                    # multi-host vCenter render instead of showing an empty tree.
                    "host_name": vm.get("host"),
                    # Cluster of the VM's host — groups the tree by cluster the
                    # way vSphere does. None for a standalone host.
                    "cluster_name": cluster_by_host.get(str(vm.get("host") or "")),
                    "cluster_external_id": cluster_by_host.get(str(vm.get("host") or "")),
                },
            }
        )

    for disk in inventory["disks"]:
        resources.append(
            {
                "resource_type": "disk",
                "external_id": disk["external_id"],
                "name": disk["name"],
                "payload": {
                    "vm_id": disk["vm_id"],
                    "capacity_gb": disk["capacity_gb"],
                    "datastore_path": disk["datastore_path"],
                    "uuid": disk["uuid"],
                    "source_disk_type_code": disk.get("source_disk_type_code"),
                    "source_disk_type_name": disk.get("source_disk_type_name"),
                },
            }
        )

    for nic in inventory["network_adapters"]:
        resources.append(
            {
                "resource_type": "network_adapter",
                "external_id": nic["external_id"],
                "name": nic["name"],
                "payload": {
                    "vm_id": nic["vm_id"],
                    "mac": nic["mac"],
                    "network": nic["network"],
                    "connected": nic["connected"],
                },
            }
        )

    return resources
