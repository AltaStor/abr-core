# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from python.cli.commands.job_commands import JobCommands, _replication_rows
from python.cli.parsing.parser_builder import build_parser
from python.cli.services.job_service import JobService
from python.srv.api.auth_guard import require_session
from python.srv.backup_server import app
from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    RESOURCES_BY_HV,
    TASKS,
)
from python.srv.services import abr_v41

STORES = (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS)
REPLICATION_JOB = {"name": "SQL-DR", "job_type": "replication"}
BACKUP_JOB = {"name": "Nightly", "job_type": "backup"}
COPY_JOB = {"name": "Offsite", "job_type": "backup-copy"}
REQUIRED = [
    "--source-job", "Nightly", "--target-inventory", "vc-dr", "--target-host", "esx-dr-01", "--datastore", "ds-dr",
    "--replica-name", "sql-01-replica",
]


class _Cli(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = build_parser()
        self.jobs = JobService(None)

    def _args(self, *argv: str):
        return self.parser.parse_args(["job", *argv])

    def _create_payload(self, *argv: str) -> dict:
        return self.jobs.create_replication_job_payload(self._args("create-replication", "--name", "SQL-DR", *argv))

    def _edit(self, job: dict, *argv: str) -> dict:
        return self.jobs.build_edit_payload(job, self._args("edit", "--name", job["name"], *argv))


class CreatePayloadTests(_Cli):
    def test_every_replication_setting_goes_into_the_request(self) -> None:
        payload = self._create_payload(
            *REQUIRED, "--source-vm", "sql-01", "--network-map", "VM Network=DR Network", "--network-map", "Mgmt=DR Mgmt",
            "--run-after-source", "--restore-point", "rp-1", "--schedule", "daily", "--time", "03:00",
            "--description", "dr copy",
        )

        self.assertEqual(payload["source_job"], "Nightly")
        self.assertEqual(payload["source_vm"], "sql-01")
        self.assertEqual(payload["target_inventory"], "vc-dr")
        self.assertEqual(payload["target_host"], "esx-dr-01")
        self.assertEqual(payload["target_datastore"], "ds-dr")
        self.assertEqual(payload["network_map"], ["VM Network=DR Network", "Mgmt=DR Mgmt"])
        self.assertEqual(payload["replica_name"], "sql-01-replica")
        self.assertIs(payload["run_after_source"], True)
        self.assertEqual(payload["restore_point_id"], "rp-1")
        self.assertEqual(payload["schedule"]["type"], "daily")
        self.assertEqual(payload["description"], "dr copy")

    def test_no_backup_settings_are_sent(self) -> None:
        payload = self._create_payload(*REQUIRED)

        for key in ("retention_days", "gfs", "repo", "active_full_schedule", "synthetic_full_schedule", "task_parameters"):
            self.assertNotIn(key, payload)

    def test_backup_options_are_not_offered(self) -> None:
        for flag in ("--retention-days", "--repo", "--active-full-schedule", "--set-encryption"):
            with self.subTest(flag=flag), self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
                self._args("create-replication", "--name", "x", *REQUIRED, flag, "1")

    def test_the_source_and_the_replica_name_are_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires the source backup job"):
            self._create_payload("--replica-name", "r")
        with self.assertRaisesRegex(ValueError, "--replica-name is required"):
            self._create_payload("--source-job", "Nightly")

    def test_disabled_without_a_schedule_is_sent_as_enabled_false(self) -> None:
        self.assertIs(self._create_payload(*REQUIRED, "--disabled")["enabled"], False)


class EditPayloadTests(_Cli):
    def test_replication_settings_are_edited(self) -> None:
        payload = self._edit(
            REPLICATION_JOB, "--replica-name", "sql-01-dr", "--no-run-after-source", "--target-host", "esx-dr-02",
            "--network-map", "none", "--source-job", "Weekly", "--disabled",
        )

        self.assertEqual(payload["replica_name"], "sql-01-dr")
        self.assertIs(payload["run_after_source"], False)
        self.assertEqual(payload["target_host"], "esx-dr-02")
        self.assertEqual(payload["network_map"], [])
        self.assertEqual(payload["source_job"], "Weekly")
        self.assertIs(payload["enabled"], False)

    def test_two_source_jobs_are_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "one source backup job"):
            self._edit(REPLICATION_JOB, "--source-job", "A", "--source-job", "B")

    def test_copy_and_backup_options_are_refused_on_a_replication_job(self) -> None:
        with self.assertRaisesRegex(ValueError, "--mode apply to backup copy jobs only, and SQL-DR is a replication job"):
            self._edit(REPLICATION_JOB, "--mode", "immediate")
        with self.assertRaisesRegex(ValueError, "--inventory apply to backup jobs only, and SQL-DR is a replication job"):
            self._edit(REPLICATION_JOB, "--inventory", "vCenter")

    def test_replication_options_are_refused_on_other_jobs(self) -> None:
        for job, kind in ((BACKUP_JOB, "backup"), (COPY_JOB, "backup copy")):
            pattern = f"--replica-name apply to replication jobs only, and {job['name']} is a {kind} job"
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, pattern):
                self._edit(job, "--replica-name", "x")

    def test_copy_and_backup_edits_keep_working(self) -> None:
        self.assertEqual(self._edit(COPY_JOB, "--mode", "periodic")["copy_mode"], "periodic")
        self.assertEqual(self._edit(BACKUP_JOB, "--retention-days", "9")["retention_days"], 9)


