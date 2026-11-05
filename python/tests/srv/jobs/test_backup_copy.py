# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from python.srv.core.job_scheduler import JobScheduler
from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    RESOURCES_BY_HV,
    TASKS,
)
from python.srv.services import abr_v41, backup_copy
from python.srv.services.api_errors import ApiError
from python.srv.services.backup_catalog import register_backup_from_completed_task
from python.srv.services.backup_copy import BackupCopyError, BackupCopyValidationError

FULL = {"type": "weekly", "days": ["sunday"], "time": "02:00"}
VM = "vm-1"


class _Catalog(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS)])
        HYPERVISORS["vc"] = {"id": "vc", "name": "vCenter", "kind": "vcenter", "type": "vsphere"}
        HYPERVISORS["hv"] = {"id": "hv", "name": "Hyper-V", "kind": "hyperv", "type": "hyper-v"}
        REPOSITORIES["src"] = {"id": "src", "name": "Source", "type": "local", "path": tempfile.mkdtemp()}
        REPOSITORIES["dst"] = {"id": "dst", "name": "Offsite", "type": "local", "path": tempfile.mkdtemp()}
        REPOSITORIES["smb"] = {
            "id": "smb",
            "name": "Share",
            "type": "smb",
            "path": tempfile.mkdtemp(),
            "metadata": {"server": "nas.example", "share": "copies"},
        }
        JOBS["nightly"] = abr_v41.normalize_job_record(
            {
                "id": "nightly",
                "name": "Nightly",
                "job_type": "backup",
                "inventory_id": "vc",
                "inventory_name": "vCenter",
                "vm_ids": [VM],
                "vm_names": ["sql-01"],
                "repo_id": "src",
                "repo_name": "Source",
                "task_parameters": {},
            }
        )
        self.clock = 0

    def _point(self, point_id: str, kind: str, *, job_id: str = "nightly", repo: str = "src", **extra) -> dict:
        self.clock += 1
        full = kind != "Incremental"
        point = {
            "id": point_id,
            "status": "available",
            "job_id": job_id,
            "job_name": (JOBS.get(job_id) or {}).get("name"),
            "vm_id": VM,
            "vm_name": "sql-01",
            "inventory_id": "vc",
            "repository_id": repo,
            "repository_name": REPOSITORIES[repo]["name"],
            "execution_type": kind,
            "full_backup": full,
            "created_at": f"2026-11-0{1 + self.clock // 10}T0{self.clock % 10}:00:00+00:00",
            "chain_id": f"backup::{job_id}::{VM}::{repo}",
            "artifact": {"disks": [{"index": 0, "abr_backup_name": "disk-0.vmdk", "abr_path": f"/r/{point_id}"}]},
        }
        point.update(extra)
        BACKUPS[point_id] = abr_v41.normalize_backup_point(point)
        return BACKUPS[point_id]

    def _copy_job(self, **payload) -> dict:
        body = {
            "name": "Offsite copy",
            "source_jobs": ["Nightly"],
            "repo": "Offsite",
            "active_full_schedule": FULL,
            "copy_mode": "periodic",
        }
        body.update(payload)
        return abr_v41.create_backup_copy_job(body)

    def _copied(self, point_id: str, source_id: str, kind: str = "ActiveFull") -> dict:
        job = JOBS[self.copy["id"]]
        return self._point(
            point_id,
            kind,
            job_id=job["id"],
            repo="dst",
            copy_source={"job_id": "nightly", "repository_id": "src", "point_id": source_id},
        )

    def _refused(self, call) -> ApiError:
        with self.assertRaises(ApiError) as caught:
            call()
        return caught.exception


