# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from python.cli.errors import CliStructuredError
from python.cli.parsing.parser_builder import AltaStorParserBuilder
from python.cli.parsing.secret_arguments import ASK, resolve_secret
from python.cli.services.job_service import JobService
from python.cli.services.restore_service import RestoreService


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


class ResolveSecretTests(unittest.TestCase):
    def test_a_value_given_on_the_command_line_is_still_used(self) -> None:
        with patch("getpass.getpass") as prompt:
            self.assertEqual(resolve_secret("typed", env_var="ABR_X", prompt="Secret", needed=True), "typed")
        prompt.assert_not_called()

    def test_an_optional_secret_that_was_not_asked_for_stays_unset(self) -> None:
        with patch.dict("os.environ", {"ABR_X": "from-env"}), patch("getpass.getpass") as prompt:
            self.assertIsNone(resolve_secret(None, env_var="ABR_X", prompt="Secret"))
        prompt.assert_not_called()

    def test_the_environment_variable_comes_before_the_prompt(self) -> None:
        with patch.dict("os.environ", {"ABR_X": "from-env"}), patch("getpass.getpass") as prompt:
            self.assertEqual(resolve_secret(ASK, env_var="ABR_X", prompt="Secret"), "from-env")
            self.assertEqual(resolve_secret(None, env_var="ABR_X", prompt="Secret", needed=True), "from-env")
        prompt.assert_not_called()

    def test_without_the_variable_the_secret_is_prompted_for(self) -> None:
        with patch.dict("os.environ", {}, clear=True), patch("getpass.getpass", return_value="typed-at-prompt") as prompt:
            self.assertEqual(resolve_secret(ASK, env_var="ABR_X", prompt="Secret"), "typed-at-prompt")
        prompt.assert_called_once_with("Secret: ")

    def test_a_new_key_is_confirmed_and_a_mismatch_is_refused(self) -> None:
        with patch.dict("os.environ", {}, clear=True), patch("getpass.getpass", side_effect=["one", "two"]):
            with self.assertRaisesRegex(CliStructuredError, "did not match"):
                resolve_secret(ASK, env_var="ABR_X", prompt="Encryption key", confirm=True)
        with patch.dict("os.environ", {}, clear=True), patch("getpass.getpass", side_effect=["same", "same"]):
            self.assertEqual(resolve_secret(ASK, env_var="ABR_X", prompt="Encryption key", confirm=True), "same")

    def test_no_terminal_and_no_variable_names_the_variable(self) -> None:
        with patch.dict("os.environ", {}, clear=True), patch("getpass.getpass", side_effect=EOFError):
            with self.assertRaisesRegex(CliStructuredError, "No secret provided; set ABR_X or run interactively"):
                resolve_secret(ASK, env_var="ABR_X", prompt="Secret")

    def test_an_empty_answer_is_refused(self) -> None:
        with patch.dict("os.environ", {}, clear=True), patch("getpass.getpass", return_value=""):
            with self.assertRaisesRegex(CliStructuredError, "No secret entered"):
                resolve_secret(ASK, env_var="ABR_X", prompt="Secret")


class SecretFlagParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = AltaStorParserBuilder().build()

    def test_every_secret_flag_takes_a_value_or_nothing(self) -> None:
        cases = [
            (["inventory", "add", "--name", "v", "--type", "vsphere", "--address", "a", "--password"], "password"),
            (["inventory", "add", "--name", "v", "--type", "proxmox-ve", "--address", "a", "--token"], "token"),
            (["inventory", "edit", "--name", "v", "--password"], "password"),
            (["inventory", "edit", "--name", "v", "--token"], "token"),
            (["repo", "add", "--name", "r", "--type", "smb", "--password"], "password"),
            (["repo", "add", "--name", "r", "--type", "s3", "--secret-key"], "secret_key"),
            (["repo", "edit", "--name", "r", "--password"], "password"),
            (["repo", "edit", "--name", "r", "--secret-key"], "secret_key"),
            (["job", "create", "--name", "j", "--set-encryption", "--encryption-key"], "encryption_key"),
            (["job", "edit", "--name", "j", "--set-encryption", "--encryption-key"], "encryption_key"),
            (["restore", "start", "--restore-point", "p", "--encryption-key"], "encryption_key"),
            (["backup", "verify", "--id", "b", "--encryption-key"], "encryption_key"),
        ]
        for argv, dest in cases:
            with self.subTest(argv=argv):
                self.assertEqual(getattr(self.parser.parse_args(argv), dest), ASK)
                self.assertEqual(getattr(self.parser.parse_args([*argv, "typed"]), dest), "typed")

    def test_a_bare_flag_does_not_swallow_the_next_option(self) -> None:
        args = self.parser.parse_args(
            ["inventory", "add", "--name", "v", "--type", "vsphere", "--address", "a", "--password", "--insecure"]
        )
        self.assertEqual(args.password, ASK)
        self.assertTrue(args.insecure)

    def test_the_help_warns_about_a_typed_value_and_names_the_variable(self) -> None:
        for command, variable in (
            (["inventory", "add"], "ABR_INVENTORY_PASSWORD"),
            (["repo", "add"], "ABR_REPO_SECRET_KEY"),
            (["repo", "add"], "ABR_REPO_PASSWORD"),
            (["job", "create"], "ABR_ENCRYPTION_KEY"),
            (["restore", "start"], "ABR_ENCRYPTION_KEY"),
            (["backup", "verify"], "ABR_ENCRYPTION_KEY"),
        ):
            with self.subTest(command=command, variable=variable):
                stdout = io.StringIO()
                with redirect_stdout(stdout), self.assertRaises(SystemExit):
                    self.parser.parse_args([*command, "--help"])
                text = " ".join(stdout.getvalue().split())
                self.assertIn(variable, text)
                self.assertIn("visible to every local user (ps, /proc)", text)


class SecretCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = AltaStorParserBuilder().build()

    def _run(self, argv: list[str], environ: dict[str, str], prompt=None) -> dict[str, object]:
        args = self.parser.parse_args(["--base-url", "http://example.test", "--output", "json", *argv])
        sent: list[dict[str, object]] = []

        def fake_request(method, url, json=None, **kwargs):
            if method in {"POST", "PATCH"}:
                sent.append(json or {})
            if method == "GET" and url.endswith("/api/v1/inventories"):
                return _FakeResponse({"items": [{"id": "inv-1", "name": "vc"}]})
            if method == "GET" and url.endswith("/api/v1/repositories"):
                return _FakeResponse({"items": [{"id": "repo-1", "name": "r"}]})
            if method == "GET":
                return _FakeResponse({"id": "repo-1", "name": "r", "type": "smb"})
            return _FakeResponse({"id": "x", "name": "x"})

        with patch.dict("os.environ", environ, clear=True), patch(
            "python.cli.api.client.requests.request", side_effect=fake_request
        ), patch("getpass.getpass", side_effect=prompt or AssertionError("unexpected prompt")), redirect_stdout(io.StringIO()):
            self.assertEqual(args.handler(args), 0)
        return sent[-1]

    def test_inventory_add_takes_the_vcenter_password_from_the_environment(self) -> None:
        payload = self._run(
            ["inventory", "add", "--name", "vc", "--type", "vsphere", "--address", "vc.local", "--username", "admin"],
            {"ABR_INVENTORY_PASSWORD": "from-env"},
        )
        self.assertEqual(payload["password"], "from-env")

    def test_inventory_add_prompts_for_the_password_when_nothing_else_gives_it(self) -> None:
        payload = self._run(
            ["inventory", "add", "--name", "vc", "--type", "vsphere", "--address", "vc.local", "--username", "admin"],
            {},
            prompt=["typed"],
        )
        self.assertEqual(payload["password"], "typed")

    def test_inventory_add_with_a_token_and_no_user_does_not_prompt(self) -> None:
        payload = self._run(
            ["inventory", "add", "--name", "pve", "--type", "proxmox-ve", "--address", "pve.local", "--token"],
            {"ABR_INVENTORY_TOKEN": "token-from-env"},
        )
        self.assertEqual(payload["token"], "token-from-env")
        self.assertIsNone(payload["password"])

    def test_inventory_edit_leaves_the_password_alone_unless_asked(self) -> None:
        payload = self._run(["inventory", "edit", "--name", "vc", "--port", "444"], {"ABR_INVENTORY_PASSWORD": "from-env"})
        self.assertNotIn("password", payload)
        payload = self._run(["inventory", "edit", "--name", "vc", "--password"], {"ABR_INVENTORY_PASSWORD": "from-env"})
        self.assertEqual(payload["password"], "from-env")

    def test_repo_add_smb_prompts_for_the_password(self) -> None:
        payload = self._run(
            ["repo", "add", "--name", "r", "--type", "smb", "--address", "//nas/share", "--username", "svc"],
            {},
            prompt=["typed"],
        )
        self.assertEqual(payload["password"], "typed")

    def test_repo_add_local_prompts_for_the_share_password_only_with_a_username(self) -> None:
        base = ["repo", "add", "--name", "r", "--type", "local", "--path", "/srv/backups", "--server", "backup01", "--share", "hv"]
        payload = self._run([*base, "--username", "CORP\\svc"], {}, prompt=["typed"])
        self.assertEqual(payload["password"], "typed")
        payload = self._run([*base, "--username", "CORP\\svc"], {"ABR_REPO_PASSWORD": "from-env"})
        self.assertEqual(payload["password"], "from-env")
        payload = self._run(base, {"ABR_REPO_PASSWORD": "from-env"})
        self.assertIsNone(payload.get("password"))

    def test_repo_add_s3_takes_the_secret_key_from_the_environment_only_with_an_access_key(self) -> None:
        base = ["repo", "add", "--name", "r", "--type", "s3", "--bucket", "b", "--region", "us-east-1"]
        payload = self._run([*base, "--access-key", "AK"], {"ABR_REPO_SECRET_KEY": "from-env"})
        self.assertEqual(payload["s3"]["secret_key"], "from-env")
        payload = self._run(base, {"ABR_REPO_SECRET_KEY": "from-env"})
        self.assertNotIn("secret_key", payload["s3"])

    def test_repo_edit_leaves_the_password_alone_unless_asked(self) -> None:
        payload = self._run(["repo", "edit", "--name", "r", "--description", "d"], {"ABR_REPO_PASSWORD": "from-env"})
        self.assertNotIn("password", payload)
        payload = self._run(["repo", "edit", "--name", "r", "--password"], {}, prompt=["typed"])
        self.assertEqual(payload["password"], "typed")


