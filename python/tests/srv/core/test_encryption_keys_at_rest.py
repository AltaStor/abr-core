# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from python.srv.core import credential_encryption, mongo_db_wrapper
from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    RESOURCES_BY_HV,
    TASKS,
)
from python.srv.core.storage_tool import StorageToolError, split_disk_tool_secrets
from python.srv.plugins.vcenter.vcenter_backup import (
    VCenterBackupError,
    _resolve_encryption_settings,
)
from python.srv.services import abr_v41
from python.srv.services.api_errors import ApiError

KEY_1 = "placeholder-key-one-1234"
KEY_2 = "placeholder-key-two-5678"


class _RecordingDatabase:
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, dict]] = {}

    def find(self, collection: str, query: dict) -> list[dict]:
        return [copy.deepcopy(row) for row in self.collections.setdefault(collection, {}).values()]

    def upsert(self, collection: str, id_field: str, document: dict) -> None:
        self.collections.setdefault(collection, {})[document[id_field]] = copy.deepcopy(document)

    def get_queued_tasks(self, limit: int | None = None) -> list[dict]:
        rows = self.collections.get("tasks", {}).values()
        return [copy.deepcopy(row) for row in rows if row.get("status") == "queued"]

    def claim_task(self, task_id: str) -> dict | None:
        row = self.collections.get("tasks", {}).get(task_id)
        if not row or row.get("status") != "queued":
            return None
        row["status"] = "running"
        return copy.deepcopy(row)

    def everything_written(self) -> str:
        return json.dumps(self.collections)


def _job(key: str = KEY_1, keys: list[str] | None = None) -> dict:
    return {
        "id": "job-1",
        "name": "encrypted-job",
        "task_parameters": {
            "encryption_enabled": True,
            "encryption_key": key,
            "encryption_keys": [{"key_id": index, "key": value} for index, value in enumerate(keys or [key])],
        },
    }


def _point(key: str = KEY_1) -> dict:
    return {"id": "rp-1", "job_id": "job-1", "encryption_enabled": True, "encryption_key": key, "encryption_key_id": 0}


def _task(key: str = KEY_1, status: str = "queued") -> dict:
    return {
        "id": "task-1",
        "job_id": "job-1",
        "status": status,
        "parameters": {
            "encryption_enabled": True,
            "encryption_key": key,
            "encryption_keys": [{"key_id": 0, "key": key}],
        },
    }


def _foreign(value: str) -> str:
    return credential_encryption.ENCRYPTED_PREFIX + Fernet(Fernet.generate_key()).encrypt(value.encode()).decode()


class _KeyFileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.key_path = Path(self._tmp.name) / "credentials.key"
        env = patch.dict(os.environ, {credential_encryption.KEY_FILE_ENV: str(self.key_path)})
        env.start()
        self.addCleanup(env.stop)
        credential_encryption.reset_cipher()
        self.addCleanup(credential_encryption.reset_cipher)


class _CatalogTestCase(_KeyFileTestCase):
    def setUp(self) -> None:
        super().setUp()
        for store in (BACKUPS, JOBS, TASKS):
            store.clear()
            self.addCleanup(store.clear)
        self.db = _RecordingDatabase()
        handle = patch.object(mongo_db_wrapper, "get_database", lambda: self.db)
        handle.start()
        self.addCleanup(handle.stop)

    def _break_the_key_file(self) -> None:
        self.key_path.write_bytes(b"")
        credential_encryption.reset_cipher()