class CopyPlanTests(_Catalog):
    def setUp(self) -> None:
        super().setUp()
        self._point("f1", "ActiveFull")
        self._point("i1", "Incremental")
        self._point("i2", "Incremental")
        self.copy = self._copy_job()

    def _plan(self, **kwargs):
        return backup_copy.plan_copy(abr_v41.normalize_job_record(JOBS[self.copy["id"]]), VM, **kwargs)

    def _ids(self, plan) -> list[str]:
        return [point["id"] for point in plan["points"]]

    def test_the_first_run_copies_the_newest_state_as_a_full(self) -> None:
        plan = self._plan(requested_type="ActiveFull")

        self.assertEqual((plan["kind"], self._ids(plan), plan["target"]["id"]), ("full", ["f1", "i1", "i2"], "i2"))

    def test_the_first_immediate_run_also_starts_from_the_newest_state(self) -> None:
        JOBS[self.copy["id"]]["copy_mode"] = "immediate"

        plan = self._plan(requested_type="ActiveFull")

        self.assertEqual((plan["kind"], plan["target"]["id"]), ("full", "i2"))

    def test_periodic_merges_everything_new_into_one_incremental(self) -> None:
        self._copied("c1", "f1")

        plan = self._plan()

        self.assertEqual((plan["kind"], self._ids(plan), plan["parent"]["id"]), ("incremental", ["i1", "i2"], "c1"))

    def test_immediate_copies_the_next_point_alone_and_says_what_is_left(self) -> None:
        JOBS[self.copy["id"]]["copy_mode"] = "immediate"
        self._copied("c1", "f1")

        plan = self._plan()

        self.assertEqual((plan["kind"], self._ids(plan), plan["remaining"]), ("incremental", ["i1"], 1))

    def test_nothing_new_plans_nothing(self) -> None:
        self._copied("c1", "i2")

        self.assertIsNone(self._plan())

    def test_an_active_full_with_nothing_new_copies_the_newest_state_again(self) -> None:
        self._copied("c1", "i2")

        plan = self._plan(requested_type="ActiveFull")

        self.assertEqual((plan["kind"], self._ids(plan)), ("full", ["f1", "i1", "i2"]))

    def test_a_new_chain_in_the_source_starts_a_new_full_in_the_destination(self) -> None:
        self._copied("c1", "i2")
        self._point("f2", "ActiveFull")
        self._point("i3", "Incremental")

        plan = self._plan()

        self.assertEqual((plan["kind"], self._ids(plan)), ("full", ["f2", "i3"]))

    def test_immediate_copies_a_new_source_full_on_its_own_first(self) -> None:
        JOBS[self.copy["id"]]["copy_mode"] = "immediate"
        self._copied("c1", "i2")
        self._point("f2", "ActiveFull")
        self._point("i3", "Incremental")

        plan = self._plan()

        self.assertEqual((plan["kind"], self._ids(plan), plan["remaining"]), ("full", ["f2"], 1))

    def test_a_copied_point_the_source_no_longer_holds_makes_the_next_copy_a_full(self) -> None:
        self._copied("c1", "i1")
        del BACKUPS["i1"]

        plan = self._plan()

        self.assertEqual((plan["kind"], self._ids(plan)), ("full", ["f1", "i2"]))

    def test_the_copy_job_own_points_are_never_a_source(self) -> None:
        self._copied("c1", "i2")

        sources = [point["id"] for point in backup_copy.source_points(JOBS[self.copy["id"]])]

        self.assertEqual(sources, ["f1", "i1", "i2"])
        self.assertIsNone(self._plan())

    def test_every_vm_of_the_sources_is_copied_unless_the_job_picks_some(self) -> None:
        BACKUPS["x1"] = dict(BACKUPS["i2"], id="x1", vm_id="vm-9", vm_name="web-09")
        job = JOBS[self.copy["id"]]

        self.assertEqual([target["vm_id"] for target in backup_copy.copy_targets(job)], [VM, "vm-9"])
        job["vm_ids"] = [VM]
        self.assertEqual([target["vm_id"] for target in backup_copy.copy_targets(job)], [VM])


class CopiedDiskTests(unittest.TestCase):
    def test_a_copied_disk_tells_restore_whether_it_starts_a_chain_or_continues_one(self) -> None:
        merge = {
            "template": {"abr_backup_name": "disk-0.vmdk", "index": 0},
            "newest": {"change_id": "52 2"},
            "first": {"previous_change_id": "52 1"},
            "source_names": ["a/disk-0.vmdk", "b/disk-0.vmdk"],
        }
        result = {"raw_data_size": 100, "stored_data_size": 40}

        full = backup_copy._disk_entry(dict(merge, kind="full"), result)
        incremental = backup_copy._disk_entry(dict(merge, kind="incremental"), result)

        self.assertEqual((full["store_mode"], incremental["store_mode"]), ("full", "incremental"))
        self.assertTrue(full["copied"])
        self.assertEqual((incremental["change_id"], incremental["previous_change_id"]), ("52 2", "52 1"))


