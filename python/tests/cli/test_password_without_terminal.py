# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import getpass
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from python.cli.commands.auth_commands import PASSWORD_ENV_VAR, AuthCommands
from python.cli.commands.user_commands import UserCommands
from python.cli.errors import CliStructuredError
from python.tests.cli.test_auth_user import _args, _FakeResponse, _IsolatedConfigDirCase

STDIN = "run the command in a terminal, or give the password on its stdin (one line)"


def _no_terminal(prompt: str) -> str:
    return getpass.fallback_getpass(prompt)


class PasswordWithoutTerminalTests(_IsolatedConfigDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(patch("getpass.getpass", side_effect=_no_terminal))
        self.request = self.enterContext(patch("python.cli.api.client.requests.request"))
        environ = dict(os.environ)
        environ.pop(PASSWORD_ENV_VAR, None)
        self.enterContext(patch.dict(os.environ, environ, clear=True))

    def _stdin(self, text: str) -> None:
        self.enterContext(patch("sys.stdin", io.StringIO(text)))

    def _refused(self, call) -> CliStructuredError:
        with self.assertRaises(CliStructuredError) as raised, redirect_stdout(io.StringIO()):
            call()
        self.assertEqual(raised.exception.exit_code, 2)
        return raised.exception

    def test_user_add_without_a_password_on_stdin_says_how_to_give_it(self) -> None:
        self._stdin("")
        error = self._refused(lambda: UserCommands().add(_args(name="alice", full_name="", role="auditor")))

        self.assertEqual(str(error), f"No password for the new user: {STDIN}")
        self.request.assert_not_called()

    def test_user_edit_reset_password_without_a_password_on_stdin_says_how_to_give_it(self) -> None:
        self._stdin("")
        self.request.return_value = _FakeResponse({"items": [{"id": "u2", "name": "alice"}]})
        args = _args(name="alice", id=None, new_name=None, full_name=None, role=None, enabled=None, reset_password=True)

        error = self._refused(lambda: UserCommands().edit(args))

        self.assertEqual(str(error), f"No new password for the user: {STDIN}")
        self.assertEqual([call.args[0] for call in self.request.call_args_list], ["GET"])

    def test_change_password_without_either_password_names_the_variable_and_stdin(self) -> None:
        self._stdin("")
        error = self._refused(lambda: AuthCommands().change_password(_args()))

        self.assertEqual(
            str(error),
            "No current password: set ABR_PASSWORD, run the command in a terminal, or give the current and the new "
            "password on its stdin (one line each)",
        )
        self.request.assert_not_called()

    def test_change_password_with_the_current_one_in_the_variable_still_needs_the_new_one(self) -> None:
        self._stdin("")
        with patch.dict(os.environ, {PASSWORD_ENV_VAR: "CurrentPassw0rd"}):
            error = self._refused(lambda: AuthCommands().change_password(_args()))

        self.assertEqual(str(error), f"No new password: {STDIN}")
        self.request.assert_not_called()

    def test_a_too_short_password_on_stdin_is_refused_instead_of_asked_again(self) -> None:
        self._stdin("short\n")
        error = self._refused(lambda: UserCommands().add(_args(name="alice", full_name="", role="auditor")))

        self.assertEqual(str(error), "The password must be at least 8 characters")
        self.request.assert_not_called()

    def test_passwords_on_stdin_are_taken_one_line_each(self) -> None:
        self.request.return_value = _FakeResponse({"id": "u2", "name": "alice"})
        self._stdin("Passw0rd-new\n")
        with redirect_stdout(io.StringIO()):
            UserCommands().add(_args(name="alice", full_name="", role="auditor"))
        self.assertEqual(self.request.call_args.kwargs["json"]["password"], "Passw0rd-new")

        self._stdin("CurrentPassw0rd\nNewPassw0rd1\n")
        with redirect_stdout(io.StringIO()):
            AuthCommands().change_password(_args())
        self.assertEqual(
            self.request.call_args.kwargs["json"], {"current_password": "CurrentPassw0rd", "new_password": "NewPassw0rd1"}
        )

    def test_the_cli_prints_one_error_line_and_no_traceback(self) -> None:
        from python.cli import abr_cli

        self._stdin("")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit) as exited:
            abr_cli.main(["--base-url", "http://example.test", "--output", "json", "user", "add", "--name", "alice", "--role", "auditor"])

        self.assertEqual(exited.exception.code, 2)
        error = json.loads(err.getvalue()[err.getvalue().index("{"):])["error"]
        self.assertEqual((error["code"], error["message"]), ("invalid_argument", f"No password for the new user: {STDIN}"))
        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn("EOFError", out.getvalue() + err.getvalue())
