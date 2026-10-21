# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, SYSTEM_STATE, TASKS
from python.srv.services import (
    abr_v41,
    audit_service,
    backup_catalog,
    clock_guard,
    rbac,
)
from python.tests.srv.rbac.rbac_case import RbacApiCase

START = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self) -> None:
        self.wall = START
        self.monotonic = 1000.0

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.monotonic += seconds

    def jump(self, delta: timedelta) -> None:
        self.wall += delta


class ClockCase(unittest.TestCase):
    def setUp(self) -> None:
        SYSTEM_STATE.clear()
        clock_guard.forget()
        self.addCleanup(SYSTEM_STATE.clear)
        self.addCleanup(clock_guard.forget)
        self.clock = FakeClock()
        for target in (
            patch.object(clock_guard, "_now", side_effect=lambda: self.clock.wall),
            patch.object(clock_guard.time, "monotonic", side_effect=lambda: self.clock.monotonic),
        ):
            target.start()
            self.addCleanup(target.stop)

    def restart(self, downtime: timedelta) -> dict:
        clock_guard.forget()
        self.clock.advance(downtime.total_seconds())
        return clock_guard.load()

    def block_by_jump(self) -> None:
        clock_guard.load()
        self.clock.advance(60)
        self.clock.jump(timedelta(days=400))
        with self.assertLogs("python.srv.services.clock_guard", level="ERROR"):
            clock_guard.tick()


class ClockGuardTests(ClockCase):
    def test_days_of_ordinary_ticks_never_block(self) -> None:
        clock_guard.load()
        for _ in range(3 * 24 * 60):
            self.clock.advance(60)
            clock_guard.tick()

        self.assertIsNone(clock_guard.retention_block())
        self.assertEqual(SYSTEM_STATE["clock"]["last_known_at"], (START + timedelta(days=3)).isoformat(timespec="seconds"))

    def test_a_forward_jump_while_running_blocks_retention(self) -> None:
        self.block_by_jump()

        status = clock_guard.status()
        self.assertTrue(status["blocked"])
        self.assertEqual(status["source"], clock_guard.RUNNING)
        self.assertEqual(status["jump_seconds"], 400 * 86400)
        self.assertIn("400 day(s)", status["message"])
        self.assertIn("abr system clock confirm", status["message"])
        self.assertTrue(SYSTEM_STATE["clock"]["blocked"])

    def test_jumps_up_to_the_threshold_or_backwards_do_not_block(self) -> None:
        clock_guard.load()
        for delta in (timedelta(hours=23), timedelta(hours=-30), timedelta(minutes=5)):
            with self.subTest(delta=delta):
                self.clock.advance(60)
                self.clock.jump(delta)
                clock_guard.tick()
                self.assertIsNone(clock_guard.retention_block())

    def test_the_threshold_comes_from_the_environment(self) -> None:
        clock_guard.load()
        self.clock.advance(60)
        self.clock.jump(timedelta(hours=3))

        with patch.dict("os.environ", {"BACKUPSERVER_CLOCK_JUMP_HOURS": "2"}), self.assertLogs(
            "python.srv.services.clock_guard", level="ERROR"
        ):
            clock_guard.tick()

        self.assertTrue(clock_guard.status()["blocked"])

    def test_a_restart_after_a_short_downtime_does_not_block(self) -> None:
        clock_guard.load()

        status = self.restart(timedelta(hours=6))

        self.assertFalse(status["blocked"])
        self.assertEqual(status["last_known_at"], (START + timedelta(hours=6)).isoformat(timespec="seconds"))

    def test_a_start_long_after_the_last_known_time_blocks(self) -> None:
        clock_guard.load()

        with self.assertLogs("python.srv.services.clock_guard", level="ERROR"):
            status = self.restart(timedelta(days=3))

        self.assertTrue(status["blocked"])
        self.assertEqual(status["source"], clock_guard.STARTUP)
        self.assertEqual(status["last_trusted_at"], START.isoformat(timespec="seconds"))
        self.assertIn("between two runs of the server", status["message"])

    def test_the_first_start_ever_does_not_block(self) -> None:
        self.assertFalse(clock_guard.load()["blocked"])

    def test_a_block_survives_a_restart_until_it_is_confirmed(self) -> None:
        self.block_by_jump()

        self.assertTrue(self.restart(timedelta(minutes=1))["blocked"])

        result = clock_guard.confirm("admin")

        self.assertTrue(result["was_blocked"])
        self.assertFalse(result["blocked"])
        self.assertEqual(result["confirmed_by"], "admin")
        self.assertIsNone(clock_guard.retention_block())
        self.assertFalse(self.restart(timedelta(minutes=1))["blocked"])

    def test_confirming_an_unblocked_clock_says_so(self) -> None:
        clock_guard.load()

        self.assertFalse(clock_guard.confirm("admin")["was_blocked"])

    def test_an_unreadable_catalog_does_not_block(self) -> None:
        with patch.object(clock_guard, "load_system_state", side_effect=OSError("down")), self.assertLogs(
            "python.srv.services.clock_guard", level="WARNING"
        ):
            self.assertFalse(clock_guard.load()["blocked"])


