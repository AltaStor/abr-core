# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from python.srv.core.job_scheduler import JobScheduler
from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, REPOSITORIES, TASKS
from python.srv.core.storage_tool import (
    DiskToolCorruptBackup,
    StorageToolError,
    verify_disk_tool_backup,
)
from python.srv.core.subprocess_util import CompletedProcess
from python.srv.services import abr_v41, backup_copy, notification_detectors
from python.srv.services.api_errors import ApiError

CORRUPT_RESULT = {
    "command": "verify",
    "status": "error",
    "intact": False,
    "data_ranges": 16,
    "corrupt_ranges": 1,
    "error": "backup is corrupt: 1 of 16 data ranges failed verification",
}


def _point(point_id: str, *, parent: str | None, day: int, job_id: str = "job-1", status: str = "available") -> dict:
    return abr_v41.normalize_backup_point(
        {
            "id": point_id,
            "job_id": job_id,
            "job_name": "Nightly",
            "vm_id": "vm-1",
            "vm_name": "VM-1",
            "repository_id": "repo-1",
            "repository_name": "Repo",
            "repository_type": "local",
            "execution_type": "Incremental" if parent else "ActiveFull",
            "full_backup": parent is None,
            "parent_point_id": parent,
            "status": status,
            "created_at": f"2026-06-{day:02d}T12:00:00+00:00",
            "artifact": {"disks": [{"disk_key": "disk-1", "abr_path": f"vm-1/{point_id}", "abr_backup_name": "01-disk.vmdk"}]},
        }
    )


class _Catalog(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES, TASKS):
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in (BACKUPS, JOBS, REPOSITORIES, TASKS)])
        self.root = tempfile.mkdtemp(prefix="abr-verify-marks-")
        REPOSITORIES["repo-1"] = {"id": "repo-1", "name": "Repo", "type": "local", "path": self.root}
        JOBS["job-1"] = {"id": "job-1", "name": "Nightly", "job_type": "backup", "repo_id": "repo-1", "vm_ids": ["vm-1"]}

    def store(self, *points: dict) -> None:
        for point in points:
            BACKUPS[point["id"]] = point

    def chain(self) -> None:
        self.store(
            _point("full-a", parent=None, day=1),
            _point("inc-a1", parent="full-a", day=2),
            _point("full-b", parent=None, day=3),
            _point("inc-b1", parent="full-b", day=4),
            _point("inc-b2", parent="inc-b1", day=5),
            _point("inc-b3", parent="inc-b2", day=6),
        )

    @staticmethod
    def statuses() -> dict[str, str]:
        return {point_id: str(point.get("status")) for point_id, point in BACKUPS.items()}

    def mark(self, outcomes: dict[str, str]) -> dict:
        return abr_v41.apply_verify_marks(
            {point_id: [{"status": status, "message": f"{point_id} {status}"}] for point_id, status in outcomes.items()},
            session_id="session-1",
            found_by="backup verify",
        )


