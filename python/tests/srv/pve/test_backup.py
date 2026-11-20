# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import HYPERVISORS, RESOURCES_BY_HV, TASKS
from python.srv.core.task_executor import TaskStopRequested
from python.srv.plugins.pve import pve_backup
from python.srv.plugins.pve.pve import PveAccess
from python.srv.plugins.pve.pve_backup import (
    PveBackupError,
    TaskLog,
    build_run_file,
    disk_bitmaps,
    disk_outputs,
    drive_device,
    fleecing_storage,
    immutability_option,
    last_vzdump_task,
    node_repository,
    run_pve_backup,
    session_kind,
)
from python.srv.services import backup_catalog

UPID = "UPID:node1:0004B3A2:01C9F3E1:691F0A2B:vzdump:9002:abr@pve!altastor-inv1:"
ENV = {"ALTASTOR_PVE_PARALLEL_DISKS": "4", "ALTASTOR_PVE_RUN_FILE_MINUTES": "60"}

S3 = {
    "id": "repo-s3",
    "name": "S3",
    "type": "s3",
    "metadata": {
        "s3": {
            "bucket": "backups",
            "prefix": "pve",
            "endpoint_url": "http://s3.example.com:9000/",
            "region": "us-east-1",
            "access_key": "abr-dev",
            "secret_key": "s3-secret",
            "path_style": True,
        }
    },
}

CONFIG = {
    "name": "web",
    "scsi0": "local-lvm:vm-9002-disk-1,size=5G",
    "efidisk0": "local-lvm:vm-9002-disk-0,size=528K",
    "tpmstate0": "local-lvm:vm-9002-disk-2,size=4M",
    "scsi1": "local-lvm:vm-9002-disk-3,backup=0,size=1G",
    "ide2": "none,media=cdrom",
    "digest": "abc123",
}


def _disks() -> list[dict]:
    return [
        {
            "drive": "scsi0",
            "capacity_bytes": 5 << 30,
            "storage": "local-lvm",
            "datastore_path": "local-lvm:vm-9002-disk-1",
            "format": "raw",
        },
        {
            "drive": "efidisk0",
            "capacity_bytes": 528 << 10,
            "storage": "local-lvm",
            "datastore_path": "local-lvm:vm-9002-disk-0",
            "format": "raw",
        },
        {
            "drive": "tpmstate0",
            "capacity_bytes": 4 << 20,
            "storage": "local-lvm",
            "datastore_path": "local-lvm:vm-9002-disk-2",
            "format": "raw",
        },
    ]


