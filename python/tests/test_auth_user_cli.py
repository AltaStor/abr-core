# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from python.cli import token_store
from python.cli.api.client import AltaStorApiClient
from python.cli.cli_context import TOKEN_ENV_VAR, resolve_token
from python.cli.commands.auth_commands import PASSWORD_ENV_VAR, USERNAME_ENV_VAR, AuthCommands
from python.cli.commands.user_commands import UserCommands
from python.cli.errors import CliStructuredError
from python.cli.parsing.parser_builder import AltaStorParserBuilder


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.content = b"{}"
        self.headers: dict[str, str] = {}

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _args(**overrides):
    base = {"base_url": "http://example.test", "output": "table"}
    base.update(overrides)
    return SimpleNamespace(**base)


class _IsolatedConfigDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._env_patcher = patch.dict(
            os.environ, {token_store.CONFIG_DIR_ENV_VAR: self._tmpdir.name, TOKEN_ENV_VAR: ""}
        )
        self._env_patcher.start()
        os.environ.pop(TOKEN_ENV_VAR, None)

    def tearDown(self) -> None:
        self._env_patcher.stop()
        self._tmpdir.cleanup()


class ParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = AltaStorParserBuilder().build()

    def test_auth_help_lists_login_and_logout(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer), self.assertRaises(SystemExit):
            self.parser.parse_args(["auth", "--help"])
        for command in ("login", "logout", "change-password"):
            self.assertIn(command, buffer.getvalue())

    def test_user_help_lists_add_delete_list(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer), self.assertRaises(SystemExit):
            self.parser.parse_args(["user", "--help"])
        for command in ("add", "delete", "list"):
            self.assertIn(command, buffer.getvalue())

    def test_login_name_flag_is_optional(self) -> None:
        args = self.parser.parse_args(["auth", "login"])
        self.assertIsNone(args.name)

    def test_user_add_requires_name(self) -> None:
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["user", "add"])


class TokenStoreTests(_IsolatedConfigDirCase):
    def test_save_load_clear_round_trip(self) -> None:
        self.assertIsNone(token_store.load_token("http://a.test"))

        token_store.save_token("http://a.test", "tok-123", "2099-01-01T00:00:00+00:00")
        self.assertEqual(token_store.load_token("http://a.test"), "tok-123")

        token_store.clear_token("http://a.test")
        self.assertIsNone(token_store.load_token("http://a.test"))

    def test_tokens_are_scoped_per_base_url(self) -> None:
        token_store.save_token("http://a.test", "tok-a", "2099-01-01T00:00:00+00:00")
        token_store.save_token("http://b.test", "tok-b", "2099-01-01T00:00:00+00:00")

        self.assertEqual(token_store.load_token("http://a.test"), "tok-a")
        self.assertEqual(token_store.load_token("http://b.test"), "tok-b")

        token_store.clear_token("http://a.test")
        self.assertIsNone(token_store.load_token("http://a.test"))
        self.assertEqual(token_store.load_token("http://b.test"), "tok-b")

    def test_one_server_spelled_differently_shares_one_token(self) -> None:
        token_store.save_token("https://backup01:443/", "tok-443", "2099-01-01T00:00:00+00:00")
        self.assertEqual(token_store.load_token("https://backup01"), "tok-443")
        self.assertEqual(token_store.load_token("https://BACKUP01/"), "tok-443")

        stored = json.loads((Path(self._tmpdir.name) / "credentials.json").read_text())
        self.assertEqual(list(stored), ["https://backup01"])

        token_store.clear_token("https://backup01:443")
        self.assertIsNone(token_store.load_token("https://backup01"))

    def test_a_non_default_port_stays_in_the_key(self) -> None:
        token_store.save_token("https://backup01:8443", "tok-8443", "2099-01-01T00:00:00+00:00")
        self.assertEqual(token_store.load_token("https://backup01:8443"), "tok-8443")
        self.assertIsNone(token_store.load_token("https://backup01"))

    def test_a_token_written_before_normalisation_is_still_readable(self) -> None:
        path = Path(self._tmpdir.name) / "credentials.json"
        path.write_text(json.dumps({"https://backup01:443": {"token": "legacy", "expires_at": "2099-01-01T00:00:00+00:00"}}))
        self.assertEqual(token_store.load_token("https://backup01:443"), "legacy")
        self.assertEqual(token_store.load_token("https://backup01"), "legacy")

    def test_credentials_file_is_owner_only(self) -> None:
        token_store.save_token("http://a.test", "tok-123", "2099-01-01T00:00:00+00:00")
        path = Path(self._tmpdir.name) / "credentials.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class ResolveTokenTests(_IsolatedConfigDirCase):
    def test_env_var_wins_over_stored_token(self) -> None:
        token_store.save_token("http://example.test", "stored-token", "2099-01-01T00:00:00+00:00")
        with patch.dict(os.environ, {TOKEN_ENV_VAR: "env-token"}):
            self.assertEqual(resolve_token("http://example.test"), "env-token")

    def test_falls_back_to_stored_token_when_env_unset(self) -> None:
        token_store.save_token("http://example.test", "stored-token", "2099-01-01T00:00:00+00:00")
        self.assertEqual(resolve_token("http://example.test"), "stored-token")

    def test_returns_none_when_nothing_is_configured(self) -> None:
        self.assertIsNone(resolve_token("http://example.test"))