class ToolVerdictTests(unittest.TestCase):
    def run_verify(self, returncode: int, stdout: str) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sh") as launcher, patch(
            "python.srv.core.storage_tool.default_disk_tool_path", return_value=Path(launcher.name)
        ), patch(
            "python.srv.core.storage_tool.run_capped",
            return_value=CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="Error: data range 0 is corrupt"),
        ) as run:
            try:
                verify_disk_tool_backup({"type": "local", "path": "/repo"}, "vm-1/t", "01-disk.vmdk")
            finally:
                self.argv = run.call_args.args[0]

    def test_damaged_data_is_told_apart_from_a_failed_check(self) -> None:
        with self.assertRaises(DiskToolCorruptBackup) as caught:
            self.run_verify(1, "50%\n100%\n" + json.dumps(CORRUPT_RESULT))
        self.assertEqual((caught.exception.corrupt_ranges, caught.exception.data_ranges), (1, 16))
        self.assertEqual(self.argv[-1], "--json")

    def test_a_backup_that_cannot_be_opened_is_a_plain_failure(self) -> None:
        with self.assertRaises(StorageToolError) as caught:
            self.run_verify(1, json.dumps({"command": "verify", "status": "error", "error": "failed to open backup"}))
        self.assertNotIsInstance(caught.exception, DiskToolCorruptBackup)

    def test_a_failure_without_a_result_document_is_a_plain_failure(self) -> None:
        with self.assertRaises(StorageToolError) as caught:
            self.run_verify(1, "")
        self.assertNotIsInstance(caught.exception, DiskToolCorruptBackup)

    def test_a_damaged_first_block_that_fails_the_key_check_is_still_damage(self) -> None:
        key_refused = json.dumps({"command": "verify", "status": "error", "error": "the key does not decrypt backup"})
        results = iter([
            CompletedProcess(args=[], returncode=1, stdout=key_refused, stderr="CRC32C mismatch for restored chunk 0!"),
            CompletedProcess(args=[], returncode=1, stdout=json.dumps(CORRUPT_RESULT), stderr="Error: data range 0 is corrupt"),
        ])
        with tempfile.NamedTemporaryFile(suffix=".sh") as launcher, patch(
            "python.srv.core.storage_tool.default_disk_tool_path", return_value=Path(launcher.name)
        ), patch("python.srv.core.storage_tool.run_capped", side_effect=lambda *a, **k: next(results)), self.assertRaises(DiskToolCorruptBackup):
            verify_disk_tool_backup({"type": "local", "path": "/repo"}, "vm-1/t", "01-disk.vmdk", encryption_key="placeholder-key-123")

    def test_a_wrong_key_on_intact_data_stays_a_key_failure(self) -> None:
        key_refused = json.dumps({"command": "verify", "status": "error", "error": "the key does not decrypt backup"})
        results = iter([
            CompletedProcess(args=[], returncode=1, stdout=key_refused, stderr="Error: the key does not decrypt backup"),
            CompletedProcess(args=[], returncode=0, stdout=json.dumps({"command": "verify", "status": "ok", "intact": True}), stderr=""),
        ])
        with tempfile.NamedTemporaryFile(suffix=".sh") as launcher, patch(
            "python.srv.core.storage_tool.default_disk_tool_path", return_value=Path(launcher.name)
        ), patch("python.srv.core.storage_tool.run_capped", side_effect=lambda *a, **k: next(results)), self.assertRaises(StorageToolError) as caught:
            verify_disk_tool_backup({"type": "local", "path": "/repo"}, "vm-1/t", "01-disk.vmdk", encryption_key="placeholder-key-123")
        self.assertNotIsInstance(caught.exception, DiskToolCorruptBackup)
        self.assertIn("does not decrypt", str(caught.exception))

    def test_an_intact_backup_passes(self) -> None:
        self.run_verify(0, json.dumps({"command": "verify", "status": "ok", "intact": True}))


