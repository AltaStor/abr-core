# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import os
import struct
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
    SYSTEM_STATE,
    TASKS,
)
from python.srv.plugins.vcenter import disk_transport
from python.srv.plugins.vcenter.vcenter_backup import (
    VCenterBackupError,
    _build_vddk_args,
)
from python.srv.plugins.vcenter.vcenter_restore import _build_abr_restore_command
from python.srv.services import abr_v41, vsphere_access
from python.srv.services.api_errors import ApiError


def _launcher(root: Path, *, with_vddk: bool, takes_library_file: bool = False) -> Path:
    tool = root / "tools" / "abr-vmdk-tool" / "abr-vmdk-tool.sh"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\n" + (': "${ABR_VMDK_TOOL_VDDK_LIB:-}"\n' if takes_library_file else ""))
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
        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, SYSTEM_STATE, TASKS):
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

    def test_the_first_start_hands_the_old_vddk_default_back_to_the_server_wide_one(self) -> None:
        vddk = self._vsphere()
        soap = self._vsphere("vc2", transport="soap")
        HYPERVISORS[vddk["id"]]["metadata"]["vsphere_transport"] = "vddk"
        HYPERVISORS["hv"] = {"id": "hv", "name": "hv", "type": "hyper-v", "metadata": {}}
        with patch.object(vsphere_access, "persist_hypervisor", side_effect=lambda record: HYPERVISORS.__setitem__(record["id"], record)):
            self.assertEqual(vsphere_access.initialise(), 1)
            self.assertEqual(vsphere_access.initialise(), 0)
        self.assertEqual(disk_transport.global_settings()["api"], "vddk")
        self.assertIsNone(disk_transport.global_settings()["vddk_library"])
        self.assertNotIn("vsphere_transport", HYPERVISORS[vddk["id"]]["metadata"])
        self.assertEqual(HYPERVISORS[soap["id"]]["metadata"]["vsphere_transport"], "soap")
        self.assertEqual(abr_v41.resolve_inventory(record_id=vddk["id"])["transport"], "vddk")

    def test_inherit_hands_the_transport_back_to_the_server_wide_one(self) -> None:
        inventory = self._vsphere(api="soap")
        self.assertEqual((inventory["api"], inventory["effective_api"], inventory["api_source"]), ("soap", "soap", "hypervisor"))
        edited = abr_v41.edit_inventory_record(inventory["id"], {"api": "inherit"})
        self.assertEqual(
            (edited["transport"], edited["api"], edited["effective_api"], edited["api_source"]), ("vddk", "inherit", "vddk", "global")
        )
        self.assertNotIn("vsphere_transport", HYPERVISORS[inventory["id"]]["metadata"])

    def test_api_and_its_earlier_name_transport_must_agree(self) -> None:
        self.assertEqual(self._vsphere(api="soap", transport="soap")["api"], "soap")
        error = self._refused(self._vsphere, "vc2", api="soap", transport="vddk")
        self.assertIn("api and transport name the same setting", error.message)


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


def _shared_library(path: Path, symbols: dict[str, bool]) -> Path:
    """A minimal ELF64 file with a dynamic symbol table: name -> defined (True) or only imported (False)."""
    names = b"\0" + b"".join(name.encode() + b"\0" for name in symbols)
    table = bytes(24)
    offset = 1
    for name, defined in symbols.items():
        table += struct.pack("<IBBHQQ", offset, 0x12, 0, 7 if defined else 0, 0, 0)
        offset += len(name) + 1
    sections_at = 64 + len(names) + len(table)
    header = b"\x7fELF" + bytes([2, 1, 1]) + bytes(9)
    header += struct.pack("<HHIQQQIHHHHHH", 3, 62, 1, 0, 0, sections_at, 0, 64, 0, 0, 64, 3, 0)
    sections = bytes(64)
    sections += struct.pack("<IIQQQQIIQQ", 0, 3, 0, 0, 64, len(names), 0, 0, 1, 0)
    sections += struct.pack("<IIQQQQIIQQ", 0, 11, 0, 0, 64 + len(names), len(table), 1, 1, 8, 24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + names + table + sections)
    return path


class LibraryFileTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def test_a_vddk_library_is_one_that_defines_vixdisklib_initex(self) -> None:
        vddk = _shared_library(self.root / "libvixDiskLib.so.9.1.0.0", {"VixDiskLib_Open": True, "VixDiskLib_InitEx": True})
        user = _shared_library(self.root / "libvixMntapi.so", {"VixDiskLib_InitEx": False})
        self.assertIsNone(disk_transport.library_problem(str(vddk)))
        self.assertIn("does not export 'VixDiskLib_InitEx'", disk_transport.library_problem(str(user)))
        text = self.root / "notes.txt"
        text.write_text("not a library")
        self.assertIn("is not a shared library", disk_transport.library_problem(str(text)))

    def test_a_library_must_be_a_full_path_to_a_file_that_is_there(self) -> None:
        self.assertIn("full path", disk_transport.library_problem("lib64/libvixDiskLib.so"))
        self.assertIn("There is no file", disk_transport.library_problem(str(self.root / "missing.so")))
        self.assertIn("is not a file", disk_transport.library_problem(str(self.root)))

    def test_the_resolved_file_name_follows_the_links(self) -> None:
        real = _shared_library(self.root / "libvixDiskLib.so.9.1.0.0", {"VixDiskLib_InitEx": True})
        (self.root / "libvixDiskLib.so").symlink_to(real.name)
        self.assertEqual(disk_transport.resolved_file_name(self.root / "libvixDiskLib.so"), "libvixDiskLib.so.9.1.0.0")
        self.assertIsNone(disk_transport.resolved_file_name(self.root / "missing.so"))


class AccessResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("ABR_VMDK_TOOL_VDDK_DIR", None)
        SYSTEM_STATE.clear()
        self.addCleanup(SYSTEM_STATE.clear)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.tool = _launcher(self.root, with_vddk=True, takes_library_file=True)
        located = patch.object(disk_transport, "default_disk_tool_path", return_value=self.tool)
        located.start()
        self.addCleanup(located.stop)
        self.directory = self.root / "tools" / "vddk"
        self.default = str(self.directory / "lib64" / "libvixDiskLib.so")
        self.own = str(_shared_library(self.root / "own" / "libvixDiskLib.so.9.1.0.0", {"VixDiskLib_InitEx": True}))
        self.server = str(_shared_library(self.root / "ovddk" / "libvixDiskLib.so.8.0.3", {"VixDiskLib_InitEx": True}))

    def _inventory(self, **metadata) -> dict:
        return {"id": "vc", "type": "vsphere", "metadata": metadata}

    def _library(self, inventory: dict) -> tuple[str, str]:
        access = disk_transport.resolve_access(inventory)
        return access["vddk_library"], access["vddk_library_source"]

    def test_nothing_set_falls_back_to_the_vddk_directory(self) -> None:
        self.assertEqual(self._library(self._inventory()), (self.default, "vddk_directory"))
        self.assertEqual(disk_transport.resolve_access(self._inventory())["api_source"], "global")

    def test_the_server_wide_library_applies_to_inventories_without_their_own(self) -> None:
        disk_transport.store_global_settings("vddk", self.server, updated_by="admin")
        self.assertEqual(self._library(self._inventory()), (self.server, "global"))

    def test_the_inventory_library_wins_over_the_server_wide_one(self) -> None:
        own = self._inventory(vddk_library=self.own)
        self.assertEqual(self._library(own), (self.own, "hypervisor"))
        disk_transport.store_global_settings("vddk", self.server, updated_by="admin")
        self.assertEqual(self._library(own), (self.own, "hypervisor"))

    def test_the_transport_comes_from_the_job_then_the_inventory_then_the_server(self) -> None:
        disk_transport.store_global_settings("soap", None, updated_by="admin")
        inventory = self._inventory()
        self.assertEqual(disk_transport.resolve_access(inventory)["api"], "soap")
        inventory["metadata"]["vsphere_transport"] = "vddk"
        self.assertEqual(disk_transport.resolve_access(inventory)["api_source"], "hypervisor")
        job = {"task_parameters": {"vsphere_transport": "soap"}}
        self.assertEqual(
            {key: disk_transport.resolve_access(inventory, job)[key] for key in ("api", "api_source")},
            {"api": "soap", "api_source": "job"},
        )

    def test_a_library_passes_only_the_library_variable_whatever_the_service_has(self) -> None:
        parameters = disk_transport.session_parameters(self._inventory(vddk_library=self.own))
        service = {"ABR_VMDK_TOOL_VDDK_DIR": "/opt/vddk", "ABR_VMDK_TOOL_VDDK_LIB": "/old.so", "PATH": "/usr/bin"}
        environment = disk_transport.tool_environment(parameters, base=service)
        self.assertEqual(environment["ABR_VMDK_TOOL_VDDK_LIB"], self.own)
        self.assertNotIn("ABR_VMDK_TOOL_VDDK_DIR", environment)
        self.assertEqual(environment["PATH"], "/usr/bin")
        self.assertEqual(parameters["vddk_library_file"], "libvixDiskLib.so.9.1.0.0")

    def test_the_default_library_passes_only_the_directory(self) -> None:
        service = {"ABR_VMDK_TOOL_VDDK_LIB": "/old.so"}
        for inventory in (self._inventory(), self._inventory(vddk_library=self.default)):
            with self.subTest(inventory=inventory):
                environment = disk_transport.tool_environment(disk_transport.session_parameters(inventory), base=service)
                self.assertEqual(environment, {"ABR_VMDK_TOOL_VDDK_DIR": str(self.directory)})

    def test_soap_passes_neither_and_a_session_from_before_keeps_the_service_environment(self) -> None:
        parameters = disk_transport.session_parameters(self._inventory(vsphere_transport="soap", vddk_library=self.own))
        self.assertEqual(parameters, {"vsphere_transport": "soap", "vsphere_api_source": "hypervisor"})
        service = {"ABR_VMDK_TOOL_VDDK_DIR": "/opt/vddk", "ABR_VMDK_TOOL_VDDK_LIB": "/old.so"}
        self.assertEqual(disk_transport.tool_environment(parameters, base=service), {})
        self.assertIsNone(disk_transport.tool_environment({"vsphere_transport": "vddk"}, base=service))

    def test_a_library_the_launcher_does_not_know_is_refused(self) -> None:
        old = _launcher(self.root / "old", with_vddk=True)
        problem = disk_transport.transport_problem("vddk", old, library=self.own)
        self.assertIn("does not take a VDDK library file", problem)
        self.assertIsNone(disk_transport.transport_problem("vddk", self.tool, library=self.own))
        self.assertIn("There is no file", disk_transport.transport_problem("vddk", self.tool, library="/gone/lib.so"))