class ApiClientAuthHeaderTests(unittest.TestCase):
    @patch("python.cli.api.client.requests.request")
    def test_no_bearer_header_is_sent_without_a_token(self, request_mock) -> None:
        # X-Client-Type is always sent (see auth_service.login's web/cli
        # ceiling exemption) -- only the Authorization header is conditional.
        request_mock.return_value = _FakeResponse({"ok": True})
        AltaStorApiClient(base_url="http://example.test").get("api/v1/repositories")
        self.assertEqual(request_mock.call_args.kwargs["headers"], {"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"})

    @patch("python.cli.api.client.requests.request")
    def test_bearer_header_is_sent_with_a_token(self, request_mock) -> None:
        request_mock.return_value = _FakeResponse({"ok": True})
        AltaStorApiClient(base_url="http://example.test", token="tok-123").get("api/v1/repositories")
        self.assertEqual(
            request_mock.call_args.kwargs["headers"],
            {"X-Client-Type": "cli", "X-ABR-API-Version": "1.0", "Authorization": "Bearer tok-123"},
        )


class AuthCommandsTests(_IsolatedConfigDirCase):
    @patch("python.cli.api.client.requests.request")
    def test_login_stores_token_and_prints_confirmation(self, request_mock) -> None:
        request_mock.return_value = _FakeResponse(
            {"token": "tok-abc", "expires_at": "2099-01-01T00:00:00+00:00", "user": {"id": "u1", "name": "admin"}}
        )
        args = _args(name="admin")
        with patch.dict(os.environ, {PASSWORD_ENV_VAR: "Sup3rSecretPW"}), redirect_stdout(io.StringIO()) as out:
            exit_code = AuthCommands().login(args)

        self.assertEqual(exit_code, 0)
        self.assertIn("Logged in as admin", out.getvalue())
        self.assertEqual(request_mock.call_args.kwargs["json"], {"name": "admin", "password": "Sup3rSecretPW"})
        self.assertEqual(token_store.load_token("http://example.test"), "tok-abc")

    @patch("python.cli.api.client.requests.request")
    def test_login_warns_when_password_policy_is_exempt(self, request_mock) -> None:
        request_mock.return_value = _FakeResponse(
            {
                "token": "tok-abc",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "user": {"id": "u1", "name": "admin", "passwordPolicyExempt": True},
            }
        )
        args = _args(name="admin")
        with patch.dict(os.environ, {PASSWORD_ENV_VAR: "admin"}):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                exit_code = AuthCommands().login(args)

        self.assertEqual(exit_code, 0)
        self.assertIn("WARNING", err.getvalue())
        self.assertIn("does not meet the minimum security policy", err.getvalue())

    @patch("python.cli.api.client.requests.request")
    def test_login_falls_back_to_username_env_var(self, request_mock) -> None:
        request_mock.return_value = _FakeResponse(
            {"token": "tok-abc", "expires_at": "2099-01-01T00:00:00+00:00", "user": {"id": "u1", "name": "admin"}}
        )
        args = _args(name=None)
        with patch.dict(os.environ, {USERNAME_ENV_VAR: "admin", PASSWORD_ENV_VAR: "Sup3rSecretPW"}):
            with redirect_stdout(io.StringIO()):
                AuthCommands().login(args)
        self.assertEqual(request_mock.call_args.kwargs["json"]["name"], "admin")

    @patch("python.cli.api.client.requests.request")
    @patch("python.cli.commands.auth_commands.getpass.getpass", side_effect=EOFError)
    def test_login_without_a_terminal_or_the_password_variable_is_a_usage_error(self, _getpass, request_mock) -> None:
        os.environ.pop(PASSWORD_ENV_VAR, None)

        with self.assertRaises(CliStructuredError) as raised:
            AuthCommands().login(_args(name="admin"))

        self.assertEqual(raised.exception.exit_code, 2)
        self.assertEqual(raised.exception.code, "invalid_argument")
        self.assertEqual(str(raised.exception), "No password provided; set ABR_PASSWORD or run interactively")
        request_mock.assert_not_called()

    @patch("python.cli.api.client.requests.request")
    @patch("builtins.input", side_effect=EOFError)
    def test_login_without_a_terminal_or_a_user_name_is_a_usage_error(self, _input, request_mock) -> None:
        os.environ.pop(USERNAME_ENV_VAR, None)

        with self.assertRaises(CliStructuredError) as raised:
            AuthCommands().login(_args(name=None))

        self.assertEqual(raised.exception.exit_code, 2)
        self.assertEqual(
            str(raised.exception), "No user name provided; pass --name, set ABR_USERNAME or run interactively"
        )
        request_mock.assert_not_called()

    @patch("python.cli.commands.auth_commands.getpass.getpass", side_effect=EOFError)
    def test_the_cli_answers_a_missing_password_with_one_json_error_and_exit_2(self, _getpass) -> None:
        from python.cli import abr_cli

        os.environ.pop(PASSWORD_ENV_VAR, None)
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {USERNAME_ENV_VAR: "admin"}), redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as exited:
                abr_cli.main(["--base-url", "http://example.test", "--output", "json", "auth", "login"])

        self.assertEqual(exited.exception.code, 2)
        error = json.loads(err.getvalue()[err.getvalue().index("{"):])["error"]
        self.assertEqual(error["code"], "invalid_argument")
        self.assertEqual(error["message"], "No password provided; set ABR_PASSWORD or run interactively")
        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn("EOFError", out.getvalue() + err.getvalue())

    @patch("python.cli.api.client.requests.request")
    def test_print_token_flag_writes_only_the_token_to_stdout(self, request_mock) -> None:
        request_mock.return_value = _FakeResponse(
            {"token": "tok-abc", "expires_at": "2099-01-01T00:00:00+00:00", "user": {"id": "u1", "name": "admin"}}
        )
        args = _args(name="admin", print_token=True)
        with patch.dict(os.environ, {PASSWORD_ENV_VAR: "Sup3rSecretPW"}):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                exit_code = AuthCommands().login(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(out.getvalue().strip(), "tok-abc")
        self.assertIn("Logged in as admin", err.getvalue())
        self.assertEqual(token_store.load_token("http://example.test"), "tok-abc")

    @patch("python.cli.api.client.requests.request")
    def test_logout_calls_api_and_clears_stored_token(self, request_mock) -> None:
        token_store.save_token("http://example.test", "tok-abc", "2099-01-01T00:00:00+00:00")
        request_mock.return_value = _FakeResponse({"status": "ok"})

        with redirect_stdout(io.StringIO()):
            exit_code = AuthCommands().logout(_args())

        self.assertEqual(exit_code, 0)
        request_mock.assert_called_once_with(
            "POST",
            "http://example.test/api/v1/auth/logout",
            json=None,
            params=None,
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0", "Authorization": "Bearer tok-abc"},
            verify=False,
        )
        self.assertIsNone(token_store.load_token("http://example.test"))

    @patch("python.cli.api.client.requests.request")
    def test_logout_without_a_stored_token_skips_the_network_call(self, request_mock) -> None:
        with redirect_stdout(io.StringIO()):
            exit_code = AuthCommands().logout(_args())
        self.assertEqual(exit_code, 0)
        request_mock.assert_not_called()

    @patch("python.cli.commands.auth_commands.getpass.getpass")
    @patch("python.cli.api.client.requests.request")
    def test_change_password_prompts_for_current_and_new_password(self, request_mock, getpass_mock) -> None:
        getpass_mock.side_effect = ["CurrentPassw0rd", "NewPassw0rd1", "NewPassw0rd1"]
        request_mock.return_value = _FakeResponse({"id": "u1", "name": "admin"})

        with redirect_stdout(io.StringIO()) as out:
            exit_code = AuthCommands().change_password(_args())

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            request_mock.call_args.kwargs["json"],
            {"current_password": "CurrentPassw0rd", "new_password": "NewPassw0rd1"},
        )
        self.assertEqual(request_mock.call_args.args, ("POST", "http://example.test/api/v1/auth/change-password"))
        self.assertIn("Password changed", out.getvalue())
        self.assertEqual(getpass_mock.call_count, 3)

    @patch("python.cli.commands.auth_commands.getpass.getpass")
    @patch("python.cli.api.client.requests.request")
    def test_change_password_current_password_falls_back_to_env_var(self, request_mock, getpass_mock) -> None:
        getpass_mock.side_effect = ["NewPassw0rd1", "NewPassw0rd1"]
        request_mock.return_value = _FakeResponse({"id": "u1", "name": "admin"})

        with patch.dict(os.environ, {PASSWORD_ENV_VAR: "CurrentPassw0rd"}), redirect_stdout(io.StringIO()):
            AuthCommands().change_password(_args())

        self.assertEqual(request_mock.call_args.kwargs["json"]["current_password"], "CurrentPassw0rd")
        # Only the two "new password" prompts -- current password came from the env var.
        self.assertEqual(getpass_mock.call_count, 2)

    @patch("python.cli.commands.auth_commands.getpass.getpass")
    @patch("python.cli.api.client.requests.request")
    def test_change_password_reprompts_on_mismatched_confirmation(self, request_mock, getpass_mock) -> None:
        getpass_mock.side_effect = ["CurrentPassw0rd", "first-new-pw", "second-new-pw", "match-pw", "match-pw"]
        request_mock.return_value = _FakeResponse({"id": "u1", "name": "admin"})

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            AuthCommands().change_password(_args())

        self.assertEqual(request_mock.call_args.kwargs["json"]["new_password"], "match-pw")
        self.assertIn("didn't match", err.getvalue())

    @patch("python.cli.commands.auth_commands.getpass.getpass")
    @patch("python.cli.api.client.requests.request")
    def test_change_password_reprompts_on_too_short_new_password(self, request_mock, getpass_mock) -> None:
        getpass_mock.side_effect = ["CurrentPassw0rd", "short", "longenoughpw", "longenoughpw"]
        request_mock.return_value = _FakeResponse({"id": "u1", "name": "admin"})

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            AuthCommands().change_password(_args())

        self.assertEqual(request_mock.call_args.kwargs["json"]["new_password"], "longenoughpw")
        self.assertIn("at least 8 characters", err.getvalue())

    @patch("python.cli.commands.auth_commands.getpass.getpass")
    @patch("python.cli.api.client.requests.request")
    def test_change_password_propagates_wrong_current_password_error(self, request_mock, getpass_mock) -> None:
        import requests

        getpass_mock.side_effect = ["WrongCurrentPw", "NewPassw0rd1", "NewPassw0rd1"]
        response = SimpleNamespace(
            status_code=401,
            json=lambda: {"error": {"code": "invalid_credentials", "message": "Current password is incorrect."}},
            text="",
        )
        error = requests.RequestException("401 Client Error")
        error.response = response
        request_mock.side_effect = error

        with self.assertRaises(requests.RequestException):
            AuthCommands().change_password(_args())