class VerifyMarkTests(_Catalog):
    def test_a_damaged_incremental_marks_it_and_what_builds_on_it_only(self) -> None:
        self.chain()

        summary = self.mark({"inc-b2": "corrupt"})

        self.assertEqual(
            self.statuses(),
            {"full-a": "available", "inc-a1": "available", "full-b": "available", "inc-b1": "available", "inc-b2": "corrupt", "inc-b3": "corrupt"},
        )
        self.assertEqual(BACKUPS["inc-b2"]["corruption"]["kind"], "damaged")
        self.assertEqual(BACKUPS["inc-b3"]["corruption"], {**BACKUPS["inc-b3"]["corruption"], "kind": "dependent", "damaged_point_id": "inc-b2"})
        self.assertEqual((summary["damaged"], summary["dependent"], sorted(summary["corrupt_now"])), (["inc-b2"], ["inc-b3"], ["inc-b2", "inc-b3"]))

    def test_a_damaged_full_marks_its_segment_and_leaves_the_older_one(self) -> None:
        self.chain()

        self.mark({"full-b": "corrupt", "inc-b1": "ok", "inc-b2": "ok", "inc-b3": "ok"})

        self.assertEqual([BACKUPS[p]["status"] for p in ("full-a", "inc-a1")], ["available", "available"])
        self.assertEqual([BACKUPS[p]["status"] for p in ("full-b", "inc-b1", "inc-b2", "inc-b3")], ["corrupt"] * 4)

    def test_a_check_that_finds_the_data_intact_again_clears_the_marks(self) -> None:
        self.chain()
        self.mark({"inc-b2": "corrupt"})

        summary = self.mark({"inc-b2": "ok", "inc-b3": "ok"})

        self.assertEqual(set(self.statuses().values()), {"available"})
        self.assertIsNone(BACKUPS["inc-b2"]["corruption"])
        self.assertEqual(sorted(summary["cleared"]), ["inc-b2", "inc-b3"])

    def test_a_check_that_could_not_read_a_damaged_point_keeps_it_and_its_dependents_marked(self) -> None:
        self.chain()
        self.mark({"inc-b2": "corrupt"})

        self.mark({"inc-b2": "failed", "inc-b3": "ok"})

        self.assertEqual((BACKUPS["inc-b2"]["status"], BACKUPS["inc-b3"]["status"]), ("corrupt", "corrupt"))

    def test_a_failed_check_never_marks_a_point(self) -> None:
        self.chain()

        summary = self.mark({"inc-b2": "failed"})

        self.assertEqual(set(self.statuses().values()), {"available"})
        self.assertEqual(summary["corrupt_now"], [])

    def test_points_waiting_for_deletion_or_missing_keep_their_status(self) -> None:
        self.store(_point("full-a", parent=None, day=1), _point("inc-1", parent="full-a", day=2, status="deleting"))
        self.store(_point("inc-2", parent="inc-1", day=3, status="missing"))

        self.mark({"full-a": "corrupt"})

        self.assertEqual(self.statuses(), {"full-a": "corrupt", "inc-1": "deleting", "inc-2": "missing"})

    def test_a_corrupt_point_is_refused_for_restore_with_the_reason(self) -> None:
        self.chain()
        self.mark({"inc-b2": "corrupt"})

        self.assertFalse(abr_v41._restore_point_is_available(BACKUPS["inc-b3"]))
        message = abr_v41.corrupt_point_message(BACKUPS["inc-b3"])
        self.assertIn("builds on restore point inc-b2", message)
        self.assertIn("next run takes an active full", message)
        dto = abr_v41.restore_point_to_dto(BACKUPS["inc-b3"])
        self.assertEqual((dto["available"], dto["corruption"]["kind"]), (False, "dependent"))
        self.assertIsNone(abr_v41.restore_point_to_dto(BACKUPS["inc-b1"])["corruption"])

    def test_the_chain_shows_how_many_points_are_corrupt(self) -> None:
        self.chain()
        self.mark({"inc-b2": "corrupt"})

        chain = abr_v41.backup_chain_to_dto(abr_v41.grouped_backup_points()[BACKUPS["full-a"]["chain_id"]])

        self.assertEqual(chain["status"], "corrupt")
        self.assertEqual((chain["corrupt"]["restore_point_count"], chain["corrupt"]["damaged_point_ids"]), (2, ["inc-b2"]))


class VerifyChainTests(_Catalog):
    def fake_artifact(self, damaged: set[str]):
        def verify(point, disk, **_kwargs):
            if point["id"] in damaged:
                return {"disk_key": "disk-1", "status": "corrupt", "message": "is CORRUPT", "corrupt_ranges": 1, "data_ranges": 4}
            return {"disk_key": "disk-1", "status": "ok", "message": "Verified"}

        return verify

    def test_verify_marks_what_it_found_and_reports_it(self) -> None:
        self.chain()
        with patch.object(abr_v41, "_verify_disk_artifact", side_effect=self.fake_artifact({"inc-b1"})):
            report = abr_v41.verify_backup_chain(BACKUPS["full-a"]["chain_id"])

        self.assertEqual(report["result"], "invalid")
        self.assertEqual([(e["point_id"], e["damaged"]) for e in report["errors"]], [("inc-b1", True)])
        self.assertEqual(sorted(report["corrupt"]["corrupt_now"]), ["inc-b1", "inc-b2", "inc-b3"])

    def test_the_newest_point_scope_reads_only_that_point(self) -> None:
        self.chain()
        seen: list[str] = []

        def verify(point, disk, **_kwargs):
            seen.append(point["id"])
            return {"disk_key": "disk-1", "status": "ok", "message": "Verified"}

        with patch.object(abr_v41, "_verify_disk_artifact", side_effect=verify):
            report = abr_v41.verify_backup_chain(BACKUPS["full-a"]["chain_id"], point_ids={"inc-b3"})

        self.assertEqual((seen, report["checked_restore_points"]), (["inc-b3"], 1))