class EncryptionKeyTests(unittest.TestCase):
    def test_job_create_with_set_encryption_reads_the_key_from_the_environment(self) -> None:
        args = SimpleNamespace(set_encryption=True, reset_encryption=False, encryption_key=None)
        with patch.dict("os.environ", {"ABR_ENCRYPTION_KEY": "from-env"}, clear=True), patch("getpass.getpass") as prompt:
            updates = JobService._build_task_parameter_updates(args, creating=True)
        self.assertEqual(updates["encryption_key"], "from-env")
        prompt.assert_not_called()

    def test_job_create_prompts_twice_for_a_new_key(self) -> None:
        args = SimpleNamespace(set_encryption=True, reset_encryption=False, encryption_key=None)
        with patch.dict("os.environ", {}, clear=True), patch("getpass.getpass", side_effect=["k", "k"]) as prompt:
            updates = JobService._build_task_parameter_updates(args, creating=True)
        self.assertEqual(updates["encryption_key"], "k")
        self.assertEqual(prompt.call_count, 2)

    def test_job_edit_set_encryption_without_the_flag_keeps_the_stored_key(self) -> None:
        args = SimpleNamespace(set_encryption=True, reset_encryption=False, encryption_key=None)
        with patch.dict("os.environ", {"ABR_ENCRYPTION_KEY": "from-env"}, clear=True), patch("getpass.getpass") as prompt:
            updates = JobService._build_task_parameter_updates(args)
        self.assertEqual(updates, {"encryption_enabled": True})
        prompt.assert_not_called()

    def test_a_bare_key_flag_without_set_encryption_is_still_refused(self) -> None:
        args = SimpleNamespace(set_encryption=False, reset_encryption=False, encryption_key=ASK)
        with self.assertRaisesRegex(ValueError, "--encryption-key requires --set-encryption"):
            JobService._build_task_parameter_updates(args)

    def test_restore_start_takes_the_key_from_the_environment_when_asked(self) -> None:
        service = RestoreService(context=SimpleNamespace())
        with patch.dict("os.environ", {"ABR_ENCRYPTION_KEY": "from-env"}, clear=True), patch("getpass.getpass") as prompt:
            asked = service.build_start_payload(SimpleNamespace(restore_point="p", encryption_key=ASK))
            not_asked = service.build_start_payload(SimpleNamespace(restore_point="p", encryption_key=None))
        self.assertEqual(asked["encryption_key"], "from-env")
        self.assertNotIn("encryption_key", not_asked)
        prompt.assert_not_called()

    def test_backup_verify_sends_the_prompted_key(self) -> None:
        parser = AltaStorParserBuilder().build()
        args = parser.parse_args(
            ["--base-url", "http://example.test", "--output", "json", "backup", "verify", "--id", "b-1", "--encryption-key"]
        )
        sent: list[object] = []

        def fake_request(method, url, json=None, **kwargs):
            sent.append(json)
            return _FakeResponse({"status": "queued"})

        with patch.dict("os.environ", {}, clear=True), patch(
            "python.cli.api.client.requests.request", side_effect=fake_request
        ), patch("getpass.getpass", return_value="typed"), redirect_stdout(io.StringIO()):
            args.handler(args)
        self.assertEqual(sent[0], {"encryption_key": "typed"})


if __name__ == "__main__":
    unittest.main()
