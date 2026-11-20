# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, HYPERVISORS, REPOSITORIES, RESOURCES_BY_HV
from python.srv.core.task_executor import TaskStopRequested
from python.srv.plugins.pve import pve_restore
from python.srv.plugins.pve.pve import PveAccess, restore_metadata, vm_name_problem
from python.srv.plugins.pve.pve_api import PveApiError
from python.srv.plugins.pve.ssh import CommandResult
from python.srv.plugins.pve.pve_restore import (
    PveRestoreError,
    RestoreLog,
    adapter_targets,
    archive_name,
    build_run_file,
    disk_chain,
    disk_targets,
    map_lines,
    point_ctime,
    qualified_chains,
    restore_guest_config,
    run_pve_restore,
)
from python.srv.services import abr_v41, backup_catalog

ENV = {"ALTASTOR_PVE_RUN_FILE_MINUTES": "60"}
NODE = "node1"
UPID = "UPID:node1:0004B3A2:01C9F3E1:691F0A2B:qmrestore:120:abr@pve!altastor-inv1:"
START_UPID = "UPID:node1:0004B3A3:01C9F3E2:691F0A2C:qmstart:120:abr@pve!altastor-inv1:"

S3 = {
    "id": "repo-s3",
    "name": "S3",
    "type": "s3",
    "metadata": {
        "s3": {
            "bucket": "backups",
            "endpoint_url": "http://s3.example.com:9000",
            "access_key": "abr-dev",
            "secret_key": "s3-secret",
            "path_style": True,
        }
    },
}

GUEST_CONFIG = """bios: ovmf
boot: order=scsi0
efidisk0: local-lvm:vm-9002-disk-0,efitype=4m,size=528K
memory: 2048
name: web
net0: virtio=BC:24:11:00:00:01,bridge=vmbr0,firewall=1
net1: e1000=BC:24:11:00:00:02,bridge=vmbr1
scsi0: local-lvm:vm-9002-disk-1,size=5G
tpmstate0: local-lvm:vm-9002-disk-2,size=4M,version=v2.0
#qmdump#map:efidisk0:drive-efidisk0:local-lvm:raw:
#qmdump#map:scsi0:drive-scsi0:local-lvm:raw:
#qmdump#map:tpmstate0:drive-tpmstate0-backup:local-lvm:raw:
"""

CONFIG = {
    "bios": "ovmf",
    "cores": 2,
    "sockets": 1,
    "memory": "2048",
    "ostype": "l26",
    "name": "web",
    "net0": "virtio=BC:24:11:00:00:01,bridge=vmbr0,firewall=1",
    "net1": "e1000=BC:24:11:00:00:02,bridge=vmbr1",
    "scsi0": "local-lvm:vm-9002-disk-1,size=5G",
    "efidisk0": "local-lvm:vm-9002-disk-0,efitype=4m,size=528K",
    "tpmstate0": "local-lvm:vm-9002-disk-2,size=4M,version=v2.0",
}

SIZES = {"scsi0": 5 << 30, "efidisk0": 540672, "tpmstate0": 4 << 20}


def _disk(drive: str, task: str, mode: str, storage: str = "local-lvm") -> dict:
    return {
        "index": list(SIZES).index(drive) + 1,
        "drive": drive,
        "disk_label": drive,
        "datastore_path": f"{storage}:vm-9002-disk-{list(SIZES).index(drive)}",
        "storage": storage,
        "capacity_bytes": SIZES[drive],
        "store_mode": mode,
        "abr_path": f"pve/web/{task}",
        "abr_backup_name": drive,
    }


def _manifest(task: str, modes: dict[str, str]) -> dict:
    return {
        "hypervisor_kind": "proxmox-ve",
        "vm_name": "web",
        "vm_external_id": "9002",
        "disks": [_disk(drive, task, mode) for drive, mode in modes.items()],
        "vm_config": {
            **restore_metadata("9002", "web", NODE, CONFIG),
            "guest_config": GUEST_CONFIG,
            "firewall_config": "[OPTIONS]\nenable: 1\n",
        },
    }


# The TPM state is backed up in full every time; the other disks chain onto the first full.
CHAIN = [
    ("point-a", "task-a", {"scsi0": "full", "efidisk0": "full", "tpmstate0": "full"}),
    ("point-b", "task-b", {"scsi0": "incremental", "efidisk0": "incremental", "tpmstate0": "full"}),
    ("point-c", "task-c", {"scsi0": "incremental", "efidisk0": "incremental", "tpmstate0": "full"}),
]


