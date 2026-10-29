# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import logging
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from python.srv.backup_server import app
from python.srv.core import credential_encryption
from python.srv.core.mongo_db_wrapper import REPOSITORIES, SESSIONS, USERS
from python.srv.plugins.hyperv import vhdx_tool
from python.srv.services import abr_v41, auth_service, default_repository
from python.srv.services.api_errors import ApiError

BOOTSTRAP_ENV = {
    "ABR_BOOTSTRAP_ADMIN_USER": "admin",
    "ABR_BOOTSTRAP_ADMIN_PASSWORD": "Sup3rSecretPW",
}


class _DefaultRepositoryCase(unittest.TestCase):
    def setUp(self) -> None:
        REPOSITORIES.clear()
        self.addCleanup(REPOSITORIES.clear)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "var" / "lib" / "altastor-backup" / "repository"
        environment = patch.dict("os.environ", {default_repository.PATH_ENV: str(self.path)})
        environment.start()
        self.addCleanup(environment.stop)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        for name in ("REPO_ROOT", "APP_ROOT"):
            installed = patch.object(default_repository, name, self.checkout)
            installed.start()
            self.addCleanup(installed.stop)

    def _folder(self, name: str) -> str:
        folder = self.root / name
        folder.mkdir(mode=0o750)
        return str(folder)


class EnsureDefaultRepositoryTests(_DefaultRepositoryCase):
    def test_a_missing_default_repository_is_created_with_its_folder(self) -> None:
        repository, created = default_repository.ensure_default_repository()

        self.assertTrue(created)
        self.assertEqual(repository["name"], "Default Repo")
        self.assertEqual(repository["type"], "local")
        self.assertEqual(repository["path"], str(self.path))
        self.assertTrue(repository["default"])
        self.assertFalse(repository["support_hyperv"])
        self.assertEqual(repository["status"], "available")
        self.assertTrue(self.path.is_dir())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o750)
        self.assertEqual(len(REPOSITORIES), 1)

    def test_an_existing_default_repository_is_kept_and_no_second_one_is_made(self) -> None:
        first, _created = default_repository.ensure_default_repository()

        again, created = default_repository.ensure_default_repository()

        self.assertFalse(created)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(REPOSITORIES), 1)

    def test_a_renamed_default_repository_is_still_the_default_one(self) -> None:
        first, _created = default_repository.ensure_default_repository()
        abr_v41.edit_repository_record(first["id"], {"name": "Main", "description": "Our backups"})

        again, created = default_repository.ensure_default_repository()

        self.assertFalse(created)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["name"], "Main")
        self.assertTrue(again["default"])
        self.assertEqual(len(REPOSITORIES), 1)

    def test_a_deleted_default_repository_is_created_again(self) -> None:
        first, _created = default_repository.ensure_default_repository()
        abr_v41.remove_repository_record(first["id"])

        again, created = default_repository.ensure_default_repository()

        self.assertTrue(created)
        self.assertNotEqual(again["id"], first["id"])
        self.assertEqual(len(REPOSITORIES), 1)

    def test_a_repository_named_like_it_at_its_folder_becomes_the_default_one(self) -> None:
        self.path.mkdir(mode=0o750, parents=True)
        own = abr_v41.create_repository_record({"name": "Default Repo", "type": "local", "path": str(self.path)})

        repository, created = default_repository.ensure_default_repository()

        self.assertFalse(created)
        self.assertEqual(repository["id"], own["id"])
        self.assertTrue(repository["default"])
        self.assertEqual(len(REPOSITORIES), 1)

    def test_a_repository_of_that_name_elsewhere_is_left_alone(self) -> None:
        other = abr_v41.create_repository_record(
            {"name": "Default Repo", "type": "local", "path": self._folder("elsewhere")}
        )

        with self.assertRaises(ApiError) as caught:
            default_repository.ensure_default_repository()

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.code, "default_repository_name_taken")
        self.assertIn("Rename it", caught.exception.message)
        self.assertEqual(list(REPOSITORIES), [other["id"]])
        self.assertFalse(default_repository.is_default_repository(REPOSITORIES[other["id"]]))
        self.assertFalse(self.path.exists())

    def test_a_folder_that_cannot_be_created_is_refused_with_the_setting_to_change(self) -> None:
        blocker = self.root / "var"
        blocker.write_text("not a folder")

        with self.assertRaises(ApiError) as caught:
            default_repository.ensure_default_repository()

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.code, "default_repository_path_unavailable")
        self.assertIn(default_repository.PATH_ENV, caught.exception.message)
        self.assertEqual(list(REPOSITORIES), [])

    def test_an_existing_folder_is_used_as_it_is(self) -> None:
        self.path.mkdir(mode=0o750, parents=True)
        (self.path / "kept.txt").write_text("data")

        _repository, created = default_repository.ensure_default_repository()

        self.assertTrue(created)
        self.assertEqual((self.path / "kept.txt").read_text(), "data")

    def test_the_marks_are_not_listed_as_settable_and_survive_an_edit(self) -> None:
        first, _created = default_repository.ensure_default_repository()

        edited = abr_v41.edit_repository_record(first["id"], {"description": "Changed"})

        self.assertTrue(edited["default"])
        self.assertFalse(edited["support_hyperv"])

    def test_a_repository_created_through_the_api_is_never_the_default_one(self) -> None:
        repository = abr_v41.create_repository_record(
            {"name": "Lab", "type": "local", "path": self._folder("lab"), "default_repository": True}
        )

        self.assertFalse(repository["default"])
        self.assertIsNone(default_repository.find_default_repository())


