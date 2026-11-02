# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from python.cli.commands.job_commands import JobCommands, _backup_copy_rows
from python.cli.parsing.parser_builder import build_parser
from python.cli.services.job_service import JobService
from python.cli.services.restore_service import RestoreService
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
FULL = ["--active-full-schedule", "weekly", "--active-full-days", "sun", "--active-full-time", "02:00"]
VM = "vm-1"
COPY_JOB = {"name": "Offsite", "job_type": "backup-copy"}
BACKUP_JOB = {"name": "Nightly", "job_type": "backup"}


class _Cli(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = build_parser()
        self.jobs = JobService(None)

    def _args(self, *argv: str):
        return self.parser.parse_args(["job", *argv])

    def _create_payload(self, *argv: str) -> dict:
        return self.jobs.create_backup_copy_job_payload(self._args("create-backup-copy", *argv))


class CreatePayloadTests(_Cli):
    def test_every_copy_setting_goes_into_the_request(self) -> None:
        payload = self._create_payload(
            "--name", "Offsite", "--mode", "immediate", "--source-job", "Nightly", "--source-job", "Weekly",
            "--source-repo", "Local", "--vms", "sql-01,web-01", "--repo", "S3", "--copy-window", "mon,tue 22:00-06:00",
            "--copy-window", "sat 00:00-24:00", "--retry-count", "2", "--retry-interval", "15", "--retention-days", "90",
            *FULL,
        )

        self.assertEqual(payload["copy_mode"], "immediate")
        self.assertEqual(payload["source_jobs"], ["Nightly", "Weekly"])
        self.assertEqual(payload["source_repositories"], ["Local"])
        self.assertEqual(payload["vms"], ["sql-01", "web-01"])
        self.assertEqual(payload["repo"], "S3")
        self.assertEqual(payload["copy_window"], ["mon,tue 22:00-06:00", "sat 00:00-24:00"])
        self.assertEqual((payload["retry_count"], payload["retry_interval_minutes"]), (2, 15))
        self.assertEqual(payload["retention_days"], 90)
        self.assertEqual(payload["active_full_schedule"]["type"], "weekly")

    def test_a_schedule_and_run_after_job_reach_a_periodic_copy(self) -> None:
        payload = self._create_payload(
            "--name", "Offsite", "--source-job", "Nightly", "--repo", "S3", "--schedule", "daily", "--time", "03:00",
            "--run-after-job", "Nightly", *FULL,
        )

        self.assertEqual(payload["schedule"]["type"], "daily")
        self.assertEqual(payload["run_after_job"], "Nightly")
        self.assertNotIn("copy_mode", payload)

    def test_the_encryption_key_comes_from_the_environment_like_backup_jobs(self) -> None:
        with patch.dict(os.environ, {"ABR_ENCRYPTION_KEY": "placeholder-copy-key-1"}):
            payload = self._create_payload(
                "--name", "Offsite", "--source-job", "Nightly", "--repo", "S3", "--set-encryption", "--encryption-key", *FULL
            )

        self.assertEqual(payload["task_parameters"], {"encryption_enabled": True, "encryption_key": "placeholder-copy-key-1"})

    def test_a_copy_needs_a_source_and_a_destination(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires a source"):
            self._create_payload("--name", "Offsite", "--repo", "S3", *FULL)
        with self.assertRaisesRegex(ValueError, "repository the copies go to"):
            self._create_payload("--name", "Offsite", "--source-job", "Nightly", *FULL)

    def test_backup_only_options_are_not_offered(self) -> None:
        for option in ("--inventory", "--on-quiesce-failure", "--guest-consistency"):
            with self.subTest(option=option), self.assertRaises(SystemExit), patch("sys.stderr"):
                self._args("create-backup-copy", "--name", "x", option, "y")


class EditPayloadTests(_Cli):
    def _edit(self, job: dict, *argv: str) -> dict:
        return self.jobs.build_edit_payload(job, self._args("edit", "--name", job["name"], *argv))

    def test_copy_settings_are_edited_and_none_clears_them(self) -> None:
        payload = self._edit(
            COPY_JOB,
            "--mode", "periodic", "--copy-window", "none", "--source-repo", "none", "--run-after-job", "none", "--all-vms",
        )

        self.assertEqual(payload["copy_mode"], "periodic")
        self.assertEqual(payload["copy_window"], [])
        self.assertEqual(payload["source_repositories"], [])
        self.assertEqual(payload["run_after_job"], "")
        self.assertEqual(payload["vms"], [])

    def test_copy_options_are_refused_on_a_backup_job_and_backup_options_on_a_copy_job(self) -> None:
        with self.assertRaisesRegex(ValueError, "--mode, --copy-window apply to backup copy jobs only"):
            self._edit(BACKUP_JOB, "--mode", "immediate", "--copy-window", "22:00-23:00")
        with self.assertRaisesRegex(ValueError, "--inventory apply to backup jobs only"):
            self._edit(COPY_JOB, "--inventory", "vCenter")


class ViewTests(unittest.TestCase):
    def test_job_info_shows_the_copy_settings_as_rows(self) -> None:
        rows = _backup_copy_rows(
            {
                "copy_mode": "immediate",
                "source_jobs": [{"id": "1", "name": "Nightly"}],
                "source_repositories": [],
                "run_after_job": None,
                "copy_window_summary": "Mon, Tue 22:00-06:00",
                "retry": {"count": 3, "interval_minutes": 10},
            }
        )

        self.assertEqual(
            rows,
            {
                "copy_mode": "immediate",
                "source_jobs": "Nightly",
                "source_repositories": "-",
                "run_after_job": "-",
                "copy_window": "Mon, Tue 22:00-06:00",
                "retry": "3 time(s), 10 minute(s) apart",
            },
        )

    def test_job_list_shows_the_job_type(self) -> None:
        self.assertIn(("job_type", "TYPE"), JobCommands._columns())

    def test_job_list_shows_all_vms_for_a_copy_without_a_vm_filter(self) -> None:
        self.assertEqual(JobCommands._decorate_job({"job_type": "backup-copy", "vm_count": 0})["vm_count"], "all")
        self.assertEqual(JobCommands._decorate_job({"job_type": "backup-copy", "vm_count": 2})["vm_count"], 2)
        self.assertEqual(JobCommands._decorate_job({"job_type": "backup", "vm_count": 0})["vm_count"], 0)

    def test_restore_list_passes_the_job_filter(self) -> None:
        args = build_parser().parse_args(["restore", "list", "--job", "Offsite"])

        self.assertEqual(RestoreService(None).build_restore_list_params(args), {"job": "Offsite"})


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
        REPOSITORIES["src"] = {"id": "src", "name": "Local", "type": "local", "path": tempfile.mkdtemp()}
        REPOSITORIES["dst"] = {"id": "dst", "name": "Offsite", "type": "local", "path": tempfile.mkdtemp()}
        JOBS["nightly"] = abr_v41.normalize_job_record(
            {"id": "nightly", "name": "Nightly", "job_type": "backup", "inventory_id": "vc", "vm_ids": [VM], "vm_names": ["sql-01"], "repo_id": "src", "repo_name": "Local", "task_parameters": {}}
        )
        BACKUPS["f1"] = abr_v41.normalize_backup_point(
            {"id": "f1", "status": "available", "job_id": "nightly", "job_name": "Nightly", "vm_id": VM, "vm_name": "sql-01", "inventory_id": "vc", "repository_id": "src", "execution_type": "ActiveFull", "full_backup": True, "created_at": "2026-11-01T01:00:00+00:00", "chain_id": f"backup::nightly::{VM}::src"}
        )

    def _create(self, *argv: str):
        payload = self._create_payload("--name", "Copy", "--source-job", "Nightly", "--repo", "Offsite", *FULL, *argv)
        return self.client.post("/api/v1/jobs/backup-copy", json=payload)

    def test_a_job_made_by_the_cli_comes_back_with_its_copy_settings(self) -> None:
        response = self._create("--copy-window", "mon,wed 22:00-06:00", "--retry-count", "1")

        self.assertEqual(response.status_code, 200)
        view = response.json()["backup_copy"]
        self.assertEqual(view["copy_window_summary"], "Mon, Wed 22:00-06:00")
        self.assertEqual(view["retry"]["count"], 1)
        self.assertEqual(view["source_jobs"], [{"id": "nightly", "name": "Nightly"}])

    def test_server_refusals_reach_the_cli_with_their_reason(self) -> None:
        for argv, code in (
            (["--copy-window", "thu,mon 22:00-06:00"], "invalid_copy_window"),
            (["--repo", "Local"], "backup_copy_same_repository"),
            (["--mode", "immediate", "--schedule", "daily", "--time", "03:00"], "invalid_job"),
        ):
            with self.subTest(argv=argv):
                payload = self._create_payload("--name", "Copy", "--source-job", "Nightly", "--repo", "Offsite", *FULL, *argv)
                response = self.client.post("/api/v1/jobs/backup-copy", json=payload)

                self.assertEqual((response.status_code, response.json()["error"]["code"]), (422, code))
                self.assertTrue(response.json()["error"]["message"])

    def test_an_edit_from_the_cli_changes_the_copy_and_a_backup_job_refuses_copy_fields(self) -> None:
        job = self._create().json()

        edited = self.client.patch(
            f"/api/v1/jobs/{job['id']}",
            json=self.jobs.build_edit_payload(job, self._args("edit", "--name", "Copy", "--copy-window", "sat 00:00-24:00")),
        )
        refused = self.client.patch("/api/v1/jobs/nightly", json={"copy_mode": "immediate"})

        self.assertEqual(edited.json()["backup_copy"]["copy_window_summary"], "Sat 00:00-24:00")
        self.assertEqual((refused.status_code, refused.json()["error"]["code"]), (422, "invalid_job"))
        self.assertIn("apply to backup copy jobs only", refused.json()["error"]["message"])

    def test_restore_points_can_be_listed_by_job(self) -> None:
        job = self._create().json()
        BACKUPS["c1"] = dict(BACKUPS["f1"], id="c1", job_id=job["id"], job_name="Copy", repository_id="dst", copy_source={"point_id": "f1"})

        listed = self.client.get("/api/v1/restore/points", params={"job": "Copy"}).json()["items"]

        self.assertEqual([(item["id"], item["copy_source"]) for item in listed], [("c1", {"point_id": "f1"})])


if __name__ == "__main__":
    unittest.main()
