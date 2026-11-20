# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import unittest

from python.cli.parsing.parser_builder import build_parser
from python.cli.services.job_service import JobService

FULL = ["--active-full-schedule", "periodic", "--active-full-every", "3650d"]
BACKUP = ["job", "create", "--name", "A", "--vms", "vm-1", "--inventory", "vc", *FULL]


class DefaultRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = build_parser()
        self.jobs = JobService(None)

    def backup(self, *argv: str) -> dict:
        return self.jobs.create_backup_job_payload(self.parser.parse_args([*BACKUP, *argv]))

    def test_a_job_without_a_repository_is_sent_for_the_server_to_pick_the_default(self) -> None:
        payload = self.backup()

        self.assertNotIn("repo", payload)
        self.assertNotIn("repo_id", payload)

    def test_a_named_repository_is_still_sent(self) -> None:
        for argv, key, value in (
            (["--repository", "Local"], "repo", "Local"),
            (["--repo", "Local"], "repo", "Local"),
            (["--repository-id", "repo-1"], "repo_id", "repo-1"),
            (["--repo-id", "repo-1"], "repo_id", "repo-1"),
        ):
            with self.subTest(argv=argv):
                payload = self.backup(*argv)

                self.assertEqual(payload[key], value)

    def test_a_backup_copy_job_still_needs_the_repository_the_copies_go_to(self) -> None:
        args = self.parser.parse_args(["job", "create-backup-copy", "--name", "C", "--source-job", "A", *FULL])

        with self.assertRaises(ValueError) as caught:
            self.jobs.create_backup_copy_job_payload(args)
        self.assertIn("--repo", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
