# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from python.cli.parsing.parser_builder import build_parser
from python.cli.services.job_service import JobService

BACKUP_JOB = {"name": "Nightly", "job_type": "backup"}
COPY_JOB = {"name": "Offsite", "job_type": "backup-copy"}
REPLICATION_JOB = {"name": "DR", "job_type": "replication"}
FULL = ["--active-full-schedule", "weekly", "--active-full-days", "sun", "--active-full-time", "02:00"]


class InventoryTransportCliTests(unittest.TestCase):
    def _sent(self, argv: list[str], method: str) -> dict:
        args = build_parser().parse_args(["--base-url", "http://127.0.0.1:8000", "--output", "json", *argv])
        with (
            patch(f"python.cli.api.client.AltaStorApiClient.{method}", return_value={"id": "inv-1"}) as call,
            redirect_stdout(io.StringIO()),
        ):
            args.handler(args)
        return call.call_args.kwargs["json"]

    def test_inventory_add_sends_the_transport_only_when_it_is_chosen(self) -> None:
        base = ["inventory", "add", "--name", "vc", "--type", "vsphere", "--address", "192.0.2.10", "--username", "u", "--password", "p"]

        self.assertEqual(self._sent([*base, "--transport", "soap"], "post")["transport"], "soap")
        self.assertNotIn("transport", self._sent(base, "post"))

    def test_inventory_edit_sends_the_transport_only_when_it_is_chosen(self) -> None:
        self.assertEqual(self._sent(["inventory", "edit", "--id", "inv-1", "--transport", "vddk"], "patch"), {"transport": "vddk"})
        self.assertNotIn("transport", self._sent(["inventory", "edit", "--id", "inv-1", "--new-name", "x"], "patch"))

    def test_an_unknown_transport_is_refused_before_anything_is_sent(self) -> None:
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            build_parser().parse_args(["inventory", "edit", "--id", "inv-1", "--transport", "nbd"])


class JobTransportCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = build_parser()
        self.jobs = JobService(None)

    def _create(self, *argv: str) -> dict:
        args = self.parser.parse_args(["job", "create", "--name", "Nightly", "--vm", "web", "--inventory", "vc", "--repository", "repo", *FULL, *argv])
        return self.jobs.create_backup_job_payload(args)

    def _edit(self, job: dict, *argv: str) -> dict:
        return self.jobs.build_edit_payload(job, self.parser.parse_args(["job", "edit", "--name", job["name"], *argv]))

    def test_job_create_sends_an_override_only_when_one_is_chosen(self) -> None:
        self.assertEqual(self._create("--transport", "soap")["task_parameters"]["vsphere_transport"], "soap")
        self.assertNotIn("vsphere_transport", self._create().get("task_parameters") or {})

    def test_job_edit_sets_an_override_or_hands_the_choice_back_to_the_inventory(self) -> None:
        self.assertEqual(self._edit(BACKUP_JOB, "--transport", "soap")["task_parameters"]["vsphere_transport"], "soap")
        self.assertEqual(self._edit(BACKUP_JOB, "--transport", "inventory")["task_parameters"]["vsphere_transport"], "inventory")

    def test_the_override_is_for_backup_jobs_only(self) -> None:
        for job in (COPY_JOB, REPLICATION_JOB):
            with self.subTest(job=job["job_type"]), self.assertRaisesRegex(ValueError, "--transport apply to backup jobs only"):
                self._edit(job, "--transport", "soap")
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            self.parser.parse_args(["job", "create-backup-copy", "--name", "x", "--transport", "soap"])


class TransportErrorExitTests(unittest.TestCase):
    def _exit_code(self, status: int, code: str) -> int:
        import json

        import requests

        from python.cli import abr_cli

        response = requests.Response()
        response.status_code = status
        response._content = json.dumps({"error": {"code": code, "message": "The VDDK transport is not available"}}).encode()
        response.headers["Content-Type"] = "application/json"
        with (
            patch("python.cli.api.client.requests.request", return_value=response),
            patch("sys.stderr", new_callable=io.StringIO),
            self.assertRaises(SystemExit) as caught,
        ):
            abr_cli.main(["--base-url", "http://example.test", "inventory", "edit", "--id", "inv-1", "--transport", "vddk"])
        return caught.exception.code

    def test_a_transport_the_server_cannot_use_exits_as_an_invalid_state(self) -> None:
        self.assertEqual(self._exit_code(503, "transport_unavailable"), 4)

    def test_a_wrong_value_from_the_server_exits_as_invalid_usage(self) -> None:
        self.assertEqual(self._exit_code(422, "invalid_inventory"), 2)

class TransportHelpTests(unittest.TestCase):
    def _help(self, *argv: str) -> str:
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            build_parser().parse_args([*argv, "--help"])
        return out.getvalue()

    def test_help_names_the_choices_and_what_they_mean(self) -> None:
        inventory = self._help("inventory", "add")
        self.assertIn("--transport {vddk,soap}", inventory)
        self.assertIn("the VMware VDDK, the default", inventory)
        job = self._help("job", "create")
        self.assertIn("--transport {vddk,soap,inventory}", job)
        self.assertIn("inventory (the default) uses it", job)


if __name__ == "__main__":
    unittest.main()
