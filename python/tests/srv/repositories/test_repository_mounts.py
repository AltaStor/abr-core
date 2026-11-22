# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core import broker_client, credential_encryption, mongo_db_wrapper
from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    TASKS,
)
from python.srv.services import abr_v41, repository_mounts, share_mount
from python.srv.services.api_errors import ApiError, api_error

PASSWORD = "Nas-Secret,with=odd chars"
REAL_MOUNT_IN_BACKGROUND = repository_mounts.mount_in_background


class FakeMountsBroker:
    def __init__(self, root: Path, mountinfo: Path) -> None:
        self.root = root
        self.mountinfo = mountinfo
        self.mounts: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []
        self.refuse: dict[str, broker_client.BrokerRefused | broker_client.BrokerUnavailable] = {}
        self.busy: set[str] = set()
        self.write()

    def write(self) -> None:
        lines = ["20 1 0:20 / / rw - ext4 /dev/sda1 rw", "21 20 0:21 / /run rw - tmpfs tmpfs rw"]
        lines += [f"{30 + index} 21 0:{30 + index} / {point} rw - cifs {source} rw" for index, (point, source) in enumerate(self.mounts.items())]
        self.mountinfo.write_text("\n".join(lines) + "\n")

    def point(self, arguments: dict) -> Path:
        return self.root / f"{arguments['repository_id']}{'.trial' if arguments.get('trial') else ''}"

    def __call__(self, socket_path, operation, arguments, *, timeout):
        self.calls.append((operation, dict(arguments)))
        key = operation + (".trial" if arguments.get("trial") else "")
        if key in self.refuse:
            raise self.refuse[key]
        point = self.point(arguments)
        if operation == "mount":
            source = "//" + "/".join([arguments["server"], arguments["share"], *([arguments["subfolder"]] if arguments.get("subfolder") else [])])
            point.mkdir(exist_ok=True)
            self.mounts[str(point)] = source
            self.write()
            return {"mount_point": str(point), "source": source, "already_mounted": False}
        if str(point) in self.busy:
            raise broker_client.BrokerRefused("busy", f"{point} is in use, so it is not unmounted")
        source = self.mounts.pop(str(point), None)
        self.write()
        if point.exists():
            shutil.rmtree(point)
        return {"mount_point": str(point), "unmounted": source is not None}

    def operations(self) -> list[str]:
        return [operation + (".trial" if arguments.get("trial") else "") for operation, arguments in self.calls]


class _MountsCase(unittest.TestCase):
    def setUp(self) -> None:
        for store in (REPOSITORIES, JOBS, TASKS, HYPERVISORS, BACKUPS):
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in (REPOSITORIES, JOBS, TASKS, HYPERVISORS, BACKUPS)])
        folder = tempfile.TemporaryDirectory(prefix="abr-repository-mounts-")
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        (base / "mounts").mkdir()
        self.broker = FakeMountsBroker(base / "mounts", base / "mountinfo")
        for handle in (
            patch.dict("os.environ", {credential_encryption.KEY_FILE_ENV: str(base / "credentials.key")}),
            patch.object(repository_mounts, "MOUNTS_ROOT", str(base / "mounts")),
            patch.object(share_mount, "MOUNTINFO", base / "mountinfo"),
            patch.object(repository_mounts.broker_client, "call", side_effect=self.broker),
            patch.object(repository_mounts, "mount_in_background"),
        ):
            handle.start()
            self.addCleanup(handle.stop)
        credential_encryption.reset_cipher()
        self.addCleanup(credential_encryption.reset_cipher)
        repository_mounts.STATE.clear()
        self.addCleanup(repository_mounts.STATE.clear)

    def add(self, **changes) -> dict:
        payload = {"name": "NAS", "type": "smb", "address": "\\\\nas.example\\backups\\vm", "username": "backup@corp.local", "password": PASSWORD}
        payload.update(changes)
        return abr_v41.create_repository_record(payload)

    def refused(self, call) -> ApiError:
        with self.assertRaises(ApiError) as caught:
            call()
        return caught.exception

    def mount_arguments(self) -> list[dict]:
        return [arguments for operation, arguments in self.broker.calls if operation == "mount"]


