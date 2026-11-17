# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import json
import logging
import os
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from python.srv.brokers import broker_core, shares_broker
from python.srv.core import broker_client, credential_encryption
from python.srv.core.mongo_db_wrapper import HYPERVISORS, JOBS, REPOSITORIES, TASKS
from python.srv.services import abr_v41, rbac, repository_shares
from python.srv.services.api_errors import ApiError
from python.tests.srv.brokers.fake_samba import SMB_CONF_TEXT, current_user, make_system
from python.tests.srv.rbac.rbac_case import RbacApiCase

SHARE_NAME = r"^abr-[a-z0-9]{8}\$$"
USER_NAME = r"^abr-smb-[a-z0-9]{8}$"


class _Broker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.error: Exception | None = None

    def __call__(self, socket_path, operation, arguments, *, timeout):
        self.calls.append((operation, dict(arguments)))
        if self.error is not None:
            raise self.error
        return {"share": arguments.get("share"), "user": arguments.get("user")}


class _Catalog(unittest.TestCase):
    def setUp(self) -> None:
        for store in (REPOSITORIES, JOBS, TASKS, HYPERVISORS):
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in (REPOSITORIES, JOBS, TASKS, HYPERVISORS)])
        folder = tempfile.TemporaryDirectory(prefix="abr-repository-shares-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        environment = patch.dict(
            "os.environ",
            {credential_encryption.KEY_FILE_ENV: str(self.root / "credentials.key"), repository_shares.SHARES_CONF_ENV: str(self.root / "no-shares.conf")},
        )
        environment.start()
        self.addCleanup(environment.stop)
        credential_encryption.reset_cipher()
        self.addCleanup(credential_encryption.reset_cipher)
        self.folder = self.root / "repo1"
        self.folder.mkdir()
        self.repository = abr_v41.create_repository_record({"name": "Local-Repo-1", "type": "local", "path": str(self.folder)})
        HYPERVISORS["hv"] = {"id": "hv", "name": "Hyper-V", "kind": "hyperv", "type": "hyper-v"}
        HYPERVISORS["vc"] = {"id": "vc", "name": "vCenter", "kind": "vcenter", "type": "vsphere"}

    def stored(self) -> dict:
        return REPOSITORIES[self.repository["id"]]

    def refused(self, call) -> ApiError:
        with self.assertRaises(ApiError) as caught:
            call()
        return caught.exception


class _SharesCase(_Catalog):
    def setUp(self) -> None:
        super().setUp()
        self.broker = _Broker()
        call = patch.object(repository_shares.broker_client, "call", side_effect=self.broker)
        call.start()
        self.addCleanup(call.stop)

    def share(self) -> dict:
        return repository_shares.share_repository(self.stored())

    def unshare(self) -> dict:
        return repository_shares.unshare_repository(self.stored())


class ShareTests(_SharesCase):
    def test_a_local_repository_is_shared_with_generated_names_and_an_encrypted_password(self) -> None:
        shared = self.share()

        [(operation, arguments)] = self.broker.calls
        self.assertEqual(operation, "add-share")
        self.assertRegex(arguments["share"], SHARE_NAME)
        self.assertRegex(arguments["user"], USER_NAME)
        self.assertRegex(arguments["password"], r"^[A-Za-z0-9]{40}$")
        self.assertEqual(arguments["path"], os.path.realpath(self.folder))
        metadata = self.stored()["metadata"]
        self.assertEqual((self.stored()["type"], metadata["support_hyperv"]), ("smb", True))
        self.assertEqual((metadata["share"], metadata["username"]), (arguments["share"], arguments["user"]))
        self.assertNotEqual(metadata["password"], arguments["password"])
        self.assertEqual(credential_encryption.decrypt_secret(metadata["password"]), arguments["password"])
        self.assertEqual(repository_shares.network_path(shared), f"\\\\{metadata['server']}\\{arguments['share']}")

    def test_a_shared_repository_is_not_shared_again(self) -> None:
        self.share()

        self.share()

        self.assertEqual([operation for operation, _ in self.broker.calls], ["add-share"])

    def test_a_repository_that_is_not_local_is_refused(self) -> None:
        REPOSITORIES[self.repository["id"]] = {**self.stored(), "type": "s3"}

        error = self.refused(self.share)

        self.assertEqual((error.status_code, error.code), (409, "repository_not_local"))
        self.assertEqual(self.broker.calls, [])

    def test_without_a_credential_key_nothing_is_created(self) -> None:
        with patch.object(
            repository_shares, "encrypt_secret", side_effect=credential_encryption.CredentialKeyUnavailable("The credential key cannot be read")
        ):
            error = self.refused(self.share)

        self.assertEqual(error.code, "credential_key_unavailable")
        self.assertEqual(self.broker.calls, [])

    def test_a_share_the_catalog_cannot_record_is_removed_again(self) -> None:
        with patch.object(repository_shares, "persist_repository", side_effect=RuntimeError("catalog down")), self.assertRaises(RuntimeError):
            self.share()

        self.assertEqual([operation for operation, _ in self.broker.calls], ["add-share", "remove-share"])
        self.assertEqual(self.broker.calls[1][1], {"share": self.broker.calls[0][1]["share"], "user": self.broker.calls[0][1]["user"]})
        self.assertEqual(self.stored()["type"], "local")

    def test_the_password_is_never_logged(self) -> None:
        with self.assertLogs("python.srv", level=logging.DEBUG) as logs:
            self.share()
            self.unshare()

        password = self.broker.calls[0][1]["password"]
        self.assertTrue(logs.output)
        self.assertFalse(any(password in line for line in logs.output))