class DevCheckoutTests(_DefaultRepositoryCase):
    def setUp(self) -> None:
        super().setUp()
        checkout = patch.object(default_repository, "APP_ROOT", self.root / "opt" / "altastor-backup")
        checkout.start()
        self.addCleanup(checkout.stop)

    def test_a_checkout_that_cannot_create_the_folder_uses_one_of_its_own(self) -> None:
        (self.root / "var").write_text("not a folder")

        with self.assertLogs(default_repository.logger, "WARNING") as logs:
            repository, created = default_repository.ensure_default_repository()

        self.assertTrue(created)
        self.assertEqual(repository["path"], str(self.checkout / "backups" / "default-repo"))
        self.assertTrue((self.checkout / "backups" / "default-repo").is_dir())
        self.assertIn("Expected in a dev checkout", logs.output[0])

    def test_a_checkout_that_can_create_the_folder_uses_it(self) -> None:
        repository, _created = default_repository.ensure_default_repository()

        self.assertEqual(repository["path"], str(self.path))
        self.assertFalse((self.checkout / "backups").exists())

    def test_an_install_never_puts_the_repository_next_to_the_program(self) -> None:
        installed = patch.object(default_repository, "APP_ROOT", self.checkout)
        installed.start()
        self.addCleanup(installed.stop)
        (self.root / "var").write_text("not a folder")

        with self.assertRaises(ApiError) as caught:
            default_repository.ensure_default_repository()

        self.assertEqual(caught.exception.code, "default_repository_path_unavailable")
        self.assertFalse((self.checkout / "backups").exists())


class StartupTests(_DefaultRepositoryCase):
    def test_startup_creates_the_default_repository(self) -> None:
        with patch.object(default_repository, "get_database", return_value=object()):
            default_repository.create_default_repository_at_startup()

        self.assertIsNotNone(default_repository.find_default_repository())

    def test_startup_without_the_catalog_creates_nothing(self) -> None:
        with (
            patch.object(default_repository, "get_database", return_value=None),
            self.assertLogs(default_repository.logger, "WARNING") as logs,
        ):
            default_repository.create_default_repository_at_startup()

        self.assertEqual(list(REPOSITORIES), [])
        self.assertIn("catalog database is unavailable", logs.output[0])

    def test_a_default_repository_that_cannot_be_made_is_logged_and_startup_goes_on(self) -> None:
        (self.root / "var").write_text("not a folder")

        with (
            patch.object(default_repository, "get_database", return_value=object()),
            self.assertLogs(default_repository.logger, "WARNING") as logs,
        ):
            default_repository.create_default_repository_at_startup()

        self.assertEqual(list(REPOSITORIES), [])
        self.assertIn("could not be created", logs.output[0])