class ShareCheckTests(_MountsCase):
    def check_share(self, **changes) -> dict:
        payload = {"type": "smb", "address": "\\\\nas.example\\backups\\vm", "username": "backup@corp.local", "password": PASSWORD}
        payload.update(changes)
        return abr_v41.test_smb_connection(payload)

    def test_a_share_without_a_path_is_mounted_on_a_trial_point_checked_and_unmounted(self) -> None:
        result = self.check_share()

        self.assertEqual(result["status"], "success")
        self.assertIn("was mounted with this account, written to and unmounted again", result["message"])
        self.assertEqual(self.broker.operations(), ["mount.trial", "unmount.trial"])
        [arguments] = self.mount_arguments()
        self.assertEqual((arguments["server"], arguments["share"], arguments["subfolder"]), ("nas.example", "backups", "vm"))
        self.assertEqual(self.broker.mounts, {})
        self.assertEqual(REPOSITORIES, {})

    def test_a_share_that_does_not_mount_is_refused_as_the_create_call_refuses_it(self) -> None:
        self.broker.refuse["mount.trial"] = broker_client.BrokerRefused("refused", "mount error(13): Permission denied")
        self.broker.refuse["mount"] = broker_client.BrokerRefused("refused", "mount error(13): Permission denied")

        tested = self.refused(self.check_share)
        created = self.refused(self.add)

        self.assertEqual((tested.status_code, tested.code), (created.status_code, created.code))
        self.assertEqual(tested.code, "share_mount_failed")
        self.assertIn("mount error(13): Permission denied", tested.message)
        self.assertEqual(REPOSITORIES, {})

    def test_an_account_is_required_and_nothing_is_mounted_without_one(self) -> None:
        for changes in ({"username": None}, {"password": None}):
            with self.subTest(changes=changes):
                error = self.refused(lambda changes=changes: self.check_share(**changes))
                self.assertIn("needs username and password", error.message)
        self.assertEqual(self.broker.calls, [])

    def test_the_refusals_name_request_fields_not_cli_flags(self) -> None:
        messages = [
            self.refused(lambda: self.add(username=None, password=None)).message,
            self.refused(lambda: self.check_share(username=None)).message,
            self.refused(lambda: self.check_share(address="nas-only")).message,
            self.refused(lambda: self.add(name="NAS-2", address=None)).message,
        ]

        for message in messages:
            with self.subTest(message=message):
                self.assertNotIn("--", message)
        self.assertIn("needs username and password", messages[0])

    def test_the_trial_mount_is_unmounted_when_the_check_breaks(self) -> None:
        with patch.object(abr_v41, "_verify_local_repository", side_effect=RuntimeError("check broke")), self.assertRaises(RuntimeError):
            self.check_share()

        self.assertEqual(self.broker.operations(), ["mount.trial", "unmount.trial"])
        self.assertEqual(self.broker.mounts, {})