class RepositoryTests(unittest.TestCase):
    def test_an_s3_repository_is_its_bucket_and_the_sessions_prefix(self):
        repository, secrets = node_repository(S3, "pve/web/task-1", "pve1.example.com")
        self.assertEqual(
            repository,
            {
                "mode": "s3",
                "folder": "backups",
                "path": "pve/web/task-1",
                "s3": {
                    "access_key": "abr-dev",
                    "path_style": True,
                    "verify_tls": True,
                    "endpoint": "http://s3.example.com:9000",
                    "region": "us-east-1",
                },
            },
        )
        self.assertEqual(secrets, {"secret_key": "s3-secret"})

    def test_an_s3_repository_without_its_own_key_cannot_be_used_from_a_node(self):
        profile_only = {
            **S3,
            "metadata": {"s3": {**S3["metadata"]["s3"], "access_key": "", "secret_key": "", "profile": "s3"}},
        }
        with self.assertRaisesRegex(PveBackupError, "cannot use this appliance's AWS profile"):
            node_repository(profile_only, "pve/web/task-1", "pve1.example.com")

    def test_an_smb_repository_is_its_share_the_chain_folder_and_the_session(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        smb = {
            "id": "repo-smb",
            "name": "NAS",
            "type": "smb",
            "path": str(root),
            "server": "nas",
            "share": "backups",
            "username": "CORP\\backup",
            "password": "share-secret",
        }
        repository, secrets = node_repository(smb, str(root / "web" / "task-1"), "pve1.example.com")
        self.assertEqual(
            repository,
            {
                "mode": "file",
                "folder": "web",
                "path": "task-1",
                "smb": {
                    "share": "//nas/backups",
                    "username": "backup",
                    "version": "3.1.1",
                    "seal": True,
                    "domain": "CORP",
                },
            },
        )
        self.assertEqual(secrets, {"smb_password": "share-secret"})

    def test_a_local_repository_this_appliance_shares_is_reached_at_its_address_toward_the_node(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        local = {
            "id": "repo-local",
            "name": "Local",
            "type": "smb",
            "path": str(root),
            "share": "abr-local",
            "username": "abr-share",
            "metadata": {"support_hyperv": True},
        }
        with patch("python.srv.plugins.hyperv.vhdx_tool.appliance_address_toward", return_value="backup.example.com"):
            repository, _secrets = node_repository(local, str(root / "web" / "task-1"), "pve1.example.com")
        self.assertEqual(repository["smb"]["share"], "//backup.example.com/abr-local")

    def test_a_folder_of_this_appliance_alone_is_refused(self):
        local = {"id": "repo-local", "name": "Local-Repo-1", "type": "local", "path": "/srv/backups"}
        with self.assertRaisesRegex(
            PveBackupError, 'PVE VMs need an S3 or SMB repository.*abr repo share add --name "Local-Repo-1"'
        ):
            node_repository(local, "/srv/backups/web/task-1", "pve1.example.com")

    def test_immutability_is_passed_as_days_or_a_date(self):
        self.assertIsNone(immutability_option(S3))
        with patch.object(
            pve_backup, "disk_tool_immutability_args", return_value=["--immutable", "governance", "--retention", "7"]
        ):
            self.assertEqual(immutability_option(S3), {"mode": "governance", "retention_days": 7})
        with patch.object(
            pve_backup,
            "disk_tool_immutability_args",
            return_value=["--immutable", "compliance", "--retention-till", "2027-01-01T00:00:00Z"],
        ):
            self.assertEqual(immutability_option(S3), {"mode": "compliance", "retention_till": "2027-01-01T00:00:00Z"})


class RunFileTests(unittest.TestCase):
    def test_bitmaps_keep_a_disk_the_previous_backup_had_at_the_same_size(self):
        previous = {
            "disks": [
                {"drive": "scsi0", "capacity_bytes": 5 << 30},
                {"drive": "efidisk0", "capacity_bytes": 1},
                {"drive": "tpmstate0", "capacity_bytes": 4 << 20},
            ]
        }
        self.assertEqual(
            disk_bitmaps(_disks(), previous, True), {"scsi0": "use", "efidisk0": "new", "tpmstate0": "new"}
        )
        self.assertEqual(
            disk_bitmaps(_disks(), previous, False), {"scsi0": "new", "efidisk0": "new", "tpmstate0": "new"}
        )
        self.assertEqual(disk_bitmaps(_disks(), None, True)["scsi0"], "new")

    def test_the_run_file_names_each_disk_by_its_drive_and_carries_only_given_secrets(self):
        now = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
        with patch.dict("os.environ", ENV):
            run = build_run_file(
                run_id="task-1",
                task_user="abr@pve!altastor-inv1",
                storage="abr",
                vmid="9002",
                repository={"mode": "s3", "folder": "b", "path": "p"},
                secrets={"secret_key": "s", "smb_password": ""},
                disks=_disks(),
                bitmaps={"scsi0": "use", "efidisk0": "use", "tpmstate0": "new"},
                compression=5,
                immutability={"mode": "governance", "retention_days": 7},
                now=now,
            )
        self.assertEqual((run["format"], run["version"], run["kind"], run["vmid"]), ("abr-pve-run", 1, "backup", 9002))
        self.assertEqual((run["created"], run["expires"]), ("2026-11-20T10:00:00Z", "2026-11-20T11:00:00Z"))
        self.assertEqual(
            run["options"],
            {"compression": 5, "parallel_disks": 4, "immutability": {"mode": "governance", "retention_days": 7}},
        )
        self.assertEqual(
            run["disks"],
            {
                "drive-scsi0": {"name": "scsi0", "bitmap": "use", "size": 5 << 30},
                "drive-efidisk0": {"name": "efidisk0", "bitmap": "use", "size": 528 << 10},
                "drive-tpmstate0-backup": {"name": "tpmstate0", "bitmap": "new", "size": 4 << 20},
            },
        )
        self.assertEqual(run["secrets"], {"secret_key": "s"})
        self.assertEqual(drive_device("tpmstate0"), "drive-tpmstate0-backup")

    def test_fleecing_goes_to_the_storage_asked_for_or_of_the_vms_disks(self):
        storages = [
            {"storage": "local-zfs", "active": 1},
            {"storage": "local-lvm", "active": 1},
            {"storage": "nfs", "active": 0},
        ]
        self.assertEqual(fleecing_storage(storages, _disks()), "local-lvm")
        self.assertEqual(fleecing_storage(storages, [{"storage": "ceph"}]), "local-zfs")
        self.assertEqual(fleecing_storage(storages, _disks(), "local-zfs"), "local-zfs")
        with self.assertRaisesRegex(PveBackupError, "Fleecing storage nfs is not an active storage"):
            fleecing_storage(storages, _disks(), "nfs")
        with self.assertRaisesRegex(PveBackupError, "no active storage for VM disks"):
            fleecing_storage([], [])


class TaskLogTests(unittest.TestCase):
    def test_progress_counts_each_disk_by_its_size_and_the_log_says_what_happened(self):
        events: list[tuple] = []
        log = TaskLog(
            {"drive-scsi0": 300, "drive-efidisk0": 100},
            lambda stage, progress, message, **extra: events.append((progress, message, extra)),
        )
        for line in (
            "INFO: scsi0: dirty-bitmap status: OK (4.0 MiB of 5.0 GiB dirty)",
            "INFO: drive-scsi0: incremental backup to scsi0, bitmap reuse",
            "INFO: drive-scsi0: 50%",
            "INFO: drive-efidisk0: full backup efidisk0, 540672 bytes read, 4553 stored",
            "ERROR: drive-scsi1: the share went away",
            "INFO: nothing of ours",
        ):
            log.line(line)
        self.assertEqual(
            [(progress, message) for progress, message, _extra in events],
            [
                (15, "scsi0: dirty bitmap OK (4.0 MiB of 5.0 GiB dirty)"),
                (15, "drive-scsi0: incremental backup, PVE bitmap reuse"),
                (42, "drive-scsi0: 50%"),
                (60, "drive-efidisk0: full backup done, 528.0 KiB read, 4.4 KiB stored"),
                (60, "ERROR: drive-scsi1: the share went away"),
            ],
        )
        self.assertEqual(events[2][2], {"log_entry": False})
        self.assertEqual(events[4][2], {"log_level": "warn"})
        self.assertEqual(log.lines, 6)


def _result(kinds: dict[str, tuple[str, str, str]], status: str = "ok") -> dict:
    disks = {}
    for drive, (requested, granted, kind) in kinds.items():
        disks[drive_device(drive)] = {
            "name": drive,
            "size": {"scsi0": 5 << 30, "efidisk0": 528 << 10, "tpmstate0": 4 << 20}[drive],
            "bitmap_requested": requested,
            "bitmap_granted": granted,
            "bitmap_name": "snapshot-access:abr",
            "kind": kind,
            "status": "ok",
            "tool": {"raw_data_size": 4096, "stored_data_size": 100, "sector_size": 512, "physical_sector_size": 4096},
        }
    return {
        "format": "abr-pve-result",
        "run_id": "task-1",
        "status": status,
        "error": None if status == "ok" else "backup of drive-scsi0 failed: the share went away",
        "warnings": [],
        "archive": "backup/9002/qemu-1795130093",
        "provider_version": "0.1.0",
        "guest_config": "name: web\n",
        "firewall_config": None,
        "disks": disks,
    }


class ResultTests(unittest.TestCase):
    def test_each_disk_entry_says_where_its_backup_is_and_whether_it_is_full(self):
        result = _result(
            {
                "scsi0": ("use", "reuse", "incremental"),
                "efidisk0": ("use", "reuse", "incremental"),
                "tpmstate0": ("new", "new", "full"),
            }
        )
        outputs = disk_outputs(result, _disks(), S3, "pve/web/task-1")
        self.assertEqual(
            [(o["drive"], o["store_mode"], o["abr_backup_name"]) for o in outputs],
            [
                ("scsi0", "incremental", "scsi0"),
                ("efidisk0", "incremental", "efidisk0"),
                ("tpmstate0", "full", "tpmstate0"),
            ],
        )
        self.assertEqual(
            {
                key: outputs[0][key]
                for key in (
                    "abr_path",
                    "storage_mode",
                    "change_id",
                    "previous_change_id",
                    "logical_bytes",
                    "stored_bytes",
                    "physical_sector_size",
                )
            },
            {
                "abr_path": "pve/web/task-1",
                "storage_mode": "s3",
                "change_id": "snapshot-access:abr",
                "previous_change_id": "snapshot-access:abr",
                "logical_bytes": 4096,
                "stored_bytes": 100,
                "physical_sector_size": 4096,
            },
        )
        self.assertEqual(outputs[2]["previous_change_id"], "")
        self.assertEqual(
            session_kind(outputs, True, "StandardIncremental"), ("Incremental", "StandardIncremental", True)
        )

    def test_a_session_whose_every_disk_is_full_is_a_full(self):
        outputs = [{"store_mode": "full"}, {"store_mode": "full"}]
        self.assertEqual(
            session_kind(outputs, True, "StandardIncremental"), ("ActiveFull", "IncrementalFallbackToFull", False)
        )
        self.assertEqual(session_kind(outputs, False, "FirstRun"), ("ActiveFull", "FirstRun", False))

    def test_a_disk_pve_did_not_back_up_fails_the_session(self):
        result = _result({"scsi0": ("use", "reuse", "incremental")})
        with self.assertRaisesRegex(
            PveBackupError, "Disk efidisk0 has no backup in the provider's result: PVE did not offer it"
        ):
            disk_outputs(result, _disks(), S3, "pve/web/task-1")

    def test_the_last_vzdump_run_of_a_vm_is_the_newest_that_succeeded(self):
        def task(task_id: str, finished: str, ok: bool, vmid: str = "9002") -> dict:
            return {
                "id": task_id,
                "hypervisor_id": "inv-1",
                "result": {"pve_run": {"vmid": vmid, "vzdump_ok": ok, "finished_at": finished}},
            }

        records = [
            task("a", "2026-11-20T01:00:00Z", True),
            task("b", "2026-11-20T03:00:00Z", False),
            task("c", "2026-11-20T02:00:00Z", True),
            task("d", "2026-11-20T04:00:00Z", True, "9003"),
        ]
        with patch.object(pve_backup, "catalog_records", return_value=records):
            self.assertEqual(last_vzdump_task("inv-1", "9002", "now"), "c")
            self.assertEqual(last_vzdump_task("inv-1", "9002", "c"), "a")
            self.assertIsNone(last_vzdump_task("inv-2", "9002", "now"))


RESULT_FILE = "/run/abr/backup-9002.result.json"


class FakeShell:
    def __init__(self) -> None:
        self.address = "pve1.example.com"
        self.files: dict[str, bytes] = {}
        self.written: dict[str, dict] = {}
        self.removed: list[str] = []

    def make_directory(self, path, mode):
        self.files.setdefault(path + "/", b"")

    def write_file(self, path, data, mode=0o600):
        self.written[path] = json.loads(data)

    def read_file(self, path):
        return self.files.get(path)

    def remove_file(self, path):
        self.removed.append(path)
        return self.files.pop(path, None) is not None


class FakeApi:
    def __init__(self, cluster: "FakeCluster") -> None:
        self.cluster = cluster
        self.started: list[tuple[str, dict]] = []
        self.followed: list[dict] = []

    def get(self, path, **params):
        return [{"storage": "local-lvm", "active": 1}]

    def start_task(self, path, **params):
        """The provider writes the result file once vzdump runs."""
        self.started.append((path, params))
        if self.cluster.result is not None:
            self.cluster.shell_.files[RESULT_FILE] = json.dumps(self.cluster.result).encode()
        return UPID

    def wait_task(self, upid, *, on_log, should_stop, log_start=0):
        self.followed.append({"upid": upid, "log_start": log_start})
        for line in self.cluster.log:
            on_log(line)
        if self.cluster.stop_during_task:
            self.cluster.stopped_by_task = should_stop()
        return self.cluster.status


class FakeCluster:
    def __init__(self, result: dict | None, status: dict | None = None) -> None:
        self.access = PveAccess(
            host="pve1.example.com",
            token_id="abr@pve!altastor-inv1",
            token_secret="t",
            ssh_key="k",
            nodes=[{"name": "node1", "address": "pve1.example.com", "host_keys": []}],
        )
        self.result = result
        self.shell_ = FakeShell()
        self.api = FakeApi(self)
        self.log = ["INFO: drive-scsi0: 100%"]
        self.status = status or {"status": "stopped", "exitstatus": "OK"}
        self.stop_during_task = False
        self.stopped_by_task = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def locate_vm(self, vmid):
        return {"vmid": 9002, "node": "node1", "status": "running", "name": "web"}

    def vm_config(self, node, vmid):
        return dict(CONFIG)

    def shell(self, node):
        return self.shell_

    def get_service_type(self):
        return "cluster"


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.task_root = self.folder / "web" / "task-1"
        HYPERVISORS["inv-1"] = {"id": "inv-1", "kind": "proxmox-ve", "name": "PVE-Cluster"}
        RESOURCES_BY_HV["inv-1"] = {("virtual_machine", "9002"): {"external_id": "9002", "name": "web", "payload": {}}}
        self.addCleanup(HYPERVISORS.clear)
        self.addCleanup(RESOURCES_BY_HV.clear)
        self.previous: dict | None = None
        self.discarded: list[tuple] = []
        self.progress: list[dict] = []
        for target, value in (
            ("_resolve_repository", lambda task: S3),
            ("_resolve_backup_storage_layout", lambda **kw: (self.folder, self.task_root, "pve/web/task-1")),
            (
                "_previous_manifest",
                lambda **kw: (self.folder / "manifest.json" if self.previous else None, self.previous),
            ),
            ("ensure_package", lambda shell, node, package, progress: "0.1.0"),
            ("package_path", lambda: None),
            ("run_disk_tool", lambda args, timeout: None),
            ("apply_repository_immutability", lambda *args, **kw: None),
            ("make_durable", lambda *args, **kw: None),
            ("_discard_session", lambda *args: self.discarded.append(args) or ([], "")),
            (
                "_stopped",
                lambda *args: self.discarded.append(args[1:]) or "Session stopped; what it wrote was discarded",
            ),
            ("last_vzdump_task", lambda *args: None),
        ):
            self.enterContext(patch.object(pve_backup, target, value))
        self.enterContext(patch.dict("os.environ", ENV))

    def run_session(self, cluster: FakeCluster, parameters: dict | None = None, result: dict | None = None) -> dict:
        task = {
            "id": "task-1",
            "job_id": "job-1",
            "hypervisor_id": "inv-1",
            "resource_type": "virtual_machine",
            "resource_external_id": "9002",
            "parameters": parameters or {},
            "result": result or {},
        }
        with patch.object(pve_backup.PveNode, "from_record", return_value=cluster):
            return run_pve_backup(task, progress_callback=self.progress.append)

    def test_a_first_backup_writes_the_run_file_starts_vzdump_and_registers_a_full(self):
        cluster = FakeCluster(
            _result(
                {
                    "scsi0": ("new", "new", "full"),
                    "efidisk0": ("new", "new", "full"),
                    "tpmstate0": ("new", "new", "full"),
                }
            )
        )
        outcome = self.run_session(cluster)
        run = cluster.shell_.written["/run/abr/backup-9002.json"]
        self.assertEqual(sorted(run["disks"]), ["drive-efidisk0", "drive-scsi0", "drive-tpmstate0-backup"])
        self.assertEqual(
            (run["task_user"], run["repository"]["path"], run["secrets"]),
            ("abr@pve!altastor-inv1", "pve/web/task-1", {"secret_key": "s3-secret"}),
        )
        path, params = cluster.api.started[0]
        self.assertEqual(path, "/nodes/node1/vzdump")
        self.assertEqual(
            params,
            {
                "vmid": "9002",
                "storage": "abr",
                "mode": "snapshot",
                "remove": False,
                "fleecing": "enabled=1,storage=local-lvm",
                "notification-mode": "legacy-sendmail",
            },
        )
        self.assertEqual(
            (outcome["resolved_backup_type"], outcome["backup_mode"], outcome["hypervisor_kind"]),
            ("ActiveFull", "full", "proxmox-ve"),
        )
        self.assertEqual(outcome["pve_run"]["upid"], UPID)
        self.assertTrue(outcome["pve_run"]["vzdump_ok"])
        manifest = json.loads((self.task_root / "manifest.json").read_text())
        self.assertEqual(
            (manifest["task_id"], manifest["vm_config"]["guest_config"], manifest["pve"]["archive"]),
            ("task-1", "name: web\n", "backup/9002/qemu-1795130093"),
        )
        self.assertEqual(
            sorted(cluster.shell_.removed),
            [
                "/run/abr/backup-9002.json",
                "/run/abr/backup-9002.json",
                "/run/abr/backup-9002.log",
                "/run/abr/backup-9002.log",
                "/run/abr/backup-9002.result.json",
                "/run/abr/backup-9002.result.json",
            ],
        )
        self.assertTrue(
            any(
                isinstance(update.get("pve_run"), dict) and update["pve_run"].get("upid") == UPID
                for update in self.progress
            )
        )
        self.assertEqual(self.discarded, [])

    def test_an_incremental_keeps_the_bitmaps_and_the_tpm_state_is_full_within_it(self):
        self.previous = {
            "task_id": "task-0",
            "disks": [
                {"drive": "scsi0", "capacity_bytes": 5 << 30},
                {"drive": "efidisk0", "capacity_bytes": 528 << 10},
                {"drive": "tpmstate0", "capacity_bytes": 4 << 20},
            ],
        }
        cluster = FakeCluster(
            _result(
                {
                    "scsi0": ("use", "reuse", "incremental"),
                    "efidisk0": ("use", "reuse", "incremental"),
                    "tpmstate0": ("new", "new", "full"),
                }
            )
        )
        outcome = self.run_session(
            cluster, {"resolved_backup_type": "Incremental", "resolved_backup_reason": "StandardIncremental"}
        )
        run = cluster.shell_.written["/run/abr/backup-9002.json"]
        self.assertEqual(
            {device: disk["bitmap"] for device, disk in run["disks"].items()},
            {"drive-scsi0": "use", "drive-efidisk0": "use", "drive-tpmstate0-backup": "new"},
        )
        self.assertEqual((outcome["resolved_backup_type"], outcome["backup_mode"]), ("Incremental", "incremental"))
        self.assertEqual([disk["store_mode"] for disk in outcome["disks"]], ["incremental", "incremental", "full"])

    def test_lost_bitmaps_make_the_session_a_full_and_say_why(self):
        self.previous = {
            "task_id": "task-0",
            "disks": [
                {"drive": "scsi0", "capacity_bytes": 5 << 30},
                {"drive": "efidisk0", "capacity_bytes": 528 << 10},
            ],
        }
        cluster = FakeCluster(
            _result(
                {
                    "scsi0": ("use", "new", "full"),
                    "efidisk0": ("use", "new", "full"),
                    "tpmstate0": ("new", "new", "full"),
                }
            )
        )
        outcome = self.run_session(cluster, {"resolved_backup_type": "Incremental"})
        self.assertEqual(
            (outcome["resolved_backup_type"], outcome["resolved_backup_reason"]),
            ("ActiveFull", "IncrementalFallbackToFull"),
        )
        self.assertTrue(
            any("no longer had the dirty bitmap of scsi0, efidisk0" in warning for warning in outcome["warnings"])
        )

    def test_bitmaps_another_chain_moved_on_make_every_disk_new(self):
        self.previous = {"task_id": "task-0", "disks": [{"drive": "scsi0", "capacity_bytes": 5 << 30}]}
        cluster = FakeCluster(
            _result(
                {
                    "scsi0": ("new", "new", "full"),
                    "efidisk0": ("new", "new", "full"),
                    "tpmstate0": ("new", "new", "full"),
                }
            )
        )
        with patch.object(pve_backup, "last_vzdump_task", lambda *args: "task-of-another-job"):
            self.run_session(cluster, {"resolved_backup_type": "Incremental"})
        run = cluster.shell_.written["/run/abr/backup-9002.json"]
        self.assertEqual({disk["bitmap"] for disk in run["disks"].values()}, {"new"})

    def test_a_failed_vzdump_run_says_why_and_discards_the_session(self):
        cluster = FakeCluster(
            _result({"scsi0": ("new", "new", "full")}, status="error"),
            {"status": "stopped", "exitstatus": "job errors"},
        )
        cluster.log = [
            "ERROR: Backup of VM 9002 failed - backup of drive-scsi0 failed: the share went away",
            "TASK ERROR: job errors",
        ]
        with self.assertLogs("python.srv.plugins.pve.pve_backup", "ERROR"):
            with self.assertRaisesRegex(PveBackupError, r"failed: backup of drive-scsi0 failed: the share went away$"):
                self.run_session(cluster)
        self.assertEqual(len(self.discarded), 1)
        self.assertIn("/run/abr/backup-9002.result.json", cluster.shell_.removed)

    def test_a_provider_that_left_no_result_fails_the_session(self):
        cluster = FakeCluster(None)
        with self.assertLogs("python.srv.plugins.pve.pve_backup", "ERROR"):
            with self.assertRaisesRegex(PveBackupError, "the provider left no result file for this run"):
                self.run_session(cluster)

    def test_a_stop_stops_the_task_and_discards_the_session(self):
        cluster = FakeCluster(
            _result({"scsi0": ("new", "new", "full")}, status="error"),
            {"status": "stopped", "exitstatus": "interrupted by signal"},
        )
        cluster.stop_during_task = True
        with patch.object(pve_backup, "cancel_requested", return_value=True):
            with self.assertRaisesRegex(TaskStopRequested, "Session stopped; what it wrote was discarded"):
                self.run_session(cluster)
        self.assertTrue(cluster.stopped_by_task)
        self.assertEqual(len(self.discarded), 1)

    def test_a_run_that_ended_well_is_kept_when_the_stop_came_too_late(self):
        cluster = FakeCluster(
            _result(
                {
                    "scsi0": ("new", "new", "full"),
                    "efidisk0": ("new", "new", "full"),
                    "tpmstate0": ("new", "new", "full"),
                }
            )
        )
        with patch.object(pve_backup, "cancel_requested", return_value=True):
            outcome = self.run_session(cluster)
        self.assertTrue(outcome["stop_requested_after_completion"])
        self.assertEqual(self.discarded, [])

    def test_a_session_the_server_lost_follows_its_task_again(self):
        cluster = FakeCluster(
            _result(
                {
                    "scsi0": ("new", "new", "full"),
                    "efidisk0": ("new", "new", "full"),
                    "tpmstate0": ("new", "new", "full"),
                }
            )
        )
        earlier = {
            "pve_run": {"upid": UPID, "node": "node1", "vmid": "9002", "run_id": "task-1", "log_lines": 120}
        }
        cluster.shell_.files[RESULT_FILE] = json.dumps(cluster.result).encode()
        outcome = self.run_session(cluster, result=earlier)
        self.assertEqual(cluster.api.started, [])
        self.assertEqual(cluster.api.followed, [{"upid": UPID, "log_start": 120}])
        self.assertNotIn("/run/abr/backup-9002.json", cluster.shell_.written)
        self.assertEqual(outcome["pve_run"]["log_lines"], 121)

    def test_a_disk_level_backup_is_refused(self):
        task = {"id": "t", "hypervisor_id": "inv-1", "resource_type": "disk", "resource_external_id": "9002:scsi0"}
        with self.assertRaisesRegex(PveBackupError, "Proxmox VE backs up whole VMs"):
            run_pve_backup(task)

    def test_a_failure_adds_the_error_line_that_tells_more(self):
        status = {"status": "stopped", "exitstatus": "job errors"}
        message = pve_backup._failure(
            "9002", "n1", status, None, ["cannot setup backup access without fleecing", "job errors"]
        )
        self.assertEqual(
            message,
            "vzdump of VM 9002 on node n1 failed: job errors; "
            "the task log says: cannot setup backup access without fleecing",
        )


class InterruptedSessionTests(unittest.TestCase):
    def test_a_session_following_a_vzdump_task_runs_again_and_others_fail(self):
        records = {
            "pve": {
                "id": "pve",
                "status": "running",
                "task_type": "backup",
                "result": {"pve_run": {"upid": UPID, "node": "node1"}},
            },
            "pve-before-start": {"id": "pve-before-start", "status": "running", "task_type": "backup", "result": {}},
            "done": {"id": "done", "status": "completed", "task_type": "backup", "result": {"pve_run": {"upid": UPID}}},
        }
        persisted: dict[str, dict] = {}

        def persist(record):
            persisted[record["id"]] = record
            records[record["id"]] = record

        with patch.object(backup_catalog, "catalog_records", lambda collection: list(records.values())), patch.object(
            backup_catalog, "persist_task", persist
        ), patch.object(backup_catalog, "_start_interrupted_task_sweep", lambda tasks: None):
            with self.assertLogs("python.srv.services.backup_catalog", "WARNING"):
                failed = backup_catalog.fail_interrupted_tasks()
        self.assertEqual(failed, 1)
        self.assertEqual(persisted["pve"]["status"], "queued")
        self.assertIn("follows that task again", persisted["pve"]["logs"][-1]["message"])
        self.assertEqual(persisted["pve-before-start"]["status"], "failed")
        self.assertNotIn("done", persisted)


if __name__ == "__main__":
    unittest.main()