class LeftBehindShareTests(_SharesCase):
    def setUp(self) -> None:
        super().setUp()
        self.conf = self.root / "samba-shares.conf"
        environment = patch.dict("os.environ", {repository_shares.SHARES_CONF_ENV: str(self.conf)})
        environment.start()
        self.addCleanup(environment.stop)

    def configure(self, share: str, user: str, path: str) -> None:
        with self.conf.open("a") as handle:
            handle.write(f"[{share}]\n    path = {path}\n    valid users = {user}\n\n")

    def test_a_share_left_behind_on_the_same_folder_is_removed_before_the_new_one(self) -> None:
        self.configure("abr-orphan01$", "abr-smb-orphan01", os.path.realpath(self.folder))

        self.share()

        self.assertEqual([operation for operation, _ in self.broker.calls], ["remove-share", "add-share"])
        self.assertEqual(self.broker.calls[0][1], {"share": "abr-orphan01$", "user": "abr-smb-orphan01"})
        self.assertTrue(repository_shares.is_shared(self.stored()))

    def test_shares_of_other_folders_or_recorded_by_a_repository_are_left_alone(self) -> None:
        other = self.root / "other"
        other.mkdir()
        recorded = abr_v41.create_repository_record({"name": "Other", "type": "local", "path": str(other)})
        repository_shares.share_repository(REPOSITORIES[recorded["id"]])
        kept = REPOSITORIES[recorded["id"]]["metadata"]
        self.configure(kept["share"], kept["username"], os.path.realpath(self.folder))
        self.configure("abr-elsewh01$", "abr-smb-elsewh01", "/srv/elsewhere")
        self.broker.calls.clear()

        self.share()

        self.assertEqual([operation for operation, _ in self.broker.calls], ["add-share"])

    def test_a_shares_file_that_cannot_be_read_does_not_stop_the_share(self) -> None:
        self.conf.mkdir()

        with self.assertLogs("python.srv", level=logging.WARNING) as logs:
            self.share()

        self.assertIn("cannot be read", "\n".join(logs.output))
        self.assertEqual([operation for operation, _ in self.broker.calls], ["add-share"])

    def test_a_left_behind_share_that_cannot_be_removed_stops_the_new_one(self) -> None:
        self.configure("abr-orphan01$", "abr-smb-orphan01", os.path.realpath(self.folder))
        self.broker.error = broker_client.BrokerRefused("refused", "the system account abr-smb-orphan01 is not one AltaStor Backup made")

        error = self.refused(self.share)

        self.assertEqual(error.code, "repository_share_refused")
        self.assertEqual([operation for operation, _ in self.broker.calls], ["remove-share"])
        self.assertFalse(repository_shares.is_shared(self.stored()))


