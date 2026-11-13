# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    RESOURCES_BY_HV,
    TASKS,
)
from python.srv.plugins.vcenter import disk_transport
from python.srv.plugins.vcenter.vcenter_backup import (
    VCenterBackupError,
    _build_vddk_args,
)
from python.srv.plugins.vcenter.vcenter_restore import _build_abr_restore_command
from python.srv.services import abr_v41
from python.srv.services.api_errors import ApiError


def _launcher(root: Path, *, with_vddk: bool) -> Path:
    tool = root / "tools" / "abr-vmdk-tool" / "abr-vmdk-tool.sh"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\n")
    if with_vddk:
        library = root / "tools" / "vddk" / "lib64" / "libvixDiskLib.so"
        library.parent.mkdir(parents=True)
        library.write_text("")
    return tool


class TransportChoiceTests(unittest.TestCase):
    def test_values_are_read_case_blind_and_inventory_means_none(self) -> None:
        self.assertEqual(disk_transport.parse_transport(" SOAP "), "soap")
        self.assertIsNone(disk_transport.parse_transport(""))
        self.assertIsNone(disk_transport.parse_transport("inventory", inventory_allowed=True))
        for value, allowed in (("nbd", False), ("inventory", False), ("hotadd", True)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                disk_transport.parse_transport(value, inventory_allowed=allowed)

    def test_a_session_takes_the_job_override_else_the_inventory_else_vddk(self) -> None:
        soap_inventory = {"metadata": {"vsphere_transport": "soap"}}
        self.assertEqual(disk_transport.session_transport({"task_parameters": {"vsphere_transport": "vddk"}}, soap_inventory), "vddk")
        self.assertEqual(disk_transport.session_transport({"task_parameters": {}}, soap_inventory), "soap")
        self.assertEqual(disk_transport.session_transport(None, {"metadata": {}}), "vddk")
        self.assertEqual(disk_transport.session_transport(None, {"metadata": {"vsphere_transport": "bogus"}}), "vddk")


class TransportAvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("ABR_VMDK_TOOL_VDDK_DIR", None)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_the_vddk_is_found_next_to_the_tool_like_the_launcher_finds_it(self) -> None:
        tool = _launcher(self.root, with_vddk=True)
        self.assertEqual(disk_transport.vddk_library(tool), self.root / "tools" / "vddk" / "lib64" / "libvixDiskLib.so")
        self.assertIsNone(disk_transport.transport_problem("vddk", tool))

    def test_a_missing_vddk_is_named_with_the_way_out(self) -> None:
        tool = _launcher(self.root, with_vddk=False)
        problem = disk_transport.transport_problem("vddk", tool)
        self.assertIn("the VMware VDDK is not installed", problem)
        self.assertIn("use the vSphere SOAP transport", problem)

    def test_a_configured_vddk_folder_without_the_vddk_stops_both_transports_and_says_so(self) -> None:
        tool = _launcher(self.root, with_vddk=True)
        os.environ["ABR_VMDK_TOOL_VDDK_DIR"] = str(self.root / "elsewhere")
        for transport in ("vddk", "soap"):
            with self.subTest(transport=transport), patch.object(disk_transport, "disk_tool_has_soap_api", return_value=True):
                problem = disk_transport.transport_problem(transport, tool)
                self.assertIn("ABR_VMDK_TOOL_VDDK_DIR is set to", problem)
                self.assertIn("neither transport works", problem)

    def test_a_configured_vddk_folder_with_the_vddk_is_used(self) -> None:
        tool = _launcher(self.root, with_vddk=False)
        library = self.root / "custom" / "lib64" / "libvixDiskLib.so"
        library.parent.mkdir(parents=True)
        library.write_text("")
        os.environ["ABR_VMDK_TOOL_VDDK_DIR"] = str(self.root / "custom")
        self.assertIsNone(disk_transport.transport_problem("vddk", tool))

    def test_soap_needs_a_tool_that_knows_it(self) -> None:
        tool = _launcher(self.root, with_vddk=True)
        with patch.object(disk_transport, "disk_tool_has_soap_api", return_value=False):
            self.assertIn("has no --api soap", disk_transport.transport_problem("soap", tool))
        with patch.object(disk_transport, "disk_tool_has_soap_api", return_value=True):
            self.assertIsNone(disk_transport.transport_problem("soap", tool))

    def test_a_tool_that_is_not_the_launcher_is_not_judged(self) -> None:
        other = self.root / "custom-tool"
        other.write_text("")
        self.assertIsNone(disk_transport.transport_problem("vddk", other))


class ToolArgumentTests(unittest.TestCase):
    def _args(self, **overrides) -> list[str]:
        options = {
            "vmdk_path": "[ds] vm/vm.vmdk",
            "host": "vc.local",
            "port": 443,
            "username": "user",
            "password": "placeholder",
            "thumbprint": "AA:BB",
            "vm_spec": "moref=vm-1",
            "transport_mode": "nbdssl",
        }
        options.update(overrides)
        return _build_vddk_args(**options)

    def test_vddk_commands_stay_as_they_were(self) -> None:
        args = self._args()
        self.assertEqual(args[-2:], ["--transport", "nbdssl"])
        self.assertNotIn("--api", args)
        self.assertEqual(self._args(api="vddk"), args)

    def test_soap_asks_for_the_soap_api_and_drops_the_vddk_transport(self) -> None:
        args = self._args(api="soap")
        self.assertEqual(args[-2:], ["--api", "soap"])
        self.assertNotIn("--transport", args)

    def test_only_soap_may_leave_out_the_vm(self) -> None:
        self.assertNotIn("--vm", self._args(api="soap", vm_spec=None))
        with self.assertRaises(VCenterBackupError):
            self._args(vm_spec=None)

    def test_a_soap_restore_names_the_cluster_of_its_temporary_vm(self) -> None:
        common = {
            "tool_path": Path("/opt/tool"),
            "repository": {"type": "local"},
            "abr_path": "/srv/repo/vm",
            "backup_name": "disk-1",
            "remote_datastore_path": "[ds] vm/01-disk.vmdk",
            "host": "vc.local",
            "port": 443,
            "username": "user",
            "password": "placeholder",
            "thumbprint": "AA:BB",
            "vm_moref": "moref=vm-1",
            "transport_mode": "nbdssl",
        }
        soap = _build_abr_restore_command(**common, api="soap", cluster="Cluster-A")
        self.assertEqual(soap[-4:], ["--api", "soap", "--cluster", "Cluster-A"])
        vddk = _build_abr_restore_command(**common, cluster="Cluster-A")
        self.assertNotIn("--cluster", vddk)
        self.assertEqual(vddk[-2:], ["--transport", "nbdssl"])


class _Catalog(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            store.clear()
            self.addCleanup(store.clear)
        registrable = patch.object(abr_v41, "assert_inventory_registrable", return_value={})
        registrable.start()
        self.addCleanup(registrable.stop)
        available = patch.object(disk_transport, "transport_problem", return_value=None)
        self.problem = available.start()
        self.addCleanup(available.stop)

    def _refused(self, call, *args, **kwargs) -> ApiError:
        with self.assertRaises(ApiError) as caught:
            call(*args, **kwargs)
        return caught.exception

    def _vsphere(self, name: str = "vc", **extra) -> dict:
        return abr_v41.create_inventory_record(
            {"name": name, "type": "vsphere", "address": f"{name}.local", "username": "user", "password": "placeholder", **extra}
        )


class InventoryTransportTests(_Catalog):
    def test_a_new_vsphere_inventory_uses_vddk_unless_told_otherwise(self) -> None:
        self.assertEqual(self._vsphere()["transport"], "vddk")
        soap = self._vsphere("vc2", transport="soap")
        self.assertEqual(soap["transport"], "soap")
        self.assertEqual(HYPERVISORS[soap["id"]]["metadata"]["vsphere_transport"], "soap")

    def test_an_unknown_transport_or_one_on_hyper_v_is_refused(self) -> None:
        self.assertIn("transport must be one of: vddk, soap", self._refused(self._vsphere, transport="nbd").message)
        hyperv = {"name": "hv", "type": "hyper-v", "address": "hv.local", "username": "user", "password": "placeholder", "transport": "soap"}
        self.assertIn("vSphere inventories", self._refused(abr_v41.create_inventory_record, hyperv).message)

    def test_a_transport_this_server_cannot_use_is_refused_with_its_reason(self) -> None:
        self.problem.return_value = "The VDDK transport is not available: the VMware VDDK is not installed"
        error = self._refused(self._vsphere, transport="vddk")
        self.assertEqual((error.status_code, error.code), (503, "transport_unavailable"))
        self.assertIn("VDDK is not installed", error.message)

    def test_edit_switches_the_transport(self) -> None:
        inventory = self._vsphere()
        with patch.object(abr_v41, "_recheck_inventory_connection", side_effect=AssertionError("no reconnect for a transport")):
            edited = abr_v41.edit_inventory_record(inventory["id"], {"transport": "soap"})
        self.assertEqual(edited["transport"], "soap")

    def test_inventories_from_before_transports_get_vddk_once(self) -> None:
        inventory = self._vsphere()
        HYPERVISORS[inventory["id"]]["metadata"].pop("vsphere_transport")
        HYPERVISORS["hv"] = {"id": "hv", "name": "hv", "type": "hyper-v", "metadata": {}}
        with patch.object(abr_v41, "persist_hypervisor", side_effect=lambda record: HYPERVISORS.__setitem__(record["id"], record)):
            self.assertEqual(abr_v41.backfill_inventory_transports(), 1)
            self.assertEqual(abr_v41.backfill_inventory_transports(), 0)
        self.assertEqual(HYPERVISORS[inventory["id"]]["metadata"]["vsphere_transport"], "vddk")
        self.assertNotIn("vsphere_transport", HYPERVISORS["hv"]["metadata"])


class JobTransportTests(_Catalog):
    def setUp(self) -> None:
        super().setUp()
        REPOSITORIES["repo-1"] = {"id": "repo-1", "name": "Repo One", "type": "local", "path": "/tmp/repo-1", "metadata": {}}
        self.inventory = self._vsphere()
        RESOURCES_BY_HV[str(self.inventory["id"])] = {
            ("virtual_machine", "id-web"): {
                "resource_type": "virtual_machine",
                "external_id": "id-web",
                "name": "web",
                "payload": {},
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            }
        }

    def _job(self, **task_parameters) -> dict:
        return abr_v41.create_backup_job(
            {
                "name": f"job-{len(JOBS)}",
                "vms": ["web"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "task_parameters": task_parameters,
            }
        )

    def test_a_job_uses_its_inventory_until_it_overrides_it(self) -> None:
        plain = self._job()
        self.assertEqual((plain["transport"], plain["effective_transport"]), ("inventory", "vddk"))
        soap = self._job(vsphere_transport="soap")
        self.assertEqual((soap["transport"], soap["effective_transport"]), ("soap", "soap"))
        abr_v41.edit_inventory_record(self.inventory["id"], {"transport": "soap"})
        self.assertEqual(abr_v41.job_to_dto(JOBS[plain["id"]])["effective_transport"], "soap")

    def test_inventory_clears_the_override(self) -> None:
        job = self._job(vsphere_transport="soap")
        edited = abr_v41.edit_job_record(job["id"], {"task_parameters": {"vsphere_transport": "inventory"}})
        self.assertEqual(edited["transport"], "inventory")
        self.assertNotIn("vsphere_transport", JOBS[job["id"]]["task_parameters"])

    def test_an_unknown_override_is_refused(self) -> None:
        self.assertIn("vsphere_transport must be one of", self._refused(self._job, vsphere_transport="hotadd").message)

    def test_a_session_keeps_the_transport_it_was_created_with(self) -> None:
        job = JOBS[self._job()["id"]]
        session = abr_v41._create_backup_session(job, vm_id=job["vm_ids"][0], vm_name="web", execution_type="ActiveFull", execution_reason="Manual", run_id="run-1")
        self.assertEqual(TASKS[session["id"]]["parameters"]["vsphere_transport"], "vddk")
        abr_v41.edit_inventory_record(self.inventory["id"], {"transport": "soap"})
        self.assertEqual(TASKS[session["id"]]["parameters"]["vsphere_transport"], "vddk")
        later = abr_v41._create_backup_session(job, vm_id=job["vm_ids"][0], vm_name="web", execution_type="ActiveFull", execution_reason="Manual", run_id="run-2")
        self.assertEqual(TASKS[later["id"]]["parameters"]["vsphere_transport"], "soap")


class SessionViewTests(unittest.TestCase):
    def test_session_details_name_the_transport_of_vsphere_sessions_only(self) -> None:
        soap = {"id": "t1", "task_type": "restore", "status": "completed", "parameters": {"vsphere_transport": "soap"}, "result": {}}
        hyperv = {"id": "t2", "task_type": "backup", "status": "completed", "parameters": {}, "result": {}}
        self.assertEqual(abr_v41.session_to_dto(soap)["transport"], "soap")
        self.assertNotIn("transport", abr_v41.session_to_dto(hyperv))

if __name__ == "__main__":
    unittest.main()
