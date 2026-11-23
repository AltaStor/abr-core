# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from python.srv.core.mongo_db_wrapper import BACKUPS, REPOSITORIES
from python.srv.core.storage_tool import session_data_folder
from python.srv.plugins.vcenter import vcenter_restore
from python.srv.services import abr_v41
from python.srv.services.api_errors import ApiError

VM = "vm-ext-1"


class MovedFolderImportTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()
        REPOSITORIES.clear()
        self.addCleanup(BACKUPS.clear)
        self.addCleanup(REPOSITORIES.clear)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.old = Path(folder.name) / "old"
        self.new = Path(folder.name) / "new"
        self.new.mkdir()
        REPOSITORIES["new"] = {"id": "new", "name": "New", "type": "local", "path": str(self.new)}

    def _session(self, name: str, created_at: str, kind: str, parent: str | None = None, *, with_parent_path: bool = True) -> Path:
        written = self.old / "job-1" / VM / name
        session = self.new / "job-1" / VM / name
        session.mkdir(parents=True)
        disk = {"index": 1, "abr_path": str(written), "abr_backup_name": "01-disk.vmdk", "store_mode": "incremental" if parent else "full"}
        if parent and with_parent_path:
            disk["parent_manifest_path"] = str(self.old / "job-1" / VM / parent / "manifest.json")
        manifest = {
            "vm_name": "web-01",
            "vm_external_id": VM,
            "created_at": created_at,
            "resolved_backup_type": kind,
            "resolved_backup_reason": "StandardIncremental" if parent else "FirstRun",
            "backup_mode": "incremental" if parent else "full",
            "backup_root": str(written),
            "repository_storage_path": str(written),
            "storage_mode": "local",
            "disks": [disk],
        }
        (session / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return session

    def _import(self, session: Path) -> dict:
        abr_v41.import_backup_chain(path_value=str(session), repo_id="new")
        return next(point for point in abr_v41.normalized_backup_points() if point["artifact"]["manifest_path"] == str(session / "manifest.json"))

    def test_a_full_and_its_incremental_become_one_chain_with_their_own_times(self) -> None:
        full = self._import(self._session("s1", "2026-11-01T01:00:00+00:00", "ActiveFull"))
        incremental = self._import(self._session("s2", "2026-11-02T01:00:00+00:00", "Incremental", "s1"))

        self.assertEqual((full["execution_type"], incremental["execution_type"]), ("ActiveFull", "Incremental"))
        self.assertEqual(incremental["parent_point_id"], full["id"])
        self.assertEqual(incremental["chain_id"], full["chain_id"])
        self.assertEqual(incremental["segment_id"], full["segment_id"])
        self.assertEqual((full["created_at"], incremental["created_at"]), ("2026-11-01T01:00:00+00:00", "2026-11-02T01:00:00+00:00"))
        self.assertEqual((full["vm_id"], full["vm_name"]), (VM, "web-01"))
        self.assertEqual(incremental["artifact"]["disks"][0]["abr_path"], str(self.new / "job-1" / VM / "s2"))

    def test_restore_reads_every_layer_from_the_folder_it_was_imported_from(self) -> None:
        self._import(self._session("s1", "2026-11-01T01:00:00+00:00", "ActiveFull"))
        incremental = self._import(self._session("s2", "2026-11-02T01:00:00+00:00", "Incremental", "s1"))

        layers = vcenter_restore._resolve_abr_restore_layers(
            selected_point=incremental, selected_disk={"index": 1}, disk_index=1, repository=REPOSITORIES["new"]
        )

        self.assertEqual([layer["store_mode"] for layer in layers], ["full", "incremental"])
        self.assertEqual([layer["abr_path"] for layer in layers], [str(self.new / "job-1" / VM / "s1"), str(self.new / "job-1" / VM / "s2")])
        self.assertTrue(all(str(self.old) not in layer["source"] for layer in layers))

    def test_an_incremental_before_its_full_is_refused_and_nothing_is_imported(self) -> None:
        self._session("s1", "2026-11-01T01:00:00+00:00", "ActiveFull")
        session = self._session("s2", "2026-11-02T01:00:00+00:00", "Incremental", "s1")

        with self.assertRaises(ApiError) as caught:
            abr_v41.import_backup_chain(path_value=str(session), repo_id="new")

        self.assertEqual((caught.exception.status_code, caught.exception.code), (409, "import_parent_missing"))
        self.assertIn(f"import {self.new / 'job-1' / VM / 's1'} first", caught.exception.message)
        self.assertEqual(BACKUPS, {})

    def test_an_incremental_without_a_parent_path_builds_on_the_previous_session_of_its_folder(self) -> None:
        full = self._import(self._session("a", "2026-11-01T01:00:00+00:00", "ActiveFull"))
        first = self._import(self._session("b", "2026-11-02T01:00:00+00:00", "Incremental", "a", with_parent_path=False))
        self._session("c", "2026-11-03T01:00:00+00:00", "Incremental", "b", with_parent_path=False)
        gap = self._session("d", "2026-11-04T01:00:00+00:00", "Incremental", "c", with_parent_path=False)

        with self.assertRaises(ApiError) as caught:
            abr_v41.import_backup_chain(path_value=str(gap), repo_id="new")

        self.assertEqual(first["parent_point_id"], full["id"])
        self.assertIn(f"import {self.new / 'job-1' / VM / 'c'} first", caught.exception.message)

    def test_a_session_already_imported_is_refused_and_not_imported_twice(self) -> None:
        session = self._session("s1", "2026-11-01T01:00:00+00:00", "ActiveFull")
        first = self._import(session)

        with self.assertRaises(ApiError) as caught:
            abr_v41.import_backup_chain(path_value=str(session / "manifest.json"), repo_id="new")

        self.assertEqual((caught.exception.status_code, caught.exception.code), (409, "import_already_done"))
        self.assertIn(f"as restore point {first['id']}", caught.exception.message)
        self.assertEqual(len(BACKUPS), 1)

    def test_the_same_session_may_still_be_imported_into_another_repository(self) -> None:
        REPOSITORIES["other"] = {"id": "other", "name": "Other", "type": "local", "path": str(self.new.parent / "other")}
        session = self._session("s1", "2026-11-01T01:00:00+00:00", "ActiveFull")
        self._import(session)

        abr_v41.import_backup_chain(path_value=str(session), repo_id="other")

        self.assertEqual(sorted(point["repository_id"] for point in BACKUPS.values()), ["new", "other"])

    def test_an_imported_point_keeps_its_platform_and_goes_back_to_it(self) -> None:
        session = self._session("s1", "2026-11-01T01:00:00+00:00", "ActiveFull")
        manifest = json.loads((session / "manifest.json").read_text())
        (session / "manifest.json").write_text(json.dumps({**manifest, "hypervisor_kind": "hyper-v"}), encoding="utf-8")

        point = self._import(session)

        self.assertEqual(point["hypervisor_kind"], "hyper-v")
        self.assertTrue(abr_v41._restore_target_kind_matches(point, {"type": "hyper-v"}))
        self.assertFalse(abr_v41._restore_target_kind_matches(point, {"type": "vsphere"}))

    def _verified(self, point: dict) -> tuple[dict, list]:
        seen: list = []
        with patch.object(abr_v41, "list_disk_tool_backups", side_effect=lambda repository, path, **kw: seen.append(("list", repository["id"], path)) or ["01-disk.vmdk"]), patch.object(
            abr_v41, "verify_disk_tool_backup", side_effect=lambda repository, path, name, **kw: seen.append(("verify", repository["id"], path))
        ):
            outcome = abr_v41._verify_disk_artifact(point, point["artifact"]["disks"][0])
        return outcome, seen

    def test_an_imported_chain_verifies_against_its_repository_and_the_folder_it_was_imported_from(self) -> None:
        session = self._session("s1", "2026-11-01T01:00:00+00:00", "ActiveFull")
        point = self._import(session)

        outcome, seen = self._verified(point)

        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(seen[-1], ("verify", "new", str(session)))

    def test_a_point_imported_before_this_change_verifies_too(self) -> None:
        session = self._session("s1", "2026-11-01T01:00:00+00:00", "ActiveFull")
        point = self._import(session)
        legacy = dict(point)
        legacy["artifact"] = {**point["artifact"], "disks": [{**point["artifact"]["disks"][0], "repository_id": "gone", "abr_path": str(self.old / "job-1" / VM / "s1")}]}

        outcome, seen = self._verified(legacy)

        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(seen[-1], ("verify", "new", str(session)))

    def test_a_disk_of_a_repository_that_is_gone_is_still_reported(self) -> None:
        session = self._session("s1", "2026-11-01T01:00:00+00:00", "ActiveFull")
        point = {**self._import(session), "repository_id": "missing"}
        point["artifact"] = {**point["artifact"], "disks": [{**point["artifact"]["disks"][0], "repository_id": "missing"}]}

        outcome, _seen = self._verified(point)

        self.assertEqual((outcome["status"], outcome["message"]), ("failed", "Repository not found for backup artifact"))

    def test_a_chain_restored_where_it_was_written_keeps_its_recorded_folder(self) -> None:
        manifest = {"backup_root": "/repo/job/vm/s1", "storage_mode": "local"}

        self.assertEqual(session_data_folder(manifest, "/repo/job/vm/s1/manifest.json", "/repo/job/vm/s1"), "/repo/job/vm/s1")
        self.assertEqual(session_data_folder(manifest, "/moved/job/vm/s1/manifest.json", "/repo/job/vm/s1"), "/moved/job/vm/s1")

    def test_a_folder_the_session_did_not_write_and_s3_paths_are_left_alone(self) -> None:
        local = {"backup_root": "/repo/job/vm/s2", "storage_mode": "local"}
        s3 = {"backup_root": "/cache/s1", "repository_storage_path": "prefix/job/vm/s1", "storage_mode": "s3"}

        self.assertEqual(session_data_folder(local, "/moved/job/vm/s2/manifest.json", "/elsewhere/data"), "/elsewhere/data")
        self.assertEqual(session_data_folder(s3, "/cache/s1/manifest.json", "prefix/job/vm/s1"), "prefix/job/vm/s1")


if __name__ == "__main__":
    unittest.main()