class CreateTests(_MountsCase):
    def test_a_share_without_a_path_is_mounted_by_the_server_at_its_own_point(self) -> None:
        created = self.add()

        [arguments] = self.mount_arguments()
        self.assertEqual(
            {key: value for key, value in arguments.items() if key != "repository_id"},
            {"server": "nas.example", "share": "backups", "subfolder": "vm", "username": "backup", "domain": "corp.local", "password": PASSWORD},
        )
        self.assertEqual(arguments["repository_id"], created["id"])
        self.assertEqual(created["path"], str(self.broker.root / created["id"]))
        self.assertEqual(created["status"], "available")
        self.assertTrue(REPOSITORIES[created["id"]]["metadata"]["managed_mount"])
        self.assertEqual(REPOSITORIES[created["id"]]["metadata"]["username"], "backup@corp.local")

    def test_an_account_is_required_and_nothing_is_mounted_without_one(self) -> None:
        for changes in ({"username": None}, {"password": None}):
            with self.subTest(changes=changes):
                error = self.refused(lambda changes=changes: self.add(**changes))
                self.assertIn("needs username and password", error.message)
        self.assertEqual(self.broker.calls, [])

    def test_a_bad_account_or_folder_is_refused_before_the_broker(self) -> None:
        for changes, words in (
            ({"username": "backup@корп"}, "no Cyrillic or other non-Latin characters"),
            ({"username": "Олег@corp"}, "no Cyrillic or other non-Latin characters"),
            ({"username": "backup@corp local"}, "may only hold Latin letters"),
            ({"username": "a@b@c"}, "more than one"),
            ({"address": "\\\\nas\\backups\\..\\etc"}, ". or .. parts"),
        ):
            with self.subTest(changes=changes):
                self.assertIn(words, self.refused(lambda changes=changes: self.add(**changes)).message)
        self.assertEqual(self.broker.calls, [])

    def test_a_share_that_cannot_be_mounted_leaves_no_repository(self) -> None:
        self.broker.refuse["mount"] = broker_client.BrokerRefused("refused", "//nas.example/backups/vm could not be mounted: mount error 13 (Permission denied): the user name or password is wrong")

        error = self.refused(self.add)

        self.assertEqual((error.status_code, error.code), (422, "share_mount_failed"))
        self.assertIn("the user name or password is wrong", error.message)
        self.assertEqual(REPOSITORIES, {})

    def test_an_absent_broker_is_named(self) -> None:
        self.broker.refuse["mount"] = broker_client.BrokerUnavailable("nothing listens on /run/altastor-backup-mounts.sock")

        error = self.refused(self.add)

        self.assertEqual((error.status_code, error.code), (503, "mounts_broker_unavailable"))
        self.assertIn("systemctl status altastor-backup-mounts.socket", error.message)

    def test_a_later_refusal_unmounts_what_was_mounted(self) -> None:
        with patch.object(abr_v41, "_assert_repository_is_usable", side_effect=abr_v41.api_error(422, "x", "probe failed")):
            self.refused(self.add)

        self.assertEqual(self.broker.operations(), ["mount", "unmount"])
        self.assertEqual(self.broker.mounts, {})
        self.assertEqual(REPOSITORIES, {})

    def test_a_taken_name_is_refused_before_mounting(self) -> None:
        self.add()
        self.broker.calls.clear()

        self.assertEqual(self.refused(lambda: self.add(address="\\\\nas.example\\other")).code, "repository_name_exists")
        self.assertEqual(self.broker.calls, [])

    def test_without_a_credential_key_nothing_is_mounted(self) -> None:
        unusable = credential_encryption.CredentialKeyUnavailable("The credential key cannot be used")
        with patch.object(mongo_db_wrapper, "encrypt_secret", side_effect=unusable):
            error = self.refused(self.add)

        self.assertEqual((error.status_code, error.code), (503, "credential_key_unavailable"))
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(REPOSITORIES, {})

    def test_a_share_with_a_path_stays_mounted_by_hand(self) -> None:
        folder = self.broker.root.parent / "by-hand"
        folder.mkdir()

        with patch.object(abr_v41, "share_mount_check", return_value=None):
            created = self.add(path=str(folder))

        self.assertEqual(self.broker.calls, [])
        self.assertNotIn("managed_mount", REPOSITORIES[created["id"]]["metadata"])