class EncryptionKeysAtRestTests(_CatalogTestCase):
    def test_a_job_its_restore_points_and_sessions_reach_the_database_encrypted(self) -> None:
        mongo_db_wrapper.persist_job(_job(KEY_2, [KEY_1, KEY_2]))
        mongo_db_wrapper.persist_backup(_point())
        mongo_db_wrapper.persist_task(_task())

        written = self.db.everything_written()
        self.assertNotIn(KEY_1, written)
        self.assertNotIn(KEY_2, written)
        stored = self.db.collections["jobs"]["job-1"]["task_parameters"]["encryption_key"]
        self.assertTrue(stored.startswith(credential_encryption.ENCRYPTED_PREFIX))
        self.assertEqual(JOBS["job-1"]["task_parameters"]["encryption_key"], KEY_2)
        self.assertEqual(BACKUPS["rp-1"]["encryption_key"], KEY_1)
        self.assertEqual(TASKS["task-1"]["parameters"]["encryption_keys"][0]["key"], KEY_1)

    def test_what_was_stored_encrypted_loads_back_as_the_keys_themselves(self) -> None:
        mongo_db_wrapper.persist_job(_job(KEY_2, [KEY_1, KEY_2]))
        mongo_db_wrapper.persist_backup(_point())
        mongo_db_wrapper.persist_task(_task())
        credential_encryption.reset_cipher()

        jobs = mongo_db_wrapper.load_jobs()
        self.assertEqual(jobs["job-1"]["task_parameters"]["encryption_key"], KEY_2)
        keys = [entry["key"] for entry in jobs["job-1"]["task_parameters"]["encryption_keys"]]
        self.assertEqual(keys, [KEY_1, KEY_2])
        self.assertEqual(mongo_db_wrapper.load_backups()["rp-1"]["encryption_key"], KEY_1)
        self.assertEqual(mongo_db_wrapper.load_tasks()["task-1"]["parameters"]["encryption_key"], KEY_1)

    def test_a_queued_session_is_handed_to_its_worker_with_the_key_itself(self) -> None:
        mongo_db_wrapper.persist_task(_task())
        TASKS.clear()

        queued = mongo_db_wrapper.load_queued_tasks()
        claimed = mongo_db_wrapper.claim_task("task-1")

        self.assertEqual(queued[0]["parameters"]["encryption_key"], KEY_1)
        self.assertEqual(claimed["parameters"]["encryption_keys"][0]["key"], KEY_1)
        self.assertEqual(TASKS["task-1"]["parameters"]["encryption_key"], KEY_1)

    def test_keys_stored_in_clear_by_an_older_server_are_encrypted_when_loaded(self) -> None:
        self.db.upsert("jobs", "id", _job())
        self.db.upsert("backups", "id", _point())
        self.db.upsert("tasks", "id", _task(status="completed"))

        with self.assertLogs(mongo_db_wrapper.LOGGER, "INFO") as logs:
            jobs = mongo_db_wrapper.load_jobs()
            mongo_db_wrapper.load_backups()
            mongo_db_wrapper.load_tasks()

        self.assertNotIn(KEY_1, self.db.everything_written())
        self.assertEqual(jobs["job-1"]["task_parameters"]["encryption_key"], KEY_1)
        self.assertIn("Encrypted the stored encryption keys of 1 jobs.", "\n".join(logs.output))

    def test_a_record_without_keys_is_neither_rewritten_nor_changed(self) -> None:
        plain = {"id": "job-2", "name": "plain", "task_parameters": {"encryption_enabled": False, "encryption_key": ""}}
        self.db.upsert("jobs", "id", plain)
        rewritten = patch.object(self.db, "upsert", wraps=self.db.upsert)

        with rewritten as upsert:
            jobs = mongo_db_wrapper.load_jobs()

        upsert.assert_not_called()
        self.assertEqual(jobs["job-2"]["task_parameters"], plain["task_parameters"])

    def test_a_key_encrypted_with_another_credential_key_is_named_and_left_as_it_is(self) -> None:
        foreign = _foreign(KEY_1)
        self.db.upsert("jobs", "id", _job(foreign))

        with self.assertLogs(mongo_db_wrapper.LOGGER, "ERROR") as logs:
            jobs = mongo_db_wrapper.load_jobs()

        self.assertEqual(jobs["job-1"]["task_parameters"]["encryption_key"], foreign)
        self.assertIn("encryption keys of jobs encrypted-job cannot be decrypted", "\n".join(logs.output))


    def test_an_export_carries_the_keys_and_an_import_stores_them_encrypted(self) -> None:
        mongo_db_wrapper.persist_job(_job())
        mongo_db_wrapper.persist_backup(_point())
        snapshot = mongo_db_wrapper.snapshot_catalog()
        self.db.collections.clear()
        credential_encryption.reset_cipher()
        self.key_path.unlink()

        mongo_db_wrapper.restore_catalog(snapshot)

        self.assertEqual(snapshot["jobs"]["job-1"]["task_parameters"]["encryption_key"], KEY_1)
        self.assertNotIn(KEY_1, self.db.everything_written())
        credential_encryption.reset_cipher()
        self.assertEqual(mongo_db_wrapper.load_backups()["rp-1"]["encryption_key"], KEY_1)


