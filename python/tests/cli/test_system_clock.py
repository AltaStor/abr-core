# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from python.cli.exit_codes import INVALID_STATE
from python.cli.parsing.parser_builder import AltaStorParserBuilder

MESSAGE = (
    "Retention is blocked: the server clock moved forward by 400 day(s) while the server was running: it read "
    "2027-11-24T08:00:00+00:00 where 2026-10-20T08:00:00+00:00 was due. Restore points would look older than "
    "they are, so retention deletes nothing until an Administrator confirms the time is right "
    "(abr system clock confirm)."
)
BLOCKED = {"blocked": True, "server_time": "2027-11-24T08:00:00+00:00", "source": "running", "message": MESSAGE}
CONFIRMED = {"blocked": False, "server_time": "2027-11-24T08:01:00+00:00", "confirmed_by": "admin", "was_blocked": True}


def _retention(clock_blocked: str | None, *, dry_run: bool) -> dict:
    chain = {
        "vm_id": "vm-1",
        "vm_name": "demo-vm",
        "repository_name": "Local",
        "points": 3,
        "removed": ["full-old", "incr-old"] if dry_run or not clock_blocked else [],
        "held": [],
        "failed": [],
        "kept_for_chain": [],
        "skipped_reason": clock_blocked if not dry_run else None,
        "clock_blocked": clock_blocked,
    }
    return {
        "job_id": "job-1",
        "job_name": "nightly",
        "dry_run": dry_run,
        "clock_blocked": clock_blocked,
        "retention_days": 7,
        "gfs": {},
        "summary": {"chains": 1, "points": 3, "removed": len(chain["removed"]), "held": 0, "failed": 0},
        "chains": [chain],
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


class SystemClockCliTests(unittest.TestCase):
    def _run(self, payload: dict, *argv: str, output: str = "table") -> tuple[int, str, str, list]:
        args = AltaStorParserBuilder().build().parse_args(["--base-url", "http://example.test", "--output", output, *argv])
        calls = []

        def fake_request(method, url, **kwargs):
            calls.append((method, url))
            return _FakeResponse(payload)

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("python.cli.api.client.requests.request", side_effect=fake_request), redirect_stdout(
            stdout
        ), redirect_stderr(stderr):
            code = args.handler(args)
        return code, stdout.getvalue(), stderr.getvalue(), calls

    def test_show_names_the_block_and_exits_with_invalid_state(self) -> None:
        code, stdout, _stderr, calls = self._run(BLOCKED, "system", "clock", "show")

        self.assertEqual(code, INVALID_STATE)
        self.assertIn("Retention is blocked", stdout)
        self.assertTrue(calls[0][1].endswith("/api/v1/system/clock"))

    def test_show_exits_0_on_a_trusted_clock(self) -> None:
        code, _stdout, _stderr, _calls = self._run({"blocked": False, "message": None}, "system", "clock", "show")

        self.assertEqual(code, 0)

    def test_confirm_posts_and_says_retention_deletes_again(self) -> None:
        code, stdout, _stderr, calls = self._run(CONFIRMED, "system", "clock", "confirm")

        self.assertEqual(code, 0)
        self.assertEqual(calls[0][0].upper(), "POST")
        self.assertTrue(calls[0][1].endswith("/api/v1/system/clock/confirm"))
        self.assertIn("retention deletes expired restore points again", stdout)

    def test_confirm_json_keeps_the_api_fields(self) -> None:
        _code, stdout, _stderr, _calls = self._run(CONFIRMED, "system", "clock", "confirm", output="json")

        self.assertTrue(json.loads(stdout)["was_blocked"])


class JobRetentionClockCliTests(SystemClockCliTests):
    def test_a_blocked_run_marks_the_chain_and_exits_with_invalid_state(self) -> None:
        code, stdout, stderr, _calls = self._run(_retention(MESSAGE, dry_run=False), "job", "retention", "--id", "job-1")

        self.assertEqual(code, INVALID_STATE)
        self.assertIn("skipped: server clock jumped forward", stdout)
        self.assertIn(f"Error: {MESSAGE}", stderr)

    def test_a_blocked_dry_run_previews_and_warns(self) -> None:
        code, _stdout, stderr, _calls = self._run(
            _retention(MESSAGE, dry_run=True), "job", "retention", "--id", "job-1", "--dry-run"
        )

        self.assertEqual(code, 0)
        self.assertIn("2 of 3 restore point(s) would remove", stderr)
        self.assertIn(f"Warning: {MESSAGE}", stderr)

    def test_a_run_on_a_trusted_clock_is_unchanged(self) -> None:
        code, _stdout, stderr, _calls = self._run(_retention(None, dry_run=False), "job", "retention", "--id", "job-1")

        self.assertEqual(code, 0)
        self.assertNotIn("blocked", stderr)


if __name__ == "__main__":
    unittest.main()