class EditTests(_MountsCase):
    def setUp(self) -> None:
        super().setUp()
        self.repository = self.add()
        self.broker.calls.clear()

    def edit(self, **payload) -> dict:
        return abr_v41.edit_repository_record(self.repository["id"], payload)

    def test_a_new_password_is_tried_on_its_own_point_before_the_swap(self) -> None:
        self.edit(password="New-Secret")

        self.assertEqual(self.broker.operations(), ["mount.trial", "unmount.trial", "unmount", "mount"])
        self.assertEqual(self.mount_arguments()[-1]["password"], "New-Secret")
        self.assertEqual(REPOSITORIES[self.repository["id"]]["metadata"]["password"], "New-Secret")
        self.assertTrue(repository_mounts.is_mounted(REPOSITORIES[self.repository["id"]]))

    def test_a_wrong_new_password_leaves_the_repository_and_its_mount_alone(self) -> None:
        self.broker.refuse["mount.trial"] = broker_client.BrokerRefused("refused", "mount error 13 (Permission denied)")

        error = self.refused(lambda: self.edit(password="typo"))

        self.assertEqual(error.code, "share_mount_failed")
        self.assertEqual(self.broker.operations(), ["mount.trial"])
        self.assertEqual(REPOSITORIES[self.repository["id"]]["metadata"]["password"], PASSWORD)
        self.assertTrue(repository_mounts.is_mounted(REPOSITORIES[self.repository["id"]]))

    def test_a_share_without_the_repositorys_backups_is_refused(self) -> None:
        point_dir = Path(self.repository["path"]) / "app-01" / "chain-1" / "task-1"
        BACKUPS["p1"] = abr_v41.normalize_backup_point(
            {
                "id": "p1", "repository_id": self.repository["id"], "vm_id": "vm-1", "vm_name": "app-01", "status": "available",
                "created_at": "2026-11-19T10:00:00+00:00", "execution_type": "ActiveFull",
                "artifact": {"disks": [{"index": 1, "abr_path": str(point_dir), "abr_backup_name": "01-disk.vmdk"}]},
            }
        )

        error = self.refused(lambda: self.edit(address="\\\\nas.example\\elsewhere"))

        self.assertEqual((error.status_code, error.code), (409, "repository_share_mismatch"))
        self.assertEqual(self.broker.operations(), ["mount.trial", "unmount.trial"])
        self.assertEqual(REPOSITORIES[self.repository["id"]]["metadata"]["share"], "backups/vm")

    def test_a_share_that_holds_the_backups_is_taken(self) -> None:
        point_dir = Path(self.repository["path"]) / "app-01" / "chain-1" / "task-1"
        BACKUPS["p1"] = abr_v41.normalize_backup_point(
            {
                "id": "p1", "repository_id": self.repository["id"], "vm_id": "vm-1", "vm_name": "app-01", "status": "available",
                "created_at": "2026-11-19T10:00:00+00:00", "execution_type": "ActiveFull",
                "artifact": {"disks": [{"index": 1, "abr_path": str(point_dir), "abr_backup_name": "01-disk.vmdk"}]},
            }
        )
        original_call = self.broker.__call__

        def with_backups(socket_path, operation, arguments, *, timeout):
            reply = original_call(socket_path, operation, arguments, timeout=timeout)
            if operation == "mount" and arguments.get("trial"):
                (Path(reply["mount_point"]) / "app-01" / "chain-1" / "task-1").mkdir(parents=True)
            return reply

        with patch.object(repository_mounts.broker_client, "call", side_effect=with_backups):
            self.edit(address="\\\\192.168.1.20\\backups\\vm")

        self.assertEqual(REPOSITORIES[self.repository["id"]]["metadata"]["server"], "192.168.1.20")

    def test_a_change_is_refused_while_sessions_use_the_repository(self) -> None:
        TASKS["t1"] = {"id": "t1", "status": "running", "task_type": "backup", "parameters": {"repository_id": self.repository["id"]}}

        error = self.refused(lambda: self.edit(password="New-Secret"))

        self.assertEqual(error.status_code, 409)
        self.assertEqual(self.broker.calls, [])

    def test_a_change_that_touches_no_mount_field_does_not_remount(self) -> None:
        self.edit(description="NAS in rack 2")

        self.assertEqual(self.broker.calls, [])

    def test_a_path_cannot_be_given_to_a_share_the_server_mounts(self) -> None:
        error = self.refused(lambda: self.edit(path="/mnt/elsewhere"))

        self.assertIn("takes no path", error.message)

    def test_a_share_in_use_is_not_swapped(self) -> None:
        self.broker.busy.add(self.repository["path"])

        error = self.refused(lambda: self.edit(password="New-Secret"))

        self.assertEqual(error.code, "repository_share_busy")
        self.assertEqual(REPOSITORIES[self.repository["id"]]["metadata"]["password"], PASSWORD)


    def test_a_new_password_that_cannot_be_stored_is_refused_before_the_share_is_touched(self) -> None:
        unusable = credential_encryption.CredentialKeyUnavailable("The credential key cannot be used")
        with patch.object(mongo_db_wrapper, "encrypt_secret", side_effect=unusable):
            error = self.refused(lambda: self.edit(password="New-Secret"))

        self.assertEqual(error.code, "credential_key_unavailable")
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(REPOSITORIES[self.repository["id"]]["metadata"]["password"], PASSWORD)
        self.assertTrue(repository_mounts.is_mounted(REPOSITORIES[self.repository["id"]]))

    def test_without_a_credential_key_an_edit_that_keeps_the_secret_is_saved(self) -> None:
        unusable = credential_encryption.CredentialKeyUnavailable("The credential key cannot be used")
        with patch.object(mongo_db_wrapper, "encrypt_secret", side_effect=unusable):
            self.edit(description="NAS in rack 2")

        self.assertEqual(REPOSITORIES[self.repository["id"]]["metadata"]["description"], "NAS in rack 2")
        self.assertEqual(self.broker.calls, [])

    def test_an_edit_refused_after_the_swap_mounts_the_old_settings_again(self) -> None:
        refusal = api_error(503, "credential_key_unavailable", "The credential key cannot be used")
        with patch.object(abr_v41, "persist_repository", side_effect=refusal):
            error = self.refused(lambda: self.edit(password="New-Secret"))

        self.assertEqual(error.code, "credential_key_unavailable")
        self.assertEqual(self.broker.operations(), ["mount.trial", "unmount.trial", "unmount", "mount", "unmount", "mount"])
        self.assertEqual(self.mount_arguments()[-1]["password"], PASSWORD)
        self.assertEqual(REPOSITORIES[self.repository["id"]]["metadata"]["password"], PASSWORD)
        self.assertTrue(repository_mounts.is_mounted(REPOSITORIES[self.repository["id"]]))
        self.assertNotIn(self.repository["id"], repository_mounts.STATE)

    def test_a_check_that_refuses_the_new_share_leaves_the_old_one_mounted(self) -> None:
        refusal = api_error(422, "invalid_repository", "The Hyper-V host refused the account")
        with patch.object(abr_v41, "_assert_repository_is_usable", side_effect=refusal):
            self.refused(lambda: self.edit(address="\\\\192.168.1.20\\backups\\vm"))

        record = REPOSITORIES[self.repository["id"]]
        self.assertEqual(record["metadata"]["server"], "nas.example")
        self.assertEqual(repository_mounts.current_mount(record).source, "//nas.example/backups/vm")

    def test_old_settings_that_no_longer_mount_are_named_and_retried(self) -> None:
        original_call = self.broker.__call__
        mounts = []

        def old_password_rejected(socket_path, operation, arguments, *, timeout):
            if operation == "mount" and not arguments.get("trial"):
                mounts.append(arguments["password"])
                if arguments["password"] == PASSWORD:
                    raise broker_client.BrokerRefused("refused", "mount error 13 (Permission denied)")
            return original_call(socket_path, operation, arguments, timeout=timeout)

        refusal = api_error(503, "credential_key_unavailable", "The credential key cannot be used")
        with (
            patch.object(repository_mounts.broker_client, "call", side_effect=old_password_rejected),
            patch.object(abr_v41, "persist_repository", side_effect=refusal),
        ):
            error = self.refused(lambda: self.edit(password="New-Secret"))

        self.assertEqual(error.code, "credential_key_unavailable")
        self.assertEqual(mounts, ["New-Secret", PASSWORD])
        self.assertIn("Permission denied", repository_mounts.STATE[self.repository["id"]]["error"])
        repository_mounts.mount_in_background.assert_called_once()