class ServerWideSettingsTests(_Catalog):
    def setUp(self) -> None:
        super().setUp()
        self.problem.stop()
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("ABR_VMDK_TOOL_VDDK_DIR", None)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        located = patch.object(disk_transport, "default_disk_tool_path", return_value=_launcher(self.root, with_vddk=True, takes_library_file=True))
        located.start()
        self.addCleanup(located.stop)
        soap = patch.object(disk_transport, "disk_tool_has_soap_api", return_value=True)
        soap.start()
        self.addCleanup(soap.stop)
        self.library = str(_shared_library(self.root / "ovddk" / "libvixDiskLib.so", {"VixDiskLib_InitEx": True}))

    def test_without_the_flag_inventories_keep_their_own_values(self) -> None:
        own = self._vsphere(transport="soap")
        plain = self._vsphere("vc2")
        settings, changed, updated = vsphere_access.update_settings({"vddk_library": self.library}, updated_by="admin")
        self.assertEqual((changed, updated), (["vddk_library"], []))
        self.assertEqual((settings["vddk_library"], settings["vddk_library_source"]), (self.library, "global"))
        self.assertEqual(abr_v41.resolve_inventory(record_id=own["id"])["transport"], "soap")
        self.assertEqual(abr_v41.resolve_inventory(record_id=plain["id"])["effective_vddk_library"], self.library)

    def test_the_flag_hands_every_inventory_back_and_names_them(self) -> None:
        own = self._vsphere(transport="soap")
        self._vsphere("vc2")
        _settings, _changed, updated = vsphere_access.update_settings(
            {"api": "vddk", "apply_to_existing_hypervisors": True}, updated_by="admin"
        )
        self.assertEqual(updated, [{"id": own["id"], "name": "vc"}])
        self.assertEqual(abr_v41.resolve_inventory(record_id=own["id"])["api_source"], "global")

    def test_the_directory_default_or_null_resets_the_library(self) -> None:
        vsphere_access.update_settings({"vddk_library": self.library}, updated_by="admin")
        settings, _changed, _updated = vsphere_access.update_settings({"vddk_library": None}, updated_by="admin")
        self.assertIsNone(settings["vddk_library"])
        settings, changed, _updated = vsphere_access.update_settings({"vddk_library": settings["default_vddk_library"]}, updated_by="admin")
        self.assertEqual((settings["vddk_library"], changed), (None, []))

    def test_a_file_that_is_not_a_vddk_is_refused_next_to_the_field(self) -> None:
        other = str(_shared_library(self.root / "libcrypto.so.3", {"EVP_sha256": True}))
        for call in (
            lambda: vsphere_access.update_settings({"vddk_library": other}, updated_by="admin"),
            lambda: self._vsphere(vddk_library=other),
        ):
            error = self._refused(call)
            self.assertEqual((error.status_code, error.code), (422, "invalid_vddk_library"))
            self.assertIn("does not export 'VixDiskLib_InitEx'", error.details["fields"]["vddk_library"])

    def test_an_inventory_library_needs_the_vddk(self) -> None:
        error = self._refused(self._vsphere, transport="soap", vddk_library=self.library)
        self.assertEqual(error.code, "invalid_vddk_library")
        inventory = self._vsphere(vddk_library=self.library)
        self.assertEqual((inventory["vddk_library"], inventory["vddk_library_source"]), (self.library, "hypervisor"))
        cleared = abr_v41.edit_inventory_record(inventory["id"], {"vddk_library": "inherit"})
        self.assertEqual((cleared["vddk_library"], cleared["vddk_library_source"]), (None, "vddk_directory"))

    def test_a_session_records_and_shows_what_it_uses(self) -> None:
        inventory = self._vsphere(vddk_library=self.library)
        parameters = abr_v41.session_transport_parameters(HYPERVISORS[inventory["id"]])
        dto = abr_v41.session_to_dto({"id": "t1", "task_type": "backup", "status": "running", "parameters": parameters, "result": {}})
        self.assertEqual(
            {key: dto[key] for key in ("transport", "api_source", "vddk_library", "vddk_library_file", "vddk_library_source")},
            {
                "transport": "vddk",
                "api_source": "global",
                "vddk_library": self.library,
                "vddk_library_file": "libvixDiskLib.so",
                "vddk_library_source": "hypervisor",
            },
        )
        self.assertEqual(
            disk_transport.describe_access(parameters, verb="read"),
            f"Disks are read over VDDK (the server-wide default), with the VDDK library {self.library} (set for the hypervisor)",
        )


if __name__ == "__main__":
    unittest.main()
