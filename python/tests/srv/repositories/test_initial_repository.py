# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core import broker_client, credential_encryption
from python.srv.core.mongo_db_wrapper import REPOSITORIES
from python.srv.plugins.hyperv import vhdx_tool
from python.srv.services import (
    abr_v41,
    default_repository,
    initial_repository,
    repository_shares,
)
from python.srv.services.api_errors import ApiError


class _Broker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str], str]] = []
        self.answer: BaseException | None = None

    def __call__(self, socket_path, operation, arguments, *, timeout):
        self.calls.append((operation, dict(arguments), socket_path))
        if self.answer is not None:
            raise self.answer
        return {}


class _InitialRepositoryCase(unittest.TestCase):
    def setUp(self) -> None:
        REPOSITORIES.clear()
        self.addCleanup(REPOSITORIES.clear)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "var" / "lib" / "altastor-backup" / "repository"
        environment = patch.dict(
            "os.environ",
            {
                initial_repository.PATH_ENV: str(self.path),
                credential_encryption.KEY_FILE_ENV: str(self.root / "credentials.key"),
                repository_shares.SHARES_CONF_ENV: str(self.root / "no-shares.conf"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        credential_encryption.reset_cipher()
        self.addCleanup(credential_encryption.reset_cipher)
        self.helper = _Broker()
        call = patch.object(repository_shares.broker_client, "call", side_effect=self.helper)
        call.start()
        self.addCleanup(call.stop)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        for name in ("REPO_ROOT", "APP_ROOT"):
            installed = patch.object(initial_repository, name, self.checkout)
            installed.start()
            self.addCleanup(installed.stop)

    def _folder(self, name: str) -> str:
        folder = self.root / name
        folder.mkdir(mode=0o750)
        return str(folder)


class EnsureInitialRepositoryTests(_InitialRepositoryCase):
    def test_a_missing_initial_repository_is_created_with_its_folder_shared_and_the_default(self) -> None:
        repository, created = initial_repository.ensure_initial_repository()

        self.assertTrue(created)
        self.assertEqual(repository["name"], "Initial Repo")
        self.assertEqual(repository["path"], str(self.path))
        self.assertTrue(repository["initial"])
        self.assertTrue(repository["default"])
        self.assertEqual((repository["type"], repository["support_hyperv"]), ("smb", True))
        self.assertEqual(self.helper.calls[0][0], "add-share")
        self.assertEqual(self.helper.calls[0][1]["path"], str(self.path))
        self.assertEqual(repository["status"], "available")
        self.assertTrue(self.path.is_dir())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o750)
        self.assertEqual(len(REPOSITORIES), 1)

    def test_without_the_broker_it_is_created_unshared_and_the_reason_is_logged(self) -> None:
        self.helper.answer = broker_client.BrokerUnavailable("nothing listens on /run/altastor-backup-shares.sock")

        with self.assertLogs(initial_repository.logger, "WARNING") as logs:
            repository, created = initial_repository.ensure_initial_repository()

        self.assertTrue(created)
        self.assertEqual((repository["type"], repository["support_hyperv"]), ("local", False))
        self.assertIn("The Initial Repo is not shared", logs.output[-1])

    def test_it_is_not_the_default_when_another_repository_already_is(self) -> None:
        other = abr_v41.create_repository_record({"name": "Lab", "type": "local", "path": self._folder("lab")})
        default_repository.set_default_repository(other["id"], True)

        repository, _created = initial_repository.ensure_initial_repository()

        self.assertTrue(repository["initial"])
        self.assertFalse(repository["default"])

    def test_an_existing_initial_repository_is_kept_and_no_second_one_is_made(self) -> None:
        first, _created = initial_repository.ensure_initial_repository()

        again, created = initial_repository.ensure_initial_repository()

        self.assertFalse(created)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(REPOSITORIES), 1)

    def test_a_renamed_initial_repository_is_still_the_initial_one(self) -> None:
        first, _created = initial_repository.ensure_initial_repository()
        abr_v41.edit_repository_record(first["id"], {"name": "Main", "description": "Our backups"})

        again, created = initial_repository.ensure_initial_repository()

        self.assertFalse(created)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["name"], "Main")
        self.assertTrue(again["initial"])
        self.assertEqual(len(REPOSITORIES), 1)

    def test_a_deleted_initial_repository_is_created_again(self) -> None:
        first, _created = initial_repository.ensure_initial_repository()
        abr_v41.remove_repository_record(first["id"])

        again, created = initial_repository.ensure_initial_repository()

        self.assertTrue(created)
        self.assertNotEqual(again["id"], first["id"])
        self.assertEqual(len(REPOSITORIES), 1)

    def test_a_repository_named_like_it_at_its_folder_becomes_the_initial_one(self) -> None:
        self.path.mkdir(mode=0o750, parents=True)
        own = abr_v41.create_repository_record({"name": "Initial Repo", "type": "local", "path": str(self.path)})

        repository, created = initial_repository.ensure_initial_repository()

        self.assertFalse(created)
        self.assertEqual(repository["id"], own["id"])
        self.assertTrue(repository["initial"])
        self.assertTrue(repository["default"])
        self.assertEqual(len(REPOSITORIES), 1)

    def test_a_repository_of_that_name_elsewhere_is_left_alone(self) -> None:
        other = abr_v41.create_repository_record(
            {"name": "Initial Repo", "type": "local", "path": self._folder("elsewhere")}
        )

        with self.assertRaises(ApiError) as caught:
            initial_repository.ensure_initial_repository()

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.code, "initial_repository_name_taken")
        self.assertIn("Rename it", caught.exception.message)
        self.assertEqual(list(REPOSITORIES), [other["id"]])
        self.assertFalse(initial_repository.is_initial_repository(REPOSITORIES[other["id"]]))
        self.assertFalse(self.path.exists())

    def test_a_folder_that_cannot_be_created_is_refused_with_the_setting_to_change(self) -> None:
        blocker = self.root / "var"
        blocker.write_text("not a folder")

        with self.assertRaises(ApiError) as caught:
            initial_repository.ensure_initial_repository()

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.code, "initial_repository_path_unavailable")
        self.assertIn(initial_repository.PATH_ENV, caught.exception.message)
        self.assertEqual(list(REPOSITORIES), [])

    def test_an_existing_folder_is_used_as_it_is(self) -> None:
        self.path.mkdir(mode=0o750, parents=True)
        (self.path / "kept.txt").write_text("data")

        _repository, created = initial_repository.ensure_initial_repository()

        self.assertTrue(created)
        self.assertEqual((self.path / "kept.txt").read_text(), "data")

    def test_the_marks_are_not_listed_as_settable_and_survive_an_edit(self) -> None:
        first, _created = initial_repository.ensure_initial_repository()

        edited = abr_v41.edit_repository_record(first["id"], {"description": "Changed"})

        self.assertTrue(edited["initial"])
        self.assertTrue(edited["default"])
        self.assertTrue(edited["support_hyperv"])

    def test_a_repository_created_through_the_api_is_never_the_initial_one(self) -> None:
        repository = abr_v41.create_repository_record(
            {"name": "Lab", "type": "local", "path": self._folder("lab"), "default_repository": True, "initial_repository": True}
        )

        self.assertFalse(repository["initial"])
        self.assertFalse(repository["default"])
        self.assertIsNone(initial_repository.find_initial_repository())


