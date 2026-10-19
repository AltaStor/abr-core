# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest
from unittest.mock import patch

from python.srv.core import job_scheduler
from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, REPOSITORIES, TASKS
from python.srv.services import abr_v41
from python.srv.services.api_errors import ApiError

JOB_ID = "job-1"
VM_ID = "vm-1"
REPO_ID = "repo-1"


def _point(point_id: str, execution_type: str, created_at: str) -> dict:
    return abr_v41.normalize_backup_point(
        {
            "id": point_id,
            "job_id": JOB_ID,
            "vm_id": VM_ID,
            "vm_name": "vm",
            "repository_id": REPO_ID,
            "status": "available",
            "full_backup": execution_type != "Incremental",
            "execution_type": execution_type,
            "execution_reason": "ForceFull" if execution_type != "Incremental" else "StandardIncremental",
            "created_at": created_at,
            "artifacts": [{"disk_key": 2000, "path": f"{point_id}/disk-0.qcow2"}],
        }
    )


class ManualStartOnASyntheticDayTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES, TASKS):
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in (BACKUPS, JOBS, REPOSITORIES, TASKS)])
        REPOSITORIES[REPO_ID] = {"id": REPO_ID, "name": "Local", "type": "local", "path": "/srv/backups"}
        JOBS[JOB_ID] = abr_v41.normalize_job_record(
            {
                "id": JOB_ID,
                "name": "j1",
                "job_type": "backup",
                "inventory_id": "inv-1",
                "inventory_name": "inv",
                "repo_id": REPO_ID,
                "repo_name": "Local",
                "vm_ids": [VM_ID],
                "vm_names": ["vm"],
                "retention_days": 7,
                "active_full_days": ["mon"],
                "synthetic_full_days": ["sun"],
            }
        )
        BACKUPS["full-1"] = _point("full-1", "ActiveFull", "2026-10-18T08:00:00+00:00")
        for target in (
            patch.object(abr_v41, "persist_task", side_effect=lambda task: TASKS.__setitem__(task["id"], task)),
            patch.object(abr_v41, "_persist_canonical_job", side_effect=lambda job: job),
        ):
            target.start()
            self.addCleanup(target.stop)

    def _on(self, *days: str):
        return patch.object(
            abr_v41,
            "schedule_today_matches_days",
            side_effect=lambda job, wanted, now=None: any(str(day).lower()[:3] in days for day in wanted),
        )

    def _start(self, **flags) -> list[dict]:
        return abr_v41.start_job_record(JOB_ID, **flags)["items"]

    def _decisions(self, sessions: list[dict]) -> list[tuple[str, str]]:
        return [(TASKS[s["id"]]["parameters"]["resolved_backup_type"], TASKS[s["id"]]["parameters"]["resolved_backup_reason"]) for s in sessions]

    def test_a_plain_start_behind_a_lone_full_takes_an_incremental_then_the_synthetic_full(self) -> None:
        with self._on("sun"):
            sessions = self._start()

        self.assertEqual(
            self._decisions(sessions),
            [("Incremental", "SyntheticFullSourceIncrement"), ("SyntheticFull", "SyntheticFullWindow")],
        )
        first, second = (TASKS[s["id"]] for s in sessions)
        self.assertNotIn("requires_task_id", first["parameters"])
        self.assertEqual(second["parameters"]["requires_task_id"], first["id"])
        self.assertEqual(first["run_id"], second["run_id"])

    def test_a_plain_start_on_a_chain_with_incrementals_also_takes_todays_increment_first(self) -> None:
        BACKUPS["inc-1"] = _point("inc-1", "Incremental", "2026-10-18T09:00:00+00:00")

        with self._on("sun"):
            sessions = self._start()

        self.assertEqual(
            self._decisions(sessions),
            [("Incremental", "SyntheticFullSourceIncrement"), ("SyntheticFull", "SyntheticFullWindow")],
        )

    def test_a_forced_synthetic_still_merges_the_chain_as_it_stands(self) -> None:
        BACKUPS["inc-1"] = _point("inc-1", "Incremental", "2026-10-18T09:00:00+00:00")

        with self._on("sun"), patch.object(abr_v41, "_ensure_chain_can_be_merged"):
            sessions = self._start(force_synthetic=True)

        self.assertEqual(self._decisions(sessions), [("SyntheticFull", "ForceSynthetic")])
        self.assertNotIn("requires_task_id", TASKS[sessions[0]["id"]]["parameters"])

    def test_a_forced_synthetic_on_a_lone_full_is_still_refused_at_start(self) -> None:
        with self._on("sun"), self.assertRaises(ApiError) as caught:
            self._start(force_synthetic=True)

        self.assertEqual((caught.exception.status_code, caught.exception.code), (422, "chain_cannot_be_merged"))
        self.assertEqual(TASKS, {})

    def test_other_days_and_flags_still_start_one_session(self) -> None:
        cases = {
            "ordinary day": ((), {}, ("Incremental", "StandardIncremental")),
            "active full day": (("mon",), {}, ("ActiveFull", "ActiveFullWindow")),
            "both days": (("mon", "sun"), {}, ("ActiveFull", "ActiveOverridesSynthetic")),
            "--full on a synthetic day": (("sun",), {"force_full": True}, ("ActiveFull", "ForceFull")),
        }
        for name, (days, flags, expected) in cases.items():
            with self.subTest(name):
                TASKS.clear()
                with self._on(*days):
                    sessions = self._start(**flags)
                self.assertEqual(self._decisions(sessions), [expected])


class ExecutionPhasesTests(unittest.TestCase):
    def test_the_scheduler_and_a_manual_start_split_a_synthetic_full_the_same_way(self) -> None:
        self.assertIs(job_scheduler.execution_phases, abr_v41.execution_phases)
        self.assertEqual(
            abr_v41.execution_phases("SyntheticFull", "SyntheticFullSchedule"),
            [("Incremental", "SyntheticFullSourceIncrement"), ("SyntheticFull", "SyntheticFullSchedule")],
        )
        self.assertEqual(abr_v41.execution_phases("SyntheticFull", "ForceSynthetic"), [("SyntheticFull", "ForceSynthetic")])
        self.assertEqual(abr_v41.execution_phases("Incremental", "StandardIncremental"), [("Incremental", "StandardIncremental")])


if __name__ == "__main__":
    unittest.main()
