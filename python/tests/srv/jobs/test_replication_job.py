# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    RESOURCES_BY_HV,
    TASKS,
)
from python.srv.services import abr_v41
from python.srv.services.api_errors import ApiError

STORES = (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReplicationJobSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        reachable = patch("python.srv.services.abr_v41.assert_inventory_registrable", return_value=None)
        reachable.start()
        self.addCleanup(reachable.stop)
        for store in STORES:
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in STORES])
        self.source_inventory = abr_v41.create_inventory_record(
            {"name": "vc-prod", "type": "vsphere", "address": "192.0.2.10", "username": "u", "password": "p"}
        )
        self.target_inventory = abr_v41.create_inventory_record(
            {"name": "vc-dr", "type": "vsphere", "address": "192.0.2.20", "username": "u", "password": "p"}
        )
        self._host(self.target_inventory["id"], "esx-dr-01", ["ds-dr", "ds-dr-2"])
        self._host(self.target_inventory["id"], "esx-dr-02", ["ds-dr"])
        self.repository = abr_v41.create_repository_record({"name": "repo", "type": "local", "path": tempfile.mkdtemp()})
        self.source = self._backup_job("sql-nightly", [("vm-1", "sql-01")])
        self._point("rp-1", "sql-nightly", "vm-1", "sql-01", "2026-11-01T01:00:00+00:00")
        self._point("rp-2", "sql-nightly", "vm-1", "sql-01", "2026-11-02T01:00:00+00:00", execution_type="Incremental")

    def _host(self, inventory_id: str, name: str, datastores: list[str]) -> None:
        RESOURCES_BY_HV[inventory_id][("host", name)] = {
            "resource_type": "host",
            "external_id": name,
            "name": name,
            "payload": {"datastores": datastores},
            "discovered_at": _now(),
        }

    def _vm(self, inventory_id: str, vm_id: str, name: str) -> None:
        RESOURCES_BY_HV[inventory_id][("virtual_machine", vm_id)] = {
            "resource_type": "virtual_machine",
            "external_id": vm_id,
            "name": name,
            "payload": {},
            "discovered_at": _now(),
        }

    def _backup_job(self, name: str, vms: list[tuple[str, str]], job_type: str = "backup") -> dict:
        job = abr_v41.normalize_job_record(
            {
                "id": f"job-{name}",
                "name": name,
                "job_type": job_type,
                "inventory_id": self.source_inventory["id"],
                "inventory_name": self.source_inventory["name"],
                "vm_ids": [vm_id for vm_id, _name in vms],
                "vm_names": [vm_name for _id, vm_name in vms],
                "repo_id": self.repository["id"],
                "repo_name": self.repository["name"],
            }
        )
        JOBS[job["id"]] = job
        return job

    def _point(
        self,
        point_id: str,
        job_name: str,
        vm_id: str,
        vm_name: str,
        created_at: str,
        *,
        execution_type: str = "ActiveFull",
        kind: str = "vcenter",
        status: str = "available",
    ) -> None:
        BACKUPS[point_id] = abr_v41.normalize_backup_point(
            {
                "id": point_id,
                "job_id": f"job-{job_name}",
                "job_name": job_name,
                "vm_id": vm_id,
                "vm_name": vm_name,
                "inventory_id": self.source_inventory["id"],
                "inventory_name": self.source_inventory["name"],
                "repository_id": self.repository["id"],
                "repository_name": self.repository["name"],
                "execution_type": execution_type,
                "hypervisor_kind": kind,
                "created_at": created_at,
                "artifact": {"manifest_path": f"/tmp/{point_id}.json", "backup_root": "/tmp"},
                "placements": [
                    {
                        "repository_id": self.repository["id"],
                        "repository_name": self.repository["name"],
                        "manifest_path": f"/tmp/{point_id}.json",
                        "backup_root": "/tmp",
                    }
                ],
                "status": status,
            }
        )

    def _payload(self, **changes: object) -> dict:
        payload = {
            "name": "sql-dr",
            "source_job": "sql-nightly",
            "target_inventory": "vc-dr",
            "target_host": "esx-dr-01",
            "target_datastore": "ds-dr",
            "replica_name": "sql-01-replica",
        }
        payload.update(changes)
        return {key: value for key, value in payload.items() if value is not None}

    def _create(self, **changes: object) -> dict:
        return abr_v41.create_replication_job(self._payload(**changes))

    def _refused(self, call, *args, **kwargs) -> ApiError:
        with self.assertRaises(ApiError) as caught:
            call(*args, **kwargs)
        return caught.exception

    def _with_replica(self, job: dict) -> dict:
        stored = JOBS[job["id"]]
        stored["replica"] = {"vm_id": "vm-r1", "vm_name": "sql-01-replica", "point_id": "rp-2"}
        return stored

    def test_create_resolves_the_source_vm_and_the_target(self) -> None:
        job = self._create(network_map=["VM Network=DR Network"], run_after_source=True)

        self.assertEqual(job["job_type"], "replication")
        view = job["replication"]
        self.assertEqual(view["source_job"], {"id": self.source["id"], "name": "sql-nightly"})
        self.assertEqual(view["source_vm"], {"id": "vm-1", "name": "sql-01"})
        self.assertEqual(view["target_inventory"]["name"], "vc-dr")
        self.assertEqual(view["target_host"], "esx-dr-01")
        self.assertEqual(view["target_datastore"], "ds-dr")
        self.assertEqual(view["network_map"], ["VM Network=DR Network"])
        self.assertEqual(view["replica_name"], "sql-01-replica")
        self.assertTrue(view["run_after_source"])
        self.assertIsNone(view["replica"]["vm_id"])
        self.assertEqual(job["inventory"]["name"], "vc-dr")
        self.assertEqual(job["vm_ids"], ["vm-1"])
        self.assertEqual(job["repository"]["name"], "repo")

    def test_create_takes_the_vm_by_name_or_id(self) -> None:
        self.assertEqual(self._create(source_vm="SQL-01")["replication"]["source_vm"]["id"], "vm-1")
        JOBS.pop(next(iter(job_id for job_id, job in JOBS.items() if job["name"] == "sql-dr")))
        self.assertEqual(self._create(source_vm="vm-1")["replication"]["source_vm"]["id"], "vm-1")

    def test_a_source_with_two_vms_is_refused(self) -> None:
        self._backup_job("two", [("vm-1", "sql-01"), ("vm-2", "web-01")])

        error = self._refused(self._create, source_job="two")

        self.assertEqual(error.status_code, 422)
        self.assertEqual(
            error.message, "Source backup job contains more than one VM. Only backup jobs with a single VM are supported."
        )

    def test_a_backup_copy_or_replication_source_is_refused(self) -> None:
        self._backup_job("offsite", [("vm-1", "sql-01")], job_type="backup-copy")
        self.assertIn("is a backup copy job", self._refused(self._create, source_job="offsite").message)
        self._create()
        self.assertIn("is a replication job", self._refused(self._create, name="again", source_job="sql-dr").message)

    def test_a_source_without_valid_points_is_refused(self) -> None:
        self._backup_job("empty", [("vm-9", "empty-01")])
        self._point("rp-x", "empty", "vm-9", "empty-01", "2026-11-01T01:00:00+00:00", status="deleting")

        error = self._refused(self._create, source_job="empty")

        self.assertIn("has no restore point of empty-01 that can be replicated", error.message)

    def test_a_vm_that_is_not_in_the_source_job_is_refused(self) -> None:
        self.assertIn("VM web-01 is not in source backup job", self._refused(self._create, source_vm="web-01").message)

    def test_each_required_setting_is_named(self) -> None:
        cases = {
            "source_job": "needs a source backup job",
            "target_inventory": "needs a target environment",
            "target_host": "needs a target host",
            "target_datastore": "needs a target datastore",
            "replica_name": "--replica-name is required",
        }
        for key, text in cases.items():
            with self.subTest(key=key):
                self.assertIn(text, self._refused(self._create, **{key: None}).message)

    def test_another_platform_as_target_is_refused_with_both_platforms_named(self) -> None:
        hyperv = abr_v41.create_inventory_record(
            {"name": "hv-dr", "type": "hyper-v", "address": "192.0.2.30", "username": "u", "password": "p"}
        )
        self._host(hyperv["id"], "hv-01", ["D:\\VMs"])

        error = self._refused(self._create, target_inventory="hv-dr", target_host="hv-01", target_datastore="D:\\VMs")

        self.assertEqual(error.code, "replication_platform_mismatch")
        self.assertEqual(error.message, "Source VM platform (VMware) does not match target environment platform (Hyper-V).")

    def test_a_hyperv_source_cannot_go_to_vmware(self) -> None:
        self._backup_job("hv-job", [("hv-vm-1", "app-01")])
        self._point("rp-h", "hv-job", "hv-vm-1", "app-01", "2026-11-01T01:00:00+00:00", kind="hyper-v")

        error = self._refused(self._create, source_job="hv-job")

        self.assertEqual(error.message, "Source VM platform (Hyper-V) does not match target environment platform (VMware).")

    def test_a_replica_name_taken_on_the_target_is_refused(self) -> None:
        self._vm(self.target_inventory["id"], "vm-busy", "sql-01-replica")

        error = self._refused(self._create)

        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.code, "target_vm_exists")

    def test_an_unknown_host_or_datastore_is_refused(self) -> None:
        self.assertEqual(self._refused(self._create, target_host="esx-missing").status_code, 404)
        self.assertIn(self._refused(self._create, target_datastore="ds-missing").status_code, (404, 422))

    def test_a_start_point_of_another_vm_is_refused_and_one_of_ours_is_kept(self) -> None:
        self._backup_job("web", [("vm-2", "web-01")])
        self._point("rp-w", "web", "vm-2", "web-01", "2026-11-01T01:00:00+00:00")
        self.assertIn("is not an available restore point", self._refused(self._create, restore_point_id="rp-w").message)

        job = self._create(restore_point_id="rp-1")

        self.assertEqual(job["replication"]["start_point_id"], "rp-1")

    def test_backup_settings_are_refused_on_edit(self) -> None:
        job = self._create()
        for key, value in (("retention_days", 7), ("repo", "repo"), ("vms", ["sql-01"]), ("gfs_weekly", 2)):
            with self.subTest(key=key):
                error = self._refused(abr_v41.edit_job_record, job["id"], {key: value})
                self.assertIn("do not apply to replication job", error.message)
        self.assertIn("apply to backup copy jobs only", self._refused(abr_v41.edit_job_record, job["id"], {"copy_mode": "immediate"}).message)

    def test_replication_settings_are_refused_on_other_jobs(self) -> None:
        error = self._refused(abr_v41.edit_job_record, self.source["id"], {"replica_name": "x"})
        self.assertIn("apply to replication jobs only", error.message)

    def test_before_the_replica_exists_target_settings_can_change(self) -> None:
        job = self._create()

        edited = abr_v41.edit_job_record(job["id"], {"target_host": "esx-dr-02", "network_map": ["A=B"]})

        self.assertEqual(edited["replication"]["target_host"], "esx-dr-02")
        self.assertEqual(edited["replication"]["network_map"], ["A=B"])

    def test_edit_checks_the_merged_settings(self) -> None:
        job = self._create()
        self.assertEqual(self._refused(abr_v41.edit_job_record, job["id"], {"target_host": "esx-missing"}).status_code, 404)
        self.assertEqual(JOBS[job["id"]]["replication"]["target_host"], "esx-dr-01")

    def test_after_the_replica_exists_locked_settings_are_refused(self) -> None:
        job = self._create()
        self._with_replica(job)
        changes = {
            "source_job": "other",
            "source_vm": "web-01",
            "target_inventory": "vc-prod",
            "target_host": "esx-dr-02",
            "target_datastore": "ds-dr-2",
            "network_map": ["A=B"],
            "restore_point_id": "rp-2",
        }
        for key, value in changes.items():
            with self.subTest(key=key):
                error = self._refused(abr_v41.edit_job_record, job["id"], {key: value})
                self.assertEqual(error.code, "replication_setting_locked")
                self.assertEqual(
                    error.message, "Cannot be changed after the replica is created. Create a new Replication Job instead."
                )

    def test_after_the_replica_exists_the_same_values_and_free_settings_pass(self) -> None:
        job = self._create()
        self._with_replica(job)

        edited = abr_v41.edit_job_record(
            job["id"],
            {"target_host": "esx-dr-01", "run_after_source": True, "name": "sql-dr-2", "description": "dr"},
        )

        self.assertTrue(edited["replication"]["run_after_source"])
        self.assertEqual(edited["name"], "sql-dr-2")

    def test_the_start_point_can_change_until_the_replica_exists(self) -> None:
        job = self._create(restore_point_id="rp-1")

        edited = abr_v41.edit_job_record(job["id"], {"restore_point_id": "rp-2"})

        self.assertEqual(edited["replication"]["start_point_id"], "rp-2")

    def test_after_the_replica_exists_the_start_point_is_locked_but_the_same_value_passes(self) -> None:
        job = self._create(restore_point_id="rp-1")
        self._with_replica(job)

        same = abr_v41.edit_job_record(job["id"], {"restore_point_id": "rp-1", "run_after_source": True})
        cleared = self._refused(abr_v41.edit_job_record, job["id"], {"restore_point_id": ""})

        self.assertEqual(same["replication"]["start_point_id"], "rp-1")
        self.assertEqual(cleared.code, "replication_setting_locked")

    def test_after_the_replica_exists_an_empty_start_point_stays_empty(self) -> None:
        job = self._create()
        self._with_replica(job)

        edited = abr_v41.edit_job_record(job["id"], {"restore_point_id": "", "run_after_source": True})

        self.assertIsNone(edited["replication"]["start_point_id"])

    def test_a_rename_after_the_replica_exists_waits_for_the_next_run(self) -> None:
        job = self._create()
        self._with_replica(job)

        edited = abr_v41.edit_job_record(job["id"], {"replica_name": "sql-01-dr"})

        self.assertEqual(edited["replication"]["replica_name"], "sql-01-dr")
        self.assertEqual(edited["replication"]["replica"]["pending_name"], "sql-01-dr")
        self.assertEqual(edited["replication"]["replica"]["vm_name"], "sql-01-replica")

    def test_a_rename_to_a_taken_name_is_refused(self) -> None:
        job = self._create()
        self._with_replica(job)
        self._vm(self.target_inventory["id"], "vm-busy", "busy-01")

        self.assertEqual(self._refused(abr_v41.edit_job_record, job["id"], {"replica_name": "busy-01"}).status_code, 409)

    def test_the_source_job_cannot_be_deleted_while_a_replication_job_uses_it(self) -> None:
        replication = self._create()

        error = self._refused(abr_v41.delete_job_record, self.source["id"])

        self.assertEqual(error.code, "replication_source_in_use")
        self.assertEqual(
            error.message,
            "Backup job sql-nightly is the source of replication job sql-dr. Delete replication job sql-dr first.",
        )
        abr_v41.delete_job_record(replication["id"])
        self.assertTrue(abr_v41.delete_job_record(self.source["id"])["deleted"])

    def test_the_replicated_vm_cannot_leave_the_source_job(self) -> None:
        self._create()
        self._vm(self.source_inventory["id"], "vm-1", "sql-01")
        self._vm(self.source_inventory["id"], "vm-2", "web-01")

        error = self._refused(abr_v41.edit_job_record, self.source["id"], {"vms": ["web-01"]})

        self.assertEqual(
            error.message, "VM sql-01 is replicated by replication job sql-dr. Delete replication job sql-dr first."
        )
        edited = abr_v41.edit_job_record(self.source["id"], {"vms": ["sql-01", "web-01"]})
        self.assertEqual(edited["vm_ids"], ["vm-1", "vm-2"])

    def test_retention_refuses_a_replication_job(self) -> None:
        job = self._create()
        self.assertIn("keeps no restore points", self._refused(abr_v41.run_job_retention, job["id"]).message)

    def test_a_copy_job_edit_with_a_source_job_still_works(self) -> None:
        other = self.repository
        copy = abr_v41.normalize_job_record(
            {
                "id": "job-copy",
                "name": "copy",
                "job_type": "backup-copy",
                "source_job_ids": [self.source["id"]],
                "source_job_names": ["sql-nightly"],
                "repo_id": other["id"],
                "repo_name": other["name"],
            }
        )
        JOBS["job-copy"] = copy
        with patch("python.srv.services.backup_copy.validate_copy_job"):
            edited = abr_v41.edit_job_record("job-copy", {"source_job": "sql-nightly"})
        self.assertEqual(edited["job_type"], "backup-copy")

    def test_a_rescan_does_not_move_a_replication_job_to_another_vm(self) -> None:
        job = self._create()
        stored = JOBS[job["id"]]

        abr_v41._reconcile_jobs_after_vm_identity_change(
            self.target_inventory["id"],
            {"sql-01": "vm-1"},
            [{"resource_type": "virtual_machine", "external_id": "vm-77", "name": "sql-01"}],
        )

        self.assertEqual(JOBS[job["id"]]["vm_ids"], stored["vm_ids"])


if __name__ == "__main__":
    unittest.main()