class RestoreTests(_SharesCase):
    def setUp(self) -> None:
        super().setUp()
        self.conf = self.root / "samba-shares.conf"
        environment = patch.dict("os.environ", {repository_shares.SHARES_CONF_ENV: str(self.conf)})
        environment.start()
        self.addCleanup(environment.stop)
        self.share()
        self.metadata = dict(self.stored()["metadata"])
        self.password = self.broker.calls[0][1]["password"]
        self.broker.calls.clear()

    def configure(self, share: str) -> None:
        self.conf.write_text(f"[{share}]\n    path = {os.path.realpath(self.folder)}\n    valid users = {self.metadata['username']}\n")

    def test_a_share_samba_no_longer_has_is_restored_with_the_stored_names_and_password(self) -> None:
        with self.assertLogs("python.srv", level=logging.INFO) as logs:
            outcome = repository_shares.restore_shares()

        self.assertEqual(outcome, {"restored": ["Local-Repo-1"], "reset": [], "kept": []})
        self.assertEqual(
            self.broker.calls,
            [("add-share", {"share": self.metadata["share"], "user": self.metadata["username"], "password": self.password, "path": os.path.realpath(self.folder)})],
        )
        self.assertEqual(self.stored()["metadata"], self.metadata)
        self.assertIn("Restored the share", "\n".join(logs.output))
        self.assertFalse(any(self.password in line for line in logs.output))

    def test_a_share_samba_has_is_left_alone(self) -> None:
        self.configure(self.metadata["share"])

        self.assertEqual(repository_shares.restore_shares(), {"restored": [], "reset": [], "kept": []})
        self.assertEqual(self.broker.calls, [])

    def test_a_shares_file_that_cannot_be_read_restores_nothing_and_resets_nothing(self) -> None:
        self.conf.mkdir()

        with self.assertLogs("python.srv", level=logging.WARNING) as logs:
            repository_shares.restore_shares()

        self.assertEqual(self.broker.calls, [])
        self.assertTrue(repository_shares.is_shared(self.stored()))
        self.assertIn("are not checked against Samba", "\n".join(logs.output))

    def test_a_password_the_key_cannot_decrypt_resets_the_repository_and_removes_the_account(self) -> None:
        REPOSITORIES[self.repository["id"]]["metadata"]["password"] = "abr-enc:v1:not-a-token"

        with self.assertLogs("python.srv", level=logging.WARNING) as logs:
            outcome = repository_shares.restore_shares()

        self.assertEqual(outcome["reset"], ["Local-Repo-1"])
        self.assertEqual(self.broker.calls, [("remove-share", {"share": self.metadata["share"], "user": self.metadata["username"]})])
        self.assertEqual(self.stored()["type"], "local")
        self.assertIn("cannot be decrypted", "\n".join(logs.output))

    def test_the_reset_goes_ahead_even_when_the_account_cannot_be_removed(self) -> None:
        REPOSITORIES[self.repository["id"]]["metadata"]["password"] = "abr-enc:v1:not-a-token"
        self.broker.error = broker_client.BrokerUnavailable("nothing listens")

        with self.assertLogs("python.srv", level=logging.WARNING) as logs:
            repository_shares.restore_shares()

        self.assertEqual(self.stored()["type"], "local")
        self.assertIn(f"the account {self.metadata['username']} is left in Samba", "\n".join(logs.output))

    def test_a_refusing_or_absent_broker_keeps_the_share_for_the_next_start(self) -> None:
        for error in (broker_client.BrokerRefused("refused", "smbd is not running"), broker_client.BrokerUnavailable("nothing listens")):
            with self.subTest(error=type(error).__name__):
                self.broker.error = error
                with self.assertLogs("python.srv", level=logging.WARNING) as logs:
                    outcome = repository_shares.restore_shares()
                self.assertEqual(outcome["kept"], ["Local-Repo-1"])
                self.assertEqual(self.stored()["metadata"], self.metadata)
                self.assertIn("tried again when the server starts next time", "\n".join(logs.output))

    def test_only_the_missing_shares_are_restored_and_repositories_not_shared_are_skipped(self) -> None:
        second = self.root / "second"
        second.mkdir()
        other = abr_v41.create_repository_record({"name": "Other", "type": "local", "path": str(second)})
        third = self.root / "third"
        third.mkdir()
        abr_v41.create_repository_record({"name": "Plain", "type": "local", "path": str(third)})
        repository_shares.share_repository(REPOSITORIES[other["id"]])
        self.configure(REPOSITORIES[other["id"]]["metadata"]["share"])
        self.broker.calls.clear()

        outcome = repository_shares.restore_shares()

        self.assertEqual(outcome["restored"], ["Local-Repo-1"])
        self.assertEqual([arguments["share"] for _operation, arguments in self.broker.calls], [self.metadata["share"]])

    def test_nothing_shared_reads_nothing(self) -> None:
        repository_shares.unshare_repository(self.stored())
        self.broker.calls.clear()

        with patch.object(repository_shares, "_read_configured_shares") as read:
            self.assertEqual(repository_shares.restore_shares(), {"restored": [], "reset": [], "kept": []})
        read.assert_not_called()

    def test_the_background_run_logs_a_crash_instead_of_raising(self) -> None:
        with patch.object(repository_shares, "restore_shares", side_effect=RuntimeError("boom")), self.assertLogs("python.srv", level=logging.ERROR) as logs:
            repository_shares.restore_shares_in_background().join(5)

        self.assertIn("Could not check the shares of the repositories against Samba", "\n".join(logs.output))


