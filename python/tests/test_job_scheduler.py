# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pathlib
import sys
import unittest

try:
    from python.srv.core.job_scheduler import JobScheduler
    from python.srv.core.mongo_db_wrapper import HYPERVISORS, JOBS, RESOURCES_BY_HV, TASKS
except ModuleNotFoundError:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    venv_lib = repo_root / ".venv" / "lib"
    for site_packages in sorted(venv_lib.glob("python*/site-packages")):
        sys.path.insert(0, str(site_packages))
    from python.srv.core.job_scheduler import JobScheduler
    from python.srv.core.mongo_db_wrapper import HYPERVISORS, JOBS, RESOURCES_BY_HV, TASKS


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
        self.assertEqual(task["parameters"], {"duration_seconds": 5})

        job = JOBS["job-1"]
        self.assertEqual(job["last_task_id"], task["id"])
        self.assertEqual(job["last_session_id"], task["id"])
        self.assertEqual(job["status"], "active")
        self.assertEqual(job["last_run_at"], task["created_at"])
        self.assertIsInstance(job["next_run_at"], str)
        self.assertIsInstance(job["last_scheduled_at"], str)

    def test_due_interval_backup_job_preserves_incremental_task_parameter(self) -> None:
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
                "task_parameters": {"incremental": True},
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
        self.assertEqual(task["parameters"], {"incremental": True})

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
        self.assertEqual(
            task["parameters"],
            {"compression_level": 6, "encryption_enabled": True, "encryption_key": "demo-passphrase-1"},
        )

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
        self.assertGreater(datetime.fromisoformat(JOBS["job-1"]["next_run_at"]), datetime.now(timezone.utc))

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


if __name__ == "__main__":
    unittest.main()
