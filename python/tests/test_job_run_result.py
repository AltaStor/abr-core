# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from python.cli.commands.job_commands import JobCommands, _last_run_issues, _last_run_text
from python.srv.core.job_scheduler import JobScheduler
from python.srv.core.mongo_db_wrapper import BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS
from python.srv.services import abr_v41

VMS = ("db-01", "web-01", "app-01")


class _Catalog(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            store.clear()
            self.addCleanup(store.clear)
        REPOSITORIES["repo-1"] = {"id": "repo-1", "name": "Repo One", "type": "local", "path": "/tmp/repo-1"}
        for patcher in (
            patch.object(abr_v41, "assert_inventory_registrable", return_value={"identity": {}}),
            patch.object(abr_v41, "AUTO_DISCOVER_ON_INVENTORY_ADD", False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _forget_job() -> None:
        for store in (JOBS, TASKS, HYPERVISORS, RESOURCES_BY_HV):
            store.clear()

    def _job(self, names: tuple[str, ...] = VMS) -> dict:
        inventory = abr_v41.create_inventory_record(
            {"name": "hv", "type": "hyper-v", "address": "192.168.10.10", "username": "administrator", "password": "secret"}
        )
        now = datetime.now(timezone.utc).isoformat()
        RESOURCES_BY_HV[str(inventory["id"])] = {
            ("virtual_machine", f"id-{name}"): {
                "resource_type": "virtual_machine",
                "external_id": f"id-{name}",
                "name": name,
                "payload": {"path": f"/Production/{name}"},
                "discovered_at": now,
            }
            for name in names
        }
        return abr_v41.create_backup_job(
            {
                "name": "Nightly",
                "vms": list(names),
                "inventory_id": inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
            }
        )

    @staticmethod
    def _finish(session_id: str, status: str, *, message: str | None = None, warnings: list | None = None) -> None:
        task = TASKS[session_id]
        task["status"] = status
        task["result"] = {**task.get("result", {}), "message": message, "finished_at": datetime.now(timezone.utc).isoformat()}
        if warnings:
            task["warnings"] = warnings

    def _run(self, job: dict) -> tuple[list[str], dict]:
        started = abr_v41.start_job_record(str(job["id"]))
        return [item["id"] for item in started["items"]], started


class RunRecordTests(_Catalog):
    def test_the_sessions_of_one_start_share_a_run_and_the_job_remembers_all_of_them(self) -> None:
        job = self._job()

        sessions, _started = self._run(job)

        run_ids = {TASKS[session_id]["run_id"] for session_id in sessions}
        self.assertEqual(len(run_ids), 1)
        last_run = JOBS[str(job["id"])]["last_run"]
        self.assertEqual(last_run["id"], run_ids.pop())
        self.assertEqual(last_run["session_ids"], sessions)
        self.assertEqual(last_run["trigger"], "manual")
        self.assertEqual({abr_v41.get_session(session_id)["run_id"] for session_id in sessions}, {last_run["id"]})

    def test_another_start_is_another_run(self) -> None:
        job = self._job()
        first, _ = self._run(job)
        second, _ = self._run(job)

        self.assertNotEqual(TASKS[first[0]]["run_id"], TASKS[second[0]]["run_id"])
        self.assertEqual(JOBS[str(job["id"])]["last_run"]["session_ids"], second)

    def test_the_sessions_of_a_run_can_be_listed_on_their_own(self) -> None:
        job = self._job()
        first, _ = self._run(job)
        second, _ = self._run(job)
        run_id = TASKS[second[0]]["run_id"]

        page = abr_v41.list_sessions_page(run_id=run_id)

        self.assertEqual(sorted(item["id"] for item in page["items"]), sorted(second))
        self.assertEqual(page["total"], 3)


class RunResultTests(_Catalog):
    def test_a_failed_vm_is_shown_whatever_its_place_in_the_job(self) -> None:
        for failing in (0, 2):
            with self.subTest(failing_vm=VMS[failing]):
                self._forget_job()
                job = self._job()
                sessions, _ = self._run(job)
                for index, session_id in enumerate(sessions):
                    if index == failing:
                        self._finish(session_id, "failed", message="vCenter task failed: Exceeded the maximum number of permitted snapshots.")
                    else:
                        self._finish(session_id, "completed")

                summary = abr_v41.job_to_dto(JOBS[str(job["id"])])["last_run"]

                self.assertEqual(summary["result"], "failed")
                self.assertEqual((summary["vm_count"], summary["succeeded"], summary["failed"]), (3, 2, 1))
                failed = [vm for vm in summary["vms"] if vm["result"] == "failed"]
                self.assertEqual([vm["vm_name"] for vm in failed], [VMS[failing]])
                self.assertIn("Exceeded the maximum number of permitted snapshots", failed[0]["message"])
                self.assertEqual(failed[0]["session_ids"], [sessions[failing]])
                self.assertIsNotNone(summary["finished_at"])

    def test_a_run_still_going_is_running_until_its_last_vm_ends(self) -> None:
        job = self._job()
        sessions, _ = self._run(job)
        self._finish(sessions[0], "failed", message="boom")
        self._finish(sessions[1], "completed")

        summary = abr_v41.job_to_dto(JOBS[str(job["id"])])["last_run"]

        self.assertEqual((summary["result"], summary["running"], summary["failed"]), ("running", 1, 1))
        self.assertIsNone(summary["finished_at"])

    def test_results_rank_failed_over_stopped_over_warning_over_success(self) -> None:
        cases = (
            (("completed", "completed", "completed"), "success"),
            (("completed", "completed-warn", "completed"), "warning"),
            (("completed-warn", "stopped", "completed"), "stopped"),
            (("stopped", "failed", "completed-warn"), "failed"),
        )
        for statuses, expected in cases:
            with self.subTest(statuses=statuses):
                self._forget_job()
                job = self._job()
                sessions, _ = self._run(job)
                for session_id, status in zip(sessions, statuses):
                    if status == "completed-warn":
                        self._finish(session_id, "completed", warnings=[{"message": "Retention could not remove a point"}])
                    else:
                        self._finish(session_id, status, message="Session stopped by user" if status == "stopped" else None)

                self.assertEqual(abr_v41.job_to_dto(JOBS[str(job["id"])])["last_run"]["result"], expected)

    def test_a_vm_with_two_sessions_counts_once_and_takes_the_worse_result(self) -> None:
        job = self._job(("db-01",))
        sessions, _ = self._run(job)
        self._finish(sessions[0], "completed")
        synthetic = dict(TASKS[sessions[0]], id="synthetic-1")
        TASKS["synthetic-1"] = synthetic
        self._finish("synthetic-1", "failed", message="merge failed")
        JOBS[str(job["id"])]["last_run"]["session_ids"].append("synthetic-1")

        summary = abr_v41.job_to_dto(JOBS[str(job["id"])])["last_run"]

        self.assertEqual((summary["vm_count"], summary["result"]), (1, "failed"))
        self.assertEqual(summary["vms"][0]["session_ids"], [sessions[0], "synthetic-1"])

    def test_a_job_that_has_not_run_since_the_upgrade_has_no_result(self) -> None:
        job = self._job()
        JOBS[str(job["id"])].update(last_run_at="2026-10-01T00:00:00+00:00", last_session_id="old-session")

        self.assertIsNone(abr_v41.job_to_dto(JOBS[str(job["id"])])["last_run"])

    def test_sessions_that_are_gone_are_left_out(self) -> None:
        job = self._job()
        sessions, _ = self._run(job)
        self._finish(sessions[0], "completed")
        TASKS.pop(sessions[1])
        TASKS.pop(sessions[2])

        summary = abr_v41.job_to_dto(JOBS[str(job["id"])])["last_run"]
        self.assertEqual((summary["vm_count"], summary["result"]), (1, "success"))

        TASKS.pop(sessions[0])
        self.assertIsNone(abr_v41.job_to_dto(JOBS[str(job["id"])])["last_run"])


class ScheduledRunTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (HYPERVISORS, JOBS, RESOURCES_BY_HV, TASKS):
            store.clear()
            self.addCleanup(store.clear)

    def test_a_scheduled_run_gets_one_run_id_for_all_its_vms(self) -> None:
        created_at = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        HYPERVISORS["hv-1"] = {"id": "hv-1", "kind": "mock"}
        RESOURCES_BY_HV["hv-1"] = {
            ("virtual_machine", vm_id): {"resource_type": "virtual_machine", "external_id": vm_id, "name": f"demo-{vm_id}", "payload": {}, "discovered_at": created_at}
            for vm_id in ("vm-1", "vm-2")
        }
        JOBS["job-1"] = {
            "id": "job-1",
            "job_type": "backup",
            "hypervisor_id": "hv-1",
            "status": "active",
            "metadata": {"vm_external_ids": ["vm-1", "vm-2"], "task_parameters": {}},
            "schedule": {"schedule_type": "interval", "enabled": True, "timezone": "UTC", "interval_minutes": 10, "start_time": None, "weekdays": [], "day_of_month": None},
            "created_at": created_at,
        }

        JobScheduler(poll_interval_seconds=0.1)._dispatch_due_jobs()

        self.assertEqual(len(TASKS), 2)
        run_ids = {task["run_id"] for task in TASKS.values()}
        self.assertEqual(len(run_ids), 1)
        last_run = JOBS["job-1"]["last_run"]
        self.assertEqual((last_run["id"], last_run["trigger"]), (run_ids.pop(), "schedule"))
        self.assertEqual(sorted(last_run["session_ids"]), sorted(TASKS))


class CliLastResultTests(unittest.TestCase):
    RUN = {
        "id": "run-1",
        "result": "failed",
        "vm_count": 3,
        "succeeded": 2,
        "warning": 0,
        "failed": 1,
        "stopped": 0,
        "running": 0,
        "vms": [
            {"vm_name": "db-01", "result": "failed", "session_ids": ["s-1"], "message": "Exceeded the maximum number of permitted snapshots."},
            {"vm_name": "web-01", "result": "success", "session_ids": ["s-2"], "message": None},
            {"vm_name": "app-01", "result": "success", "session_ids": ["s-3"], "message": None},
        ],
    }

    def test_the_result_reads_as_text(self) -> None:
        self.assertEqual(_last_run_text(self.RUN), "Failed (1 of 3 VMs failed)")
        self.assertEqual(_last_run_text({**self.RUN, "result": "running", "running": 1}), "Running (2/3 VMs done)")
        self.assertEqual(_last_run_text({**self.RUN, "result": "success", "vm_count": 1}), "Success")
        self.assertEqual(_last_run_text({**self.RUN, "result": "warning", "warning": 1}), "Warning (1 of 3 VMs with warnings)")
        self.assertEqual(_last_run_text(None), "")
        self.assertEqual(
            _last_run_issues(self.RUN), "db-01: Exceeded the maximum number of permitted snapshots. (session s-1)"
        )

    def test_job_list_shows_the_last_result_column(self) -> None:
        job = {"id": "job-1", "name": "Nightly", "vm_ids": ["a", "b", "c"], "last_run_at": "2026-10-11T03:00:00+00:00", "last_run": self.RUN}
        client = SimpleNamespace(get=lambda path, **kwargs: {"items": [job]})
        context = SimpleNamespace(api_client=client, output="table", print_output=lambda payload, columns=None: printed.append((payload, columns)))
        printed: list = []

        with patch("python.cli.commands.job_commands.CommandDependencies", lambda args: SimpleNamespace(context=context)):
            JobCommands().list(SimpleNamespace())

        payload, columns = printed[0]
        self.assertIn(("last_result", "LAST RESULT"), columns)
        self.assertEqual(payload["items"][0]["last_result"], "Failed (1 of 3 VMs failed)")

    def test_job_info_names_the_failed_vm_and_its_session(self) -> None:
        job = {"id": "job-1", "name": "Nightly", "vm_ids": ["a", "b", "c"], "last_run": self.RUN}
        client = SimpleNamespace(get=lambda path, **kwargs: job)
        printed: list = []
        context = SimpleNamespace(api_client=client, output="table", print_output=lambda payload, columns=None: printed.append(payload))

        with patch("python.cli.commands.job_commands.CommandDependencies", lambda args: SimpleNamespace(context=context)), patch.object(
            JobCommands, "_resolve_id", staticmethod(lambda deps, args: "job-1")
        ), redirect_stdout(io.StringIO()):
            JobCommands().info(SimpleNamespace())

        shown = printed[0]
        self.assertEqual(shown["last_result"], "Failed (1 of 3 VMs failed)")
        self.assertEqual(shown["last_run_id"], "run-1")
        self.assertIn("db-01: Exceeded the maximum number of permitted snapshots. (session s-1)", shown["last_run_issues"])
        self.assertNotIn("last_run", shown)


if __name__ == "__main__":
    unittest.main()