def _stamp(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def _point(point_id: str, age: timedelta, *, full: bool, parent: str | None = None) -> dict:
    return abr_v41.normalize_backup_point(
        {
            "id": point_id,
            "job_id": "job-1",
            "vm_id": "vm-1",
            "vm_name": "demo-vm",
            "repository_id": "repo-1",
            "execution_type": "ActiveFull" if full else "Incremental",
            "parent_point_id": parent,
            "segment_id": parent or point_id,
            "created_at": _stamp(-age),
            "artifact": {"backup_root": f"/does/not/matter/{point_id}"},
        }
    )


def _job(**overrides) -> dict:
    return {"id": "job-1", "name": "Nightly", "job_type": "backup", "repo_id": "repo-1", "retention_days": 7, "gfs": {}, **overrides}


class RetentionCase(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, TASKS, SYSTEM_STATE):
            store.clear()
        clock_guard.forget()
        self.addCleanup(lambda: [store.clear() for store in (BACKUPS, JOBS, TASKS, SYSTEM_STATE)])
        self.addCleanup(clock_guard.forget)
        JOBS["job-1"] = _job()
        for point in (
            _point("full-old", timedelta(days=20), full=True),
            _point("incr-old", timedelta(days=19), full=False, parent="full-old"),
            _point("full-new", timedelta(days=1), full=True),
        ):
            BACKUPS[point["id"]] = point
        deleter = patch.object(abr_v41, "_delete_backup_artifacts_from_point", return_value=True)
        deleter.start()
        self.addCleanup(deleter.stop)

    @staticmethod
    def block() -> None:
        clock_guard.load()
        with patch.object(clock_guard, "_now", return_value=datetime.now(timezone.utc) + timedelta(days=400)), patch(
            "python.srv.services.clock_guard.LOGGER"
        ):
            clock_guard.tick()


class RetentionUnderAClockJumpTests(RetentionCase):
    def test_retention_without_a_jump_still_removes_the_expired_segment(self) -> None:
        clock_guard.load()

        with self.assertLogs("python.srv.services.abr_v41", level="INFO") as captured:
            abr_v41.apply_retention_to_chain(_job(), vm_id="vm-1", repository_id="repo-1")

        self.assertEqual(sorted(BACKUPS), ["full-new"])
        removed = [line for line in captured.output if "Retention removed restore point" in line]
        self.assertEqual(len(removed), 2, captured.output)
        self.assertTrue(any("full-old" in line and "7 day(s)" in line and "cutoff" in line for line in removed))

    def test_a_blocked_clock_deletes_nothing_and_says_why(self) -> None:
        self.block()
        report = abr_v41.new_retention_report(_job(), "vm-1", "repo-1")

        with self.assertLogs("python.srv.services.abr_v41", level="WARNING") as captured:
            abr_v41.apply_retention_to_chain(_job(), vm_id="vm-1", repository_id="repo-1", report=report)

        self.assertEqual(sorted(BACKUPS), ["full-new", "full-old", "incr-old"])
        self.assertEqual(report["removed"], [])
        self.assertIn("Retention is blocked", report["skipped_reason"])
        self.assertEqual(report["clock_blocked"], report["skipped_reason"])
        self.assertTrue(any("deleted nothing" in line for line in captured.output))

    def test_a_dry_run_on_a_blocked_clock_still_previews(self) -> None:
        self.block()

        result = abr_v41.run_job_retention("job-1", dry_run=True)

        self.assertIn("Retention is blocked", result["clock_blocked"])
        self.assertEqual(sorted(result["chains"][0]["removed"]), ["full-old", "incr-old"])
        self.assertIsNone(result["chains"][0]["skipped_reason"])
        self.assertEqual(len(BACKUPS), 3)

    def test_a_job_retention_run_reports_the_block_and_resumes_after_confirm(self) -> None:
        self.block()

        blocked = abr_v41.run_job_retention("job-1")

        self.assertIn("Retention is blocked", blocked["clock_blocked"])
        self.assertEqual((blocked["summary"]["removed"], blocked["summary"]["skipped"]), (0, 1))
        self.assertEqual(len(BACKUPS), 3)

        clock_guard.confirm("admin")
        resumed = abr_v41.run_job_retention("job-1")

        self.assertIsNone(resumed["clock_blocked"])
        self.assertEqual(resumed["summary"]["removed"], 2)
        self.assertEqual(sorted(BACKUPS), ["full-new"])

    def test_health_reports_the_clock_while_retention_is_blocked(self) -> None:
        clock_guard.load()
        self.assertEqual(abr_v41.system_health()["components"]["clock"], "ok")

        self.block()

        health = abr_v41.system_health()
        self.assertEqual((health["status"], health["components"]["clock"]), ("degraded", "jump_detected"))


def _completed_backup_task() -> dict:
    return {
        "id": "task-1",
        "task_type": "backup",
        "status": "completed",
        "hypervisor_id": "",
        "resource_type": "virtual_machine",
        "resource_external_id": "vm-1",
        "job_id": "job-1",
        "updated_at": _stamp(-timedelta(minutes=1)),
        "result": {
            "vm_name": "demo-vm",
            "vm_external_id": "vm-1",
            "backup_mode": "full",
            "incremental": False,
            "resolved_backup_type": "ActiveFull",
            "resolved_backup_reason": "ForceFull",
            "finished_at": _stamp(-timedelta(minutes=1)),
        },
    }


class RetentionAfterABackupTests(RetentionCase):
    def test_the_session_warns_and_keeps_every_point_while_the_clock_is_blocked(self) -> None:
        self.block()
        task = _completed_backup_task()

        with self.assertLogs("python.srv.services.abr_v41", level="WARNING"):
            backup_catalog.register_backup_from_completed_task(task)

        self.assertTrue({"full-old", "incr-old", "full-new"} <= set(BACKUPS))
        session = abr_v41.session_to_dto(task)
        self.assertTrue(any("Retention is blocked" in str(item.get("message")) for item in session["warnings"]))
        self.assertEqual(session["state"], "completed")

    def test_the_session_log_names_the_points_retention_removed(self) -> None:
        clock_guard.load()
        task = _completed_backup_task()

        backup_catalog.register_backup_from_completed_task(task)

        self.assertNotIn("full-old", BACKUPS)
        session = abr_v41.session_to_dto(task)
        self.assertEqual(session["warnings"], [])
        lines = [str(entry.get("message")) for entry in session.get("logs") or []]
        self.assertTrue(
            any("Retention removed 2 restore point(s)" in line and "full-old" in line for line in lines), lines
        )


class ClockApiTests(RbacApiCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(SYSTEM_STATE.clear)
        clock_guard.load()
        with patch.object(clock_guard, "_now", return_value=datetime.now(timezone.utc) + timedelta(days=400)), patch(
            "python.srv.services.clock_guard.LOGGER"
        ):
            clock_guard.tick()

    def test_every_role_reads_the_clock(self) -> None:
        for role in (rbac.AUDITOR, rbac.BACKUP_OPERATOR, rbac.ADMINISTRATOR):
            with self.subTest(role=role):
                response = self.client.get("/api/v1/system/clock", headers=self.headers(role))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["blocked"])

    def test_only_an_administrator_confirms_and_it_is_audited(self) -> None:
        denied = self.client.post("/api/v1/system/clock/confirm", headers=self.headers(rbac.BACKUP_ADMINISTRATOR))
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertIsNotNone(clock_guard.retention_block())

        response = self.client.post("/api/v1/system/clock/confirm", headers=self.headers(rbac.ADMINISTRATOR))

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["blocked"], body["was_blocked"], body["confirmed_by"]), (False, True, "admin"))
        self.assertIsNone(clock_guard.retention_block())
        denied_entry, allowed_entry = self.audit(function_id="system.clock_confirm")
        self.assertEqual((denied_entry["result"], denied_entry["user"]), (audit_service.DENIED, "bart"))
        self.assertEqual((allowed_entry["result"], allowed_entry["user"]), (audit_service.ALLOWED, "admin"))
        self.assertTrue(allowed_entry["details"]["was_blocked"])


if __name__ == "__main__":
    unittest.main()