class StartupRestoreTests(unittest.TestCase):
    def test_the_server_starts_the_restore_when_it_starts(self) -> None:
        from fastapi.testclient import TestClient

        from python.srv.backup_server import app

        with patch.object(repository_shares, "restore_shares_in_background") as restore, patch.dict(
            "os.environ", {"ABR_BOOTSTRAP_ADMIN_USER": "admin", "ABR_BOOTSTRAP_ADMIN_PASSWORD": "Sup3rSecretPW"}
        ), TestClient(app):
            pass

        restore.assert_called_once_with()


class BrokerErrorTests(_SharesCase):
    def assert_unchanged(self, before: str) -> None:
        self.assertEqual(json.dumps(self.stored(), sort_keys=True), before)

    def test_each_broker_answer_becomes_an_api_error_and_changes_nothing(self) -> None:
        cases = [
            (broker_client.BrokerUnavailable("nothing listens on /run/altastor-backup-shares.sock"), 503, "repository_share_unavailable", "altastor-backup-shares.socket"),
            (broker_client.BrokerRefused("refused", "smbd is not running"), 409, "repository_share_refused", "smbd is not running"),
            (broker_client.BrokerRefused("not_permitted", "uid 1000 may not use this broker"), 503, "repository_share_unavailable", "refused this server"),
            (broker_client.BrokerRefused("failed", "smbcontrol smbd failed"), 500, "repository_share_failed", "smbcontrol smbd failed"),
            (broker_client.BrokerRefused("invalid_request", "unknown operation"), 500, "repository_share_failed", "unknown operation"),
        ]
        before = json.dumps(self.stored(), sort_keys=True)
        for exception, status, code, text in cases:
            with self.subTest(code=code, text=text):
                self.broker.error = exception
                error = self.refused(self.share)
                self.assertEqual((error.status_code, error.code), (status, code))
                self.assertIn("The share of Local-Repo-1 could not be created", error.message)
                self.assertIn(text, error.message)
                self.assert_unchanged(before)

    def test_the_socket_and_the_timeout_come_from_the_settings(self) -> None:
        with patch.dict("os.environ", {repository_shares.SOCKET_ENV: "/tmp/elsewhere.sock", repository_shares.TIMEOUT_ENV: "7"}), patch.object(
            repository_shares.broker_client, "call", return_value={}
        ) as call:
            self.share()

        self.assertEqual(call.call_args.args[0], "/tmp/elsewhere.sock")
        self.assertEqual(call.call_args.kwargs["timeout"], 7)

    def test_the_socket_defaults_to_the_package_one(self) -> None:
        with patch.dict("os.environ", {repository_shares.SOCKET_ENV: ""}):
            self.assertEqual(repository_shares.broker_socket(), "/run/altastor-backup-shares.sock")


class InUseTests(_SharesCase):
    def task(self, task_id: str, status: str = "running", **fields) -> None:
        TASKS[task_id] = {"id": task_id, "status": status, "task_type": "backup", **fields}

    def test_sharing_and_unsharing_wait_for_sessions_that_use_the_repository(self) -> None:
        JOBS["nightly"] = {"id": "nightly", "name": "Nightly", "repo_id": self.repository["id"]}
        JOBS["copy"] = {"id": "copy", "name": "Copy", "repo_id": "elsewhere", "source_repository_ids": [self.repository["id"]]}
        JOBS["copy-of-copy"] = {"id": "copy-of-copy", "repo_id": "far", "source_job_ids": ["copy"]}
        cases = {
            "a backup into it": {"job_id": "nightly"},
            "a copy from it": {"job_id": "copy"},
            "a copy of a copy from it": {"job_id": "copy-of-copy"},
            "a verify of its chain": {"parameters": {"repository_id": self.repository["id"]}},
            "a restore from it": {"parameters": {"restore": {"source": {"repository_ids": [self.repository["id"]]}}}},
        }
        for label, fields in cases.items():
            with self.subTest(label):
                TASKS.clear()
                self.task("t-1", **fields)
                error = self.refused(self.share)
                self.assertEqual((error.status_code, error.code), (409, "repository_share_busy"))
                self.assertEqual(error.details["sessions"], ["t-1"])
        self.assertEqual(self.broker.calls, [])

    def test_finished_sessions_and_sessions_of_other_repositories_do_not_stop_it(self) -> None:
        JOBS["other"] = {"id": "other", "repo_id": "elsewhere"}
        self.task("done", status="completed", parameters={"repository_id": self.repository["id"]})
        self.task("other", job_id="other")

        self.share()

        self.assertEqual([operation for operation, _ in self.broker.calls], ["add-share"])

    def test_a_queued_session_stops_the_unshare_too(self) -> None:
        self.share()
        self.task("t-1", status="queued", parameters={"repository_id": self.repository["id"]})

        self.assertEqual(self.refused(self.unshare).code, "repository_share_busy")