class RemoveTests(_MountsCase):
    def test_removing_a_repository_unmounts_its_share(self) -> None:
        repository = self.add()

        abr_v41.remove_repository_record(repository["id"])

        self.assertEqual(self.broker.mounts, {})
        self.assertNotIn(repository["id"], REPOSITORIES)

    def test_a_share_in_use_keeps_its_repository(self) -> None:
        repository = self.add()
        self.broker.busy.add(repository["path"])

        error = self.refused(lambda: abr_v41.remove_repository_record(repository["id"]))

        self.assertEqual((error.status_code, error.code), (409, "repository_share_busy"))
        self.assertIn(repository["id"], REPOSITORIES)


class StartupTests(_MountsCase):
    def setUp(self) -> None:
        super().setUp()
        self.repository = self.add()
        self.broker.calls.clear()

    def test_a_share_still_mounted_is_kept(self) -> None:
        repository_mounts.mount_all_once()

        self.assertEqual(self.broker.calls, [])

    def test_a_share_mounted_from_elsewhere_is_mounted_again(self) -> None:
        self.broker.mounts[self.repository["path"]] = "//old-nas/backups/vm"
        self.broker.write()

        repository_mounts.mount_all_once()

        self.assertEqual(self.broker.operations(), ["unmount", "mount"])
        self.assertEqual(self.broker.mounts[self.repository["path"]], "//nas.example/backups/vm")

    def test_a_missing_share_is_mounted_and_retried_with_growing_pauses(self) -> None:
        self.broker.mounts.clear()
        self.broker.write()
        self.broker.refuse["mount"] = broker_client.BrokerRefused("refused", "mount error 113 (No route to host): the server cannot be reached")
        pauses: list[int] = []

        def sleep(seconds: int) -> None:
            pauses.append(seconds)
            if len(pauses) == 3:
                self.broker.refuse.clear()

        repository_mounts._keep_mounting(sleep)

        self.assertEqual(pauses, [30, 60, 120])
        self.assertTrue(repository_mounts.is_mounted(REPOSITORIES[self.repository["id"]]))
        self.assertIsNone(repository_mounts.problem(self.repository["id"]))

    def test_until_then_the_repository_is_unavailable_with_the_reason(self) -> None:
        self.broker.mounts.clear()
        self.broker.write()
        self.broker.refuse["mount"] = broker_client.BrokerRefused("refused", "mount error 113 (No route to host): the server cannot be reached")
        repository_mounts.mount_all_once()

        listed = abr_v41.normalize_repository_record(REPOSITORIES[self.repository["id"]])

        self.assertEqual(listed["status"], "unavailable")
        self.assertIn("The share " + chr(92) * 2 + "nas.example" + chr(92) + "backups" + chr(92) + "vm of repository NAS is not mounted at", listed["storage"]["error"])
        self.assertIn("the server cannot be reached", listed["storage"]["error"])
        self.assertIn("This server mounts it itself and keeps trying", listed["storage"]["error"])

    def test_a_password_this_server_cannot_read_is_named(self) -> None:
        self.broker.mounts.clear()
        self.broker.write()
        REPOSITORIES[self.repository["id"]]["metadata"]["password"] = credential_encryption.ENCRYPTED_PREFIX + "unreadable"

        failures = repository_mounts.mount_all_once()

        self.assertIn("cannot be decrypted with this server's credential key", failures[self.repository["id"]])
        self.assertEqual(self.broker.calls, [])