class DefaultRepositoryTests(_InitialRepositoryCase):
    def test_setting_a_default_moves_the_mark_and_the_initial_repository_stays_initial(self) -> None:
        first, _created = initial_repository.ensure_initial_repository()
        other = abr_v41.create_repository_record({"name": "Lab", "type": "local", "path": self._folder("lab")})

        moved = default_repository.set_default_repository(other["id"], True)

        self.assertTrue(moved["default"])
        initial = abr_v41.resolve_repository(record_id=first["id"])
        self.assertEqual((initial["default"], initial["initial"]), (False, True))
        self.assertEqual(default_repository.find_default_repository()["id"], other["id"])

    def test_a_repository_made_before_the_initial_mark_is_still_read_as_the_initial_one(self) -> None:
        older = abr_v41.create_repository_record(
            {"name": "Default Repo", "type": "local", "path": self._folder("older")},
            metadata_flags={"default_repository": True, "support_hyperv": False},
        )
        other = abr_v41.create_repository_record({"name": "Lab", "type": "local", "path": self._folder("lab")})

        self.assertTrue(older["initial"])
        default_repository.set_default_repository(other["id"], True)

        self.assertTrue(initial_repository.is_initial_repository(REPOSITORIES[older["id"]]))
        self.assertFalse(default_repository.is_default_repository(REPOSITORIES[older["id"]]))

    def test_false_leaves_no_default(self) -> None:
        first, _created = initial_repository.ensure_initial_repository()

        cleared = default_repository.set_default_repository(first["id"], False)

        self.assertFalse(cleared["default"])
        self.assertIsNone(default_repository.find_default_repository())


