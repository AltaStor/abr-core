# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Self
from unittest.mock import patch

from pyVmomi import vmodl

from python.srv.plugins.vcenter.vcenter import _reported_ip_address
from python.srv.services.discovery import build_vcenter_resources

INVENTORY_RECORD = {
    "id": "vc-1",
    "name": "vCenter-test",
    "kind": "vcenter",
    "endpoint": "vc.example.test",
    "metadata": {"host": "vc.example.test", "credentials": {"username": "user", "password": "placeholder-password"}},
}


class _Vm:
    def __init__(self, nics: list[list[str]] | None = None) -> None:
        self.guest_reads = 0
        self._nics = nics or []

    @property
    def guest(self) -> SimpleNamespace:
        self.guest_reads += 1
        return SimpleNamespace(net=[SimpleNamespace(ipAddress=addresses) for addresses in self._nics])


def _summary(ip_address: str | None, tools: str = "guestToolsRunning") -> SimpleNamespace:
    return SimpleNamespace(guest=SimpleNamespace(ipAddress=ip_address, toolsRunningStatus=tools))


class ReportedIpAddressTests(unittest.TestCase):
    def test_the_address_vcenter_reports_for_the_guest_is_taken(self) -> None:
        vm = _Vm()

        self.assertEqual(_reported_ip_address(vm, _summary("192.0.2.172")), "192.0.2.172")
        self.assertEqual(vm.guest_reads, 0)

    def test_without_a_usable_primary_address_the_adapters_are_asked(self) -> None:
        for primary in (None, "169.254.3.4", "fe80::1"):
            with self.subTest(primary=primary):
                vm = _Vm([["fe80::250:56ff:fe8a:1"], ["2001:db8::9", "192.0.2.50"]])

                self.assertEqual(_reported_ip_address(vm, _summary(primary)), "192.0.2.50")

    def test_a_vm_without_running_tools_reports_nothing_and_costs_no_extra_call(self) -> None:
        for tools in ("guestToolsNotRunning", None):
            with self.subTest(tools=tools):
                vm = _Vm([["192.0.2.50"]])

                self.assertEqual(_reported_ip_address(vm, _summary(None, tools)), "")
                self.assertEqual(vm.guest_reads, 0)

    def test_an_unreadable_guest_reports_nothing(self) -> None:
        class _Broken:
            @property
            def guest(self):
                raise vmodl.fault.ManagedObjectNotFound()

        self.assertEqual(_reported_ip_address(_Broken(), _summary(None)), "")


class _FakeNode:
    def __init__(self, *args, **kwargs) -> None:
        self.vms: list[dict] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def get_inventory(self) -> dict:
        return {"virtual_machines": self.vms, "disks": [], "network_adapters": []}

    def list_hosts(self) -> list:
        return []


class InventoryPayloadTests(unittest.TestCase):
    def _payload(self, ip_address: str) -> dict:
        node = _FakeNode()
        node.vms = [
            {"vm_id": "vm-1", "name": "web-01", "power_state": "poweredOn", "num_cpu": 1, "memory_mb": 1024,
             "guest_os": "Debian GNU/Linux 12 (64-bit)", "ip_address": ip_address, "firmware": "efi", "host": None}
        ]
        with patch("python.srv.plugins.vcenter.vcenter.VCenterNode", return_value=node):
            resources = build_vcenter_resources(dict(INVENTORY_RECORD))
        return next(item for item in resources if item["resource_type"] == "virtual_machine")["payload"]

    def test_the_guest_address_reaches_the_inventory(self) -> None:
        self.assertEqual(self._payload("192.0.2.172")["ip_address"], "192.0.2.172")

    def test_a_vm_without_an_address_has_none(self) -> None:
        self.assertIsNone(self._payload("")["ip_address"])


if __name__ == "__main__":
    unittest.main()
