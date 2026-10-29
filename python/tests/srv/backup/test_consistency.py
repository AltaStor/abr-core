# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from unittest.mock import patch

from python.cli.parsing.parser_builder import build_parser
from python.cli.services.job_service import JobService
from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    RESOURCES_BY_HV,
    TASKS,
)
from python.srv.services import abr_v41, backup_catalog
from python.srv.services.api_errors import ApiError


def _completed_backup(**result: object) -> dict:
    return {
        "id": "task-consistency",
        "task_type": "backup",
        "status": "completed",
        "hypervisor_id": "",
        "resource_type": "virtual_machine",
        "resource_external_id": "vm-1",
        "job_id": None,
        "updated_at": "2026-10-14T00:00:00+00:00",
        "result": {
            "vm_name": "db-01",
            "vm_external_id": "vm-1",
            "backup_mode": "full",
            "resolved_backup_type": "ActiveFull",
            "resolved_backup_reason": "FirstRun",
            "transferred_bytes": 2048,
            "finished_at": "2026-10-14T00:05:00+00:00",
            **result,
        },
    }


class _Stores(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            store.clear()
            self.addCleanup(store.clear)


class RestorePointConsistencyTests(_Stores):
    def test_the_restore_point_records_how_its_backup_was_taken(self) -> None:
        for consistency, reason in (("quiesced", None), ("crash_consistent", "tools_not_running")):
            with self.subTest(consistency=consistency):
                BACKUPS.clear()
                record = backup_catalog.register_backup_from_completed_task(
                    _completed_backup(consistency=consistency, consistency_reason=reason)
                )

                self.assertEqual(record["consistency"], consistency)
                self.assertEqual(record["consistency_reason"], reason)

    def test_a_backup_that_did_not_report_it_stays_unknown(self) -> None:
        record = backup_catalog.register_backup_from_completed_task(_completed_backup())

        self.assertIsNone(record["consistency"])
        self.assertIsNone(record["consistency_reason"])

    def test_backup_info_and_the_restore_point_list_show_it(self) -> None:
        record = backup_catalog.register_backup_from_completed_task(
            _completed_backup(consistency="crash_consistent", consistency_reason="quiesce_failed")
        )

        chain_point = abr_v41.backup_chain_to_dto([record])["restore_points"][0]
        listed = next(item for item in abr_v41.restore_point_list("vm-1") if item["id"] == record["id"])

        self.assertEqual(chain_point["consistency"], "crash_consistent")
        self.assertEqual(chain_point["consistency_reason"], "quiesce_failed")
        self.assertEqual(listed["consistency"], "crash_consistent")
        self.assertEqual(listed["consistency_state"], "crash_consistent")
        self.assertEqual(listed["consistency_reason"], "quiesce_failed")

    def test_the_session_shows_it(self) -> None:
        task = _completed_backup(consistency="quiesced", consistency_reason=None)
        TASKS[task["id"]] = task

        session = abr_v41.get_session(task["id"])

        self.assertEqual(session["consistency"], "quiesced")
        self.assertIsNone(session["consistency_reason"])


class QuiesceFailureOptionTests(_Stores):
    def setUp(self) -> None:
        super().setUp()
        for patcher in (
            patch.object(abr_v41, "assert_inventory_registrable", return_value={"identity": {}}),
            patch.object(abr_v41, "AUTO_DISCOVER_ON_INVENTORY_ADD", False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _job(self, **task_parameters: object) -> dict:
        REPOSITORIES["repo-1"] = {"id": "repo-1", "name": "Repo One", "type": "smb", "metadata": {"server": "nas.example", "share": "backups"}, "path": "/tmp/repo-1"}
        inventory = abr_v41.create_inventory_record(
            {"name": "hv", "type": "hyper-v", "address": "192.168.10.10", "username": "administrator", "password": "secret"}
        )
        RESOURCES_BY_HV[str(inventory["id"])] = {
            ("virtual_machine", "id-db-01"): {
                "resource_type": "virtual_machine",
                "external_id": "id-db-01",
                "name": "db-01",
                "payload": {"path": "/Production/db-01"},
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            }
        }
        return abr_v41.create_backup_job(
            {
                "name": "Nightly",
                "vms": ["db-01"],
                "inventory_id": inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "task_parameters": task_parameters,
            }
        )

    def test_a_job_fails_on_a_failed_quiesce_unless_told_otherwise(self) -> None:
        job = self._job()

        self.assertEqual(job["on_quiesce_failure"], "fail")
        self.assertNotIn("on_quiesce_failure", JOBS[job["id"]]["task_parameters"])

    def test_a_job_can_fall_back_to_a_crash_consistent_backup_and_back(self) -> None:
        job = self._job(on_quiesce_failure="crash-consistent")

        self.assertEqual(job["on_quiesce_failure"], "crash_consistent")
        self.assertEqual(JOBS[job["id"]]["task_parameters"]["on_quiesce_failure"], "crash_consistent")

        edited = abr_v41.edit_job_record(job["id"], {"task_parameters": {"on_quiesce_failure": "fail"}})

        self.assertEqual(edited["on_quiesce_failure"], "fail")

    def test_the_option_reaches_every_backup_session_of_the_job(self) -> None:
        job = self._job(on_quiesce_failure="crash_consistent")

        started = abr_v41.start_job_record(job["id"])

        for item in started["items"]:
            self.assertEqual(TASKS[item["id"]]["parameters"]["on_quiesce_failure"], "crash_consistent")

    def test_an_unknown_choice_is_refused(self) -> None:
        with self.assertRaises(ApiError) as caught:
            self._job(on_quiesce_failure="ignore")

        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("fail, crash_consistent", caught.exception.message)
        self.assertEqual(JOBS, {})


class QuiesceFailureCliTests(unittest.TestCase):
    def test_job_create_and_edit_take_the_option(self) -> None:
        for argv in (
            ["job", "create", "--name", "Nightly", "--repository", "Repo One", "--vm", "db-01", "--on-quiesce-failure", "crash-consistent"],
            ["job", "edit", "--name", "Nightly", "--on-quiesce-failure", "crash-consistent"],
        ):
            with self.subTest(command=argv[1]):
                args = build_parser().parse_args(argv)

                self.assertEqual(
                    JobService._build_task_parameter_updates(args)["on_quiesce_failure"], "crash_consistent"
                )

    def test_without_the_flag_the_job_keeps_its_setting(self) -> None:
        args = build_parser().parse_args(["job", "edit", "--name", "Nightly", "--description", "x"])

        self.assertNotIn("on_quiesce_failure", JobService._build_task_parameter_updates(args))

    def test_an_unknown_choice_is_refused_by_the_cli(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["job", "edit", "--name", "Nightly", "--on-quiesce-failure", "ignore"])

    def test_the_restore_point_tables_have_a_consistency_column(self) -> None:
        from python.cli.commands.backup_commands import RESTORE_POINT_COLUMNS
        from python.cli.commands.restore_commands import RestoreCommands

        self.assertIn(("consistency", "CONSISTENCY"), RESTORE_POINT_COLUMNS)
        self.assertIn(("consistency", "CONSISTENCY"), RestoreCommands._point_columns())


if __name__ == "__main__":
    unittest.main()