class _Helper:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str], list[str]]] = []
        self.answer = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")

    def __call__(self, command, *, input, **_kwargs):
        fields = dict(line.split("=", 1) for line in input.splitlines() if line)
        self.calls.append((command[-1], fields, command[:-1]))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class _HyperVCase(_DefaultRepositoryCase):
    def setUp(self) -> None:
        super().setUp()
        key = patch.dict("os.environ", {credential_encryption.KEY_FILE_ENV: str(self.root / "credentials.key")})
        key.start()
        self.addCleanup(key.stop)
        credential_encryption.reset_cipher()
        self.addCleanup(credential_encryption.reset_cipher)
        self.helper = _Helper()
        run = patch.object(default_repository.subprocess, "run", side_effect=self.helper)
        run.start()
        self.addCleanup(run.stop)
        self.repository, _created = default_repository.ensure_default_repository()

    def _stored(self) -> dict:
        return REPOSITORIES[self.repository["id"]]


class HyperVSwitchTests(_HyperVCase):
    def test_turning_it_on_creates_a_hidden_share_and_an_account_and_keeps_the_password_encrypted(self) -> None:
        repository, changed = default_repository.set_hyperv_support(True)

        self.assertTrue(changed)
        [(command, fields, prefix)] = self.helper.calls
        self.assertEqual(command, "enable-share")
        self.assertEqual(prefix, ["sudo", "-n", default_repository.DEFAULT_HELPER])
        self.assertRegex(fields["share"], r"^abr-[a-z0-9]{8}\$$")
        self.assertRegex(fields["user"], r"^abr-smb-[a-z0-9]{8}$")
        self.assertRegex(fields["password"], r"^[A-Za-z0-9]{40}$")
        self.assertEqual(fields["path"], str(self.path.resolve()))
        stored = self._stored()["metadata"]
        self.assertEqual(self._stored()["type"], "smb")
        self.assertEqual((stored["share"], stored["username"]), (fields["share"], fields["user"]))
        self.assertTrue(credential_encryption.is_encrypted(stored["password"]))
        self.assertEqual(credential_encryption.decrypt_secret(stored["password"]), fields["password"])
        self.assertTrue(repository["support_hyperv"])
        self.assertEqual(repository["share"], fields["share"])
        self.assertNotIn(fields["password"], json.dumps(repository))

    def test_turning_it_on_twice_changes_nothing(self) -> None:
        default_repository.set_hyperv_support(True)
        share = self._stored()["metadata"]["share"]

        _repository, changed = default_repository.set_hyperv_support(True)

        self.assertFalse(changed)
        self.assertEqual(len(self.helper.calls), 1)
        self.assertEqual(self._stored()["metadata"]["share"], share)

    def test_turning_it_off_removes_the_share_and_the_account(self) -> None:
        default_repository.set_hyperv_support(True)
        share, user = self._stored()["metadata"]["share"], self._stored()["metadata"]["username"]

        repository, changed = default_repository.set_hyperv_support(False)

        self.assertTrue(changed)
        self.assertEqual(self.helper.calls[-1][:2], ("disable-share", {"share": share, "user": user}))
        self.assertEqual(self._stored()["type"], "local")
        self.assertTrue(all(self._stored()["metadata"][field] is None for field in default_repository.SHARE_FIELDS))
        self.assertFalse(repository["support_hyperv"])

    def test_turning_it_off_when_it_is_off_runs_nothing(self) -> None:
        _repository, changed = default_repository.set_hyperv_support(False)

        self.assertFalse(changed)
        self.assertEqual(self.helper.calls, [])

    def test_a_refused_share_leaves_the_repository_as_it_was(self) -> None:
        self.helper.answer = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="abr-samba-helper: a share named abr-x$ already exists in the Samba configuration\n"
        )
        before = json.dumps(self._stored(), sort_keys=True)

        with self.assertRaises(ApiError) as caught:
            default_repository.set_hyperv_support(True)

        self.assertEqual((caught.exception.status_code, caught.exception.code), (500, "hyperv_share_failed"))
        self.assertIn("a share named abr-x$ already exists", caught.exception.message)
        self.assertEqual(json.dumps(self._stored(), sort_keys=True), before)

    def test_a_server_without_the_sudo_rule_is_told_so(self) -> None:
        self.helper.answer = subprocess.CompletedProcess([], 1, stdout="", stderr="sudo: a password is required\n")

        with self.assertRaises(ApiError) as caught:
            default_repository.set_hyperv_support(True)

        self.assertEqual((caught.exception.status_code, caught.exception.code), (503, "hyperv_share_not_permitted"))
        self.assertIn("sudo rule", caught.exception.message)

    def test_a_server_without_sudo_is_told_so(self) -> None:
        self.helper.answer = FileNotFoundError(2, "No such file or directory", "sudo")

        with self.assertRaises(ApiError) as caught:
            default_repository.set_hyperv_support(True)

        self.assertEqual(caught.exception.code, "hyperv_share_not_permitted")

    def test_a_share_the_catalog_cannot_record_is_removed_again(self) -> None:
        with (
            patch.object(default_repository, "persist_repository", side_effect=RuntimeError("catalog down")),
            self.assertRaises(RuntimeError),
        ):
            default_repository.set_hyperv_support(True)

        self.assertEqual([call[0] for call in self.helper.calls], ["enable-share", "disable-share"])
        self.assertEqual(self.helper.calls[1][1]["share"], self.helper.calls[0][1]["share"])
        self.assertFalse(self._stored()["metadata"]["support_hyperv"])

    def test_without_a_credential_key_nothing_is_created(self) -> None:
        with (
            patch.object(
                default_repository,
                "encrypt_secret",
                side_effect=credential_encryption.CredentialKeyUnavailable("The credential key cannot be read"),
            ),
            self.assertRaises(ApiError) as caught,
        ):
            default_repository.set_hyperv_support(True)

        self.assertEqual(caught.exception.code, "credential_key_unavailable")
        self.assertEqual(self.helper.calls, [])

    def test_the_password_is_never_logged(self) -> None:
        with self.assertLogs("python.srv", level=logging.DEBUG) as logs:
            default_repository.set_hyperv_support(True)
            default_repository.set_hyperv_support(False)

        password = self.helper.calls[0][1]["password"]
        self.assertTrue(logs.output)
        self.assertFalse(any(password in line for line in logs.output))

    def test_the_managed_share_cannot_be_edited_by_hand(self) -> None:
        default_repository.set_hyperv_support(True)

        with self.assertRaises(ApiError) as caught:
            abr_v41.edit_repository_record(self.repository["id"], {"password": "mine", "username": "me"})

        self.assertEqual((caught.exception.status_code, caught.exception.code), (409, "default_repository_share_managed"))
        self.assertEqual(caught.exception.details["fields"], ["password", "username"])
        edited = abr_v41.edit_repository_record(self.repository["id"], {"description": "Ours"})
        self.assertEqual(edited["description"], "Ours")

    def test_removing_the_default_repository_removes_its_share(self) -> None:
        default_repository.set_hyperv_support(True)
        share = self._stored()["metadata"]["share"]

        abr_v41.remove_repository_record(self.repository["id"])

        self.assertEqual(self.helper.calls[-1][0], "disable-share")
        self.assertEqual(self.helper.calls[-1][1]["share"], share)
        self.assertEqual(list(REPOSITORIES), [])