class ChainTests(unittest.TestCase):
    def points(self, chain=CHAIN):
        return [{"id": point_id} for point_id, _task, _modes in chain], [
            _manifest(task, modes) for _point_id, task, modes in chain
        ]

    def test_each_disk_starts_at_its_newest_full_backup(self):
        points, manifests = self.points()
        names = {drive: [layer["abr_backup_name"] for layer in disk_chain(points, manifests, drive)] for drive in SIZES}
        paths = {drive: [layer["abr_path"] for layer in disk_chain(points, manifests, drive)] for drive in SIZES}
        self.assertEqual(paths["scsi0"], ["pve/web/task-a", "pve/web/task-b", "pve/web/task-c"])
        self.assertEqual(paths["tpmstate0"], ["pve/web/task-c"])
        self.assertEqual(names["efidisk0"], ["efidisk0", "efidisk0", "efidisk0"])

    def test_a_synthetic_full_starts_a_chain_too(self):
        chain = [*CHAIN[:2], ("point-c", "task-c", {"scsi0": "synthetic-full", "efidisk0": "incremental"})]
        points, manifests = self.points(chain)
        self.assertEqual([layer["abr_path"] for layer in disk_chain(points, manifests, "scsi0")], ["pve/web/task-c"])

    def test_a_disk_missing_from_a_point_starts_again_at_its_next_full(self):
        chain = [
            CHAIN[0],
            ("point-b", "task-b", {"efidisk0": "incremental"}),
            ("point-c", "task-c", {"scsi0": "full", "efidisk0": "incremental"}),
        ]
        points, manifests = self.points(chain)
        self.assertEqual([layer["abr_path"] for layer in disk_chain(points, manifests, "scsi0")], ["pve/web/task-c"])

    def test_a_chain_without_a_full_backup_is_refused(self):
        points, manifests = self.points([("point-b", "task-b", {"scsi0": "incremental"})])
        with self.assertRaisesRegex(PveRestoreError, "no full backup of disk scsi0"):
            disk_chain(points, manifests, "scsi0")

    def test_chains_across_session_folders_name_each_backup_below_their_common_folder(self):
        points, manifests = self.points()
        common, names = qualified_chains({drive: disk_chain(points, manifests, drive) for drive in SIZES})
        self.assertEqual(common, "pve/web")
        self.assertEqual(names["scsi0"], ["task-a/scsi0", "task-b/scsi0", "task-c/scsi0"])
        self.assertEqual(names["tpmstate0"], ["task-c/tpmstate0"])

    def test_chains_within_one_session_are_plain_names(self):
        points, manifests = self.points(CHAIN[:1])
        common, names = qualified_chains({drive: disk_chain(points, manifests, drive) for drive in SIZES})
        self.assertEqual((common, names["scsi0"]), ("pve/web/task-a", ["scsi0"]))


class GuestConfigTests(unittest.TestCase):
    def test_the_backup_config_is_kept_when_nothing_changes(self):
        self.assertEqual(restore_guest_config(GUEST_CONFIG), GUEST_CONFIG)

    def test_name_storages_and_bridges_change_and_other_lines_stay(self):
        text = restore_guest_config(
            GUEST_CONFIG,
            name="web-restored",
            storages={"scsi0": "local-zfs", "tpmstate0": "backup-dir"},
            bridges={"net1": "vmbr9"},
        )
        lines = text.splitlines()
        self.assertIn("name: web-restored", lines)
        self.assertIn("#qmdump#map:scsi0:drive-scsi0:local-zfs:raw:", lines)
        self.assertIn("#qmdump#map:tpmstate0:drive-tpmstate0-backup:backup-dir:raw:", lines)
        self.assertIn("#qmdump#map:efidisk0:drive-efidisk0:local-lvm:raw:", lines)
        self.assertIn("net0: virtio=BC:24:11:00:00:01,bridge=vmbr0,firewall=1", lines)
        self.assertIn("net1: e1000=BC:24:11:00:00:02,bridge=vmbr9", lines)
        self.assertIn("scsi0: local-lvm:vm-9002-disk-1,size=5G", lines)
        self.assertEqual(len(lines), len(GUEST_CONFIG.splitlines()))

    def test_links_go_down_when_adapters_are_not_to_connect(self):
        lines = restore_guest_config(GUEST_CONFIG, link_down=True).splitlines()
        self.assertIn("net0: virtio=BC:24:11:00:00:01,bridge=vmbr0,firewall=1,link_down=1", lines)
        self.assertIn("net1: e1000=BC:24:11:00:00:02,bridge=vmbr1,link_down=1", lines)

    def test_a_config_without_a_name_gets_one(self):
        text = restore_guest_config("memory: 512\n", name="web-restored")
        self.assertEqual(text, "name: web-restored\nmemory: 512\n")

    def test_a_disk_left_out_loses_its_map_line_and_keeps_its_config_line(self):
        config = GUEST_CONFIG + "scsi10: /dev/disk/by-id/ata-disk-1,size=5G\n#qmdump#map:scsi10:drive-scsi10::raw:\n"
        lines = restore_guest_config(config, left_out={"scsi10"}).splitlines()
        self.assertIn("scsi10: /dev/disk/by-id/ata-disk-1,size=5G", lines)
        self.assertNotIn("#qmdump#map:scsi10:drive-scsi10::raw:", lines)
        self.assertIn("#qmdump#map:scsi0:drive-scsi0:local-lvm:raw:", lines)

    def test_the_map_lines_tell_which_disks_pve_restores(self):
        self.assertEqual(
            map_lines(GUEST_CONFIG)["tpmstate0"],
            {"device": "drive-tpmstate0-backup", "storage": "local-lvm", "format": "raw"},
        )


