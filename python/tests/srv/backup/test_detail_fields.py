# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    repo_root = pathlib.Path(__file__).resolve().parents[4]
    venv_lib = repo_root / ".venv" / "lib"
    for site_packages in sorted(venv_lib.glob("python*/site-packages")):
        sys.path.insert(0, str(site_packages))
    from fastapi.testclient import TestClient

from python.srv.backup_server import app
from python.srv.core.mongo_db_wrapper import BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, SESSIONS, USERS
from python.srv.services import abr_v41, auth_service, backup_catalog

BOOTSTRAP_ENV = {
    "ABR_BOOTSTRAP_ADMIN_USER": "admin",
    "ABR_BOOTSTRAP_ADMIN_PASSWORD": "Sup3rSecretPW",
}


def _completed_backup_task(**overrides) -> dict:
    task = {
        "id": "task-detail-1",
        "task_type": "backup",
        "status": "completed",
        "hypervisor_id": "",
        "resource_type": "virtual_machine",
        "resource_external_id": "vm-1",
        "job_id": None,
        "updated_at": "2026-01-01T00:00:00+00:00",
        "result": {
            "vm_name": "demo-vm",
            "vm_external_id": "vm-1",
            "backup_mode": "full",
            "resolved_backup_type": "ActiveFull",
            "resolved_backup_reason": "FirstRun",
            "transferred_bytes": 2048,
            "compression_level": 6,
            "encryption_enabled": True,
            "encryption_algorithm": 1,
            "finished_at": "2026-01-01T00:05:00+00:00",
        },
    }
    task.update(overrides)
    return task


class RegisterBackupFieldsTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()
        HYPERVISORS.clear()
        JOBS.clear()

    def tearDown(self) -> None:
        BACKUPS.clear()
        HYPERVISORS.clear()
        JOBS.clear()

    def test_new_backup_carries_compression_and_encryption_from_the_task_result(self) -> None:
        record = backup_catalog.register_backup_from_completed_task(_completed_backup_task())
        self.assertEqual(record["compression_level"], 6)
        self.assertEqual(record["encryption_enabled"], True)
        self.assertEqual(record["size_bytes"], 2048)

    def test_disabled_encryption_and_default_compression_are_recorded_too(self) -> None:
        task = _completed_backup_task()
        task["result"]["compression_level"] = 0
        task["result"]["encryption_enabled"] = False
        record = backup_catalog.register_backup_from_completed_task(task)
        self.assertEqual(record["compression_level"], 0)
        self.assertEqual(record["encryption_enabled"], False)

    def test_missing_compression_level_in_the_result_stays_unknown_not_zero(self) -> None:
        task = _completed_backup_task()
        del task["result"]["compression_level"]
        record = backup_catalog.register_backup_from_completed_task(task)
        self.assertIsNone(record["compression_level"])


    def test_a_disk_left_out_of_the_backup_is_recorded_on_the_restore_point(self) -> None:
        skipped = {
            "disk_label": "Hard disk 3",
            "datastore_path": "[ds] demo-vm/extra.vmdk",
            "uuid": "disk-uuid-3",
            "capacity_gb": 1,
            "disk_mode": "independent_persistent",
            "reason": "independent disks are not part of a VM snapshot",
        }
        task = _completed_backup_task()
        task["result"]["skipped_disks"] = [skipped]
        record = backup_catalog.register_backup_from_completed_task(task)
        self.assertEqual(record["artifact"]["skipped_disks"], [skipped])
        self.assertEqual(abr_v41.restore_point_to_dto(record)["skipped_disks"], [skipped])
        self.assertEqual(abr_v41.backup_chain_to_dto([record])["restore_points"][0]["skipped_disks"], [skipped])

    def test_a_backup_that_left_nothing_out_records_no_skipped_disks(self) -> None:
        record = backup_catalog.register_backup_from_completed_task(_completed_backup_task())
        self.assertEqual(record["artifact"]["skipped_disks"], [])
        self.assertEqual(abr_v41.restore_point_to_dto(record)["skipped_disks"], [])
        self.assertEqual(abr_v41.restore_point_to_dto({"id": "legacy"})["skipped_disks"], [])
        self.assertEqual(abr_v41.backup_chain_to_dto([record])["restore_points"][0]["skipped_disks"], [])

    def test_a_jobs_first_full_in_id_folders_flags_the_job_for_good(self) -> None:
        JOBS["job-old"] = {"id": "job-old", "name": "Old", "job_type": "backup", "vm_ids": ["vm-1"], "vm_names": ["demo-vm"]}
        task = _completed_backup_task(job_id="job-old")
        task["result"]["storage_layout"] = "by_id"
        backup_catalog.register_backup_from_completed_task(task)
        self.assertEqual(JOBS["job-old"]["storage_layout"], "by_id")
        self.assertEqual(abr_v41.job_to_dto(abr_v41.normalize_job_record(JOBS["job-old"]))["storage_layout"], "by_id")

    def test_a_backup_in_name_folders_leaves_an_unflagged_job_as_it_is(self) -> None:
        JOBS["job-old"] = {"id": "job-old", "name": "Old", "job_type": "backup", "vm_ids": ["vm-1"], "vm_names": ["demo-vm"]}
        task = _completed_backup_task(id="task-detail-2", job_id="job-old")
        task["result"]["storage_layout"] = "by_name"
        backup_catalog.register_backup_from_completed_task(task)
        self.assertNotIn("storage_layout", JOBS["job-old"])
        self.assertEqual(abr_v41.job_to_dto(abr_v41.normalize_job_record(JOBS["job-old"]))["storage_layout"], "by_name")

