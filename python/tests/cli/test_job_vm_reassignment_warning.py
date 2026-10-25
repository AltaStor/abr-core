# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from python.cli.parsing.parser_builder import AltaStorParserBuilder

MESSAGE = (
    "Inventory rescan at 2026-10-22T10:00:00+00:00 moved this job from VM vm-old, which is no longer in the "
    "inventory, to VM vm-new, which now carries its name 'app'. The job backs up that VM from now on, starting "
    "with a full backup"
)
MOVED = {
    "id": "job-1",
    "name": "nightly",
    "vm_ids": ["vm-new"],
    "clock_anomalies": [],
    "vm_reassignments": [
        {
            "vm_name": "app",
            "previous_vm_id": "vm-old",
            "vm_id": "vm-new",
            "reassigned_at": "2026-10-22T10:00:00+00:00",
            "message": MESSAGE,
        }
    ],
}
STAYED = {"id": "job-2", "name": "weekly", "vm_ids": ["vm-1"], "clock_anomalies": [], "vm_reassignments": []}


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.status_code = 200
        self.content = b"{}"
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, object]:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class JobVmReassignmentWarningCliTests(unittest.TestCase):
    def _run(self, *argv: str, output: str = "table") -> tuple[str, str]:
        args = AltaStorParserBuilder().build().parse_args(["--base-url", "http://example.test", "--output", output, *argv])

        def fake_request(method, url, **kwargs):
            return _FakeResponse(MOVED if url.endswith("/job-1") else STAYED)

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("python.cli.api.client.requests.request", side_effect=fake_request), redirect_stdout(
            stdout
        ), redirect_stderr(stderr):
            self.assertEqual(args.handler(args), 0)
        return stdout.getvalue(), stderr.getvalue()

    def test_job_info_warns_that_the_job_was_moved_to_another_vm(self) -> None:
        stdout, stderr = self._run("job", "info", "--id", "job-1")

        self.assertIn("vm_reassignment_warning", stdout)
        self.assertNotIn('"previous_vm_id"', stdout)
        self.assertEqual(stderr.strip(), f"Warning: {MESSAGE}")

    def test_job_info_json_keeps_the_move_as_data(self) -> None:
        stdout, _stderr = self._run("job", "info", "--id", "job-1", output="json")

        payload = json.loads(stdout)
        self.assertEqual(payload["vm_reassignments"][0]["previous_vm_id"], "vm-old")
        self.assertNotIn("vm_reassignment_warning", payload)

    def test_a_job_that_kept_its_vms_shows_no_warning(self) -> None:
        stdout, stderr = self._run("job", "info", "--id", "job-2")

        self.assertNotIn("vm_reassignment", stdout)
        self.assertEqual(stderr, "")


if __name__ == "__main__":
    unittest.main()
