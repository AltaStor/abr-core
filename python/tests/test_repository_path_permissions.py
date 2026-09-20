# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    RESOURCES_BY_HV,
    TASKS,
)
from python.srv.services import abr_v41
from python.srv.services.api_errors import ApiError


class LocalRepositoryPathPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        for catalog in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            catalog.clear()
        self._root = tempfile.mkdtemp(prefix="abr-repo-perms-")
        os.chmod(self._root, 0o755)

    def tearDown(self) -> None:
        for catalog in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            catalog.clear()
        shutil.rmtree(self._root, ignore_errors=True)

    def _dir(self, name: str, mode: int) -> Path:
        path = Path(self._root) / name
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)
        return path

    def _add(self, name: str, path: Path) -> dict:
        return abr_v41.create_repository_record({"name": name, "type": "local", "path": str(path)})

    def test_repo_add_refuses_a_world_writable_directory_and_names_the_mode(self) -> None:
        path = self._dir("world-writable", 0o777)

        with self.assertRaises(ApiError) as ctx:
            self._add("Probe", path)

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.code, "insecure_repository_path")
        self.assertIn("world-writable", ctx.exception.message)
        self.assertIn("0777", ctx.exception.message)
        self.assertIn(str(path), ctx.exception.message)
        self.assertEqual(ctx.exception.details["mode"], "0777")
        self.assertEqual(REPOSITORIES, {})

    def test_a_refused_path_keeps_the_mode_it_had(self) -> None:
        path = self._dir("untouched", 0o777)

        with self.assertRaises(ApiError):
            self._add("Probe", path)

        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o777)

    def test_the_message_points_at_the_mode_the_product_uses_for_its_own_storage(self) -> None:
        path = self._dir("hint", 0o777)

        with self.assertRaises(ApiError) as ctx:
            self._add("Probe", path)

        self.assertIn("chmod 750", ctx.exception.message)

    def test_a_sticky_world_writable_directory_is_refused_with_accurate_wording(self) -> None:
        path = self._dir("sticky", 0o1777)

        with self.assertRaises(ApiError) as ctx:
            self._add("Probe", path)

        self.assertEqual(ctx.exception.code, "insecure_repository_path")
        self.assertIn("1777", ctx.exception.message)
        self.assertIn("sticky bit", ctx.exception.message)

    def test_a_group_readable_directory_at_the_product_standard_is_accepted_quietly(self) -> None:
        path = self._dir("healthy", 0o750)

        record = self._add("Healthy", path)

        self.assertEqual(record["status"], "available")
        self.assertEqual(record["warnings"], [])
        self.assertEqual(record["storage"]["permissions"]["mode"], "0750")
        self.assertFalse(record["storage"]["permissions"]["world_writable"])

    def test_a_group_writable_directory_is_still_accepted(self) -> None:
        path = self._dir("group-writable", 0o770)

        record = self._add("GroupShared", path)

        self.assertEqual(record["warnings"], [])

    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permission checks")
    def test_a_directory_the_service_account_cannot_write_is_refused(self) -> None:
        path = self._dir("read-only", 0o555)

        with self.assertRaises(ApiError) as ctx:
            self._add("ReadOnly", path)

        self.assertEqual(ctx.exception.code, "repository_validation_failed")
        self.assertIn("not readable and writable", ctx.exception.message)
        self.assertEqual(list(REPOSITORIES), [])

    def test_a_world_writable_parent_is_warned_about_but_not_refused(self) -> None:
        parent = self._dir("open-parent", 0o777)
        path = parent / "repo"
        path.mkdir()
        os.chmod(path, 0o750)

        record = self._add("UnderOpenParent", path)

        self.assertEqual(record["status"], "available")
        self.assertEqual(len(record["warnings"]), 1)
        self.assertIn("world-writable parent", record["warnings"][0])
        self.assertIn(str(parent), record["warnings"][0])

    def test_repo_edit_refuses_to_repoint_a_repository_at_a_world_writable_directory(self) -> None:
        record = self._add("Movable", self._dir("start", 0o750))
        loose = self._dir("destination", 0o777)

        with self.assertRaises(ApiError) as ctx:
            abr_v41.edit_repository_record(record["id"], {"path": str(loose)})

        self.assertEqual(ctx.exception.code, "insecure_repository_path")

    def test_an_unrelated_edit_of_an_already_loose_repository_still_works(self) -> None:
        path = self._dir("drifts", 0o750)
        record = self._add("Drifted", path)
        os.chmod(path, 0o777)

        updated = abr_v41.edit_repository_record(record["id"], {"description": "still editable"})

        self.assertEqual(updated["description"], "still editable")
        self.assertTrue(any("world-writable" in warning for warning in updated["warnings"]))

    def test_re_supplying_the_unchanged_path_is_not_treated_as_a_repoint(self) -> None:
        path = self._dir("unchanged", 0o750)
        record = self._add("Unchanged", path)
        os.chmod(path, 0o777)

        updated = abr_v41.edit_repository_record(
            record["id"], {"path": str(path), "description": "same path"}
        )

        self.assertEqual(updated["path"], str(path))

    def test_repo_list_reports_the_mode_of_a_repository_that_drifted_open(self) -> None:
        path = self._dir("listed", 0o750)
        self._add("Listed", path)
        os.chmod(path, 0o777)

        listed = next(item for item in abr_v41.list_repository_records() if item["name"] == "Listed")

        self.assertEqual(listed["storage"]["permissions"]["mode"], "0777")
        self.assertTrue(listed["storage"]["permissions"]["world_writable"])
        self.assertTrue(any("world-writable" in warning for warning in listed["warnings"]))

    def test_repo_verify_reports_the_mode_without_failing_the_repository(self) -> None:
        path = self._dir("verified-loose", 0o750)
        record = self._add("VerifyLoose", path)
        os.chmod(path, 0o777)

        result = abr_v41.verify_repository(record["id"])

        self.assertEqual(result["status"], "success")
        permissions_check = next(check for check in result["checks"] if check["name"] == "permissions")
        self.assertEqual(permissions_check["status"], "warning")
        self.assertTrue(result["message"].startswith("Repository is available. Warning: "))
        self.assertIn("0777", result["message"])

    def test_repo_verify_of_a_healthy_repository_is_unchanged(self) -> None:
        record = self._add("VerifyHealthy", self._dir("verified-healthy", 0o750))

        result = abr_v41.verify_repository(record["id"])

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["message"], "Repository is available")
        permissions_check = next(check for check in result["checks"] if check["name"] == "permissions")
        self.assertEqual(permissions_check["status"], "ok")
        self.assertIn("0750", permissions_check["message"])

    def test_a_remote_repository_carries_no_local_permission_probe(self) -> None:
        record = abr_v41.create_repository_record(
            {"name": "Remote", "type": "nfs", "address": "fileserver:/export/backups"}
        )

        self.assertIsNone(record["storage"]["permissions"])
        self.assertEqual(record["warnings"], [])

    def test_a_path_that_disappears_after_registration_does_not_break_listing(self) -> None:
        path = self._dir("vanishes", 0o750)
        self._add("Vanishes", path)
        shutil.rmtree(path)

        listed = next(item for item in abr_v41.list_repository_records() if item["name"] == "Vanishes")

        self.assertIsNone(listed["storage"]["permissions"])
        self.assertEqual(listed["warnings"], [])
        self.assertEqual(listed["status"], "unavailable")

    def test_the_missing_and_not_a_directory_errors_still_come_first(self) -> None:
        missing = Path(self._root) / "never-created"
        with self.assertRaises(ApiError) as missing_ctx:
            self._add("Missing", missing)
        self.assertIn("does not exist", missing_ctx.exception.message)

        a_file = Path(self._root) / "a-file"
        a_file.write_text("not a directory")
        os.chmod(a_file, 0o666)
        with self.assertRaises(ApiError) as file_ctx:
            self._add("AFile", a_file)
        self.assertIn("not a directory", file_ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