class UnshareTests(_SharesCase):
    def test_unsharing_removes_the_share_and_the_account_and_keeps_the_repository_local(self) -> None:
        self.share()
        share, user = self.stored()["metadata"]["share"], self.stored()["metadata"]["username"]

        self.unshare()

        self.assertEqual(self.broker.calls[-1], ("remove-share", {"share": share, "user": user}))
        metadata = self.stored()["metadata"]
        self.assertEqual(self.stored()["type"], "local")
        self.assertEqual([metadata[field] for field in ("server", "share", "username", "password", "support_hyperv")], [None, None, None, None, False])

    def test_unsharing_a_repository_that_is_not_shared_runs_nothing(self) -> None:
        self.unshare()

        self.assertEqual(self.broker.calls, [])

    def test_unsharing_is_refused_while_hyperv_jobs_back_up_into_it(self) -> None:
        self.share()
        JOBS["hv-job"] = {"id": "hv-job", "name": "HV-nightly", "repo_id": self.repository["id"], "inventory_id": "hv"}

        error = self.refused(self.unshare)

        self.assertEqual((error.status_code, error.code), (409, "repository_share_used_by_hyperv_jobs"))
        self.assertEqual(error.details["jobs"], ["HV-nightly"])
        self.assertTrue(repository_shares.is_shared(self.stored()))

    def test_vcenter_jobs_do_not_keep_the_share(self) -> None:
        self.share()
        JOBS["vc-job"] = {"id": "vc-job", "name": "VC-nightly", "repo_id": self.repository["id"], "inventory_id": "vc"}

        self.unshare()

        self.assertFalse(repository_shares.is_shared(self.stored()))

    def test_a_broker_failure_keeps_the_share_in_the_catalog(self) -> None:
        self.share()
        self.broker.error = broker_client.BrokerRefused("failed", "smbcontrol smbd failed")

        self.assertEqual(self.refused(self.unshare).code, "repository_share_failed")
        self.assertTrue(repository_shares.is_shared(self.stored()))

    def test_removing_a_shared_repository_removes_its_share(self) -> None:
        self.share()
        share = self.stored()["metadata"]["share"]

        abr_v41.remove_repository_record(self.repository["id"])

        self.assertEqual(self.broker.calls[-1][0], "remove-share")
        self.assertEqual(self.broker.calls[-1][1]["share"], share)
        self.assertNotIn(self.repository["id"], REPOSITORIES)

    def test_removing_a_repository_that_is_not_shared_runs_nothing(self) -> None:
        abr_v41.remove_repository_record(self.repository["id"])

        self.assertEqual(self.broker.calls, [])


class ManagedShareTests(_SharesCase):
    def test_the_share_of_a_shared_repository_cannot_be_edited_by_hand(self) -> None:
        self.share()

        error = self.refused(lambda: abr_v41.edit_repository_record(self.repository["id"], {"password": "mine", "username": "me", "path": "/srv"}))

        self.assertEqual((error.status_code, error.code), (409, "repository_share_managed"))
        self.assertEqual(error.details["fields"], ["password", "path", "username"])
        self.assertEqual(abr_v41.edit_repository_record(self.repository["id"], {"description": "Ours"})["description"], "Ours")

    def test_a_repository_that_is_not_shared_is_edited_as_before(self) -> None:
        edited = abr_v41.edit_repository_record(self.repository["id"], {"description": "Local"})

        self.assertEqual(edited["description"], "Local")


class ListTests(_SharesCase):
    def test_local_and_shared_repositories_are_listed_by_path_without_a_password(self) -> None:
        second = self.root / "a-first"
        second.mkdir()
        other = abr_v41.create_repository_record({"name": "Local-Repo-0", "type": "local", "path": str(second)})
        self.share()
        REPOSITORIES["s3"] = {"id": "s3", "name": "Cloud", "type": "s3", "metadata": {}}

        items = repository_shares.list_shares()["items"]

        self.assertEqual([item["id"] for item in items], [other["id"], self.repository["id"]])
        self.assertEqual([item["shared"] for item in items], [False, True])
        self.assertEqual(items[0]["network_path"], None)
        self.assertTrue(items[1]["network_path"].endswith(self.stored()["metadata"]["share"]))
        self.assertNotIn("password", json.dumps(items))