class EmptySourceSizeTests(unittest.TestCase):
    def test_a_source_with_nothing_allocated_reports_a_logical_size_of_zero(self) -> None:
        self.assertEqual(backup_catalog.resolve_logical_size_bytes({"logical_bytes": 0}), 0)

    def test_an_engine_that_reports_nothing_still_has_no_logical_size(self) -> None:
        for raw in (None, "", "not a number"):
            with self.subTest(raw=raw):
                self.assertIsNone(backup_catalog.resolve_logical_size_bytes({"logical_bytes": raw}))
        self.assertIsNone(backup_catalog.resolve_logical_size_bytes({}))

    def test_a_negative_count_is_not_a_size(self) -> None:
        self.assertIsNone(backup_catalog.resolve_logical_size_bytes({"logical_bytes": -1}))

    def test_a_real_count_is_kept(self) -> None:
        self.assertEqual(backup_catalog.resolve_logical_size_bytes({"logical_bytes": 5724823552}), 5724823552)

    def test_nothing_stored_over_nothing_logical_yields_no_ratio(self) -> None:
        self.assertIsNone(
            backup_catalog.resolve_compression_ratio({"logical_bytes": 0, "transferred_bytes": 0})
        )

    def test_a_ratio_is_still_derived_when_both_counts_are_real(self) -> None:
        ratio = backup_catalog.resolve_compression_ratio(
            {"logical_bytes": 5724823552, "transferred_bytes": 2090978456}
        )

        self.assertEqual(ratio, 0.365248)


class BackupEncryptionKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()
        HYPERVISORS.clear()
        JOBS.clear()
        self.addCleanup(BACKUPS.clear)
        self.addCleanup(HYPERVISORS.clear)
        self.addCleanup(JOBS.clear)

    def _job(self, key: str = "job-key") -> dict:
        JOBS["job-1"] = {
            "id": "job-1",
            "name": "Encrypted",
            "job_type": "backup",
            "task_parameters": {"encryption_enabled": True, "encryption_key": key},
        }
        return JOBS["job-1"]

    def test_a_new_point_keeps_the_key_the_backup_was_written_with(self) -> None:
        self._job()
        task = _completed_backup_task(job_id="job-1")
        task["parameters"] = {"encryption_enabled": True, "encryption_key": "key-used-by-this-run"}

        record = backup_catalog.register_backup_from_completed_task(task)

        self.assertEqual(record["encryption_key"], "key-used-by-this-run")

    def test_a_new_point_falls_back_to_the_job_key(self) -> None:
        self._job("job-key")
        record = backup_catalog.register_backup_from_completed_task(_completed_backup_task(job_id="job-1"))

        self.assertEqual(record["encryption_key"], "job-key")

    def test_an_unencrypted_point_stores_no_key(self) -> None:
        task = _completed_backup_task()
        task["result"]["encryption_enabled"] = False
        task["parameters"] = {"encryption_key": "leftover"}

        record = backup_catalog.register_backup_from_completed_task(task)

        self.assertIsNone(record["encryption_key"])

    def test_the_backfill_copies_the_job_key_onto_older_points(self) -> None:
        self._job("job-key")
        BACKUPS["old-encrypted"] = {"id": "old-encrypted", "job_id": "job-1", "encryption_enabled": True}
        BACKUPS["old-plain"] = {"id": "old-plain", "job_id": "job-1", "encryption_enabled": False}

        updated = backup_catalog.backfill_backup_encryption_keys()

        self.assertEqual(updated, 1)
        self.assertEqual(BACKUPS["old-encrypted"]["encryption_key"], "job-key")
        self.assertNotIn("encryption_key", BACKUPS["old-plain"])

    def test_the_backfill_leaves_a_point_that_already_has_a_key_alone(self) -> None:
        self._job("job-key")
        BACKUPS["kept"] = {
            "id": "kept",
            "job_id": "job-1",
            "encryption_enabled": True,
            "encryption_key": "written-with-this",
        }

        updated = backup_catalog.backfill_backup_encryption_keys()

        self.assertEqual(updated, 0)
        self.assertEqual(BACKUPS["kept"]["encryption_key"], "written-with-this")

    def test_the_backfill_skips_a_point_whose_job_is_gone(self) -> None:
        BACKUPS["orphan"] = {"id": "orphan", "job_id": "job-that-is-gone", "encryption_enabled": True}

        updated = backup_catalog.backfill_backup_encryption_keys()

        self.assertEqual(updated, 0)
        self.assertNotIn("encryption_key", BACKUPS["orphan"])


class BackfillDetailFieldsTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self) -> None:
        BACKUPS.clear()

    def _manifest(self, **fields) -> str:
        path = pathlib.Path(self._tmp.name) / f"manifest-{len(fields)}-{fields.get('compression_level')}.json"
        path.write_text(json.dumps(fields), encoding="utf-8")
        return str(path)

    def test_backfills_an_old_point_from_its_manifest_on_disk(self) -> None:
        manifest_path = self._manifest(compression_level=4, encryption_enabled=True)
        BACKUPS["old-1"] = {
            "id": "old-1",
            "size_bytes": 999,
            "artifact": {"manifest_path": manifest_path},
        }

        updated = backup_catalog.backfill_backup_detail_fields()

        self.assertEqual(updated, 1)
        self.assertEqual(BACKUPS["old-1"]["compression_level"], 4)
        self.assertEqual(BACKUPS["old-1"]["encryption_enabled"], True)
        self.assertEqual(BACKUPS["old-1"]["size_bytes"], 999)

    def test_backfills_logical_size_and_compression_ratio_from_the_manifest(self) -> None:
        manifest_path = self._manifest(
            compression_level=3,
            encryption_enabled=False,
            logical_bytes=8_471_179_264,
            transferred_bytes=3_016_784_896,
            compression_ratio=0.356124,
        )
        BACKUPS["sized-1"] = {"id": "sized-1", "artifact": {"manifest_path": manifest_path}}

        backup_catalog.backfill_backup_detail_fields()

        self.assertEqual(BACKUPS["sized-1"]["logical_size_bytes"], 8_471_179_264)
        self.assertAlmostEqual(BACKUPS["sized-1"]["compression_ratio"], 0.356124)

    def test_a_manifest_without_sizes_leaves_them_unknown_rather_than_zero(self) -> None:
        manifest_path = self._manifest(compression_level=4, encryption_enabled=True)
        BACKUPS["sizeless"] = {"id": "sizeless", "artifact": {"manifest_path": manifest_path}}

        backup_catalog.backfill_backup_detail_fields()

        self.assertIsNone(BACKUPS["sizeless"]["logical_size_bytes"])
        self.assertIsNone(BACKUPS["sizeless"]["compression_ratio"])

    def test_a_point_that_already_has_every_field_is_skipped(self) -> None:
        BACKUPS["already-done"] = {
            "id": "already-done",
            "compression_level": 9,
            "encryption_enabled": False,
            "compression_ratio": 0.5,
            "artifact": {"manifest_path": "/does/not/matter"},
        }
        updated = backup_catalog.backfill_backup_detail_fields()
        self.assertEqual(updated, 0)
        self.assertEqual(BACKUPS["already-done"]["compression_level"], 9)

    def test_a_point_predating_the_size_fields_is_revisited_not_skipped(self) -> None:
        # The older backfill only ever wrote compression_level and
        # encryption_enabled, so a point it already touched still has no sizes.
        manifest_path = self._manifest(
            compression_level=6,
            encryption_enabled=True,
            logical_bytes=1_000,
            transferred_bytes=250,
        )
        BACKUPS["half-done"] = {
            "id": "half-done",
            "compression_level": 6,
            "encryption_enabled": True,
            "artifact": {"manifest_path": manifest_path},
        }

        updated = backup_catalog.backfill_backup_detail_fields()

        self.assertEqual(updated, 1)
        self.assertEqual(BACKUPS["half-done"]["logical_size_bytes"], 1_000)
        self.assertAlmostEqual(BACKUPS["half-done"]["compression_ratio"], 0.25)

    def test_is_idempotent_the_second_call_is_a_no_op(self) -> None:
        manifest_path = self._manifest(compression_level=2, encryption_enabled=False)
        BACKUPS["old-2"] = {"id": "old-2", "artifact": {"manifest_path": manifest_path}}

        first = backup_catalog.backfill_backup_detail_fields()
        second = backup_catalog.backfill_backup_detail_fields()

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)

    def test_a_missing_manifest_file_is_recorded_as_unknown_not_retried_forever(self) -> None:
        BACKUPS["gone"] = {"id": "gone", "artifact": {"manifest_path": "/no/such/file.json"}}
        first = backup_catalog.backfill_backup_detail_fields()
        self.assertEqual(first, 1)
        self.assertIsNone(BACKUPS["gone"]["compression_level"])
        self.assertEqual(BACKUPS["gone"]["encryption_enabled"], False)
        second = backup_catalog.backfill_backup_detail_fields()
        self.assertEqual(second, 0)

    def test_a_point_with_no_manifest_path_at_all_is_still_marked_unknown(self) -> None:
        BACKUPS["no-artifact"] = {"id": "no-artifact"}
        updated = backup_catalog.backfill_backup_detail_fields()
        self.assertEqual(updated, 1)
        self.assertIsNone(BACKUPS["no-artifact"]["compression_level"])

    def test_a_missing_manifest_is_recovered_via_abr_load_meta_when_a_repository_is_known(self) -> None:
        REPOSITORIES.clear()
        self.addCleanup(REPOSITORIES.clear)
        REPOSITORIES["repo-1"] = {"id": "repo-1", "type": "local", "path": self._tmp.name}
        manifest_path = pathlib.Path(self._tmp.name) / "missing" / "manifest.json"
        BACKUPS["recoverable"] = {
            "id": "recoverable",
            "repository_id": "repo-1",
            "artifact": {
                "manifest_path": str(manifest_path),
                "disks": [{"abr_path": "vm-1/task-1", "abr_backup_name": "01-disk.vmdk"}],
            },
        }

        def _fake_hydrate(repository, abr_path, path, **kwargs):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"compression_level": 6, "encryption_enabled": True}), encoding="utf-8")
            return True

        with patch("python.srv.services.backup_catalog.hydrate_manifest_via_disk_tool", side_effect=_fake_hydrate) as hydrate_mock:
            updated = backup_catalog.backfill_backup_detail_fields()

        hydrate_mock.assert_called_once()
        self.assertEqual(hydrate_mock.call_args.args[1], "vm-1/task-1")
        self.assertEqual(updated, 1)
        self.assertEqual(BACKUPS["recoverable"]["compression_level"], 6)
        self.assertEqual(BACKUPS["recoverable"]["encryption_enabled"], True)

    def test_a_missing_manifest_stays_unknown_when_hydration_fails(self) -> None:
        REPOSITORIES.clear()
        self.addCleanup(REPOSITORIES.clear)
        REPOSITORIES["repo-1"] = {"id": "repo-1", "type": "local", "path": self._tmp.name}
        manifest_path = pathlib.Path(self._tmp.name) / "missing" / "manifest.json"
        BACKUPS["unrecoverable"] = {
            "id": "unrecoverable",
            "repository_id": "repo-1",
            "artifact": {
                "manifest_path": str(manifest_path),
                "disks": [{"abr_path": "vm-1/task-1", "abr_backup_name": "01-disk.vmdk"}],
            },
        }

        with patch("python.srv.services.backup_catalog.hydrate_manifest_via_disk_tool", return_value=False):
            updated = backup_catalog.backfill_backup_detail_fields()

        self.assertEqual(updated, 1)
        self.assertIsNone(BACKUPS["unrecoverable"]["compression_level"])
        self.assertEqual(BACKUPS["unrecoverable"]["encryption_enabled"], False)

    def test_corrupt_manifest_json_does_not_raise(self) -> None:
        path = pathlib.Path(self._tmp.name) / "corrupt.json"
        path.write_text("{not valid json", encoding="utf-8")
        BACKUPS["corrupt"] = {"id": "corrupt", "artifact": {"manifest_path": str(path)}}
        updated = backup_catalog.backfill_backup_detail_fields()
        self.assertEqual(updated, 1)
        self.assertIsNone(BACKUPS["corrupt"]["compression_level"])


class BackupChainDtoFieldsTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()

    def tearDown(self) -> None:
        BACKUPS.clear()

    def test_list_and_get_expose_compression_and_encryption_per_restore_point(self) -> None:
        point = abr_v41.normalize_backup_point(
            {
                "id": "rp-detail-1",
                "job_id": "job-1",
                "vm_id": "vm-1",
                "repository_id": "repo-1",
                "size_bytes": 4096,
                "compression_level": 7,
                "encryption_enabled": True,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        BACKUPS[point["id"]] = point

        [chain] = abr_v41.list_backup_chains()
        [restore_point] = chain["restore_points"]
        self.assertEqual(restore_point["compression_level"], 7)
        self.assertEqual(restore_point["encryption_enabled"], True)
        self.assertEqual(restore_point["size_bytes"], 4096)

        fetched = abr_v41.get_backup_chain(chain["id"])
        self.assertEqual(fetched["restore_points"][0]["compression_level"], 7)


class ImportBackupChainDetailFieldsTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()
        REPOSITORIES.clear()
        REPOSITORIES["repo-import"] = {
            "id": "repo-import",
            "name": "import-target",
            "type": "local",
            "status": "available",
        }
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self) -> None:
        BACKUPS.clear()
        REPOSITORIES.clear()

    def _manifest_dir(self, **fields) -> str:
        directory = pathlib.Path(self._tmp.name) / "import-source"
        directory.mkdir(exist_ok=True)
        (directory / "manifest.json").write_text(json.dumps(fields), encoding="utf-8")
        return str(directory)

    def test_import_carries_compression_and_encryption_immediately(self) -> None:
        source = self._manifest_dir(vm_name="demo-vm", compression_level=6, encryption_enabled=True)
        chain = abr_v41.import_backup_chain(path_value=source, repo_id="repo-import")
        point = chain["restore_points"][0]
        self.assertEqual(point["compression_level"], 6)
        self.assertEqual(point["encryption_enabled"], True)

    def test_import_without_those_keys_in_the_manifest_stays_unknown(self) -> None:
        source = self._manifest_dir(vm_name="demo-vm")
        chain = abr_v41.import_backup_chain(path_value=source, repo_id="repo-import")
        point = chain["restore_points"][0]
        self.assertIsNone(point["compression_level"])
        self.assertEqual(point["encryption_enabled"], False)

    def test_import_ignores_a_non_numeric_compression_level(self) -> None:
        source = self._manifest_dir(vm_name="demo-vm", compression_level="not-a-number")
        chain = abr_v41.import_backup_chain(path_value=source, repo_id="repo-import")
        self.assertIsNone(chain["restore_points"][0]["compression_level"])


class BackupApiResponseTests(unittest.TestCase):
    def setUp(self) -> None:
        USERS.clear()
        SESSIONS.clear()
        BACKUPS.clear()
        REPOSITORIES.clear()
        auth_service.reset_login_lockout_state_for_testing()
        self._env_patcher = patch.dict("os.environ", BOOTSTRAP_ENV)
        self._env_patcher.start()
        self.client_cm = TestClient(app)
        self.client = self.client_cm.__enter__()
        login = self.client.post("/api/v1/auth/login", json={"name": "admin", "password": "Sup3rSecretPW"})
        self.headers = {"Authorization": f"Bearer {login.json()['token']}"}

        point = abr_v41.normalize_backup_point(
            {
                "id": "rp-api-1",
                "job_id": "job-1",
                "vm_id": "vm-1",
                "repository_id": "repo-1",
                "size_bytes": 123456,
                "compression_level": 3,
                "encryption_enabled": False,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        BACKUPS[point["id"]] = point
        self.chain_id = point["chain_id"]

    def tearDown(self) -> None:
        self.client_cm.__exit__(None, None, None)
        self._env_patcher.stop()
        USERS.clear()
        SESSIONS.clear()
        BACKUPS.clear()
        REPOSITORIES.clear()

    def test_list_backups_returns_the_detail_fields_over_http(self) -> None:
        response = self.client.get("/api/v1/backups", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        [point] = response.json()["items"][0]["restore_points"]
        self.assertEqual(point["size_bytes"], 123456)
        self.assertEqual(point["compression_level"], 3)
        self.assertEqual(point["encryption_enabled"], False)

    def test_get_backup_returns_the_detail_fields_over_http(self) -> None:
        response = self.client.get(f"/api/v1/backups/{self.chain_id}", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        point = response.json()["restore_points"][0]
        self.assertEqual(point["compression_level"], 3)
        self.assertEqual(point["encryption_enabled"], False)


class LifespanBackfillWiringTests(unittest.TestCase):
    def test_a_legacy_point_is_backfilled_during_real_server_startup(self) -> None:
        import python.srv.backup_server as backup_server_module

        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = pathlib.Path(tmp) / "manifest.json"
            manifest_path.write_text(
                json.dumps({"compression_level": 5, "encryption_enabled": True}), encoding="utf-8"
            )
            legacy_point = {
                "id": "legacy-point",
                "status": "available",
                "size_bytes": 555,
                "job_id": "job-x",
                "vm_id": "vm-x",
                "repository_id": "repo-x",
                "created_at": "2020-01-01T00:00:00+00:00",
                "artifact": {"manifest_path": str(manifest_path)},
            }
            with patch.object(backup_server_module, "load_backups", return_value={"legacy-point": legacy_point}):
                with TestClient(backup_server_module.app):
                    record = BACKUPS["legacy-point"]
                    self.assertEqual(record["compression_level"], 5)
                    self.assertEqual(record["encryption_enabled"], True)


if __name__ == "__main__":
    unittest.main()
