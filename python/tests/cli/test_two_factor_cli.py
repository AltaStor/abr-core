# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""Signing in with two-factor from the CLI, against the real API (in memory)."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pyotp

from python.cli.command_functions import COMMAND_FUNCTIONS
from python.cli.commands.auth_commands import PASSWORD_ENV_VAR
from python.cli.exit_codes import INVALID_USAGE, UNAUTHORIZED
from python.srv.core.mongo_db_wrapper import USERS
from python.srv.services import two_factor
from python.tests.cli.test_rbac_cli import CliApiCase
from python.tests.srv.rbac.rbac_case import ADMIN_PASSWORD

PROMPT = "python.cli.commands.auth_commands.getpass.getpass"


class TwoFactorCliTests(CliApiCase):
    def setUp(self) -> None:
        clock = patch.object(two_factor, "_now", return_value=datetime.now(timezone.utc))
        clock.start()
        self.addCleanup(clock.stop)
        super().setUp()
        logs = tempfile.TemporaryDirectory()
        self.addCleanup(logs.cleanup)
        self.log_dir = Path(logs.name)
        env = patch.dict(os.environ, {"ALTASTOR_LOG_DIR": logs.name})
        env.start()
        self.addCleanup(env.stop)
        admin_token = self.login("admin", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {admin_token}"}
        setup = self.client.post("/api/v1/auth/totp/setup", json={"password": ADMIN_PASSWORD}, headers=headers).json()
        self.secret = setup["secret"]
        enabled = self.client.post("/api/v1/auth/totp/enable", json={"totp_code": self._code()}, headers=headers)
        self.assertEqual(enabled.status_code, 200, enabled.text)
        self.recovery_codes = enabled.json()["recovery_codes"]
        # Turning it on ended the administrator's other sessions.
        self.tokens["administrator"] = admin_token

    def _code(self, steps: int = 0) -> str:
        return pyotp.TOTP(self.secret).at(two_factor._now() + timedelta(seconds=30 * steps))

    def _login(self, *answers: str):
        """``abr auth login`` with the password set and each prompt for a code
        answered in turn."""
        replies = iter(answers)

        def prompt(text: str) -> str:
            try:
                return next(replies)
            except StopIteration:
                raise EOFError from None

        with patch.dict(os.environ, {PASSWORD_ENV_VAR: ADMIN_PASSWORD}), patch(PROMPT, side_effect=prompt) as asked:
            code, out, err = self.run_cli("administrator", "auth", "login", "--name", "admin")
        return code, out, err, [call.args[0] for call in asked.call_args_list]

    def test_a_valid_code_at_the_prompt_signs_in(self) -> None:
        code, out, err, prompts = self._login(self._code(1))
        self.assertEqual(code, 0, err)
        self.assertIn("Logged in as admin", out)
        self.assertEqual(prompts, ["Authentication code: "])

    def test_spaces_in_the_code_are_fine(self) -> None:
        typed = self._code(1)
        self.assertEqual(self._login(f" {typed[:3]} {typed[3:]} ")[0], 0)

    def test_an_invalid_code_is_rejected_with_the_unauthorized_exit_code(self) -> None:
        wrong = f"{(int(self._code(1)) + 1) % 1_000_000:06d}"
        code, _out, err, _ = self._login(wrong)
        self.assertEqual(code, UNAUTHORIZED)
        self.assertIn("isn't valid", err)
        self.assertNotIn(wrong, err)

    def test_an_expired_code_says_so(self) -> None:
        code, _out, err, _ = self._login(self._code(-3))
        self.assertEqual(code, UNAUTHORIZED)
        self.assertIn("has expired", err)

    def test_a_used_code_says_so(self) -> None:
        self.assertEqual(self._login(self._code(1))[0], 0)
        code, _out, err, _ = self._login(self._code(1))
        self.assertEqual(code, UNAUTHORIZED)
        self.assertIn("already used", err)

    def test_a_recovery_code_is_not_taken_and_the_prompt_says_where_it_is(self) -> None:
        code, _out, err, prompts = self._login(self.recovery_codes[0], self._code(1))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(prompts), 2)
        self.assertIn("Recovery codes are accepted only in the Web UI", err)
        self.assertIn(two_factor.recovery_digest(self._admin_id(), self.recovery_codes[0]), self._stored_digests())

    def test_without_a_terminal_it_says_what_is_missing(self) -> None:
        code, _out, err, _ = self._login()
        self.assertEqual(code, INVALID_USAGE)
        self.assertIn("in a terminal", err)

    def test_print_token_still_prints_only_the_token(self) -> None:
        with patch.dict(os.environ, {PASSWORD_ENV_VAR: ADMIN_PASSWORD}), patch(PROMPT, return_value=self._code(1)):
            code, out, err = self.run_cli("administrator", "auth", "login", "--name", "admin", "--print-token")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.split()), 1)

    def test_the_code_reaches_no_log_file_and_no_output(self) -> None:
        typed = self._code(1)
        wrong = f"{(int(typed) + 1) % 1_000_000:06d}"
        _, out_wrong, err_wrong, _ = self._login(wrong)
        _, out, err, _ = self._login(typed)
        # The CLI's own log file, at its DEBUG level, with the in-process
        # server's records too.
        logged = "\n".join(path.read_text(errors="replace") for path in self.log_dir.rglob("*") if path.is_file())
        self.assertIn("CLI session started", logged)
        for text in (logged, out_wrong, err_wrong, out, err):
            self.assertNotIn(typed, text)
            self.assertNotIn(wrong, text)

    def test_there_is_no_command_or_variable_to_pass_a_code_or_manage_two_factor(self) -> None:
        self.assertFalse([command for command in COMMAND_FUNCTIONS if "two-factor" in command or "totp" in command])
        code, _out, _err = self.run_cli("administrator", "user", "edit", "--name", "olga", "--reset-two-factor")
        self.assertEqual(code, INVALID_USAGE)

    def test_whoami_says_whether_it_is_on(self) -> None:
        self.assertIn("Two-factor: on, 10 recovery codes left", self.run_cli("administrator", "auth", "whoami")[1])

    def test_the_user_list_shows_who_has_it(self) -> None:
        code, out, err = self.run_cli("administrator", "user", "list")
        self.assertEqual(code, 0, err)
        self.assertIn("TWO-FACTOR", out)

    def _admin_id(self) -> str:
        return next(user["id"] for user in USERS.values() if user["name"] == "admin")

    def _stored_digests(self) -> list[str]:
        return USERS[self._admin_id()]["totp_recovery"]
