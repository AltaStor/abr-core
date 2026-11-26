# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from python.cli.errors import CliStructuredError
from python.cli.exit_codes import INVALID_USAGE
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

    def test_inventory_edit_sets_or_resets_the_api_and_the_vddk_library(self) -> None:
        edit = ["inventory", "edit", "--id", "inv-1"]
        self.assertEqual(
            self._sent([*edit, "--api", "vddk", "--vddk-lib", "/opt/vddk/lib64/libvixDiskLib.so.9.1.0.0"], "patch"),
            {"api": "vddk", "vddk_library": "/opt/vddk/lib64/libvixDiskLib.so.9.1.0.0"},
        )
        self.assertEqual(self._sent([*edit, "--reset-api", "--reset-vddk-lib"], "patch"), {"api": "inherit", "vddk_library": "inherit"})
        for conflicting in (["--api", "soap", "--reset-api"], ["--api", "soap", "--transport", "soap"], ["--vddk-lib", "/x.so", "--reset-vddk-lib"]):
            with self.subTest(conflicting=conflicting), self.assertRaises(SystemExit), patch("sys.stderr"):
                build_parser().parse_args([*edit, *conflicting])


FOUND = {
    "api": "vddk",
    "vddk_library": None,
    "effective_vddk_library": "/opt/altastor-backup/tools/vddk/lib64/libvixDiskLib.so",
    "default_vddk_library": "/opt/altastor-backup/tools/vddk/lib64/libvixDiskLib.so",
    "detected_vddk_libraries": [
        {"path": "/opt/altastor-backup/tools/librevddk/lib64/libvixDiskLib.so", "kind": "librevddk"},
        {"path": "/opt/vmware-vix-disklib/lib64/libvixDiskLib.so", "kind": "vmware_vddk"},
    ],
    "hypervisors": [],
}


class SettingsVsphereCliTests(unittest.TestCase):
    SETTINGS = {"api": "vddk", "hypervisors": [{"id": "h1", "name": "esxi-edge", "has_own_settings": True}, {"id": "h2", "name": "vc", "has_own_settings": False}]}

    def _run(self, *argv: str, tty: bool = False, answer: str = "", answers: list[str] | None = None, settings: dict | None = None) -> tuple[int, object]:
        args = build_parser().parse_args(["--base-url", "http://127.0.0.1:8000", "--output", "json", "settings", "vsphere", "set", *argv])
        self.printed = io.StringIO()
        with (
            patch("python.cli.api.client.AltaStorApiClient.get", return_value=settings or self.SETTINGS),
            patch("python.cli.api.client.AltaStorApiClient.put", return_value={"api": "soap", "updated_hypervisors": [{"id": "h1", "name": "esxi-edge"}]}) as put,
            patch("sys.stdin.isatty", return_value=tty),
            patch("builtins.input", side_effect=answers) if answers is not None else patch("builtins.input", return_value=answer),
            redirect_stdout(self.printed),
        ):
            try:
                code = args.handler(args)
            except CliStructuredError as exc:
                return exc.exit_code, exc
        return code, put.call_args.kwargs["json"]

    def test_set_sends_the_api_and_a_library_or_default(self) -> None:
        self.assertEqual(self._run("--api", "soap"), (0, {"api": "soap"}))
        self.assertEqual(self._run("--vddk-lib", "default"), (0, {"vddk_library": None}))

    def test_updating_existing_hypervisors_asks_first_unless_yes(self) -> None:
        self.assertEqual(self._run("--api", "soap", "--update-existing-hypervisors", "--yes")[1], {"api": "soap", "apply_to_existing_hypervisors": True})
        code, refused = self._run("--api", "soap", "--update-existing-hypervisors")
        self.assertEqual(code, INVALID_USAGE)
        self.assertIn("esxi-edge", str(refused))
        self.assertIn("--yes", str(refused))
        self.assertEqual(self._run("--update-existing-hypervisors", tty=True, answer="y"), (0, {"apply_to_existing_hypervisors": True}))
        self.assertEqual(self._run("--update-existing-hypervisors", tty=True, answer="n")[0], INVALID_USAGE)

    def test_set_without_a_change_is_refused_even_in_a_terminal(self) -> None:
        for tty in (False, True):
            with self.subTest(tty=tty):
                code, refused = self._run(tty=tty, answers=[])
                self.assertEqual(code, INVALID_USAGE)
                self.assertIn("Give --api, --vddk-lib or --update-existing-hypervisors", str(refused))

    def test_librevddk_names_the_same_library(self) -> None:
        self.assertEqual(self._run("--vddk-lib", "librevddk", settings=FOUND), (0, {"vddk_library": "/opt/altastor-backup/tools/librevddk/lib64/libvixDiskLib.so"}))
        code, refused = self._run("--vddk-lib", "librevddk", settings={**FOUND, "detected_vddk_libraries": []})
        self.assertEqual(code, INVALID_USAGE)
        self.assertIn("LibreVDDK is not installed", str(refused))

    def test_the_old_word_ovddk_is_not_a_library_name_any_more(self) -> None:
        self.assertEqual(self._run("--vddk-lib", "ovddk", settings=FOUND), (0, {"vddk_library": "ovddk"}))


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
        self.assertIn("--api {vddk,soap}", inventory)
        self.assertIn("without it the inventory uses the server-wide API", inventory)
        self.assertIn("--transport {vddk,soap}", inventory)
        self.assertIn("--vddk-lib PATH", inventory)
        job = self._help("job", "create")
        self.assertIn("--transport {vddk,soap,inventory}", job)
        self.assertIn("inventory (the default) uses it", job)

    def test_the_value_ovddk_is_explained_as_the_librevddk_library(self) -> None:
        for argv in (("settings", "vsphere", "set"), ("inventory", "add"), ("inventory", "edit")):
            with self.subTest(command=" ".join(argv)):
                text = " ".join(self._help(*argv).split())
                self.assertIn("librevddk", text)
                self.assertIn("LibreVDDK library", text)
                self.assertNotIn("ovddk", text.lower())


if __name__ == "__main__":
    unittest.main()