class UserCommandsTests(_IsolatedConfigDirCase):
    @patch("python.cli.commands.user_commands.getpass.getpass")
    @patch("python.cli.api.client.requests.request")
    def test_add_prompts_for_and_confirms_password(self, request_mock, getpass_mock) -> None:
        getpass_mock.side_effect = ["Passw0rd!", "Passw0rd!"]
        request_mock.return_value = _FakeResponse({"id": "u2", "name": "alice", "fullName": "Alice"})

        args = _args(name="alice", full_name="Alice")
        with redirect_stdout(io.StringIO()):
            exit_code = UserCommands().add(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            request_mock.call_args.kwargs["json"],
            {"name": "alice", "fullName": "Alice", "password": "Passw0rd!"},
        )

    @patch("python.cli.commands.user_commands.getpass.getpass")
    def test_add_reprompts_on_password_mismatch(self, getpass_mock) -> None:
        getpass_mock.side_effect = ["first-pass", "second-pass", "match-pass", "match-pass"]
        with patch("python.cli.api.client.requests.request") as request_mock:
            request_mock.return_value = _FakeResponse({"id": "u2", "name": "alice", "fullName": ""})
            with redirect_stdout(io.StringIO()):
                UserCommands().add(_args(name="alice", full_name=""))
        self.assertEqual(request_mock.call_args.kwargs["json"]["password"], "match-pass")

    @patch("python.cli.api.client.requests.request")
    def test_delete_resolves_by_name_then_deletes(self, request_mock) -> None:
        request_mock.side_effect = [
            _FakeResponse({"items": [{"id": "u2", "name": "alice"}]}),
            _FakeResponse({"id": "u2", "deleted": True}),
        ]
        with redirect_stdout(io.StringIO()):
            exit_code = UserCommands().delete(_args(name="alice", id=None))

        self.assertEqual(exit_code, 0)
        self.assertEqual(request_mock.call_args_list[1].args, ("DELETE", "http://example.test/api/v1/users/u2"))

    @patch("python.cli.api.client.requests.request")
    def test_list_prints_users(self, request_mock) -> None:
        request_mock.return_value = _FakeResponse(
            {"items": [{"id": "u1", "name": "admin", "fullName": "", "created_at": "2026-01-01T00:00:00+00:00"}]}
        )
        with redirect_stdout(io.StringIO()) as out:
            exit_code = UserCommands().list(_args())
        self.assertEqual(exit_code, 0)
        self.assertIn("admin", out.getvalue())


class CliUnauthorizedExitCodeTests(_IsolatedConfigDirCase):
    def test_401_response_maps_to_unauthorized_exit_code_and_hint(self) -> None:
        import requests

        from python.cli.abr_cli import main
        from python.cli.exit_codes import UNAUTHORIZED

        response = SimpleNamespace(
            status_code=401,
            json=lambda: {"error": {"code": "unauthorized", "message": "Missing, invalid, or expired session token."}},
            text="",
        )
        error = requests.RequestException("401 Client Error")
        error.response = response

        with patch("python.cli.api.client.requests.request", side_effect=error):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                with self.assertRaises(SystemExit) as exc:
                    main(["repo", "list"])
        self.assertEqual(exc.exception.code, UNAUTHORIZED)


if __name__ == "__main__":
    unittest.main()