class PlacementTests(unittest.TestCase):
    disks = [
        _disk("scsi0", "t", "full"),
        _disk("efidisk0", "t", "full", "local-dir"),
        _disk("tpmstate0", "t", "full"),
    ]

    def test_a_new_vms_disks_go_where_mapped_by_position_by_storage_or_to_the_restores_storage(self):
        parameters = {
            "target_datastore_name": "local-lvm",
            "datastore_map": {"local-dir": "backup-dir"},
            "disk_datastore_map": [{"index": 3, "target_datastore": "local-zfs"}],
        }
        self.assertEqual(
            disk_targets(self.disks, parameters, None),
            {"scsi0": "local-lvm", "efidisk0": "backup-dir", "tpmstate0": "local-zfs"},
        )

    def test_in_place_disks_stay_where_the_vm_keeps_them_now(self):
        current = {"scsi0": "local-zfs:vm-9002-disk-1,size=5G", "efidisk0": "local-zfs:vm-9002-disk-0,size=528K"}
        self.assertEqual(
            disk_targets(self.disks, {"target_datastore_name": "ignored"}, current),
            {"scsi0": "local-zfs", "efidisk0": "local-zfs", "tpmstate0": "local-lvm"},
        )

    def test_adapters_land_on_the_bridge_mapped_by_position_by_bridge_or_their_own(self):
        vm_config = _manifest("t", {})["vm_config"]
        self.assertEqual([item["target_network"] for item in adapter_targets(vm_config, {})], ["vmbr0", "vmbr1"])
        mapped = adapter_targets(
            vm_config,
            {"network_map": {"vmbr0": "vmbr5"}, "network_adapter_map": [{"index": 1, "target_network": "vmbr7"}]},
        )
        self.assertEqual(
            [(item["label"], item["target_network"]) for item in mapped], [("net0", "vmbr5"), ("net1", "vmbr7")]
        )

    def test_adapters_of_an_older_manifest_come_from_the_vm_config(self):
        vm_config = {"vmid": "9002", "config": CONFIG}
        self.assertEqual([item["source_network"] for item in adapter_targets(vm_config, {})], ["vmbr0", "vmbr1"])


class VmNameTests(unittest.TestCase):
    def test_pve_takes_dns_names_only(self):
        for name in ("web", "web-restored-2", "web.example.com", "A1"):
            self.assertIsNone(vm_name_problem(name), name)
        for name in ("web_restored", "web restored", "-web", "web-", "web..x", "x" * 64, ""):
            self.assertIn("cannot name a Proxmox VE VM", vm_name_problem(name) or "", name)


class RunFileTests(unittest.TestCase):
    def test_the_run_file_restores_each_disk_from_its_chain(self):
        with patch.dict("os.environ", ENV):
            run = build_run_file(
                run_id="task-9",
                task_user="abr@pve!altastor-inv1",
                storage="abr",
                archive=archive_name("9002", 1795467995),
                target_vmid="120",
                guest_config=GUEST_CONFIG,
                firewall_config="",
                repository={"mode": "s3", "folder": "backups", "path": "pve/web"},
                secrets={"key": "k", "secret_key": ""},
                disks=[_disk("scsi0", "t", "full"), _disk("tpmstate0", "t", "full")],
                chains={"scsi0": ["task-a/scsi0", "task-b/scsi0"], "tpmstate0": ["task-b/tpmstate0"]},
                now=datetime(2026, 11, 20, 9, 30, tzinfo=timezone.utc),
            )
        self.assertEqual(
            (run["kind"], run["archive"], run["target_vmid"]), ("restore", "backup/9002/qemu-1795467995", 120)
        )
        self.assertEqual((run["created"], run["expires"]), ("2026-11-20T09:30:00Z", "2026-11-20T10:30:00Z"))
        self.assertEqual(
            run["disks"],
            {
                "drive-scsi0": {"size": 5 << 30, "chain": ["task-a/scsi0", "task-b/scsi0"]},
                "drive-tpmstate0-backup": {"size": 4 << 20, "chain": ["task-b/tpmstate0"]},
            },
        )
        self.assertIsNone(run["firewall_config"])
        self.assertEqual(run["secrets"], {"key": "k"})

    def test_a_restore_is_named_after_the_points_time(self):
        self.assertEqual(point_ctime({"created_at": "2026-11-17T21:00:00Z"}), 1794949200)
        self.assertEqual(point_ctime({"created_at": "2026-11-17T21:00:00+00:00"}), 1794949200)