class HyperVHostTests(_HyperVCase):
    def test_hosts_get_the_address_this_appliance_reaches_them_from_and_the_decrypted_password(self) -> None:
        default_repository.set_hyperv_support(True)
        password = self.helper.calls[0][1]["password"]

        with patch.object(vhdx_tool, "appliance_address_toward", return_value="10.0.0.5") as toward:
            repository = vhdx_tool.for_hyperv_host(self._stored(), "hv01.lab")
        _args, env = vhdx_tool.storage_invocation(repository, abr_path=str(self.path / "job" / "vm"))

        toward.assert_called_once_with("hv01.lab")
        self.assertEqual(env["ABR_SMB_SHARE"], f"\\\\10.0.0.5\\{self._stored()['metadata']['share']}")
        self.assertEqual(env["ABR_SMB_USER"], self._stored()["metadata"]["username"])
        self.assertEqual(env["ABR_SMB_PASSWORD"], password)

    def test_other_repositories_are_passed_on_untouched(self) -> None:
        with patch.object(vhdx_tool, "appliance_address_toward") as toward:
            self.assertIs(vhdx_tool.for_hyperv_host(self._stored(), "hv01.lab"), self._stored())

        toward.assert_not_called()

    def test_a_password_this_appliance_cannot_decrypt_stops_the_run(self) -> None:
        default_repository.set_hyperv_support(True)
        with patch.object(vhdx_tool, "appliance_address_toward", return_value="10.0.0.5"):
            repository = vhdx_tool.for_hyperv_host(self._stored(), "hv01.lab")
        credential_encryption.reset_cipher()
        (self.root / "credentials.key").write_bytes(b"A" * 43 + b"=")
        credential_encryption.reset_cipher()

        with self.assertRaises(vhdx_tool.VhdxToolError) as caught:
            vhdx_tool.storage_invocation(repository, abr_path=str(self.path))

        self.assertIn("cannot be decrypted", str(caught.exception))

    def test_the_address_toward_a_host_is_a_local_one(self) -> None:
        self.assertEqual(vhdx_tool.appliance_address_toward("127.0.0.1"), "127.0.0.1")

    def test_a_host_without_a_route_is_named(self) -> None:
        with self.assertRaises(vhdx_tool.VhdxToolError) as caught:
            vhdx_tool.appliance_address_toward("no-such-host.invalid")

        self.assertIn("no-such-host.invalid", str(caught.exception))


