# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

import unittest
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import JOBS
from python.srv.services import abr_v41
from python.tests.srv.jobs.test_guest_consistency_scope import (
    GuestConsistencyScopeTests,
)


class QuiesceFailureDefaultTests(GuestConsistencyScopeTests):
    def test_a_hyperv_job_made_without_the_option_shows_crash_consistent(self) -> None:
        job = self._create("hv", self.hyperv, "hv-01", "smb")

        self.assertEqual(job["on_quiesce_failure"], "crash_consistent")

    def test_a_vmware_job_made_without_the_option_shows_fail_as_before(self) -> None:
        job = self._create("vc", self.vcenter, "vc-01", "local")

        self.assertEqual(job["on_quiesce_failure"], "fail")

    def test_a_hyperv_job_set_to_fail_shows_and_keeps_fail(self) -> None:
        job = self._create("hv", self.hyperv, "hv-01", "smb")

        edited = abr_v41.edit_job_record(job["id"], {"task_parameters": {"on_quiesce_failure": "fail"}})

        self.assertEqual(edited["on_quiesce_failure"], "fail")
        self.assertEqual(JOBS[job["id"]]["task_parameters"]["on_quiesce_failure"], "fail")


class KeepFallbackTests(GuestConsistencyScopeTests):
    def setUp(self) -> None:
        super().setUp()
        self.state: dict[str, dict] = {}
        for name, effect in (
            ("load_system_state", lambda state_id: self.state.get(state_id)),
            ("persist_system_state", lambda record: self.state.__setitem__(record["id"], record)),
        ):
            patcher = patch.object(abr_v41, name, side_effect=effect)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_hyperv_jobs_stored_with_fail_are_set_to_crash_consistent_once(self) -> None:
        hv = self._create("hv", self.hyperv, "hv-01", "smb", {"on_quiesce_failure": "fail"})
        vc = self._create("vc", self.vcenter, "vc-01", "local", {"on_quiesce_failure": "fail"})

        with self.assertLogs(abr_v41.LOGGER, "INFO") as logs:
            moved = abr_v41.keep_hyperv_jobs_falling_back()

        self.assertEqual(moved, ["hv"])
        self.assertEqual(JOBS[hv["id"]]["task_parameters"]["on_quiesce_failure"], "crash_consistent")
        self.assertEqual(JOBS[vc["id"]]["task_parameters"]["on_quiesce_failure"], "fail")
        self.assertIn("crash_consistent on 1 Hyper-V job(s)", "\n".join(logs.output))

    def test_a_fail_set_after_the_first_start_is_kept_at_the_next_starts(self) -> None:
        abr_v41.keep_hyperv_jobs_falling_back()
        hv = self._create("hv", self.hyperv, "hv-01", "smb", {"on_quiesce_failure": "fail"})

        self.assertEqual(abr_v41.keep_hyperv_jobs_falling_back(), [])

        self.assertEqual(JOBS[hv["id"]]["task_parameters"]["on_quiesce_failure"], "fail")

    def test_jobs_without_the_setting_are_left_alone_and_the_start_is_recorded(self) -> None:
        hv = self._create("hv", self.hyperv, "hv-01", "smb")

        self.assertEqual(abr_v41.keep_hyperv_jobs_falling_back(), [])

        self.assertNotIn("on_quiesce_failure", JOBS[hv["id"]]["task_parameters"])
        self.assertIn(abr_v41.HYPERV_QUIESCE_STATE, self.state)


del GuestConsistencyScopeTests

if __name__ == "__main__":
    unittest.main()