class RestoreLogTests(unittest.TestCase):
    def test_progress_counts_each_disk_by_its_size_and_the_log_says_what_happened(self):
        messages = []
        log = RestoreLog(
            {"drive-scsi0": 300, "drive-efidisk0": 100},
            lambda stage, progress, message, **extra: messages.append((progress, message, extra)),
            start=0,
            span=100,
        )
        for line in (
            "INFO: serving drive-efidisk0 from a chain of 1 backup at /dev/nbd0",
            "importing drive 'drive-efidisk0' from '/dev/nbd0'",
            "transferred 100.0 B of 100.0 B (100.00%)",
            "INFO: serving drive-scsi0 from a chain of 2 backups at /dev/nbd0",
            "importing drive 'drive-scsi0' from '/dev/nbd0'",
            "transferred 30.0 B of 300.0 B (10.00%)",
            "transferred 33.0 B of 300.0 B (11.00%)",
            "transferred 150.0 B of 300.0 B (50.00%)",
            'Logical volume "vm-120-disk-0" created.',
        ):
            log.line(line)
        self.assertEqual(log.progress(), 62)
        self.assertEqual(
            [message for _progress, message, _extra in messages],
            [
                "drive-efidisk0: serving a chain of 1 backup at /dev/nbd0",
                "drive-efidisk0: PVE copies it to its new volume (100 B)",
                "drive-efidisk0: 100%",
                "drive-scsi0: serving a chain of 2 backups at /dev/nbd0",
                "drive-scsi0: PVE copies it to its new volume (300 B)",
                "drive-scsi0: 10%",
                "drive-scsi0: 50%",
            ],
        )
        self.assertEqual(log.lines, 9)

    def test_errors_are_kept_for_the_failure_message(self):
        messages = []
        log = RestoreLog({"drive-scsi0": 1}, lambda *args, **extra: messages.append(extra))
        log.line("TASK ERROR: unable to restore VM 120 - storage 'nope' does not exist")
        self.assertEqual(log.errors, ["unable to restore VM 120 - storage 'nope' does not exist"])
        self.assertEqual(messages, [{"log_level": "warn"}])


# ---- sessions --------------------------------------------------------------------------------------------------


class FakeShell:
    def __init__(self) -> None:
        self.address = "pve1.example.com"
        self.files: dict[str, bytes] = {}
        self.written: dict[str, dict] = {}
        self.removed: list[str] = []
        self.nbd = "16\n0\n"

    def make_directory(self, path, mode):
        pass

    def write_file(self, path, data, mode=0o600):
        self.written[path] = json.loads(data)

    def run(self, command, check=True):
        """The node's NBD devices: how many there are, then how many are in use."""
        return CommandResult(0, self.nbd.encode(), b"")

    def read_file(self, path):
        return self.files.get(path)

    def remove_file(self, path):
        self.removed.append(path)
        self.files.pop(path, None)


class FakeApi:
    def __init__(self, cluster: "FakeCluster") -> None:
        self.cluster = cluster
        self.started: list[tuple[str, dict]] = []
        self.followed: list[dict] = []

    def tasks(self, node, **filters):
        self.cluster.task_queries.append(filters)
        return list(self.cluster.pve_tasks)

    def task_log(self, upid, start=0, limit=None):
        return list(self.cluster.task_logs.get(upid, []))

    def start_task(self, path, method="POST", **params):
        self.started.append((path if method == "POST" else f"{method} {path}", params))
        if path.endswith("/qemu"):
            if self.cluster.taken:
                self.cluster.taken.pop(0)
                raise PveApiError(f"POST {path}: unable to create VM {params['vmid']} - VM already exists on node")
            if self.cluster.result is not None:
                ctime = params["archive"].rsplit("-", 1)[1]
                self.cluster.shell_.files[f"/run/abr/restore-9002-{ctime}.result.json"] = json.dumps(
                    self.cluster.result
                ).encode()
            return UPID
        return START_UPID

    def wait_task(self, upid, *, on_log=None, should_stop=None, log_start=0):
        if upid != UPID:
            self.cluster.other_tasks.append(upid)
            return {"status": "stopped", "exitstatus": "OK"}
        self.followed.append({"upid": upid, "log_start": log_start})
        for line in self.cluster.log:
            on_log(line)
        if self.cluster.stop_during_task:
            self.cluster.stopped_by_task = should_stop()
        return self.cluster.status


class FakeCluster:
    def __init__(self, status: dict | None = None) -> None:
        self.access = PveAccess(
            host="pve1.example.com",
            token_id="abr@pve!altastor-inv1",
            token_secret="t",
            ssh_key="k",
            nodes=[
                {"name": NODE, "address": "pve1.example.com", "host_keys": []},
                {"name": "node2", "address": "pve2.example.com", "host_keys": []},
            ],
        )
        self.shell_ = FakeShell()
        self.api = FakeApi(self)
        self.result: dict | None = {"run_id": "task-9", "status": "ok", "warnings": []}
        self.status = status or {"status": "stopped", "exitstatus": "OK"}
        self.log = [
            "INFO: serving drive-scsi0 from a chain of 3 backups at /dev/nbd0",
            "importing drive 'drive-scsi0' from '/dev/nbd0'",
            "transferred 5.0 GiB of 5.0 GiB (100.00%)",
        ]
        self.next_ids = ["120", "121", "122"]
        self.taken: list[str] = []
        self.storages = {"local-lvm": {"storage": "local-lvm", "active": 1}, "local-zfs": {"active": 1}}
        self.state = "running"
        self.config = dict(CONFIG)
        self.located = {"vmid": 9002, "node": NODE, "name": "web", "status": "running"}
        self.stop_during_task = False
        self.stopped_by_task = None
        self.other_tasks: list[str] = []
        self.destroyed: list[tuple] = []
        self.pve_tasks: list[dict] = []
        self.task_logs: dict[str, list[str]] = {}
        self.task_queries: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def cluster_status(self):
        return [
            {"type": "node", "name": NODE, "online": 1},
            {"type": "node", "name": "node2", "online": 0},
        ]

    def next_vm_id(self):
        return self.next_ids.pop(0)

    def image_storages(self, node):
        return self.storages

    def bridges(self, node):
        return ["vmbr0", "vmbr1"]

    def locate_vm(self, vmid):
        return dict(self.located)

    def vm_config(self, node, vmid):
        return dict(self.config)

    def vm_state(self, node, vmid):
        state, self.state = self.state, "stopped"
        return state

    def shell(self, node):
        return self.shell_


class SessionTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        parent = None
        for position, (point_id, task, modes) in enumerate(CHAIN):
            path = folder / f"{task}.json"
            path.write_text(json.dumps(_manifest(task, modes)))
            BACKUPS[point_id] = {
                "id": point_id,
                "parent_point_id": parent,
                "created_at": f"2026-11-1{5 + position}T21:00:00Z",
                "repository_id": "repo-s3",
                "artifact": {"manifest_path": str(path)},
            }
            parent = point_id
        HYPERVISORS["inv-1"] = {"id": "inv-1", "kind": "proxmox-ve", "name": "PVE-Cluster"}
        REPOSITORIES["repo-s3"] = S3
        for table in (BACKUPS, HYPERVISORS, REPOSITORIES):
            self.addCleanup(table.clear)
        self.progress: list[dict] = []
        self.rescans: list[str] = []
        self.enterContext(patch.object(pve_restore, "ensure_package", lambda shell, node, package, progress: "0.1.1"))
        self.enterContext(patch.object(pve_restore, "package_path", lambda: None))
        self.enterContext(patch.object(pve_restore.time, "sleep", lambda seconds: None))
        self.enterContext(
            patch.object(
                abr_v41, "schedule_inventory_discovery", lambda inventory, force: self.rescans.append(inventory)
            )
        )
        self.enterContext(patch.dict("os.environ", ENV))

    def run_session(self, cluster: FakeCluster, parameters: dict | None = None, result: dict | None = None) -> dict:
        task = {
            "id": "task-9",
            "task_type": "restore",
            "hypervisor_id": "inv-1",
            "parameters": {
                "point_id": "point-c",
                "repository_id": "repo-s3",
                "restore_mode": "new_vm",
                "target_host_name": NODE,
                "target_datastore_name": "local-lvm",
                "target_vm_name": "web-restored",
                **(parameters or {}),
            },
            "result": result or {},
        }
        with patch.object(pve_restore.PveNode, "from_record", return_value=cluster):
            return run_pve_restore(task, progress_callback=self.progress.append)

    def add_physical_disk(self) -> None:
        """The VM gets a physical disk, which vzdump backs up in every session like the others."""
        for point_id, task, modes in CHAIN:
            path = Path(BACKUPS[point_id]["artifact"]["manifest_path"])
            manifest = json.loads(path.read_text())
            manifest["disks"].append(
                {
                    **_disk("scsi0", task, modes["scsi0"]),
                    "index": 4,
                    "drive": "scsi10",
                    "disk_label": "scsi10",
                    "datastore_path": "/dev/disk/by-id/ata-disk-1",
                    "storage": "",
                    "abr_backup_name": "scsi10",
                }
            )
            manifest["vm_config"][
                "guest_config"
            ] += "scsi10: /dev/disk/by-id/ata-disk-1,size=5G\n#qmdump#map:scsi10:drive-scsi10::raw:\n"
            path.write_text(json.dumps(manifest))

    def runs(self) -> list[dict]:
        return [item["pve_run"] for item in self.progress if "pve_run" in item]

    def messages(self) -> list[str]:
        return [str(item.get("message")) for item in self.progress if item.get("message")]

    def test_a_restore_to_a_new_vm_writes_the_run_file_and_starts_the_restore_with_new_macs(self):
        outcome = self.run_session(cluster := FakeCluster(), {"encryption_key": "secret-key"})
        archive = f"backup/9002/qemu-{point_ctime(BACKUPS['point-c'])}"
        path, params = cluster.api.started[0]
        self.assertEqual(path, f"/nodes/{NODE}/qemu")
        self.assertEqual(params, {"vmid": "120", "archive": f"abr:{archive}", "unique": 1})
        run = cluster.shell_.written[f"/run/abr/restore-9002-{point_ctime(BACKUPS['point-c'])}.json"]
        self.assertEqual((run["archive"], run["target_vmid"], run["run_id"]), (archive, 120, "task-9"))
        self.assertEqual(run["repository"]["path"], "pve/web")
        self.assertEqual(run["disks"]["drive-scsi0"]["chain"], ["task-a/scsi0", "task-b/scsi0", "task-c/scsi0"])
        self.assertEqual(run["disks"]["drive-tpmstate0-backup"]["chain"], ["task-c/tpmstate0"])
        self.assertEqual(run["secrets"], {"secret_key": "s3-secret", "key": "secret-key"})
        self.assertIn("name: web-restored", run["guest_config"].splitlines())
        self.assertEqual(run["firewall_config"], "[OPTIONS]\nenable: 1\n")
        self.assertEqual(
            (outcome["target_vm_id"], outcome["target_vm_name"], outcome["target_host_name"]),
            ("120", "web-restored", NODE),
        )
        self.assertEqual(outcome["power_state"], "stopped")
        self.assertEqual(outcome["pve_run"]["upid"], UPID)
        self.assertTrue(outcome["pve_run"]["restore_ok"])
        self.assertIn(f"/run/abr/restore-9002-{point_ctime(BACKUPS['point-c'])}.result.json", cluster.shell_.removed)
        self.assertEqual(self.rescans, ["inv-1"])
        self.assertIn("drive-scsi0: serving a chain of 3 backups at /dev/nbd0", self.messages())

    def test_disks_and_adapters_go_where_the_restore_maps_them(self):
        cluster = FakeCluster()
        self.run_session(
            cluster,
            {
                "disk_datastore_map": [{"index": 1, "target_datastore": "local-zfs"}],
                "network_adapter_map": [{"index": 0, "target_network": "vmbr1"}],
                "connect_nics": False,
            },
        )
        config = next(iter(cluster.shell_.written.values()))["guest_config"].splitlines()
        self.assertIn("#qmdump#map:scsi0:drive-scsi0:local-zfs:raw:", config)
        self.assertIn("#qmdump#map:efidisk0:drive-efidisk0:local-lvm:raw:", config)
        self.assertIn("net0: virtio=BC:24:11:00:00:01,bridge=vmbr1,firewall=1,link_down=1", config)

    def test_an_id_another_vm_took_meanwhile_is_skipped(self):
        cluster = FakeCluster()
        cluster.taken = ["120"]
        outcome = self.run_session(cluster)
        self.assertEqual([params["vmid"] for _path, params in cluster.api.started], ["120", "121"])
        self.assertEqual(outcome["target_vm_id"], "121")

    def test_the_vm_starts_afterwards_when_asked(self):
        cluster = FakeCluster()
        outcome = self.run_session(cluster, {"power_on": True})
        self.assertEqual(cluster.api.started[1][0], f"/nodes/{NODE}/qemu/120/status/start")
        self.assertEqual(outcome["power_state"], "running")

    def test_a_failed_restore_removes_the_vm_it_created_and_says_why(self):
        cluster = FakeCluster({"status": "stopped", "exitstatus": "drive-scsi0: S3 bucket 'b' is not accessible"})
        cluster.result = {"run_id": "task-9", "status": "error", "error": "serving drive-scsi0 failed"}
        cluster.log = [
            "error before or during data restore, some or all disks were not completely restored. VM 120 state is NOT "
            "cleaned up.",
            "TASK ERROR: drive-scsi0: S3 bucket 'b' is not accessible",
        ]
        with self.assertRaises(PveRestoreError) as caught:
            self.run_session(cluster)
        message = str(caught.exception)
        self.assertIn(
            "The restore to VM 120 on node node1 failed: drive-scsi0: S3 bucket 'b' is not accessible. VM 120 the "
            "restore had created was removed",
            message,
        )
        self.assertIn("VM 120 the restore had created was removed", message)
        self.assertEqual(
            cluster.api.started[-1], (f"DELETE /nodes/{NODE}/qemu/120", {"purge": 1, "destroy-unreferenced-disks": 1})
        )

    def test_a_stop_stops_the_task_and_removes_the_vm(self):
        cluster = FakeCluster({"status": "stopped", "exitstatus": "interrupted by signal"})
        cluster.stop_during_task = True
        # The stop comes while PVE restores.
        with patch.object(pve_restore, "cancel_requested", lambda task_id: bool(cluster.api.followed)):
            with self.assertRaisesRegex(TaskStopRequested, "VM 120 the restore had created was removed"):
                self.run_session(cluster)
        self.assertTrue(cluster.stopped_by_task)
        self.assertEqual(cluster.api.started[-1][0], f"DELETE /nodes/{NODE}/qemu/120")

    def test_a_stop_before_the_task_starts_leaves_nothing_to_undo(self):
        cluster = FakeCluster()
        with patch.object(pve_restore, "cancel_requested", return_value=True):
            with self.assertRaisesRegex(TaskStopRequested, "^Session stop requested$"):
                self.run_session(cluster)
        self.assertEqual(cluster.api.started, [])
        self.assertEqual(cluster.shell_.written, {})

    def test_a_session_the_server_lost_follows_its_task_again(self):
        cluster = FakeCluster()
        ctime = point_ctime(BACKUPS["point-c"])
        cluster.shell_.files[f"/run/abr/restore-9002-{ctime}.result.json"] = json.dumps(cluster.result).encode()
        earlier = {
            "pve_run": {
                "kind": "restore",
                "upid": UPID,
                "node": NODE,
                "target_vmid": "120",
                "created_vm": True,
                "ctime": ctime,
                "run_id": "task-9",
                "storages": {"scsi0": "local-lvm"},
                "log_lines": 40,
            }
        }
        outcome = self.run_session(cluster, result=earlier)
        self.assertEqual(cluster.api.started, [])
        self.assertEqual(cluster.api.followed, [{"upid": UPID, "log_start": 40}])
        self.assertEqual(outcome["target_vm_id"], "120")

    def test_an_in_place_restore_turns_the_vm_off_and_restores_over_it(self):
        cluster = FakeCluster()
        cluster.config["scsi0"] = "local-zfs:vm-9002-disk-1,size=5G"
        outcome = self.run_session(cluster, {"restore_mode": "in_place", "force": True})
        self.assertEqual(cluster.api.started[0][0], f"/nodes/{NODE}/qemu/9002/status/stop")
        path, params = cluster.api.started[1]
        self.assertEqual((path, params["vmid"], params["force"]), (f"/nodes/{NODE}/qemu", "9002", 1))
        self.assertNotIn("unique", params)
        config = next(iter(cluster.shell_.written.values()))["guest_config"].splitlines()
        self.assertIn("name: web", config)
        self.assertIn("#qmdump#map:scsi0:drive-scsi0:local-zfs:raw:", config)
        self.assertEqual((outcome["target_vm_id"], outcome["identity_preserved"]), ("9002", True))

    def test_an_in_place_restore_leaves_a_physical_disk_as_it_is(self):
        self.add_physical_disk()
        cluster = FakeCluster()
        cluster.config["scsi10"] = "/dev/disk/by-id/ata-disk-1,size=5G"
        outcome = self.run_session(cluster, {"restore_mode": "in_place", "force": True})
        run = next(iter(cluster.shell_.written.values()))
        self.assertNotIn("drive-scsi10", run["disks"])
        config = run["guest_config"].splitlines()
        self.assertNotIn("#qmdump#map:scsi10:drive-scsi10::raw:", config)
        self.assertIn("scsi10: /dev/disk/by-id/ata-disk-1,size=5G", config)
        self.assertIn(
            "scsi10 is a physical disk (/dev/disk/by-id/ata-disk-1); an in-place restore leaves it as it is, without "
            "the backup's data of it",
            outcome["warnings"],
        )
        self.assertEqual(outcome["disk_count"], 3)

    def test_a_new_vm_gets_a_physical_disk_as_a_volume(self):
        self.add_physical_disk()
        cluster = FakeCluster()
        outcome = self.run_session(cluster)
        run = next(iter(cluster.shell_.written.values()))
        self.assertEqual(run["disks"]["drive-scsi10"]["chain"], ["task-a/scsi10", "task-b/scsi10", "task-c/scsi10"])
        self.assertIn("#qmdump#map:scsi10:drive-scsi10:local-lvm:raw:", run["guest_config"].splitlines())
        self.assertIn(
            "scsi10 was a physical disk (/dev/disk/by-id/ata-disk-1); the new VM gets it as a volume on local-lvm",
            outcome["warnings"],
        )

    def test_the_session_records_that_it_starts_a_restore_before_it_starts_it(self):
        self.run_session(cluster := FakeCluster())
        runs = self.runs()
        self.assertEqual((runs[0]["state"], runs[0]["target_vmid"], runs[0]["created_vm"]), ("starting", "120", True))
        self.assertNotIn("upid", runs[0])
        self.assertEqual(runs[1]["upid"], UPID)
        self.assertEqual(len(cluster.api.started), 1)

    def starting(self, **changes) -> dict:
        return {
            "pve_run": {
                "kind": "restore",
                "state": "starting",
                "node": NODE,
                "target_vmid": "120",
                "created_vm": True,
                "ctime": point_ctime(BACKUPS["point-c"]),
                "run_id": "task-9",
                "storages": {"scsi0": "local-lvm"},
                "prepared_at": 1795000000.0,
                **changes,
            }
        }

    def test_a_restore_the_server_lost_while_starting_it_is_found_by_its_run_and_followed(self):
        cluster = FakeCluster()
        ctime = point_ctime(BACKUPS["point-c"])
        cluster.shell_.files[f"/run/abr/restore-9002-{ctime}.result.json"] = json.dumps(cluster.result).encode()
        other = "UPID:node1:0004B3A9:01C9F3E9:691F0A30:qmrestore:120:root@pam:"
        cluster.pve_tasks = [
            {"upid": other, "user": "root@pam", "starttime": 1795000001},
            {"upid": UPID, "user": "abr@pve!altastor-inv1", "starttime": 1795000002},
        ]
        cluster.task_logs[UPID] = ["INFO: run task-9: restore of backup/9002/qemu-1 to VM 120 from s3://backups/pve"]
        outcome = self.run_session(cluster, result=self.starting())
        self.assertEqual(cluster.api.started, [])
        self.assertEqual(cluster.api.followed, [{"upid": UPID, "log_start": 0}])
        self.assertEqual(
            cluster.task_queries,
            [{"vmid": "120", "typefilter": "qmrestore", "since": 1794999400, "source": "all", "limit": 100}],
        )
        self.assertEqual(outcome["target_vm_id"], "120")
        self.assertTrue(any("which this session started before the server stopped" in m for m in self.messages()))

    def test_a_restore_that_never_started_starts_again(self):
        cluster = FakeCluster()
        outcome = self.run_session(cluster, result=self.starting())
        self.assertEqual(cluster.api.started[0][0], f"/nodes/{NODE}/qemu")
        self.assertEqual(outcome["target_vm_id"], "120")
        self.assertTrue(any("never started" in m for m in self.messages()))

    def test_a_node_whose_nbd_devices_are_all_in_use_is_refused_before_the_start(self):
        for devices, message in (
            ("16\n16\n", "All 16 kernel NBD devices of node node1 are in use"),
            ("0\n0\n", "The nbd kernel module is not loaded on node node1"),
        ):
            with self.subTest(message=message):
                cluster = FakeCluster()
                cluster.shell_.nbd = devices
                with self.assertRaisesRegex(PveRestoreError, message):
                    self.run_session(cluster)
                self.assertEqual(cluster.api.started, [])

    def test_an_in_place_restore_that_failed_says_whether_the_vm_kept_its_disks(self):
        cluster = FakeCluster({"status": "stopped", "exitstatus": "command failed"})
        cluster.state = "stopped"
        cluster.config = {"memory": "128"}
        with self.assertRaisesRegex(PveRestoreError, "PVE had removed the disks of web \\(9002\\) before the restore"):
            self.run_session(cluster, {"restore_mode": "in_place", "force": True})
        self.assertEqual(len(cluster.api.started), 1)

    def test_an_in_place_restore_over_a_vm_with_another_name_is_refused(self):
        cluster = FakeCluster()
        cluster.config["name"] = "db"
        with self.assertRaisesRegex(PveRestoreError, "now named db, not web"):
            self.run_session(cluster, {"restore_mode": "in_place", "force": True})
        self.assertEqual(cluster.api.started, [])

    def test_what_pve_cannot_take_is_refused_before_anything_changes(self):
        for parameters, cluster_change, message in (
            ({"target_vm_name": "web restored"}, None, "cannot name a Proxmox VE VM"),
            ({"target_host_name": "node2"}, None, "Node node2 is offline"),
            ({"target_datastore_name": "nope"}, None, "Storage nope for disk efidisk0 does not hold VM disks"),
            ({"network_map": {"vmbr1": "vmbr9"}}, None, "bridge vmbr9, which node node1 does not have"),
            (
                {},
                lambda cluster: cluster.storages["local-lvm"].update(active=0),
                "local-lvm for disk efidisk0 is not active",
            ),
        ):
            with self.subTest(message=message):
                cluster = FakeCluster()
                if cluster_change:
                    cluster_change(cluster)
                with self.assertRaisesRegex(PveRestoreError, message):
                    self.run_session(cluster, parameters)
                self.assertEqual(cluster.api.started, [])