class DefaultRepositoryApiTests(_DefaultRepositoryCase):
    def setUp(self) -> None:
        super().setUp()
        USERS.clear()
        SESSIONS.clear()
        auth_service.reset_login_lockout_state_for_testing()
        environment = patch.dict("os.environ", BOOTSTRAP_ENV)
        environment.start()
        self.addCleanup(environment.stop)
        client = TestClient(app)
        self.client = client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        login = self.client.post("/api/v1/auth/login", json={"name": "admin", "password": "Sup3rSecretPW"})
        self.headers = {"Authorization": f"Bearer {login.json()['token']}"}
        self.addCleanup(USERS.clear)
        self.addCleanup(SESSIONS.clear)

    def test_requires_a_session(self) -> None:
        self.assertEqual(self.client.get("/api/v1/repositories/default").status_code, 401)
        self.assertEqual(self.client.post("/api/v1/repositories/default").status_code, 401)

    def test_create_then_create_again_then_read(self) -> None:
        missing = self.client.get("/api/v1/repositories/default", headers=self.headers)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "default_repository_missing")

        first = self.client.post("/api/v1/repositories/default", headers=self.headers)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["created"])
        repository = first.json()["repository"]
        self.assertEqual(repository["name"], "Default Repo")
        self.assertTrue(repository["default"])
        self.assertFalse(repository["support_hyperv"])

        second = self.client.post("/api/v1/repositories/default", headers=self.headers)
        self.assertFalse(second.json()["created"])
        self.assertEqual(second.json()["repository"]["id"], repository["id"])

        read = self.client.get("/api/v1/repositories/default", headers=self.headers)
        self.assertEqual(read.json()["id"], repository["id"])
        listed = self.client.get("/api/v1/repositories", headers=self.headers).json()["items"]
        self.assertEqual([item["default"] for item in listed], [True])

    def test_the_default_path_does_not_shadow_a_repository_id(self) -> None:
        created = self.client.post("/api/v1/repositories/default", headers=self.headers).json()["repository"]

        by_id = self.client.get(f"/api/v1/repositories/{created['id']}", headers=self.headers)

        self.assertEqual(by_id.status_code, 200)
        self.assertEqual(by_id.json()["id"], created["id"])

    def test_the_hyperv_switch_through_the_api(self) -> None:
        helper = _Helper()
        key = patch.dict("os.environ", {credential_encryption.KEY_FILE_ENV: str(self.root / "credentials.key")})
        with key, patch.object(default_repository.subprocess, "run", side_effect=helper):
            credential_encryption.reset_cipher()
            self.addCleanup(credential_encryption.reset_cipher)
            on = self.client.post("/api/v1/repositories/default", headers=self.headers, json={"support_hyperv": True})
            again = self.client.post("/api/v1/repositories/default", headers=self.headers, json={"support_hyperv": True})
            off = self.client.post("/api/v1/repositories/default", headers=self.headers, json={"support_hyperv": False})

        self.assertEqual(on.status_code, 200)
        self.assertEqual((on.json()["created"], on.json()["changed"]), (True, True))
        self.assertTrue(on.json()["repository"]["support_hyperv"])
        self.assertRegex(on.json()["repository"]["share"], r"^abr-[a-z0-9]{8}\$$")
        self.assertNotIn(helper.calls[0][1]["password"], on.text)
        self.assertEqual((again.json()["created"], again.json()["changed"]), (False, False))
        self.assertEqual((off.json()["changed"], off.json()["repository"]["support_hyperv"]), (True, False))

    def test_an_error_answers_with_its_code(self) -> None:
        (self.root / "var").write_text("not a folder")

        response = self.client.post("/api/v1/repositories/default", headers=self.headers)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "default_repository_path_unavailable")


if __name__ == "__main__":
    unittest.main()