class SourceKeyTests(unittest.TestCase):
    def _points(self, *keys) -> list[dict]:
        return [
            {"id": f"p{index}", "encryption_enabled": bool(key), "encryption_key": key}
            for index, key in enumerate(keys)
        ]

    def test_a_chain_under_one_key_is_read_with_it(self) -> None:
        with patch("python.srv.services.abr_v41._resolve_restore_encryption_key", side_effect=lambda p: p["encryption_key"]):
            self.assertEqual(backup_copy._source_encryption_key(self._points("key-b", "key-b")), "key-b")
            self.assertEqual(backup_copy._source_encryption_key(self._points("", "")), "")

    def test_a_chain_mixing_keys_or_plain_and_encrypted_points_is_refused(self) -> None:
        with patch("python.srv.services.abr_v41._resolve_restore_encryption_key", side_effect=lambda p: p["encryption_key"]):
            for keys in (("key-a", "key-b"), ("", "key-b")):
                with self.subTest(keys=keys), self.assertRaises(BackupCopyValidationError):
                    backup_copy._source_encryption_key(self._points(*keys))


class CopyJobSettingsTests(_Catalog):
    def test_a_copy_job_is_made_with_its_own_settings(self) -> None:
        job = self._copy_job(retry_count=2, retry_interval_minutes=15, retention_days=90)

        self.assertEqual(job["job_type"], "backup-copy")
        self.assertEqual(job["repository"]["name"], "Offsite")
        self.assertEqual(job["retention_days"], 90)
        self.assertEqual(job["backup_copy"]["copy_mode"], "periodic")
        self.assertEqual(job["backup_copy"]["source_jobs"], [{"id": "nightly", "name": "Nightly"}])
        self.assertEqual(job["backup_copy"]["retry"], {"count": 2, "interval_minutes": 15})

    def test_retry_defaults_follow_the_copy_mode(self) -> None:
        self.assertEqual(self._copy_job()["backup_copy"]["retry"], {"count": 5, "interval_minutes": 0})
        immediate = self._copy_job(name="Immediate copy", copy_mode="immediate")

        self.assertEqual(immediate["backup_copy"]["retry"], {"count": 3, "interval_minutes": 10})

    def test_the_destination_cannot_be_a_source(self) -> None:
        for payload in ({"repo": "Source"}, {"source_jobs": None, "source_repositories": ["Offsite"]}):
            with self.subTest(payload=payload):
                error = self._refused(lambda payload=payload: self._copy_job(**payload))

                self.assertEqual((error.status_code, error.code), (422, "backup_copy_same_repository"))
        self.assertEqual([job for job in JOBS.values() if job["job_type"] == "backup-copy"], [])

    def test_a_copy_job_needs_a_source_and_a_known_mode(self) -> None:
        self.assertIn("at least one source", self._refused(lambda: self._copy_job(source_jobs=[])).message)
        self.assertIn("periodic or immediate", self._refused(lambda: self._copy_job(copy_mode="hourly")).message)

    def test_an_immediate_copy_takes_no_schedule_and_no_run_after(self) -> None:
        daily = {"type": "daily", "time": "03:00"}

        self.assertIn("takes no schedule", self._refused(lambda: self._copy_job(copy_mode="immediate", schedule=daily)).message)
        self.assertIn(
            "run-after-job is for periodic",
            self._refused(lambda: self._copy_job(copy_mode="immediate", run_after_job="Nightly")).message,
        )

    def test_a_periodic_copy_can_run_after_another_job(self) -> None:
        job = self._copy_job(run_after_job="Nightly")

        self.assertEqual(job["backup_copy"]["run_after_job"], {"id": "nightly", "name": "Nightly"})

    def test_encrypted_sources_need_a_key_of_the_copy_job(self) -> None:
        JOBS["nightly"]["task_parameters"] = {"encryption_enabled": True, "encryption_key": "placeholder-key-123456"}

        self.assertIn("encryption key", self._refused(self._copy_job).message)
        keyed = self._copy_job(
            task_parameters={"encryption_enabled": True, "encryption_key": "placeholder-copy-key-1"}
        )
        self.assertTrue(keyed["encryption"]["enabled"])

    def test_encrypted_points_of_vms_the_job_does_not_copy_need_no_key(self) -> None:
        self._point("f1", "ActiveFull")
        self._point("x1", "ActiveFull", vm_id="vm-9", vm_name="web-09", encryption_enabled=True)

        job = self._copy_job(source_jobs=None, source_repositories=["Source"], vms=["sql-01"])
        self.assertEqual(job["vm_ids"], [VM])
        self.assertIn("encryption key", self._refused(lambda: self._copy_job(name="All", source_jobs=None, source_repositories=["Source"])).message)

    def test_hyperv_sources_need_a_destination_their_hosts_reach(self) -> None:
        JOBS["nightly"]["inventory_id"] = "hv"

        self.assertEqual(self._refused(self._copy_job).code, "hyperv_repository_unreachable")
        self.assertEqual(self._copy_job(repo="Share")["repository"]["name"], "Share")

    def test_only_vms_the_sources_back_up_can_be_picked(self) -> None:
        self.assertEqual(self._copy_job(vms=["sql-01"])["vm_ids"], [VM])
        self.assertIn("not backed up", self._refused(lambda: self._copy_job(name="Other", vms=["web-09"])).message)

    def test_an_edit_keeps_the_same_rules(self) -> None:
        job = self._copy_job()

        error = self._refused(lambda: abr_v41.edit_job_record(job["id"], {"repo": "Source"}))
        self.assertEqual(error.code, "backup_copy_same_repository")
        edited = abr_v41.edit_job_record(job["id"], {"copy_mode": "immediate", "retry_count": 1})
        self.assertEqual(edited["backup_copy"]["copy_mode"], "immediate")
        self.assertEqual(edited["backup_copy"]["retry"]["count"], 1)

    def test_cloning_a_copy_job_keeps_its_copy_settings(self) -> None:
        job = self._copy_job(run_after_job="Nightly", retry_count=4)

        clone = abr_v41.create_copy_job({"name": "Offsite copy 2", "source_id": job["id"]})

        self.assertEqual(clone["job_type"], "backup-copy")
        self.assertEqual(clone["backup_copy"]["source_jobs"], job["backup_copy"]["source_jobs"])
        self.assertEqual(clone["backup_copy"]["retry"]["count"], 4)


