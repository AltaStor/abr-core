# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""The CLI against the real API (in memory): a command the role does not
allow exits with PERMISSION_DENIED, says why, and is audited as a CLI
command."""

from __future__ import annotations

import io
import json
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

from python.cli import token_store
from python.cli.abr_cli import main
from python.cli.cli_context import TOKEN_ENV_VAR
from python.cli.command_functions import COMMAND_FUNCTIONS
from python.cli.exit_codes import PERMISSION_DENIED
from python.srv.core.mongo_db_wrapper import CUSTOM_ROLES, JOBS, USERS
from python.srv.services import rbac
from python.tests.srv.rbac.rbac_case import RbacApiCase

BASE_URL = "http://abr.test"


class CliPermissionTests(RbacApiCase):
    def setUp(self) -> None:
        super().setUp()
        config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(config_dir.cleanup)
        env = patch.dict(os.environ, {token_store.CONFIG_DIR_ENV_VAR: config_dir.name})
        env.start()
        self.addCleanup(env.stop)
        bridge = patch("python.cli.api.client.requests.request", side_effect=self._forward)
        bridge.start()
        self.addCleanup(bridge.stop)
        JOBS["job-1"] = {"id": "job-1", "name": "nightly"}

    def _forward(self, method, url, *, json=None, params=None, headers=None, **_kwargs):
        """Send the CLI's request to the in-process API and answer it as
        requests would."""
        served = self.client.request(method, urlsplit(url).path, json=json, params=params, headers=headers)
        response = requests.Response()
        response.status_code = served.status_code
        response._content = served.content
        response.headers.update(served.headers)
        response.url = url
        response.request = requests.Request(method, url).prepare()
        return response

    def run_cli(self, role: str, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {TOKEN_ENV_VAR: self.tokens[role]}):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    code = main(["--base-url", BASE_URL, *argv])
                except SystemExit as exit_:
                    code = int(exit_.code or 0)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_a_denied_command_exits_with_permission_denied_and_a_json_error(self) -> None:
        code, _out, err = self.run_cli(rbac.AUDITOR, "--output", "json", "job", "delete", "--id", "job-1")
        self.assertEqual(code, PERMISSION_DENIED)
        error = json.loads(err[err.index("{"):])["error"]
        self.assertEqual(error["code"], "permission_denied")
        self.assertEqual(error["details"]["function_id"], "job.delete")
        self.assertEqual(error["details"]["role"], rbac.AUDITOR)
        self.assertIn("job-1", JOBS)

    def test_a_denied_command_says_why_in_a_table(self) -> None:
        code, _out, err = self.run_cli(rbac.RESTORE_OPERATOR, "job", "start", "--id", "job-1")
        self.assertEqual(code, PERMISSION_DENIED)
        self.assertIn("Restore Operator role does not allow job.start", err)

    def test_an_allowed_command_runs(self) -> None:
        code, out, _err = self.run_cli(rbac.BACKUP_OPERATOR, "--output", "json", "job", "list")
        self.assertEqual(code, 0)
        self.assertEqual([item["name"] for item in json.loads(out)["items"]], ["nightly"])

    def test_cli_commands_are_audited_as_such(self) -> None:
        self.run_cli(rbac.AUDITOR, "--output", "json", "job", "delete", "--id", "job-1")
        [entry] = self.audit(function_id="job.delete")
        self.assertEqual((entry["interface"], entry["command"], entry["result"]), ("cli", "job delete", "denied"))

    def test_whoami_lists_the_role_and_its_functions(self) -> None:
        code, out, _err = self.run_cli(rbac.BACKUP_OPERATOR, "--output", "json", "auth", "whoami")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["role"]["id"], rbac.BACKUP_OPERATOR)
        self.assertEqual(payload["permissions"], rbac.allowed_functions(rbac.BACKUP_OPERATOR))

    def test_users_get_and_change_roles_from_the_cli(self) -> None:
        code, _out, err = self.run_cli(rbac.ADMINISTRATOR, "user", "edit", "--name", "olga", "--role", "auditor")
        self.assertEqual(code, 0, err)
        code, out, _err = self.run_cli(rbac.ADMINISTRATOR, "--output", "json", "user", "info", "--name", "olga")
        self.assertEqual(json.loads(out)["role"], rbac.AUDITOR)
        code, _out, err = self.run_cli(rbac.ADMINISTRATOR, "user", "edit", "--name", "admin", "--disable")
        self.assertEqual(code, 4, err)
        self.assertIn("last active Administrator", err)

    def test_custom_roles_are_created_cloned_changed_and_deleted_from_the_cli(self) -> None:
        code, out, err = self.run_cli(
            rbac.ADMINISTRATOR, "--output", "json", "role", "create", "--name", "Night Operator",
            "--allow", "job.view,session.*",
        )
        self.assertEqual(code, 0, err)
        role = json.loads(out)
        self.assertEqual(
            set(role["allowed_functions"]), {"job.view", "session.view", "session.stop", *rbac.BASELINE_FUNCTIONS}
        )

        code, out, err = self.run_cli(
            rbac.ADMINISTRATOR, "--output", "json", "role", "clone", "--name", "Night Operator", "--new-name", "Night Copy"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["cloned_from"]["id"], role["id"])

        code, _out, err = self.run_cli(
            rbac.ADMINISTRATOR, "role", "edit", "--name", "night operator", "--allow", "job.start", "--deny", "session.stop"
        )
        self.assertEqual(code, 0, err)
        self.assertTrue(rbac.role_allows(role["id"], "job.start"))
        self.assertFalse(rbac.role_allows(role["id"], "session.stop"))

        code, _out, err = self.run_cli(rbac.ADMINISTRATOR, "user", "edit", "--name", "olga", "--role", "Night Operator")
        self.assertEqual(code, 0, err)
        self.assertEqual(USERS[self.user_id("olga")]["role"], role["id"])
        code, _out, err = self.run_cli(rbac.ADMINISTRATOR, "role", "delete", "--name", "Night Operator")
        self.assertEqual(code, 4, err)
        self.assertIn("olga", err)
        code, _out, err = self.run_cli(rbac.ADMINISTRATOR, "role", "delete", "--name", "Night Copy")
        self.assertEqual(code, 0, err)
        self.assertEqual([item["name"] for item in CUSTOM_ROLES.values()], ["Night Operator"])

    def test_role_management_is_refused_without_the_permission_or_for_a_built_in_role(self) -> None:
        code, _out, err = self.run_cli(rbac.AUDITOR, "role", "create", "--name", "Mine", "--allow", "job.view")
        self.assertEqual(code, PERMISSION_DENIED, err)
        self.assertEqual(CUSTOM_ROLES, {})
        code, _out, err = self.run_cli(rbac.ADMINISTRATOR, "role", "edit", "--id", "auditor", "--allow", "job.delete")
        self.assertEqual(code, 4, err)
        self.assertIn("built-in role", err)

    def test_every_command_names_a_server_function(self) -> None:
        for command, function_id in COMMAND_FUNCTIONS.items():
            self.assertIn(function_id, rbac.FUNCTIONS_BY_ID, command)
