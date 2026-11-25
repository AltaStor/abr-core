# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

import unittest

from python.cli.parsing.parser_builder import build_parser
from python.cli.services.job_service import JobService

FULL = ["--active-full-schedule", "periodic", "--active-full-every", "3650d"]
BACKUP = ["job", "create", "--name", "A", "--vms", "vm-1", "--inventory", "vc", "--repo", "Local", *FULL]
COPY = ["job", "create-backup-copy", "--name", "C", "--source-job", "A", "--repo", "S3", *FULL]
CLONE = ["job", "create-copy", "--name", "B", "--source", "A"]


class CreateStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = build_parser()
        self.jobs = JobService(None)

    def backup(self, *argv: str) -> dict:
        return self.jobs.create_backup_job_payload(self.parser.parse_args([*BACKUP, *argv]))

    def copy(self, *argv: str) -> dict:
        return self.jobs.create_backup_copy_job_payload(self.parser.parse_args([*COPY, *argv]))

    def clone(self, *argv: str) -> dict:
        return self.jobs.create_copy_job_payload(self.parser.parse_args([*CLONE, *argv]))

    def test_a_job_with_only_a_full_schedule_is_created_disabled(self) -> None:
        for build in (self.backup, self.copy, self.clone):
            with self.subTest(build=build.__name__):
                payload = build("--disabled")

                self.assertIs(payload["enabled"], False)
                self.assertNotIn("schedule", payload)

    def test_enabled_is_sent_as_well(self) -> None:
        for build in (self.backup, self.copy, self.clone):
            with self.subTest(build=build.__name__):
                self.assertIs(build("--enabled")["enabled"], True)

    def test_without_a_state_nothing_is_sent(self) -> None:
        for build in (self.backup, self.copy, self.clone):
            with self.subTest(build=build.__name__):
                payload = build()

                self.assertNotIn("enabled", payload)
                self.assertNotIn("schedule", payload)

    def test_a_regular_schedule_carries_the_state_as_before(self) -> None:
        for build in (self.backup, self.copy, self.clone):
            with self.subTest(build=build.__name__):
                payload = build("--schedule", "daily", "--time", "23:00", "--disabled")

                self.assertEqual(payload["schedule"], {"type": "daily", "time": "23:00", "enabled": False})
                self.assertNotIn("enabled", payload)

    def test_schedule_details_without_a_schedule_are_still_refused(self) -> None:
        for build in (self.backup, self.copy, self.clone):
            with self.subTest(build=build.__name__), self.assertRaisesRegex(ValueError, "Schedule details require --schedule"):
                build("--time", "03:00", "--disabled")

    def test_the_full_schedule_is_still_sent(self) -> None:
        self.assertEqual(self.backup("--disabled")["active_full_schedule"]["every"], "3650d")
        self.assertEqual(self.copy("--disabled")["active_full_schedule"]["every"], "3650d")


if __name__ == "__main__":
    unittest.main()