class InterruptedRestoreTests(unittest.TestCase):
    def test_a_restore_following_its_pve_task_runs_again_and_one_before_it_fails(self):
        records = {
            "starting": {
                "id": "starting",
                "status": "running",
                "task_type": "restore",
                "result": {"pve_run": {"state": "starting", "node": NODE}},
            },
            "following": {
                "id": "following",
                "status": "running",
                "task_type": "restore",
                "result": {"pve_run": {"upid": UPID, "node": NODE}},
            },
            "before-start": {"id": "before-start", "status": "running", "task_type": "restore", "result": {}},
        }
        persisted: dict[str, dict] = {}

        def persist(record):
            persisted[record["id"]] = record
            records[record["id"]] = record

        with patch.object(backup_catalog, "catalog_records", lambda collection: list(records.values())), patch.object(
            backup_catalog, "persist_task", persist
        ), patch.object(backup_catalog, "_start_interrupted_task_sweep", lambda tasks: None):
            with self.assertLogs("python.srv.services.backup_catalog", "WARNING"):
                backup_catalog.fail_interrupted_tasks()
        self.assertEqual((persisted["following"]["status"], persisted["before-start"]["status"]), ("queued", "failed"))
        self.assertIn("restore task", persisted["following"]["logs"][-1]["message"])
        self.assertIn("finishes the restore", persisted["following"]["logs"][-1]["message"])
        self.assertEqual(persisted["starting"]["status"], "queued")
        self.assertIn("looks for that task", persisted["starting"]["logs"][-1]["message"])