class UnusableKeyFileTests(_CatalogTestCase):
    def test_a_new_job_key_is_refused_rather_than_stored_in_clear(self) -> None:
        self._break_the_key_file()

        with self.assertRaises(ApiError) as caught:
            mongo_db_wrapper.persist_job(_job())

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.code, "credential_key_unavailable")
        self.assertNotIn("job-1", self.db.collections.get("jobs", {}))
        self.assertNotIn("job-1", JOBS)

    def test_a_job_whose_key_did_not_change_is_still_saved(self) -> None:
        self.db.upsert("jobs", "id", _job())
        self._break_the_key_file()
        with self.assertLogs(mongo_db_wrapper.LOGGER, "ERROR"):
            JOBS.update(mongo_db_wrapper.load_jobs())

        parameters = {**JOBS["job-1"]["task_parameters"], "encryption_keys": [{"key_id": 0, "key": KEY_1}]}

        mongo_db_wrapper.persist_job({**JOBS["job-1"], "name": "renamed", "task_parameters": parameters})

        self.assertEqual(self.db.collections["jobs"]["job-1"]["name"], "renamed")

    def test_keys_stored_in_clear_stay_as_they_are_and_the_reason_is_logged(self) -> None:
        self.db.upsert("backups", "id", _point())
        self._break_the_key_file()

        with self.assertLogs(mongo_db_wrapper.LOGGER, "ERROR") as logs:
            points = mongo_db_wrapper.load_backups()

        self.assertEqual(points["rp-1"]["encryption_key"], KEY_1)
        self.assertIn("stay as they are until it is fixed", "\n".join(logs.output))

    def test_a_session_or_point_carrying_a_known_key_is_still_saved(self) -> None:
        self._break_the_key_file()

        mongo_db_wrapper.persist_backup(_point())
        mongo_db_wrapper.persist_task(_task())

        self.assertIn("rp-1", self.db.collections["backups"])
        self.assertIn("task-1", self.db.collections["tasks"])


