# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
from typing import Any
import unittest
import uuid
from unittest.mock import patch

os.environ["ABR_ENABLE_MOCK_INVENTORY"] = "1"

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    venv_lib = repo_root / ".venv" / "lib"
    for site_packages in sorted(venv_lib.glob("python*/site-packages")):
        sys.path.insert(0, str(site_packages))
from fastapi.testclient import TestClient

from python.srv.api.auth_guard import require_session
from python.srv.backup_server import MAX_CONCURRENT_BACKUPS, WORKER_COUNT, app
from python.srv.core.mongo_db_wrapper import BACKUPS, HYPERVISORS, REPOSITORIES, RESOURCES_BY_HV
from python.srv.services import abr_v41


class BackupServerApiFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        reachable = patch(
            "python.srv.services.abr_v41.assert_inventory_registrable", return_value=None
        )
        reachable.start()
        self.addCleanup(reachable.stop)
        app.dependency_overrides[require_session] = lambda: {"user_id": "test-user", "user_name": "test-user"}
        self.client_cm = TestClient(app)
        self.client = self.client_cm.__enter__()
        self._repo_root = tempfile.mkdtemp(prefix="abr-test-repos-")

    def tearDown(self) -> None:
        self.client_cm.__exit__(None, None, None)
        app.dependency_overrides.pop(require_session, None)
        shutil.rmtree(self._repo_root, ignore_errors=True)

    def test_end_to_end_mock_backup_flow(self) -> None:
        inventory = self._create_mock_inventory("test-e2e-hv")
        repository = self._create_repository("e2e-repo", "local", path="/backup/e2e")
        job = self._create_backup_job(
            name="e2e-job",
            inventory_id=inventory["id"],
            vm_names=["vm-1", "vm-2"],
            repo_id=repository["id"],
            task_parameters={"duration_seconds": 0.2},
        )

        started = self._start_job(job["id"])
        session_ids = [item["id"] for item in started["items"]]
        self.assertEqual(len(session_ids), 2)

        completed_sessions = [self._wait_for_session(session_id) for session_id in session_ids]
        for session in completed_sessions:
            self.assertEqual(session["state"], "completed")
            self.assertEqual(session["type"], "backup")
            self.assertIn(session["resolved_backup_type"], {"ActiveFull", "Incremental", "SyntheticFull"})

        # A backup session names the VM it protected. Without this the console
        # had only the job name for its "Virtual machine" column and showed the
        # job name there instead of the machine.
        self.assertEqual(
            sorted(session["vm_name"] for session in completed_sessions),
            ["vm-1", "vm-2"],
        )
        for session in completed_sessions:
            self.assertIsNone(session["source_vm_name"])

    def test_queue_respects_worker_capacity(self) -> None:
        repository = self._create_repository("capacity-repo", "local", path="/backup/capacity")

        vm_count = 6
        session_ids: list[str] = []
        for index in range(vm_count):
            inventory = self._create_mock_inventory(f"test-capacity-hv-{index}")
            job = self._create_backup_job(
                name=f"capacity-job-{index}",
                inventory_id=inventory["id"],
                vm_names=["vm-1"],
                repo_id=repository["id"],
                task_parameters={"duration_seconds": 1.2},
            )
            started = self._start_job(job["id"])
            session_ids.append(started["items"][0]["id"])

        expected_running = min(WORKER_COUNT, MAX_CONCURRENT_BACKUPS, vm_count)
        expected_queued = vm_count - expected_running
        self._wait_for_status_mix(session_ids, expected_running=expected_running, expected_queued=expected_queued)

        finished = [self._wait_for_session(session_id, timeout=15.0) for session_id in session_ids]
        self.assertTrue(all(session["state"] == "completed" for session in finished))

    def test_verbose_end_to_end_trace(self) -> None:
        self._log("starting verbose end-to-end flow")

        repository = self._create_repository("verbose-repo", "local", path="/backup/verbose")

        durations = [0.8, 1.0, 1.2, 1.4, 1.1]
        session_ids: list[str] = []
        for index, duration_seconds in enumerate(durations):
            inventory = self._create_mock_inventory(f"test-verbose-hv-{index}")
            self._log(f"registered mock inventory: {inventory['id']}")
            job = self._create_backup_job(
                name=f"verbose-job-{index}",
                inventory_id=inventory["id"],
                vm_names=["vm-1"],
                repo_id=repository["id"],
                task_parameters={"duration_seconds": duration_seconds},
            )
            started = self._start_job(job["id"])
            session_id = started["items"][0]["id"]
            session_ids.append(session_id)
            self._log(f"created session: id={session_id} duration={duration_seconds:.1f}s")

        self._log("waiting for queue saturation")
        expected_running = min(WORKER_COUNT, MAX_CONCURRENT_BACKUPS, len(session_ids))
        expected_queued = len(session_ids) - expected_running
        self._wait_for_status_mix(session_ids, expected_running=expected_running, expected_queued=expected_queued)
        self._log("queue saturation observed")

        completed = self._trace_sessions_until_complete(session_ids, timeout=15.0)
        self.assertTrue(all(session["state"] == "completed" for session in completed.values()))
        self._log("all sessions completed")

    def test_job_can_store_weekly_schedule(self) -> None:
        inventory = self._create_mock_inventory("test-schedule-hv")
        repository = self._create_repository("schedule-repo", "local", path="/backup/schedule")

        response = self.client.post(
            "/api/v1/jobs",
            json={
                "name": "weekly-schedule-job",
                "vms": ["vm-1"],
                "inventory_id": inventory["id"],
                "repo_id": repository["id"],
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "schedule": {
                    "schedule_type": "weekly",
                    "start_time": "22:30",
                    "weekdays": ["monday", "wednesday", "friday"],
                    "timezone": "America/New_York",
                },
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        job = response.json()

        self.assertEqual(job["schedule"]["type"], "weekly")
        self.assertEqual(job["schedule"]["time"], "22:30")
        self.assertEqual(job["schedule"]["days"], ["monday", "wednesday", "friday"])
        self.assertEqual(job["schedule_summary"], "Weekly on Mon, Wed, Fri at 22:30 (America/New_York)")

        listed = self.client.get("/api/v1/jobs")
        self.assertEqual(listed.status_code, 200)
        items = listed.json()["items"]
        self.assertTrue(any(item["id"] == job["id"] and item["schedule"]["type"] == "weekly" for item in items))

    def test_job_rejects_invalid_schedule_field_combinations(self) -> None:
        inventory = self._create_mock_inventory("test-invalid-schedule-hv")
        repository = self._create_repository("invalid-schedule-repo", "local", path="/backup/invalid-schedule")

        response = self.client.post(
            "/api/v1/jobs",
            json={
                "name": "invalid-schedule-job",
                "vms": ["vm-1"],
                "inventory_id": inventory["id"],
                "repo_id": repository["id"],
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "schedule": {
                    "schedule_type": "interval",
                    "interval_minutes": 30,
                    "start_time": "09:00",
                },
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_can_create_abstract_repository_and_list_it(self) -> None:
        repository = self._create_repository("local1", "local", path="/backup/altastor")

        response = self.client.get("/api/v1/repositories")
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertTrue(any(item["id"] == repository["id"] and item["name"] == "local1" for item in items))
        listed = next(item for item in items if item["id"] == repository["id"])
        self.assertEqual(listed["location"], repository["path"])
        self.assertIn("updated_at", listed)

    def test_repository_test_validates_local_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = self._create_repository("local-test", "local", path=tmpdir)
            response = self.client.post(f"/api/v1/repositories/{repository['id']}/verify")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["location"], tmpdir)
        self.assertTrue(any(check["name"] == "filesystem_probe" and check["status"] == "ok" for check in payload["checks"]))

    def test_completed_backup_is_cataloged_with_origin_and_repository_placement(self) -> None:
        repository = self._create_repository("local1", "local", path="/backup/altastor")
        inventory = self._create_mock_inventory("test-backup-catalog-hv")

        job = self._create_backup_job(
            name="vm-101-backup",
            inventory_id=inventory["id"],
            vm_names=["vm-1"],
            repo_id=repository["id"],
            task_parameters={"duration_seconds": 0.1},
        )
        started = self._start_job(job["id"])
        session_id = started["items"][0]["id"]
        self._wait_for_session(session_id, timeout=5.0)

        backups_response = self.client.get("/api/v1/backups")
        self.assertEqual(backups_response.status_code, 200)
        backups = [item for item in backups_response.json()["items"] if item["job"]["id"] == job["id"]]
        self.assertEqual(len(backups), 1)

        backup = backups[0]
        restore_point = backup["restore_points"][0]
        self.assertEqual(backup["job"]["id"], job["id"])
        self.assertEqual(backup["job"]["name"], "vm-101-backup")
        self.assertEqual(backup["inventory"]["id"], inventory["id"])
        self.assertEqual(backup["vm"]["name"], "vm-1")
        self.assertEqual(backup["repository"]["id"], repository["id"])
        self.assertEqual(backup["repository"]["name"], repository["name"])
        self.assertEqual(restore_point["execution_type"], "ActiveFull")
        self.assertEqual(restore_point["execution_reason"], "FirstRun")

    def test_backup_management_filter_verify_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backup_root = pathlib.Path(tmpdir) / "backup-1"
            backup_root.mkdir()
            manifest_path = backup_root / "manifest.json"
            disk_path = backup_root / "01-tstvm.vmdk"
            manifest_path.write_text('{"vm_name": "tstvm"}', encoding="utf-8")
            disk_path.write_bytes(b"backup payload")
            BACKUPS["backup-1"] = {
                "id": "backup-1",
                "status": "available",
                "backup_mode": "full",
                "source": {
                    "hypervisor_id": "hv-1",
                    "resource_type": "virtual_machine",
                    "resource_external_id": "vm-1",
                    "vm_name": "tstvm",
                    "vm_external_id": "vm-1",
                },
                "origin": {
                    "task_id": "task-1",
                    "job_id": "job-1",
                    "job_name": "blackdog-tstvm",
                    "created_at": "2026-05-13T07:02:19+00:00",
                },
                "artifact": {
                    "manifest_path": str(manifest_path),
                    "backup_root": str(backup_root),
                    "disk_count": 1,
                    "disks": [{"destination_path": str(disk_path)}],
                },
                "placements": [
                    {
                        "status": "available",
                        "present": True,
                        "created_at": "2026-05-13T07:02:19+00:00",
                        "manifest_path": str(manifest_path),
                        "backup_root": str(backup_root),
                        "disk_paths": [str(disk_path)],
                        "repository_id": "repo-1",
                        "repository_name": "blackdog-local",
                    }
                ],
                "created_at": "2026-05-13T07:02:19+00:00",
            }
            BACKUPS["backup-2"] = {
                "id": "backup-2",
                "status": "available",
                "source": {"hypervisor_id": "hv-1", "vm_external_id": "vm-2"},
                "origin": {"job_id": "job-2", "job_name": "other-job"},
                "placements": [{"repository_id": "repo-2", "repository_name": "other-repo"}],
                "created_at": "2026-05-13T07:03:19+00:00",
            }

            listed = self.client.get("/api/v1/backups")
            self.assertEqual(listed.status_code, 200)
            matches = [
                item
                for item in listed.json()["items"]
                if item["job"]["id"] == "job-1" and item["vm"]["id"] == "vm-1" and item["repository"]["id"] == "repo-1"
            ]
            self.assertEqual(len(matches), 1)
            backup_chain_id = matches[0]["id"]
            self.assertEqual(matches[0]["restore_point_count"], 1)

            info = self.client.get(f"/api/v1/backups/{backup_chain_id}")
            self.assertEqual(info.status_code, 200)
            self.assertEqual(info.json()["repository"]["name"], "blackdog-local")
            self.assertEqual(info.json()["restore_points"][0]["id"], "backup-1")

            verify = self.client.post(f"/api/v1/backups/{backup_chain_id}/verify")
            self.assertEqual(verify.status_code, 200)
            self.assertEqual(verify.json()["result"], "valid")
            self.assertEqual(verify.json()["errors"], [])

            verify_session_id = verify.json()["session_id"]
            verify_session = self.client.get(f"/api/v1/sessions/{verify_session_id}")
            self.assertEqual(verify_session.status_code, 200)
            self.assertEqual(verify_session.json()["type"], "verify")
            self.assertEqual(verify_session.json()["state"], "completed")
            self.assertEqual(verify_session.json()["progress"], 100)
            self.assertEqual(verify_session.json()["verify"]["result"], "valid")

            started_verify = self.client.post(
                f"/api/v1/backups/{backup_chain_id}/verify", params={"wait": "false"}
            )
            self.assertEqual(started_verify.status_code, 200)
            self.assertEqual(started_verify.json()["result"], "running")
            followed = self._wait_for_session(started_verify.json()["session_id"])
            self.assertEqual(followed["state"], "completed")
            self.assertEqual(followed["verify"]["result"], "valid")

            deleted = self.client.delete(f"/api/v1/backups/{backup_chain_id}")
            self.assertEqual(deleted.status_code, 200)
            self.assertTrue(deleted.json()["deleted"])
            self.assertNotIn("backup-1", BACKUPS)
            self.assertFalse(backup_root.exists())

    def test_backup_segment_scoped_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:

            def make_point(
                point_id: str, *, full: bool, created_at: str, parent_point_id: str | None
            ) -> tuple[dict[str, Any], pathlib.Path]:
                point_root = pathlib.Path(tmpdir) / point_id
                point_root.mkdir()
                manifest_path = point_root / "manifest.json"
                disk_path = point_root / "01-segvm.vmdk"
                manifest_path.write_text('{"vm_name": "segvm"}', encoding="utf-8")
                disk_path.write_bytes(b"backup payload")
                point = {
                    "id": point_id,
                    "status": "available",
                    "execution_type": "ActiveFull" if full else "Incremental",
                    "parent_point_id": parent_point_id,
                    "source": {
                        "hypervisor_id": "hv-1",
                        "resource_type": "virtual_machine",
                        "resource_external_id": "vm-seg",
                        "vm_name": "segvm",
                        "vm_external_id": "vm-seg",
                    },
                    "origin": {
                        "task_id": f"task-{point_id}",
                        "job_id": "job-seg",
                        "job_name": "seg-job",
                        "created_at": created_at,
                    },
                    "artifact": {
                        "manifest_path": str(manifest_path),
                        "backup_root": str(point_root),
                        "disk_count": 1,
                        "disks": [{"destination_path": str(disk_path)}],
                    },
                    "placements": [
                        {
                            "status": "available",
                            "present": True,
                            "created_at": created_at,
                            "manifest_path": str(manifest_path),
                            "backup_root": str(point_root),
                            "disk_paths": [str(disk_path)],
                            "repository_id": "repo-seg",
                            "repository_name": "seg-repo",
                        }
                    ],
                    "created_at": created_at,
                }
                return point, point_root

            seg_a_full, seg_a_full_root = make_point(
                "seg-a-full", full=True, created_at="2026-05-01T00:00:00+00:00", parent_point_id=None
            )
            seg_a_inc, seg_a_inc_root = make_point(
                "seg-a-inc", full=False, created_at="2026-05-02T00:00:00+00:00", parent_point_id="seg-a-full"
            )
            seg_b_full, seg_b_full_root = make_point(
                "seg-b-full", full=True, created_at="2026-06-01T00:00:00+00:00", parent_point_id=None
            )
            seg_b_inc, seg_b_inc_root = make_point(
                "seg-b-inc", full=False, created_at="2026-06-02T00:00:00+00:00", parent_point_id="seg-b-full"
            )
            for point in (seg_a_full, seg_a_inc, seg_b_full, seg_b_inc):
                BACKUPS[point["id"]] = point

            listed = self.client.get("/api/v1/backups")
            self.assertEqual(listed.status_code, 200)
            matches = [
                item
                for item in listed.json()["items"]
                if item["job"]["id"] == "job-seg"
                and item["vm"]["id"] == "vm-seg"
                and item["repository"]["id"] == "repo-seg"
            ]
            self.assertEqual(len(matches), 1)
            chain_id = matches[0]["id"]
            self.assertEqual(matches[0]["restore_point_count"], 4)

            # Deleting an unknown segment 404s and leaves everything untouched.
            missing = self.client.delete(f"/api/v1/backups/{chain_id}", params={"segment": "not-a-real-segment"})
            self.assertEqual(missing.status_code, 404)
            self.assertIn("seg-a-full", BACKUPS)
            self.assertIn("seg-b-full", BACKUPS)

            # Deleting segment A removes only its full backup + incremental...
            deleted = self.client.delete(f"/api/v1/backups/{chain_id}", params={"segment": "seg-a-full"})
            self.assertEqual(deleted.status_code, 200)
            body = deleted.json()
            self.assertTrue(body["deleted"])
            self.assertEqual(body["restore_point_count"], 2)
            self.assertEqual(body["segment_id"], "seg-a-full")
            self.assertNotIn("seg-a-full", BACKUPS)
            self.assertNotIn("seg-a-inc", BACKUPS)
            self.assertFalse(seg_a_full_root.exists())
            self.assertFalse(seg_a_inc_root.exists())

            # ...and segment B — a later, independent full backup generation in
            # the same (job, vm, repository) chain — survives untouched.
            self.assertIn("seg-b-full", BACKUPS)
            self.assertIn("seg-b-inc", BACKUPS)
            self.assertTrue(seg_b_full_root.exists())
            self.assertTrue(seg_b_inc_root.exists())
            info = self.client.get(f"/api/v1/backups/{chain_id}")
            self.assertEqual(info.status_code, 200)
            remaining_ids = {rp["id"] for rp in info.json()["restore_points"]}
            self.assertEqual(remaining_ids, {"seg-b-full", "seg-b-inc"})

    def test_backup_tree_endpoint_returns_nodes_and_empty_state(self) -> None:
        BACKUPS.clear()

        empty_response = self.client.get("/api/v1/backups/tree")
        self.assertEqual(empty_response.status_code, 200)
        self.assertEqual(empty_response.json(), {"nodes": []})

        BACKUPS["rp-full"] = {
            "id": "rp-full",
            "chain_id": "backup-1",
            "job_id": "job-1",
            "job_name": "nightly-linux",
            "vm_id": "vm-1",
            "vm_name": "fdv-test-linux-01",
            "inventory_id": "inv-1",
            "inventory_name": "blackdog",
            "repository_id": "repo-1",
            "repository_name": "bbstorage",
            "execution_type": "ActiveFull",
            "created_at": "2026-06-19T10:00:00+00:00",
            "status": "available",
        }
        BACKUPS["rp-inc"] = {
            "id": "rp-inc",
            "chain_id": "backup-1",
            "job_id": "job-1",
            "job_name": "nightly-linux",
            "vm_id": "vm-1",
            "vm_name": "fdv-test-linux-01",
            "inventory_id": "inv-1",
            "inventory_name": "blackdog",
            "repository_id": "repo-1",
            "repository_name": "bbstorage",
            "execution_type": "Incremental",
            "created_at": "2026-06-20T10:00:00+00:00",
            "status": "available",
        }

        response = self.client.get("/api/v1/backups/tree")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("nodes", payload)
        repository_node = payload["nodes"][0]
        chain_node = repository_node["children"][0]["children"][0]["children"][0]["children"][0]

        self.assertEqual(repository_node["kind"], "repository")
        self.assertEqual(repository_node["label"], "bbstorage")
        self.assertEqual(chain_node["kind"], "backup_chain")
        self.assertTrue(chain_node["can_restore"])
        self.assertEqual(chain_node["restore_payload"]["point_id"], "rp-inc")
        self.assertEqual(chain_node["restore_points_count"], 2)

    def test_restore_context_endpoint_returns_vm_metadata_and_target_lists(self) -> None:
        BACKUPS.clear()
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()

        inventory = abr_v41.create_inventory_record(
            {
                "name": "lab-vcenter",
                "type": "vsphere",
                "address": "192.168.12.37",
                "username": "root",
                "password": "secret",
            }
        )
        RESOURCES_BY_HV[inventory["id"]][("host", "esx-01.local")] = {
            "resource_type": "host",
            "external_id": "esx-01.local",
            "name": "esx-01.local",
            "payload": {"datastores": ["ds-01", "ds-02"]},
            "discovered_at": "2026-06-20T09:00:00+00:00",
        }
        RESOURCES_BY_HV[inventory["id"]][("virtual_machine", "vm-1")] = {
            "resource_type": "virtual_machine",
            "external_id": "vm-1",
            "name": "tstvm",
            "payload": {
                "guest_os": "Ubuntu Linux (64-bit)",
                "num_cpu": 2,
                "memory_mb": 4096,
                "firmware": "efi",
                "host_external_id": "esx-01.local",
            },
            "discovered_at": "2026-06-20T09:00:00+00:00",
        }
        RESOURCES_BY_HV[inventory["id"]][("network_adapter", "vm-1-nic-1")] = {
            "resource_type": "network_adapter",
            "external_id": "vm-1-nic-1",
            "name": "Network adapter 1",
            "payload": {
                "vm_id": "vm-1",
                "network": "VM Network",
                "mac": "00:50:56:aa:bb:cc",
                "connected": True,
            },
            "discovered_at": "2026-06-20T09:00:00+00:00",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = pathlib.Path(tmpdir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "vm_name": "tstvm",
                        "vm_external_id": "vm-1",
                        "vm_config": {
                            "name": "tstvm",
                            "instance_uuid": "instance-uuid-1",
                            "guest_os": "Ubuntu Linux (64-bit)",
                            "num_cpu": 2,
                            "memory_mb": 4096,
                            "firmware": "efi",
                            "source_host": "esx-01.local",
                            "network_adapters": [
                                {
                                    "label": "Network adapter 1",
                                    "network": "VM Network",
                                    "connected": True,
                                }
                            ],
                        },
                        "disks": [
                            {
                                "disk_label": "Hard disk 1",
                                "capacity_gb": 40,
                                "datastore_path": "[ds-01] tstvm/tstvm.vmdk",
                                "source_disk_type_name": "vmfs_thin",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            BACKUPS["rp-restore-context"] = abr_v41.normalize_backup_point(
                {
                    "id": "rp-restore-context",
                    "chain_id": "backup-ctx-1",
                    "job_id": "job-ctx-1",
                    "job_name": "nightly-linux",
                    "vm_id": "vm-1",
                    "vm_name": "tstvm",
                    "inventory_id": inventory["id"],
                    "inventory_name": inventory["name"],
                    "repository_id": "repo-1",
                    "repository_name": "Repo",
                    "execution_type": "Incremental",
                    "created_at": "2026-06-20T10:00:00+00:00",
                    "status": "available",
                    "artifact": {
                        "manifest_path": str(manifest_path),
                        "backup_root": tmpdir,
                    },
                    "placements": [
                        {
                            "repository_id": "repo-1",
                            "repository_name": "Repo",
                            "manifest_path": str(manifest_path),
                            "backup_root": tmpdir,
                        }
                    ],
                }
            )

            response = self.client.get("/api/v1/restore/context", params={"restore_point_id": "rp-restore-context"})
            self.assertEqual(response.status_code, 200)
            payload = response.json()

        self.assertEqual(payload["restore_point"]["id"], "rp-restore-context")
        self.assertEqual(payload["restore_point"]["type"], "incremental")
        self.assertEqual(payload["source_vm"]["name"], "tstvm")
        self.assertEqual(payload["source_vm"]["firmware"], "efi")
        self.assertEqual(payload["source_vm"]["original_host"], "esx-01.local")
        self.assertEqual(payload["source_vm"]["disks"][0]["label"], "Hard disk 1")
        self.assertEqual(payload["defaults"]["target_inventory_id"], inventory["id"])
        self.assertEqual(payload["defaults"]["target_host_id"], "esx-01.local")
        self.assertTrue(any(item["name"] == "VM Network" for item in payload["targets"]["networks"]))
        self.assertTrue(payload["defaults"]["preserve_firmware"])

    def test_restore_context_endpoint_rejects_unknown_restore_point(self) -> None:
        response = self.client.get("/api/v1/restore/context", params={"restore_point_id": "missing-point"})
        self.assertEqual(response.status_code, 404)

    def test_first_job_start_without_full_creates_full_backup(self) -> None:
        session = self._run_single_vm_backup("test-first-auto-full-hv", force_full=False)
        self.assertEqual(session["resolved_backup_type"], "ActiveFull")
        self.assertEqual(session["resolved_backup_reason"], "FirstRun")

    def test_first_job_start_with_full_creates_full_backup(self) -> None:
        session = self._run_single_vm_backup("test-first-forced-full-hv", force_full=True)
        self.assertEqual(session["resolved_backup_type"], "ActiveFull")
        self.assertEqual(session["resolved_backup_reason"], "ForceFull")

    def test_second_job_start_without_full_creates_incremental_backup(self) -> None:
        inventory = self._create_mock_inventory("test-second-auto-incremental-hv")
        repository = self._create_repository("second-auto-repo", "local", path="/backup/second-auto")
        job = self._create_backup_job(
            name="second-auto-job",
            inventory_id=inventory["id"],
            vm_names=["vm-1"],
            repo_id=repository["id"],
            task_parameters={"duration_seconds": 0.1},
        )
        first_started = self._start_job(job["id"], force_full=False)
        self._wait_for_session(first_started["items"][0]["id"], timeout=5.0)

        second_started = self._start_job(job["id"], force_full=False)
        second_session = self._wait_for_session(second_started["items"][0]["id"], timeout=5.0)

        self.assertEqual(second_session["resolved_backup_type"], "Incremental")
        self.assertEqual(second_session["resolved_backup_reason"], "StandardIncremental")

    def test_second_job_start_with_full_creates_full_backup(self) -> None:
        inventory = self._create_mock_inventory("test-second-forced-full-hv")
        repository = self._create_repository("second-forced-repo", "local", path="/backup/second-forced")
        job = self._create_backup_job(
            name="second-forced-job",
            inventory_id=inventory["id"],
            vm_names=["vm-1"],
            repo_id=repository["id"],
            task_parameters={"duration_seconds": 0.1},
        )
        first_started = self._start_job(job["id"], force_full=False)
        self._wait_for_session(first_started["items"][0]["id"], timeout=5.0)

        second_started = self._start_job(job["id"], force_full=True)
        second_session = self._wait_for_session(second_started["items"][0]["id"], timeout=5.0)

        self.assertEqual(second_session["resolved_backup_type"], "ActiveFull")
        self.assertEqual(second_session["resolved_backup_reason"], "ForceFull")

    def test_job_create_rejects_retention_days_without_a_full_schedule(self) -> None:
        inventory = self._create_mock_inventory("test-retention-nofull-hv")
        repository = self._create_repository("retention-nofull-repo", "local", path="/backup/retention-nofull")

        response = self.client.post(
            "/api/v1/jobs",
            json={
                "name": "retention-nofull-job",
                "vms": ["vm-1"],
                "inventory_id": inventory["id"],
                "repo_id": repository["id"],
                "retention_days": 30,
            },
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("full backup schedule", response.json()["error"]["message"])
        listed = self.client.get("/api/v1/jobs")
        self.assertFalse(any(item["name"] == "retention-nofull-job" for item in listed.json()["items"]))

    def test_job_create_accepts_retention_days_with_a_synthetic_full_schedule(self) -> None:
        inventory = self._create_mock_inventory("test-retention-synthetic-hv")
        repository = self._create_repository("retention-synthetic-repo", "local", path="/backup/retention-synthetic")

        response = self.client.post(
            "/api/v1/jobs",
            json={
                "name": "retention-synthetic-job",
                "vms": ["vm-1"],
                "inventory_id": inventory["id"],
                "repo_id": repository["id"],
                "retention_days": 30,
                "schedule": {"type": "daily", "time": "22:00"},
                "synthetic_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "03:00"},
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()
        self.assertEqual(item["synthetic_full_schedule"]["type"], "weekly")
        self.assertIsNotNone(item["synthetic_full_next_run_at"])
        self.assertIsNone(item["active_full_schedule"])

    def test_job_create_rejects_retention_days_zero_like_job_edit_does(self) -> None:
        inventory = self._create_mock_inventory("test-retention-zero-create-hv")
        repository = self._create_repository("retention-zero-repo", "local", path="/backup/retention-zero")

        response = self.client.post(
            "/api/v1/jobs",
            json={
                "name": "retention-zero-job",
                "vms": ["vm-1"],
                "inventory_id": inventory["id"],
                "repo_id": repository["id"],
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "retention_days": 0,
            },
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("retention-days must be greater than 0", response.json()["error"]["message"])
        listed = self.client.get("/api/v1/jobs")
        self.assertFalse(any(item["name"] == "retention-zero-job" for item in listed.json()["items"]))

    def test_job_create_copy_rejects_retention_days_zero(self) -> None:
        inventory = self._create_mock_inventory("test-retention-zero-copy-hv")
        repository = self._create_repository("retention-zero-copy-repo", "local", path="/backup/retention-zero-copy")
        source_job = self._create_backup_job(
            name="retention-zero-copy-source",
            inventory_id=inventory["id"],
            vm_names=["vm-1"],
            repo_id=repository["id"],
        )

        response = self.client.post(
            "/api/v1/jobs/copy",
            json={
                "name": "retention-zero-copy-target",
                "source_id": source_job["id"],
                "retention_days": 0,
            },
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("retention-days must be greater than 0", response.json()["error"]["message"])

    def test_repository_delete_is_blocked_when_backup_placement_references_it(self) -> None:
        repository = self._create_repository("local1", "local", path="/backup/altastor")
        inventory = self._create_mock_inventory("test-repo-delete-hv")
        job = self._create_backup_job(
            name="repo-protected-job",
            inventory_id=inventory["id"],
            vm_names=["vm-1"],
            repo_id=repository["id"],
            task_parameters={"duration_seconds": 0.1},
        )
        started = self._start_job(job["id"])
        self._wait_for_session(started["items"][0]["id"], timeout=5.0)

        delete_response = self.client.delete(f"/api/v1/repositories/{repository['id']}")
        self.assertEqual(delete_response.status_code, 409)

    def test_repository_delete_is_blocked_when_job_references_it(self) -> None:
        repository = self._create_repository("repo-used", "local", path="/mnt/repo-used")
        inventory = self._create_mock_inventory("test-repo-delete-job-only-hv")
        self._create_backup_job(
            name="Job-SQL-Server",
            inventory_id=inventory["id"],
            vm_names=["vm-1"],
            repo_id=repository["id"],
        )

        delete_response = self.client.delete(f"/api/v1/repositories/{repository['id']}")

        self.assertEqual(delete_response.status_code, 409)
        error = delete_response.json()["error"]
        self.assertEqual(error["code"], "repository_in_use")
        self.assertEqual(error["details"]["jobs"][0]["name"], "Job-SQL-Server")

    def _run_single_vm_backup(self, inventory_name: str, *, force_full: bool) -> dict[str, object]:
        inventory = self._create_mock_inventory(inventory_name)
        repository = self._create_repository(f"{inventory_name}-repo", "local", path=f"/backup/{inventory_name}")
        job = self._create_backup_job(
            name=f"{inventory_name}-job",
            inventory_id=inventory["id"],
            vm_names=["vm-1"],
            repo_id=repository["id"],
            task_parameters={"duration_seconds": 0.1},
        )
        started = self._start_job(job["id"], force_full=force_full)
        return self._wait_for_session(started["items"][0]["id"], timeout=5.0)

    def _create_mock_inventory(self, name: str) -> dict[str, object]:
        response = self.client.post(
            "/api/v1/inventories",
            json={
                "name": name,
                "type": "mock",
                "address": f"mock://{name}",
                "username": "mock",
                "password": "mock",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        inventory = response.json()

        rescan = self.client.post(f"/api/v1/inventories/{inventory['id']}/rescan")
        self.assertEqual(rescan.status_code, 200, rescan.text)
        return inventory

    def _create_repository(self, name: str, repo_type: str, path: str | None = None) -> dict[str, object]:
        if repo_type == "local" and path and not os.path.isdir(path):
            path = os.path.join(self._repo_root, path.lstrip("/"))
            os.makedirs(path, exist_ok=True)
        response = self.client.post(
            "/api/v1/repositories",
            json={
                "name": name,
                "type": repo_type,
                "path": path,
                "metadata": {},
                "locations": [],
            },
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _create_backup_job(
        self,
        *,
        name: str,
        inventory_id: str,
        vm_names: list[str],
        repo_id: str,
        task_parameters: dict[str, object] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": name,
            "vms": vm_names,
            "inventory_id": inventory_id,
            "repo_id": repo_id,
            "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
        }
        if task_parameters is not None:
            payload["task_parameters"] = task_parameters
        response = self.client.post("/api/v1/jobs", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _start_job(self, job_id: str, *, force_full: bool = False) -> dict[str, object]:
        response = self.client.post(f"/api/v1/jobs/{job_id}/start", json={"force_full": force_full})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _get_session(self, session_id: str) -> dict[str, object]:
        response = self.client.get(f"/api/v1/sessions/{session_id}")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _trace_sessions_until_complete(
        self,
        session_ids: list[str],
        timeout: float = 10.0,
    ) -> dict[str, dict[str, object]]:
        deadline = time.time() + timeout
        last_snapshots: dict[str, tuple[object, object]] = {}

        while time.time() < deadline:
            sessions = {session_id: self._get_session(session_id) for session_id in session_ids}
            completed_count = 0

            for session_id, session in sessions.items():
                snapshot = (session.get("state"), session.get("progress"))
                if last_snapshots.get(session_id) != snapshot:
                    self._log(self._format_session_trace(session_id, session))
                    last_snapshots[session_id] = snapshot

                if session["state"] == "completed":
                    completed_count += 1

            if completed_count == len(session_ids):
                return sessions

            time.sleep(0.05)

        self.fail("Timed out waiting for verbose end-to-end trace to finish")

    def _format_session_trace(self, session_id: str, session: dict[str, object]) -> str:
        return f"session={session_id} state={session.get('state')} progress={session.get('progress')}"

    def _log(self, message: str) -> None:
        print(f"[trace] {message}")

    def _wait_for_session(self, session_id: str, timeout: float = 5.0) -> dict[str, object]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            session = self._get_session(session_id)
            if session["state"] in {"completed", "failed"}:
                return session
            time.sleep(0.05)
        self.fail(f"Timed out waiting for session {session_id} to finish")

    def _wait_for_status_mix(
        self,
        session_ids: list[str],
        expected_running: int,
        expected_queued: int,
        timeout: float = 3.0,
    ) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            states = [self._get_session(session_id)["state"] for session_id in session_ids]
            if states.count("running") == expected_running and states.count("queued") == expected_queued:
                return
            time.sleep(0.05)
        self.fail(
            "Timed out waiting for expected queue state. "
            f"Expected running={expected_running}, queued={expected_queued}."
        )

    def test_deleting_an_imported_reference_chain_does_not_destroy_the_source_repository_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_a_path = pathlib.Path(tmpdir) / "repoA"
            repo_b_path = pathlib.Path(tmpdir) / "repoB"

            repo_a_path.mkdir()
            repo_b_path.mkdir()

            repo_a = self._create_repository("repoA", "local", path=str(repo_a_path))
            self._create_repository("repoB", "local", path=str(repo_b_path))

            session_dir = repo_a_path / "vm-101" / "session-1"
            session_dir.mkdir(parents=True)
            disk_path = session_dir / "01-vm-101.vmdk"
            (session_dir / "manifest.json").write_text(json.dumps({"vm_name": "vm-101"}), encoding="utf-8")
            disk_path.write_bytes(b"original backup payload")

            size_before = sum(f.stat().st_size for f in repo_a_path.rglob("*") if f.is_file())
            self.assertGreater(size_before, 0)

            imported = self.client.post(
                "/api/v1/backups/import",
                json={"path": str(session_dir), "repo": "repoB", "repo_id": None},
            )
            self.assertEqual(imported.status_code, 200, imported.text)
            imported_chain_id = imported.json()["id"]

            self.assertEqual(sum(1 for _ in repo_b_path.rglob("*") if _.is_file()), 0)

            deleted = self.client.delete(f"/api/v1/backups/{imported_chain_id}")
            self.assertEqual(deleted.status_code, 200, deleted.text)

            payload = deleted.json()

            self.assertTrue(payload["deleted"])
            self.assertFalse(payload["artifacts_removed"])

            size_after = sum(f.stat().st_size for f in repo_a_path.rglob("*") if f.is_file())

            self.assertEqual(size_after, size_before, "original backup data in repo A must survive deleting the imported reference in repo B")
            self.assertTrue(disk_path.exists())

    def test_deleting_an_imported_chain_does_not_remove_the_source_repositorys_disk_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_a_path = pathlib.Path(tmpdir) / "repoA"
            repo_b_path = pathlib.Path(tmpdir) / "repoB"
            repo_a_path.mkdir()
            repo_b_path.mkdir()

            self._create_repository("repoA-disks", "local", path=str(repo_a_path))
            self._create_repository("repoB-disks", "local", path=str(repo_b_path))

            session_dir = repo_a_path / "vm-101" / "session-1"
            session_dir.mkdir(parents=True)
            disk_data_path = session_dir / "01-vm-101.vmdk.abrd"
            disk_index_path = session_dir / "01-vm-101.vmdk.abri"
            disk_data_path.write_bytes(b"original backup payload")
            disk_index_path.write_bytes(b"original backup index")
            manifest = {
                "vm_name": "vm-101",
                "disks": [
                    {
                        "index": 1,
                        "abr_path": str(session_dir),
                        "abr_backup_name": "01-vm-101.vmdk",
                    }
                ],
            }
            (session_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            size_before = sum(f.stat().st_size for f in repo_a_path.rglob("*") if f.is_file())
            self.assertGreater(size_before, 0)

            imported = self.client.post(
                "/api/v1/backups/import",
                json={"path": str(session_dir), "repo": "repoB-disks", "repo_id": None},
            )
            self.assertEqual(imported.status_code, 200, imported.text)
            imported_chain_id = imported.json()["id"]

            with patch("python.srv.core.storage_tool.run_disk_tool") as run_disk_tool_mock:
                deleted = self.client.delete(f"/api/v1/backups/{imported_chain_id}")
            self.assertEqual(deleted.status_code, 200, deleted.text)

            for call in run_disk_tool_mock.call_args_list:
                command = call.args[0]
                self.assertNotIn(
                    str(session_dir),
                    command,
                    f"must never invoke remove against the source repository's path: {command}",
                )

            payload = deleted.json()
            self.assertTrue(payload["deleted"])
            self.assertFalse(payload["artifacts_removed"])

            size_after = sum(f.stat().st_size for f in repo_a_path.rglob("*") if f.is_file())
            self.assertEqual(
                size_after,
                size_before,
                "deleting the imported entry must not touch the source repository's .abrd/.abri disk artifacts",
            )
            self.assertTrue(disk_data_path.exists())
            self.assertTrue(disk_index_path.exists())

    def test_repo_add_rejects_a_path_nested_inside_an_existing_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            outer_path = pathlib.Path(tmpdir) / "parent"
            inner_path = outer_path / "inner"
            inner_path.mkdir(parents=True)

            self._create_repository("nested-inner", "local", path=str(inner_path))

            conflict = self.client.post(
                "/api/v1/repositories",
                json={"name": "nested-outer", "type": "local", "path": str(outer_path), "metadata": {}, "locations": []},
            )
            self.assertEqual(conflict.status_code, 409, conflict.text)
            self.assertEqual(conflict.json()["error"]["code"], "repository_path_overlaps")

    def test_abr31_repo_add_rejects_nonexistent_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_path = str(pathlib.Path(tmpdir) / "does-not-exist")

            response = self.client.post(
                "/api/v1/repositories",
                json={"name": "abr31-missing-path", "type": "local", "path": missing_path, "metadata": {}, "locations": []},
            )

            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["error"]["code"], "invalid_repository")

        items = self.client.get("/api/v1/repositories").json()["items"]
        self.assertFalse(any(item["name"] == "abr31-missing-path" for item in items))

    def test_repo_add_rejects_path_that_is_a_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = pathlib.Path(tmpdir) / "not-a-directory.txt"
            file_path.write_text("this is a file, not a directory", encoding="utf-8")

            response = self.client.post(
                "/api/v1/repositories",
                json={"name": "abr31-file-path", "type": "local", "path": str(file_path), "metadata": {}, "locations": []},
            )

            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["error"]["code"], "invalid_repository")

        items = self.client.get("/api/v1/repositories").json()["items"]
        self.assertFalse(any(item["name"] == "abr31-file-path" for item in items))

    def test_deleting_an_imported_chain_does_not_destroy_data_when_repository_roots_are_nested(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            outer_path = pathlib.Path(tmpdir) / "parent"

            inner_path = outer_path / "inner"
            inner_path.mkdir(parents=True)

            outer_id = str(uuid.uuid4())
            inner_id = str(uuid.uuid4())

            REPOSITORIES[outer_id] = {
                "id": outer_id,
                "name": "nested-outer-rt",
                "type": "local",
                "path": str(outer_path),
                "status": "available",
                "metadata": {},
            }
            REPOSITORIES[inner_id] = {
                "id": inner_id,
                "name": "nested-inner-rt",
                "type": "local",
                "path": str(inner_path),
                "status": "available",
                "metadata": {},
            }

            session_dir = inner_path / "vm-201" / "session-1"
            session_dir.mkdir(parents=True)

            disk_path = session_dir / "01-vm-201.vmdk"
            (session_dir / "manifest.json").write_text(json.dumps({"vm_name": "vm-201"}), encoding="utf-8")
            disk_path.write_bytes(b"real inner-repository backup payload")

            size_before = sum(f.stat().st_size for f in inner_path.rglob("*") if f.is_file())
            self.assertGreater(size_before, 0)

            imported = self.client.post(
                "/api/v1/backups/import",
                json={"path": str(session_dir), "repo_id": outer_id},
            )
            self.assertEqual(imported.status_code, 200, imported.text)
            imported_chain_id = imported.json()["id"]

            deleted = self.client.delete(f"/api/v1/backups/{imported_chain_id}")
            self.assertEqual(deleted.status_code, 200, deleted.text)
            payload = deleted.json()

            self.assertTrue(payload["deleted"])
            self.assertFalse(
                payload["artifacts_removed"],
                "delete must refuse to remove files that belong to a different, more specific registered repository",
            )

            size_after = sum(f.stat().st_size for f in inner_path.rglob("*") if f.is_file())
            self.assertEqual(
                size_after,
                size_before,
                "inner repository's real backup data must survive deleting the imported reference in the nested outer repository",
            )
            self.assertTrue(disk_path.exists())


if __name__ == "__main__":
    unittest.main()