class CopyRetentionTests(_Catalog):
    def test_copy_retention_drops_old_copies_and_never_a_source_point(self) -> None:
        old = "2026-01-0{}T01:00:00+00:00"
        for index, (point_id, kind) in enumerate((("f1", "ActiveFull"), ("i1", "Incremental")), start=1):
            self._point(point_id, kind, created_at=old.format(index))
        self.copy = self._copy_job(retention_days=7)
        self._copied("c1", "i1")
        BACKUPS["c1"]["created_at"] = old.format(3)
        self._copied("c2", "i1")

        with patch.object(abr_v41.clock_guard, "retention_block", return_value=None):
            report = abr_v41.run_job_retention(self.copy["id"], dry_run=True)

        removed = [item["restore_point_id"] if isinstance(item, dict) else item for chain in report["chains"] for item in chain["removed"]]
        self.assertEqual(removed, ["c1"])
        self.assertEqual({chain["repository_id"] for chain in report["chains"]}, {"dst"})


class CopyTriggerTests(_Catalog):
    def setUp(self) -> None:
        super().setUp()
        self._point("f1", "ActiveFull")

    def _finished_backup(self, **extra) -> dict:
        task = {
            "id": "t-backup",
            "job_id": "nightly",
            "task_type": "backup",
            "status": "completed",
            "resource_external_id": VM,
            "run_id": "run-1",
            "result": {"restore_point_id": "f1", "repository_id": "src"},
        }
        task.update(extra)
        TASKS[task["id"]] = task
        return task

    def _copy_sessions(self, job_id: str) -> list[dict]:
        return [task for task in TASKS.values() if task.get("job_id") == job_id]

    def test_an_immediate_copy_starts_when_a_source_backup_adds_a_point(self) -> None:
        job = self._copy_job(copy_mode="immediate")

        backup_copy.on_session_finished(self._finished_backup())

        sessions = self._copy_sessions(job["id"])
        self.assertEqual(len(sessions), 1)
        self.assertEqual((sessions[0]["trigger"], sessions[0]["resource_external_id"]), ("immediate", VM))
        self.assertTrue(sessions[0]["parameters"]["copy_job"])

    def test_a_periodic_copy_does_not_start_on_a_source_backup(self) -> None:
        job = self._copy_job()

        backup_copy.on_session_finished(self._finished_backup())

        self.assertEqual(self._copy_sessions(job["id"]), [])

    def test_a_periodic_copy_runs_after_its_job_once_the_whole_run_is_over(self) -> None:
        job = self._copy_job(run_after_job="Nightly")
        TASKS["t-other"] = {"id": "t-other", "job_id": "nightly", "task_type": "backup", "status": "running", "run_id": "run-1"}

        backup_copy.on_session_finished(self._finished_backup())
        self.assertEqual(self._copy_sessions(job["id"]), [])

        TASKS["t-other"]["status"] = "failed"
        backup_copy.on_session_finished(self._finished_backup())
        self.assertEqual([task["trigger"] for task in self._copy_sessions(job["id"])], ["after-job"])

    def test_a_failed_backup_starts_no_immediate_copy(self) -> None:
        job = self._copy_job(copy_mode="immediate")

        backup_copy.on_session_finished(self._finished_backup(status="failed", result={}))

        self.assertEqual(self._copy_sessions(job["id"]), [])

    def test_a_disabled_immediate_copy_does_not_start_and_starts_again_once_enabled(self) -> None:
        job = self._copy_job(copy_mode="immediate")

        abr_v41.edit_job_record(job["id"], {"enabled": False})
        backup_copy.on_session_finished(self._finished_backup())
        self.assertEqual(self._copy_sessions(job["id"]), [])

        abr_v41.edit_job_record(job["id"], {"enabled": True})
        backup_copy.on_session_finished(self._finished_backup())
        self.assertEqual(len(self._copy_sessions(job["id"])), 1)

    def test_a_switched_off_source_job_does_not_stop_its_points_being_copied(self) -> None:
        job = self._copy_job()
        abr_v41.edit_job_record("nightly", {"enabled": False})

        started = abr_v41.start_job_record(job["id"])

        self.assertEqual([task["resource_external_id"] for task in started["items"]], [VM])
        self.assertEqual(backup_copy.plan_copy(abr_v41.normalize_job_record(JOBS[job["id"]]), VM)["target"]["id"], "f1")

    def test_a_vm_with_a_copy_already_queued_gets_no_second_one(self) -> None:
        job = self._copy_job(copy_mode="immediate")

        backup_copy.on_session_finished(self._finished_backup())
        backup_copy.on_session_finished(self._finished_backup(id="t-backup-2"))

        self.assertEqual(len(self._copy_sessions(job["id"])), 1)

    def test_a_manual_start_queues_a_copy_per_vm(self) -> None:
        job = self._copy_job()

        started = abr_v41.start_job_record(job["id"])

        self.assertEqual([task["resource_external_id"] for task in started["items"]], [VM])
        self.assertEqual(started["items"][0]["parameters"]["resolved_backup_type"], "ActiveFull")