class _SmbClient:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.commands: list[list[str]] = []
        self.auth: list[tuple[str, int]] = []
        self.corrupt = False
        self.keep = False
        self.lose = False
        self.status = ""

    def __call__(self, argv, **_kwargs):
        self.commands.append(list(argv))
        auth = Path(argv[argv.index("-A") + 1])
        self.auth.append((auth.read_text(), stat.S_IMODE(auth.stat().st_mode)))
        if self.status:
            return subprocess.CompletedProcess(argv, 1, stdout=self.status, stderr="")
        for step in argv[argv.index("-c") + 1].split("; "):
            verb, local, *remote = [part.strip('"') for part in step.split(" ")]
            if verb == "put" and not self.lose:
                (self.folder / remote[0]).write_text(Path(local).read_text())
            elif verb == "get":
                Path(remote[0]).write_text((self.folder / local).read_text() + ("x" if self.corrupt else ""))
            elif verb == "del" and not self.keep:
                (self.folder / local).unlink()
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


class CheckTests(_SharesCase):
    def setUp(self) -> None:
        super().setUp()
        self.share()
        self.smbclient = _SmbClient(self.folder)
        run = patch.object(repository_shares.subprocess, "run", side_effect=self.smbclient)
        run.start()
        self.addCleanup(run.stop)
        which = patch.object(repository_shares.shutil, "which", return_value="/usr/bin/smbclient")
        which.start()
        self.addCleanup(which.stop)

    def check(self) -> dict:
        return repository_shares.check_share(self.stored())

    def test_a_working_share_passes_write_read_and_delete_with_its_own_account(self) -> None:
        result = self.check()

        metadata = self.stored()["metadata"]
        self.assertTrue(result["checked"])
        self.assertIn("write, read back and delete passed", result["message"])
        self.assertEqual(self.smbclient.commands[0][1], f"//{metadata['server']}/{metadata['share']}")
        password = credential_encryption.decrypt_secret(metadata["password"])
        self.assertEqual(self.smbclient.auth[0], (f"username={metadata['username']}\npassword={password}\n", 0o600))
        self.assertNotIn(password, " ".join(self.smbclient.commands[0]))
        self.assertEqual(list(self.folder.iterdir()), [])
        self.assertFalse(Path(self.smbclient.commands[0][self.smbclient.commands[0].index("-A") + 1]).exists())

    def test_each_failure_is_named(self) -> None:
        cases = {
            "status": ("NT_STATUS_LOGON_FAILURE\n", "NT_STATUS_LOGON_FAILURE"),
            "corrupt": (True, "differs from the one written"),
            "lose": (True, "did not appear in"),
            "keep": (True, "is still in"),
        }
        for attribute, (value, text) in cases.items():
            with self.subTest(attribute):
                for name in cases:
                    setattr(self.smbclient, name, "" if name == "status" else False)
                for leftover in self.folder.iterdir():
                    leftover.unlink()
                setattr(self.smbclient, attribute, value)
                error = self.refused(self.check)
                self.assertEqual((error.status_code, error.code), (502, "repository_share_check_failed"))
                self.assertIn(text, error.message)

    def test_a_repository_that_is_not_shared_or_a_missing_smbclient_is_refused(self) -> None:
        self.unshare()
        self.assertEqual(self.refused(self.check).code, "repository_not_shared")

        self.share()
        with patch.object(repository_shares.shutil, "which", return_value=None):
            self.assertEqual(self.refused(self.check).code, "smbclient_missing")

    def test_an_smbclient_that_cannot_run_is_named(self) -> None:
        with patch.object(repository_shares.subprocess, "run", side_effect=subprocess.TimeoutExpired(["smbclient"], 60)):
            self.assertIn("smbclient could not be run", self.refused(self.check).message)


class _BrokerProcess:
    def __init__(self, path: Path, operations, *, service_user: str | None = None) -> None:
        self.service_user = service_user or current_user()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(8)
        self.operations = operations
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        self.listener.settimeout(0.1)
        while not self.stopped.is_set():
            try:
                connection, _address = self.listener.accept()
            except TimeoutError:
                continue
            with connection, redirect_stderr(io.StringIO()):
                broker_core.serve(self.operations, service_user=self.service_user, connection=connection)

    def close(self) -> None:
        self.stopped.set()
        self.thread.join(timeout=5)
        self.listener.close()


