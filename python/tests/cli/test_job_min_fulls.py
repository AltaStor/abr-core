# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from python.cli.parsing.parser_builder import AltaStorParserBuilder
from python.cli.services.job_service import JobService

CREATE = [
    "job", "create", "--name", "nightly", "--inventory", "vc", "--vm", "vm-1", "--repository", "repo",
    "--active-full-schedule", "weekly", "--active-full-days", "sun", "--active-full-time", "02:00",
]


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status_code = 200
        self.content = b"{}"
        self.headers: dict[str, str] = {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class RetentionMinFullsFlagTests(unittest.TestCase):
    @staticmethod
    def _parse(*argv: str):
        return AltaStorParserBuilder().build().parse_args(["--base-url", "http://example.test", *argv])

    def test_create_sends_the_floor_only_when_it_is_given(self) -> None:
        self.assertNotIn("retention_min_fulls", JobService(None).create_backup_job_payload(self._parse(*CREATE)))
        payload = JobService(None).create_backup_job_payload(self._parse(*CREATE, "--retention-min-fulls", "2"))
        self.assertEqual(payload["retention_min_fulls"], 2)

    def test_edit_and_create_copy_send_it_too_and_zero_switches_it_off(self) -> None:
        edit = JobService(None).build_edit_payload({"id": "job-1"}, self._parse("job", "edit", "--id", "job-1", "--retention-min-fulls", "0"))
        untouched = JobService(None).build_edit_payload({"id": "job-1"}, self._parse("job", "edit", "--id", "job-1", "--description", "x"))
        copy = JobService(None).create_copy_job_payload(
            self._parse("job", "create-copy", "--name", "clone", "--source", "nightly", "--retention-min-fulls", "3")
        )

        self.assertEqual(edit["retention_min_fulls"], 0)
        self.assertNotIn("retention_min_fulls", untouched)
        self.assertEqual(copy["retention_min_fulls"], 3)

    def _retention_stderr(self, result: dict) -> tuple[str, str]:
        args = self._parse("job", "retention", "--id", "job-1")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("python.cli.api.client.requests.request", return_value=_FakeResponse(result)), redirect_stdout(
            stdout
        ), redirect_stderr(stderr):
            self.assertEqual(args.handler(args), 0)
        return stdout.getvalue(), stderr.getvalue()

    def test_job_retention_names_the_floor_and_what_it_kept(self) -> None:
        chain = {
            "vm_name": "demo-vm", "repository_name": "Local", "points": 4, "removed": ["full-a", "incr-a"],
            "held": [], "failed": [], "kept_for_chain": [], "kept_min_fulls": ["full-b", "incr-b"], "skipped_reason": None,
        }
        result = {
            "job_name": "nightly", "retention_days": 7, "retention_min_fulls": 1, "gfs": {},
            "summary": {"points": 4, "removed": 2, "failed": 0}, "chains": [chain],
        }

        stdout, stderr = self._retention_stderr(result)

        self.assertIn("keep 7 day(s), at least the newest 1 full backup(s). 2 of 4 restore point(s) removed.", stderr)
        self.assertIn("2 expired kept as the newest full(s)", stdout)

    def test_job_retention_reads_as_before_when_the_floor_is_off(self) -> None:
        result = {"job_name": "nightly", "retention_days": 7, "retention_min_fulls": 0, "gfs": {}, "summary": {"points": 0, "removed": 0}, "chains": []}

        _stdout, stderr = self._retention_stderr(result)

        self.assertIn("Retention of job nightly: keep 7 day(s). 0 of 0 restore point(s) removed.", stderr)


if __name__ == "__main__":
    unittest.main()
