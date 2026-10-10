# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from python.cli.parsing.parser_builder import AltaStorParserBuilder

MESSAGE = "The last run is dated 2026-10-13T07:15:08+00:00, ahead of this server's time (2026-10-09T07:15:41+00:00)."
AHEAD = {
    "id": "job-1",
    "name": "nightly",
    "last_run_at": "2026-10-13T07:15:08+00:00",
    "next_run_at": "2026-10-10T03:00:00+00:00",
    "clock_anomalies": [
        {
            "kind": "last_run_ahead_of_server_clock",
            "last_run_at": "2026-10-13T07:15:08+00:00",
            "server_time": "2026-10-09T07:15:41+00:00",
            "message": MESSAGE,
        }
    ],
}
SANE = {
    "id": "job-2",
    "name": "weekly",
    "last_run_at": "2026-10-09T06:15:08+00:00",
    "next_run_at": "2026-10-10T03:00:00+00:00",
    "clock_anomalies": [],
}


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


class JobClockWarningCliTests(unittest.TestCase):
    def _run(self, *argv: str, output: str = "table") -> tuple[str, str]:
        args = AltaStorParserBuilder().build().parse_args(["--base-url", "http://example.test", "--output", output, *argv])

        def fake_request(method, url, **kwargs):
            if url.endswith("/api/v1/jobs"):
                return _FakeResponse({"items": [AHEAD, SANE]})
            return _FakeResponse(AHEAD if url.endswith("/job-1") else SANE)

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("python.cli.api.client.requests.request", side_effect=fake_request), redirect_stdout(
            stdout
        ), redirect_stderr(stderr):
            self.assertEqual(args.handler(args), 0)
        return stdout.getvalue(), stderr.getvalue()

    def test_the_job_list_marks_a_last_run_ahead_of_the_server_clock(self) -> None:
        stdout, stderr = self._run("job", "list")
        rows = {line.split()[1]: line for line in stdout.splitlines() if line.startswith("job-")}
        self.assertIn("2026-10-13T07:15:08+00:00 (!)", rows["nightly"])
        self.assertNotIn("(!)", rows["weekly"])
        self.assertIn("Warning: job 'nightly' last ran at 2026-10-13T07:15:08+00:00", stderr)
        self.assertNotIn("weekly", stderr)

    def test_the_job_list_json_keeps_the_raw_fields(self) -> None:
        stdout, _stderr = self._run("job", "list", output="json")
        items = {item["name"]: item for item in json.loads(stdout)["items"]}
        self.assertEqual(items["nightly"]["last_run_at"], "2026-10-13T07:15:08+00:00")
        self.assertEqual(items["nightly"]["clock_anomalies"][0]["kind"], "last_run_ahead_of_server_clock")

    def test_job_info_shows_a_readable_warning_instead_of_raw_json(self) -> None:
        stdout, stderr = self._run("job", "info", "--id", "job-1")
        self.assertIn("clock_warning", stdout)
        self.assertNotIn("clock_anomalies", stdout)
        self.assertNotIn('"kind"', stdout)
        self.assertEqual(stderr.strip(), f"Warning: {MESSAGE}")

    def test_job_info_json_keeps_the_anomaly_as_data(self) -> None:
        stdout, _stderr = self._run("job", "info", "--id", "job-1", output="json")
        payload = json.loads(stdout)
        self.assertEqual(payload["clock_anomalies"][0]["message"], MESSAGE)
        self.assertNotIn("clock_warning", payload)

    def test_a_job_on_a_sane_clock_shows_no_warning(self) -> None:
        stdout, stderr = self._run("job", "info", "--id", "job-2")
        self.assertNotIn("clock_warning", stdout)
        self.assertEqual(stderr, "")


if __name__ == "__main__":
    unittest.main()