class EndToEndTests(_Catalog):
    def setUp(self) -> None:
        super().setUp()
        self.system, folder = make_system()
        self.addCleanup(folder.cleanup)
        socket_path = self.root / "shares.sock"
        broker = _BrokerProcess(socket_path, shares_broker.operations(self.system.samba()))
        self.addCleanup(broker.close)
        environment = patch.dict("os.environ", {repository_shares.SOCKET_ENV: str(socket_path), repository_shares.SHARES_CONF_ENV: str(self.system.shares_conf)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_a_share_left_behind_by_a_server_that_stopped_is_cleared_through_the_real_broker(self) -> None:
        orphan = {"share": "abr-orphan01$", "user": "abr-smb-orphan01", "password": "B" * 40, "path": os.path.realpath(self.folder)}
        shares_broker.add_share(self.system.samba(), orphan)

        repository_shares.share_repository(self.stored())

        sections = shares_broker.parse_sections(self.system.shares_conf.read_text())
        self.assertEqual(list(sections), [self.stored()["metadata"]["share"]])
        self.assertNotIn("abr-smb-orphan01", self.system.accounts)

    def test_a_closed_share_comes_back_through_the_real_broker_with_the_same_account(self) -> None:
        repository_shares.share_repository(self.stored())
        metadata = self.stored()["metadata"]
        with patch.object(shares_broker.os, "geteuid", return_value=0), redirect_stdout(io.StringIO()):
            self.assertEqual(shares_broker.close_from_command_line(self.system.samba()), shares_broker.CLOSED)
        self.assertEqual(self.system.smb_conf.read_text(), SMB_CONF_TEXT)
        useradds = [argv for argv, _ in self.system.calls if argv[0] == "useradd"]

        outcome = repository_shares.restore_shares()

        self.assertEqual(outcome["restored"], ["Local-Repo-1"])
        self.assertIn(metadata["share"], shares_broker.parse_sections(self.system.shares_conf.read_text()))
        self.assertEqual(self.system.smb_conf.read_text(), SMB_CONF_TEXT + f"include = {self.system.shares_conf}\n")
        self.assertEqual([argv for argv, _ in self.system.calls if argv[0] == "useradd"], useradds)
        self.assertEqual(self.system.samba_accounts[metadata["username"]], credential_encryption.decrypt_secret(metadata["password"]))

    def test_an_account_samba_lost_is_made_again_with_the_stored_password(self) -> None:
        repository_shares.share_repository(self.stored())
        metadata = self.stored()["metadata"]
        with patch.object(shares_broker.os, "geteuid", return_value=0), redirect_stdout(io.StringIO()):
            shares_broker.close_from_command_line(self.system.samba())
        self.system.samba_accounts.clear()
        self.system.accounts.clear()

        repository_shares.restore_shares()

        self.assertEqual(self.system.accounts[metadata["username"]], shares_broker.ACCOUNT_COMMENT)
        self.assertEqual(self.system.samba_accounts[metadata["username"]], credential_encryption.decrypt_secret(metadata["password"]))

    def test_a_share_goes_from_the_server_through_the_socket_to_samba_and_back_out(self) -> None:
        repository_shares.share_repository(self.stored())

        metadata = self.stored()["metadata"]
        sections = shares_broker.parse_sections(self.system.shares_conf.read_text())
        self.assertEqual(sections[metadata["share"]]["path"], os.path.realpath(self.folder))
        self.assertEqual(sections[metadata["share"]]["valid users"], metadata["username"])
        self.assertEqual(self.system.samba_accounts[metadata["username"]], credential_encryption.decrypt_secret(metadata["password"]))

        repository_shares.unshare_repository(self.stored())

        self.assertEqual(self.system.smb_conf.read_text(), SMB_CONF_TEXT)
        self.assertFalse(self.system.shares_conf.exists())
        self.assertEqual((self.system.accounts, self.system.samba_accounts), ({}, {}))

    def test_a_samba_refusal_reaches_the_api_with_its_reason(self) -> None:
        self.system.running = False

        with self.assertRaises(ApiError) as caught:
            repository_shares.share_repository(self.stored())

        self.assertEqual((caught.exception.status_code, caught.exception.code), (409, "repository_share_refused"))
        self.assertIn("smbd is not running", caught.exception.message)
        self.assertEqual(self.stored()["type"], "local")

    def test_a_server_that_is_not_the_brokers_service_user_is_refused(self) -> None:
        if os.getuid() == 0:
            self.skipTest("root is always let in")
        other = self.root / "elsewhere.sock"
        broker = _BrokerProcess(other, shares_broker.operations(self.system.samba()), service_user="abr-nobody-here")
        self.addCleanup(broker.close)

        with patch.dict("os.environ", {repository_shares.SOCKET_ENV: str(other)}), self.assertRaises(ApiError) as caught:
            repository_shares.share_repository(self.stored())

        self.assertEqual((caught.exception.status_code, caught.exception.code), (503, "repository_share_unavailable"))
        self.assertIn("refused this server", caught.exception.message)
        self.assertFalse(self.system.shares_conf.exists())


class ShareApiTests(RbacApiCase):
    def setUp(self) -> None:
        super().setUp()
        REPOSITORIES.clear()
        self.addCleanup(REPOSITORIES.clear)
        folder = tempfile.TemporaryDirectory(prefix="abr-share-api-")
        self.addCleanup(folder.cleanup)
        key = patch.dict(
            "os.environ",
            {credential_encryption.KEY_FILE_ENV: str(Path(folder.name) / "credentials.key"), repository_shares.SHARES_CONF_ENV: str(Path(folder.name) / "no-shares.conf")},
        )
        key.start()
        self.addCleanup(key.stop)
        credential_encryption.reset_cipher()
        self.addCleanup(credential_encryption.reset_cipher)
        path = Path(folder.name) / "repo1"
        path.mkdir()
        self.repository = abr_v41.create_repository_record({"name": "Local-Repo-1", "type": "local", "path": str(path)})
        self.broker = _Broker()
        call = patch.object(repository_shares.broker_client, "call", side_effect=self.broker)
        call.start()
        self.addCleanup(call.stop)

    def url(self, suffix: str = "") -> str:
        return f"/api/v1/repositories/{self.repository['id']}/share{suffix}"

    def test_shares_are_listed_and_the_list_is_not_taken_for_a_repository_id(self) -> None:
        response = self.client.get("/api/v1/repositories/shares", headers=self.headers(rbac.AUDITOR))

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([item["name"] for item in response.json()["items"]], ["Local-Repo-1"])

    def test_add_and_del_need_repository_edit(self) -> None:
        denied = self.client.post(self.url(), headers=self.headers(rbac.AUDITOR))
        self.assertEqual((denied.status_code, denied.json()["error"]["code"]), (403, "permission_denied"))

        added = self.client.post(self.url(), headers=self.headers(rbac.BACKUP_ADMINISTRATOR))
        self.assertEqual(added.status_code, 200, added.text)
        self.assertTrue(added.json()["shared"])
        self.assertRegex(added.json()["share"], SHARE_NAME)
        self.assertNotIn(self.broker.calls[0][1]["password"], added.text)
        self.assertNotIn("password", added.json())

        self.assertEqual(self.client.delete(self.url(), headers=self.headers(rbac.RESTORE_OPERATOR)).status_code, 403)
        removed = self.client.delete(self.url(), headers=self.headers(rbac.BACKUP_ADMINISTRATOR))
        self.assertEqual((removed.status_code, removed.json()["shared"]), (200, False))
        self.assertEqual([operation for operation, _ in self.broker.calls], ["add-share", "remove-share"])

    def test_check_needs_repository_verify_and_answers_with_its_code(self) -> None:
        self.assertEqual(self.client.post(self.url("/check"), headers=self.headers(rbac.AUDITOR)).status_code, 403)

        response = self.client.post(self.url("/check"), headers=self.headers(rbac.ADMINISTRATOR))

        self.assertEqual((response.status_code, response.json()["error"]["code"]), (409, "repository_not_shared"))

    def test_an_unknown_repository_is_not_found(self) -> None:
        response = self.client.post("/api/v1/repositories/missing/share", headers=self.headers(rbac.ADMINISTRATOR))

        self.assertEqual((response.status_code, response.json()["error"]["code"]), (404, "repository_not_found"))

    def test_the_broker_being_away_answers_503_and_is_audited_as_failed(self) -> None:
        self.broker.error = broker_client.BrokerUnavailable("nothing listens on /run/altastor-backup-shares.sock")

        response = self.client.post(self.url(), headers=self.headers(rbac.ADMINISTRATOR))

        self.assertEqual((response.status_code, response.json()["error"]["code"]), (503, "repository_share_unavailable"))
        [record] = self.audit(function_id="repository.edit")
        self.assertEqual(record["result"], "failed")

    def test_the_share_routes_carry_their_functions(self) -> None:
        from python.srv.api import repositories

        functions = {
            (route.path, method): route.openapi_extra.get("x-function-id")
            for route in repositories.router.routes
            for method in route.methods
            if "share" in route.path
        }

        self.assertEqual(
            functions,
            {
                ("/api/v1/repositories/shares", "GET"): "repository.view",
                ("/api/v1/repositories/{repository_id}/share", "POST"): "repository.edit",
                ("/api/v1/repositories/{repository_id}/share", "DELETE"): "repository.edit",
                ("/api/v1/repositories/{repository_id}/share/check", "POST"): "repository.verify",
            },
        )


if __name__ == "__main__":
    unittest.main()