class CopyScheduleTests(_Catalog):
    def setUp(self) -> None:
        super().setUp()
        self._point("f1", "ActiveFull")
        self.scheduler = JobScheduler(poll_interval_seconds=0.1)

    def _due(self, job: dict) -> None:
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        JOBS[job["id"]].update({"created_at": past, "next_incremental_run_at": past})

    def test_a_periodic_copy_runs_when_its_schedule_comes_round(self) -> None:
        job = self._copy_job(schedule={"type": "daily", "time": "03:00"})
        self._due(job)

        self.scheduler._dispatch_due_jobs()

        sessions = [task for task in TASKS.values() if task.get("job_id") == job["id"]]
        self.assertEqual([(task["trigger"], task["resource_external_id"]) for task in sessions], [("schedule", VM)])
        self.assertTrue(sessions[0]["parameters"]["copy_job"])
        self.assertGreater(datetime.fromisoformat(JOBS[job["id"]]["next_incremental_run_at"]), datetime.now(timezone.utc))

    def test_a_scheduled_run_with_nothing_new_queues_nothing_and_moves_on(self) -> None:
        job = self._copy_job(schedule={"type": "daily", "time": "03:00"})
        self.copy = job
        self._copied("c1", "f1")
        self._due(job)

        self.scheduler._dispatch_due_jobs()

        self.assertEqual([task for task in TASKS.values() if task.get("job_id") == job["id"]], [])
        self.assertGreater(datetime.fromisoformat(JOBS[job["id"]]["next_incremental_run_at"]), datetime.now(timezone.utc))

    def test_a_disabled_copy_job_is_not_run_by_the_schedule(self) -> None:
        job = self._copy_job(schedule={"type": "daily", "time": "03:00", "enabled": False})
        self._due(job)

        self.scheduler._dispatch_due_jobs()

        self.assertEqual([task for task in TASKS.values() if task.get("job_id") == job["id"]], [])