class RetryAfterOutageTests(_MountsCase):
    def test_a_failed_mount_on_demand_starts_the_retries_once(self) -> None:
        repository = self.add()
        self.broker.mounts.clear()
        self.broker.write()
        self.broker.refuse["mount"] = broker_client.BrokerRefused("refused", "mount error 113 (No route to host)")

        with patch.object(repository_mounts, "mount_in_background") as retries:
            for _ in range(2):
                with self.assertRaises(ApiError):
                    repository_mounts.ensure_mounted(REPOSITORIES[repository["id"]])

        self.assertEqual(retries.call_count, 2)

    def test_only_one_retrier_runs_at_a_time(self) -> None:
        release = threading.Event()

        with patch.object(repository_mounts, "_keep_mounting", side_effect=lambda: release.wait(5)):
            first = REAL_MOUNT_IN_BACKGROUND()
            second = REAL_MOUNT_IN_BACKGROUND()
            release.set()
            first.join(5)

        self.assertIs(first, second)
        self.assertNotIn("thread", repository_mounts.RETRIER)

    def test_a_share_in_use_starts_no_retries(self) -> None:
        repository = self.add()
        self.broker.mounts[repository["path"]] = "//old/share"
        self.broker.write()
        self.broker.busy.add(repository["path"])

        with patch.object(repository_mounts, "mount_in_background") as retries, self.assertRaises(ApiError):
            repository_mounts.ensure_mounted(REPOSITORIES[repository["id"]])

        retries.assert_not_called()