class RestoreContextTests(unittest.TestCase):
    def test_a_pve_volume_is_a_datastore_and_a_windows_drive_is_not_one(self):
        self.assertEqual(
            abr_v41._parse_datastore_path_parts("local-lvm:vm-9002-disk-1"), ("local-lvm", None, "vm-9002-disk-1")
        )
        self.assertEqual(
            abr_v41._parse_datastore_path_parts("local-dir:9002/vm-9002-disk-0.qcow2"),
            ("local-dir", None, "9002/vm-9002-disk-0.qcow2"),
        )
        self.assertEqual(abr_v41._parse_datastore_path_parts("C:\\VMs\\a.vhdx"), ("C:", "VMs", "a.vhdx"))

    def test_the_source_vm_of_a_pve_point_has_its_node_storage_firmware_and_adapters(self):
        RESOURCES_BY_HV.clear()
        self.addCleanup(RESOURCES_BY_HV.clear)
        manifest = _manifest("task-c", CHAIN[2][2])
        source = abr_v41._build_restore_source_vm({"id": "point-c", "inventory_id": "inv-1", "vm_id": "9002"}, manifest)
        self.assertEqual(
            (source["original_host"], source["original_datastore"], source["firmware"]), (NODE, "local-lvm", "efi")
        )
        self.assertEqual((source["cpu_count"], source["memory_mb"]), (2, 2048))
        self.assertEqual(
            [(nic["label"], nic["source_network"], nic["mac_address"]) for nic in source["nics"]],
            [("net0", "vmbr0", "BC:24:11:00:00:01"), ("net1", "vmbr1", "BC:24:11:00:00:02")],
        )


if __name__ == "__main__":
    unittest.main()
