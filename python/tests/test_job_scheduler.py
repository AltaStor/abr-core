# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pathlib
import sys
import unittest
from unittest.mock import patch

try:
    from python.srv.core.job_scheduler import JobScheduler
    from python.srv.core.mongo_db_wrapper import BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS
    from python.srv.services import abr_v41
    from python.srv.services.abr_v41 import normalize_job_record
    from python.srv.services.synthetic_full import is_synthetic_full_task
except ModuleNotFoundError:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    venv_lib = repo_root / ".venv" / "lib"
    for site_packages in sorted(venv_lib.glob("python*/site-packages")):
        sys.path.insert(0, str(site_packages))
    from python.srv.core.job_scheduler import JobScheduler
    from python.srv.core.mongo_db_wrapper import BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS
    from python.srv.services import abr_v41
    from python.srv.services.abr_v41 import normalize_job_record
    from python.srv.services.synthetic_full import is_synthetic_full_task


class JobSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        HYPERVISORS.clear()
        JOBS.clear()
        RESOURCES_BY_HV.clear()
        TASKS.clear()
        self.scheduler = JobScheduler(poll_interval_seconds=0.1)

    def tearDown(self) -> None:
        HYPERVISORS.clear()
        JOBS.clear()
        RESOURCES_BY_HV.clear()
        TASKS.clear()

    def _register_vm_resources(self, *vm_ids: str) -> None:
        HYPERVISORS["hv-1"] = {"id": "hv-1", "kind": "mock"}
        discovered_at = datetime.now(timezone.utc).isoformat()
        RESOURCES_BY_HV["hv-1"] = {
            ("virtual_machine", vm_id): {
                "resource_type": "virtual_machine",
                "external_id": vm_id,
                "name": f"demo-{vm_id}",
                "payload": {},
                "discovered_at": discovered_at,
            }
            for vm_id in vm_ids
        }

    def test_due_interval_backup_job_enqueues_vm_task(self) -> None:
        created_at = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        HYPERVISORS["hv-1"] = {"id": "hv-1", "kind": "mock"}
        RESOURCES_BY_HV["hv-1"] = {
            ("virtual_machine", "vm-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-1",
                "name": "demo-vm",
                "payload": {},
                "discovered_at": created_at,
            }
        }
        JOBS["job-1"] = {
            "id": "job-1",
            "job_type": "backup",
            "hypervisor_id": "hv-1",
            "status": "active",
            "metadata": {
                "vm_external_id": "vm-1",
                "task_parameters": {"duration_seconds": 5},
            },
            "schedule": {
                "schedule_type": "interval",
                "enabled": True,
                "timezone": "UTC",
                "interval_minutes": 10,
                "start_time": None,
                "weekdays": [],
                "day_of_month": None,
            },
            "created_at": created_at,
        }

        self.scheduler._dispatch_due_jobs()

        self.assertEqual(len(TASKS), 1)
        task = next(iter(TASKS.values()))
        self.assertEqual(task["job_id"], "job-1")
        self.assertEqual(task["task_type"], "backup")
        self.assertEqual(task["resource_type"], "virtual_machine")
        self.assertEqual(task["resource_external_id"], "vm-1")
        self.assertEqual(task["parameters"]["duration_seconds"], 5)
        self.assertEqual(task["parameters"]["resolved_backup_type"], "ActiveFull")

        job = JOBS["job-1"]
        self.assertEqual(job["last_task_id"], task["id"])
        self.assertEqual(job["last_session_id"], task["id"])
        self.assertEqual(job["status"], "active")
        self.assertEqual(job["last_run_at"], task["created_at"])
        self.assertIsInstance(job["next_incremental_run_at"], str)
        self.assertIsInstance(job["last_scheduled_at"], str)

    def test_due_interval_backup_job_reads_task_parameters_from_metadata(self) -> None:
        created_at = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        HYPERVISORS["hv-1"] = {"id": "hv-1", "kind": "vcenter"}
        RESOURCES_BY_HV["hv-1"] = {
            ("virtual_machine", "vm-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-1",
                "name": "demo-vm",
                "payload": {},
                "discovered_at": created_at,
            }
        }
        JOBS["job-1"] = {
            "id": "job-1",
            "job_type": "backup",
            "hypervisor_id": "hv-1",
            "status": "active",
            "metadata": {
                "vm_external_id": "vm-1",
                "task_parameters": {"incremental": True, "compression_level": 6},
            },
            "schedule": {
                "schedule_type": "interval",
                "enabled": True,
                "timezone": "UTC",
                "interval_minutes": 10,
                "start_time": None,
                "weekdays": [],
                "day_of_month": None,
            },
            "created_at": created_at,
        }

        self.scheduler._dispatch_due_jobs()

        self.assertEqual(len(TASKS), 1)
        task = next(iter(TASKS.values()))
        self.assertEqual(task["parameters"]["compression_level"], 6)
        self.assertEqual(task["parameters"]["resolved_backup_type"], "ActiveFull")
        self.assertFalse(task["parameters"]["incremental"])

    def test_due_interval_backup_job_preserves_top_level_task_parameters(self) -> None:
        created_at = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        HYPERVISORS["hv-1"] = {"id": "hv-1", "kind": "vcenter"}
        RESOURCES_BY_HV["hv-1"] = {
            ("virtual_machine", "vm-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-1",
                "name": "demo-vm",
                "payload": {},
                "discovered_at": created_at,
            }
        }
        JOBS["job-1"] = {
            "id": "job-1",
            "job_type": "backup",
            "hypervisor_id": "hv-1",
            "status": "active",
            "vm_ids": ["vm-1"],
            "task_parameters": {
                "compression_level": 6,
                "encryption_enabled": True,
                "encryption_key": "demo-passphrase-1",
            },
            "schedule": {
                "schedule_type": "interval",
                "enabled": True,
                "timezone": "UTC",
                "interval_minutes": 10,
                "start_time": None,
                "weekdays": [],
                "day_of_month": None,
            },
            "created_at": created_at,
        }

        self.scheduler._dispatch_due_jobs()

        self.assertEqual(len(TASKS), 1)
        task = next(iter(TASKS.values()))
        for key, value in {
            "compression_level": 6,
            "encryption_enabled": True,
            "encryption_key": "demo-passphrase-1",
        }.items():
            self.assertEqual(task["parameters"][key], value)

    def test_due_daily_backup_job_enqueues_vm_task(self) -> None:
        created_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self._register_vm_resources("vm-1")
        JOBS["job-1"] = {
            "id": "job-1",
            "job_type": "backup",
            "hypervisor_id": "hv-1",
            "status": "active",
            "metadata": {"vm_external_id": "vm-1"},
            "schedule": {
                "schedule_type": "daily",
                "enabled": True,
                "timezone": "UTC",
                "start_time": "00:00",
            },
            "created_at": created_at,
        }

        self.scheduler._dispatch_due_jobs()

        self.assertEqual(len(TASKS), 1)
        self.assertEqual(next(iter(TASKS.values()))["resource_external_id"], "vm-1")
        self.assertGreater(datetime.fromisoformat(JOBS["job-1"]["next_incremental_run_at"]), datetime.now(timezone.utc))

    def test_due_weekly_backup_job_enqueues_vm_task(self) -> None:
        created_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        today = datetime.now(timezone.utc).strftime("%A").lower()
        self._register_vm_resources("vm-1")
        JOBS["job-1"] = {
            "id": "job-1",
            "job_type": "backup",
            "hypervisor_id": "hv-1",
            "status": "active",
            "metadata": {"vm_external_id": "vm-1"},
            "schedule": {
                "schedule_type": "weekly",
                "enabled": True,
                "timezone": "UTC",
                "start_time": "00:00",
                "weekdays": [today],
            },
            "created_at": created_at,
        }

        self.scheduler._dispatch_due_jobs()

        self.assertEqual(len(TASKS), 1)
        self.assertEqual(next(iter(TASKS.values()))["resource_external_id"], "vm-1")

    def test_due_monthly_backup_job_enqueues_vm_task(self) -> None:
        created_at = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        today = datetime.now(timezone.utc).day
        self._register_vm_resources("vm-1")
        JOBS["job-1"] = {
            "id": "job-1",
            "job_type": "backup",
            "hypervisor_id": "hv-1",
            "status": "active",
            "metadata": {"vm_external_id": "vm-1"},
            "schedule": {
                "schedule_type": "monthly",
                "enabled": True,
                "timezone": "UTC",
                "start_time": "00:00",
                "day_of_month": today,
            },
            "created_at": created_at,
        }

        self.scheduler._dispatch_due_jobs()

        self.assertEqual(len(TASKS), 1)
        self.assertEqual(next(iter(TASKS.values()))["resource_external_id"], "vm-1")

    def test_scheduled_multi_vm_job_enqueues_one_task_per_vm(self) -> None:
        created_at = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        self._register_vm_resources("vm-1", "vm-2")
        JOBS["job-1"] = {
            "id": "job-1",
            "job_type": "backup",
            "hypervisor_id": "hv-1",
            "status": "active",
            "metadata": {"vm_external_ids": ["vm-1", "vm-2"]},
            "schedule": {
                "schedule_type": "interval",
                "enabled": True,
                "timezone": "UTC",
                "interval_minutes": 10,
            },
            "created_at": created_at,
        }

        self.scheduler._dispatch_due_jobs()

        self.assertEqual(
            sorted(task["resource_external_id"] for task in TASKS.values()),
            ["vm-1", "vm-2"],
        )
        self.assertEqual(JOBS["job-1"]["last_task_id"], list(TASKS.values())[-1]["id"])

    def test_scheduler_does_not_enqueue_duplicate_task_while_previous_one_is_pending(self) -> None:
        created_at = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        HYPERVISORS["hv-1"] = {"id": "hv-1", "kind": "mock"}
        RESOURCES_BY_HV["hv-1"] = {
            ("virtual_machine", "vm-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-1",
                "name": "demo-vm",
                "payload": {},
                "discovered_at": created_at,
            }
        }
        JOBS["job-1"] = {
            "id": "job-1",
            "job_type": "backup",
            "hypervisor_id": "hv-1",
            "status": "active",
            "metadata": {"vm_external_id": "vm-1"},
            "schedule": {
                "schedule_type": "interval",
                "enabled": True,
                "timezone": "UTC",
                "interval_minutes": 10,
                "start_time": None,
                "weekdays": [],
                "day_of_month": None,
            },
            "created_at": created_at,
        }
        TASKS["task-existing"] = {
            "id": "task-existing",
            "job_id": "job-1",
            "task_type": "backup",
            "hypervisor_id": "hv-1",
            "resource_type": "virtual_machine",
            "resource_external_id": "vm-1",
            "status": "running",
            "parameters": {},
            "result": None,
            "created_at": created_at,
            "updated_at": created_at,
        }

        self.scheduler._dispatch_due_jobs()

        self.assertEqual(list(TASKS.keys()), ["task-existing"])

    def test_scheduler_marks_stale_running_task_failed_and_enqueues_replacement(self) -> None:
        created_at = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        stale_updated_at = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        HYPERVISORS["hv-1"] = {"id": "hv-1", "kind": "mock"}
        RESOURCES_BY_HV["hv-1"] = {
            ("virtual_machine", "vm-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-1",
                "name": "demo-vm",
                "payload": {},
                "discovered_at": created_at,
            }
        }
        JOBS["job-1"] = {
            "id": "job-1",
            "job_type": "backup",
            "hypervisor_id": "hv-1",
            "status": "active",
            "metadata": {
                "vm_external_id": "vm-1",
                "task_parameters": {"tool_timeout_seconds": 300},
            },
            "schedule": {
                "schedule_type": "interval",
                "enabled": True,
                "timezone": "UTC",
                "interval_minutes": 10,
                "start_time": None,
                "weekdays": [],
                "day_of_month": None,
            },
            "created_at": created_at,
        }
        TASKS["task-stale"] = {
            "id": "task-stale",
            "job_id": "job-1",
            "task_type": "backup",
            "hypervisor_id": "hv-1",
            "resource_type": "virtual_machine",
            "resource_external_id": "vm-1",
            "status": "running",
            "parameters": {"tool_timeout_seconds": 300},
            "result": {"stage": "transfer", "progress": 25},
            "created_at": created_at,
            "updated_at": stale_updated_at,
        }

        self.scheduler._dispatch_due_jobs()

        self.assertEqual(TASKS["task-stale"]["status"], "failed")
        self.assertIn("marked failed", TASKS["task-stale"]["result"]["message"])
        self.assertEqual(len(TASKS), 2)
        replacement_tasks = [task for task_id, task in TASKS.items() if task_id != "task-stale"]
        self.assertEqual(len(replacement_tasks), 1)
        replacement = replacement_tasks[0]
        self.assertEqual(replacement["job_id"], "job-1")
        self.assertEqual(replacement["status"], "queued")

    def test_scheduler_handles_mixed_naive_and_aware_timestamps(self) -> None:
        created_at = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        stale_updated_at = (datetime.now(timezone.utc) - timedelta(minutes=20)).replace(tzinfo=None).isoformat()
        HYPERVISORS["hv-1"] = {"id": "hv-1", "kind": "mock"}
        RESOURCES_BY_HV["hv-1"] = {
            ("virtual_machine", "vm-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-1",
                "name": "demo-vm",
                "payload": {},
                "discovered_at": created_at,
            }
        }
        JOBS["job-1"] = {
            "id": "job-1",
            "job_type": "backup",
            "hypervisor_id": "hv-1",
            "status": "active",
            "metadata": {
                "vm_external_id": "vm-1",
                "task_parameters": {"tool_timeout_seconds": 300},
            },
            "schedule": {
                "schedule_type": "interval",
                "enabled": True,
                "timezone": "UTC",
                "interval_minutes": 10,
                "start_time": None,
                "weekdays": [],
                "day_of_month": None,
            },
            "created_at": created_at,
        }
        TASKS["task-stale"] = {
            "id": "task-stale",
            "job_id": "job-1",
            "task_type": "backup",
            "hypervisor_id": "hv-1",
            "resource_type": "virtual_machine",
            "resource_external_id": "vm-1",
            "status": "running",
            "parameters": {"tool_timeout_seconds": 300},
            "result": {"stage": "transfer", "progress": 25},
            "created_at": created_at,
            "updated_at": stale_updated_at,
        }

        self.scheduler._dispatch_due_jobs()

        self.assertEqual(TASKS["task-stale"]["status"], "failed")


class SyntheticFullSchedulingTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (HYPERVISORS, JOBS, RESOURCES_BY_HV, TASKS, BACKUPS, REPOSITORIES):
            store.clear()
            self.addCleanup(store.clear)
        self.scheduler = JobScheduler(poll_interval_seconds=0.1)

        HYPERVISORS["hv-1"] = {"id": "hv-1", "kind": "mock"}
        RESOURCES_BY_HV["hv-1"] = {
            ("virtual_machine", "vm-1"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-1",
                "name": "demo-vm",
                "payload": {},
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            }
        }
        REPOSITORIES["repo-1"] = {
            "id": "repo-1",
            "name": "Repo",
            "type": "local",
            "path": "/tmp/repo-1",
        }
        BACKUPS["point-1"] = {
            "id": "point-1",
            "chain_id": "backup::job-1::vm-1::repo-1",
            "job_id": "job-1",
            "vm_id": "vm-1",
            "repository_id": "repo-1",
            "status": "available",
            "execution_type": "ActiveFull",
            "created_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        }

    @staticmethod
    def _due_daily() -> dict:
        moment = datetime.now(timezone.utc) - timedelta(minutes=1)
        return {"type": "daily", "time": moment.strftime("%H:%M"), "timezone": "UTC"}

    @staticmethod
    def _weekday(offset: int = 0) -> str:
        names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        return names[(datetime.now(timezone.utc).weekday() + offset) % 7]

    def _register_job(self, **overrides) -> None:
        JOBS["job-1"] = {
            "id": "job-1",
            "name": "nightly",
            "job_type": "backup",
            "inventory_id": "hv-1",
            "hypervisor_id": "hv-1",
            "status": "active",
            "repo_id": "repo-1",
            "repo_name": "Repo",
            "vm_ids": ["vm-1"],
            "vm_names": ["demo-vm"],
            "metadata": {"vm_external_id": "vm-1"},
            "task_parameters": {"compression_level": 3},
            "schedule": {
                "schedule_type": "interval",
                "enabled": True,
                "timezone": "UTC",
                "interval_minutes": 10,
                "start_time": None,
                "weekdays": [],
                "day_of_month": None,
            },
            "created_at": (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(),
            **overrides,
        }

    def _dispatch(self) -> list[dict]:
        self.scheduler._dispatch_due_jobs()
        return sorted(TASKS.values(), key=lambda task: str(task.get("created_at")))

    @staticmethod
    def _types(tasks: list[dict]) -> list[str]:
        return [task["parameters"]["resolved_backup_type"] for task in tasks]

    def test_a_due_synthetic_schedule_takes_todays_increment_before_merging(self) -> None:
        self._register_job(synthetic_full_schedule=self._due_daily())

        tasks = self._dispatch()

        self.assertEqual(self._types(tasks), ["Incremental", "SyntheticFull"])
        self.assertEqual(tasks[1]["parameters"]["requires_task_id"], tasks[0]["id"])
        self.assertLess(tasks[0]["created_at"], tasks[1]["created_at"])

    def test_a_scheduled_synthetic_full_reaches_the_synthetic_engine(self) -> None:
        self._register_job(synthetic_full_schedule=self._due_daily())

        synthetic = self._dispatch()[1]

        self.assertTrue(is_synthetic_full_task(synthetic))
        self.assertEqual(synthetic["parameters"]["repository_id"], "repo-1")
        self.assertEqual(synthetic["parameters"]["resolved_backup_reason"], "SyntheticFullSchedule")

    def test_a_job_without_a_synthetic_schedule_only_takes_increments(self) -> None:
        self._register_job()

        self.assertEqual(self._types(self._dispatch()), ["Incremental"])

    def test_a_synthetic_schedule_that_is_not_due_leaves_the_base_schedule_alone(self) -> None:
        self._register_job(
            synthetic_full_schedule={
                "type": "weekly",
                "days": [self._weekday(3)],
                "time": "03:00",
                "timezone": "UTC",
            }
        )

        self.assertEqual(self._types(self._dispatch()), ["Incremental"])

    def test_a_disabled_synthetic_schedule_keeps_its_settings_but_never_fires(self) -> None:
        schedule = dict(self._due_daily(), enabled=False)
        self._register_job(synthetic_full_schedule=schedule)

        self.assertEqual(self._types(self._dispatch()), ["Incremental"])

    def test_a_legacy_record_keeps_the_next_run_it_already_stored(self) -> None:
        self._register_job(next_run_at=(datetime.now(timezone.utc) + timedelta(hours=4)).isoformat())
        JOBS["job-1"].pop("next_incremental_run_at", None)

        self.assertEqual(self._dispatch(), [])

    def test_a_job_without_a_base_schedule_still_runs_its_full_schedules(self) -> None:
        self._register_job(active_full_schedule=self._due_daily())
        JOBS["job-1"]["schedule"] = None

        self.assertEqual(self._types(self._dispatch()), ["ActiveFull"])

    def test_a_switched_off_job_runs_nothing_even_without_a_base_schedule(self) -> None:
        self._register_job(active_full_schedule=self._due_daily())
        JOBS["job-1"]["schedule"] = None
        JOBS["job-1"]["enabled"] = False

        self.assertEqual(self._types(self._dispatch()), [])

    def test_a_disabled_base_schedule_stops_the_full_schedules_too(self) -> None:
        self._register_job(active_full_schedule=self._due_daily(), synthetic_full_schedule=self._due_daily())
        JOBS["job-1"]["schedule"]["enabled"] = False

        self.assertEqual(self._types(self._dispatch()), [])

    def test_a_base_schedule_that_is_not_due_still_lets_the_active_full_fire(self) -> None:
        self._register_job(
            schedule={
                "schedule_type": "daily",
                "enabled": True,
                "timezone": "UTC",
                "start_time": "05:00",
                "weekdays": [],
                "day_of_month": None,
            },
            active_full_schedule=self._due_daily(),
        )
        JOBS["job-1"]["next_run_at"] = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()

        self.assertEqual(self._types(self._dispatch()), ["ActiveFull"])

    def test_an_active_full_schedule_wins_when_both_are_due(self) -> None:
        self._register_job(
            active_full_schedule=self._due_daily(), synthetic_full_schedule=self._due_daily()
        )

        self.assertEqual(self._types(self._dispatch()), ["ActiveFull"])

    def test_a_schedule_that_lost_the_tie_does_not_fire_straight_after(self) -> None:
        self._register_job(active_full_schedule=self._due_daily())

        first = self._types(self._dispatch())
        TASKS.clear()
        second = self._types(self._dispatch())

        self.assertEqual(first, ["ActiveFull"])
        self.assertEqual(second, [], "the base schedule was due too and must have been advanced")

    def test_one_dispatch_enqueues_the_synthetic_full_once(self) -> None:
        self._register_job(synthetic_full_schedule=self._due_daily())

        for _ in range(3):
            self.scheduler._dispatch_due_jobs()

        synthetic = [
            task for task in TASKS.values()
            if task["parameters"]["resolved_backup_type"] == "SyntheticFull"
        ]
        self.assertEqual(len(synthetic), 1)

    def test_a_repository_that_cannot_merge_falls_back_to_a_single_active_full(self) -> None:
        REPOSITORIES["repo-nfs"] = {"id": "repo-nfs", "name": "NFS Repo", "type": "nfs", "path": ""}
        self._register_job(synthetic_full_schedule=self._due_daily())
        JOBS["job-1"]["repo_id"] = "repo-nfs"
        JOBS["job-1"]["repo_name"] = "NFS Repo"
        BACKUPS["point-1"]["chain_id"] = "backup::job-1::vm-1::repo-nfs"
        BACKUPS["point-1"]["repository_id"] = "repo-nfs"

        tasks = self._dispatch()

        self.assertEqual(self._types(tasks), ["ActiveFull"])
        self.assertEqual(
            tasks[0]["parameters"]["resolved_backup_reason"],
            "RepositorySyntheticUnsupportedEscalatedToActive",
        )

    def test_each_vm_in_a_job_gets_its_own_pair_of_phases(self) -> None:
        RESOURCES_BY_HV["hv-1"][("virtual_machine", "vm-2")] = {
            "resource_type": "virtual_machine",
            "external_id": "vm-2",
            "name": "demo-vm-2",
            "payload": {},
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        BACKUPS["point-2"] = {
            **BACKUPS["point-1"],
            "id": "point-2",
            "chain_id": "backup::job-1::vm-2::repo-1",
            "vm_id": "vm-2",
        }
        self._register_job(synthetic_full_schedule=self._due_daily())
        JOBS["job-1"]["vm_ids"] = ["vm-1", "vm-2"]
        JOBS["job-1"]["vm_names"] = ["demo-vm", "demo-vm-2"]

        tasks = self._dispatch()

        self.assertEqual(len(tasks), 4)
        for vm_id in ("vm-1", "vm-2"):
            own = [task for task in tasks if task["resource_external_id"] == vm_id]
            self.assertEqual(self._types(own), ["Incremental", "SyntheticFull"])
            self.assertEqual(own[1]["parameters"]["requires_task_id"], own[0]["id"])


class LegacyFullDayMigrationTests(unittest.TestCase):
    @staticmethod
    def _job(schedule: dict, **overrides) -> dict:
        return normalize_job_record(
            {
                "id": "job-1",
                "name": "nightly",
                "job_type": "backup",
                "created_at": "2026-09-15T14:37:00+00:00",
                "schedule": schedule,
                **overrides,
            }
        )

    def test_day_lists_become_weekly_schedules_at_the_base_time(self) -> None:
        job = self._job(
            {"type": "daily", "time": "02:00", "timezone": "UTC"},
            active_full_days=["sunday"],
            synthetic_full_days=["wednesday"],
        )

        self.assertEqual(job["active_full_schedule"]["weekdays"], ["Sun"])
        self.assertEqual(job["active_full_schedule"]["time"], "02:00")
        self.assertEqual(job["synthetic_full_schedule"]["weekdays"], ["Wed"])
        self.assertEqual(job["synthetic_full_schedule"]["time"], "02:00")

    def test_a_periodic_base_falls_back_to_the_time_the_job_was_created(self) -> None:
        job = self._job(
            {"type": "periodic", "every": "10m", "timezone": "UTC"},
            synthetic_full_days=["sunday"],
        )

        self.assertEqual(job["synthetic_full_schedule"]["time"], "14:37")

    def test_the_migrated_time_follows_the_schedule_timezone(self) -> None:
        job = self._job(
            {"type": "periodic", "every": "10m", "timezone": "Europe/Kyiv"},
            synthetic_full_days=["sunday"],
        )

        self.assertEqual(job["synthetic_full_schedule"]["time"], "17:37")

    def test_an_explicit_schedule_is_never_overwritten_by_the_old_day_list(self) -> None:
        job = self._job(
            {"type": "daily", "time": "02:00", "timezone": "UTC"},
            synthetic_full_days=["sunday"],
            synthetic_full_schedule={"type": "weekly", "days": ["friday"], "time": "23:00", "timezone": "UTC"},
        )

        self.assertEqual(job["synthetic_full_schedule"]["weekdays"], ["Fri"])
        self.assertEqual(job["synthetic_full_schedule"]["time"], "23:00")

    def test_a_job_with_no_schedule_of_its_own_derives_a_fixed_time_and_zone(self) -> None:
        job = self._job(None, synthetic_full_days=["wednesday"])

        self.assertEqual(job["synthetic_full_schedule"]["weekdays"], ["Wed"])
        self.assertEqual(job["synthetic_full_schedule"]["time"], "00:00")
        self.assertEqual(job["synthetic_full_schedule"]["timezone"], "UTC")

    def test_the_derived_time_does_not_depend_on_the_server_timezone(self) -> None:
        with patch.object(abr_v41, "_server_timezone_name", return_value="Pacific/Auckland"):
            job = self._job(None, synthetic_full_days=["wednesday"])

        self.assertEqual(job["synthetic_full_schedule"]["timezone"], "UTC")
        self.assertEqual(job["synthetic_full_schedule"]["time"], "00:00")

    def test_a_day_list_reports_when_it_fires_next(self) -> None:
        job = self._job(
            {"type": "daily", "time": "02:00", "timezone": "UTC", "enabled": True},
            synthetic_full_days=["wednesday"],
        )

        self.assertTrue(job["synthetic_full_next_run_at"])
        self.assertTrue(job["synthetic_full_next_run_at"].endswith("02:00:00+00:00"))

    def test_a_stored_next_run_is_not_recomputed(self) -> None:
        job = self._job(
            {"type": "daily", "time": "02:00", "timezone": "UTC", "enabled": True},
            synthetic_full_days=["wednesday"],
            synthetic_full_next_run_at="2026-12-31T00:00:00+00:00",
        )

        self.assertEqual(job["synthetic_full_next_run_at"], "2026-12-31T00:00:00+00:00")

    def test_a_switched_off_job_promises_no_next_run(self) -> None:
        job = self._job(
            {"type": "daily", "time": "02:00", "timezone": "UTC", "enabled": False},
            synthetic_full_days=["wednesday"],
            synthetic_full_next_run_at="2026-12-31T00:00:00+00:00",
        )

        self.assertTrue(job["synthetic_full_schedule"]["enabled"])
        self.assertIsNone(job["synthetic_full_next_run_at"])

    def test_a_job_without_a_base_schedule_still_promises_its_full_run(self) -> None:
        job = self._job(
            None,
            synthetic_full_days=["wednesday"],
            synthetic_full_next_run_at="2026-12-31T00:00:00+00:00",
        )

        self.assertTrue(job["synthetic_full_schedule"]["enabled"])
        self.assertEqual(job["synthetic_full_next_run_at"], "2026-12-31T00:00:00+00:00")

    def test_a_job_with_no_day_lists_has_no_extra_schedules(self) -> None:
        job = self._job({"type": "daily", "time": "02:00", "timezone": "UTC"})

        self.assertIsNone(job["active_full_schedule"])
        self.assertIsNone(job["synthetic_full_schedule"])


if __name__ == "__main__":
    unittest.main()
