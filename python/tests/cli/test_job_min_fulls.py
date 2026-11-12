# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from python.cli.commands.inventory_commands import INVENTORY_SCAN_TIMEOUT_SECONDS
from python.cli.exit_codes import GENERAL_ERROR, INVALID_STATE
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


    def test_job_retention_fails_naming_a_share_that_is_not_mounted(self) -> None:
        reason = "The share \\\\nas\\backups of repository Shared is not mounted at /mnt/hv: it is a folder on this appliance's own ext4 filesystem at /. Mount it there, then try again"
        chain = {
            "vm_name": "demo-vm", "repository_name": "Shared", "points": 2, "removed": [], "held": [], "failed": [],
            "kept_for_chain": [], "kept_min_fulls": [], "skipped_reason": reason, "repository_unavailable": reason,
        }
        result = {
            "job_name": "nightly", "retention_days": 7, "retention_min_fulls": 0, "gfs": {},
            "summary": {"points": 2, "removed": 0, "failed": 0, "unavailable": 1}, "chains": [chain],
        }
        codes = {}
        for dry_run in (False, True):
            argv = ["job", "retention", "--id", "job-1", *(["--dry-run"] if dry_run else [])]
            args = self._parse(*argv)
            stderr = io.StringIO()
            with patch("python.cli.api.client.requests.request", return_value=_FakeResponse(result)), redirect_stdout(
                io.StringIO()
            ), redirect_stderr(stderr):
                codes[dry_run] = (args.handler(args), stderr.getvalue())

        self.assertEqual(codes[False][0], INVALID_STATE)
        self.assertIn("Error: 1 chain(s) were not touched: The share", codes[False][1])
        self.assertEqual(codes[True][0], 0)
        self.assertIn("Warning: 1 chain(s) were not touched", codes[True][1])

    def _retention_run(self, result: dict, *extra: str) -> tuple[int, str, object]:
        args = self._parse("job", "retention", "--id", "job-1", *extra)
        stderr = io.StringIO()
        with patch("python.cli.api.client.requests.request", return_value=_FakeResponse(result)) as request, redirect_stdout(
            io.StringIO()
        ), redirect_stderr(stderr):
            code = args.handler(args)
        return code, stderr.getvalue(), request

    def test_job_retention_waits_for_the_server_as_long_as_a_backup_delete(self) -> None:
        from python.cli.commands.backup_commands import BACKUP_DELETE_TIMEOUT_SECONDS
        from python.cli.commands.job_commands import RETENTION_TIMEOUT_SECONDS

        result = {"job_name": "nightly", "retention_days": 7, "gfs": {}, "summary": {"points": 0, "removed": 0}, "chains": []}
        for extra in ((), ("--dry-run",)):
            with self.subTest(extra=extra):
                _code, _stderr, request = self._retention_run(result, *extra)

                self.assertEqual(request.call_args.kwargs["timeout"], RETENTION_TIMEOUT_SECONDS)
                self.assertEqual(RETENTION_TIMEOUT_SECONDS, BACKUP_DELETE_TIMEOUT_SECONDS)

    def test_job_retention_exits_with_an_error_when_points_could_not_be_removed(self) -> None:
        chain = {
            "vm_name": "demo-vm", "repository_name": "S3", "points": 4, "removed": ["incr-a"], "held": [],
            "failed": ["full-a"], "kept_for_chain": [], "kept_min_fulls": [], "skipped_reason": None,
        }
        result = {"job_name": "nightly", "retention_days": 2, "gfs": {}, "summary": {"points": 4, "removed": 1, "failed": 1}, "chains": [chain]}

        code, stderr, _request = self._retention_run(result)

        self.assertEqual(code, GENERAL_ERROR)
        self.assertIn("Error: 1 expired restore point(s) could not be removed", stderr)

    def test_job_retention_succeeds_when_points_are_only_held_by_object_lock(self) -> None:
        chain = {
            "vm_name": "demo-vm", "repository_name": "S3", "points": 4, "removed": ["incr-a"], "held": [{"id": "full-a", "until": "2026-11-10T00:00:00+00:00"}],
            "failed": [], "kept_for_chain": [], "kept_min_fulls": [], "skipped_reason": None,
        }
        result = {"job_name": "nightly", "retention_days": 2, "gfs": {}, "summary": {"points": 4, "removed": 1, "failed": 0}, "chains": [chain]}

        code, _stderr, _request = self._retention_run(result)

        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()


class JobSaveWaitsForDiscoveryTests(unittest.TestCase):
    @staticmethod
    def _parse(*argv: str):
        return AltaStorParserBuilder().build().parse_args(["--base-url", "http://example.test", *argv])

    def _requests(self, *argv: str) -> list:
        args = self._parse(*argv)
        job = {"id": "job-1", "name": "nightly", "job_type": "backup", "items": []}
        with patch("python.cli.api.client.requests.request", return_value=_FakeResponse(job)) as request, redirect_stdout(
            io.StringIO()
        ), redirect_stderr(io.StringIO()):
            self.assertEqual(args.handler(args), 0)
        return [(call.args[0], call.args[1].removeprefix("http://example.test/"), call.kwargs["timeout"]) for call in request.call_args_list]

    def test_job_create_waits_for_the_first_scan_of_a_new_inventory_as_long_as_a_rescan(self) -> None:
        sent = self._requests(*CREATE)

        self.assertIn(("POST", "api/v1/jobs", INVENTORY_SCAN_TIMEOUT_SECONDS), sent)

    def test_job_edit_of_the_vms_waits_as_long_as_a_rescan_too(self) -> None:
        sent = self._requests("job", "edit", "--id", "job-1", "--vm", "vm-2")

        self.assertIn(("PATCH", "api/v1/jobs/job-1", INVENTORY_SCAN_TIMEOUT_SECONDS), sent)
        self.assertIn(("GET", "api/v1/jobs/job-1", 30), sent)