class ViewTests(unittest.TestCase):
    def test_job_info_rows_before_the_replica_exists(self) -> None:
        rows = _replication_rows(
            {
                "source_job": {"id": "1", "name": "Nightly"},
                "source_vm": {"id": "vm-1", "name": "sql-01"},
                "target_inventory": {"id": "2", "name": "vc-dr"},
                "target_host": "esx-dr-01",
                "target_datastore": "ds-dr",
                "network_map": ["A=B"],
                "replica_name": "sql-01-replica",
                "run_after_source": True,
                "start_point_id": "rp-1",
                "replica": {},
                "run_pending": False,
            }
        )

        self.assertEqual(rows["replica_vm"], "not created yet")
        self.assertEqual(rows["start_point"], "rp-1")
        self.assertEqual(rows["run_after_source"], "yes")
        self.assertEqual(rows["last_success_at"], "-")
        self.assertNotIn("pending_rename", rows)

    def test_job_info_rows_after_an_update(self) -> None:
        rows = _replication_rows(
            {
                "source_job": {"name": "Nightly"},
                "source_vm": {"name": "sql-01"},
                "target_inventory": {"name": "vc-dr"},
                "replica_name": "sql-01-dr",
                "start_point_id": "rp-1",
                "replica": {
                    "vm_id": "vm-r",
                    "vm_name": "sql-01-replica",
                    "point_id": "rp-5",
                    "point_created_at": "2026-11-05T01:00:00+00:00",
                    "last_success_at": "2026-11-05T01:20:00+00:00",
                    "pending_name": "sql-01-dr",
                },
                "run_pending": True,
            }
        )

        self.assertEqual(rows["replica_vm"], "sql-01-replica")
        self.assertEqual(rows["replica_point"], "rp-5")
        self.assertEqual(rows["last_success_at"], "2026-11-05T01:20:00+00:00")
        self.assertEqual(rows["pending_rename"], "sql-01-dr")
        self.assertEqual(rows["run_pending"], "yes")
        self.assertNotIn("start_point", rows)