class CopyToggleTests(_Catalog):
    def test_a_periodic_copy_switched_off_skips_its_slot_and_resumes_its_chain_once_back_on(self) -> None:
        self._point("f1", "ActiveFull")
        self.copy = self._copy_job(schedule={"type": "daily", "time": "03:00"})
        self._copied("c1", "f1")
        self._point("i1", "Incremental")
        scheduler = JobScheduler(poll_interval_seconds=0.1)
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()

        abr_v41.edit_job_record(self.copy["id"], {"enabled": False})
        JOBS[self.copy["id"]].update({"created_at": past, "next_incremental_run_at": past})
        scheduler._dispatch_due_jobs()
        self.assertEqual([task for task in TASKS.values() if task.get("job_id") == self.copy["id"]], [])

        abr_v41.edit_job_record(self.copy["id"], {"enabled": True})
        JOBS[self.copy["id"]]["next_incremental_run_at"] = past
        scheduler._dispatch_due_jobs()
        sessions = [task for task in TASKS.values() if task.get("job_id") == self.copy["id"]]
        self.assertEqual([task["trigger"] for task in sessions], ["schedule"])
        plan = backup_copy.plan_copy(abr_v41.normalize_job_record(JOBS[self.copy["id"]]), VM)
        self.assertEqual((plan["kind"], [point["id"] for point in plan["points"]], plan["parent"]["id"]), ("incremental", ["i1"], "c1"))


class CopyRetryTests(_Catalog):
    def setUp(self) -> None:
        super().setUp()
        self._point("f1", "ActiveFull")
        self.copy = self._copy_job(retry_count=2, retry_interval_minutes=0)
        self.task = backup_copy.create_copy_sessions(JOBS[self.copy["id"]], trigger="manual", include_idle=True)[0]
        self.messages: list[dict] = []

    def _run(self, effects):
        with patch.object(backup_copy, "_copy_once", side_effect=effects) as copy_once:
            try:
                return backup_copy.run_backup_copy(self.task, progress_callback=self.messages.append), copy_once
            finally:
                self.calls = copy_once.call_count

    def _done(self) -> dict:
        return {"logical_bytes": 10, "transferred_bytes": 4}

    def test_a_transient_failure_is_retried_and_reported(self) -> None:
        result, _ = self._run([BackupCopyError("network glitch"), self._done()])

        self.assertEqual((self.calls, result["copy_attempts"]), (2, 2))
        self.assertTrue(any("Attempt 1/3" in str(item.get("message")) and item.get("log_level") == "warn" for item in self.messages))

    def test_retries_stop_at_the_configured_count(self) -> None:
        with self.assertRaises(BackupCopyError):
            self._run([BackupCopyError("down")] * 5)

        self.assertEqual(self.calls, 3)

    def test_a_configuration_error_is_not_retried(self) -> None:
        with self.assertRaises(BackupCopyValidationError):
            self._run([BackupCopyValidationError("repository gone"), self._done()])

        self.assertEqual(self.calls, 1)

    def test_a_share_that_is_not_mounted_is_not_retried_and_named(self) -> None:
        with patch.object(backup_copy, "unmounted_share_message", return_value="The share is not mounted"), patch.object(
            backup_copy, "_copy_once", wraps=backup_copy._copy_once
        ) as copy_once, self.assertRaises(BackupCopyValidationError) as caught:
            backup_copy.run_backup_copy(self.task, progress_callback=self.messages.append)

        self.assertEqual(str(caught.exception), "The share is not mounted")
        self.assertEqual(copy_once.call_count, 1)

    def test_a_run_with_nothing_new_records_no_restore_point(self) -> None:
        with patch.object(backup_copy, "plan_copy", return_value=None):
            result = backup_copy.run_backup_copy(self.task)
        finished = dict(self.task, status="completed", result=result)

        self.assertTrue(result["nothing_copied"])
        self.assertIsNone(register_backup_from_completed_task(finished))


if __name__ == "__main__":
    unittest.main()