class DamagedChainRunTests(_Catalog):
    def test_the_next_run_after_damage_is_an_active_full(self) -> None:
        self.chain()
        job = abr_v41.normalize_job_record(JOBS["job-1"])
        self.assertEqual(abr_v41.resolve_job_execution(job, vm_id="vm-1", force_full=False), ("Incremental", "StandardIncremental"))

        self.mark({"inc-b2": "corrupt"})

        self.assertEqual(abr_v41.damaged_chain_points("job-1", "vm-1", "repo-1"), ["inc-b2"])
        self.assertEqual(abr_v41.resolve_job_execution(job, vm_id="vm-1", force_full=False), ("ActiveFull", "DamagedChain"))

    def test_a_forced_synthetic_full_on_a_damaged_chain_is_refused(self) -> None:
        self.chain()
        self.mark({"inc-b3": "corrupt"})
        job = abr_v41.normalize_job_record(JOBS["job-1"])

        with patch.object(abr_v41, "repository_supports_synthetic", return_value=True), self.assertRaises(ApiError) as caught:
            abr_v41.resolve_job_execution(job, vm_id="vm-1", force_full=False, force_synthetic=True)

        self.assertIn("Run an active full instead", caught.exception.message)

    def test_a_damaged_older_segment_does_not_force_a_full(self) -> None:
        self.chain()
        self.mark({"inc-a1": "corrupt"})

        self.assertEqual(abr_v41.damaged_chain_points("job-1", "vm-1", "repo-1"), [])

    def test_a_queued_incremental_becomes_an_active_full_with_a_warning(self) -> None:
        self.chain()
        self.mark({"inc-b3": "corrupt"})
        task = {
            "id": "task-1",
            "task_type": "backup",
            "job_id": "job-1",
            "resource_external_id": "vm-1",
            "parameters": {"resolved_backup_type": "Incremental", "resolved_backup_reason": "StandardIncremental"},
            "logs": [],
        }
        TASKS["task-1"] = task

        refreshed = abr_v41.refresh_queued_backup_decision(task)

        params = refreshed["parameters"]
        self.assertEqual((params["resolved_backup_type"], params["resolved_backup_reason"], params["backup_mode_request"]), ("ActiveFull", "DamagedChain", "full"))
        self.assertEqual([w["code"] for w in refreshed["warnings"]], ["damaged_chain"])

    def test_a_copy_job_whose_own_chain_is_damaged_copies_a_full(self) -> None:
        self.chain()
        self.mark({"inc-b3": "corrupt"})
        job = {"id": "job-1", "repo_id": "repo-1"}
        source = [_point(f"src-{i}", parent=None if i == 1 else f"src-{i - 1}", day=10 + i, job_id="src-job") for i in (1, 2)]
        with patch.object(backup_copy, "_source_chain", return_value=source):
            plan = backup_copy.plan_copy(job, "vm-1")

        self.assertEqual((plan["kind"], plan["damaged"]), ("full", True))