class EnsureMountedTests(_MountsCase):
    def test_a_session_mounts_a_share_that_went_away(self) -> None:
        repository = self.add()
        self.broker.mounts.clear()
        self.broker.write()

        share_mount.ensure_share_mounted(REPOSITORIES[repository["id"]])

        self.assertTrue(repository_mounts.is_mounted(REPOSITORIES[repository["id"]]))

    def test_a_share_that_cannot_be_mounted_stops_the_session_with_the_reason(self) -> None:
        repository = self.add()
        self.broker.mounts.clear()
        self.broker.write()
        self.broker.refuse["mount"] = broker_client.BrokerRefused("refused", "mount error 112 (Host is down): the server is down")

        with self.assertRaises(share_mount.ShareNotMountedError) as caught:
            share_mount.ensure_share_mounted(REPOSITORIES[repository["id"]])
        self.assertIn("the server is down", str(caught.exception))

    def test_an_api_check_mounts_it_too(self) -> None:
        repository = self.add()
        self.broker.mounts.clear()
        self.broker.write()

        abr_v41._ensure_share_mounted(REPOSITORIES[repository["id"]])

        self.assertTrue(repository_mounts.is_mounted(REPOSITORIES[repository["id"]]))


    def test_a_repository_check_mounts_a_share_that_went_away(self) -> None:
        repository = self.add()
        self.broker.mounts.clear()
        self.broker.write()
        shutil.rmtree(repository["path"])

        result = abr_v41.verify_repository(repository["id"])

        self.assertNotIn("Path does not exist", str(result.get("message")))
        self.assertTrue(repository_mounts.is_mounted(REPOSITORIES[repository["id"]]))

    def test_a_repository_check_names_why_the_share_cannot_be_mounted(self) -> None:
        repository = self.add()
        self.broker.mounts.clear()
        self.broker.write()
        shutil.rmtree(repository["path"])
        self.broker.refuse["mount"] = broker_client.BrokerRefused("refused", "mount error 112 (Host is down): the server is down")

        result = abr_v41.verify_repository(repository["id"])

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["checks"], [{"name": "mounted", "status": "failed"}])
        self.assertIn("the server is down", result["message"])


class LostServerTests(unittest.TestCase):
    def test_only_an_smb_repository_whose_server_does_not_answer_is_called_lost(self) -> None:
        smb = {"id": "r1", "name": "nas", "type": "smb", "path": "/run/altastor-backup-mounts/r1", "metadata": {"server": "nas.example"}}
        with patch.object(repository_mounts, "server_answers", return_value=False):
            self.assertIn("stopped answering", repository_mounts.share_lost_message(smb) or "")
            self.assertIsNone(repository_mounts.share_lost_message({"type": "local", "path": "/srv"}))
            self.assertIsNone(repository_mounts.share_lost_message({"type": "smb", "metadata": {}}))
        with patch.object(repository_mounts, "server_answers", return_value=True):
            self.assertIsNone(repository_mounts.share_lost_message(smb))

    def test_a_synthetic_full_names_the_lost_server_too(self) -> None:
        from python.srv.services import synthetic_full

        smb = {"id": "r1", "name": "nas", "type": "smb", "path": "/run/altastor-backup-mounts/r1", "metadata": {"server": "nas.example"}}
        with patch.object(repository_mounts, "server_answers", return_value=False):
            self.assertIn("stopped answering", synthetic_full._out_of_space_message(smb, 0))
            self.assertIn("stopped answering", synthetic_full._no_room_detail(smb, [], None) or "")

    def test_the_server_is_asked_on_445_then_139(self) -> None:
        tried = []

        def connect(address, timeout):
            tried.append(address[1])
            raise OSError("refused")

        with patch.object(repository_mounts.socket, "create_connection", side_effect=connect):
            self.assertFalse(repository_mounts.server_answers("nas.example"))
        self.assertEqual(tried, [445, 139])


if __name__ == "__main__":
    unittest.main()
