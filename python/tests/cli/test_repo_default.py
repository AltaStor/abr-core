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
from python.cli.exit_codes import INVALID_STATE, PERMISSION_DENIED
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

    def test_a_server_without_the_shares_broker_exits_non_zero_with_the_reason(self) -> None:
        code, stderr = self._fail(
            503,
            "repository_share_unavailable",
            "The share of Default Repo could not be created: the shares broker could not be reached: nothing listens on "
            "/run/altastor-backup-shares.sock. It is the altastor-backup-shares.socket unit of the altastor-backup-server package",
        )

        self.assertNotEqual(code, 0)
        self.assertIn("altastor-backup-shares.socket", stderr)

    def test_a_share_samba_refuses_exits_with_invalid_state_and_the_reason(self) -> None:
        code, stderr = self._fail(409, "repository_share_refused", "The share of Default Repo could not be created: smbd is not running")

        self.assertEqual(code, INVALID_STATE)
        self.assertIn("smbd is not running", stderr)

    def test_a_folder_that_cannot_be_created_exits_non_zero_with_the_reason(self) -> None:
        code, stderr = self._fail(
            422,
            "default_repository_path_unavailable",
            "The folder of the Default Repo, /var/lib/altastor-backup/repository, could not be created: Permission denied",
        )

        self.assertNotEqual(code, 0)
        self.assertIn("could not be created: Permission denied", stderr)



def _share(*, shared: bool) -> dict[str, object]:
    return {
        "id": "repo-1",
        "name": "Local-Repo-1",
        "path": "/mnt/backup/repo1",
        "default": False,
        "shared": shared,
        "network_path": "\\\\backup01\\abr-k3j9x2qa$" if shared else None,
        "share": "abr-k3j9x2qa$" if shared else None,
        "username": "abr-smb-p4m8z1wt" if shared else None,
    }


class RepoShareCommandTests(unittest.TestCase):
    def _run(self, argv: list[str], answers: dict[tuple[str, str], tuple[int, object]], output: str = "table") -> tuple[int, list, str, str]:
        sent: list[tuple[str, str]] = []

        def fake_request(method, url, json=None, **kwargs):
            path = url.split("/api/v1/", 1)[1]
            sent.append((method, path))
            status, body = answers[(method, path)]
            return _response(status, body)

        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("python.cli.api.client.requests.request", side_effect=fake_request),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            try:
                code = abr_cli.main(["--base-url", "http://example.test", "--output", output, "repo", "share", *argv])
            except SystemExit as exc:
                code = exc.code
        return code, sent, stdout.getvalue(), stderr.getvalue()

    def lookup(self) -> dict[tuple[str, str], tuple[int, object]]:
        return {("GET", "repositories"): (200, {"items": [{"id": "repo-1", "name": "Local-Repo-1"}]})}

    def test_show_lists_the_shares(self) -> None:
        code, sent, stdout, _stderr = self._run(["show"], {("GET", "repositories/shares"): (200, {"items": [_share(shared=True), {**_share(shared=False), "id": "repo-2", "name": "Spare"}]})})

        self.assertEqual((code, sent), (0, [("GET", "repositories/shares")]))
        self.assertIn("NETWORK PATH", stdout)
        self.assertIn("abr-smb-p4m8z1wt", stdout)
        self.assertIn("Spare", stdout)

    def test_add_finds_the_repository_by_name_and_shows_the_share_without_a_password(self) -> None:
        answers = {**self.lookup(), ("POST", "repositories/repo-1/share"): (200, _share(shared=True))}

        code, sent, stdout, stderr = self._run(["add", "--name", "local-repo-1"], answers, output="json")

        self.assertEqual((code, sent[-1]), (0, ("POST", "repositories/repo-1/share")))
        self.assertEqual(json.loads(stdout)["share"], "abr-k3j9x2qa$")
        self.assertNotIn("password", stdout)
        self.assertIn("is shared as \\\\backup01\\abr-k3j9x2qa$, account abr-smb-p4m8z1wt", stderr)

    def test_del_and_check_go_to_their_routes(self) -> None:
        answers = {
            **self.lookup(),
            ("DELETE", "repositories/repo-1/share"): (200, _share(shared=False)),
            ("POST", "repositories/repo-1/share/check"): (200, {**_share(shared=True), "checked": True, "message": "write, read back and delete passed"}),
        }

        removed = self._run(["del", "--id", "repo-1"], answers)
        checked = self._run(["check", "--name", "Local-Repo-1"], answers)

        self.assertEqual((removed[0], removed[1]), (0, [("DELETE", "repositories/repo-1/share")]))
        self.assertIn("not shared any more; its backups stay", removed[3])
        self.assertEqual((checked[0], checked[1][-1]), (0, ("POST", "repositories/repo-1/share/check")))
        self.assertIn("write, read back and delete passed", checked[2])

    def test_refusals_exit_with_their_codes(self) -> None:
        for status, error_code, exit_code in ((409, "repository_share_busy", INVALID_STATE), (403, "permission_denied", PERMISSION_DENIED)):
            with self.subTest(error_code):
                answers = {**self.lookup(), ("POST", "repositories/repo-1/share"): (status, {"error": {"code": error_code, "message": f"{error_code} here"}})}
                code, _sent, _stdout, stderr = self._run(["add", "--name", "Local-Repo-1"], answers)
                self.assertEqual(code, exit_code)
                self.assertIn(f"{error_code} here", stderr)

    def test_an_unknown_repository_name_is_refused_before_any_change(self) -> None:
        code, sent, _stdout, stderr = self._run(["add", "--name", "Nope"], self.lookup())

        self.assertNotEqual(code, 0)
        self.assertEqual(sent, [("GET", "repositories")])
        self.assertIn("Repository not found: Nope", stderr)

    def test_the_commands_carry_their_functions(self) -> None:
        from python.cli.command_functions import COMMAND_FUNCTIONS

        self.assertEqual(
            {name: COMMAND_FUNCTIONS[name] for name in COMMAND_FUNCTIONS if name.startswith("repo share")},
            {"repo share show": "repository.view", "repo share add": "repository.edit", "repo share del": "repository.edit", "repo share check": "repository.verify"},
        )


if __name__ == "__main__":
    unittest.main()