class UndecryptableKeyIsNeverUsedTests(_KeyFileTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.foreign = _foreign(KEY_1)

    def test_a_backup_refuses_a_job_key_it_cannot_decrypt(self) -> None:
        with self.assertRaises(VCenterBackupError) as caught:
            _resolve_encryption_settings({"encryption_enabled": True, "encryption_key": self.foreign})

        self.assertIn("The stored encryption key of this job cannot be decrypted", str(caught.exception))

    def test_a_backup_with_a_readable_key_is_unchanged(self) -> None:
        self.assertEqual(_resolve_encryption_settings({"encryption_enabled": True, "encryption_key": KEY_1})[1], KEY_1)

    def test_the_disk_tool_is_never_handed_an_encrypted_key(self) -> None:
        for option in ("--key", "--source-key"):
            with self.subTest(option=option), self.assertRaises(StorageToolError) as caught:
                split_disk_tool_secrets(["abr-vmdk-tool", "restore", option, self.foreign])

            self.assertIn("cannot be decrypted", str(caught.exception))

    def test_the_disk_tool_still_gets_a_readable_key_on_its_standard_input(self) -> None:
        argv, secrets = split_disk_tool_secrets(["abr-vmdk-tool", "restore", "--key", KEY_1])

        self.assertNotIn(KEY_1, argv)
        self.assertEqual(secrets, [("key", KEY_1)])


class RestoreKeyTests(_KeyFileTestCase):
    def setUp(self) -> None:
        super().setUp()
        registrable = patch("python.srv.services.abr_v41.assert_inventory_registrable", return_value=None)
        registrable.start()
        self.addCleanup(registrable.stop)
        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            store.clear()
        stores = (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS)
        self.addCleanup(lambda: [store.clear() for store in stores])
        self.inventory = abr_v41.create_inventory_record(
            {"name": "esxi", "type": "vsphere", "address": "192.0.2.37", "username": "root", "password": "secret"}
        )
        RESOURCES_BY_HV[self.inventory["id"]][("host", "esx-01.local")] = {
            "resource_type": "host",
            "external_id": "esx-01.local",
            "name": "esx-01.local",
            "payload": {"datastores": ["tstds"]},
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        self.repository = abr_v41.create_repository_record(
            {"name": "RepoA", "type": "local", "path": tempfile.mkdtemp()}
        )

    def _seed_point(self, key: str) -> None:
        BACKUPS["rp-1"] = abr_v41.normalize_backup_point(
            {
                "id": "rp-1",
                "chain_id": "backup-1",
                "vm_id": "vm-1",
                "vm_name": "tstvm",
                "inventory_id": self.inventory["id"],
                "inventory_name": "esxi",
                "repository_id": self.repository["id"],
                "repository_name": "RepoA",
                "execution_type": "ActiveFull",
                "created_at": "2026-05-26T20:00:00+00:00",
                "artifact": {"manifest_path": "/tmp/backups/tstvm/x.json", "backup_root": "/tmp/backups/tstvm"},
                "status": "available",
                "encryption_enabled": True,
                "encryption_key": key,
            }
        )

    def _start(self, **extra: str) -> dict:
        return abr_v41.restore_start(
            {
                "point_id": "rp-1",
                "name": "restoredtstvm",
                "target_inventory_id": self.inventory["id"],
                "target_host": "192.0.2.37",
                "target_datastore": "tstds",
                **extra,
            }
        )

    def _refused(self, **extra: str) -> ApiError:
        with self.assertRaises(ApiError) as caught:
            self._start(**extra)
        return caught.exception

    def test_a_restore_point_key_that_cannot_be_decrypted_is_refused_before_the_session(self) -> None:
        self._seed_point(_foreign(KEY_1))

        error = self._refused()

        self.assertEqual((error.status_code, error.code), (409, "encryption_key_unreadable"))
        self.assertIn("abr restore start --encryption-key", error.message)
        self.assertEqual(TASKS, {})

    def test_a_key_given_with_the_restore_is_used_instead_of_an_unreadable_one(self) -> None:
        self._seed_point(_foreign(KEY_1))

        task = self._start(encryption_key=KEY_1)

        self.assertEqual(TASKS[task["id"]]["parameters"]["encryption_key"], KEY_1)

    def test_a_key_given_while_the_key_file_is_unusable_is_refused_rather_than_stored_in_clear(self) -> None:
        self._seed_point(KEY_1)
        self.key_path.write_bytes(b"")
        credential_encryption.reset_cipher()

        error = self._refused(encryption_key=KEY_1)

        self.assertEqual((error.status_code, error.code), (503, "credential_key_unavailable"))
        self.assertEqual(TASKS, {})

    def test_a_readable_copy_on_the_point_is_used_when_the_job_key_is_unreadable(self) -> None:
        JOBS["job-1"] = {
            "id": "job-1",
            "name": "encrypted-job",
            "job_type": "backup",
            "task_parameters": {
                "encryption_enabled": True,
                "encryption_key": _foreign(KEY_1),
                "encryption_keys": [{"key_id": 0, "key": _foreign(KEY_1)}],
            },
        }
        self._seed_point(KEY_1)
        BACKUPS["rp-1"].update({"job_id": "job-1", "encryption_key_id": 0})

        task = self._start()

        self.assertEqual(TASKS[task["id"]]["parameters"]["encryption_key"], KEY_1)

    def test_a_restore_with_the_stored_key_still_starts(self) -> None:
        self._seed_point(KEY_1)

        task = self._start()

        self.assertEqual(TASKS[task["id"]]["parameters"]["encryption_key"], KEY_1)


if __name__ == "__main__":
    unittest.main()