class ApiRoundTripTests(_Cli):
    def setUp(self) -> None:
        super().setUp()
        for store in STORES:
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in STORES])
        app.dependency_overrides[require_session] = lambda: {"user_id": "t", "user_name": "t", "role": "administrator"}
        self.addCleanup(app.dependency_overrides.pop, require_session, None)
        client_cm = TestClient(app)
        self.client = client_cm.__enter__()
        self.addCleanup(client_cm.__exit__, None, None, None)
        HYPERVISORS["vc"] = {"id": "vc", "name": "vCenter", "kind": "vcenter", "type": "vsphere"}
        HYPERVISORS["dr"] = {"id": "dr", "name": "vc-dr", "kind": "vcenter", "type": "vsphere", "status": "connected"}
        RESOURCES_BY_HV["dr"] = {}
        RESOURCES_BY_HV["dr"][("host", "esx-dr-01")] = {
            "resource_type": "host",
            "external_id": "esx-dr-01",
            "name": "esx-dr-01",
            "payload": {"datastores": ["ds-dr"]},
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        REPOSITORIES["src"] = {"id": "src", "name": "Local", "type": "local", "path": tempfile.mkdtemp()}
        JOBS["nightly"] = abr_v41.normalize_job_record(
            {"id": "nightly", "name": "Nightly", "job_type": "backup", "inventory_id": "vc", "vm_ids": ["vm-1"], "vm_names": ["sql-01"], "repo_id": "src", "repo_name": "Local", "task_parameters": {}}
        )
        BACKUPS["f1"] = abr_v41.normalize_backup_point(
            {"id": "f1", "status": "available", "job_id": "nightly", "job_name": "Nightly", "vm_id": "vm-1", "vm_name": "sql-01", "inventory_id": "vc", "repository_id": "src", "repository_name": "Local", "hypervisor_kind": "vcenter", "execution_type": "ActiveFull", "full_backup": True, "created_at": "2026-11-01T01:00:00+00:00", "artifact": {"manifest_path": "/tmp/f1.json", "backup_root": "/tmp"}, "placements": [{"repository_id": "src", "repository_name": "Local", "manifest_path": "/tmp/f1.json", "backup_root": "/tmp"}]}
        )

    def _create(self, *argv: str):
        return self.client.post("/api/v1/jobs/replication", json=self._create_payload(*REQUIRED, *argv))

    def test_a_job_made_by_the_cli_comes_back_with_its_settings(self) -> None:
        response = self._create("--run-after-source")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["job_type"], "replication")
        self.assertEqual(body["replication"]["source_vm"], {"id": "vm-1", "name": "sql-01"})
        self.assertTrue(body["replication"]["run_after_source"])
        self.assertEqual(body["inventory"]["name"], "vc-dr")

    def test_server_refusals_reach_the_cli_with_their_reason(self) -> None:
        JOBS["two"] = abr_v41.normalize_job_record(
            {"id": "two", "name": "Two", "job_type": "backup", "inventory_id": "vc", "vm_ids": ["a", "b"], "vm_names": ["a", "b"], "repo_id": "src"}
        )
        payload = self._create_payload(*REQUIRED)
        payload["source_job"] = "Two"

        response = self.client.post("/api/v1/jobs/replication", json=payload)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["error"]["message"],
            "Source backup job contains more than one VM. Only backup jobs with a single VM are supported.",
        )

    def test_an_edit_and_a_locked_edit_go_through_the_api(self) -> None:
        job = self._create().json()
        edited = self.client.patch(f"/api/v1/jobs/{job['id']}", json=self._edit(job, "--replica-name", "sql-01-dr"))
        self.assertEqual(edited.json()["replication"]["replica_name"], "sql-01-dr")

        JOBS[job["id"]]["replica"] = {"vm_id": "vm-r"}
        locked = self.client.patch(f"/api/v1/jobs/{job['id']}", json=self._edit(job, "--target-host", "esx-dr-02"))

        self.assertEqual((locked.status_code, locked.json()["error"]["code"]), (422, "replication_setting_locked"))

    def test_deleting_the_source_job_is_refused_through_the_api(self) -> None:
        self._create()

        response = self.client.delete("/api/v1/jobs/nightly")

        self.assertEqual((response.status_code, response.json()["error"]["code"]), (409, "replication_source_in_use"))

    def test_the_query_flag_reaches_the_server_and_the_answer_names_the_replica(self) -> None:
        job = self._create().json()
        removed = {"replica_deleted": True, "replica_vm_name": "sql-01-replica", "removed_vms": ["sql-01-replica"], "files_left": []}

        with patch("python.srv.services.replication_service.delete_replica", return_value=removed) as deleting:
            response = self.client.delete(f"/api/v1/jobs/{job['id']}", params={"delete_replica": "true"})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(deleting.call_count, 1)
        self.assertEqual(response.json()["message"], "Deleted the job and its replica sql-01-replica")
        self.assertNotIn(job["id"], JOBS)

    def test_a_plain_delete_answers_as_before(self) -> None:
        job = self._create().json()

        with patch("python.srv.services.replication_service.delete_replica") as deleting:
            response = self.client.delete(f"/api/v1/jobs/{job['id']}")

        self.assertEqual(response.json(), {"id": job["id"], "deleted": True})
        self.assertEqual(deleting.call_count, 0)

    def test_the_flag_on_a_backup_job_is_refused(self) -> None:
        response = self.client.delete("/api/v1/jobs/nightly", params={"delete_replica": "true"})

        self.assertEqual((response.status_code, response.json()["error"]["code"]), (422, "invalid_job"))
        self.assertIn("nightly", JOBS)

    def test_job_info_table_hides_backup_settings_and_shows_the_replica(self) -> None:
        job = self._create().json()
        api = SimpleNamespace(get=lambda path: self.client.get(f"/{path}").json())
        printed: list = []
        context = SimpleNamespace(api_client=api, output="table", print_output=lambda payload, *rest: printed.append(payload))
        with patch("python.cli.commands.job_commands.CommandDependencies", return_value=SimpleNamespace(context=context)):
            JobCommands().info(SimpleNamespace(id=job["id"], name=None))

        shown = printed[0]
        self.assertEqual(shown["replica_vm"], "not created yet")
        self.assertEqual(shown["source_job"], "Nightly")
        for hidden in ("retention_days", "gfs", "active_full_schedule", "encryption", "replication"):
            self.assertNotIn(hidden, shown)