class CorruptRetentionTests(_Catalog):
    def test_retention_deletes_expired_corrupt_points_like_any_other(self) -> None:
        self.chain()
        self.store(_point("full-c", parent=None, day=28))
        for point_id in ("full-c",):
            BACKUPS[point_id]["created_at"] = datetime.now(timezone.utc).isoformat()
        self.mark({"inc-a1": "corrupt"})
        JOBS["job-1"].update({"retention_days": 7, "retention_min_fulls": 0, "gfs": {}})

        with patch.object(abr_v41, "_delete_backup_artifacts_from_point", return_value=True), patch.object(
            abr_v41.clock_guard, "retention_block", return_value=None
        ):
            abr_v41.run_job_retention("job-1")

        self.assertEqual(sorted(BACKUPS), ["full-c"])


class VerifyNotificationTests(unittest.TestCase):
    def test_a_failed_verify_sends_the_event_naming_the_damage(self) -> None:
        report = {
            "id": "chain-1",
            "result": "invalid",
            "checked_artifacts": 4,
            "checked_restore_points": 4,
            "errors": [{"point_id": "inc-2", "damaged": True, "message": "Error: data range 0 is corrupt"}],
            "corrupt": {"corrupt_now": ["inc-2", "inc-3"]},
        }
        task = {"id": "s-1", "parameters": {"vm_name": "VM-1", "job_name": "Nightly", "repository_name": "Repo", "backup_id": "chain-1"}}
        with patch.object(notification_detectors, "notify") as notify:
            notification_detectors.verify_finished(task, report)

        event, subject, body = notify.call_args.args
        self.assertEqual((event, subject), ("backup.verify_failed", "Backup verification of VM-1 found damaged data"))
        self.assertIn("2 restore point(s) of the chain are marked corrupt", body)
        self.assertIn("Job:           Nightly", body)

    def test_a_passing_or_stopped_verify_sends_nothing(self) -> None:
        with patch.object(notification_detectors, "notify") as notify:
            notification_detectors.verify_finished({"id": "s"}, {"result": "valid"})
            notification_detectors.verify_finished({"id": "s"}, {"result": "invalid", "canceled": True})
        notify.assert_not_called()

    def test_a_repo_verify_that_found_damage_sends_the_event(self) -> None:
        health = {"status": "failed", "message": "1 of 3 failed", "errors": [{"point_id": "p", "damaged": True}], "corrupt": {"corrupt_now": ["p"]}}
        with patch.object(notification_detectors, "notify") as notify:
            notification_detectors.repository_verify_failed({"id": "r", "name": "Repo"}, health)
            notification_detectors.repository_verify_failed({"id": "r", "name": "Repo"}, {"status": "ok"})
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(notify.call_args.args[1], "Repo verify of Repo found damaged data")


class VerifyScheduleSettingsTests(unittest.TestCase):
    def test_a_schedule_its_state_and_the_whole_chain_cycle_are_read(self) -> None:
        updates = abr_v41.verify_settings_from_request(
            {"verify_schedule": {"type": "daily", "time": "05:00"}, "verify_whole_chain_every": 3}
        )
        self.assertEqual((updates["verify_schedule"]["kind"], updates["verify_whole_chain_every"]), ("daily", 3))
        self.assertIsNotNone(updates["verify_next_run_at"])

        off = abr_v41.verify_settings_from_request({"verify_enabled": False}, {"verify_schedule": updates["verify_schedule"]})
        self.assertFalse(off["verify_schedule"]["enabled"])
        self.assertIsNone(off["verify_next_run_at"])
        self.assertIsNone(abr_v41.verify_settings_from_request({"verify_schedule": "none"}, updates)["verify_schedule"])
        self.assertIsNone(abr_v41.verify_settings_from_request({"verify_schedule": {"type": "daily", "time": "05:00"}}, job_enabled=False)["verify_next_run_at"])

    def test_bad_settings_are_refused(self) -> None:
        for payload in (
            {"verify_whole_chain_every": 0},
            {"verify_whole_chain_every": 101},
            {"verify_whole_chain_every": "2"},
            {"verify_enabled": True},
            {"verify_schedule": {"type": "weekly", "time": "05:00"}},
        ):
            with self.subTest(payload=payload), self.assertRaises(ApiError):
                abr_v41.verify_settings_from_request(payload)


class _ImmediateThread:
    def __init__(self, target, args=(), **_kwargs) -> None:
        self.target, self.args = target, args

    def start(self) -> None:
        self.target(*self.args)