class DevCheckoutTests(_InitialRepositoryCase):
    def setUp(self) -> None:
        super().setUp()
        checkout = patch.object(initial_repository, "APP_ROOT", self.root / "opt" / "altastor-backup")
        checkout.start()
        self.addCleanup(checkout.stop)

    def test_a_checkout_that_cannot_create_the_folder_uses_one_of_its_own(self) -> None:
        (self.root / "var").write_text("not a folder")

        with self.assertLogs(initial_repository.logger, "WARNING") as logs:
            repository, created = initial_repository.ensure_initial_repository()

        self.assertTrue(created)
        self.assertEqual(repository["path"], str(self.checkout / "backups" / "default-repo"))
        self.assertTrue((self.checkout / "backups" / "default-repo").is_dir())
        self.assertIn("Expected in a dev checkout", logs.output[0])

    def test_a_checkout_that_can_create_the_folder_uses_it(self) -> None:
        repository, _created = initial_repository.ensure_initial_repository()

        self.assertEqual(repository["path"], str(self.path))
        self.assertFalse((self.checkout / "backups").exists())

    def test_an_install_never_puts_the_repository_next_to_the_program(self) -> None:
        installed = patch.object(initial_repository, "APP_ROOT", self.checkout)
        installed.start()
        self.addCleanup(installed.stop)
        (self.root / "var").write_text("not a folder")

        with self.assertRaises(ApiError) as caught:
            initial_repository.ensure_initial_repository()

        self.assertEqual(caught.exception.code, "initial_repository_path_unavailable")
        self.assertFalse((self.checkout / "backups").exists())


class StartupTests(_InitialRepositoryCase):
    def test_startup_creates_the_initial_repository(self) -> None:
        with patch.object(initial_repository, "get_database", return_value=object()):
            initial_repository.create_initial_repository_at_startup()

        self.assertIsNotNone(initial_repository.find_initial_repository())

    def test_startup_renames_a_default_repo_of_an_older_version(self) -> None:
        self.path.mkdir(mode=0o750, parents=True)
        older = abr_v41.create_repository_record(
            {"name": "Default Repo", "type": "local", "path": str(self.path)},
            metadata_flags={"default_repository": True, "support_hyperv": False},
        )

        with patch.object(initial_repository, "get_database", return_value=object()):
            initial_repository.create_initial_repository_at_startup()

        self.assertEqual(list(REPOSITORIES), [older["id"]])
        self.assertEqual(REPOSITORIES[older["id"]]["name"], "Initial Repo")
        self.assertEqual(self.helper.calls, [])

    def test_a_default_repo_the_operator_renamed_keeps_its_name(self) -> None:
        older = abr_v41.create_repository_record(
            {"name": "Main", "type": "local", "path": self._folder("main")},
            metadata_flags={"default_repository": True, "support_hyperv": False},
        )

        with patch.object(initial_repository, "get_database", return_value=object()):
            initial_repository.create_initial_repository_at_startup()

        self.assertEqual(REPOSITORIES[older["id"]]["name"], "Main")

    def test_startup_without_the_catalog_creates_nothing(self) -> None:
        with (
            patch.object(initial_repository, "get_database", return_value=None),
            self.assertLogs(initial_repository.logger, "WARNING") as logs,
        ):
            initial_repository.create_initial_repository_at_startup()

        self.assertEqual(list(REPOSITORIES), [])
        self.assertIn("catalog database is unavailable", logs.output[0])

    def test_an_initial_repository_that_cannot_be_made_is_logged_and_startup_goes_on(self) -> None:
        (self.root / "var").write_text("not a folder")

        with (
            patch.object(initial_repository, "get_database", return_value=object()),
            self.assertLogs(initial_repository.logger, "WARNING") as logs,
        ):
            initial_repository.create_initial_repository_at_startup()

        self.assertEqual(list(REPOSITORIES), [])
        self.assertIn("could not be created", logs.output[0])


class HyperVHostTests(_InitialRepositoryCase):
    def setUp(self) -> None:
        super().setUp()
        self.repository, _created = initial_repository.ensure_initial_repository()

    def _stored(self) -> dict:
        return REPOSITORIES[self.repository["id"]]

    def test_hosts_get_the_address_this_appliance_reaches_them_from_and_the_decrypted_password(self) -> None:
        password = self.helper.calls[0][1]["password"]

        with patch.object(vhdx_tool, "appliance_address_toward", return_value="10.0.0.5") as toward:
            repository = vhdx_tool.for_hyperv_host(self._stored(), "hv01.lab")
        _args, env = vhdx_tool.storage_invocation(repository, abr_path=str(self.path / "job" / "vm"))

        toward.assert_called_once_with("hv01.lab")
        self.assertEqual(env["ABR_SMB_SHARE"], f"\\\\10.0.0.5\\{self._stored()['metadata']['share']}")
        self.assertEqual(env["ABR_SMB_USER"], self._stored()["metadata"]["username"])
        self.assertEqual(env["ABR_SMB_PASSWORD"], password)

    def test_other_repositories_are_passed_on_untouched(self) -> None:
        other = REPOSITORIES[abr_v41.create_repository_record({"name": "Lab", "type": "local", "path": self._folder("lab")})["id"]]

        with patch.object(vhdx_tool, "appliance_address_toward") as toward:
            self.assertIs(vhdx_tool.for_hyperv_host(other, "hv01.lab"), other)

        toward.assert_not_called()

    def test_a_password_this_appliance_cannot_decrypt_stops_the_run(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
