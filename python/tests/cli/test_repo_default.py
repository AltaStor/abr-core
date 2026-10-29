# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import requests

from python.cli import abr_cli
from python.cli.parsing.parser_builder import AltaStorParserBuilder

PASSWORD_MASK = "********"


def _response(status_code: int, payload: dict[str, object]) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response._content = json.dumps(payload).encode("utf-8")
    response.headers["Content-Type"] = "application/json"
    return response


def _repository(*, hyperv: bool) -> dict[str, object]:
    return {
        "id": "repo-default",
        "name": "Default Repo",
        "type": "smb" if hyperv else "local",
        "path": "/var/lib/altastor-backup/repository",
        "status": "available",
        "default": True,
        "support_hyperv": hyperv,
        "share": "abr-k3j9x2qa$" if hyperv else None,
        "username": "abr-smb-p4m8z1wt" if hyperv else None,
        "metadata": {"password": PASSWORD_MASK if hyperv else None},
    }


class RepoDefaultCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = AltaStorParserBuilder().build()

    def _run(self, argv: list[str], answer: dict[str, object], output: str = "table") -> tuple[dict, str, str]:
        args = self.parser.parse_args(["--base-url", "http://example.test", "--output", output, "repo", "default", *argv])
        sent: list[tuple[str, str, dict]] = []

        def fake_request(method, url, json=None, **kwargs):
            sent.append((method, url, json))
            return _response(200, answer)

        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("python.cli.api.client.requests.request", side_effect=fake_request),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(args.handler(args), 0)
        [(method, url, body)] = sent
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/api/v1/repositories/default"))
        return body, stdout.getvalue(), stderr.getvalue()

    def test_without_an_option_it_only_asks_for_the_default_repository(self) -> None:
        body, stdout, stderr = self._run([], {"created": True, "changed": False, "repository": _repository(hyperv=False)})

        self.assertEqual(body, {})
        self.assertIn("/var/lib/altastor-backup/repository", stdout)
        self.assertIn("Created Default Repo.", stderr)

    def test_an_existing_default_repository_is_reported_as_unchanged(self) -> None:
        _body, _stdout, stderr = self._run([], {"created": False, "changed": False, "repository": _repository(hyperv=False)})

        self.assertIn("Default Repo already exists; nothing changed.", stderr)

    def test_support_hyperv_sends_the_switch_and_shows_the_share_but_no_password(self) -> None:
        answer = {"created": False, "changed": True, "repository": _repository(hyperv=True)}

        body, stdout, stderr = self._run(["--support-hyperv"], answer, output="json")

        self.assertEqual(body, {"support_hyperv": True})
        shown = json.loads(stdout)
        self.assertEqual(shown["support_hyperv"], True)
        self.assertEqual(shown["share"], "abr-k3j9x2qa$")
        self.assertEqual(shown["share_user"], "abr-smb-p4m8z1wt")
        self.assertEqual(shown["path"], "/var/lib/altastor-backup/repository")
        self.assertNotIn("password", stdout)
        self.assertNotIn(PASSWORD_MASK, stdout)
        self.assertIn("Hyper-V support of Default Repo turned on.", stderr)

    def test_no_support_hyperv_turns_it_off(self) -> None:
        body, stdout, stderr = self._run(
            ["--no-support-hyperv"], {"created": False, "changed": True, "repository": _repository(hyperv=False)}
        )

        self.assertEqual(body, {"support_hyperv": False})
        self.assertNotIn("abr-k3j9x2qa$", stdout)
        self.assertIn("turned off", stderr)

    def test_the_two_switches_cannot_be_given_together(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parser.parse_args(["repo", "default", "--support-hyperv", "--no-support-hyperv"])


class RepoDefaultErrorTests(unittest.TestCase):
    def _fail(self, status: int, code: str, message: str) -> tuple[int, str]:
        stderr = io.StringIO()
        with (
            patch(
                "python.cli.api.client.requests.request",
                return_value=_response(status, {"error": {"code": code, "message": message}}),
            ),
            redirect_stderr(stderr),
            redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            abr_cli.main(["--base-url", "http://example.test", "repo", "default", "--support-hyperv"])
        return caught.exception.code, stderr.getvalue()

    def test_a_server_that_may_not_create_the_share_exits_non_zero_with_the_reason(self) -> None:
        code, stderr = self._fail(
            503,
            "hyperv_share_not_permitted",
            "The Hyper-V share of the Default Repo could not be created: this server may not run "
            "/opt/altastor-backup/python/srv/plugins/hyperv/smb_share/abr-samba-helper.sh as root",
        )

        self.assertNotEqual(code, 0)
        self.assertIn("may not run /opt/altastor-backup/python/srv/plugins/hyperv/smb_share/abr-samba-helper.sh as root", stderr)

    def test_a_share_samba_refuses_exits_non_zero_with_the_reason(self) -> None:
        code, stderr = self._fail(500, "hyperv_share_failed", "Samba is not installed (/etc/samba/smb.conf is missing)")

        self.assertNotEqual(code, 0)
        self.assertIn("Samba is not installed", stderr)

    def test_a_folder_that_cannot_be_created_exits_non_zero_with_the_reason(self) -> None:
        code, stderr = self._fail(
            422,
            "default_repository_path_unavailable",
            "The folder of the Default Repo, /var/lib/altastor-backup/repository, could not be created: Permission denied",
        )

        self.assertNotEqual(code, 0)
        self.assertIn("could not be created: Permission denied", stderr)


if __name__ == "__main__":
    unittest.main()
