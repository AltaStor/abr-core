# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from python.cli.commands.backup_commands import BackupCommands
from python.cli.errors import CliStructuredError
from python.cli.exit_codes import INVALID_USAGE
from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    JOBS,
    REPOSITORIES,
    SYSTEM_STATE,
    TASKS,
)
from python.srv.services import abr_v41, deleted_backups
from python.srv.services.api_errors import ApiError


class RecycleBinTests(unittest.TestCase):
    def setUp(self) -> None:
        stores = (BACKUPS, JOBS, REPOSITORIES, TASKS, SYSTEM_STATE)
        for store in stores:
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in stores])
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        REPOSITORIES["repo-1"] = {"id": "repo-1", "name": "Local", "type": "local", "path": str(self.root)}
        JOBS["job-1"] = {"id": "job-1", "name": "Nightly", "job_type": "backup", "repo_id": "repo-1", "retention_days": 1, "gfs": {}}
        self._point("full-1", "2020-01-01T00:00:00+00:00", None)
        self._point("inc-1", "2020-01-02T00:00:00+00:00", "full-1")
        self.removed: list[str] = []

        def remove(point):
            self.removed.append(str(point["id"]))
            return True

        self.enterContext(patch.object(abr_v41, "_delete_backup_artifacts_from_point", side_effect=remove))
        self.enterContext(patch.object(abr_v41, "_ensure_share_mounted", return_value=None))
        self.enterContext(patch.object(abr_v41, "_point_repository_unmounted", return_value=None))
        self.enterContext(patch.object(abr_v41.clock_guard, "retention_block", return_value=None))
        self.enterContext(patch.object(abr_v41.audit_service, "record_server_start", return_value=None))

    def _point(self, point_id: str, created_at: str, parent: str | None, **extra) -> None:
        folder = self.root / "vm" / point_id
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text("{}", encoding="utf-8")
        BACKUPS[point_id] = {
            "id": point_id,
            "job_id": "job-1",
            "vm_id": "vm-1",
            "vm_name": "db-01",
            "repository_id": "repo-1",
            "status": "available",
            "full_backup": parent is None,
            "execution_type": "ActiveFull" if parent is None else "Incremental",
            "parent_point_id": parent,
            "created_at": created_at,
            "artifact": {
                "backup_root": str(folder),
                "manifest_path": str(folder / "manifest.json"),
                "disks": [{"disk_key": 1, "abr_path": str(folder), "abr_backup_name": "01-vm.vhdx"}],
            },
            **extra,
        }

    @property
    def chain_id(self) -> str:
        return abr_v41._chain_id_for_point("job-1", "vm-1", "repo-1")

    def _keep(self, days: int) -> None:
        deleted_backups.set_keep_days(days, user="admin")

    def _backdate(self, *point_ids: str) -> None:
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        for point_id in point_ids:
            BACKUPS[point_id] = {**BACKUPS[point_id], "purge_after": past}

    def test_without_a_recycle_bin_a_delete_removes_the_data_at_once(self) -> None:
        report = abr_v41.delete_backup_chain(self.chain_id)

        self.assertTrue(report["deleted"])
        self.assertNotIn("recycle_bin", report)
        self.assertEqual(sorted(self.removed), ["full-1", "inc-1"])
        self.assertEqual(BACKUPS, {})

    def test_a_delete_moves_the_chain_to_the_recycle_bin_and_keeps_its_data(self) -> None:
        self._keep(7)
        BACKUPS["inc-1"]["status"] = "corrupt"

        report = abr_v41.delete_backup_chain_request(self.chain_id, user="alice")

        self.assertEqual(self.removed, [])
        self.assertTrue(report["deleted"])
        self.assertEqual(report["recycle_bin"]["restore_points"], 2)
        self.assertIn(f"abr backup undelete --id {self.chain_id}", report["message"])
        self.assertEqual(TASKS[report["session_id"]]["status"], "completed")
        purge_after = datetime.fromisoformat(report["recycle_bin"]["purge_after"])
        self.assertAlmostEqual((purge_after - datetime.now(timezone.utc)).total_seconds(), 7 * 86400, delta=60)
        for point_id, before in (("full-1", "available"), ("inc-1", "corrupt")):
            record = BACKUPS[point_id]
            self.assertEqual((record["status"], record["status_before_delete"], record["deleted_by"]), ("deleted", before, "alice"))
            self.assertTrue(Path(record["artifact"]["manifest_path"]).exists())

    def test_points_in_the_recycle_bin_are_out_of_use(self) -> None:
        self._keep(7)
        abr_v41.delete_backup_chain(self.chain_id)

        self.assertEqual(abr_v41._points_for_job_vm_repo("job-1", "vm-1", "repo-1"), [])
        self.assertIsNone(abr_v41._latest_successful_point("job-1", "vm-1", "repo-1"))
        chain = abr_v41.get_backup_chain(self.chain_id)
        self.assertEqual(chain["status"], "deleted")
        self.assertTrue(chain["recycle_bin"]["whole_chain"])
        self.assertTrue(all(point["purge_after"] for point in chain["restore_points"]))
        with self.assertRaises(ApiError) as caught:
            abr_v41.plan_restore_session({"point_id": "inc-1"})
        self.assertEqual(caught.exception.code, "restore_point_unavailable")
        self.assertIn("Restore point is in the recycle bin", caught.exception.message)
        self.assertIn(f"abr backup undelete --id {self.chain_id}", caught.exception.message)

    def test_retention_neither_counts_nor_removes_points_in_the_recycle_bin(self) -> None:
        self._keep(7)
        abr_v41.delete_backup_chain(self.chain_id)

        report = abr_v41.new_retention_report(JOBS["job-1"], "vm-1", "repo-1")
        abr_v41.apply_retention_to_chain(JOBS["job-1"], vm_id="vm-1", repository_id="repo-1", report=report)

        self.assertEqual((report["points"], report["removed"]), (0, []))
        self.assertEqual({BACKUPS[point]["status"] for point in ("full-1", "inc-1")}, {"deleted"})

    def test_deleting_a_chain_that_is_all_in_the_recycle_bin_deletes_it_for_good(self) -> None:
        self._keep(7)
        abr_v41.delete_backup_chain(self.chain_id)

        report = abr_v41.delete_backup_chain(self.chain_id)

        self.assertTrue(report["deleted"])
        self.assertNotIn("recycle_bin", report)
        self.assertEqual(sorted(self.removed), ["full-1", "inc-1"])
        self.assertEqual(BACKUPS, {})

    def test_a_point_already_waiting_for_deletion_is_tried_again_not_moved_to_the_recycle_bin(self) -> None:
        self._keep(7)
        for point_id in ("full-1", "inc-1"):
            BACKUPS[point_id] = {**BACKUPS[point_id], "status": "deleting", "deletion_requested_at": "2020-03-01T00:00:00+00:00"}

        report = abr_v41.delete_backup_chain(self.chain_id)

        self.assertNotIn("recycle_bin", report)
        self.assertEqual(sorted(self.removed), ["full-1", "inc-1"])
        self.assertEqual(BACKUPS, {})

    def test_undelete_brings_the_points_back_with_their_status(self) -> None:
        self._keep(7)
        BACKUPS["inc-1"]["status"] = "corrupt"
        abr_v41.delete_backup_chain(self.chain_id)

        result = abr_v41.undelete_backup_chain(self.chain_id)

        self.assertEqual(result["undeleted_restore_points"], 2)
        self.assertEqual((BACKUPS["full-1"]["status"], BACKUPS["inc-1"]["status"]), ("available", "corrupt"))
        self.assertEqual([BACKUPS["full-1"].get(field) for field in deleted_backups.BIN_FIELDS], [None] * len(deleted_backups.BIN_FIELDS))
        self.assertIsNone(result["backup"]["recycle_bin"])
        with self.assertRaises(ApiError) as caught:
            abr_v41.undelete_backup_chain(self.chain_id)
        self.assertEqual((caught.exception.status_code, caught.exception.code), (409, "backup_not_in_recycle_bin"))

    def test_the_purge_deletes_only_what_is_due(self) -> None:
        self._keep(7)
        abr_v41.delete_backup_chain(self.chain_id)

        self.assertEqual(abr_v41.purge_recycle_bin(), 0)
        self._backdate("full-1", "inc-1")
        with patch.object(abr_v41.clock_guard, "retention_block", return_value="the clock jumped"), self.assertLogs(abr_v41.LOGGER, "WARNING"):
            self.assertEqual(abr_v41.purge_recycle_bin(), 0)
        self.assertEqual(self.removed, [])

        self.assertEqual(abr_v41.purge_recycle_bin(), 2)

        self.assertEqual(BACKUPS, {})
        [session] = TASKS.values()
        self.assertEqual((session["task_type"], session["trigger"], session["status"]), ("delete", "recycle_bin", "completed"))

    def test_a_new_backup_after_a_delete_starts_a_new_segment_beside_the_recycle_bin(self) -> None:
        self._keep(7)
        abr_v41.delete_backup_chain(self.chain_id)
        self._point("full-2", "2020-02-01T00:00:00+00:00", None)

        chain = abr_v41.get_backup_chain(self.chain_id)
        self.assertEqual(chain["status"], "available")
        self.assertEqual((chain["recycle_bin"]["restore_points"], chain["recycle_bin"]["whole_chain"]), (2, False))
        first_purge = BACKUPS["full-1"]["purge_after"]

        report = abr_v41.delete_backup_chain(self.chain_id)

        self.assertEqual(report["recycle_bin"]["restore_points"], 1)
        self.assertEqual(BACKUPS["full-1"]["purge_after"], first_purge)
        self.assertEqual(BACKUPS["full-2"]["status"], "deleted")

    def test_the_purge_leaves_a_newer_incremental_built_on_older_points(self) -> None:
        self._keep(7)
        self._point("full-2", "2020-01-03T00:00:00+00:00", None)
        abr_v41.delete_backup_chain(self.chain_id, segment_id="full-2")
        self._point("inc-2", "2020-01-04T00:00:00+00:00", "inc-1")

        with self.assertRaises(ApiError) as caught:
            abr_v41.undelete_backup_chain(self.chain_id)
        self.assertEqual((caught.exception.code, caught.exception.details.get("restore_point_ids")), ("backup_undelete_conflict", ["inc-2"]))

        self._backdate("full-2")
        self.assertEqual(abr_v41.purge_recycle_bin(), 1)
        self.assertEqual(self.removed, ["full-2"])
        self.assertEqual(sorted(BACKUPS), ["full-1", "inc-1", "inc-2"])

    def test_gfs_still_needs_force_to_go_to_the_recycle_bin_but_not_to_leave_it(self) -> None:
        self._keep(7)
        BACKUPS["full-1"]["gfs_protected"] = True

        with self.assertRaises(ApiError) as caught:
            abr_v41.delete_backup_chain(self.chain_id)
        self.assertEqual(caught.exception.code, "gfs_protected_backup")

        self.assertTrue(abr_v41.delete_backup_chain(self.chain_id, force=True)["gfs_protection_overridden"])
        self.assertTrue(abr_v41.delete_backup_chain(self.chain_id)["deleted"])
        self.assertEqual(BACKUPS, {})

    def test_a_repository_whose_points_are_all_in_the_recycle_bin_says_so(self) -> None:
        self._keep(7)
        abr_v41.delete_backup_chain(self.chain_id)
        JOBS.clear()

        with self.assertRaises(ApiError) as caught:
            abr_v41.remove_repository_record("repo-1")

        self.assertEqual(caught.exception.code, "repository_has_backups")
        self.assertIn("recycle bin holds 2 of its restore point(s)", caught.exception.message)

    def test_the_days_setting_takes_0_to_365_only(self) -> None:
        self.assertEqual(deleted_backups.settings_view()["keep_days"], 0)
        for good in (0, 365, "14"):
            with self.subTest(good):
                self.assertEqual(deleted_backups.set_keep_days(good, user="admin")["keep_days"], int(good))
        for bad in (-1, 366, "x", True, 1.5, 0.1, 7.9, "0.1", "", " ", "1e2", None, []):
            with self.subTest(bad), self.assertRaises(ApiError) as caught:
                deleted_backups.set_keep_days(bad)
            self.assertEqual((caught.exception.status_code, caught.exception.code), (422, "invalid_deleted_backups_settings"))


class BackupDeleteConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = MagicMock()
        self.client.get.side_effect = lambda path, **kwargs: (
            {"keep_days": self.keep_days}
            if path.endswith("recycle-bin")
            else {"vm": {"name": "db-01"}, "restore_points": [{"status": "available"}, {"status": "available"}]}
        )
        self.keep_days = 0

    def _ask(self, *, tty: bool, answer: str = "") -> list[str]:
        asked: list[str] = []
        stdin = SimpleNamespace(isatty=lambda: tty)
        with patch("sys.stdin", stdin), patch("builtins.input", side_effect=lambda question: asked.append(question) or answer):
            BackupCommands._confirm_delete(self.client, "chain-1")
        return asked

    def test_a_terminal_is_asked_and_yes_goes_on(self) -> None:
        [question] = self._ask(tty=True, answer="y")

        self.assertEqual(question, "Delete chain chain-1 of db-01 (2 restore point(s)) for good? [y/N] ")

    def test_the_question_names_the_recycle_bin_when_it_keeps_deleted_backups(self) -> None:
        self.keep_days = 14

        [question] = self._ask(tty=True, answer="yes")

        self.assertIn("It goes to the recycle bin and is deleted for good after 14 day(s).", question)

    def test_anything_but_yes_deletes_nothing(self) -> None:
        with self.assertRaises(CliStructuredError) as caught:
            self._ask(tty=True, answer="")

        self.assertEqual((caught.exception.code, caught.exception.exit_code), ("cancelled", INVALID_USAGE))

    def test_without_a_terminal_it_needs_yes(self) -> None:
        with self.assertRaises(CliStructuredError) as caught:
            self._ask(tty=False)

        self.assertEqual((caught.exception.code, caught.exception.exit_code), ("confirmation_required", INVALID_USAGE))
        self.assertIn("add --yes", caught.exception.message)

    def test_yes_skips_the_question(self) -> None:
        commands = BackupCommands()
        args = SimpleNamespace(id="chain-1", yes=True, force=False)
        context = SimpleNamespace(api_client=self.client, output="json", print_output=lambda *a, **k: None)
        self.client.delete.return_value = {"id": "chain-1", "deleted": True}
        with patch("python.cli.commands.backup_commands.CommandDependencies", return_value=SimpleNamespace(context=context)), patch(
            "sys.stdin", io.StringIO()
        ):
            self.assertEqual(commands.delete(args), 0)

        self.client.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