class JobVerifyRunTests(_Catalog):
    def run_verify(self) -> list[tuple[str, object]]:
        calls: list[tuple[str, object]] = []

        def session(chain_id, task_id, **kwargs):
            calls.append((TASKS[task_id]["parameters"]["scope"], kwargs.get("point_ids")))
            return {"result": "valid"}

        with patch.object(abr_v41.threading, "Thread", _ImmediateThread), patch.object(abr_v41, "_run_verify_session", side_effect=session):
            abr_v41.run_job_verify("job-1")
        return calls

    def test_every_nth_run_checks_whole_chains_and_the_others_the_newest_point(self) -> None:
        self.chain()
        JOBS["job-1"]["verify_whole_chain_every"] = 2

        runs = [self.run_verify() for _ in range(3)]

        self.assertEqual(runs, [[("newest_points", {"inc-b3"})], [("whole_chains", None)], [("newest_points", {"inc-b3"})]])
        last = JOBS["job-1"]["last_verify"]
        self.assertEqual((last["run"], last["scope"], last["result"]), (3, "newest_points", "success"))
        sessions = [task for task in TASKS.values() if task["task_type"] == "verify"]
        self.assertEqual({(task["job_id"], task["trigger"]) for task in sessions}, {("job-1", "schedule")})
        self.assertFalse(abr_v41.job_verify_running("job-1"))

    def test_a_job_already_verifying_is_not_started_again(self) -> None:
        self.chain()
        abr_v41._VERIFY_RUNS.add("job-1")
        self.addCleanup(abr_v41._VERIFY_RUNS.discard, "job-1")

        self.assertIsNone(abr_v41.run_job_verify("job-1"))


class VerifyDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        JOBS.clear()
        TASKS.clear()
        self.addCleanup(JOBS.clear)
        self.addCleanup(TASKS.clear)
        self.scheduler = JobScheduler(poll_interval_seconds=0.1)
        self.now = datetime.now(timezone.utc)

    def job(self, due_in: timedelta) -> dict:
        schedule = abr_v41.verify_settings_from_request({"verify_schedule": {"type": "daily", "time": "05:00"}})["verify_schedule"]
        JOBS["job-1"] = {"id": "job-1", "name": "Nightly", "job_type": "backup", "verify_schedule": schedule, "verify_next_run_at": (self.now + due_in).isoformat()}
        return JOBS["job-1"]

    def test_a_due_verify_starts_and_moves_its_next_run(self) -> None:
        job = self.job(timedelta(minutes=-1))
        with patch("python.srv.core.job_scheduler.run_job_verify", return_value={"run": 1, "scope": "newest_points"}) as start:
            self.scheduler._dispatch_due_verify(job, abr_v41.normalize_job_record(job), self.now)

        start.assert_called_once_with("job-1", trigger="schedule")
        self.assertGreater(datetime.fromisoformat(JOBS["job-1"]["verify_next_run_at"]), self.now)

    def test_a_verify_that_is_not_due_or_switched_off_waits(self) -> None:
        job = self.job(timedelta(minutes=5))
        with patch("python.srv.core.job_scheduler.run_job_verify") as start:
            self.scheduler._dispatch_due_verify(job, abr_v41.normalize_job_record(job), self.now)
            job = self.job(timedelta(minutes=-1))
            job["verify_schedule"] = dict(job["verify_schedule"], enabled=False)
            self.scheduler._dispatch_due_verify(job, abr_v41.normalize_job_record(job), self.now)
        start.assert_not_called()

    def test_a_job_that_is_verifying_starts_no_backup(self) -> None:
        self.job(timedelta(minutes=-1))
        with patch.object(JobScheduler, "_is_supported_backup_job", return_value=True), patch(
            "python.srv.core.job_scheduler.job_verify_running", return_value=True
        ), patch.object(JobScheduler, "_due_slots") as due:
            self.scheduler._dispatch_due_jobs()
        due.assert_not_called()


if __name__ == "__main__":
    unittest.main()