class DeleteReplicaCliTests(_Cli):
    def _delete(self, *argv: str) -> tuple[list, list]:
        calls: list = []
        printed: list = []

        def delete(path, **kwargs):
            calls.append((path, kwargs))
            return {"id": "j1", "deleted": True}

        api = SimpleNamespace(delete=delete, get=lambda _path: {"items": []})
        context = SimpleNamespace(api_client=api, output="table", print_output=lambda payload, columns: printed.append(columns))
        with patch("python.cli.commands.job_commands.CommandDependencies", return_value=SimpleNamespace(context=context)):
            JobCommands().delete(self._args("delete", "--id", "j1", *argv))
        return calls, printed

    def test_the_flag_asks_the_server_to_delete_the_replica_and_waits_for_it(self) -> None:
        calls, printed = self._delete("--delete-replica")

        self.assertEqual(calls, [("api/v1/jobs/j1", {"params": {"delete_replica": "true"}, "timeout": 1800})])
        self.assertIn(("replica_deleted", "REPLICA DELETED"), printed[0])

    def test_without_the_flag_the_request_is_unchanged(self) -> None:
        calls, printed = self._delete()

        self.assertEqual(calls, [("api/v1/jobs/j1", {})])
        self.assertEqual(printed[0], [("id", "ID"), ("deleted", "DELETED")])

    def test_the_help_says_the_replica_is_kept_without_the_flag(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            self.parser.parse_args(["job", "delete", "--help"])

        self.assertIn("--delete-replica", output.getvalue())
        self.assertIn("keeps its replica VM on the target unless --delete-replica", output.getvalue())

if __name__ == "__main__":
    unittest.main()
