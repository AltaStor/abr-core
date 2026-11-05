# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import contextlib
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core.credential_encryption import decrypt_secret
from python.srv.core.mongo_db_wrapper import HYPERVISORS, JOBS, REPOSITORIES
from python.srv.plugins.hyperv import hyperv, hyperv_backup
from python.srv.plugins.hyperv.hyperv import HyperVUnreachableError, check_share_account
from python.srv.services import abr_v41
from python.srv.services.api_errors import ApiError

REFUSED_LINES = [
    (
        "Error: HV-NODE-01 cannot open the repository share \\\\nas.example.test\\backups as NAS\\backup: "
        "The specified network password is not correct."
    ),
    "ABR_TOOL_EXIT=1",
]


class _Node:
    def __init__(self, run: dict | BaseException) -> None:
        self.run = run
        self.nodes = ["HV-NODE-01"]
        self.calls: list[tuple] = []

    def run_disk_tool(self, node, args, *, env=None, timeout=None):
        self.calls.append((node, list(args), dict(env or {})))
        if isinstance(self.run, BaseException):
            raise self.run
        return self.run


def _inventory(inventory_id: str, host: str) -> dict:
    return {
        "id": inventory_id,
        "name": inventory_id,
        "kind": "hyper-v",
        "metadata": {"host": host, "credentials": {"username": "CORP\\admin", "password": "secret"}},
    }


class ShareAccountCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (HYPERVISORS, JOBS, REPOSITORIES):
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in (HYPERVISORS, JOBS, REPOSITORIES)])
        self.repository = {
            "id": "repo-1",
            "name": "Shared",
            "type": "smb",
            "path": "/mnt/hv",
            "metadata": {"server": "nas.example.test", "share": "backups", "username": "NAS\\backup", "password": "new"},
        }
        HYPERVISORS["inv-1"] = _inventory("inv-1", "hv1.example.test")

    def _check(self, run: dict | BaseException, opened: list | None = None) -> dict:
        node = _Node(run)

        def open_node(connection):
            if opened is not None:
                opened.append(connection["host"])
            return contextlib.nullcontext(node)

        with patch.object(hyperv_backup, "open_node", side_effect=open_node), patch.dict(
            "os.environ", {"ALTASTOR_SMB_ACCOUNT_CHECK_SECONDS": "5"}
        ):
            result = check_share_account(self.repository)
        self.node = node
        return result

    def test_a_host_that_opens_the_share_passes_it_with_the_account(self) -> None:
        result = self._check({"exit_code": 0, "result": {"command": "info", "status": "ok"}, "lines": ["{", "}"]})

        self.assertEqual(result, {"name": "share_account", "status": "ok", "message": "HV-NODE-01 opened the share \\\\nas.example.test\\backups as NAS\\backup"})
        node, args, env = self.node.calls[0]
        self.assertEqual((node, args[:2]), ("HV-NODE-01", ["info", "--json"]))
        self.assertEqual((env["ABR_SMB_USER"], env["ABR_SMB_PASSWORD"]), ("NAS\\backup", "new"))

    def test_a_host_that_refuses_the_password_fails_the_check_with_its_reason(self) -> None:
        result = self._check({"exit_code": 1, "result": None, "lines": REFUSED_LINES})

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["message"],
            "HV-NODE-01 could not open the share \\\\nas.example.test\\backups as NAS\\backup: "
            "The specified network password is not correct.",
        )

    def test_another_tool_failure_leaves_the_account_unchecked(self) -> None:
        result = self._check({"exit_code": 1, "result": {"command": "info", "status": "error", "error": "tool broke"}, "lines": []})

        self.assertEqual(result["status"], "warning")
        self.assertIn("was not checked: HV-NODE-01 could not read \\\\nas.example.test\\backups", result["message"])

    def test_a_host_that_cannot_be_reached_leaves_the_account_unchecked(self) -> None:
        result = self._check(HyperVUnreachableError("hv1.example.test", [5985], protocol="http"))

        self.assertEqual(result["status"], "warning")
        self.assertIn("did not answer WinRM", result["message"])

    def test_a_host_that_does_not_answer_in_time_leaves_the_account_unchecked(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        def hang(connection):
            release.wait(10)
            return contextlib.nullcontext(_Node({"exit_code": 0, "result": {}, "lines": []}))

        with patch.object(hyperv_backup, "open_node", side_effect=hang), patch.dict(
            "os.environ", {"ALTASTOR_SMB_ACCOUNT_CHECK_SECONDS": "1"}
        ):
            result = check_share_account(self.repository)

        self.assertEqual(result["status"], "warning")
        self.assertIn("Hyper-V host hv1.example.test did not answer within 1 s", result["message"])

    def test_without_a_hyperv_inventory_the_account_is_not_checked(self) -> None:
        HYPERVISORS.clear()
        HYPERVISORS["vc-1"] = {"id": "vc-1", "name": "vc", "kind": "vcenter", "metadata": {"host": "vc.example.test"}}

        result = check_share_account(self.repository)

        self.assertEqual(result["status"], "warning")
        self.assertIn("no Hyper-V inventory is registered", result["message"])

    def test_the_host_of_a_job_writing_to_the_repository_is_asked_first(self) -> None:
        HYPERVISORS["inv-0"] = _inventory("inv-0", "hv0.example.test")
        JOBS["job-1"] = {"id": "job-1", "inventory_id": "inv-1", "repo_id": "repo-1"}
        opened: list = []

        self._check({"exit_code": 0, "result": {"status": "ok"}, "lines": []}, opened)

        self.assertEqual(opened, ["hv1.example.test"])

    def test_other_repositories_and_the_default_repository_are_not_checked(self) -> None:
        self.assertIsNone(check_share_account({"type": "local", "path": "/srv"}))
        managed = dict(self.repository, metadata={**self.repository["metadata"], "support_hyperv": True})
        self.assertIsNone(check_share_account(managed))


class PasswordEditTests(unittest.TestCase):
    def setUp(self) -> None:
        REPOSITORIES.clear()
        self._root = tempfile.mkdtemp(prefix="abr-smb-password-")
        self.addCleanup(shutil.rmtree, self._root, ignore_errors=True)
        self.addCleanup(REPOSITORIES.clear)
        path = Path(self._root) / "hv"
        path.mkdir(mode=0o750)
        mounted = patch.object(abr_v41, "share_mount_check", return_value={"name": "share_mount", "status": "ok", "message": "mounted"})
        mounted.start()
        self.addCleanup(mounted.stop)
        self.account = patch.object(hyperv, "check_share_account", return_value={"name": "share_account", "status": "ok", "message": "opened"})
        self.check = self.account.start()
        self.addCleanup(self.account.stop)
        self.repository = abr_v41.create_repository_record(
            {"name": "Shared", "type": "smb", "path": str(path), "server": "nas.example.test", "share": "backups", "username": "NAS\\backup", "password": "old"}
        )
        self.check.reset_mock()

    def test_a_password_the_host_refuses_is_refused_and_the_old_one_kept(self) -> None:
        stored = REPOSITORIES[self.repository["id"]]["metadata"]["password"]
        self.check.return_value = {"name": "share_account", "status": "failed", "message": "HV-NODE-01 could not open the share: The specified network password is not correct."}

        with self.assertRaises(ApiError) as caught:
            abr_v41.edit_repository_record(self.repository["id"], {"password": "typo"})

        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("network password is not correct", caught.exception.message)
        self.assertEqual(REPOSITORIES[self.repository["id"]]["metadata"]["password"], stored)
        self.assertEqual(self.check.call_args.args[0]["metadata"]["password"], "typo")

    def test_a_password_no_host_could_check_is_saved_with_a_warning(self) -> None:
        self.check.return_value = {"name": "share_account", "status": "warning", "message": "The share account was not checked: no Hyper-V inventory is registered to open the share with it"}

        edited = abr_v41.edit_repository_record(self.repository["id"], {"password": "new"})

        self.assertIn("The share account was not checked: no Hyper-V inventory is registered to open the share with it", edited["warnings"])
        self.assertEqual(decrypt_secret(REPOSITORIES[self.repository["id"]]["metadata"]["password"]), "new")

    def test_a_description_edit_asks_no_host_and_reads_no_backups(self) -> None:
        with patch.object(abr_v41, "_verify_repository_backup_health") as health:
            abr_v41.edit_repository_record(self.repository["id"], {"description": "edited"})

        self.check.assert_not_called()
        health.assert_not_called()

    def test_a_password_edit_reads_no_backups(self) -> None:
        with patch.object(abr_v41, "_verify_repository_backup_health") as health:
            abr_v41.edit_repository_record(self.repository["id"], {"password": "new"})

        health.assert_not_called()
        self.check.assert_called_once()


if __name__ == "__main__":
    unittest.main()
